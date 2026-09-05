import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Iterable

from pr_agent.distributed.models import RepairCategory
from pr_agent.triage.failure_explanations import FailureExplanation
from pr_agent.triage.repair_details import RepairAction
from pr_agent.triage.repair_outcome import CategoryRepairResult


class PipelineRepairStep(StrEnum):
    TRIAGE = "triage"
    FORMAT = "format"
    TERMINAL = "terminal"


class PipelineRepairPhase(StrEnum):
    PENDING = "pending"
    TRIAGE_RUNNING = "triage_running"
    TRIAGE_WAITING = "triage_waiting"
    FORMAT_RUNNING = "format_running"
    FORMAT_WAITING = "format_waiting"
    COVERAGE_RUNNING = "coverage_running"
    COVERAGE_WAITING = "coverage_waiting"
    COVERAGE_ROLLBACK_RUNNING = "coverage_rollback_running"
    COVERAGE_ROLLBACK_WAITING = "coverage_rollback_waiting"
    TERMINAL = "terminal"


class CoverageContinuationPhase(StrEnum):
    NOT_STARTED = "not_started"
    ENHANCING = "enhancing"
    WAITING = "waiting"
    ROLLING_BACK = "rolling_back"
    ROLLBACK_WAITING = "rollback_waiting"
    COMPLETED = "completed"


