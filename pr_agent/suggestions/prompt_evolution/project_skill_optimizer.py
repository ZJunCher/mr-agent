"""Deterministic SkillOpt-style batching, semantic edits, and selection gate."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal

from pr_agent.suggestions.project_prompt_rules import parse_project_rules
from pr_agent.suggestions.prompt_evolution.models import (
    EligibleCandidate,
    Evidence,
    Outcome,
    ReplayAction,
    SkillOptimizationBatch,
    SkillOptimizationReport,
    SkillReplayResult,
    WeightedCluster,
)


class InsufficientValidationEvidence(ValueError):
    """Raised when an MR-isolated train/selection batch cannot be formed."""


@dataclass(frozen=True)
class SemanticSkillDiff:
    edit_count: int
    changed_rule_ids: tuple[str, ...]
    signature: str


def _stable_hash(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _is_current_version(evidence: Evidence, base_manifest_hash: str) -> bool:
    return bool(evidence.project_skill_manifest_hash) and (
        evidence.project_skill_manifest_hash == base_manifest_hash
    )


def _deduplicate_evidence(evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    result: dict[str, Evidence] = {}
    for item in sorted(evidence, key=lambda value: (value.suggestion_id, value.created_at)):
        existing = result.get(item.suggestion_id)
        if existing is not None and existing != item:
            raise InsufficientValidationEvidence(
                f"conflicting evidence for suggestion ID {item.suggestion_id}"
            )
        result[item.suggestion_id] = item
    return tuple(result[key] for key in sorted(result))


def _rebuild_training_candidates(
    candidates: tuple[EligibleCandidate, ...],
    selection_ids: frozenset[str],
) -> tuple[EligibleCandidate, ...]:
    rebuilt = []
    for candidate in sorted(candidates, key=lambda item: item.candidate_id):
        evidence = tuple(
            item
            for item in sorted(candidate.cluster.evidence, key=lambda value: value.suggestion_id)
            if item.suggestion_id not in selection_ids
        )
        if not evidence:
            continue
        positive = sum(item.weight for item in evidence if item.outcome is Outcome.ACCEPTED)
        negative = sum(
            item.weight for item in evidence if item.outcome in {Outcome.REJECTED, Outcome.UNHANDLED}
        )
        denominator = positive + negative
        rebuilt.append(EligibleCandidate(
            candidate_id=candidate.candidate_id,
            scope=candidate.scope,
            project=candidate.project,
            source_prompt_hash=candidate.source_prompt_hash,
            cluster=WeightedCluster(
                candidate.cluster.cluster_key,
                evidence,
                positive,
                negative,
                negative / denominator if denominator else 0.0,
            ),
        ))
    return tuple(rebuilt)


def build_optimization_batch(
    candidates: tuple[EligibleCandidate, ...],
    project_evidence: tuple[Evidence, ...],
    *,
    project: str,
    base_manifest_hash: str,
    selection_ratio: float,
    min_train_mrs: int,
    min_selection_mrs: int,
    min_control_cases: int,
    max_selection_cases: int,
) -> SkillOptimizationBatch:
    """Split candidate evidence by MR and reserve accepted project controls."""
    if not candidates:
        raise InsufficientValidationEvidence("no candidate evidence")
    if not 0 < float(selection_ratio) < 1:
        raise ValueError("selection_ratio must be between zero and one")
    if min_train_mrs < 1 or min_selection_mrs < 1 or max_selection_cases < 2:
        raise ValueError("invalid train/selection limits")

    raw_target = tuple(item for candidate in candidates for item in candidate.cluster.evidence)
    if any(item.project != project or not _is_current_version(item, base_manifest_hash) for item in raw_target):
        raise InsufficientValidationEvidence("candidate evidence is not from the current-version Skill")
    target = _deduplicate_evidence(raw_target)
    target_mrs = sorted({item.mr_iid for item in target})
    explicit_rejected_mrs = {
        item.mr_iid for item in target if item.outcome is Outcome.REJECTED
    }
    required_mrs = int(min_train_mrs) + int(min_selection_mrs)
    if len(target_mrs) < required_mrs or len(explicit_rejected_mrs) < min_selection_mrs:
        raise InsufficientValidationEvidence("not enough MR groups for train/selection isolation")

    desired_selection_mrs = max(
        int(min_selection_mrs),
        int(math.ceil(len(target_mrs) * float(selection_ratio))),
    )
    desired_selection_mrs = min(desired_selection_mrs, len(target_mrs) - int(min_train_mrs))
    ranked_rejected_mrs = sorted(
        explicit_rejected_mrs,
        key=lambda mr_iid: _stable_hash(project, base_manifest_hash, "selection", mr_iid),
    )
    selection_mrs = frozenset(ranked_rejected_mrs[:desired_selection_mrs])
    if len(selection_mrs) < min_selection_mrs:
        raise InsufficientValidationEvidence("not enough rejected MR groups for selection")

    selection_targets = tuple(
        item
        for item in target
        if item.mr_iid in selection_mrs and item.outcome in {Outcome.ACCEPTED, Outcome.REJECTED}
    )
    if len(selection_targets) + int(min_control_cases) > int(max_selection_cases):
        raise InsufficientValidationEvidence("hidden selection cases exceed the configured case limit")
    selection_ids = frozenset(item.suggestion_id for item in selection_targets)
    training_candidates = _rebuild_training_candidates(candidates, selection_ids)
    training_mrs = {
        item.mr_iid for candidate in training_candidates for item in candidate.cluster.evidence
    }
    if len(training_mrs) < min_train_mrs or training_mrs.intersection(selection_mrs):
        raise InsufficientValidationEvidence("train/selection MR isolation could not be satisfied")

    target_ids = {item.suggestion_id for item in target}
    controls = tuple(
        item
        for item in _deduplicate_evidence(project_evidence)
        if item.project == project
        and item.outcome is Outcome.ACCEPTED
        and item.suggestion_id not in target_ids
        and item.mr_iid not in set(target_mrs)
        and _is_current_version(item, base_manifest_hash)
    )
    controls = tuple(sorted(
        controls,
        key=lambda item: _stable_hash(project, base_manifest_hash, "control", item.suggestion_id),
    ))
    available_control_slots = max(0, int(max_selection_cases) - len(selection_targets))
    controls = controls[:available_control_slots]
    if len(controls) < min_control_cases:
        raise InsufficientValidationEvidence("not enough accepted current-version control cases")

    selection_cases = tuple(sorted(
        selection_targets + controls,
        key=lambda item: item.suggestion_id,
    ))
    control_ids = tuple(sorted(item.suggestion_id for item in controls))
    split_payload = {
        "project": project,
        "base_manifest_hash": base_manifest_hash,
        "candidate_ids": sorted(candidate.candidate_id for candidate in candidates),
        "training_ids": sorted(
            item.suggestion_id
            for candidate in training_candidates
            for item in candidate.cluster.evidence
        ),
        "selection_ids": [item.suggestion_id for item in selection_cases],
        "control_ids": list(control_ids),
    }
    split_hash = hashlib.sha256(
        json.dumps(split_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SkillOptimizationBatch(
        project=project,
        base_manifest_hash=base_manifest_hash,
        training_candidates=training_candidates,
        selection_cases=selection_cases,
        control_ids=control_ids,
        split_hash=split_hash,
    )


def semantic_skill_diff(base_content: str, candidate_content: str, project: str) -> SemanticSkillDiff:
    base_rules = parse_project_rules(base_content, project)
    candidate_rules = parse_project_rules(candidate_content, project)
    base_by_id = {rule.id: rule for rule in base_rules.rules}
    candidate_by_id = {rule.id: rule for rule in candidate_rules.rules}
    changed_ids = tuple(sorted(
        rule_id
        for rule_id in set(base_by_id) | set(candidate_by_id)
        if base_by_id.get(rule_id) != candidate_by_id.get(rule_id)
    ))
    changes = [
        {
            "id": rule_id,
            "before": asdict(base_by_id[rule_id]) if rule_id in base_by_id else None,
            "after": asdict(candidate_by_id[rule_id]) if rule_id in candidate_by_id else None,
        }
        for rule_id in changed_ids
    ]
    signature = hashlib.sha256(
        json.dumps(changes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SemanticSkillDiff(len(changed_ids), changed_ids, signature)


def _decision_index(result: SkillReplayResult) -> tuple[dict[str, ReplayAction], bool]:
    index: dict[str, ReplayAction] = {}
    duplicate = False
    for decision in result.decisions:
        if decision.case_id in index:
            duplicate = True
        index[decision.case_id] = decision.action
    return index, duplicate


def _score(cases: tuple[Evidence, ...], decisions: dict[str, ReplayAction]) -> Decimal:
    denominator = sum((Decimal(str(item.weight)) for item in cases), Decimal("0"))
    if not denominator:
        return Decimal("0")
    numerator = Decimal("0")
    for item in cases:
        action = decisions.get(item.suggestion_id)
        if item.expected_action == "emit":
            correct = action is ReplayAction.EMIT
        elif item.expected_action == "revise":
            correct = action is ReplayAction.REVISE
        else:
            correct = (
                action is ReplayAction.EMIT
                if item.outcome is Outcome.ACCEPTED
                else action in {ReplayAction.SUPPRESS, ReplayAction.REVISE}
            )
        if correct:
            numerator += Decimal(str(item.weight))
    return numerator / denominator


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def evaluate_skill_gate(
    batch: SkillOptimizationBatch,
    baseline: SkillReplayResult,
    candidate: SkillReplayResult,
    *,
    minimum_score_delta: str,
    edit_budget: int,
    edit_count: int,
    edit_signature: str,
    rejected_signatures: tuple[str, ...] = (),
) -> SkillOptimizationReport:
    """Accept only a complete same-model replay with strict, non-regressing gain."""
    errors: list[str] = []
    expected_ids = set(batch.selection_ids)
    baseline_index, baseline_duplicate = _decision_index(baseline)
    candidate_index, candidate_duplicate = _decision_index(candidate)
    if baseline_duplicate or set(baseline_index) != expected_ids:
        errors.append("baseline_replay_incomplete")
    if candidate_duplicate or set(candidate_index) != expected_ids:
        errors.append("candidate_replay_incomplete")
    if not baseline.model or baseline.model != candidate.model:
        errors.append("replay_model_mismatch")
    if edit_count > edit_budget:
        errors.append("textual_learning_rate_exceeded")
    if edit_signature in rejected_signatures:
        errors.append("repeated_rejected_edit")

    accepted_cases = tuple(
        item for item in batch.selection_cases if item.outcome is Outcome.ACCEPTED
    )
    rejected_cases = tuple(
        item for item in batch.selection_cases if item.outcome is Outcome.REJECTED
    )
    baseline_score = _score(batch.selection_cases, baseline_index)
    candidate_score = _score(batch.selection_cases, candidate_index)
    baseline_accepted = _score(accepted_cases, baseline_index)
    candidate_accepted = _score(accepted_cases, candidate_index)
    baseline_rejected = _score(rejected_cases, baseline_index)
    candidate_rejected = _score(rejected_cases, candidate_index)
    delta = candidate_score - baseline_score
    if candidate_score <= baseline_score:
        errors.append("score_not_strictly_better")
    if delta < Decimal(str(minimum_score_delta)):
        errors.append("minimum_score_delta_not_met")
    if candidate_accepted < baseline_accepted:
        errors.append("accepted_control_regression")
    if candidate_rejected < baseline_rejected:
        errors.append("rejected_target_regression")

    accepted_regressions = tuple(sorted(
        item.suggestion_id
        for item in accepted_cases
        if baseline_index.get(item.suggestion_id) is ReplayAction.EMIT
        and candidate_index.get(item.suggestion_id) is not ReplayAction.EMIT
    ))
    rejected_regressions = tuple(sorted(
        item.suggestion_id
        for item in rejected_cases
        if baseline_index.get(item.suggestion_id) in {ReplayAction.SUPPRESS, ReplayAction.REVISE}
        and candidate_index.get(item.suggestion_id) is ReplayAction.EMIT
    ))
    return SkillOptimizationReport(
        passed=not errors,
        action="accept_new_best" if not errors else "reject",
        errors=tuple(sorted(set(errors))),
        checks=(
            "selection_isolation",
            "same_model_replay",
            "complete_replay",
            "strict_score_improvement",
            "accepted_control_non_regression",
            "rejected_target_non_regression",
            "textual_learning_rate",
            "rejected_edit_buffer",
        ),
        split_hash=batch.split_hash,
        replay_model=baseline.model if baseline.model == candidate.model else "",
        baseline_score=_decimal_text(baseline_score),
        candidate_score=_decimal_text(candidate_score),
        baseline_accepted_score=_decimal_text(baseline_accepted),
        candidate_accepted_score=_decimal_text(candidate_accepted),
        baseline_rejected_score=_decimal_text(baseline_rejected),
        candidate_rejected_score=_decimal_text(candidate_rejected),
        accepted_control_regressions=accepted_regressions,
        rejected_target_regressions=rejected_regressions,
        edit_budget=int(edit_budget),
        edit_count=int(edit_count),
        edit_signature=edit_signature,
    )
