"""Bounded owner-facing CI repair details and signed access links."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import posixpath
import re
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable
from urllib.parse import quote, urlparse

from pr_agent.config_loader import get_settings
from pr_agent.triage.failure_explanations import sanitize_failure_text

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|private[_-]?token|authorization|password|secret)\b"
    r"\s*[:=]\s*(?:bearer\s+)?\S+"
)
_CONFIDENCE_VALUES = frozenset({"confirmed", "inferred", "unknown"})
_ACTION_STATUS_VALUES = frozenset({
    "planned",
    "diagnosing",
    "editing",
    "committed",
    "verified",
    "failed",
    "no_changes",
})
_PHASE_VALUES = frozenset({
    "queued",
    "preparing",
    "diagnosing",
    "editing",
    "committing",
    "waiting_pipeline",
    "validating",
    "terminal",
})
_CATEGORY_VALUES = frozenset({"format", "clang", "build", "unknown"})
_DIFF_LINE_KINDS = frozenset({"context", "addition", "deletion"})
_FILE_CHANGE_TYPES = frozenset({"added", "modified", "deleted", "renamed"})
_PATCH_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$")
_OWNER_METADATA_KEYS = frozenset({
    "pipeline_id",
    "commit_sha",
    "changed_files_count",
    "pipeline_status",
    "coverage",
    "attempt",
    "root_cause_group_id",
})


def _enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return str(value or "").strip().lower() in {"true", "1", "yes", "on"}


def repair_details_enabled() -> bool:
    env_value = os.getenv("PR_AGENT_REPAIR_DETAILS_ENABLED")
    value = env_value if env_value is not None else get_settings().get("FEISHU.REPAIR_DETAILS_ENABLED", False)
    return _enabled(value)


def repair_details_event_limit() -> int:
    return max(20, int(get_settings().get("FEISHU.REPAIR_DETAILS_EVENT_LIMIT", 200) or 200))


def repair_details_retention_seconds() -> int:
    return max(3600, int(get_settings().get("FEISHU.REPAIR_DETAILS_RETENTION_SECONDS", 2_592_000) or 2_592_000))


def repair_details_heartbeat_seconds() -> int:
    return max(5, int(get_settings().get("FEISHU.REPAIR_DETAILS_HEARTBEAT_SECONDS", 15) or 15))


def _public_base_url() -> str:
    value = (
        os.getenv("PR_AGENT_REPAIR_DETAILS_BASE_URL")
        or str(get_settings().get("FEISHU.REPAIR_DETAILS_BASE_URL", "") or "")
    ).strip().rstrip("/")
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _signing_secret() -> bytes:
    return (os.getenv("PR_AGENT_REPAIR_DETAILS_SIGNING_SECRET") or "").encode("utf-8")


def _valid_task_id(task_id: str) -> bool:
    return bool(_TASK_ID_RE.fullmatch(str(task_id or "")))


def sign_repair_details_task(task_id: str) -> str:
    secret = _signing_secret()
    if not secret or not _valid_task_id(task_id):
        return ""
    digest = hmac.new(secret, f"v1:{task_id}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_repair_details_signature(task_id: str, signature: str) -> bool:
    expected = sign_repair_details_task(task_id)
    return bool(expected and signature and hmac.compare_digest(expected, str(signature)))


def build_repair_details_url(task_id: str) -> str:
    if not repair_details_enabled():
        return ""
    base_url = _public_base_url()
    signature = sign_repair_details_task(task_id)
    if not base_url or not signature:
        return ""
    return f"{base_url}/repair-results/{quote(task_id, safe='')}?sig={quote(signature, safe='')}"


def sanitize_repair_text(value: object, limit: int = 400) -> str:
    text = _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", str(value or ""))
    return sanitize_failure_text(text, limit)


def _safe_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path or path.startswith("/") or "\x00" in path:
        return ""
    normalized = posixpath.normpath(path)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return ""
    return sanitize_repair_text(normalized, 300)


def _unique_text(values: Iterable[object], *, limit: int, item_limit: int = 40) -> tuple[str, ...]:
    output = []
    for value in values:
        text = sanitize_repair_text(value, limit)
        if text and text not in output:
            output.append(text)
        if len(output) >= item_limit:
            break
    return tuple(output)


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: object, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_metadata(value: object) -> dict[str, str | int | float | bool]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, str | int | float | bool] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key not in _OWNER_METADATA_KEYS or not isinstance(raw_value, (str, int, float, bool)):
            continue
        output[key] = sanitize_repair_text(raw_value, 160) if isinstance(raw_value, str) else raw_value
    return output


@dataclass(frozen=True)
class RepairProgressEvent:
    task_id: str
    phase: str
    summary: str
    occurred_at: float
    categories: tuple[str, ...] = ()
    job_names: tuple[str, ...] = ()
    metadata: dict[str, str | int | float | bool] = None
    event_id: str = ""

    @classmethod
    def new(
        cls,
        task_id: str,
        phase: str,
        summary: str,
        *,
        categories: Iterable[str] = (),
        job_names: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        occurred_at: float | None = None,
    ) -> "RepairProgressEvent":
        normalized_phase = str(phase or "")
        return cls(
            task_id=sanitize_repair_text(task_id, 128),
            phase=normalized_phase if normalized_phase in _PHASE_VALUES else "diagnosing",
            summary=sanitize_repair_text(summary, 240),
            occurred_at=float(occurred_at if occurred_at is not None else time.time()),
            categories=tuple(
                category for category in _unique_text(categories, limit=32, item_limit=4)
                if category in _CATEGORY_VALUES
            ),
            job_names=_unique_text(job_names, limit=120, item_limit=20),
            metadata=_safe_metadata(metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "phase": self.phase,
            "summary": self.summary,
            "occurred_at": self.occurred_at,
            "categories": list(self.categories),
            "job_names": list(self.job_names),
            "metadata": dict(self.metadata or {}),
            "event_id": self.event_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def with_event_id(self, event_id: str) -> "RepairProgressEvent":
        return replace(self, event_id=sanitize_repair_text(event_id, 64))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairProgressEvent":
        event = cls.new(
            str(value.get("task_id") or ""),
            str(value.get("phase") or ""),
            str(value.get("summary") or ""),
            categories=value.get("categories") or (),
            job_names=value.get("job_names") or (),
            metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else {},
            occurred_at=_safe_float(value.get("occurred_at"), time.time()),
        )
        return event.with_event_id(str(value.get("event_id") or ""))

    @classmethod
    def from_json(cls, value: str) -> "RepairProgressEvent":
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("repair progress event must be an object")
        return cls.from_dict(decoded)


@dataclass(frozen=True)
class RepairDiffLine:
    kind: str
    old_line: int | None
    new_line: int | None
    content: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairDiffLine | None":
        kind = str(value.get("kind") or "")
        if kind not in _DIFF_LINE_KINDS:
            return None
        return cls(
            kind=kind,
            old_line=_safe_optional_int(value.get("old_line")),
            new_line=_safe_optional_int(value.get("new_line")),
            content=sanitize_repair_text(value.get("content"), 500),
        )


@dataclass(frozen=True)
class RepairDiffHunk:
    old_start: int = 0
    new_start: int = 0
    header: str = ""
    lines: tuple[RepairDiffLine, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_start": self.old_start,
            "new_start": self.new_start,
            "header": self.header,
            "lines": [line.to_dict() for line in self.lines],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairDiffHunk":
        lines = []
        for raw_line in value.get("lines") or ():
            if not isinstance(raw_line, dict):
                continue
            line = RepairDiffLine.from_dict(raw_line)
            if line is not None:
                lines.append(line)
            if len(lines) >= 400:
                break
        return cls(
            old_start=_safe_int(value.get("old_start")),
            new_start=_safe_int(value.get("new_start")),
            header=sanitize_repair_text(value.get("header"), 200),
            lines=tuple(lines),
        )


@dataclass(frozen=True)
class RepairFileChange:
    path: str
    change_type: str = "modified"
    summary: str = ""
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    truncated: bool = False
    omitted_lines: int = 0
    hunks: tuple[RepairDiffHunk, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "change_type": self.change_type,
            "summary": self.summary,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
            "truncated": self.truncated,
            "omitted_lines": self.omitted_lines,
            "hunks": [hunk.to_dict() for hunk in self.hunks],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairFileChange | None":
        path = _safe_path(value.get("path"))
        if not path:
            return None
        change_type = str(value.get("change_type") or "modified")
        hunks = tuple(
            RepairDiffHunk.from_dict(raw_hunk)
            for raw_hunk in (value.get("hunks") or ())[:20]
            if isinstance(raw_hunk, dict)
        )
        return cls(
            path=path,
            change_type=change_type if change_type in _FILE_CHANGE_TYPES else "modified",
            summary=sanitize_repair_text(value.get("summary"), 400),
            additions=_safe_int(value.get("additions")),
            deletions=_safe_int(value.get("deletions")),
            binary=bool(value.get("binary")),
            truncated=bool(value.get("truncated")),
            omitted_lines=_safe_int(value.get("omitted_lines")),
            hunks=hunks,
        )


def repair_file_change_from_patch(
    path: str,
    patch: str,
    *,
    change_type: str = "modified",
    truncated: bool = False,
    omitted_lines: int = 0,
) -> RepairFileChange | None:
    """Convert one bounded GitLab patch into the existing owner-page shape."""
    safe_path = _safe_path(path)
    if not safe_path:
        return None
    hunks: list[RepairDiffHunk] = []
    current: list[RepairDiffLine] | None = None
    current_header = ""
    current_old_start = current_new_start = old_line = new_line = 0
    additions = deletions = 0

    def finish_hunk() -> None:
        nonlocal current
        if current is not None:
            hunks.append(RepairDiffHunk(current_old_start, current_new_start, current_header, tuple(current)))
            current = None

    for raw_line in str(patch or "").splitlines():
        match = _PATCH_HUNK_RE.match(raw_line)
        if match:
            finish_hunk()
            current_old_start = old_line = int(match.group(1))
            current_new_start = new_line = int(match.group(3))
            current_header = sanitize_repair_text(match.group(5) or "", 200)
            current = []
            continue
        if current is None or not raw_line or raw_line.startswith("\\ No newline"):
            continue
        prefix = raw_line[0]
        if prefix not in {" ", "+", "-"}:
            continue
        kind = {" ": "context", "+": "addition", "-": "deletion"}[prefix]
        old_value = old_line if kind != "addition" else None
        new_value = new_line if kind != "deletion" else None
        if kind == "addition":
            additions += 1
            new_line += 1
        elif kind == "deletion":
            deletions += 1
            old_line += 1
        else:
            old_line += 1
            new_line += 1
        content = _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", raw_line[1:])
        current.append(RepairDiffLine(kind, old_value, new_value, content.replace("\x00", "")[:500]))
    finish_hunk()
    normalized_type = change_type if change_type in _FILE_CHANGE_TYPES else "modified"
    return RepairFileChange(
        path=safe_path,
        change_type=normalized_type,
        additions=additions,
        deletions=deletions,
        binary=not hunks and bool(patch),
        truncated=truncated,
        omitted_lines=max(0, int(omitted_lines)),
        hunks=tuple(hunks[:20]),
    )


@dataclass(frozen=True)
class RepairAction:
    action_id: str = ""
    root_cause_group_id: str = ""
    categories: tuple[str, ...] = ()
    job_names: tuple[str, ...] = ()
    root_cause: str = ""
    evidence: str = ""
    confidence: str = "unknown"
    measures: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    solution_summary: str = ""
    rationale: str = ""
    file_changes: tuple[RepairFileChange, ...] = ()
    commit_sha: str = ""
    validation_pipeline_id: int = 0
    validation_status: str = ""
    status: str = "planned"
    failure_reason: str = ""
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["categories"] = list(self.categories)
        value["job_names"] = list(self.job_names)
        value["measures"] = list(self.measures)
        value["changed_files"] = list(self.changed_files)
        value["file_changes"] = [item.to_dict() for item in self.file_changes]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairAction":
        confidence = str(value.get("confidence") or "unknown")
        status = str(value.get("status") or "planned")
        paths = tuple(
            dict.fromkeys(
                path for path in (_safe_path(item) for item in value.get("changed_files") or ()) if path
            )
        )
        file_changes = []
        for raw_change in value.get("file_changes") or ():
            if not isinstance(raw_change, dict):
                continue
            change = RepairFileChange.from_dict(raw_change)
            if change is not None:
                file_changes.append(change)
            if len(file_changes) >= 30:
                break
        return cls(
            action_id=sanitize_repair_text(value.get("action_id"), 120),
            root_cause_group_id=sanitize_repair_text(value.get("root_cause_group_id"), 120),
            categories=tuple(
                category for category in _unique_text(value.get("categories") or (), limit=32, item_limit=4)
                if category in _CATEGORY_VALUES
            ),
            job_names=_unique_text(value.get("job_names") or (), limit=120, item_limit=20),
            root_cause=sanitize_repair_text(value.get("root_cause"), 500),
            evidence=sanitize_repair_text(value.get("evidence"), 500),
            confidence=confidence if confidence in _CONFIDENCE_VALUES else "unknown",
            measures=_unique_text(value.get("measures") or (), limit=400, item_limit=12),
            changed_files=paths[:80],
            solution_summary=sanitize_repair_text(value.get("solution_summary"), 500),
            rationale=sanitize_repair_text(value.get("rationale"), 500),
            file_changes=tuple(file_changes),
            commit_sha=sanitize_repair_text(value.get("commit_sha"), 64),
            validation_pipeline_id=_safe_int(value.get("validation_pipeline_id")),
            validation_status=sanitize_repair_text(value.get("validation_status"), 32),
            status=status if status in _ACTION_STATUS_VALUES else "planned",
            failure_reason=sanitize_repair_text(value.get("failure_reason"), 400),
            started_at=sanitize_repair_text(value.get("started_at"), 64),
            completed_at=sanitize_repair_text(value.get("completed_at"), 64),
        )


def merge_repair_actions(
    current: Iterable[RepairAction],
    updates: Iterable[RepairAction | dict[str, Any]],
) -> tuple[RepairAction, ...]:
    output = list(current)
    positions = {
        (action.action_id or action.root_cause_group_id or f"position:{index}"): index
        for index, action in enumerate(output)
    }
    for raw_update in updates:
        update = raw_update if isinstance(raw_update, RepairAction) else RepairAction.from_dict(raw_update)
        identity = update.action_id or update.root_cause_group_id
        if not identity or identity not in positions:
            positions[identity or f"position:{len(output)}"] = len(output)
            output.append(update)
            continue
        index = positions[identity]
        previous = output[index]
        scalar_updates = {}
        for key, value in update.to_dict().items():
            if isinstance(value, list) or value in {"", 0, "unknown", "planned"}:
                continue
            scalar_updates[key] = value
        output[index] = RepairAction.from_dict({
            **previous.to_dict(),
            **scalar_updates,
            "categories": [*previous.categories, *update.categories],
            "job_names": [*previous.job_names, *update.job_names],
            "measures": [*previous.measures, *update.measures],
            "changed_files": [*previous.changed_files, *update.changed_files],
            "file_changes": (
                [item.to_dict() for item in update.file_changes]
                if update.file_changes
                else [item.to_dict() for item in previous.file_changes]
            ),
        })
    return tuple(output)