@dataclass(frozen=True)
class PipelineRepairState:
    phase: PipelineRepairPhase = PipelineRepairPhase.PENDING
    completed_steps: tuple[str, ...] = ()
    root_pipeline_id: int = 0
    latest_pipeline_id: int = 0
    latest_pipeline_sha: str = ""
    terminal_attempt_id: str = ""
    terminal_proof_sha: str = ""
    terminal_proof_pipeline_id: int = 0
    terminal_proof_status: str = ""
    final_pipeline_status: str = ""
    final_coverage: float | None = None
    failed_job_names: tuple[str, ...] = ()
    terminal_error: str = ""
    terminal_failure_kind: str = ""
    terminal_validation_error_code: str = ""
    terminal_validation_summary: str = ""
    normalized_diagnostic_alias_count: int = 0
    iterations: int = 0
    max_iterations: int = 0
    selected_categories: tuple[str, ...] = ()
    effective_categories: tuple[str, ...] = ()
    auto_format_cleanup: bool = False
    source_failure_explanations: tuple[FailureExplanation, ...] = ()
    failure_explanations: tuple[FailureExplanation, ...] = ()
    repair_actions: tuple[RepairAction, ...] = ()
    final_coverage_source: str = ""
    final_coverage_status: str = ""
    source_failed_job_names: tuple[str, ...] = ()
    repair_outcome: str = ""
    blocker_type: str = ""
    blocker_summary: str = ""
    blocker_suggested_action: str = ""
    blocked_job_names: tuple[str, ...] = ()
    dependency_evidence: tuple[dict, ...] = ()
    category_results: tuple[CategoryRepairResult, ...] = ()
    introduced_failure_categories: tuple[str, ...] = ()
    introduced_failed_job_names: tuple[str, ...] = ()
    verified_selected_success_count: int = 0
    auto_rollback_required: bool = False
    format_round: int = 0
    format_report_fingerprints: tuple[str, ...] = ()
    format_last_exact_report_applied: bool = False
    coverage_phase: CoverageContinuationPhase = CoverageContinuationPhase.NOT_STARTED
    coverage_attempts: int = 0
    coverage_skip_reason: str = ""
    coverage_baseline_pipeline_id: int = 0
    coverage_baseline_sha: str = ""
    coverage_enhancement_sha: str = ""
    coverage_rollback_sha: str = ""
    coverage_before: float | None = None
    coverage_after: float | None = None
    coverage_threshold: float | None = None
    coverage_job_id: int = 0
    coverage_result: str = ""
    coverage_failure_reason: str = ""

    def to_json(self) -> str:
        value = asdict(self)
        value["phase"] = self.phase.value
        value["coverage_phase"] = self.coverage_phase.value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> "PipelineRepairState":
        if not value:
            return cls()
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("pipeline repair state must be a JSON object")
        return cls(
            phase=PipelineRepairPhase(decoded.get("phase") or PipelineRepairPhase.PENDING.value),
            completed_steps=tuple(str(step) for step in decoded.get("completed_steps") or ()),
            root_pipeline_id=int(decoded.get("root_pipeline_id") or 0),
            latest_pipeline_id=int(decoded.get("latest_pipeline_id") or 0),
            latest_pipeline_sha=str(decoded.get("latest_pipeline_sha") or ""),
            terminal_attempt_id=str(decoded.get("terminal_attempt_id") or ""),
            terminal_proof_sha=str(decoded.get("terminal_proof_sha") or ""),
            terminal_proof_pipeline_id=int(decoded.get("terminal_proof_pipeline_id") or 0),
            terminal_proof_status=str(decoded.get("terminal_proof_status") or ""),
            final_pipeline_status=str(decoded.get("final_pipeline_status") or ""),
            final_coverage=(
                float(decoded["final_coverage"])
                if decoded.get("final_coverage") not in {None, ""}
                else None
            ),
            final_coverage_source=str(decoded.get("final_coverage_source") or ""),
            final_coverage_status=str(decoded.get("final_coverage_status") or ""),
            failed_job_names=tuple(str(name) for name in decoded.get("failed_job_names") or ()),
            terminal_error=str(decoded.get("terminal_error") or ""),
            terminal_failure_kind=str(decoded.get("terminal_failure_kind") or ""),
            terminal_validation_error_code=str(decoded.get("terminal_validation_error_code") or "")[:80],
            terminal_validation_summary=str(decoded.get("terminal_validation_summary") or "")[:500],
            normalized_diagnostic_alias_count=max(
                0,
                int(decoded.get("normalized_diagnostic_alias_count") or 0),
            ),
            iterations=int(decoded.get("iterations") or 0),
            max_iterations=int(decoded.get("max_iterations") or 0),
            selected_categories=tuple(str(category) for category in decoded.get("selected_categories") or ()),
            effective_categories=tuple(str(category) for category in decoded.get("effective_categories") or ()),
            auto_format_cleanup=bool(decoded.get("auto_format_cleanup", False)),
            source_failure_explanations=tuple(
                FailureExplanation.from_dict(record)
                for record in decoded.get("source_failure_explanations") or ()
                if isinstance(record, dict)
            ),
            failure_explanations=tuple(
                FailureExplanation.from_dict(record)
                for record in decoded.get("failure_explanations") or ()
                if isinstance(record, dict)
            ),
            repair_actions=tuple(
                RepairAction.from_dict(record)
                for record in decoded.get("repair_actions") or ()
                if isinstance(record, dict)
            ),
            source_failed_job_names=tuple(str(name) for name in decoded.get("source_failed_job_names") or ()),
            repair_outcome=str(decoded.get("repair_outcome") or ""),
            blocker_type=str(decoded.get("blocker_type") or ""),
            blocker_summary=str(decoded.get("blocker_summary") or ""),
            blocker_suggested_action=str(decoded.get("blocker_suggested_action") or ""),
            blocked_job_names=tuple(str(name) for name in decoded.get("blocked_job_names") or ()),
            dependency_evidence=tuple(
                record
                for record in decoded.get("dependency_evidence") or ()
                if isinstance(record, dict)
            ),
            category_results=tuple(
                CategoryRepairResult.from_dict(record)
                for record in decoded.get("category_results") or ()
                if isinstance(record, dict)
            ),
            introduced_failure_categories=tuple(
                str(category) for category in decoded.get("introduced_failure_categories") or ()
            ),
            introduced_failed_job_names=tuple(
                str(name) for name in decoded.get("introduced_failed_job_names") or ()
            ),
            verified_selected_success_count=int(decoded.get("verified_selected_success_count") or 0),
            auto_rollback_required=bool(decoded.get("auto_rollback_required", False)),
            format_round=int(decoded.get("format_round") or 0),
            format_report_fingerprints=tuple(
                str(value) for value in decoded.get("format_report_fingerprints") or ()
            ),
            format_last_exact_report_applied=bool(decoded.get("format_last_exact_report_applied", False)),
            coverage_phase=CoverageContinuationPhase(
                decoded.get("coverage_phase") or CoverageContinuationPhase.NOT_STARTED.value
            ),
            coverage_attempts=int(decoded.get("coverage_attempts") or 0),
            coverage_skip_reason=str(decoded.get("coverage_skip_reason") or ""),
            coverage_baseline_pipeline_id=int(decoded.get("coverage_baseline_pipeline_id") or 0),
            coverage_baseline_sha=str(decoded.get("coverage_baseline_sha") or ""),
            coverage_enhancement_sha=str(decoded.get("coverage_enhancement_sha") or ""),
            coverage_rollback_sha=str(decoded.get("coverage_rollback_sha") or ""),
            coverage_before=(
                float(decoded["coverage_before"])
                if decoded.get("coverage_before") not in {None, ""}
                else None
            ),
            coverage_after=(
                float(decoded["coverage_after"])
                if decoded.get("coverage_after") not in {None, ""}
                else None
            ),
            coverage_threshold=(
                float(decoded["coverage_threshold"])
                if decoded.get("coverage_threshold") not in {None, ""}
                else None
            ),
            coverage_job_id=int(decoded.get("coverage_job_id") or 0),
            coverage_result=str(decoded.get("coverage_result") or ""),
            coverage_failure_reason=str(decoded.get("coverage_failure_reason") or ""),
        )


def repair_source_failure_explanations(
    state: PipelineRepairState,
) -> tuple[FailureExplanation, ...]:
    """Return immutable source evidence, falling back to the legacy state field."""
    return state.source_failure_explanations or state.failure_explanations


def _normalize_categories(categories: Iterable[RepairCategory | str]) -> set[RepairCategory]:
    return {RepairCategory(category) for category in categories}


def initial_repair_step(categories: Iterable[RepairCategory | str]) -> PipelineRepairStep:
    normalized = _normalize_categories(categories)
    if normalized == {RepairCategory.FORMAT}:
        return PipelineRepairStep.FORMAT
    return PipelineRepairStep.TRIAGE


def next_step_after_triage(categories: Iterable[RepairCategory | str]) -> PipelineRepairStep:
    if RepairCategory.FORMAT in _normalize_categories(categories):
        return PipelineRepairStep.FORMAT
    return PipelineRepairStep.TERMINAL
