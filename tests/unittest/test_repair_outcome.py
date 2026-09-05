from pr_agent.triage.repair_outcome import (
    CategoryRepairOutcome,
    CategoryRepairResult,
    RepairOutcome,
    evaluate_repair_outcome,
    verified_selected_success_count,
)


def _jobs(*names):
    return [{"name": name} for name in names]


def test_selected_format_succeeds_while_unselected_build_remains():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("code_format_check", "build_release_arm64"),
        validation_failed_jobs=_jobs("build_release_arm64"),
        selected_categories=("format",),
    )

    assert verdict.outcome is RepairOutcome.SUCCESS
    assert {item.category: item.outcome for item in verdict.category_results} == {
        "format": CategoryRepairOutcome.SUCCEEDED,
        "build": CategoryRepairOutcome.NOT_SELECTED,
    }


def test_two_selected_categories_can_partially_succeed():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("code_format_check", "clang_tidy_check"),
        validation_failed_jobs=_jobs("clang_tidy_check"),
        selected_categories=("format", "clang"),
    )

    assert verdict.outcome is RepairOutcome.PARTIAL_SUCCESS


def test_all_selected_categories_failed_is_failure():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("code_format_check",),
        validation_failed_jobs=_jobs("code_format_check"),
        selected_categories=("format",),
    )

    assert verdict.outcome is RepairOutcome.FAILED


def test_every_build_job_must_disappear_for_build_to_succeed():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("build_release_arm64", "x86_64_ut_coverage_check"),
        validation_failed_jobs=_jobs("x86_64_ut_coverage_check"),
        selected_categories=("build",),
    )

    assert verdict.outcome is RepairOutcome.PARTIAL_SUCCESS
    assert verified_selected_success_count(verdict.category_results) == 1


def test_new_actionable_failure_invalidates_success():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("build_release_arm64",),
        validation_failed_jobs=_jobs("code_format_check"),
        selected_categories=("build",),
    )

    assert verdict.outcome is RepairOutcome.FAILED
    assert verdict.introduced_failure_categories == ("format",)
    assert verdict.introduced_failed_job_names == ("code_format_check",)


def test_unreliable_validation_is_unverified_failure():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("clang_tidy_check",),
        validation_failed_jobs=(),
        selected_categories=("clang",),
        validation_reliable=False,
    )

    assert verdict.outcome is RepairOutcome.FAILED
    assert verdict.category_results[0].outcome is CategoryRepairOutcome.UNVERIFIED


def test_verified_selected_success_count_ignores_cleanup_and_unverified():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("code_format_check", "clang_tidy_check"),
        validation_failed_jobs=_jobs("clang_tidy_check"),
        selected_categories=("clang",),
        effective_categories=("format", "clang"),
    )

    assert verified_selected_success_count(verdict.category_results) == 0


def test_verified_selected_success_count_keeps_partial_benefit():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("code_format_check", "clang_tidy_check"),
        validation_failed_jobs=_jobs("clang_tidy_check", "new_build_job"),
        selected_categories=("format", "clang"),
    )

    assert verified_selected_success_count(verdict.category_results) == 1


def test_all_selected_failures_are_external_blockers():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("build_release_arm64",),
        validation_failed_jobs=_jobs("build_release_arm64"),
        selected_categories=("build",),
        blocked_job_names=("build_release_arm64",),
    )

    assert verdict.outcome is RepairOutcome.BLOCKED
    assert verdict.category_results[0].outcome is CategoryRepairOutcome.BLOCKED
    assert verified_selected_success_count(verdict.category_results) == 0


def test_success_plus_blocked_is_partial_success():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("code_format_check", "build_release_arm64"),
        validation_failed_jobs=_jobs("build_release_arm64"),
        selected_categories=("format", "build"),
        blocked_job_names=("build_release_arm64",),
    )

    assert verdict.outcome is RepairOutcome.PARTIAL_SUCCESS
    assert {item.category: item.outcome for item in verdict.category_results} == {
        "format": CategoryRepairOutcome.SUCCEEDED,
        "build": CategoryRepairOutcome.BLOCKED,
    }


def test_failed_plus_blocked_without_success_is_failure():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("clang_tidy_check", "build_release_arm64"),
        validation_failed_jobs=_jobs("clang_tidy_check", "build_release_arm64"),
        selected_categories=("clang", "build"),
        blocked_job_names=("build_release_arm64",),
    )

    assert verdict.outcome is RepairOutcome.FAILED


def test_same_category_with_blocked_and_nonblocked_failure_is_failure():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("build_release_arm64", "x86_64_ut_coverage_check"),
        validation_failed_jobs=_jobs("build_release_arm64", "x86_64_ut_coverage_check"),
        selected_categories=("build",),
        blocked_job_names=("build_release_arm64",),
    )

    assert verdict.outcome is RepairOutcome.FAILED
    assert verdict.category_results[0].outcome is CategoryRepairOutcome.FAILED


def test_same_category_with_repaired_job_and_only_blocker_remaining_is_partial_success():
    verdict = evaluate_repair_outcome(
        source_failed_job_names=("build_release_arm64", "x86_64_ut_coverage_check"),
        validation_failed_jobs=_jobs("build_release_arm64"),
        selected_categories=("build",),
        blocked_job_names=("build_release_arm64",),
    )

    assert verdict.outcome is RepairOutcome.PARTIAL_SUCCESS
    assert verdict.category_results[0].outcome is CategoryRepairOutcome.BLOCKED
    assert verdict.category_results[0].verified_repaired_job_names == ("x86_64_ut_coverage_check",)
    assert verified_selected_success_count(verdict.category_results) == 1


def test_verified_repaired_job_names_round_trip_and_legacy_records_stay_compatible():
    result = CategoryRepairResult(
        category="build",
        outcome=CategoryRepairOutcome.BLOCKED,
        selection="selected",
        source_failed_job_names=("build_release_arm64", "x86_64_ut_coverage_check"),
        validation_failed_job_names=("x86_64_ut_coverage_check",),
        verified_repaired_job_names=("build_release_arm64",),
    )

    assert CategoryRepairResult.from_dict(result.to_dict()) == result
    assert CategoryRepairResult.from_dict({
        "category": "build",
        "outcome": "blocked",
        "selection": "selected",
    }).verified_repaired_job_names == ()
