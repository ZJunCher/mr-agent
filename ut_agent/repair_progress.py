"""Deterministic root-cause grouping and Hermes progress budgets."""

import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from ut_agent.blocker_evidence import validate_blocker_record

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TIMESTAMP_RE = re.compile(
    r"(?:"
    r"(?<!\d)\d{4}-\d{2}-\d{2}[T ](?:\d{2}:\d{2}:\d{2}|\d{6})"
    r"(?:\.\d+)?(?:[zZ]|[+-]\d{2}:?\d{2})?(?![\w.])"
    r"|"
    r"(?<!\d)\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[zZ]|[+-]\d{2}:?\d{2})?(?![\w.])"
    r")"
)
_LINE_COLUMN_RE = re.compile(r":\d+(?::\d+)?(?=[:\s])")
_GENERIC_FAILURE_RE = re.compile(r"job failed|command terminated|exited with code|uploading artifacts", re.IGNORECASE)


@dataclass(frozen=True)
class RootCauseGroup:
    root_cause_id: str
    normalized_diagnostic: str
    canonical_diagnostic: str
    canonical_job_name: str
    job_names: tuple[str, ...]
    job_ids: tuple[int | None, ...]
    pipeline_ids: tuple[int | None, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["job_names"] = list(self.job_names)
        value["job_ids"] = list(self.job_ids)
        value["pipeline_ids"] = list(self.pipeline_ids)
        return value


@dataclass(frozen=True)
class ProgressDecision:
    allowed: bool
    reason_code: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RootCauseProgress:
    root_cause_id: str
    state: str
    repair_attempts: int = 0
    failed_validations: int = 0
    last_commit_sha: str = ""

    @property
    def repeat_exhausted(self) -> bool:
        return self.state == "repeat_exhausted"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _RepairAttempt:
    group_id: str
    operation: str
    status: str
    fingerprint: str


def extract_causal_lines(diagnostic: str, *, limit: int = 3) -> list[str]:
    """Return ordered diagnostic observations for legacy ledger callers."""
    from ut_agent.ci_diagnostics import extract_diagnostic_candidates, primary_diagnostic

    result = extract_diagnostic_candidates(diagnostic, limit=max(1, limit))
    if result.candidates:
        primary = primary_diagnostic(result.candidates)
        ordered = ([primary] if primary is not None else []) + [
            item for item in result.candidates if item is not primary
        ]
        return [item.text for item in ordered[:max(1, limit)]]
    lines = [line.strip() for line in _ANSI_RE.sub("", diagnostic or "").splitlines() if line.strip()]
    selected = [line for line in lines if not _GENERIC_FAILURE_RE.search(line)] or lines
    return list(dict.fromkeys(selected))[:max(1, limit)]


def _causal_line(diagnostic: str) -> str:
    causal_lines = extract_causal_lines(diagnostic, limit=1)
    return causal_lines[0] if causal_lines else ""


def normalize_diagnostic(diagnostic: str, *, job_name: str = "") -> str:
    """Normalize volatile CI text while preserving the causal error identity."""
    had_timestamp = bool(_TIMESTAMP_RE.search(str(diagnostic or "")))
    value = _causal_line(diagnostic).replace("\\", "/")
    value = _TIMESTAMP_RE.sub("<time>", value)
    if had_timestamp and "<time>" not in value:
        value = f"<time> {value}"
    value = re.sub(r"(?:[A-Za-z]:)?/[^\s:]+/(src|include|test|tests)/", r"\1/", value)
    value = _LINE_COLUMN_RE.sub("", value)
    if job_name:
        value = re.sub(re.escape(job_name), "<job>", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value[:1000]


def diagnostic_digest(diagnostic: str, *, job_name: str = "") -> str:
    normalized = normalize_diagnostic(diagnostic, job_name=job_name)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def diagnostic_fingerprint(diagnostic: str, *, job_name: str = "") -> str:
    """Return a stable 32-character identity for one normalized diagnostic."""
    normalized = normalize_diagnostic(diagnostic, job_name=job_name)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32] if normalized else ""


def root_cause_id_for(job_name: str, diagnostic: str) -> str:
    normalized = normalize_diagnostic(diagnostic, job_name=job_name)
    identity = normalized or f"job:{job_name.lower()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _canonical_job_name(job_names: list[str]) -> str:
    def priority(name: str) -> tuple[int, str]:
        lowered = name.lower()
        if "build" in lowered:
            return 0, lowered
        if "coverage" in lowered:
            return 1, lowered
        if "format" in lowered:
            return 2, lowered
        return 3, lowered

    return min(job_names, key=priority)


def build_root_cause_groups(failed_jobs: list[dict[str, Any]]) -> list[RootCauseGroup]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    diagnostics: dict[str, str] = {}
    canonical_diagnostics: dict[str, str] = {}
    for job in failed_jobs:
        name = str(job.get("name") or job.get("job_name") or "unknown")
        diagnostic = str(job.get("log_tail") or job.get("diagnostic") or "")
        causal_lines = job.get("causal_lines")
        if isinstance(causal_lines, list) and causal_lines:
            diagnostic = str(causal_lines[0])
        elif job.get("evidence_mode") in {"raw_log_fallback", "evidence_unavailable"}:
            diagnostic = ""
        normalized = normalize_diagnostic(diagnostic, job_name=name)
        identity = normalized or f"job:{name.lower()}:{job.get('status', '')}"
        root_cause_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        grouped.setdefault(root_cause_id, []).append(job)
        diagnostics[root_cause_id] = normalized
        canonical_diagnostics.setdefault(root_cause_id, _causal_line(diagnostic))

    result = []
    for root_cause_id, jobs in grouped.items():
        names = sorted({str(job.get("name") or job.get("job_name") or "unknown") for job in jobs})
        result.append(RootCauseGroup(
            root_cause_id=root_cause_id,
            normalized_diagnostic=diagnostics[root_cause_id],
            canonical_diagnostic=canonical_diagnostics[root_cause_id],
            canonical_job_name=_canonical_job_name(names),
            job_names=tuple(names),
            job_ids=tuple(job.get("job_id") for job in jobs),
            pipeline_ids=tuple(job.get("pipeline_id") for job in jobs),
        ))
    return sorted(result, key=lambda group: (group.canonical_job_name, group.root_cause_id))


def _attempt_root_cause_id(attempt: Any) -> str:
    result = attempt.result if isinstance(getattr(attempt, "result", None), dict) else {}
    args = attempt.args if isinstance(getattr(attempt, "args", None), dict) else {}
    return str(result.get("root_cause_id") or args.get("root_cause_id") or "")


def _pipeline_root_cause_ids(pipelines: list[dict[str, Any]]) -> set[str]:
    return {
        str(group.get("root_cause_id") or "")
        for pipeline in pipelines
        for group in pipeline.get("root_cause_groups") or ()
        if isinstance(group, dict) and group.get("root_cause_id")
    }


def build_root_cause_progress(
    pipelines: list[dict[str, Any]],
    tool_attempts: list[Any],
    no_progress_limit: int = 2,
) -> dict[str, RootCauseProgress]:
    """Correlate changed repairs with pushes and exact-SHA validation results per root cause."""
    roots = _pipeline_root_cause_ids(pipelines)
    ordered_attempts = sorted(tool_attempts, key=lambda attempt: int(getattr(attempt, "sequence", 0)))
    changed_repairs = [
        attempt
        for attempt in ordered_attempts
        if getattr(attempt, "name", "") == "generate_code_tool"
        and (
            (getattr(attempt, "args", {}) or {}).get("operation")
            or (getattr(attempt, "result", {}) or {}).get("operation")
        ) in {"repair", "repair_session"}
        and (getattr(attempt, "result", {}) or {}).get("status") in {
            "changed", "partial_changes", "unexpected_changes"
        }
        and _attempt_root_cause_id(attempt)
    ]
    repair_attempts: Counter[str] = Counter()
    failed_validations: Counter[str] = Counter()
    last_commit_sha: dict[str, str] = {}
    resolved: set[str] = set()
    blocked: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()

    for attempt in ordered_attempts:
        result = attempt.result if isinstance(getattr(attempt, "result", None), dict) else {}
        args = attempt.args if isinstance(getattr(attempt, "args", None), dict) else {}
        root_cause_id = str(result.get("root_cause_id") or args.get("root_cause_id") or "")
        job_name = str(result.get("job_name") or args.get("job_name") or "")
        if (
            root_cause_id
            and result.get("status") == "blocked"
            and validate_blocker_record(result.get("blocker"), job_name) is None
        ):
            roots.add(root_cause_id)
            blocked.add(root_cause_id)

    for index, repair in enumerate(changed_repairs):
        root_cause_id = _attempt_root_cause_id(repair)
        roots.add(root_cause_id)
        repair_attempts[root_cause_id] += 1
        next_repair_sequence = (
            int(changed_repairs[index + 1].sequence)
            if index + 1 < len(changed_repairs)
            else float("inf")
        )
        push = next((
            attempt
            for attempt in ordered_attempts
            if int(repair.sequence) < int(attempt.sequence) < next_repair_sequence
            and getattr(attempt, "name", "") == "commit_and_push_tool"
            and (getattr(attempt, "result", {}) or {}).get("status") == "success"
            and (getattr(attempt, "result", {}) or {}).get("changed") is True
            and (getattr(attempt, "result", {}) or {}).get("commit_sha")
        ), None)
        if push is None:
            continue
        sha = str(push.result.get("commit_sha") or "")
        validation_results = [
            pipeline
            for pipeline in pipelines
            if int(pipeline.get("_sequence") or 0) > int(push.sequence)
            and str(pipeline.get("requested_commit_sha") or "") == sha
            and str(pipeline.get("matched_commit_sha") or "") == sha
            and str(pipeline.get("pipeline_status") or "").lower() not in {
                "", "created", "pending", "preparing", "running", "waiting_for_resource"
            }
        ]
        if not validation_results or (root_cause_id, sha) in seen_pairs:
            continue
        seen_pairs.add((root_cause_id, sha))
        last_commit_sha[root_cause_id] = sha
        validation_roots = _pipeline_root_cause_ids(validation_results)
        if root_cause_id in validation_roots:
            failed_validations[root_cause_id] += 1
        else:
            resolved.add(root_cause_id)

    latest_failed = next((
        pipeline
        for pipeline in reversed(pipelines)
        if str(pipeline.get("pipeline_status") or "").lower() == "failed"
    ), {})
    current_roots = _pipeline_root_cause_ids([latest_failed]) if latest_failed else set()
    progress = {}
    limit = max(1, int(no_progress_limit))
    for root_cause_id in sorted(roots):
        state = "unattempted"
        if root_cause_id in blocked:
            state = "blocked"
        elif failed_validations[root_cause_id] >= limit:
            state = "repeat_exhausted"
        elif root_cause_id in current_roots and repair_attempts[root_cause_id]:
            state = "attempted"
        elif root_cause_id not in current_roots and root_cause_id in resolved:
            state = "resolved"
        progress[root_cause_id] = RootCauseProgress(
            root_cause_id=root_cause_id,
            state=state,
            repair_attempts=repair_attempts[root_cause_id],
            failed_validations=failed_validations[root_cause_id],
            last_commit_sha=last_commit_sha.get(root_cause_id, ""),
        )
    return progress


def workspace_diff_digest(repo_dir: str) -> str:
    """Hash tracked diff plus untracked file contents without changing the index."""
    digest = hashlib.sha256()
    try:
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            timeout=30,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if diff.returncode != 0 or status.returncode != 0:
        return ""
    digest.update(diff.stdout)
    for line in status.stdout.splitlines():
        digest.update(line.encode("utf-8", errors="replace"))
        if not line.startswith("?? "):
            continue
        relative_path = line[3:]
        path = os.path.realpath(os.path.join(repo_dir, relative_path))
        if not path.startswith(os.path.realpath(repo_dir) + os.sep) or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
        except OSError:
            continue
    return digest.hexdigest()


def build_progress_fingerprint(
    *,
    operation: str,
    root_cause_id: str,
    diagnostic: str,
    job_name: str,
    changed_files: list[str],
    diff_digest: str,
) -> str:
    value = {
        "operation": operation,
        "root_cause_id": root_cause_id,
        "evidence_digest": diagnostic_digest(diagnostic, job_name=job_name),
        "changed_files": sorted(changed_files),
        "diff_digest": diff_digest,
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _tool_calls(message) -> list[dict[str, Any]]:
    return message.get("tool_calls", []) if isinstance(message, dict) else getattr(message, "tool_calls", [])


def _tool_call_id(message) -> str:
    return str(message.get("tool_call_id", "")) if isinstance(message, dict) else str(
        getattr(message, "tool_call_id", "")
    )


def _content(message) -> str:
    return str(message.get("content", "")) if isinstance(message, dict) else str(getattr(message, "content", ""))


def _generate_attempts(messages: list) -> list[_RepairAttempt]:
    calls: dict[str, dict[str, Any]] = {}
    attempts = []
    for message in messages:
        for tool_call in _tool_calls(message):
            name = tool_call.get("name") or tool_call.get("function", {}).get("name")
            if name != "generate_code_tool":
                continue
            args = tool_call.get("args")
            if args is None:
                args = tool_call.get("function", {}).get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls[str(tool_call.get("id") or "")] = args if isinstance(args, dict) else {}

        args = calls.get(_tool_call_id(message))
        if args is None:
            continue
        try:
            result = json.loads(_content(message))
        except (json.JSONDecodeError, TypeError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        group_id = str(
            result.get("root_cause_id")
            or args.get("root_cause_id")
            or args.get("job_name")
            or "unknown"
        )
        fingerprint = str(result.get("progress_fingerprint") or "")
        if not fingerprint:
            encoded = json.dumps(
                {
                    "status": result.get("status"),
                    "diagnostic": result.get("diagnostic"),
                    "changed_files": result.get("changed_files") or [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        attempts.append(_RepairAttempt(
            group_id=group_id,
            operation=str(args.get("operation") or result.get("operation") or ""),
            status=str(result.get("status") or ""),
            fingerprint=fingerprint,
        ))
    return attempts


def _positive_setting(name: str, default: int) -> int:
    from pr_agent.config_loader import get_settings

    try:
        value = int(get_settings().get(f"TRIAGE.{name.upper()}", default))
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def evaluate_hermes_budget(state: dict, tool_name: str, tool_args: dict[str, Any]) -> ProgressDecision:
    if tool_name != "generate_code_tool":
        return ProgressDecision(True)
    attempts = _generate_attempts(state.get("messages", []))
    group_id = str(tool_args.get("root_cause_id") or tool_args.get("job_name") or "unknown")
    group_attempts = [attempt for attempt in attempts if attempt.group_id == group_id]
    investigations = sum(attempt.operation == "investigate" for attempt in group_attempts)
    if (
        tool_args.get("operation") == "investigate"
        and investigations >= _positive_setting("max_investigations_per_group", 2)
    ):
        return ProgressDecision(
            False,
            "investigation_limit",
            f"根因组 {group_id} 已达到 2 次调查上限；请根据现有证据修复或结束。",
        )
    if len(attempts) >= _positive_setting("max_hermes_calls_total", 12):
        return ProgressDecision(False, "hermes_total_limit", "系统拒绝继续调用 Hermes：已达到本次任务总调用上限。")
    if len(group_attempts) >= _positive_setting("max_hermes_calls_per_group", 4):
        return ProgressDecision(False, "hermes_group_limit", f"系统拒绝继续处理根因组 {group_id}：已达到调用上限。")

    no_progress_statuses = {
        "repair_no_changes",
        "investigated",
        "incomplete",
        "coding_infra_error",
        "no_changes",
        "investigation_timeout",
        "repair_timeout",
    }
    consecutive = 0
    fingerprint = ""
    for attempt in reversed(group_attempts):
        if attempt.status not in no_progress_statuses:
            break
        if not fingerprint:
            fingerprint = attempt.fingerprint
        if attempt.fingerprint != fingerprint:
            break
        consecutive += 1
    if consecutive >= _positive_setting("no_progress_limit", 2):
        return ProgressDecision(
            False,
            "no_progress_limit",
            f"系统拒绝继续处理根因组 {group_id}：连续 {consecutive} 次 Hermes 调用没有产生新证据或代码变化。",
        )

    if tool_args.get("operation") == "verify_blocker":
        invalid_blockers = sum(
            attempt.operation == "verify_blocker" and attempt.status == "incomplete"
            for attempt in group_attempts
        )
        if invalid_blockers > _positive_setting("max_blocker_corrections", 1):
            return ProgressDecision(
                False,
                "blocker_contract_failed",
                f"根因组 {group_id} 连续未能生成有效阻塞证据，停止重复校正。",
            )
    return ProgressDecision(True)
