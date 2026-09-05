"""Strict, checkpoint-friendly RepairPlan models and pure scheduling reducers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ut_agent.execution_ledger import ExecutionLedger, build_execution_ledger
from ut_agent.native_repair_state import NativeCommitDecision
from ut_agent.repair_progress import build_root_cause_groups

MAX_REPAIR_WORK_ITEMS = 20
MAX_WORK_ITEM_PATHS = 30
MAX_WORK_ITEM_EVIDENCE = 10
MAX_PLAN_VERSIONS = 50
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_SOURCE_PATH = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?(?:/?[^/\s:]+/)*[^/\s:]+\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx|py))"
    r":\d+(?::\d+)?",
    re.IGNORECASE,
)
_BUILD_PATH = re.compile(r"(?P<path>(?:^|\s)(?:[^\s:]+/)?(?:CMakeLists\.txt|package\.xml))", re.IGNORECASE)
_WORK_ITEM_KIND_ORDER = {"build": 0, "coverage": 1, "test": 1, "format": 2, "lint": 2, "merge_check": 3}


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_path(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    if normalized.startswith(("/", "./", "../")) or "\x00" in normalized:
        raise ValueError("repair plan path must be repository-relative")
    pure = PurePosixPath(normalized)
    if not normalized or any(part in {"", ".", "..", ".git"} for part in pure.parts):
        raise ValueError("repair plan path is unsafe")
    return normalized


def _bounded_text(value: str, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _diagnostic_path(text: str) -> str:
    value = str(text or "").replace("\\", "/")
    match = _SOURCE_PATH.search(value) or _BUILD_PATH.search(value)
    if match is None:
        return ""
    path = match.group("path").strip()
    for marker in ("/src/", "/include/", "/tests/", "/test/"):
        if marker in path:
            path = f"{marker.strip('/')}/{path.rsplit(marker, 1)[1]}"
            break
    try:
        return _safe_path(path)
    except ValueError:
        return ""


class RepairWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_item_id: str = Field(min_length=1, max_length=80)
    job_names: tuple[str, ...] = Field(min_length=1, max_length=20)
    kind: str = Field(min_length=1, max_length=40)
    required_tool: str = Field(min_length=1, max_length=80)
    failure_signature: str = Field(min_length=1, max_length=80)
    failure_evidence: tuple[str, ...] = Field(min_length=1, max_length=MAX_WORK_ITEM_EVIDENCE)
    hypothesis: str = Field(default="", max_length=1_000)
    allowed_paths: tuple[str, ...] = Field(default=(), max_length=MAX_WORK_ITEM_PATHS)
    required_checks: tuple[str, ...] = Field(default=("diff_check",), max_length=10)
    status: Literal["pending", "blocked", "superseded", "exhausted"] = "pending"

    @field_validator("work_item_id", "failure_signature")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("invalid Repair Work Item identity")
        return value

    @field_validator("job_names", "failure_evidence", "required_checks")
    @classmethod
    def _validate_bounded_unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_bounded_text(value, 500) for value in values)
        if any(not value for value in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("Repair Work Item values must be non-empty and unique")
        return normalized

    @field_validator("allowed_paths")
    @classmethod
    def _validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_safe_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("Repair Work Item paths must be unique")
        return normalized


class RepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=64, max_length=64)
    lineage_id: str = Field(min_length=64, max_length=64)
    version: int = Field(ge=1, le=MAX_PLAN_VERSIONS)
    project_id: str = Field(min_length=1, max_length=300)
    mr_id: int = Field(ge=0)
    baseline_sha: str = Field(min_length=1, max_length=128)
    source_pipeline_id: int | None = Field(default=None, ge=1)
    source_commit_sha: str = Field(min_length=1, max_length=128)
    source_failure_digest: str = Field(min_length=64, max_length=64)
    evidence_cursor: int = Field(ge=-1)
    created_at: str = Field(min_length=1, max_length=80)
    revision_reason: str = Field(min_length=1, max_length=500)
    planning_mode: Literal["model", "deterministic_fallback"]
    planner_model: str = Field(default="", max_length=200)
    planner_error_code: str = Field(default="", max_length=100)
    work_items: tuple[RepairWorkItem, ...] = Field(min_length=1, max_length=MAX_REPAIR_WORK_ITEMS)

    @model_validator(mode="after")
    def _unique_work_items(self):
        identities = [item.work_item_id for item in self.work_items]
        if len(identities) != len(set(identities)):
            raise ValueError("Repair Work Item identities must be unique")
        return self


class RepairVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=64, max_length=64)
    lineage_id: str = Field(min_length=64, max_length=64)
    plan_version: int = Field(ge=1, le=MAX_PLAN_VERSIONS)
    work_item_id: str = Field(min_length=1, max_length=80)
    baseline_sha: str = Field(min_length=1, max_length=128)
    diff_digest: str = Field(default="", max_length=128)
    verdict: Literal["pass", "replan", "block"]
    causal_alignment: bool
    scope_compliant: bool
    evidence_sufficient: bool
    covered_work_item_ids: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_WORK_ITEMS)
    reason: str = Field(min_length=1, max_length=1_000)
    risks: tuple[str, ...] = Field(default=(), max_length=10)
    model: str = Field(default="", max_length=200)
    error_code: str = Field(default="", max_length=100)
    created_at: str = Field(min_length=1, max_length=80)

    @field_validator("covered_work_item_ids")
    @classmethod
    def _validate_coverage(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not _SAFE_ID.fullmatch(value) for value in values):
            raise ValueError("invalid covered Work Item identities")
        return values


@dataclass(frozen=True)
class RepairPlanCommitDecision:
    allowed: bool
    error_code: str = ""
    message: str = ""
    plan_id: str = ""
    diff_digest: str = ""


def latest_failed_pipeline(messages: list) -> tuple[dict[str, Any] | None, ExecutionLedger]:
    ledger = build_execution_ledger(messages)
    pipeline = next((
        value for value in reversed(ledger.pipelines)
        if str(value.get("pipeline_status") or "").lower() == "failed"
    ), None)
    return pipeline, ledger


def source_failure_digest(pipeline: dict[str, Any]) -> str:
    groups = pipeline.get("root_cause_groups") or ()
    if not groups:
        groups = [group.to_dict() for group in build_root_cause_groups(pipeline.get("failed_jobs") or [])]
    payload = [{
        "root_cause_id": str(group.get("root_cause_id") or ""),
        "canonical_diagnostic": _bounded_text(group.get("canonical_diagnostic") or "", 1_000),
        "job_names": sorted(str(name) for name in group.get("job_names") or () if str(name)),
    } for group in groups if isinstance(group, dict)]
    return _digest(payload)


def _pipeline_identity(state: dict, pipeline: dict) -> tuple[str, int | None, str, str]:
    baseline = str(
        pipeline.get("matched_commit_sha")
        or pipeline.get("requested_commit_sha")
        or state.get("commit_sha")
        or ""
    )
    pipeline_id = pipeline.get("validation_pipeline_id") or pipeline.get("pipeline_id")
    try:
        pipeline_id = int(pipeline_id) if pipeline_id not in (None, "") else None
    except (TypeError, ValueError):
        pipeline_id = None
    return baseline, pipeline_id, baseline, source_failure_digest(pipeline)


def _required_checks(kind: str, job_names: tuple[str, ...]) -> tuple[str, ...]:
    checks = ["diff_check"]
    markers = " ".join(job_names).lower()
    if kind in {"format", "lint"}:
        checks.append("lint_check")
    if kind == "build":
        checks.append("build_check")
    if kind in {"coverage", "test"} or "test" in markers or "coverage" in markers:
        checks.append("test_check")
    return tuple(checks)


def _work_items_from_pipeline(pipeline: dict[str, Any]) -> tuple[RepairWorkItem, ...]:
    groups = [group for group in pipeline.get("root_cause_groups") or () if isinstance(group, dict)]
    if not groups:
        groups = [group.to_dict() for group in build_root_cause_groups(pipeline.get("failed_jobs") or [])]
    pipeline_items = [item for item in pipeline.get("work_items") or () if isinstance(item, dict)]
    by_root: dict[str, list[dict]] = {}
    for item in pipeline_items:
        by_root.setdefault(str(item.get("root_cause_id") or ""), []).append(item)

    result = []
    for group in groups[:MAX_REPAIR_WORK_ITEMS]:
        root_id = str(group.get("root_cause_id") or "").strip()
        if not root_id:
            continue
        related = by_root.get(root_id) or ()
        job_names = tuple(dict.fromkeys(
            str(name) for name in group.get("job_names") or () if str(name).strip()
        )) or tuple(dict.fromkeys(
            str(item.get("job_name") or item.get("canonical_job_name") or "")
            for item in related
            if str(item.get("job_name") or item.get("canonical_job_name") or "")
        ))
        if not job_names:
            job_names = (str(group.get("canonical_job_name") or "unknown"),)
        primary = min(
            (item for item in related if item.get("kind")),
            key=lambda item: _WORK_ITEM_KIND_ORDER.get(str(item.get("kind") or ""), 4),
            default=None,
        )
        kind = str((primary or {}).get("kind") or "other")
        required_tool = str((primary or {}).get("required_tool") or "apply_repo_patch_tool")
        if required_tool == "generate_code_tool":
            required_tool = "apply_repo_patch_tool"
        required_checks = list(_required_checks(kind, job_names))
        for related_item in related:
            related_kind = str(related_item.get("kind") or "other")
            related_job_name = str(
                related_item.get("job_name") or related_item.get("canonical_job_name") or ""
            )
            required_checks.extend(_required_checks(related_kind, (related_job_name,) if related_job_name else ()))
        diagnostic = _bounded_text(group.get("canonical_diagnostic") or "", 500)
        path = _diagnostic_path(diagnostic)
        blocker = next((
            item.get("preflight_blocker") for item in related
            if isinstance(item.get("preflight_blocker"), dict)
            and item["preflight_blocker"].get("outcome") == "blocked"
        ), None)
        result.append(RepairWorkItem(
            work_item_id=root_id,
            job_names=job_names,
            kind=kind or "other",
            required_tool=required_tool,
            failure_signature=root_id,
            failure_evidence=(diagnostic or f"Pipeline job failure: {job_names[0]}",),
            hypothesis=diagnostic,
            allowed_paths=(path,) if path else (),
            required_checks=tuple(dict.fromkeys(required_checks)),
            status="blocked" if blocker is not None else "pending",
        ))

    def priority(item: RepairWorkItem) -> tuple[int, str, str]:
        return _WORK_ITEM_KIND_ORDER.get(item.kind, 4), item.job_names[0].lower(), item.work_item_id

    return tuple(sorted(result, key=priority))


def _plan_identifiers(
    state: dict,
    *,
    baseline_sha: str,
    pipeline_id: int | None,
    failure_digest: str,
    version: int,
    work_items: tuple[RepairWorkItem, ...],
) -> tuple[str, str]:
    lineage_payload = {
        "project_id": str(state.get("project_id") or ""),
        "mr_id": int(state.get("mr_id") or 0),
        "baseline_sha": baseline_sha,
        "source_pipeline_id": pipeline_id,
        "source_failure_digest": failure_digest,
    }
    lineage_id = _digest(lineage_payload)
    plan_id = _digest({
        "lineage_id": lineage_id,
        "version": version,
        "work_items": [item.model_dump(mode="json") for item in work_items],
    })
    return lineage_id, plan_id


def build_initial_repair_plan(
    state: dict,
    *,
    now: datetime | None = None,
    hypotheses: dict[str, str] | None = None,
    planning_mode: Literal["model", "deterministic_fallback"] = "deterministic_fallback",
    planner_model: str = "",
    planner_error_code: str = "",
) -> RepairPlan:
    pipeline, ledger = latest_failed_pipeline(state.get("messages", []))
    if pipeline is None:
        raise ValueError("failed Pipeline evidence is required before planning")
    baseline, pipeline_id, source_commit_sha, failure_digest = _pipeline_identity(state, pipeline)
    if not baseline:
        raise ValueError("RepairPlan baseline SHA is unavailable")
    work_items = _work_items_from_pipeline(pipeline)
    if not work_items:
        raise ValueError("failed Pipeline has no plannable Work Items")
    if hypotheses:
        work_items = tuple(
            item.model_copy(update={
                "hypothesis": _bounded_text(hypotheses.get(item.work_item_id) or item.hypothesis, 1_000),
            })
            for item in work_items
        )
    try:
        from ut_agent.config import REPAIR_BACKEND

        native_backend = REPAIR_BACKEND == "native"
    except Exception:
        native_backend = False
    if native_backend:
        from pr_agent.config_loader import get_settings
        from ut_agent.pipeline_reconciliation import native_exhausted_root_ids

        try:
            no_progress_limit = max(1, int(get_settings().get("TRIAGE.NO_PROGRESS_LIMIT", 2)))
        except (TypeError, ValueError):
            no_progress_limit = 2
        exhausted_roots = native_exhausted_root_ids(state, no_progress_limit)
        if exhausted_roots:
            work_items = tuple(
                item.model_copy(update={"status": "exhausted"})
                if item.status == "pending" and item.work_item_id in exhausted_roots
                else item
                for item in work_items
            )
    lineage_id, plan_id = _plan_identifiers(
        state,
        baseline_sha=baseline,
        pipeline_id=pipeline_id,
        failure_digest=failure_digest,
        version=1,
        work_items=work_items,
    )
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    evidence_cursor = max((attempt.sequence for attempt in ledger.tool_attempts), default=-1)
    return RepairPlan(
        plan_id=plan_id,
        lineage_id=lineage_id,
        version=1,
        project_id=str(state.get("project_id") or ""),
        mr_id=int(state.get("mr_id") or 0),
        baseline_sha=baseline,
        source_pipeline_id=pipeline_id,
        source_commit_sha=source_commit_sha,
        source_failure_digest=failure_digest,
        evidence_cursor=evidence_cursor,
        created_at=timestamp,
        revision_reason="initial_pipeline_evidence",
        planning_mode=planning_mode,
        planner_model=planner_model,
        planner_error_code=planner_error_code,
        work_items=work_items,
    )


def latest_repair_plan(state: dict) -> RepairPlan | None:
    for raw in reversed(state.get("repair_plans") or ()):
        try:
            return RepairPlan.model_validate(raw)
        except (TypeError, ValueError):
            continue
    return None


def latest_repair_verification(state: dict) -> RepairVerification | None:
    for raw in reversed(state.get("repair_verifications") or ()):
        try:
            return RepairVerification.model_validate(raw)
        except (TypeError, ValueError):
            continue
    return None


def verification_matches_plan(plan: RepairPlan, verification: RepairVerification) -> bool:
    """Return whether a verification is fully bound to one exact plan and its Work Items."""
    plan_ids = {item.work_item_id for item in plan.work_items}
    covered_ids = set(verification.covered_work_item_ids)
    return bool(
        covered_ids
        and verification.plan_id == plan.plan_id
        and verification.lineage_id == plan.lineage_id
        and verification.plan_version == plan.version
        and verification.baseline_sha == plan.baseline_sha
        and verification.work_item_id in plan_ids
        and verification.work_item_id in covered_ids
        and covered_ids.issubset(plan_ids)
    )


def plan_matches_latest_pipeline(state: dict, plan: RepairPlan) -> bool:
    pipeline, _ledger = latest_failed_pipeline(state.get("messages", []))
    if pipeline is None:
        return False
    baseline, pipeline_id, source_commit_sha, failure_digest = _pipeline_identity(state, pipeline)
    return (
        plan.project_id == str(state.get("project_id") or "")
        and plan.mr_id == int(state.get("mr_id") or 0)
        and plan.baseline_sha == baseline
        and plan.source_pipeline_id == pipeline_id
        and plan.source_commit_sha == source_commit_sha
        and plan.source_failure_digest == failure_digest
    )


def repair_plan_required(state: dict) -> bool:
    if state.get("trigger_type") != "pipeline_failed":
        return False
    pipeline, _ledger = latest_failed_pipeline(state.get("messages", []))
    if pipeline is None or not (pipeline.get("failed_jobs") or pipeline.get("root_cause_groups")):
        return False
    plan = latest_repair_plan(state)
    return plan is None or not plan_matches_latest_pipeline(state, plan)


def _valid_lineage_verifications(state: dict, plan: RepairPlan) -> tuple[RepairVerification, ...]:
    historical_plans = {
        (plan.plan_id, plan.lineage_id, plan.version, plan.baseline_sha): plan,
    }
    for raw in state.get("repair_plans") or ():
        try:
            candidate = RepairPlan.model_validate(raw)
        except (TypeError, ValueError):
            continue
        if candidate.lineage_id != plan.lineage_id:
            continue
        historical_plans[(
            candidate.plan_id,
            candidate.lineage_id,
            candidate.version,
            candidate.baseline_sha,
        )] = candidate

    values = []
    for raw in state.get("repair_verifications") or ():
        try:
            verification = RepairVerification.model_validate(raw)
        except (TypeError, ValueError):
            continue
        source_plan = historical_plans.get((
            verification.plan_id,
            verification.lineage_id,
            verification.plan_version,
            verification.baseline_sha,
        ))
        if source_plan is not None and verification_matches_plan(source_plan, verification):
            values.append(verification)
    return tuple(values)


def completed_work_item_ids(state: dict, plan: RepairPlan | None = None) -> frozenset[str]:
    current = plan or latest_repair_plan(state)
    if current is None:
        return frozenset()
    completed = {
        item.work_item_id
        for item in current.work_items
        if item.status in {"blocked", "superseded", "exhausted"}
    }
    current_ids = {item.work_item_id for item in current.work_items}
    for verification in _valid_lineage_verifications(state, current):
        if (
            verification.verdict == "pass"
            and verification.causal_alignment
            and verification.scope_compliant
            and verification.evidence_sufficient
        ):
            completed.update(set(verification.covered_work_item_ids) & current_ids)
        elif verification.verdict == "block":
            completed.add(verification.work_item_id)
    return frozenset(completed)


def blocked_work_item_ids(state: dict, plan: RepairPlan | None = None) -> frozenset[str]:
    current = plan or latest_repair_plan(state)
    if current is None:
        return frozenset()
    blocked = {item.work_item_id for item in current.work_items if item.status == "blocked"}
    current_ids = {item.work_item_id for item in current.work_items}
    for verification in _valid_lineage_verifications(state, current):
        if verification.verdict == "block" and verification.work_item_id in current_ids:
            blocked.add(verification.work_item_id)
    return frozenset(blocked)


def active_work_item(state: dict) -> RepairWorkItem | None:
    plan = latest_repair_plan(state)
    if plan is None or not plan_matches_latest_pipeline(state, plan):
        return None
    completed = completed_work_item_ids(state, plan)
    return next((
        item for item in plan.work_items
        if item.status == "pending" and item.work_item_id not in completed
    ), None)


def required_verification_work_item_ids(
    state: dict,
    plan: RepairPlan | None = None,
) -> tuple[str, ...]:
    """Return the Work Items that the next verification must cover.

    Intermediate verification may close only the active item.  Once the active
    item is the last unfinished item, the final verification must cover every
    executable item against the same cumulative Diff.
    """
    current_plan = plan or latest_repair_plan(state)
    current_item = active_work_item(state)
    if current_plan is None or current_item is None:
        return ()
    executable = tuple(
        item.work_item_id
        for item in current_plan.work_items
        if item.status == "pending"
    )
    completed = completed_work_item_ids(state, current_plan)
    current_is_final = all(
        work_item_id == current_item.work_item_id or work_item_id in completed
        for work_item_id in executable
    )
    return executable if current_is_final else (current_item.work_item_id,)


def plan_scoped_attempts(state: dict, ledger: ExecutionLedger | None = None) -> list:
    """Return only tool facts created after the current plan's evidence boundary."""
    current = latest_repair_plan(state)
    facts = ledger or build_execution_ledger(state.get("messages", []))
    if current is None:
        return list(facts.tool_attempts)
    return [attempt for attempt in facts.tool_attempts if attempt.sequence > current.evidence_cursor]


