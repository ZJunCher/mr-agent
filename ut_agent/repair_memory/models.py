"""Immutable value objects for the UT-Agent repair-memory subsystem.

These dataclasses are the stable public surface consumed by the live repair
path (retriever, prompt adapter, outcome tracker) and by the asynchronous
consolidation/promotion batch. Persisted JSON depends on these field names, so
they must not change without a schema-version bump.

All value objects are frozen to prevent accidental mutation after construction.
Tuples are used instead of lists so instances remain hashable and immutable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

#: Current repair-memory schema version. Bump when persisted JSON shape changes.
MEMORY_SCHEMA_VERSION = 1


class MemoryScope(StrEnum):
    """Scope of a repair memory."""

    PROJECT = "project"
    GLOBAL = "global"


class MemoryStatus(StrEnum):
    """Lifecycle status of a repair memory."""

    ACTIVE = "active"
    NEEDS_REVIEW = "needs_review"
    DISABLED = "disabled"
    SUPERSEDED = "superseded"


class EmbeddingStatus(StrEnum):
    """Lifecycle status of one persisted repair-memory vector."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class RetrievalMode(StrEnum):
    """Live-path retrieval mode.

    OFF: no retrieval; SHADOW: retrieve and audit but do not inject; INJECT:
    retrieve, audit, and render hints into the Hermes prompt.
    """

    OFF = "off"
    SHADOW = "shadow"
    INJECT = "inject"


class RetrievalAuditStatus(StrEnum):
    """Task-level outcome of the repair-memory retrieval decision."""

    NOT_ATTEMPTED = "not_attempted"
    NO_MATCH = "no_match"
    RECALLED = "recalled"
    ERROR = "error"


@dataclass(frozen=True)
class RepairEpisode:
    """One immutable, sanitized, verified repair-action record.

    An episode represents a single ``RepairAction`` that passed exact-SHA
    Pipeline validation, not an entire task. Repeated final-report processing
    is idempotent because ``(task_id, action_identity)`` is unique.
    """

    episode_id: str
    task_id: str
    action_identity: str
    root_cause_group_id: str
    project: str
    mr_iid: int
    source_pipeline_id: int
    source_sha: str
    final_pipeline_id: int
    final_sha: str
    categories: tuple[str, ...]
    job_names: tuple[str, ...]
    language_hints: tuple[str, ...]
    build_system_hints: tuple[str, ...]
    diagnostic_fingerprint: str
    causal_tokens: tuple[str, ...]
    root_cause: str
    solution_summary: str
    measures: tuple[str, ...]
    changed_files: tuple[str, ...]
    report_input_digest: str
    report_source: str
    eligibility_reason: str = "eligible"
    consolidation_status: str = "pending"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairEpisode":
        return cls(**value)


@dataclass(frozen=True)
class RepairMemory:
    """One atomic retrievable repair rule.

    A project memory is created from one verified episode. A global memory
    requires the same promotable pattern to succeed independently in at least
    two projects. Confidence is bounded to ``[0.30, 0.95]``.
    """

    memory_id: str
    scope: MemoryScope
    scope_key: str
    pattern_key: str
    pattern_version: int
    language: str
    build_system: str
    failure_family: str
    root_cause_class: str
    repair_action_class: str
    diagnostic_fingerprint: str
    causal_tokens: tuple[str, ...]
    problem_pattern: str
    applicability: tuple[str, ...]
    anti_conditions: tuple[str, ...]
    repair_guidance: str
    validation_guidance: tuple[str, ...]
    confidence: float
    support_episode_count: int
    support_project_count: int
    settled_attempts: int
    immediate_successes: int
    status: MemoryStatus
    content_locale: str = "legacy"
    supersedes_id: str = ""
    manual_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_reinforced_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairMemory":
        return cls(**value)


@dataclass(frozen=True)
class RepairMemoryEmbedding:
    """One versioned BGE-M3 vector stored separately from memory text."""

    memory_id: str
    model_name: str
    model_revision: str
    dimensions: int
    vector_blob: bytes
    source_hash: str
    status: EmbeddingStatus
    last_error_code: str = ""
    attempt_count: int = 0
    next_retry_at: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class RepairQuery:
    """Bounded query constructed from trusted current task metadata.

    No historical lookup may broaden filesystem, GitLab, dependency, or raw-log
    access beyond what the current evidence packet already provides.
    """

    project: str
    root_cause_group_id: str
    source_pipeline_id: int
    source_sha: str
    failure_category: str
    job_family: str
    failure_family: str
    language: str
    build_system: str
    diagnostic_fingerprint: str
    causal_tokens: tuple[str, ...]


@dataclass(frozen=True)
class RepairMemoryHint:
    """One selected, sanitized, bounded hint ready for prompt rendering."""

    memory_id: str
    scope: MemoryScope
    pattern_key: str
    score: int
    match_reasons: tuple[str, ...]
    problem_pattern: str
    applicability: tuple[str, ...]
    anti_conditions: tuple[str, ...]
    repair_guidance: str
    validation_guidance: tuple[str, ...]
    support_episode_count: int
    support_project_count: int
    confidence: float


@dataclass(frozen=True)
class RetrievalResult:
    """Outcome of one retrieval attempt on the live repair path."""

    mode: RetrievalMode
    attempt_id: str
    hints: tuple[RepairMemoryHint, ...]
    audit_persisted: bool
    max_prompt_chars: int


@dataclass(frozen=True)
class RepairMemoryCandidateAudit:
    """One bounded score decision for a retrieval candidate."""

    attempt_id: str
    task_id: str
    memory_id: str
    memory_scope: MemoryScope
    scoring_mode: str
    semantic_similarity: float | None
    total_score: int
    score: dict[str, Any]
    decision: str
    rejection_reason: str
    created_at: str = ""


@dataclass(frozen=True)
class RepairMemoryRetrievalAudit:
    """One task-level audit proving whether repair-memory search ran."""

    task_id: str
    project: str
    mr_iid: int
    source_pipeline_id: int
    source_sha: str
    mode: RetrievalMode
    status: RetrievalAuditStatus
    reason_code: str
    search_count: int
    candidate_count: int
    passed_threshold_count: int
    selected_count: int
    injected_count: int
    last_attempt_id: str
    error_code: str
    created_at: str
    updated_at: str
    attempted_at: str


@dataclass(frozen=True)
class MemoryEvent:
    """One append-only operator or lifecycle audit event."""

    id: int
    memory_id: str
    event_type: str
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "memory_id": self.memory_id,
            "event_type": self.event_type,
            "reason": self.reason,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryEvent":
        return cls(
            id=int(value["id"]),
            memory_id=str(value["memory_id"]),
            event_type=str(value["event_type"]),
            reason=str(value["reason"]),
            metadata=dict(value.get("metadata") or {}),
            created_at=str(value.get("created_at", "")),
        )


def _json_dumps(value: Any) -> str:
    """Serialize to compact JSON with sorted keys for deterministic storage."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads(value: str) -> Any:
    """Deserialize JSON, returning an empty container for NULL/empty strings."""
    if not value:
        return None
    return json.loads(value)
