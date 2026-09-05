from pr_agent.distributed.models import RepairCategory
from pr_agent.triage.pipeline_repair import (
    CoverageContinuationPhase,
    PipelineRepairPhase,
    PipelineRepairState,
    PipelineRepairStep,
    initial_repair_step,
)
from pr_agent.triage.repair_details import RepairAction
from pr_agent.triage.repair_outcome import CategoryRepairOutcome, CategoryRepairResult


def test_pipeline_repair_state_round_trip_preserves_selection():
    state = PipelineRepairState(
        root_pipeline_id=30100,
        selected_categories=("clang", "build"),
        effective_categories=("clang", "build", "format"),
        auto_format_cleanup=True,
        final_coverage=63.04,
        final_coverage_source="changed_lines",
        final_coverage_status="reported",
        repair_actions=(
            RepairAction.from_dict({
                "action_id": "root-1",
                "root_cause": "missing dependency",
                "solution_summary": "移除未使用依赖。",
                "rationale": "源码没有使用该依赖。",
                "changed_files": ["CMakeLists.txt"],
                "file_changes": [{"path": "CMakeLists.txt", "change_type": "modified", "hunks": []}],
            }),
        ),
        source_failed_job_names=("clang_tidy_check", "build_release_arm64"),
        repair_outcome="partial_success",
        category_results=(
            CategoryRepairResult("clang", CategoryRepairOutcome.SUCCEEDED, "selected"),
            CategoryRepairResult("build", CategoryRepairOutcome.FAILED, "selected"),
        ),
        introduced_failure_categories=("format",),
        introduced_failed_job_names=("code_format_check",),
        verified_selected_success_count=1,
        auto_rollback_required=True,
        format_round=2,
        format_report_fingerprints=("report-a", "report-b"),
        format_last_exact_report_applied=True,
    )

    restored = PipelineRepairState.from_json(state.to_json())

    assert restored.root_pipeline_id == 30100
    assert restored.selected_categories == ("clang", "build")
    assert restored.effective_categories == ("clang", "build", "format")
    assert restored.auto_format_cleanup is True
    assert restored.final_coverage == 63.04
    assert restored.final_coverage_source == "changed_lines"
    assert restored.final_coverage_status == "reported"
    assert restored.repair_actions[0].action_id == "root-1"
    assert restored.repair_actions[0].changed_files == ("CMakeLists.txt",)
    assert restored.repair_actions[0].solution_summary == "移除未使用依赖。"
    assert restored.repair_actions[0].file_changes[0].path == "CMakeLists.txt"
    assert restored.source_failed_job_names == ("clang_tidy_check", "build_release_arm64")
    assert restored.repair_outcome == "partial_success"
    assert restored.category_results[0].outcome is CategoryRepairOutcome.SUCCEEDED
    assert restored.category_results[1].outcome is CategoryRepairOutcome.FAILED
    assert restored.introduced_failure_categories == ("format",)
    assert restored.introduced_failed_job_names == ("code_format_check",)
    assert restored.verified_selected_success_count == 1
    assert restored.auto_rollback_required is True
    assert restored.format_round == 2
    assert restored.format_report_fingerprints == ("report-a", "report-b")
    assert restored.format_last_exact_report_applied is True


def test_pipeline_repair_state_reads_legacy_coverage_fields():
    restored = PipelineRepairState.from_json('{"final_coverage":63.04}')

    assert restored.final_coverage == 63.04
    assert restored.final_coverage_source == ""
    assert restored.final_coverage_status == ""
    assert restored.verified_selected_success_count == 0
    assert restored.auto_rollback_required is False
    assert restored.coverage_phase is CoverageContinuationPhase.NOT_STARTED
    assert restored.coverage_attempts == 0
    assert restored.terminal_attempt_id == ""
    assert restored.terminal_proof_sha == ""
    assert restored.terminal_proof_pipeline_id == 0
    assert restored.terminal_proof_status == ""


def test_pipeline_repair_state_round_trips_terminal_proof():
    state = PipelineRepairState(
        terminal_attempt_id="attempt-3",
        terminal_proof_sha="f" * 40,
        terminal_proof_pipeline_id=34713,
        terminal_proof_status="failed",
    )

    assert PipelineRepairState.from_json(state.to_json()) == state


def test_terminal_failure_kind_round_trips_and_old_payload_defaults_empty():
    state = PipelineRepairState(terminal_failure_kind="provider_unavailable")

    assert PipelineRepairState.from_json(state.to_json()) == state
    assert PipelineRepairState.from_json('{"phase":"terminal"}').terminal_failure_kind == ""


def test_terminal_validation_details_round_trip_and_old_payload_defaults_empty():
    state = PipelineRepairState(
        terminal_validation_error_code="diagnostic_identity_mismatch",
        terminal_validation_summary="缺少 16 条诊断身份，存在 16 条未知身份。",
        normalized_diagnostic_alias_count=0,
    )

    assert PipelineRepairState.from_json(state.to_json()) == state
    legacy = PipelineRepairState.from_json('{"phase":"terminal"}')
    assert legacy.terminal_validation_error_code == ""
    assert legacy.terminal_validation_summary == ""
    assert legacy.normalized_diagnostic_alias_count == 0


def test_pipeline_repair_state_round_trip_preserves_coverage_continuation():
    state = PipelineRepairState(
        phase=PipelineRepairPhase.COVERAGE_WAITING,
        coverage_phase=CoverageContinuationPhase.WAITING,
        coverage_attempts=1,
        coverage_baseline_pipeline_id=30101,
        coverage_baseline_sha="a" * 40,
        coverage_enhancement_sha="b" * 40,
        coverage_rollback_sha="c" * 40,
        coverage_before=63.04,
        coverage_after=82.5,
        coverage_threshold=80.0,
        coverage_job_id=107440,
        coverage_result="succeeded",
    )

    assert PipelineRepairState.from_json(state.to_json()) == state


def test_pipeline_repair_state_round_trip_preserves_dependency_blocker():
    state = PipelineRepairState(
        repair_outcome="blocked",
        blocker_type="external_dependency",
        blocker_summary="当前声明分支缺少接口。",
        blocker_suggested_action="请维护者确认候选分支。",
        blocked_job_names=("build_release_arm64",),
        dependency_evidence=({"project_path": "eabot/lhotse", "declared_branch": "dev"},),
    )

    assert PipelineRepairState.from_json(state.to_json()) == state


def test_pipeline_repair_state_legacy_json_defaults_blocker_fields_and_ignores_non_dict_evidence():
    restored = PipelineRepairState.from_json(
        '{"repair_outcome":"blocked","dependency_evidence":[null,"raw",{"project_path":"eabot/lhotse"}]}'
    )

    assert restored.blocker_type == ""
    assert restored.blocker_summary == ""
    assert restored.blocker_suggested_action == ""
    assert restored.blocked_job_names == ()
    assert restored.dependency_evidence == ({"project_path": "eabot/lhotse"},)


def test_selected_format_only_starts_formatter():
    assert initial_repair_step((RepairCategory.FORMAT,)) is PipelineRepairStep.FORMAT