def repair_plan_commit_decision(
    state: dict,
    native_decision: NativeCommitDecision,
) -> RepairPlanCommitDecision:
    plan = latest_repair_plan(state)
    if plan is None or not plan_matches_latest_pipeline(state, plan):
        return RepairPlanCommitDecision(False, "repair_plan_missing_or_stale", "当前失败快照缺少有效 RepairPlan。")
    if not native_decision.allowed:
        return RepairPlanCommitDecision(
            False,
            native_decision.error_code or "native_commit_gate_failed",
            native_decision.message,
            plan.plan_id,
        )
    if native_decision.validated_base_sha != plan.baseline_sha:
        return RepairPlanCommitDecision(
            False,
            "repair_baseline_mismatch",
            "当前验证结果的基线 SHA 与 RepairPlan 不一致。",
            plan.plan_id,
            native_decision.validated_diff_digest,
        )
    blocked = blocked_work_item_ids(state, plan)
    if blocked:
        return RepairPlanCommitDecision(
            False,
            "repair_plan_contains_blocked_items",
            "RepairPlan 仍包含无法安全修复的 Work Item。",
            plan.plan_id,
            native_decision.validated_diff_digest,
        )
    if active_work_item(state) is not None:
        return RepairPlanCommitDecision(
            False,
            "repair_plan_work_items_pending",
            "RepairPlan 仍有未完成 Work Item。",
            plan.plan_id,
            native_decision.validated_diff_digest,
        )
    verification = latest_repair_verification(state)
    executable_ids = {item.work_item_id for item in plan.work_items if item.status == "pending"}
    valid_verification = (
        verification is not None
        and verification_matches_plan(plan, verification)
        and verification.verdict == "pass"
        and verification.causal_alignment
        and verification.scope_compliant
        and verification.evidence_sufficient
        and verification.diff_digest == native_decision.validated_diff_digest
        and executable_ids.issubset(set(verification.covered_work_item_ids))
    )
    if not valid_verification:
        return RepairPlanCommitDecision(
            False,
            "repair_verification_missing_or_stale",
            "当前 RepairPlan 与 Diff 尚未通过完整独立验收。",
            plan.plan_id,
            native_decision.validated_diff_digest,
        )
    return RepairPlanCommitDecision(
        True,
        plan_id=plan.plan_id,
        diff_digest=native_decision.validated_diff_digest,
    )


