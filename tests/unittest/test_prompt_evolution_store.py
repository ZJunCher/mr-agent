from dataclasses import replace
from pathlib import Path

import pytest

from pr_agent.suggestions.prompt_evolution.models import (
    CandidateScope,
    Evidence,
    EligibleCandidate,
    EvolutionRunStatus,
    HighFidelityEvaluationReport,
    Outcome,
    PromptChangeKind,
    PromptEvaluationBatch,
    PromptFileChange,
    PromptProposal,
    SkillOptimizationBatch,
    SkillOptimizationReport,
    ValidationReport,
    WeightedCluster,
)
from pr_agent.suggestions.prompt_evolution.prompt_surface import (
    GLOBAL_PROMPT_PATHS,
    PROJECT_RULE_PREFIX,
    is_allowed_prompt_path,
    project_from_rule_repo_path,
    project_rule_repo_path,
)
from pr_agent.suggestions.prompt_evolution.store import PromptEvolutionStore


# --------------------------------------------------------------------------- #
# Step 1: schema and idempotency
# --------------------------------------------------------------------------- #
def test_store_creates_run_candidate_and_evidence_tables(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    store.migrate()
    assert {
        "prompt_evolution_runs",
        "prompt_evolution_candidates",
        "prompt_evolution_evidence",
        "prompt_evolution_source_evidence",
        "project_skill_optimization_steps",
        "global_prompt_evaluation_batches",
    }.issubset(store.table_names())


def test_start_run_is_idempotent_by_batch_id(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    first = store.start_run("2026-w34-a1b2", "group/pr-agent", "main", "base", "deployed", "target", 7)
    second = store.start_run("2026-w34-a1b2", "group/pr-agent", "main", "base", "deployed", "target", 7)
    assert first.run_id == second.run_id
    assert second.status is EvolutionRunStatus.CREATED


# --------------------------------------------------------------------------- #
# Step 4: prompt surface whitelist
# --------------------------------------------------------------------------- #
def test_whitelist_contains_exactly_14_foundation_prompt_files():
    assert len(GLOBAL_PROMPT_PATHS) == 14


def test_project_rule_repo_path_for_eabot_cook():
    assert project_rule_repo_path("eabot/cook") == f"{PROJECT_RULE_PREFIX}skill.toml"


def test_project_rule_repo_path_for_single_segment_project():
    assert project_rule_repo_path("cook") == f"{PROJECT_RULE_PREFIX}skill.toml"


def test_rejects_absolute_paths_and_traversal():
    assert not is_allowed_prompt_path("/etc/passwd")
    assert not is_allowed_prompt_path("pr_agent/settings/../secrets.toml")
    assert not is_allowed_prompt_path("pr_agent/settings//code_suggestions/pr_code_suggestions_prompts.toml")


def test_rejects_empty_segments_and_yaml():
    assert not is_allowed_prompt_path("")
    assert not is_allowed_prompt_path("pr_agent/settings/code_suggestions/pr_code_suggestions_prompts.yaml")


def test_rejects_static_agent_prompt_python_config_and_workflow_files():
    assert not is_allowed_prompt_path("pr_agent/settings/prompt_evolution_prompts.toml")
    assert not is_allowed_prompt_path("pr_agent/settings/configuration.toml")
    assert not is_allowed_prompt_path(".github/workflows/build-and-test.yml")


def test_rejects_non_canonical_project_rule_path():
    assert not is_allowed_prompt_path(f"{PROJECT_RULE_PREFIX}eabot/../cook.toml")
    assert not is_allowed_prompt_path(f"{PROJECT_RULE_PREFIX}eabot/cook.yaml")


def test_project_from_rule_repo_path_round_trip():
    with pytest.raises(ValueError, match="candidate"):
        project_from_rule_repo_path(project_rule_repo_path("eabot/cook"))


# --------------------------------------------------------------------------- #
# Step 5: store behavior
# --------------------------------------------------------------------------- #
def _evidence(suggestion_id="S1", project="eabot/cook", mr_iid="10", **kw):
    base = dict(
        suggestion_id=suggestion_id,
        project=project,
        mr_iid=mr_iid,
        mr_url="https://gl/eabot/cook/-/merge_requests/10",
        created_at="2026-08-01T00:00:00+08:00",
        file_path="src/a.cpp",
        label="correctness",
        summary="fix off-by-one",
        suggestion_content="why... fix...",
        outcome=Outcome.ACCEPTED,
        weight=1.0,
        global_prompt_set_hash="global-1",
        prompt_bundle_hash="bundle-1",
        feedback=("applied",),
    )
    base.update(kw)
    return Evidence(**base)


def _candidate(candidate_id="c1", scope=CandidateScope.PROJECT, project="eabot/cook",
               source_prompt_hash="bundle-1", cluster_key="ck1"):
    cluster = WeightedCluster(
        cluster_key=cluster_key,
        evidence=(_evidence(),),
        positive_weight=1.0,
        negative_weight=0.0,
        negative_ratio=0.0,
    )
    return EligibleCandidate(
        candidate_id=candidate_id,
        scope=scope,
        project=project,
        source_prompt_hash=source_prompt_hash,
        cluster=cluster,
    )


def test_update_run_rejects_unknown_field(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-b1", "group/pr-agent", "main", "base", "g", "t", 1)
    with pytest.raises(ValueError, match="unsupported"):
        store.update_run(run.run_id, EvolutionRunStatus.AGGREGATING, bogus_field="nope")


def test_update_run_persists_allowed_fields(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-c1", "group/pr-agent", "main", "base", "g", "t", 1)
    store.update_run(run.run_id, EvolutionRunStatus.MR_OPEN,
                     branch_name="br", commit_sha="sha", mr_iid="!1", mr_url="https://gl/!1")
    rehydrated = store.get_run_by_batch("2026-w34-c1")
    assert rehydrated.status is EvolutionRunStatus.MR_OPEN
    assert rehydrated.branch_name == "br"
    assert rehydrated.commit_sha == "sha"
    assert rehydrated.mr_iid == "!1"
    assert rehydrated.mr_url == "https://gl/!1"


def test_non_failure_status_clears_stale_errors_but_keeps_explicit_reason(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-c2", "group/pr-agent", "main", "base", "g", "t", 1)
    store.update_run(
        run.run_id,
        EvolutionRunStatus.FAILED_RETRYABLE,
        error_code="clustering_failed",
        error_message="old failure",
    )

    store.update_run(run.run_id, EvolutionRunStatus.AGGREGATING)
    rehydrated = store.get_run_by_batch("2026-w34-c2")
    assert rehydrated.error_code == ""
    assert rehydrated.error_message == ""

    store.update_run(
        run.run_id,
        EvolutionRunStatus.COMPLETED_NO_CHANGE,
        error_code="project_prompt_version_mismatch",
        error_message="suppressed candidate",
    )
    rehydrated = store.get_run_by_batch("2026-w34-c2")
    assert rehydrated.error_code == "project_prompt_version_mismatch"
    assert rehydrated.error_message == "suppressed candidate"


def test_source_snapshot_round_trip(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-d1", "group/pr-agent", "main", "base", "g", "t", 1)
    evidence = (
        _evidence(
            "S1",
            existing_code="old()",
            improved_code="new()",
            commit_sha="a" * 40,
            line_start=10,
            line_end=12,
            case_kind="false_negative",
            expected_action="emit",
            review_id="review-1",
            replayable=True,
        ),
        _evidence("S2", outcome=Outcome.REJECTED, weight=1.0),
    )
    store.save_source_snapshot(run.run_id, evidence)
    rehydrated = store.get_source_snapshot(run.run_id)
    assert tuple(e.suggestion_id for e in rehydrated) == ("S1", "S2")
    assert rehydrated[1].outcome is Outcome.REJECTED
    assert rehydrated[0] == evidence[0]


def test_global_prompt_evaluation_audit_round_trip(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-global-eval", "group/pr-agent", "main", "base", "g", "t", 1)
    batch = PromptEvaluationBatch(
        base_prompt_hash="global-1",
        training_candidates=(_candidate(),),
        selection_cases=(_evidence("selection", mr_iid="20"),),
        control_ids=("selection",),
        split_hash="split-global",
    )
    report = HighFidelityEvaluationReport(
        True,
        (),
        ("production_path", "complete_coverage"),
        ("eabot/cook!20",),
        (),
        "0.5",
        "1",
        "1",
        "1",
        "0",
        "1",
        (("eabot/cook!20", "baseline", "candidate"),),
    )

    store.save_prompt_evaluation_batch(run.run_id, batch)
    store.save_prompt_behavioral_report(run.run_id, report)

    audit = store.get_prompt_evaluation_audit(run.run_id)
    assert audit is not None
    assert audit["training_ids"] == ("S1",)
    assert audit["selection_ids"] == ("selection",)
    assert audit["split_hash"] == "split-global"
    assert audit["report"]["passed"] is True
    assert audit["report"]["condition_hashes"] == [["eabot/cook!20", "baseline", "candidate"]]


def test_empty_source_snapshot_writes_manifest_and_reads_back_empty(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-e1", "group/pr-agent", "main", "base", "g", "t", 1)
    store.save_source_snapshot(run.run_id, ())
    assert store.get_source_snapshot(run.run_id) == ()


def test_candidate_and_evidence_snapshot_round_trip(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-f1", "group/pr-agent", "main", "base", "g", "t", 1)
    candidate = _candidate()
    stored_id = store.save_candidate(run.run_id, candidate, fingerprint="fp1")
    store.save_evidence_snapshot(stored_id, candidate.cluster.evidence)
    rehydrated = store.get_candidates_for_run(run.run_id)
    assert len(rehydrated) == 1
    assert rehydrated[0].candidate_id == "c1"
    assert rehydrated[0].cluster.evidence[0].suggestion_id == "S1"


def test_save_candidate_is_idempotent(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-g1", "group/pr-agent", "main", "base", "g", "t", 1)
    candidate = _candidate()
    first = store.save_candidate(run.run_id, candidate, fingerprint="fp1")
    second = store.save_candidate(run.run_id, candidate, fingerprint="fp1")
    assert first == second
    assert len(store.get_candidates_for_run(run.run_id)) == 1


def test_proposal_and_validation_round_trip(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-h1", "group/pr-agent", "main", "base", "g", "t", 1)
    candidate = _candidate()
    store.save_candidate(run.run_id, candidate, fingerprint="fp1")
    proposal = PromptProposal(
        rationale="tighten trigger",
        change_kind=PromptChangeKind.CONSERVATIVE_TIGHTENING,
        evidence_ids=("S1",),
        changes=(PromptFileChange(
            path="pr_agent/settings/code_suggestions/pr_code_suggestions_prompts.toml",
            family="generation",
            expected_base_sha256="abc",
            content="new content",
            evidence_ids=("S1",),
        ),),
    )
    store.save_proposal(run.run_id, proposal)
    rehydrated_proposal = store.get_proposal(run.run_id)
    assert rehydrated_proposal is not None
    assert rehydrated_proposal.rationale == "tighten trigger"
    assert rehydrated_proposal.changes[0].path.endswith("pr_code_suggestions_prompts.toml")

    report = ValidationReport(passed=True, checks=("path_whitelist", "toml_valid"))
    store.save_validation(run.run_id, report)
    rehydrated_report = store.get_validation(run.run_id)
    assert rehydrated_report is not None
    assert rehydrated_report.passed is True
    assert "path_whitelist" in rehydrated_report.checks


def _optimization_batch():
    return SkillOptimizationBatch(
        project="eabot/cook",
        base_manifest_hash="manifest-1",
        training_candidates=(_candidate(),),
        selection_cases=(
            _evidence("accepted", mr_iid="11"),
            _evidence("rejected", mr_iid="12", outcome=Outcome.REJECTED),
        ),
        control_ids=("accepted",),
        split_hash="split-1",
    )


def _optimization_report(signature="edit-1", action="reject"):
    return SkillOptimizationReport(
        passed=action == "accept_new_best",
        action=action,
        errors=() if action == "accept_new_best" else ("score_not_strictly_better",),
        checks=("selection_isolation", "same_model_replay"),
        split_hash="split-1",
        replay_model="judge",
        baseline_score="0.5",
        candidate_score="0.5" if action == "reject" else "1",
        baseline_accepted_score="1",
        candidate_accepted_score="1",
        baseline_rejected_score="0",
        candidate_rejected_score="0" if action == "reject" else "1",
        accepted_control_regressions=(),
        rejected_target_regressions=(),
        edit_budget=1,
        edit_count=1,
        edit_signature=signature,
    )


def test_optimization_step_round_trip_and_rejected_buffer_is_bounded(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-opt", "eabot/cook", "main", "base", "g", "t", 1)
    batch = _optimization_batch()
    first_report = _optimization_report()
    high_fidelity = HighFidelityEvaluationReport(
        True, (), ("production_path",), ("12",), (),
        "0.5", "1", "1", "1", "0", "1", (("12", "base", "candidate"),),
    )
    first_step = store.save_optimization_step(
        run.run_id,
        "eabot/cook",
        "manifest-1",
        "candidate-1",
        batch,
        first_report,
        "proposal-1",
        high_fidelity_report=high_fidelity,
        execution_mode="enforce",
    )

    stored = store.get_optimization_step(first_step)
    assert stored is not None
    assert stored["training_ids"] == ("S1",)
    assert stored["selection_ids"] == ("accepted", "rejected")
    assert stored["control_ids"] == ("accepted",)
    assert stored["report"] == first_report
    assert stored["high_fidelity"]["passed"] is True
    assert stored["high_fidelity"]["condition_hashes"] == [["12", "base", "candidate"]]
    assert stored["execution_mode"] == "enforce"

    for index in range(2, 14):
        report = replace(first_report, edit_signature=f"edit-{index}")
        store.save_optimization_step(
            run.run_id,
            "eabot/cook",
            "manifest-1",
            f"candidate-{index}",
            replace(batch, split_hash=f"split-{index}"),
            replace(report, split_hash=f"split-{index}"),
            f"proposal-{index}",
        )

    rejected = store.get_rejected_edit_buffer("eabot/cook", "manifest-1", 10)
    assert len(rejected) == 10
    assert rejected[0]["edit_signature"] == "edit-13"
    assert rejected[-1]["edit_signature"] == "edit-4"
    assert store.get_rejected_edit_buffer("other/project", "manifest-1", 10) == ()
    assert store.get_rejected_edit_buffer("eabot/cook", "other-manifest", 10) == ()


def test_watermark_round_trip(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    assert store.get_watermark() is None
    store.set_watermark("2026-08-01T00:00:00+08:00")
    assert store.get_watermark() == "2026-08-01T00:00:00+08:00"


def test_candidate_is_suppressed_for_open_and_merged(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-i1", "group/pr-agent", "main", "base", "g", "t", 1)
    candidate = _candidate(source_prompt_hash="bundle-1")
    store.save_candidate(run.run_id, candidate, fingerprint="fp1")
    store.update_run(run.run_id, EvolutionRunStatus.MR_OPEN, mr_iid="!1")
    # same source hash, open MR -> suppressed
    assert store.candidate_is_suppressed("fp1", "bundle-1", 0.0, "2026-08-10T00:00:00+08:00", 30)
    # different source hash -> allowed
    assert not store.candidate_is_suppressed("fp1", "bundle-other", 0.0, "2026-08-10T00:00:00+08:00", 30)

    # mark merged -> still suppressed for same source hash
    store.mark_mr_state("!1", "merged", "2026-08-05T00:00:00+08:00")
    assert store.candidate_is_suppressed("fp1", "bundle-1", 0.0, "2026-08-10T00:00:00+08:00", 30)


def test_closed_candidate_suppressed_inside_cooldown_but_allowed_when_negative_weight_grows(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    run = store.start_run("2026-w34-j1", "group/pr-agent", "main", "base", "g", "t", 1)
    candidate = _candidate(source_prompt_hash="bundle-1")
    # Simulate a prior closed run with negative_weight=3.0 captured at close time.
    candidate = EligibleCandidate(
        candidate_id=candidate.candidate_id,
        scope=candidate.scope,
        project=candidate.project,
        source_prompt_hash=candidate.source_prompt_hash,
        cluster=WeightedCluster(
            cluster_key=candidate.cluster.cluster_key,
            evidence=candidate.cluster.evidence,
            positive_weight=candidate.cluster.positive_weight,
            negative_weight=3.0,
            negative_ratio=candidate.cluster.negative_ratio,
        ),
    )
    store.save_candidate(run.run_id, candidate, fingerprint="fp2")
    store.update_run(run.run_id, EvolutionRunStatus.MR_OPEN, mr_iid="!2")
    store.mark_mr_state("!2", "closed", "2026-08-01T00:00:00+08:00")
    # 5 days after close, inside 30-day cooldown, same negative weight -> suppressed
    assert store.candidate_is_suppressed("fp2", "bundle-1", 3.0, "2026-08-06T00:00:00+08:00", 30)
    # negative weight grew from 3.0 to 4.0 (>= 1.0 growth) -> allowed early
    assert not store.candidate_is_suppressed("fp2", "bundle-1", 4.0, "2026-08-06T00:00:00+08:00", 30)
    # after cooldown expires -> allowed
    assert not store.candidate_is_suppressed("fp2", "bundle-1", 3.0, "2026-09-15T00:00:00+08:00", 30)


def test_list_reconcilable_mrs_returns_only_open_with_iid(tmp_path: Path):
    store = PromptEvolutionStore(str(tmp_path / "evolution.db"))
    open_run = store.start_run("2026-w34-k1", "group/pr-agent", "main", "base", "g", "t", 1)
    store.update_run(open_run.run_id, EvolutionRunStatus.MR_OPEN, mr_iid="!10")
    closed_run = store.start_run("2026-w34-k2", "group/pr-agent", "main", "base", "g", "t", 1)
    store.update_run(closed_run.run_id, EvolutionRunStatus.CLOSED, mr_iid="!11")
    no_iid_run = store.start_run("2026-w34-k3", "group/pr-agent", "main", "base", "g", "t", 1)
    store.update_run(no_iid_run.run_id, EvolutionRunStatus.MR_OPEN)
    reconcilable = store.list_reconcilable_mrs()
    mr_iids = sorted(r.mr_iid for r in reconcilable)
    assert mr_iids == ["!10"]
