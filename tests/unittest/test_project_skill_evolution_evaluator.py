import asyncio

import pytest

from pr_agent.suggestions.prompt_evolution.models import (
    Evidence,
    Outcome,
    ReplayAction,
    SkillOptimizationBatch,
)
from pr_agent.suggestions.prompt_evolution.project_skill_evaluator import (
    ProjectSkillReplayEvaluator,
    ReplayBatchOutput,
    ReplayDecisionOutput,
)


def _case(case_id, outcome):
    return Evidence(
        suggestion_id=case_id,
        project="eabot/cook",
        mr_iid="1" if case_id == "rejected" else "2",
        mr_url="url",
        created_at="2026-08-01T00:00:00+08:00",
        file_path="src/a.py",
        label="bug",
        summary="possible false positive",
        suggestion_content="Change this code.",
        outcome=outcome,
        weight=1.0,
        global_prompt_set_hash="global",
        prompt_bundle_hash="bundle",
        existing_code="old()",
        improved_code="new()",
    )


def _batch():
    cases = (_case("accepted", Outcome.ACCEPTED), _case("rejected", Outcome.REJECTED))
    return SkillOptimizationBatch(
        project="eabot/cook",
        base_manifest_hash="manifest",
        training_candidates=(),
        selection_cases=cases,
        control_ids=("accepted",),
        split_hash="split",
    )


def _output(accepted_action="emit", rejected_action="suppress"):
    return ReplayBatchOutput(decisions=[
        ReplayDecisionOutput(case_id="accepted", action=accepted_action, reason="accepted reason"),
        ReplayDecisionOutput(case_id="rejected", action=rejected_action, reason="rejected reason"),
    ])


class Client:
    def __init__(self, first=None, second=None, model="independent-model"):
        self.first = first or _output("emit", "emit")
        self.second = second or _output("emit", "suppress")
        self.model = model
        self.calls = []

    async def call_pair_same_model(self, *args):
        self.calls.append(args)
        return self.first, self.second, self.model


def test_replay_pair_uses_same_cases_and_returns_pinned_model_actions():
    client = Client()
    evaluator = ProjectSkillReplayEvaluator(client, "independent-model")

    baseline, candidate = asyncio.run(evaluator.replay_pair(
        _batch(),
        'project = "eabot/cook"\n# baseline-only-marker',
        'project = "eabot/cook"\n# candidate-only-marker',
    ))

    assert baseline.model == candidate.model == "independent-model"
    assert tuple(item.case_id for item in baseline.decisions) == ("accepted", "rejected")
    assert candidate.decisions[1].action is ReplayAction.SUPPRESS
    args = client.calls[0]
    assert "baseline-only-marker" in args[2] and "candidate-only-marker" not in args[2]
    assert "candidate-only-marker" in args[3] and "baseline-only-marker" not in args[3]
    assert "old()" in args[2] and "old()" in args[3]
    assert '"outcome"' not in args[2]
    assert '"feedback"' not in args[2]


@pytest.mark.parametrize(
    "bad_output",
    [
        ReplayBatchOutput(decisions=[
            ReplayDecisionOutput(case_id="accepted", action="emit", reason="only one"),
        ]),
        ReplayBatchOutput(decisions=[
            ReplayDecisionOutput(case_id="accepted", action="emit", reason="first"),
            ReplayDecisionOutput(case_id="accepted", action="suppress", reason="duplicate"),
        ]),
        ReplayBatchOutput(decisions=[
            ReplayDecisionOutput(case_id="accepted", action="emit", reason="known"),
            ReplayDecisionOutput(case_id="invented", action="suppress", reason="unknown"),
        ]),
    ],
)
def test_replay_pair_rejects_missing_duplicate_and_unknown_case_ids(bad_output):
    evaluator = ProjectSkillReplayEvaluator(Client(second=bad_output), "independent-model")

    with pytest.raises(ValueError, match="case IDs"):
        asyncio.run(evaluator.replay_pair(_batch(), "baseline", "candidate"))
