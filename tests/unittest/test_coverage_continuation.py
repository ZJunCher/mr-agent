import pytest

from pr_agent.triage.coverage_continuation import (
    decide_coverage_continuation,
    is_coverage_job,
    non_coverage_jobs,
)
from pr_agent.triage.pipeline_coverage import CoverageResult
from pr_agent.triage.pipeline_repair import PipelineRepairState


def _decision(**overrides):
    values = {
        "state": PipelineRepairState(
            selected_categories=("build",),
            coverage_baseline_sha="a" * 40,
        ),
        "failed_jobs": ({"id": 17, "name": "x86_64_ut_coverage_check"},),
        "coverage": CoverageResult(63.04, "changed_lines", "reported", 17, 80.0),
        "report_available": True,
        "uncovered_line_count": 12,
        "enabled": True,
        "max_attempts": 1,
    }
    values.update(overrides)
    return decide_coverage_continuation(**values)


def test_eligible_after_build_is_fixed_and_only_coverage_remains():
    decision = _decision()

    assert decision.eligible is True
    assert decision.code == "eligible"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"enabled": False}, "disabled"),
        ({"state": PipelineRepairState(selected_categories=("format",), coverage_baseline_sha="a" * 40)},
         "unsupported_selection"),
        ({"state": PipelineRepairState(selected_categories=("build",))}, "baseline_missing"),
        ({"failed_jobs": ({"name": "build_release_arm64"}, {"name": "x86_64_ut_coverage_check"})},
         "non_coverage_failure_remains"),
        ({"failed_jobs": ()}, "coverage_job_missing"),
        ({"report_available": False}, "coverage_report_missing"),
        ({"coverage": CoverageResult(63.04, "changed_lines", "reported", 17, None)},
         "coverage_threshold_missing"),
        ({"coverage": CoverageResult(80.0, "changed_lines", "reported", 17, 80.0)},
         "coverage_already_sufficient"),
        ({"uncovered_line_count": 0}, "uncovered_lines_empty"),
        ({"state": PipelineRepairState(selected_categories=("build",), coverage_baseline_sha="a" * 40,
                                        coverage_attempts=1)}, "attempt_limit_reached"),
    ],
)
def test_ineligible_conditions_have_stable_reason(overrides, code):
    decision = _decision(**overrides)

    assert decision.eligible is False
    assert decision.code == code
    assert decision.message


def test_coverage_job_filter_is_narrow():
    jobs = (
        {"name": "x86_64_ut_coverage_check"},
        {"name": "build_release_arm64"},
        {"name": "clang_tidy_check"},
    )

    assert is_coverage_job(jobs[0]) is True
    assert non_coverage_jobs(jobs) == jobs[1:]
