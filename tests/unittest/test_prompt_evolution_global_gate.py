import asyncio
import hashlib
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from pr_agent.suggestions.prompt_evolution.gitlab_publisher import PromptWorkspace
from pr_agent.suggestions.prompt_evolution.lease import EvolutionLease
from pr_agent.suggestions.prompt_evolution.models import (
    CandidateScope,
    EligibleCandidate,
    Evidence,
    EvolutionRunStatus,
    HighFidelityEvaluationReport,
    Outcome,
    PromptChangeKind,
    PromptFileChange,
    PromptProposal,
    PublishedDraft,
    SourceSnapshot,
    ValidationReport,
    WeightedCluster,
)
from pr_agent.suggestions.prompt_evolution.runner import PromptEvolutionRunner
from pr_agent.suggestions.prompt_evolution.store import PromptEvolutionStore
from pr_agent.suggestions.prompt_provenance import compute_prompt_set_hash_from_contents

PROMPT_PATH = "pr_agent/settings/code_suggestions/pr_code_suggestions_prompts.toml"
PROMPT_CONTENT = '[pr_code_suggestions_prompt]\nsystem = "baseline"\nuser = "review"\n'
NOW = datetime(2026, 8, 27, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


def _evidence(identity: str, mr_iid: str, outcome: Outcome) -> Evidence:
    return Evidence(
        suggestion_id=identity,
        project="eabot/cook",
        mr_iid=mr_iid,
        mr_url=f"https://gitlab/eabot/cook/-/merge_requests/{mr_iid}",
        created_at="2026-08-27T00:00:00+08:00",
        file_path=f"src/{mr_iid}.py",
        label="bug",
        summary=identity,
        suggestion_content=identity,
        outcome=outcome,
        weight=1.0,
        global_prompt_set_hash="",
        prompt_bundle_hash="bundle",
        commit_sha=hashlib.sha256(f"head:{mr_iid}".encode()).hexdigest(),
        review_id=f"review-{mr_iid}",
        line_start=10,
        line_end=12,
        replayable=True,
    )


class DynamicAgent:
    async def generate(self, candidates, workspace):
        evidence_ids = tuple(
            item.suggestion_id for candidate in candidates for item in candidate.cluster.evidence
        )
        content = workspace.files[PROMPT_PATH].replace("baseline", "candidate")
        return PromptProposal(
            "improve rejected cases",
            PromptChangeKind.CONSERVATIVE_TIGHTENING,
            evidence_ids,
            (
                PromptFileChange(
                    PROMPT_PATH,
                    "generation",
                    hashlib.sha256(workspace.files[PROMPT_PATH].encode()).hexdigest(),
                    content,
                    evidence_ids,
                ),
            ),
        )

    async def regenerate(self, candidates, workspace, report):
        return await self.generate(candidates, workspace)


class StaticEvaluator:
    def __init__(self, report):
        self.report = report
        self.calls = []

    async def evaluate_pair(self, batch, workspace, proposal, **kwargs):
        self.calls.append((batch, workspace, proposal, kwargs))
        return self.report


def _report(*, passed=True, errors=()):
    return HighFidelityEvaluationReport(
        passed,
        errors,
        ("production_path", "prompt_only_treatment", "complete_coverage"),
        ("eabot/cook!3", "eabot/cook!9"),
        (),
        "0.5",
        "1" if passed else "0.5",
        "1",
        "1",
        "0",
        "1" if passed else "0",
        (),
    )


def _settings():
    return SimpleNamespace(
        prompt_evolution=SimpleNamespace(
            enabled=True,
            target_project="example-group/mr-agent",
            target_branch="main",
            branch_prefix="codex/prompt-evolution",
            lease_seconds=30,
            window_days=90,
            unhandled_after_days=14,
            project_min_negative_weight=3,
            project_min_negative_ratio=0.7,
            project_min_mrs=2,
            unhandled_only_min_count=12,
            unhandled_only_min_mrs=3,
            global_min_negative_weight=3,
            global_min_negative_ratio=0.7,
            global_min_projects=1,
            global_min_mrs=3,
            closed_candidate_cooldown_days=30,
            max_candidates_per_run=20,
            max_files_per_mr=20,
            max_prompt_file_chars=200000,
            max_diff_lines=600,
            project_skill_optimizer_selection_ratio=0.25,
            project_skill_optimizer_min_train_mrs=2,
            project_skill_optimizer_min_selection_mrs=1,
            project_skill_optimizer_min_control_cases=1,
            project_skill_optimizer_max_selection_cases=20,
            global_prompt_high_fidelity_enabled=True,
            global_prompt_high_fidelity_minimum_score_delta=0.05,
        )
    )


def _make_runner(tmp_path, monkeypatch, report, *, target_count=3):
    target_contents = {PROMPT_PATH.replace("pr_agent/settings/", "", 1): PROMPT_CONTENT}
    prompt_hash = compute_prompt_set_hash_from_contents(target_contents, tuple(target_contents))
    monkeypatch.setattr(
        "pr_agent.suggestions.prompt_evolution.runner.compute_global_prompt_set_hash",
        lambda: prompt_hash,
    )
    targets = tuple(
        Evidence(**{**_evidence(f"rejected-{index}", str(index), Outcome.REJECTED).__dict__,
                    "global_prompt_set_hash": prompt_hash})
        for index in range(1, target_count + 1)
    )
    control = Evidence(**{
        **_evidence("accepted-control", "9", Outcome.ACCEPTED).__dict__,
        "global_prompt_set_hash": prompt_hash,
    })
    cluster = WeightedCluster("global-cluster", targets, 0.0, float(len(targets)), 1.0)
    candidate = EligibleCandidate("global-candidate", CandidateScope.GLOBAL, None, prompt_hash, cluster)

    publisher = Mock()
    publisher.get_target_head.return_value = "a" * 40
    publisher.load_workspace.return_value = PromptWorkspace(
        "example-group/mr-agent",
        "main",
        "a" * 40,
        {PROMPT_PATH: PROMPT_CONTENT},
    )
    publisher.publish_draft_mr = AsyncMock(
        return_value=PublishedDraft("b" * 40, "12", "https://gitlab/mr/12")
    )
    publisher.get_mr_state.return_value = "opened"

    leases = Mock()
    leases.acquire = AsyncMock(return_value=EvolutionLease("example-group/mr-agent", "worker", 1))
    leases.renew = AsyncMock(return_value=True)
    leases.assert_current = AsyncMock()
    leases.release = AsyncMock(return_value=True)
    loader = Mock()
    loader.load.return_value = SourceSnapshot((*targets, control), "watermark", True)
    clusterer = Mock()
    clusterer.cluster.return_value = ((cluster,), ())
    aggregator = Mock()
    aggregator.select.return_value = (candidate,)
    validator = Mock()
    validator.validate.return_value = ValidationReport(True, (), ("toml_valid",))
    evaluator = StaticEvaluator(report)
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    runner = PromptEvolutionRunner(
        settings=_settings(),
        store=store,
        leases=leases,
        publisher=publisher,
        evidence_loader=loader,
        clusterer=clusterer,
        aggregator=aggregator,
        agent=DynamicAgent(),
        validator=validator,
        owner="worker",
        now=NOW,
        candidate_scopes=frozenset({CandidateScope.GLOBAL}),
        high_fidelity_evaluator=evaluator,
        behavioral_model="production-model",
    )
    return runner, publisher, evaluator, store


def test_global_prompt_gate_publishes_only_after_paired_replay_passes(tmp_path, monkeypatch):
    runner, publisher, evaluator, store = _make_runner(tmp_path, monkeypatch, _report())

    result = asyncio.run(runner.run(dry_run=False))

    assert result.status is EvolutionRunStatus.MR_OPEN
    assert len(evaluator.calls) == 1
    assert evaluator.calls[0][3]["model"] == "production-model"
    description = publisher.publish_draft_mr.await_args.kwargs["description"]
    assert "baseline_score=0.5" in description
    assert "candidate_score=1" in description
    assert "Offline behavioural evaluation: NOT RUN" not in description
    audit = store.get_prompt_evaluation_audit(result.run_id)
    assert audit is not None
    assert audit["report"]["passed"] is True


def test_global_prompt_gate_rejects_regression_without_creating_mr(tmp_path, monkeypatch):
    runner, publisher, evaluator, store = _make_runner(
        tmp_path,
        monkeypatch,
        _report(passed=False, errors=("high_fidelity_score_not_improved",)),
    )

    result = asyncio.run(runner.run(dry_run=False))

    assert result.status is EvolutionRunStatus.OPTIMIZATION_REJECTED
    assert result.error_code == "global_prompt_high_fidelity_gate_rejected"
    publisher.publish_draft_mr.assert_not_awaited()
    assert len(evaluator.calls) == 1
    assert store.get_prompt_evaluation_audit(result.run_id)["report"]["passed"] is False


def test_global_prompt_gate_rejects_insufficient_holdout_before_generation(tmp_path, monkeypatch):
    runner, publisher, evaluator, _store = _make_runner(
        tmp_path,
        monkeypatch,
        _report(),
        target_count=2,
    )

    result = asyncio.run(runner.run(dry_run=False))

    assert result.status is EvolutionRunStatus.INSUFFICIENT_VALIDATION
    assert result.error_code == "insufficient_global_prompt_evaluation_evidence"
    publisher.publish_draft_mr.assert_not_awaited()
    assert evaluator.calls == []
