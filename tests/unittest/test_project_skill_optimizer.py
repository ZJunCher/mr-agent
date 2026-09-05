from dataclasses import replace

import pytest

from pr_agent.suggestions.prompt_evolution.models import (
    CandidateScope,
    EligibleCandidate,
    Evidence,
    Outcome,
    ReplayAction,
    SkillReplayDecision,
    SkillReplayResult,
    WeightedCluster,
)
from pr_agent.suggestions.prompt_evolution.project_skill_optimizer import (
    InsufficientValidationEvidence,
    build_optimization_batch,
    evaluate_skill_gate,
    semantic_skill_diff,
)

PROJECT = "eabot/cook"
MANIFEST_HASH = "manifest-v1"
BASE_SKILL = (
    'schema_version = 1\nname = "cook"\nproject = "eabot/cook"\n'
    '[[rules]]\nid = "api"\ntargets = ["improve"]\nlanguages = ["python"]\n'
    'instruction = "Check API compatibility."\n'
)


def _evidence(suggestion_id, mr_iid, outcome, *, in_candidate=True):
    return Evidence(
        suggestion_id=suggestion_id,
        project=PROJECT,
        mr_iid=str(mr_iid),
        mr_url=f"https://gitlab/{PROJECT}/-/merge_requests/{mr_iid}",
        created_at=f"2026-08-{int(mr_iid):02d}T00:00:00+08:00",
        file_path="src/a.py",
        label="bug" if in_candidate else "style",
        summary=f"case {suggestion_id}",
        suggestion_content=f"suggestion {suggestion_id}",
        outcome=outcome,
        weight=1.0,
        global_prompt_set_hash="global",
        prompt_bundle_hash="bundle",
        project_skill_manifest_hash=MANIFEST_HASH,
        project_skill_status="loaded",
        existing_code="old()",
        improved_code="new()",
    )


def _candidate(evidence):
    evidence = tuple(evidence)
    positive = sum(item.weight for item in evidence if item.outcome is Outcome.ACCEPTED)
    negative = sum(item.weight for item in evidence if item.outcome is not Outcome.ACCEPTED)
    return EligibleCandidate(
        candidate_id="project:candidate",
        scope=CandidateScope.PROJECT,
        project=PROJECT,
        source_prompt_hash="bundle",
        cluster=WeightedCluster("false-positive", evidence, positive, negative, 1.0),
    )


def _batch_inputs():
    target = tuple(
        _evidence(f"r{mr}", mr, Outcome.REJECTED)
        for mr in (1, 2, 3, 4)
    )
    controls = tuple(
        _evidence(f"a{mr}", mr, Outcome.ACCEPTED, in_candidate=False)
        for mr in (5, 6)
    )
    return (_candidate(target),), target + controls


def _build(candidates=None, evidence=None):
    default_candidates, default_evidence = _batch_inputs()
    return build_optimization_batch(
        candidates or default_candidates,
        evidence or default_evidence,
        project=PROJECT,
        base_manifest_hash=MANIFEST_HASH,
        selection_ratio=0.25,
        min_train_mrs=2,
        min_selection_mrs=1,
        min_control_cases=1,
        max_selection_cases=20,
    )


def test_split_is_stable_mr_isolated_and_hidden_from_generator():
    candidates, evidence = _batch_inputs()
    batch = _build(candidates, evidence)
    reversed_batch = _build(tuple(reversed(candidates)), tuple(reversed(evidence)))

    train_mrs = {
        item.mr_iid
        for candidate in batch.training_candidates
        for item in candidate.cluster.evidence
    }
    selection_mrs = {item.mr_iid for item in batch.selection_cases if item.suggestion_id.startswith("r")}
    generator_ids = {
        item.suggestion_id
        for candidate in batch.training_candidates
        for item in candidate.cluster.evidence
    }

    assert batch == reversed_batch
    assert train_mrs.isdisjoint(selection_mrs)
    assert not generator_ids.intersection(batch.selection_ids)
    assert batch.control_ids
    assert all(item.outcome in {Outcome.ACCEPTED, Outcome.REJECTED} for item in batch.selection_cases)


def test_split_rejects_wrong_version_and_insufficient_mrs():
    candidates, evidence = _batch_inputs()
    stale = tuple(replace(item, project_skill_manifest_hash="stale") for item in evidence)
    with pytest.raises(InsufficientValidationEvidence, match="current-version"):
        _build(candidates, stale)

    unversioned_candidates = tuple(
        replace(
            candidate,
            cluster=replace(
                candidate.cluster,
                evidence=tuple(
                    replace(item, project_skill_manifest_hash="")
                    for item in candidate.cluster.evidence
                ),
            ),
        )
        for candidate in candidates
    )
    with pytest.raises(InsufficientValidationEvidence, match="current-version"):
        _build(unversioned_candidates, evidence)

    too_small = tuple(item for item in candidates[0].cluster.evidence if item.mr_iid in {"1", "2"})
    with pytest.raises(InsufficientValidationEvidence, match="train/selection"):
        _build((_candidate(too_small),), too_small + evidence[-2:])


