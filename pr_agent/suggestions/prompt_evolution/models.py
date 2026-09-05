from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNHANDLED = "unhandled"
    PENDING = "pending"
    INVALID = "invalid"


class ReplayAction(StrEnum):
    EMIT = "emit"
    SUPPRESS = "suppress"
    REVISE = "revise"


class CandidateScope(StrEnum):
    PROJECT = "project"
    GLOBAL = "global"


class PromptChangeKind(StrEnum):
    CONSERVATIVE_TIGHTENING = "conservative_tightening"
    SPECIFIC_RULE = "specific_rule"


class EvolutionRunStatus(StrEnum):
    CREATED = "created"
    AGGREGATING = "aggregating"
    GENERATING = "generating"
    VALIDATING = "validating"
    PUBLISHING = "publishing"
    MR_OPEN = "mr_open"
    COMPLETED_NO_CHANGE = "completed_no_change"
    DRY_RUN_VALIDATED = "dry_run_validated"
    OPTIMIZATION_REJECTED = "optimization_rejected"
    INSUFFICIENT_VALIDATION = "insufficient_validation"
    SUPERSEDED = "superseded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    MERGED = "merged"
    CLOSED = "closed"


@dataclass(frozen=True)
class Evidence:
    suggestion_id: str
    project: str
    mr_iid: str
    mr_url: str
    created_at: str
    file_path: str
    label: str
    summary: str
    suggestion_content: str
    outcome: Outcome
    weight: float
    global_prompt_set_hash: str
    prompt_bundle_hash: str
    project_rules_hash: str = ""
    project_skill_hash: str = ""
    project_skill_manifest_hash: str = ""
    project_skill_target_sha: str = ""
    project_skill_status: str = ""
    project_skill_rule_ids: tuple[str, ...] = ()
    project_skill_reference_hashes: tuple[tuple[str, str], ...] = ()
    feedback: tuple[str, ...] = ()
    existing_code: str = ""
    improved_code: str = ""
    commit_sha: str = ""
    line_start: int = 0
    line_end: int = 0
    case_kind: str = ""
    expected_action: str = ""
    review_id: str = ""
    replayable: bool = False


@dataclass(frozen=True)
class WeightedCluster:
    cluster_key: str
    evidence: tuple[Evidence, ...]
    positive_weight: float
    negative_weight: float
    negative_ratio: float


@dataclass(frozen=True)
class EligibleCandidate:
    candidate_id: str
    scope: CandidateScope
    project: str | None
    source_prompt_hash: str
    cluster: WeightedCluster


@dataclass(frozen=True)
class SkillOptimizationBatch:
    project: str
    base_manifest_hash: str
    training_candidates: tuple[EligibleCandidate, ...]
    selection_cases: tuple[Evidence, ...]
    control_ids: tuple[str, ...]
    split_hash: str

    @property
    def selection_ids(self) -> tuple[str, ...]:
        return tuple(item.suggestion_id for item in self.selection_cases)


@dataclass(frozen=True)
class PromptEvaluationBatch:
    base_prompt_hash: str
    training_candidates: tuple[EligibleCandidate, ...]
    selection_cases: tuple[Evidence, ...]
    control_ids: tuple[str, ...]
    split_hash: str

    @property
    def selection_ids(self) -> tuple[str, ...]:
        return tuple(item.suggestion_id for item in self.selection_cases)


@dataclass(frozen=True)
class SkillReplayDecision:
    case_id: str
    action: ReplayAction
    reason: str = ""


@dataclass(frozen=True)
class SkillReplayResult:
    model: str
    decisions: tuple[SkillReplayDecision, ...]


@dataclass(frozen=True)
class SkillOptimizationReport:
    passed: bool
    action: str
    errors: tuple[str, ...]
    checks: tuple[str, ...]
    split_hash: str
    replay_model: str
    baseline_score: str
    candidate_score: str
    baseline_accepted_score: str
    candidate_accepted_score: str
    baseline_rejected_score: str
    candidate_rejected_score: str
    accepted_control_regressions: tuple[str, ...]
    rejected_target_regressions: tuple[str, ...]
    edit_budget: int
    edit_count: int
    edit_signature: str


@dataclass(frozen=True)
class HighFidelityCaseResult:
    case_id: str
    mr_iid: str
    outcome: Outcome
    baseline_action: ReplayAction
    candidate_action: ReplayAction
    baseline_condition_hash: str
    candidate_condition_hash: str


@dataclass(frozen=True)
class HighFidelityEvaluationReport:
    passed: bool
    errors: tuple[str, ...]
    checks: tuple[str, ...]
    replayed_mrs: tuple[str, ...]
    case_results: tuple[HighFidelityCaseResult, ...]
    baseline_score: str
    candidate_score: str
    baseline_accepted_score: str
    candidate_accepted_score: str
    baseline_rejected_score: str
    candidate_rejected_score: str
    condition_hashes: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class SourceSnapshot:
    evidence: tuple[Evidence, ...]
    watermark: str
    has_new_signal: bool


@dataclass(frozen=True)
class PromptFileChange:
    path: str
    family: str
    expected_base_sha256: str
    content: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class PromptProposal:
    rationale: str
    change_kind: PromptChangeKind
    evidence_ids: tuple[str, ...]
    changes: tuple[PromptFileChange, ...]


@dataclass(frozen=True)
class ValidationReport:
    passed: bool
    errors: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvolutionRun:
    run_id: str
    batch_id: str
    status: EvolutionRunStatus
    target_project: str
    target_branch: str
    base_sha: str
    global_prompt_set_hash: str
    target_prompt_set_hash: str
    source_watermark: str = ""
    branch_name: str = ""
    commit_sha: str = ""
    mr_iid: str = ""
    mr_url: str = ""
    error_code: str = ""
    error_message: str = ""


MISSING_FILE_HASH = hashlib.sha256(b"<prompt-evolution-missing-file>").hexdigest()


@dataclass(frozen=True)
class PublishedDraft:
    commit_sha: str
    mr_iid: str
    mr_url: str
