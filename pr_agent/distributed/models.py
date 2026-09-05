import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from pr_agent.triage.failure_explanations import FailureExplanation

SCHEMA_VERSION = 1
TERMINAL_PIPELINE_STATUSES = {"canceled", "failed", "skipped", "success"}


class TaskStatus(StrEnum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    WAITING_PIPELINE = "waiting_pipeline"
    PAUSED_BY_TRIAGE = "paused_by_triage"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_TASK_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED})


class TaskKind(StrEnum):
    PR_COMMAND = "pr_command"
    AUTO_WORKFLOW = "auto_workflow"
    GITLAB_EVENT = "gitlab_event"
    REPAIR_ROLLBACK = "repair_rollback"
    REPAIR_REPORT = "repair_report"
    POST_REPAIR_UT = "post_repair_ut"


@dataclass(frozen=True)
class AutoWorkflowDecision:
    allowed: bool
    reason_code: str = ""
    reason: str = ""

    @classmethod
    def allow(cls) -> "AutoWorkflowDecision":
        return cls(True)

    @classmethod
    def skip(cls, reason_code: str, reason: str) -> "AutoWorkflowDecision":
        if not reason_code:
            raise ValueError("reason_code is required for a skipped workflow")
        return cls(False, reason_code, reason)


class DeliveryKind(StrEnum):
    EXECUTE = "execute"
    RESUME_PIPELINE = "resume_pipeline"
    RESUME_AUTO = "resume_auto"


class PipelineResumeClaim(StrEnum):
    CLAIMED = "claimed"
    DUPLICATE = "duplicate"
    STALE = "stale"
    LOST_LEASE = "lost_lease"


class TriageCardState(StrEnum):
    PIPELINE_FAILED = "pipeline_failed"
    REPAIR_QUEUED = "repair_queued"
    REPAIR_RUNNING = "repair_running"
    WAITING_PIPELINE = "waiting_pipeline"
    REPAIR_SUCCEEDED = "repair_succeeded"
    REPAIR_PARTIAL = "repair_partial"
    REPAIR_BLOCKED = "repair_blocked"
    REPAIR_MODEL_UNAVAILABLE = "repair_model_unavailable"
    REPAIR_FAILED = "repair_failed"
    CANCELING = "canceling"
    CANCELED = "canceled"
    ROLLBACK_QUEUED = "rollback_queued"
    ROLLBACK_RUNNING = "rollback_running"
    ROLLBACK_SUCCEEDED = "rollback_succeeded"
    ROLLBACK_FAILED = "rollback_failed"


class RepairCategory(StrEnum):
    PIPELINE = "pipeline"
    FORMAT = "format"
    CLANG = "clang"
    BUILD = "build"
    UNKNOWN = "unknown"


class RepairItemStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_PIPELINE = "waiting_pipeline"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    RESOLVED = "resolved"


