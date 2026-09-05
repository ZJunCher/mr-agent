import asyncio

import pytest

from pr_agent.suggestions.prompt_evolution.agent import (
    PromptEvolutionAgent,
    PromptFileChangeOutput,
    PromptProposalOutput,
)
from pr_agent.suggestions.prompt_evolution.gitlab_publisher import PromptWorkspace
from pr_agent.suggestions.prompt_evolution.models import (
    CandidateScope,
    EligibleCandidate,
    Evidence,
    MISSING_FILE_HASH,
    Outcome,
    WeightedCluster,
)


class StaticClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def call(self, model, system, user, tool_name, result_model):
        self.calls.append((model, system, user, tool_name))
        return self.result


def _eligible_project_candidate(feedback: tuple[str, ...]) -> EligibleCandidate:
    evidence = Evidence(
        suggestion_id="s1",
        project="eabot/cook",
        mr_iid="1",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/1",
        created_at="2026-08-01T00:00:00+08:00",
        file_path="src/a.py",
        label="bug",
        summary="Avoid speculative changes",
        suggestion_content="A speculative replacement was proposed",
        outcome=Outcome.REJECTED,
        weight=1.0,
        global_prompt_set_hash="g1",
        prompt_bundle_hash="b1",
        feedback=feedback,
    )
    cluster = WeightedCluster("speculation", (evidence,), 0.0, 1.0, 1.0)
    return EligibleCandidate("c1", CandidateScope.PROJECT, "eabot/cook", "b1", cluster)


def _workspace() -> PromptWorkspace:
    path = ".pr_agent/skills/review/skill.toml"
    return PromptWorkspace("eabot/cook", "main", "a" * 40, {path: 'schema_version = 1\nproject = "eabot/cook"\n'})


def _output(change_kind: str, evidence_ids: list[str]) -> PromptProposalOutput:
    path = ".pr_agent/skills/review/skill.toml"
    content = 'schema_version = 1\nproject = "eabot/cook"\n'
    return PromptProposalOutput(
        rationale="Reduce repeated speculative suggestions",
        change_kind=change_kind,
        evidence_ids=evidence_ids,
        changes=[PromptFileChangeOutput(
            path=path,
            family="project_rule",
            expected_base_sha256="unused-in-agent-validation",
            content=content,
            evidence_ids=evidence_ids,
        )],
    )


def test_specific_project_rule_requires_text_feedback():
    candidate = _eligible_project_candidate(feedback=())
    agent = PromptEvolutionAgent(StaticClient(
        _output(change_kind="specific_rule", evidence_ids=[candidate.cluster.evidence[0].suggestion_id])
    ), model="model")
    with pytest.raises(ValueError, match="specific project rule requires textual feedback"):
        asyncio.run(agent.generate((candidate,), _workspace()))


def test_conservative_tightening_allows_outcome_only_evidence():
    candidate = _eligible_project_candidate(feedback=())
    agent = PromptEvolutionAgent(StaticClient(
        _output(change_kind="conservative_tightening", evidence_ids=[candidate.cluster.evidence[0].suggestion_id])
    ), model="model")
    proposal = asyncio.run(agent.generate((candidate,), _workspace()))
    assert proposal.changes


def test_generator_receives_bounded_rejected_edit_summaries():
    candidate = _eligible_project_candidate(feedback=())
    client = StaticClient(_output(
        change_kind="conservative_tightening",
        evidence_ids=[candidate.cluster.evidence[0].suggestion_id],
    ))
    agent = PromptEvolutionAgent(client, model="model")

    asyncio.run(agent.generate(
        (candidate,),
        _workspace(),
        rejected_edits=({
            "edit_signature": "deadbeef",
            "errors": ("score_not_strictly_better",),
            "baseline_score": "0.5",
            "candidate_score": "0.5",
        },),
    ))

    user = client.calls[0][2]
    assert "deadbeef" in user
    assert "score_not_strictly_better" in user
