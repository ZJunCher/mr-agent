import hashlib

import pytest

from pr_agent.suggestions.prompt_evolution.models import (
    CandidateScope,
    EligibleCandidate,
    Evidence,
    Outcome,
    WeightedCluster,
)
from pr_agent.suggestions.prompt_evolution.project_skill_optimizer import InsufficientValidationEvidence
from pr_agent.suggestions.prompt_evolution.prompt_evaluation_batch import build_prompt_evaluation_batch


def _evidence(identity: str, project: str, mr_iid: str, outcome: Outcome) -> Evidence:
    return Evidence(
        suggestion_id=identity,
        project=project,
        mr_iid=mr_iid,
        mr_url=f"https://gitlab/{project}/-/merge_requests/{mr_iid}",
        created_at="2026-08-27T00:00:00+08:00",
        file_path="src/a.py",
        label="bug",
        summary=identity,
        suggestion_content=identity,
        outcome=outcome,
        weight=1.0,
        global_prompt_set_hash="prompt-v1",
        prompt_bundle_hash="bundle-v1",
        commit_sha=hashlib.sha256(f"head:{project}:{mr_iid}".encode()).hexdigest(),
        review_id=f"review-{project}-{mr_iid}",
        replayable=True,
    )


def _candidate(targets: tuple[Evidence, ...]) -> EligibleCandidate:
    return EligibleCandidate(
        candidate_id="candidate-1",
        scope=CandidateScope.GLOBAL,
        project=None,
        source_prompt_hash="prompt-v1",
        cluster=WeightedCluster("cluster", targets, 0.0, float(len(targets)), 1.0),
    )


def test_prompt_batch_isolates_mrs_and_adds_accepted_control():
    targets = tuple(_evidence(f"rejected-{index}", "g/r", str(index), Outcome.REJECTED) for index in range(3))
    control = _evidence("accepted-control", "g/other", "20", Outcome.ACCEPTED)

    batch = build_prompt_evaluation_batch(
        (_candidate(targets),),
        (*targets, control),
        base_prompt_hash="prompt-v1",
        selection_ratio=0.25,
        min_train_mrs=2,
        min_selection_mrs=1,
        min_control_cases=1,
        max_selection_cases=10,
    )

    training_mrs = {
        (item.project, item.mr_iid)
        for candidate in batch.training_candidates
        for item in candidate.cluster.evidence
    }
    selection_mrs = {(item.project, item.mr_iid) for item in batch.selection_cases}
    assert training_mrs.isdisjoint(selection_mrs)
    assert batch.control_ids == ("accepted-control",)
    assert len(batch.split_hash) == 64


def test_prompt_batch_rejects_non_replayable_holdout():
    targets = tuple(_evidence(f"rejected-{index}", "g/r", str(index), Outcome.REJECTED) for index in range(2))
    targets = tuple(Evidence(**{**item.__dict__, "replayable": False}) for item in targets)
    control = _evidence("accepted-control", "g/other", "20", Outcome.ACCEPTED)

    with pytest.raises(InsufficientValidationEvidence, match="replayable"):
        build_prompt_evaluation_batch(
            (_candidate(targets),),
            (*targets, control),
            base_prompt_hash="prompt-v1",
            selection_ratio=0.5,
            min_train_mrs=1,
            min_selection_mrs=1,
            min_control_cases=1,
            max_selection_cases=10,
        )


def test_prompt_batch_keeps_nonreplayable_case_on_training_side_when_holdout_exists():
    targets = tuple(
        _evidence(f"rejected-{index}", "g/r", str(index), Outcome.REJECTED)
        for index in range(3)
    )
    targets = (
        Evidence(**{**targets[0].__dict__, "replayable": False, "review_id": "", "commit_sha": ""}),
        *targets[1:],
    )
    control = _evidence("accepted-control", "g/other", "20", Outcome.ACCEPTED)

    batch = build_prompt_evaluation_batch(
        (_candidate(targets),),
        (*targets, control),
        base_prompt_hash="prompt-v1",
        selection_ratio=0.25,
        min_train_mrs=2,
        min_selection_mrs=1,
        min_control_cases=1,
        max_selection_cases=10,
    )

    training_ids = {
        item.suggestion_id
        for candidate in batch.training_candidates
        for item in candidate.cluster.evidence
    }
    assert "rejected-0" in training_ids
    assert all(case.replayable for case in batch.selection_cases)


def test_prompt_batch_keeps_prompt_attributable_execution_case_in_holdout():
    targets = tuple(
        Evidence(**{
            **_evidence(f"schema-{index}", "g/r", str(index), Outcome.REJECTED).__dict__,
            "case_kind": "output_schema_error",
            "expected_action": "suppress",
        })
        for index in range(3)
    )
    control = _evidence("accepted-control", "g/other", "20", Outcome.ACCEPTED)

    batch = build_prompt_evaluation_batch(
        (_candidate(targets),),
        (*targets, control),
        base_prompt_hash="prompt-v1",
        selection_ratio=0.25,
        min_train_mrs=2,
        min_selection_mrs=1,
        min_control_cases=1,
        max_selection_cases=10,
    )

    assert any(case.case_kind == "output_schema_error" for case in batch.selection_cases)
