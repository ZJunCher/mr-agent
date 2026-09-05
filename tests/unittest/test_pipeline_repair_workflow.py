import json

import pytest

from pr_agent.distributed.models import RepairCategory
from pr_agent.triage.failure_explanations import FailureExplanation
from pr_agent.triage.pipeline_repair import (
    PipelineRepairPhase,
    PipelineRepairState,
    PipelineRepairStep,
    initial_repair_step,
    next_step_after_triage,
    repair_source_failure_explanations,
)


def test_format_only_skips_triage():
    assert initial_repair_step([RepairCategory.FORMAT]) is PipelineRepairStep.FORMAT


@pytest.mark.parametrize("category", [RepairCategory.BUILD, RepairCategory.CLANG, RepairCategory.UNKNOWN])
def test_non_format_failure_starts_triage(category):
    assert initial_repair_step([category]) is PipelineRepairStep.TRIAGE


def test_empty_or_mixed_failures_start_triage():
    assert initial_repair_step([]) is PipelineRepairStep.TRIAGE
    assert initial_repair_step([RepairCategory.FORMAT, RepairCategory.BUILD]) is PipelineRepairStep.TRIAGE


def test_post_triage_format_failure_runs_formatter():
    assert next_step_after_triage([RepairCategory.FORMAT, RepairCategory.BUILD]) is PipelineRepairStep.FORMAT


def test_post_triage_non_format_failure_stops():
    assert next_step_after_triage([RepairCategory.BUILD]) is PipelineRepairStep.TERMINAL


def test_pipeline_repair_state_round_trip():
    source = FailureExplanation(
        job_name="build_release_arm64",
        job_id=105279,
        job_url="https://gitlab.example/eabot/cook/-/jobs/105279",
        trace_line=1837,
        confirmed_reason="fatal error: missing.hpp",
        confidence="confirmed",
    )
    state = PipelineRepairState(
        phase=PipelineRepairPhase.TRIAGE_WAITING,
        completed_steps=("triage_started",),
        latest_pipeline_id=30100,
        latest_pipeline_sha="abc123",
        final_pipeline_status="failed",
        final_coverage=63.04,
        final_coverage_source="changed_lines",
        final_coverage_status="reported",
        failed_job_names=("build_release_arm64",),
        terminal_error="still failing",
        iterations=12,
        max_iterations=30,
        source_failure_explanations=(source,),
        failure_explanations=(
            FailureExplanation(
                job_name="build_release_arm64",
                possible_reason="依赖声明可能缺失",
                confidence="inferred",
            ),
        ),
    )

    assert PipelineRepairState.from_json(state.to_json()) == state
    assert repair_source_failure_explanations(state) == (source,)


def test_legacy_pipeline_repair_state_uses_current_failures_as_source_fallback():
    source = FailureExplanation(
        job_name="build_release_arm64",
        confirmed_reason="fatal error: missing.hpp",
        confidence="confirmed",
    )

    restored = PipelineRepairState.from_json(json.dumps({
        "phase": "terminal",
        "failure_explanations": [source.to_dict()],
    }))

    assert restored.source_failure_explanations == ()
    assert repair_source_failure_explanations(restored) == restored.failure_explanations


def test_empty_pipeline_repair_state_is_pending():
    assert PipelineRepairState.from_json("") == PipelineRepairState()


def test_old_pipeline_repair_state_defaults_iterations_to_zero():
    restored = PipelineRepairState.from_json('{"phase":"pending"}')

    assert restored.iterations == 0
    assert restored.max_iterations == 0