def repair_plan_audit(state: dict) -> dict[str, Any]:
    plan = latest_repair_plan(state)
    if plan is None:
        return {}
    completed = completed_work_item_ids(state, plan)
    blocked = blocked_work_item_ids(state, plan)
    exhausted = {item.work_item_id for item in plan.work_items if item.status == "exhausted"}
    active = active_work_item(state)
    return {
        "plan_id": plan.plan_id,
        "lineage_id": plan.lineage_id,
        "version": plan.version,
        "source_pipeline_id": plan.source_pipeline_id,
        "work_item_count": len(plan.work_items),
        "completed_work_item_count": len(completed),
        "blocked_work_item_count": len(blocked),
        "exhausted_work_item_count": len(exhausted),
        "active_work_item_id": active.work_item_id if active is not None else "",
        "replan_count": max(0, plan.version - 1),
        "planning_mode": plan.planning_mode,
        "planner_error_code": plan.planner_error_code,
    }


def normalize_repair_path(path: str) -> str:
    """Public path normalization used by tool policy and replanning."""
    return _safe_path(path)


def plan_identity_for_revision(
    plan: RepairPlan,
    work_items: tuple[RepairWorkItem, ...],
    version: int,
) -> str:
    return _digest({
        "lineage_id": plan.lineage_id,
        "version": version,
        "work_items": [item.model_dump(mode="json") for item in work_items],
    })