def test_split_rejects_selection_payload_over_case_limit():
    candidates, evidence = _batch_inputs()
    with pytest.raises(InsufficientValidationEvidence, match="case limit"):
        build_optimization_batch(
            candidates,
            evidence,
            project=PROJECT,
            base_manifest_hash=MANIFEST_HASH,
            selection_ratio=0.5,
            min_train_mrs=2,
            min_selection_mrs=1,
            min_control_cases=1,
            max_selection_cases=2,
        )


def test_semantic_diff_ignores_formatting_and_counts_changed_rules():
    formatting_only = BASE_SKILL.replace('name = "cook"', 'name="cook"')
    one_change = BASE_SKILL.replace(
        'instruction = "Check API compatibility."',
        'instruction = "Check API compatibility with direct evidence."',
    )
    two_changes = one_change + (
        '[[rules]]\nid = "tests"\ntargets = ["improve"]\nlanguages = ["python"]\n'
        'instruction = "Require tests for behavior changes."\n'
    )

    assert semantic_skill_diff(BASE_SKILL, formatting_only, PROJECT).edit_count == 0
    assert semantic_skill_diff(BASE_SKILL, one_change, PROJECT).edit_count == 1
    diff = semantic_skill_diff(BASE_SKILL, two_changes, PROJECT)
    assert diff.edit_count == 2
    assert diff.changed_rule_ids == ("api", "tests")


def _replay(batch, model, actions):
    return SkillReplayResult(
        model=model,
        decisions=tuple(
            SkillReplayDecision(item.suggestion_id, actions[item.suggestion_id], "reason")
            for item in batch.selection_cases
        ),
    )


def test_gate_requires_strict_delta_and_preserves_controls():
    batch = _build()
    baseline_actions = {
        item.suggestion_id: ReplayAction.EMIT
        for item in batch.selection_cases
    }
    candidate_actions = {
        item.suggestion_id: (
            ReplayAction.EMIT if item.outcome is Outcome.ACCEPTED else ReplayAction.SUPPRESS
        )
        for item in batch.selection_cases
    }
    baseline = _replay(batch, "judge", baseline_actions)
    candidate = _replay(batch, "judge", candidate_actions)

    report = evaluate_skill_gate(
        batch,
        baseline,
        candidate,
        minimum_score_delta="0.05",
        edit_budget=1,
        edit_count=1,
        edit_signature="signature",
    )

    assert report.passed
    assert report.action == "accept_new_best"
    assert float(report.candidate_score) > float(report.baseline_score)
    assert report.accepted_control_regressions == ()

    same = evaluate_skill_gate(
        batch,
        baseline,
        baseline,
        minimum_score_delta="0.05",
        edit_budget=1,
        edit_count=1,
        edit_signature="signature",
    )
    assert not same.passed
    assert "score_not_strictly_better" in same.errors


def test_gate_rejects_control_regression_model_mismatch_and_incomplete_ids():
    batch = _build()
    correct = {
        item.suggestion_id: (
            ReplayAction.EMIT if item.outcome is Outcome.ACCEPTED else ReplayAction.SUPPRESS
        )
        for item in batch.selection_cases
    }
    baseline = _replay(batch, "judge", {key: ReplayAction.EMIT for key in correct})
    regressed = dict(correct)
    regressed[batch.control_ids[0]] = ReplayAction.SUPPRESS
    candidate = _replay(batch, "judge", regressed)

    control_report = evaluate_skill_gate(
        batch,
        baseline,
        candidate,
        minimum_score_delta="0",
        edit_budget=1,
        edit_count=1,
        edit_signature="signature",
    )
    assert not control_report.passed
    assert "accepted_control_regression" in control_report.errors

    wrong_model = replace(candidate, model="other")
    mismatch = evaluate_skill_gate(
        batch,
        baseline,
        wrong_model,
        minimum_score_delta="0",
        edit_budget=1,
        edit_count=1,
        edit_signature="signature",
    )
    assert "replay_model_mismatch" in mismatch.errors

    incomplete = replace(candidate, decisions=candidate.decisions[:-1])
    missing = evaluate_skill_gate(
        batch,
        baseline,
        incomplete,
        minimum_score_delta="0",
        edit_budget=1,
        edit_count=1,
        edit_signature="signature",
    )
    assert "candidate_replay_incomplete" in missing.errors


def test_gate_rejects_edit_budget_and_repeated_rejected_signature():
    batch = _build()
    actions = {
        item.suggestion_id: (
            ReplayAction.EMIT if item.outcome is Outcome.ACCEPTED else ReplayAction.SUPPRESS
        )
        for item in batch.selection_cases
    }
    replay = _replay(batch, "judge", actions)
    report = evaluate_skill_gate(
        batch,
        replay,
        replay,
        minimum_score_delta="0",
        edit_budget=1,
        edit_count=2,
        edit_signature="repeated",
        rejected_signatures=("repeated",),
    )
    assert "textual_learning_rate_exceeded" in report.errors
    assert "repeated_rejected_edit" in report.errors
