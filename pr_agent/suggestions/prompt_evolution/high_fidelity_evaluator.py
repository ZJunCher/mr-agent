"""Production-path paired replay and deterministic Project Skill scoring."""
from __future__ import annotations

from decimal import Decimal

from pr_agent.eval.conditions import compare_paired_conditions
from pr_agent.eval.production_replay import (
    NormalizedReviewItem,
    ProductionReplayRequest,
    run_production_replay,
)
from pr_agent.suggestions.prompt_evolution.models import (
    Evidence,
    HighFidelityCaseResult,
    HighFidelityEvaluationReport,
    Outcome,
    ReplayAction,
    SkillOptimizationBatch,
)


def _normalized(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _overlaps(start: int, end: int, item: NormalizedReviewItem) -> bool:
    if start <= 0 or end <= 0:
        return True
    if item.line_start <= 0 or item.line_end <= 0:
        return False
    return max(start, item.line_start) <= min(end, item.line_end)


def _action_for(evidence: Evidence, items: tuple[NormalizedReviewItem, ...]) -> tuple[ReplayAction, bool]:
    same_location = tuple(
        item for item in items
        if _normalized(item.file_path) == _normalized(evidence.file_path)
        and _overlaps(evidence.line_start, evidence.line_end, item)
    )
    evidence_content = _normalized(evidence.suggestion_content)
    evidence_summary = _normalized(evidence.summary)
    exact = tuple(
        item for item in same_location
        if (evidence_content and _normalized(item.content) == evidence_content)
        or (evidence_summary and _normalized(item.summary) == evidence_summary)
    )
    if evidence.case_kind == "false_negative" and len(same_location) == 1:
        return ReplayAction.EMIT, False
    if len(exact) == 1:
        return ReplayAction.EMIT, False
    if len(exact) > 1 or len(same_location) > 1:
        return ReplayAction.REVISE, True
    if same_location:
        return ReplayAction.REVISE, False
    return ReplayAction.SUPPRESS, False


def _score(cases: tuple[Evidence, ...], actions: dict[str, ReplayAction]) -> Decimal:
    denominator = sum((Decimal(str(case.weight)) for case in cases), Decimal("0"))
    if not denominator:
        return Decimal("0")
    numerator = Decimal("0")
    for case in cases:
        action = actions.get(case.suggestion_id)
        if case.expected_action == "emit":
            correct = action is ReplayAction.EMIT
        elif case.expected_action == "revise":
            correct = action is ReplayAction.REVISE
        else:
            correct = action is ReplayAction.EMIT if case.outcome is Outcome.ACCEPTED else action in {
                ReplayAction.SUPPRESS,
                ReplayAction.REVISE,
            }
        if correct:
            numerator += Decimal(str(case.weight))
    return numerator / denominator


def _decimal_text(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


class ProjectSkillHighFidelityEvaluator:
    """Run baseline/candidate Skills on identical frozen MR snapshots."""

    def __init__(self, record_loader, *, replay_runner=run_production_replay,
                 min_mrs: int = 1, max_mrs: int = 10, command: str = "improve") -> None:
        self.record_loader = record_loader
        self.replay_runner = replay_runner
        self.min_mrs = int(min_mrs)
        self.max_mrs = int(max_mrs)
        self.command = command

    async def evaluate_pair(
        self,
        batch: SkillOptimizationBatch,
        baseline_skill: str,
        candidate_skill: str,
        *,
        target_sha: str,
        model: str,
        minimum_score_delta: float = 0.0,
    ) -> HighFidelityEvaluationReport:
        requested_mrs = tuple(sorted({case.mr_iid for case in batch.selection_cases}))[:self.max_mrs]
        records = self.record_loader(batch.project, requested_mrs)
        by_mr = {str(record.get("mr_iid") or ""): record for record in records}
        replayed_mrs = tuple(mr for mr in requested_mrs if mr in by_mr)
        if len(replayed_mrs) < self.min_mrs:
            return self._failure("insufficient_high_fidelity_evidence", replayed_mrs)

        cases_by_mr = {
            mr: tuple(case for case in batch.selection_cases if case.mr_iid == mr)
            for mr in replayed_mrs
        }
        case_results = []
        condition_hashes = []
        errors = []
        baseline_actions = {}
        candidate_actions = {}
        for mr_iid in replayed_mrs:
            record = by_mr[mr_iid]
            common = dict(
                project=batch.project,
                mr_iid=mr_iid,
                pr_url=str(record.get("pr_url") or ""),
                base_sha=str(record.get("base_sha") or ""),
                head_sha=str(record.get("head_sha") or ""),
                target_sha=target_sha,
                input_snapshot=record.get("input") or {},
                command=self.command,
                model=model,
                captured_at=str(record.get("created_at") or ""),
            )
            baseline = await self.replay_runner(ProductionReplayRequest(skill_content=baseline_skill, **common))
            candidate = await self.replay_runner(ProductionReplayRequest(skill_content=candidate_skill, **common))
            if baseline.status != "ok" or candidate.status != "ok":
                errors.append(f"replay_failed:{mr_iid}")
                continue
            if baseline.coverage_status != "complete" or candidate.coverage_status != "complete":
                errors.append(f"incomplete_diff_coverage:{mr_iid}")
                continue
            if baseline.condition is None or candidate.condition is None:
                errors.append(f"missing_condition_manifest:{mr_iid}")
                continue
            condition_check = compare_paired_conditions(baseline.condition, candidate.condition)
            if not condition_check.matched:
                suffix = ",".join(condition_check.mismatched_fields) or condition_check.error
                errors.append(f"condition_mismatch:{mr_iid}:{suffix}")
                continue
            condition_hashes.append((
                mr_iid,
                baseline.condition.manifest_hash,
                candidate.condition.manifest_hash,
            ))
            for case in cases_by_mr[mr_iid]:
                baseline_action, baseline_ambiguous = _action_for(case, baseline.normalized_items)
                candidate_action, candidate_ambiguous = _action_for(case, candidate.normalized_items)
                if baseline_ambiguous or candidate_ambiguous:
                    errors.append(f"ambiguous_result_match:{case.suggestion_id}")
                    continue
                baseline_actions[case.suggestion_id] = baseline_action
                candidate_actions[case.suggestion_id] = candidate_action
                case_results.append(HighFidelityCaseResult(
                    case.suggestion_id,
                    mr_iid,
                    case.outcome,
                    baseline_action,
                    candidate_action,
                    baseline.condition.manifest_hash,
                    candidate.condition.manifest_hash,
                ))

        eligible_cases = tuple(case for case in batch.selection_cases if case.mr_iid in replayed_mrs)
        evaluated_ids = {result.case_id for result in case_results}
        evaluated_cases = tuple(case for case in eligible_cases if case.suggestion_id in evaluated_ids)
        if len(evaluated_cases) != len(eligible_cases):
            errors.append("incomplete_case_results")
        accepted = tuple(case for case in evaluated_cases if case.outcome is Outcome.ACCEPTED)
        rejected = tuple(case for case in evaluated_cases if case.outcome is Outcome.REJECTED)
        baseline_score = _score(evaluated_cases, baseline_actions)
        candidate_score = _score(evaluated_cases, candidate_actions)
        baseline_accepted = _score(accepted, baseline_actions)
        candidate_accepted = _score(accepted, candidate_actions)
        baseline_rejected = _score(rejected, baseline_actions)
        candidate_rejected = _score(rejected, candidate_actions)
        if candidate_score <= baseline_score:
            errors.append("high_fidelity_score_not_improved")
        if candidate_score - baseline_score < Decimal(str(minimum_score_delta)):
            errors.append("high_fidelity_score_delta_too_small")
        if candidate_accepted < baseline_accepted:
            errors.append("high_fidelity_accepted_regression")
        if candidate_rejected < baseline_rejected:
            errors.append("high_fidelity_rejected_regression")
        unique_errors = tuple(dict.fromkeys(errors))
        return HighFidelityEvaluationReport(
            passed=not unique_errors,
            errors=unique_errors,
            checks=("production_path", "paired_conditions", "complete_coverage", "deterministic_score"),
            replayed_mrs=replayed_mrs,
            case_results=tuple(case_results),
            baseline_score=_decimal_text(baseline_score),
            candidate_score=_decimal_text(candidate_score),
            baseline_accepted_score=_decimal_text(baseline_accepted),
            candidate_accepted_score=_decimal_text(candidate_accepted),
            baseline_rejected_score=_decimal_text(baseline_rejected),
            candidate_rejected_score=_decimal_text(candidate_rejected),
            condition_hashes=tuple(condition_hashes),
        )

    @staticmethod
    def _failure(error: str, replayed_mrs: tuple[str, ...]) -> HighFidelityEvaluationReport:
        return HighFidelityEvaluationReport(
            False, (error,), (), replayed_mrs, (), "0", "0", "0", "0", "0", "0", (),
        )
