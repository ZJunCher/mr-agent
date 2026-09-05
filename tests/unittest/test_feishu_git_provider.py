from types import SimpleNamespace
from unittest.mock import Mock

from pr_agent.distributed.models import TriageCardState
from pr_agent.feishu.feishu_git_provider import FeishuGitProvider

MR_URL = "https://gitlab.example.com/eabot/cook/-/merge_requests/538"


def _provider(sink=None, *, correlate_triage=False):
    original = Mock()
    original.id_project = "eabot/cook"
    original.pr = SimpleNamespace(iid=538)
    return FeishuGitProvider(
        original,
        "ou_1",
        mr_url=MR_URL,
        notification_sink=sink or Mock(),
        task_id="task-538",
        correlate_triage=correlate_triage,
    )


def test_all_feishu_results_prefix_project_and_mr():
    sink = Mock()
    provider = _provider(sink)

    provider.publish_comment("FINISHED: success=True")

    published = sink.publish_markdown.call_args.kwargs
    assert published["title"].startswith("【eabot/cook !538】")
    assert MR_URL in published["content"]


def test_temporary_triage_comment_updates_running_card():
    sink = Mock()
    sink.publish_card_update.return_value = True
    provider = _provider(sink, correlate_triage=True)

    provider.publish_comment("正在诊断", is_temporary=True)

    sink.publish_card_update.assert_called_once_with(
        state=TriageCardState.REPAIR_RUNNING,
        status_markdown="正在诊断",
    )
    sink.publish_markdown.assert_not_called()


def test_temporary_triage_comment_never_falls_back_to_standalone_message():
    sink = Mock()
    sink.publish_card_update.return_value = False
    provider = _provider(sink, correlate_triage=True)

    provider.publish_comment("正在诊断", is_temporary=True)

    sink.publish_card_update.assert_called_once_with(
        state=TriageCardState.REPAIR_RUNNING,
        status_markdown="正在诊断",
    )
    sink.publish_markdown.assert_not_called()


def test_triage_result_publishes_structured_terminal_card_with_coverage():
    sink = Mock()
    sink.publish_triage_result.return_value = True
    provider = _provider(sink, correlate_triage=True)

    provider.publish_triage_result(
        "修复完成",
        success=True,
        details={
            "pushed_sha": "abc123",
            "final_pipeline_status": "success",
            "final_coverage": 63.04,
            "coverage_source": "changed_lines",
            "coverage_status": "reported",
        },
    )

    update = sink.publish_triage_result.call_args.kwargs
    assert update["state"] is TriageCardState.REPAIR_SUCCEEDED
    assert "变更行覆盖率：63.04%" in update["status_markdown"]
    assert "abc123" in update["status_markdown"]
    assert "success" in update["status_markdown"]
    sink.publish_markdown.assert_not_called()


def test_unbound_triage_result_sends_identified_standalone_card():
    sink = Mock()
    sink.publish_triage_result.return_value = False
    provider = _provider(sink, correlate_triage=True)

    provider.publish_triage_result("修复失败", success=False, details={"error": "model unavailable"})

    published = sink.publish_markdown.call_args.kwargs
    assert published["title"] == "【eabot/cook !538】修复失败"
    assert MR_URL in published["content"]
    assert "model unavailable" in published["content"]


def test_blocked_triage_result_uses_external_dependency_terminal_state():
    sink = Mock()
    sink.publish_triage_result.return_value = True
    provider = _provider(sink, correlate_triage=True)

    provider.publish_triage_result(
        "当前声明依赖分支缺少接口。",
        success=False,
        details={
            "repair_outcome": "blocked",
            "final_pipeline_status": "failed",
            "blocker_summary": "当前声明依赖分支缺少接口。",
            "blocker_suggested_action": "请维护者确认候选分支。",
        },
    )

    published = sink.publish_triage_result.call_args.kwargs
    assert published["state"] is TriageCardState.REPAIR_BLOCKED
    assert published["title"] == "【eabot/cook !538】外部依赖阻塞"
    assert published["header_template"] == "orange"
    sink.publish_markdown.assert_not_called()


def test_missing_coverage_is_explicit():
    sink = Mock()
    sink.publish_triage_result.return_value = True
    provider = _provider(sink, correlate_triage=True)

    provider.publish_triage_result("修复完成", success=True, details={"final_pipeline_status": "success"})

    status = sink.publish_triage_result.call_args.kwargs["status_markdown"]
    assert "覆盖率：未提供" in status


def test_unknown_coverage_is_not_rendered_as_percentage():
    sink = Mock()
    sink.publish_triage_result.return_value = True
    provider = _provider(sink, correlate_triage=True)

    provider.publish_triage_result(
        "修复完成",
        success=True,
        details={"final_pipeline_status": "success", "final_coverage": "unknown"},
    )

    status = sink.publish_triage_result.call_args.kwargs["status_markdown"]
    assert "覆盖率：未提供" in status
    assert "unknown%" not in status


def test_gitlab_pipeline_coverage_uses_distinct_label():
    status = FeishuGitProvider._format_triage_result(
        "修复完成",
        {
            "final_pipeline_status": "success",
            "final_coverage": 63.04,
            "coverage_source": "gitlab_pipeline",
            "coverage_status": "reported",
        },
    )

    assert "Pipeline 覆盖率：63.04%" in status


def test_terminal_result_lists_attempts_pipeline_group_and_full_duration():
    status = FeishuGitProvider._format_triage_result(
        "修复失败",
        {
            "final_pipeline_status": "failed",
            "coverage_status": "report_missing",
            "processing_total_ms": 1_625_000,
            "push_attempts": [
                {"attempt_sequence": 1, "commit_sha": "commit-1"},
                {"attempt_sequence": 2, "commit_sha": "commit-2"},
            ],
            "pipeline_groups": [{
                "root_pipeline_id": 29920,
                "validation_pipeline_id": 29921,
                "status": "failed",
                "failed_jobs": ["build_release_arm64"],
            }],
            "duration_breakdown": {
                "same_mr_wait_ms": 152_000,
                "context_duration_ms": 30_000,
                "hermes_duration_ms": 617_000,
                "git_publish_duration_ms": 8_000,
                "pipeline_wait_duration_ms": 415_000,
                "post_pipeline_duration_ms": 96_000,
            },
        },
    )

    assert "修复提交 1: `commit-1`" in status
    assert "修复提交 2: `commit-2`" in status
    assert "root `29920` / validation `29921` / `failed`" in status
    assert "覆盖率：覆盖率 Job 通过，但未产出报告" in status
    assert "处理总耗时: 27m05s" in status
    assert "Hermes 10m17s" in status
    assert "剩余失败 jobs: build_release_arm64" in status
