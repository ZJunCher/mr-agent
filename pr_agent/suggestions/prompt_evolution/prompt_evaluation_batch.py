"""MR-isolated training/Holdout split for global Prompt evolution."""

from __future__ import annotations

import hashlib
import json
import math

from pr_agent.suggestions.prompt_evolution.models import (
    EligibleCandidate,
    Evidence,
    Outcome,
    PromptEvaluationBatch,
    WeightedCluster,
)
from pr_agent.suggestions.prompt_evolution.project_skill_optimizer import InsufficientValidationEvidence


def _hash(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _key(evidence: Evidence) -> str:
    return f"{evidence.project}\0{evidence.mr_iid}"


def _deduplicate(values: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    result = {}
    for item in sorted(values, key=lambda value: (value.suggestion_id, value.created_at)):
        existing = result.get(item.suggestion_id)
        if existing is not None and existing != item:
            raise InsufficientValidationEvidence(f"conflicting evidence for {item.suggestion_id}")
        result[item.suggestion_id] = item
    return tuple(result[key] for key in sorted(result))


def _training_candidates(
    candidates: tuple[EligibleCandidate, ...],
    selection_keys: frozenset[str],
) -> tuple[EligibleCandidate, ...]:
    rebuilt = []
    for candidate in sorted(candidates, key=lambda item: item.candidate_id):
        evidence = tuple(item for item in candidate.cluster.evidence if _key(item) not in selection_keys)
        if not evidence:
            continue
        positive = sum(item.weight for item in evidence if item.outcome is Outcome.ACCEPTED)
        negative = sum(item.weight for item in evidence if item.outcome in {Outcome.REJECTED, Outcome.UNHANDLED})
        denominator = positive + negative
        rebuilt.append(EligibleCandidate(
            candidate.candidate_id,
            candidate.scope,
            candidate.project,
            candidate.source_prompt_hash,
            WeightedCluster(
                candidate.cluster.cluster_key,
                evidence,
                positive,
                negative,
                negative / denominator if denominator else 0.0,
            ),
        ))
    return tuple(rebuilt)


def build_prompt_evaluation_batch(
    candidates: tuple[EligibleCandidate, ...],
    all_evidence: tuple[Evidence, ...],
    *,
    base_prompt_hash: str,
    selection_ratio: float,
    min_train_mrs: int,
    min_selection_mrs: int,
    min_control_cases: int,
    max_selection_cases: int,
) -> PromptEvaluationBatch:
    if not candidates:
        raise InsufficientValidationEvidence("no global Prompt candidates")
    if not 0 < selection_ratio < 1:
        raise ValueError("selection_ratio must be between zero and one")
    target = _deduplicate(tuple(item for candidate in candidates for item in candidate.cluster.evidence))
    if any(item.global_prompt_set_hash != base_prompt_hash for item in target):
        raise InsufficientValidationEvidence("candidate evidence is not from the current Prompt version")
    target_keys = sorted({_key(item) for item in target})
    required_groups = min_train_mrs + min_selection_mrs
    if len(target_keys) < required_groups:
        raise InsufficientValidationEvidence("not enough MR groups for Prompt train/selection isolation")
    eligible_selection_keys = []
    for group_key in target_keys:
        group = tuple(item for item in target if _key(item) == group_key)
        identities = {(item.review_id, item.commit_sha) for item in group}
        if (
            all(item.replayable for item in group)
            and len(identities) == 1
            and all(value for identity in identities for value in identity)
        ):
            eligible_selection_keys.append(group_key)
    if len(eligible_selection_keys) < min_selection_mrs:
        raise InsufficientValidationEvidence("not enough replayable Prompt MR groups for selection")
    desired = max(min_selection_mrs, int(math.ceil(len(target_keys) * selection_ratio)))
    desired = min(desired, len(target_keys) - min_train_mrs, len(eligible_selection_keys))
    ranked = sorted(eligible_selection_keys, key=lambda value: _hash(base_prompt_hash, "selection", value))
    selection_keys = frozenset(ranked[:desired])
    training = _training_candidates(candidates, selection_keys)
    training_keys = {_key(item) for candidate in training for item in candidate.cluster.evidence}
    if len(training_keys) < min_train_mrs or training_keys.intersection(selection_keys):
        raise InsufficientValidationEvidence("Prompt train/selection isolation failed")

    selection_targets = tuple(
        item for item in target
        if _key(item) in selection_keys and item.outcome in {Outcome.ACCEPTED, Outcome.REJECTED}
    )
    target_ids = {item.suggestion_id for item in target}
    controls = tuple(
        item for item in _deduplicate(all_evidence)
        if item.outcome is Outcome.ACCEPTED
        and item.global_prompt_set_hash == base_prompt_hash
        and item.suggestion_id not in target_ids
        and _key(item) not in set(target_keys)
        and item.replayable
    )
    controls = tuple(sorted(
        controls,
        key=lambda item: _hash(base_prompt_hash, "control", item.suggestion_id),
    ))[:max(0, max_selection_cases - len(selection_targets))]
    if len(controls) < min_control_cases:
        raise InsufficientValidationEvidence("not enough accepted Prompt control cases")
    selection_cases = tuple(sorted(selection_targets + controls, key=lambda item: item.suggestion_id))
    if not selection_targets or len(selection_cases) > max_selection_cases:
        raise InsufficientValidationEvidence("Prompt selection set is empty or too large")
    if any(not item.replayable for item in selection_cases):
        raise InsufficientValidationEvidence("Prompt selection contains non-replayable evidence")
    for group_key in {_key(item) for item in selection_cases}:
        group = tuple(item for item in selection_cases if _key(item) == group_key)
        identities = {(item.review_id, item.commit_sha) for item in group}
        if len(identities) != 1 or any(not value for identity in identities for value in identity):
            raise InsufficientValidationEvidence("Prompt selection lacks one immutable review identity per MR")
    control_ids = tuple(sorted(item.suggestion_id for item in controls))
    payload = {
        "base_prompt_hash": base_prompt_hash,
        "training_ids": sorted(item.suggestion_id for candidate in training for item in candidate.cluster.evidence),
        "selection_ids": [item.suggestion_id for item in selection_cases],
        "control_ids": list(control_ids),
    }
    split_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return PromptEvaluationBatch(base_prompt_hash, training, selection_cases, control_ids, split_hash)
