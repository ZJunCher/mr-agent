import asyncio
import hashlib
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

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
    ReplayAction,
    SkillReplayDecision,
    SkillReplayResult,
    SourceSnapshot,
    ValidationReport,
    WeightedCluster,
)
from pr_agent.suggestions.prompt_evolution.project_skill_runner import ProjectSkillEvolutionRunner
from pr_agent.suggestions.prompt_evolution.store import PromptEvolutionStore

NOW = datetime(2026, 8, 26, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
PROJECT = "eabot/cook"
BASE_SHA = "a" * 40
PATH = ".pr_agent/skills/review/skill.toml"
BASE_SKILL = 'schema_version = 1\nname = "cook"\nproject = "eabot/cook"\n'
MANIFEST_HASH = hashlib.sha256(BASE_SKILL.encode()).hexdigest()


def _settings(enabled=True, gate_mode="enforce", high_fidelity_mode="enforce"):
    return SimpleNamespace(config=SimpleNamespace(model="production-model"), prompt_evolution=SimpleNamespace(
        enabled=enabled,
        window_days=90,
        unhandled_after_days=14,
        project_min_negative_weight=3.0,
        project_min_negative_ratio=0.70,
        project_min_mrs=2,
        unhandled_only_min_count=12,
        unhandled_only_min_mrs=3,
        global_min_negative_weight=5.0,
        global_min_negative_ratio=0.70,
        global_min_projects=2,
        global_min_mrs=3,
        max_candidates_per_run=20,
        closed_candidate_cooldown_days=30,
        lease_seconds=300,
        max_prompt_file_chars=200_000,
        max_diff_lines=600,
        project_skill_branch_prefix="codex/review-skill-evolution",
        project_skill_optimizer_enabled=True,
        project_skill_optimizer_gate_mode=gate_mode,
        project_skill_optimizer_edit_budget=1,
        project_skill_optimizer_max_edit_budget=3,
        project_skill_optimizer_selection_ratio=0.25,
        project_skill_optimizer_min_train_mrs=2,
        project_skill_optimizer_min_selection_mrs=1,
        project_skill_optimizer_min_control_cases=1,
        project_skill_optimizer_max_selection_cases=20,
        project_skill_optimizer_minimum_score_delta=0.05,
        project_skill_optimizer_rejected_buffer_size=10,
        project_skill_high_fidelity_enabled=True,
        project_skill_high_fidelity_gate_mode=high_fidelity_mode,
        project_skill_high_fidelity_min_mrs=1,
        project_skill_high_fidelity_max_mrs=10,
    ))


def _evidence(
    suggestion_id="s1",
    mr_iid="1",
    outcome=Outcome.REJECTED,
    manifest_hash=MANIFEST_HASH,
):
    return Evidence(
        suggestion_id=suggestion_id,
        project=PROJECT,
        mr_iid=str(mr_iid),
        mr_url=f"https://gitlab/eabot/cook/-/merge_requests/{mr_iid}",
        created_at="2026-08-20T00:00:00+08:00",
        file_path="src/a.py",
        label="bug",
        summary="false positive",
        suggestion_content="avoid it",
        outcome=outcome,
        weight=1.0,
        global_prompt_set_hash="global",
        prompt_bundle_hash="bundle",
        project_rules_hash="legacy",
        project_skill_manifest_hash=manifest_hash,
        project_skill_status="loaded",
        existing_code="old()",
        improved_code="new()",
    )


def _candidate(evidence=None):
    evidence = tuple(evidence or (_evidence(),))
    positive = sum(item.weight for item in evidence if item.outcome is Outcome.ACCEPTED)
    negative = sum(item.weight for item in evidence if item.outcome is not Outcome.ACCEPTED)
    cluster = WeightedCluster("false-positive", evidence, positive, negative, 1.0)
    return EligibleCandidate("candidate", CandidateScope.PROJECT, PROJECT, "bundle", cluster)


def _proposal(evidence_ids=("s1",)):
    content = BASE_SKILL + (
        '[[rules]]\nid = "python-evidence"\ntargets = ["review", "improve"]\n'
        'languages = ["python"]\ninstruction = "Require direct Python evidence."\n'
    )
    return PromptProposal(
        "Reduce a repeated false positive",
        PromptChangeKind.SPECIFIC_RULE,
        tuple(evidence_ids),
        (PromptFileChange(PATH, "project_rule", MANIFEST_HASH, content, tuple(evidence_ids)),),
    )


class Harness:
    def __init__(self, content=BASE_SKILL, *, target_mrs=(1, 2, 3, 4), gate_mode="enforce",
                 high_fidelity_mode="enforce"):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PromptEvolutionStore(f"{self.temp.name}/evolution.db")
        self.leases = Mock()
        self.leases.acquire = AsyncMock(return_value=SimpleNamespace(fencing_token=7))
        self.leases.renew = AsyncMock(return_value=True)
        self.leases.assert_current = AsyncMock()
        self.leases.release = AsyncMock()
        self.publisher = Mock()
        self.publisher.get_default_branch = Mock(return_value="main")
        self.publisher.get_target_head = Mock(return_value=BASE_SHA)
        self.publisher.load_workspace = Mock(return_value=SimpleNamespace(
            project_path=PROJECT,
            target_branch="main",
            base_sha=BASE_SHA,
            files={PATH: content},
        ))
        self.publisher.publish_draft_mr = AsyncMock(return_value=SimpleNamespace(
            commit_sha="c" * 40,
            mr_iid="9",
            mr_url="https://gitlab/eabot/cook/-/merge_requests/9",
        ))
        self.publisher.get_mr_state = Mock(return_value="opened")
        target_evidence = tuple(
            _evidence(f"s{mr}", mr, Outcome.REJECTED)
            for mr in target_mrs
        )
        control_evidence = tuple(
            _evidence(f"accepted-{mr}", mr, Outcome.ACCEPTED)
            for mr in (10, 11)
        )
        candidate = _candidate(target_evidence)
        self.evidence_loader = Mock()
        self.evidence_loader.load = Mock(return_value=SourceSnapshot(
            candidate.cluster.evidence + control_evidence,
            "2026-08-26T00:00:00+08:00",
            True,
        ))
        self.clusterer = Mock()
        self.clusterer.cluster = Mock(return_value=((candidate.cluster,), ()))
        self.aggregator = Mock()
        self.aggregator.select = Mock(return_value=(candidate,))
        self.agent = Mock()
        def proposal_for(training_candidates, workspace, **kwargs):
            evidence_ids = tuple(
                item.suggestion_id
                for candidate_item in training_candidates
                for item in candidate_item.cluster.evidence
            )
            return _proposal(evidence_ids)

        self.agent.generate = AsyncMock(side_effect=proposal_for)
        self.agent.regenerate = AsyncMock(
            side_effect=lambda training_candidates, workspace, report, **kwargs: proposal_for(
                training_candidates, workspace, **kwargs
            )
        )
        self.validator = Mock()
        self.validator.validate = Mock(return_value=ValidationReport(True, checks=("schema", "scope")))
        self.evaluator = Mock()
        def replay_pair(batch, baseline_content, candidate_content):
            baseline = SkillReplayResult(
                model="independent-model",
                decisions=tuple(
                    SkillReplayDecision(item.suggestion_id, ReplayAction.EMIT, "baseline")
                    for item in batch.selection_cases
                ),
            )
            candidate_result = SkillReplayResult(
                model="independent-model",
                decisions=tuple(
                    SkillReplayDecision(
                        item.suggestion_id,
                        ReplayAction.EMIT if item.outcome is Outcome.ACCEPTED else ReplayAction.SUPPRESS,
                        "candidate",
                    )
                    for item in batch.selection_cases
                ),
            )
            return baseline, candidate_result

        self.evaluator.replay_pair = AsyncMock(side_effect=replay_pair)
        self.high_fidelity_evaluator = Mock()
        self.high_fidelity_evaluator.evaluate_pair = AsyncMock(return_value=HighFidelityEvaluationReport(
            passed=True,
            errors=(),
            checks=("production_path", "paired_conditions", "complete_coverage"),
            replayed_mrs=("3", "10"),
            case_results=(),
            baseline_score="0.5",
            candidate_score="1",
            baseline_accepted_score="1",
            candidate_accepted_score="1",
            baseline_rejected_score="0",
            candidate_rejected_score="1",
            condition_hashes=(("3", "baseline-condition", "candidate-condition"),),
        ))
        self.runner = ProjectSkillEvolutionRunner(
            settings=_settings(gate_mode=gate_mode, high_fidelity_mode=high_fidelity_mode),
            store=self.store,
            leases=self.leases,
            publisher_factory=lambda project: self.publisher,
            evidence_loader=self.evidence_loader,
            clusterer=self.clusterer,
            aggregator=self.aggregator,
            agent=self.agent,
            validator=self.validator,
            evaluator=self.evaluator,
            high_fidelity_evaluator=self.high_fidelity_evaluator,
            owner="worker",
            now=NOW,
        )


def test_opted_in_project_publishes_only_fixed_manifest_as_draft():
    harness = Harness()

    results = asyncio.run(harness.runner.run(dry_run=False))

    assert len(results) == 1 and results[0].status is EvolutionRunStatus.MR_OPEN
    call = harness.publisher.publish_draft_mr.await_args.kwargs
    assert call["branch_name"].startswith("codex/review-skill-evolution/")
    assert call["target_branch"] == "main"
    assert tuple(change.path for change in call["changes"]) == (PATH,)
    assert "never auto-merged" in call["description"]
    assert "baseline_score=" in call["description"]
    assert "candidate_score=" in call["description"]
    assert "split_hash=" in call["description"]
    harness.leases.acquire.assert_awaited_once_with(PROJECT, "worker", 300)


def test_missing_manifest_is_opt_out_and_never_calls_model_or_publisher():
    harness = Harness(content=None)

    result = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert result.status is EvolutionRunStatus.COMPLETED_NO_CHANGE
    assert result.error_code == "project_skill_not_opted_in"
    harness.agent.generate.assert_not_awaited()
    harness.publisher.publish_draft_mr.assert_not_awaited()


def test_changed_manifest_supersedes_stale_evidence():
    harness = Harness(content=BASE_SKILL + "description = \"changed\"\n")

    result = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert result.status is EvolutionRunStatus.SUPERSEDED
    assert result.error_code == "project_skill_version_mismatch"
    harness.agent.generate.assert_not_awaited()
    harness.publisher.publish_draft_mr.assert_not_awaited()


def test_same_batch_is_idempotent_and_does_not_publish_twice():
    harness = Harness()

    first = asyncio.run(harness.runner.run(dry_run=False))[0]
    second = asyncio.run(harness.runner.run(dry_run=False))

    assert first.status is EvolutionRunStatus.MR_OPEN
    assert second == ()
    harness.publisher.publish_draft_mr.assert_awaited_once()


def test_skillopt_score_rejection_prevents_publish_and_enters_buffer():
    harness = Harness()
    async def no_improvement(batch, baseline_content, candidate_content):
        replay = SkillReplayResult(
            model="independent-model",
            decisions=tuple(
                SkillReplayDecision(item.suggestion_id, ReplayAction.EMIT, "unchanged")
                for item in batch.selection_cases
            ),
        )
        return replay, replay

    harness.evaluator.replay_pair = AsyncMock(side_effect=no_improvement)

    result = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert result.status is EvolutionRunStatus.OPTIMIZATION_REJECTED
    assert result.error_code == "skillopt_gate_rejected"
    harness.publisher.publish_draft_mr.assert_not_awaited()
    rejected = harness.store.get_rejected_edit_buffer(PROJECT, MANIFEST_HASH, 10)
    assert len(rejected) == 1
    assert "score_not_strictly_better" in rejected[0]["errors"]
    harness.high_fidelity_evaluator.evaluate_pair.assert_not_awaited()


def test_repeated_rejected_edit_is_blocked_without_another_replay():
    harness = Harness()

    async def no_improvement(batch, baseline_content, candidate_content):
        replay = SkillReplayResult(
            model="independent-model",
            decisions=tuple(
                SkillReplayDecision(item.suggestion_id, ReplayAction.EMIT, "unchanged")
                for item in batch.selection_cases
            ),
        )
        return replay, replay

    harness.evaluator.replay_pair = AsyncMock(side_effect=no_improvement)
    first = asyncio.run(harness.runner.run(dry_run=False))[0]
    assert first.status is EvolutionRunStatus.OPTIMIZATION_REJECTED

    harness.runner.now = NOW + timedelta(days=7)
    harness.evaluator.replay_pair = AsyncMock(side_effect=AssertionError("replay must be skipped"))
    second = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert second.status is EvolutionRunStatus.OPTIMIZATION_REJECTED
    assert "repeated_rejected_edit" in second.error_message
    harness.evaluator.replay_pair.assert_not_awaited()
    harness.publisher.publish_draft_mr.assert_not_awaited()


def test_insufficient_selection_evidence_stops_before_generation():
    harness = Harness(target_mrs=(1, 2))

    result = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert result.status is EvolutionRunStatus.INSUFFICIENT_VALIDATION
    assert result.error_code == "insufficient_selection_evidence"
    assert harness.store.get_source_snapshot(result.run_id)
    harness.agent.generate.assert_not_awaited()
    harness.evaluator.replay_pair.assert_not_awaited()
    harness.publisher.publish_draft_mr.assert_not_awaited()


def test_replay_unavailability_is_retryable_and_never_publishes():
    harness = Harness()
    harness.evaluator.replay_pair = AsyncMock(side_effect=TimeoutError("judge unavailable"))

    result = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert result.status is EvolutionRunStatus.FAILED_RETRYABLE
    assert result.error_code == "project_skill_replay_failed"
    harness.publisher.publish_draft_mr.assert_not_awaited()


def test_shadow_mode_records_gate_but_never_publishes():
    harness = Harness(gate_mode="shadow")

    result = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert result.status is EvolutionRunStatus.DRY_RUN_VALIDATED
    harness.evaluator.replay_pair.assert_awaited_once()
    harness.publisher.publish_draft_mr.assert_not_awaited()


def test_high_fidelity_rejection_prevents_draft_mr_and_is_persisted():
    harness = Harness()
    harness.high_fidelity_evaluator.evaluate_pair = AsyncMock(return_value=HighFidelityEvaluationReport(
        False,
        ("high_fidelity_score_not_improved",),
        ("production_path", "paired_conditions", "complete_coverage"),
        ("3",),
        (),
        "1",
        "0.5",
        "1",
        "1",
        "1",
        "0",
        (("3", "baseline", "candidate"),),
    ))

    result = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert result.status is EvolutionRunStatus.OPTIMIZATION_REJECTED
    assert result.error_code == "high_fidelity_gate_rejected"
    harness.publisher.publish_draft_mr.assert_not_awaited()
    rejected = harness.store.get_rejected_edit_buffer(PROJECT, MANIFEST_HASH, 10)
    assert "high_fidelity_score_not_improved" in rejected[0]["errors"]


def test_high_fidelity_insufficient_evidence_is_normal_no_publish_terminal():
    harness = Harness()
    harness.high_fidelity_evaluator.evaluate_pair = AsyncMock(return_value=HighFidelityEvaluationReport(
        False, ("insufficient_high_fidelity_evidence",), (), (), (),
        "0", "0", "0", "0", "0", "0", (),
    ))

    result = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert result.status is EvolutionRunStatus.INSUFFICIENT_VALIDATION
    assert result.error_code == "insufficient_high_fidelity_evidence"
    harness.publisher.publish_draft_mr.assert_not_awaited()
    assert harness.store.get_rejected_edit_buffer(PROJECT, MANIFEST_HASH, 10) == ()


def test_high_fidelity_shadow_records_report_without_publish():
    harness = Harness(high_fidelity_mode="shadow")

    result = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert result.status is EvolutionRunStatus.DRY_RUN_VALIDATED
    harness.high_fidelity_evaluator.evaluate_pair.assert_awaited_once()
    harness.publisher.publish_draft_mr.assert_not_awaited()


def test_high_fidelity_runtime_failure_is_retryable():
    harness = Harness()
    harness.high_fidelity_evaluator.evaluate_pair = AsyncMock(side_effect=TimeoutError("replay unavailable"))

    result = asyncio.run(harness.runner.run(dry_run=False))[0]

    assert result.status is EvolutionRunStatus.FAILED_RETRYABLE
    assert result.error_code == "project_skill_high_fidelity_failed"
    harness.publisher.publish_draft_mr.assert_not_awaited()