class PostRepairUTStatus(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_PIPELINE = "waiting_pipeline"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    CANCELING = "canceling"
    CANCELED = "canceled"
    ROLLBACK_FAILED = "rollback_failed"


@dataclass(frozen=True)
class PostRepairUTState:
    status: PostRepairUTStatus = PostRepairUTStatus.IDLE
    task_id: str = ""
    origin_repair_task_id: str = ""
    baseline_pipeline_id: int = 0
    baseline_sha: str = ""
    coverage_before: float | None = None
    coverage_status_before: str = ""
    current_pipeline_id: int = 0
    current_sha: str = ""
    coverage_after: float | None = None
    status_markdown: str = ""
    outcome_reason: str = ""
    rollback_task_id: str = ""
    rollback_status: str = ""
    rollback_commit_sha: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "PostRepairUTState":
        value = value or {}
        return cls(
            status=PostRepairUTStatus(value.get("status") or PostRepairUTStatus.IDLE.value),
            task_id=str(value.get("task_id") or ""),
            origin_repair_task_id=str(value.get("origin_repair_task_id") or ""),
            baseline_pipeline_id=int(value.get("baseline_pipeline_id") or 0),
            baseline_sha=str(value.get("baseline_sha") or ""),
            coverage_before=(
                float(value["coverage_before"]) if value.get("coverage_before") not in {None, ""} else None
            ),
            coverage_status_before=str(value.get("coverage_status_before") or ""),
            current_pipeline_id=int(value.get("current_pipeline_id") or 0),
            current_sha=str(value.get("current_sha") or ""),
            coverage_after=(
                float(value["coverage_after"]) if value.get("coverage_after") not in {None, ""} else None
            ),
            status_markdown=str(value.get("status_markdown") or ""),
            outcome_reason=str(value.get("outcome_reason") or ""),
            rollback_task_id=str(value.get("rollback_task_id") or ""),
            rollback_status=str(value.get("rollback_status") or ""),
            rollback_commit_sha=str(value.get("rollback_commit_sha") or ""),
        )


TERMINAL_TRIAGE_CARD_STATES = frozenset({
    TriageCardState.REPAIR_SUCCEEDED,
    TriageCardState.REPAIR_PARTIAL,
    TriageCardState.REPAIR_BLOCKED,
    TriageCardState.REPAIR_MODEL_UNAVAILABLE,
    TriageCardState.REPAIR_FAILED,
    TriageCardState.CANCELED,
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _from_json(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("serialized value must be a JSON object")
    return decoded


def _validate_schema_version(value: dict[str, Any]) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {value.get('schema_version')!r}")


@dataclass(frozen=True)
class MrKey:
    project_id: str
    iid: int

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("project_id is required")
        if self.iid <= 0:
            raise ValueError("iid must be positive")

    @property
    def redis_id(self) -> str:
        return f"{quote(self.project_id, safe='')}:{self.iid}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MrKey":
        return cls(project_id=str(value["project_id"]), iid=int(value["iid"]))


@dataclass(frozen=True)
class TaskEnvelope:
    schema_version: int
    task_id: str
    kind: TaskKind
    source: str
    mr: MrKey | None
    pr_url: str
    command: str
    payload: dict[str, Any]
    idempotency_key: str
    created_at: str

    @classmethod
    def new(
        cls,
        *,
        kind: TaskKind,
        source: str,
        mr: MrKey | None,
        pr_url: str,
        command: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> "TaskEnvelope":
        return cls(
            schema_version=SCHEMA_VERSION,
            task_id=uuid4().hex,
            kind=kind,
            source=source,
            mr=mr,
            pr_url=pr_url,
            command=command,
            payload=payload,
            idempotency_key=idempotency_key,
            created_at=_utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        return value

    def to_json(self) -> str:
        return _to_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskEnvelope":
        _validate_schema_version(value)
        mr = value.get("mr")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        return cls(
            schema_version=SCHEMA_VERSION,
            task_id=str(value["task_id"]),
            kind=TaskKind(value["kind"]),
            source=str(value["source"]),
            mr=MrKey.from_dict(mr) if isinstance(mr, dict) else None,
            pr_url=str(value["pr_url"]),
            command=str(value["command"]),
            payload=payload,
            idempotency_key=str(value["idempotency_key"]),
            created_at=str(value["created_at"]),
        )

    @classmethod
    def from_json(cls, value: str) -> "TaskEnvelope":
        return cls.from_dict(_from_json(value))


@dataclass(frozen=True)
class IngressDelivery:
    message_id: str
    task_id: str


@dataclass(frozen=True)
class InboxDelivery:
    message_id: str
    task: TaskEnvelope
    kind: DeliveryKind
    payload: dict[str, Any]


@dataclass(frozen=True)
class PipelineEvent:
    schema_version: int
    project_id: str
    pipeline_id: int
    sha: str
    status: str
    ref: str
    occurred_at: str
    source: str = ""

    @classmethod
    def new(
        cls,
        *,
        project_id: str,
        pipeline_id: int,
        sha: str,
        status: str,
        ref: str,
        occurred_at: str | None = None,
        source: str = "",
    ) -> "PipelineEvent":
        return cls(SCHEMA_VERSION, project_id, pipeline_id, sha, status, ref, occurred_at or _utc_now(), source)

    @classmethod
    def from_gitlab_payload(cls, payload: dict[str, Any]) -> "PipelineEvent":
        attributes = payload.get("object_attributes") or {}
        project = payload.get("project") or {}
        raw_pipeline_id = attributes.get("id") or payload.get("id") or 0
        project_id = str(
            project.get("path_with_namespace")
            or project.get("id")
            or payload.get("project_id")
            or attributes.get("project_id")
            or ""
        )
        return cls.new(
            project_id=project_id,
            pipeline_id=int(raw_pipeline_id),
            sha=str(attributes.get("sha") or payload.get("sha") or ""),
            status=str(attributes.get("status") or payload.get("status") or ""),
            ref=str(attributes.get("ref") or payload.get("ref") or ""),
            source=str(attributes.get("source") or payload.get("source") or ""),
        )

    @property
    def terminal(self) -> bool:
        return self.status.lower() in TERMINAL_PIPELINE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return _to_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PipelineEvent":
        _validate_schema_version(value)
        return cls(
            schema_version=SCHEMA_VERSION,
            project_id=str(value["project_id"]),
            pipeline_id=int(value["pipeline_id"]),
            sha=str(value["sha"]),
            status=str(value["status"]),
            ref=str(value["ref"]),
            occurred_at=str(value["occurred_at"]),
            source=str(value.get("source", "")),
        )

    @classmethod
    def from_json(cls, value: str) -> "PipelineEvent":
        return cls.from_dict(_from_json(value))


@dataclass(frozen=True)
class PipelineWaitIdentity:
    project_id: str
    sha: str
    attempt_id: str
    pipeline_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return _to_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PipelineWaitIdentity":
        pipeline_id = value.get("pipeline_id")
        return cls(
            project_id=str(value.get("project_id", "")),
            sha=str(value.get("sha", "")),
            attempt_id=str(value.get("attempt_id", "")),
            pipeline_id=int(pipeline_id) if pipeline_id not in {None, ""} else None,
        )

    @classmethod
    def from_json(cls, value: str) -> "PipelineWaitIdentity":
        return cls.from_dict(_from_json(value))


@dataclass(frozen=True)
class RepairItem:
    category: RepairCategory
    command: str
    label: str
    display_name: str
    button_type: str
    status: RepairItemStatus
    task_id: str = ""
    pipeline_id: int = 0
    pipeline_sha: str = ""
    result_pipeline_id: int = 0
    result_pipeline_sha: str = ""
    status_markdown: str = ""
    failed_job_names: tuple[str, ...] = ()
    failure_explanations: tuple[FailureExplanation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["category"] = self.category.value
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairItem":
        return cls(
            category=RepairCategory(value["category"]),
            command=str(value["command"]),
            label=str(value["label"]),
            display_name=str(value.get("display_name") or value["label"]),
            button_type=str(value.get("button_type") or "primary"),
            status=RepairItemStatus(value.get("status") or RepairItemStatus.PENDING.value),
            task_id=str(value.get("task_id") or ""),
            pipeline_id=int(value.get("pipeline_id") or 0),
            pipeline_sha=str(value.get("pipeline_sha") or ""),
            result_pipeline_id=int(value.get("result_pipeline_id") or 0),
            result_pipeline_sha=str(value.get("result_pipeline_sha") or ""),
            status_markdown=str(value.get("status_markdown") or ""),
            failed_job_names=tuple(str(name) for name in value.get("failed_job_names") or ()),
            failure_explanations=tuple(
                FailureExplanation.from_dict(record)
                for record in value.get("failure_explanations") or ()
                if isinstance(record, dict)
            ),
        )


@dataclass(frozen=True)
class TriageCardBinding:
    schema_version: int
    card_id: str
    task_id: str
    open_message_id: str
    receive_id: str
    mr_url: str
    project_id: str
    mr_iid: int
    mr_title: str
    source_branch: str
    pipeline_id: int
    pipeline_sha: str
    original_markdown: str
    state: TriageCardState
    status_markdown: str
    fallback_sent: bool
    updated_at: str
    repair_items: tuple[RepairItem, ...]
    active_task_id: str
    active_category: str
    revision: int
    current_pipeline_id: int
    current_pipeline_sha: str
    failed_job_names: tuple[str, ...] = ()
    repair_card_mode: str = ""
    mr_author_username: str = ""
    rollback_repair_task_id: str = ""
    rollback_commit_count: int = 0
    rollback_task_id: str = ""
    rollback_status: str = ""
    rollback_commit_sha: str = ""
    rollback_trigger: str = ""
    post_repair_ut: PostRepairUTState = PostRepairUTState()

    @classmethod
    def new(
        cls,
        *,
        card_id: str,
        task_id: str,
        open_message_id: str,
        receive_id: str,
        mr_url: str,
        project_id: str,
        mr_iid: int,
        mr_title: str,
        source_branch: str,
        pipeline_id: int,
        pipeline_sha: str,
        original_markdown: str,
        repair_items: tuple[RepairItem, ...] = (),
        failed_job_names: tuple[str, ...] = (),
        repair_card_mode: str = "",
        mr_author_username: str = "",
    ) -> "TriageCardBinding":
        return cls(
            schema_version=SCHEMA_VERSION,
            card_id=card_id,
            task_id=task_id,
            open_message_id=open_message_id,
            receive_id=receive_id,
            mr_url=mr_url,
            project_id=project_id,
            mr_iid=mr_iid,
            mr_title=mr_title,
            source_branch=source_branch,
            pipeline_id=pipeline_id,
            pipeline_sha=pipeline_sha,
            original_markdown=original_markdown,
            state=TriageCardState.PIPELINE_FAILED,
            status_markdown="",
            fallback_sent=False,
            updated_at=_utc_now(),
            repair_items=repair_items,
            active_task_id="",
            active_category="",
            revision=0,
            current_pipeline_id=pipeline_id,
            current_pipeline_sha=pipeline_sha,
            failed_job_names=failed_job_names,
            repair_card_mode=repair_card_mode,
            mr_author_username=mr_author_username,
            rollback_repair_task_id="",
            rollback_commit_count=0,
            rollback_task_id="",
            rollback_status="",
            rollback_commit_sha="",
            rollback_trigger="",
            post_repair_ut=PostRepairUTState(),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        value["repair_items"] = [item.to_dict() for item in self.repair_items]
        value["post_repair_ut"] = self.post_repair_ut.to_dict()
        return value

    def to_json(self) -> str:
        return _to_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TriageCardBinding":
        _validate_schema_version(value)
        raw_items = value.get("repair_items") or []
        return cls(
            schema_version=SCHEMA_VERSION,
            card_id=str(value["card_id"]),
            task_id=str(value.get("task_id", "")),
            open_message_id=str(value.get("open_message_id", "")),
            receive_id=str(value["receive_id"]),
            mr_url=str(value["mr_url"]),
            project_id=str(value["project_id"]),
            mr_iid=int(value["mr_iid"]),
            mr_title=str(value.get("mr_title", "")),
            source_branch=str(value.get("source_branch", "")),
            pipeline_id=int(value["pipeline_id"]),
            pipeline_sha=str(value.get("pipeline_sha", "")),
            original_markdown=str(value["original_markdown"]),
            state=TriageCardState(value["state"]),
            status_markdown=str(value.get("status_markdown", "")),
            fallback_sent=bool(value.get("fallback_sent", False)),
            updated_at=str(value["updated_at"]),
            repair_items=tuple(RepairItem.from_dict(item) for item in raw_items if isinstance(item, dict)),
            active_task_id=str(value.get("active_task_id") or ""),
            active_category=str(value.get("active_category") or ""),
            revision=int(value.get("revision") or 0),
            current_pipeline_id=int(value.get("current_pipeline_id") or value["pipeline_id"]),
            current_pipeline_sha=str(value.get("current_pipeline_sha") or value.get("pipeline_sha") or ""),
            failed_job_names=tuple(str(name) for name in value.get("failed_job_names") or ()),
            repair_card_mode=str(value.get("repair_card_mode") or ""),
            mr_author_username=str(value.get("mr_author_username") or ""),
            rollback_repair_task_id=str(value.get("rollback_repair_task_id") or ""),
            rollback_commit_count=int(value.get("rollback_commit_count") or 0),
            rollback_task_id=str(value.get("rollback_task_id") or ""),
            rollback_status=str(value.get("rollback_status") or ""),
            rollback_commit_sha=str(value.get("rollback_commit_sha") or ""),
            rollback_trigger=str(value.get("rollback_trigger") or ""),
            post_repair_ut=PostRepairUTState.from_dict(
                value.get("post_repair_ut") if isinstance(value.get("post_repair_ut"), dict) else None
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> "TriageCardBinding":
        return cls.from_dict(_from_json(value))


@dataclass(frozen=True)
class NotificationEnvelope:
    schema_version: int
    notification_id: str
    task_id: str
    receive_id: str
    recipient_email: str
    recipient_username: str
    kind: str
    content: str
    title: str
    header_template: str
    mr_url: str
    created_at: str
    card_id: str = ""
    message_id: str = ""
    fallback_content: str = ""
    card_state: str = ""

    def __post_init__(self) -> None:
        if not any((self.receive_id, self.recipient_email, self.recipient_username)):
            raise ValueError("at least one recipient identity is required")

    @classmethod
    def new(
        cls,
        *,
        task_id: str,
        receive_id: str,
        recipient_email: str,
        recipient_username: str,
        kind: str,
        content: str,
        title: str,
        header_template: str,
        mr_url: str,
        notification_id: str | None = None,
        card_id: str = "",
        message_id: str = "",
        fallback_content: str = "",
        card_state: str = "",
    ) -> "NotificationEnvelope":
        return cls(
            schema_version=SCHEMA_VERSION,
            notification_id=notification_id or uuid4().hex,
            task_id=task_id,
            receive_id=receive_id,
            recipient_email=recipient_email,
            recipient_username=recipient_username,
            kind=kind,
            content=content,
            title=title,
            header_template=header_template,
            mr_url=mr_url,
            created_at=_utc_now(),
            card_id=card_id,
            message_id=message_id,
            fallback_content=fallback_content,
            card_state=card_state,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return _to_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NotificationEnvelope":
        _validate_schema_version(value)
        return cls(
            schema_version=SCHEMA_VERSION,
            notification_id=str(value["notification_id"]),
            task_id=str(value["task_id"]),
            receive_id=str(value.get("receive_id", "")),
            recipient_email=str(value.get("recipient_email", "")),
            recipient_username=str(value.get("recipient_username", "")),
            kind=str(value["kind"]),
            content=str(value["content"]),
            title=str(value["title"]),
            header_template=str(value["header_template"]),
            mr_url=str(value["mr_url"]),
            created_at=str(value["created_at"]),
            card_id=str(value.get("card_id", "")),
            message_id=str(value.get("message_id", "")),
            fallback_content=str(value.get("fallback_content", "")),
            card_state=str(value.get("card_state", "")),
        )

    @classmethod
    def from_json(cls, value: str) -> "NotificationEnvelope":
        return cls.from_dict(_from_json(value))
