from pr_agent.distributed.models import RepairCategory, RepairItemStatus
from pr_agent.triage.failure_categories import (
    bind_auto_format_cleanup,
    classify_failed_jobs,
    group_failed_jobs,
    pipeline_repair_item,
    reconcile_batch_repair_items,
    reconcile_repair_items,
    repair_items_for_categories,
    repair_items_for_failed_jobs,
)
from pr_agent.triage.failure_explanations import FailureExplanation


def test_classify_failed_jobs_keeps_stable_category_order():
    failed = [
        {"name": "build_release_arm64"},
        {"name": "code_format_check"},
        {"name": "clang_tidy_check"},
    ]

    assert classify_failed_jobs(failed) == [
        RepairCategory.FORMAT,
        RepairCategory.CLANG,
        RepairCategory.BUILD,
    ]


def test_unknown_category_is_used_when_no_known_category_matches():
    assert classify_failed_jobs([{"name": "mr_merge_commit_check"}]) == [RepairCategory.UNKNOWN]


def test_group_failed_jobs_keeps_unknown_beside_build():
    grouped = group_failed_jobs([{"name": "build_release_arm64"}, {"name": "mr_merge_commit_check"}])

    assert [job["name"] for job in grouped[RepairCategory.BUILD]] == ["build_release_arm64"]
    assert [job["name"] for job in grouped[RepairCategory.UNKNOWN]] == ["mr_merge_commit_check"]
    assert classify_failed_jobs([{"name": "build_release_arm64"}, {"name": "mr_merge_commit_check"}]) == [
        RepairCategory.BUILD,
        RepairCategory.UNKNOWN,
    ]


def test_repair_items_for_failed_jobs_preserves_job_names():
    items = repair_items_for_failed_jobs(
        [{"name": "build_release_arm64"}, {"name": "x86_64_ut_coverage_check"}],
        30100,
        "abc123",
    )

    assert len(items) == 1
    assert items[0].category is RepairCategory.BUILD
    assert items[0].failed_job_names == ("build_release_arm64", "x86_64_ut_coverage_check")


def test_non_repairable_format_job_does_not_create_action():
    items = repair_items_for_failed_jobs(
        [{
            "name": "code_format_check",
            "auto_repair_eligible": False,
            "format_job_disposition": {
                "kind": "ci_job_configuration",
                "summary": "Format Job 自身执行失败",
            },
        }],
        30100,
        "abc123",
    )

    assert items == ()


def test_non_repairable_format_job_does_not_hide_build_action():
    items = repair_items_for_failed_jobs(
        [
            {"name": "code_format_check", "auto_repair_eligible": False},
            {"name": "build_release_arm64"},
        ],
        30100,
        "abc123",
    )

    assert [item.category for item in items] == [RepairCategory.BUILD]
    assert items[0].failed_job_names == ("build_release_arm64",)


def test_pipeline_repair_item_uses_unified_command():
    item = pipeline_repair_item(30100, "abc123")

    assert item.category is RepairCategory.PIPELINE
    assert item.command == "/repair-pipeline"
    assert item.label == "修复流水线"
    assert item.pipeline_id == 30100
    assert item.pipeline_sha == "abc123"


def test_reconcile_format_success_reopens_build():
    initial = repair_items_for_categories(
        [RepairCategory.FORMAT, RepairCategory.BUILD],
        30041,
        "old-sha",
    )

    items = reconcile_repair_items(
        initial,
        RepairCategory.FORMAT,
        [RepairCategory.BUILD],
        30100,
        "new-sha",
    )

    assert items[0].status is RepairItemStatus.SUCCEEDED
    assert items[1].status is RepairItemStatus.PENDING
    assert items[1].result_pipeline_id == 30100


def test_batch_reconcile_reopens_unselected_remaining_build():
    previous = repair_items_for_failed_jobs(
        [{"name": "clang_tidy_check"}, {"name": "build_release_arm64"}],
        30100,
        "old-sha",
    )

    items = reconcile_batch_repair_items(
        previous,
        selected_categories=("clang",),
        effective_categories=("clang",),
        failed_jobs=[{"name": "build_release_arm64"}],
        pipeline_id=30101,
        pipeline_sha="new-sha",
    )

    assert items[0].category is RepairCategory.CLANG
    assert items[0].status is RepairItemStatus.SUCCEEDED
    assert items[1].category is RepairCategory.BUILD
    assert items[1].status is RepairItemStatus.PENDING
    assert items[1].failed_job_names == ("build_release_arm64",)


def test_batch_reconcile_attaches_only_current_job_explanations():
    previous = repair_items_for_failed_jobs(
        [{"name": "clang_tidy_check"}, {"name": "build_release_arm64"}],
        30100,
        "old-sha",
    )
    explanations = (
        FailureExplanation(job_name="build_release_arm64", confirmed_reason="compile error", confidence="confirmed"),
        FailureExplanation(job_name="resolved_job", confirmed_reason="old error", confidence="confirmed"),
    )

    items = reconcile_batch_repair_items(
        previous,
        selected_categories=("clang", "build"),
        effective_categories=("clang", "build"),
        failed_jobs=[{"name": "build_release_arm64"}],
        pipeline_id=30101,
        pipeline_sha="new-sha",
        failure_explanations=explanations,
    )

    assert items[0].category is RepairCategory.CLANG
    assert items[0].failure_explanations == ()
    assert items[1].category is RepairCategory.BUILD
    assert [record.job_name for record in items[1].failure_explanations] == ["build_release_arm64"]


def test_auto_format_cleanup_binds_new_format_item_to_batch_task():
    previous = repair_items_for_failed_jobs([{"name": "build_release_arm64"}], 30100, "old-sha")

    items = bind_auto_format_cleanup(
        previous,
        task_id="task-1",
        failed_jobs=[{"name": "code_format_check"}],
        pipeline_id=30101,
        pipeline_sha="triage-sha",
    )

    assert items[0].category is RepairCategory.FORMAT
    assert items[0].task_id == "task-1"
    assert items[0].status is RepairItemStatus.RUNNING
    assert items[0].status_markdown == "检测到格式问题，正在自动修复"
    assert items[1].category is RepairCategory.BUILD
