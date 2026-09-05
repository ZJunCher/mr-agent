import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import (
    NotificationEnvelope,
    RepairCategory,
    RepairItemStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.distributed.notifications import (
    NotificationConsumer,
    QueuedNotificationSink,
    build_auto_failure_rollback_reminder,
    build_card_update_notification,
    build_repair_progress_reminder,
    build_triage_result_notification,
    build_triage_terminal_reminder,
    queue_pipeline_repair_progress,
)
from pr_agent.feishu.feishu_client import FeishuClient, FeishuSendResult
from pr_agent.triage.failure_categories import pipeline_repair_item, repair_items_for_categories


def test_auto_failure_rollback_reminder_is_red_and_idempotent():
    binding = TriageCardBinding.new(
        card_id="card-1",
        task_id="repair-task",
        open_message_id="om-1",
        receive_id="ou-owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        project_id="eabot/cook",
        mr_iid=536,
        mr_title="repair",
        source_branch="feature/x",
        pipeline_id=1,
        pipeline_sha="a" * 40,
        original_markdown="failed",
    )
    binding = replace(binding, rollback_commit_count=2, rollback_trigger="auto_failure")

    success = build_auto_failure_rollback_reminder(
        binding,
        "rollback-task",
        succeeded=True,
        rollback_commit_sha="b" * 40,
        failure_message="",
    )
    replay = build_auto_failure_rollback_reminder(
        binding,
        "rollback-task",
        succeeded=True,
        rollback_commit_sha="b" * 40,
        failure_message="",
    )
    failure = build_auto_failure_rollback_reminder(
        binding,
        "rollback-task",
        succeeded=False,
        rollback_commit_sha="",
        failure_message="分支已有新提交",
    )

    assert "修复失败，本次自动修改已撤回" in success.content
    assert "撤回 Commit: bbbbbbbbbbbb" in success.content
    assert success.header_template == "red"
    assert success.notification_id == replay.notification_id
    assert "修复失败，自动撤回未完成" in failure.content
    assert "分支已有新提交" in failure.content
    assert failure.header_template == "red"


def test_notification_retry_reuses_same_feishu_uuid(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        broker = AsyncMock()
        broker.fail_notification_attempt.side_effect = [1]
        client = AsyncMock()
        client.send_notification.side_effect = [
            FeishuSendResult(False, None, True, "timeout"),
            FeishuSendResult(True, "om_1", False, ""),
        ]
        notification = NotificationEnvelope.new(
            task_id="task-1",
            receive_id="ou_1",
            recipient_email="",
            recipient_username="",
            kind="markdown",
            content="done",
            title="Triage",
            header_template="green",
            mr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        )
        consumer = NotificationConsumer(broker, client, settings, sleep=AsyncMock())

        await consumer.process(notification)

        assert [call.args[0].notification_id for call in client.send_notification.await_args_list] == [
            notification.notification_id,
            notification.notification_id,
        ]
        broker.complete_notification.assert_awaited_once_with(notification.notification_id, "om_1")

    asyncio.run(run_test())


def test_missing_recipient_is_dead_lettered(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        broker = AsyncMock()
        client = AsyncMock()
        client.resolve_open_id_for_gitlab_user.return_value = None
        notification = NotificationEnvelope.new(
            task_id="task-1",
            receive_id="",
            recipient_email="alice@example.com",
            recipient_username="alice",
            kind="markdown",
            content="done",
            title="Triage",
            header_template="green",
            mr_url="https://gitlab.example/mr/1",
        )
        consumer = NotificationConsumer(broker, client, settings)

        await consumer.process(notification)

        broker.dead_letter_notification.assert_awaited_once_with(notification.notification_id, "recipient_not_found")
        client.send_notification.assert_not_awaited()

    asyncio.run(run_test())


def test_pipeline_failure_notification_outcomes_are_persisted(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        updates = []
        monkeypatch.setattr(
            "pr_agent.triage.ci_failure_store.update_notification_state",
            lambda card_id, state, reason="": updates.append((card_id, state, reason)) or True,
        )
        broker = AsyncMock()
        client = AsyncMock()
        client.send_notification.return_value = FeishuSendResult(True, "om_1", False, "")
        notification = NotificationEnvelope.new(
            task_id="pipeline-event",
            receive_id="ou_1",
            recipient_email="",
            recipient_username="alice",
            kind="action_card",
            content="failed",
            title="Pipeline failed",
            header_template="blue",
            mr_url="https://gitlab.example/mr/1",
            notification_id="pipeline-failure-card-1",
        )

        await NotificationConsumer(broker, client, settings).process(notification)

        assert updates == [("card-1", "delivered", "")]

    asyncio.run(run_test())


def test_pipeline_failure_missing_recipient_is_persisted(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        updates = []
        monkeypatch.setattr(
            "pr_agent.triage.ci_failure_store.update_notification_state",
            lambda card_id, state, reason="": updates.append((card_id, state, reason)) or True,
        )
        broker = AsyncMock()
        client = AsyncMock()
        client.resolve_open_id_for_gitlab_user.return_value = None
        notification = NotificationEnvelope.new(
            task_id="pipeline-event",
            receive_id="",
            recipient_email="alice@example.com",
            recipient_username="alice",
            kind="action_card",
            content="failed",
            title="Pipeline failed",
            header_template="blue",
            mr_url="https://gitlab.example/mr/1",
            notification_id="pipeline-failure-card-2",
        )

        await NotificationConsumer(broker, client, settings).process(notification)

        assert updates == [("card-2", "recipient_missing", "recipient_not_found")]

    asyncio.run(run_test())


def test_pipeline_failure_retry_exhaustion_is_persisted(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        updates = []
        monkeypatch.setattr(
            "pr_agent.triage.ci_failure_store.update_notification_state",
            lambda card_id, state, reason="": updates.append((card_id, state, reason)) or True,
        )
        broker = AsyncMock()
        broker.fail_notification_attempt.return_value = settings.notification_retry_limit
        client = AsyncMock()
        client.send_notification.return_value = FeishuSendResult(False, None, False, "forbidden")
        notification = NotificationEnvelope.new(
            task_id="pipeline-event",
            receive_id="ou_1",
            recipient_email="",
            recipient_username="alice",
            kind="action_card",
            content="failed",
            title="Pipeline failed",
            header_template="blue",
            mr_url="https://gitlab.example/mr/1",
            notification_id="pipeline-failure-card-3",
        )

        await NotificationConsumer(broker, client, settings).process(notification)

        assert updates == [("card-3", "failed", "delivery_failed")]

    asyncio.run(run_test())


def test_queue_notification_retry_uses_deterministic_id():
    broker = Mock()
    sink = QueuedNotificationSink(broker, task_id="task-1")

    sink.publish_markdown(
        receive_id="ou_1",
        content="done",
        title="Triage",
        header_template="green",
        mr_url="https://gitlab.example/mr/1",
    )
    sink.publish_markdown(
        receive_id="ou_1",
        content="done",
        title="Triage",
        header_template="green",
        mr_url="https://gitlab.example/mr/1",
    )

    ids = [call.args[0].notification_id for call in broker.enqueue_notification.call_args_list]
    assert len(set(ids)) == 1


def test_terminal_result_notification_contains_complete_triage_context():
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc1234567890",
        original_markdown="build_release_arm64 failed: missing package",
    )
    status = "修复完成\n\n- Commit: `def456`\n\n- Pipeline: `success`\n\n- Coverage: 63.04%\n\n- 耗时: 554.0s"

    notification = build_triage_result_notification(
        binding,
        "task-538",
        TriageCardState.REPAIR_SUCCEEDED,
        status,
    )

    assert notification.kind == "markdown"
    assert notification.title == "【eabot/cook !538】修复成功"
    assert notification.header_template == "green"
    for expected in (
        "lidar udp",
        "feature/lidar",
        "29415",
        "build_release_arm64 failed: missing package",
        "修复完成",
        "def456",
        "success",
        "63.04%",
        "554.0s",
    ):
        assert expected in notification.content


def test_pipeline_repair_progress_updates_latest_pipeline_identity():
    async def run_test():
        item = replace(
            pipeline_repair_item(30100, "source-sha"),
            task_id="task-repair",
            status=RepairItemStatus.RUNNING,
        )
        binding = replace(
            TriageCardBinding.new(
                card_id="card-538",
                task_id="task-repair",
                open_message_id="om_538",
                receive_id="ou_owner",
                mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
                project_id="eabot/cook",
                mr_iid=538,
                mr_title="lidar udp",
                source_branch="feature/lidar",
                pipeline_id=30100,
                pipeline_sha="source-sha",
                original_markdown="build failed",
                repair_items=(item,),
            ),
            active_task_id="task-repair",
            active_category=RepairCategory.PIPELINE.value,
            state=TriageCardState.WAITING_PIPELINE,
        )
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.update_repair_progress_with_notification.return_value = True

        changed = await queue_pipeline_repair_progress(
            broker,
            "task-repair",
            TriageCardState.REPAIR_RUNNING,
            "正在检查最新流水线",
            30101,
            "triage-sha",
        )

        assert changed is True
        call = broker.update_repair_progress_with_notification.await_args
        assert call.args[4:6] == (30101, "triage-sha")
        notification = call.args[6]
        assert "30101" in notification.content
        assert "triage-sha" in notification.content

    asyncio.run(run_test())


def test_terminal_result_notification_id_is_stable_across_replays():
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        original_markdown="build failed",
    )
    first = build_triage_result_notification(
        binding,
        "task-538",
        TriageCardState.REPAIR_SUCCEEDED,
        "修复完成",
    )
    replay = build_triage_result_notification(
        binding,
        "task-538",
        TriageCardState.REPAIR_FAILED,
        "重复投递了冲突的终态",
    )

    assert first.notification_id == replay.notification_id


def test_terminal_reminder_is_short_and_stable():
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        original_markdown="build failed",
    )
    binding = replace(
        binding,
        repair_items=(replace(
            pipeline_repair_item(29415, "abc123"),
            task_id="task-538",
            result_pipeline_id=30003,
            result_pipeline_sha="def456",
        ),),
    )
    status = (
        "修复完成\n\n- 修复提交 1: `abc123`\n- 修复提交 2: `def456`\n"
        "- 流水线 2: root `30002` / validation `30003` / `success`\n- Coverage: 63.04%"
    )

    reminder = build_triage_terminal_reminder(
        binding,
        "task-538",
        TriageCardState.REPAIR_SUCCEEDED,
        status,
    )
    replay = build_triage_terminal_reminder(
        binding,
        "task-538",
        TriageCardState.REPAIR_SUCCEEDED,
        status,
    )

    assert reminder.kind == "text"
    assert reminder.notification_id == replay.notification_id
    assert "✅【eabot/cook !538】修复成功" in reminder.content
    assert "Commit: def456" in reminder.content
    assert "Pipeline: #30003" in reminder.content
    assert binding.mr_url in reminder.content
    assert "Coverage" not in reminder.content
    assert "build failed" not in reminder.content


def test_partial_terminal_reminder_is_orange_and_distinct():
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        original_markdown="build failed",
    )

    reminder = build_triage_terminal_reminder(
        binding,
        "task-538",
        TriageCardState.REPAIR_PARTIAL,
        "Format：修复成功\nClang：修复失败",
    )

    assert "⚠️【eabot/cook !538】部分修复成功" in reminder.content
    assert reminder.header_template == "orange"


def test_blocked_terminal_reminder_is_orange_and_not_called_failure():
    binding = TriageCardBinding.new(
        card_id="card-120",
        task_id="task-120",
        open_message_id="om_120",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/prism/-/merge_requests/120",
        project_id="eabot/prism",
        mr_iid=120,
        mr_title="dependency blocker",
        source_branch="end2areas",
        pipeline_id=33871,
        pipeline_sha="abc123",
        original_markdown="build_release_arm64 failed",
    )

    reminder = build_triage_terminal_reminder(
        binding,
        "task-120",
        TriageCardState.REPAIR_BLOCKED,
        "外部依赖阻塞\n- Pipeline: `#33871`",
    )

    assert "⛔【eabot/prism !120】自动修复被外部依赖阻塞" in reminder.content
    assert "修复失败" not in reminder.content
    assert "原流水线卡片已更新阻塞原因和人工处理建议" in reminder.content
    assert reminder.header_template == "orange"
    assert binding.mr_url in reminder.content


def test_model_unavailable_terminal_reminder_is_safe_and_retryable():
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-model-outage",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        original_markdown="build failed",
    )

    reminder = build_triage_terminal_reminder(
        binding,
        "task-model-outage",
        TriageCardState.REPAIR_MODEL_UNAVAILABLE,
        "模型服务暂时不可用；request-id=req-secret；claude-opus-4-8 http_503。",
    )

    assert reminder.header_template == "orange"
    assert "⚠️【eabot/cook !538】模型服务不可用，建议稍后重试。" in reminder.content
    assert "原流水线卡片已更新。" in reminder.content
    assert "原始 Pipeline: #29415" in reminder.content
    assert "request-id" not in reminder.content
    assert "claude-opus" not in reminder.content
    assert "修复失败" not in reminder.content
    assert binding.mr_url in reminder.content


def test_canceled_repair_sends_one_short_reminder():
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        original_markdown="build failed",
    )

    reminder = build_triage_terminal_reminder(
        binding,
        "task-538",
        TriageCardState.CANCELED,
        "修复已取消。已提交的代码不会自动回退。",
    )

    assert reminder.kind == "text"
    assert "⏹️【eabot/cook !538】修复已取消" in reminder.content
    assert binding.mr_url in reminder.content


def test_queued_sink_updates_original_without_duplicate_terminal_card():
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        original_markdown="build failed",
    )
    binding = replace(binding, state=TriageCardState.REPAIR_RUNNING)
    broker = Mock()
    broker.get_task_triage_card.return_value = binding
    broker.transition_triage_card_with_notification.return_value = True
    broker.enqueue_notification.return_value = True
    sink = QueuedNotificationSink(broker, task_id="task-538")

    handled = sink.publish_triage_result(
        state=TriageCardState.REPAIR_SUCCEEDED,
        status_markdown="修复完成\n\n- Coverage: 63.04%",
    )

    assert handled is True
    broker.transition_triage_card_with_notification.assert_called_once()
    broker.enqueue_notification.assert_not_called()


def test_multi_action_sink_reopens_build_after_format_or_triage_result():
    items = repair_items_for_categories([RepairCategory.FORMAT, RepairCategory.BUILD], 30041, "old-sha")
    items = tuple(
        replace(item, task_id="task-538", status=RepairItemStatus.RUNNING)
        if item.category is RepairCategory.FORMAT
        else item
        for item in items
    )
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="two failures",
        source_branch="feature/two",
        pipeline_id=30041,
        pipeline_sha="old-sha",
        original_markdown="format and build failed",
        repair_items=items,
    )
    binding = replace(
        binding,
        active_task_id="task-538",
        active_category=RepairCategory.FORMAT.value,
        revision=1,
    )
    broker = Mock()
    broker.get_task_triage_card.return_value = binding
    broker.reconcile_repair_card_with_notification.return_value = True
    sink = QueuedNotificationSink(broker, task_id="task-538")

    handled = sink.publish_triage_result(
        state=TriageCardState.REPAIR_FAILED,
        status_markdown="格式已修复，build 仍失败",
        details={
            "pipeline_groups": [
                {"validation_pipeline_id": 30100, "failed_jobs": ["build_release_arm64"]}
            ],
            "push_attempts": [{"commit_sha": "new-sha"}],
        },
    )

    assert handled is True
    call = broker.reconcile_repair_card_with_notification.call_args
    reconciled = call.args[2]
    assert reconciled[0].status is RepairItemStatus.SUCCEEDED
    assert reconciled[1].status is RepairItemStatus.PENDING
    assert call.args[3] is TriageCardState.PIPELINE_FAILED


def test_multi_action_sink_keeps_dependency_blocker_terminal():
    item = replace(
        repair_items_for_categories([RepairCategory.BUILD], 33871, "source-sha")[0],
        task_id="task-120",
        status=RepairItemStatus.RUNNING,
    )
    binding = TriageCardBinding.new(
        card_id="card-120",
        task_id="task-120",
        open_message_id="om_120",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/prism/-/merge_requests/120",
        project_id="eabot/prism",
        mr_iid=120,
        mr_title="dependency blocker",
        source_branch="end2areas",
        pipeline_id=33871,
        pipeline_sha="source-sha",
        original_markdown="build failed",
        repair_items=(item,),
    )
    binding = replace(
        binding,
        active_task_id="task-120",
        active_category=RepairCategory.BUILD.value,
        revision=1,
    )
    broker = Mock()
    broker.get_task_triage_card.return_value = binding
    broker.reconcile_repair_card_with_notification.return_value = True
    sink = QueuedNotificationSink(broker, task_id="task-120")

    handled = sink.publish_triage_result(
        state=TriageCardState.REPAIR_BLOCKED,
        status_markdown="当前声明依赖分支缺少接口。",
        details={
            "repair_outcome": "blocked",
            "final_pipeline_status": "failed",
            "blocker_summary": "当前声明依赖分支缺少接口。",
        },
    )

    assert handled is True
    call = broker.reconcile_repair_card_with_notification.call_args
    assert call.args[2][0].status is RepairItemStatus.BLOCKED
    assert call.args[3] is TriageCardState.REPAIR_BLOCKED
    assert "撤回修复" not in call.args[8].content


def test_partial_progress_reminder_is_short_and_names_remaining_category():
    items = repair_items_for_categories([RepairCategory.FORMAT, RepairCategory.BUILD], 30041, "old-sha")
    items = (
        replace(items[0], task_id="task-538", status=RepairItemStatus.SUCCEEDED),
        replace(items[1], status=RepairItemStatus.PENDING),
    )
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="two failures",
        source_branch="feature/two",
        pipeline_id=30100,
        pipeline_sha="new-sha",
        original_markdown="format and build failed",
        repair_items=items,
    )

    reminder = build_repair_progress_reminder(binding, "task-538")

    assert reminder.kind == "text"
    assert "Format 修复完成" in reminder.content
    assert "Build" in reminder.content
    assert "原卡" in reminder.content
    assert "原始失败信息" not in reminder.content


def test_batch_progress_reminder_summarizes_all_selected_categories():
    items = repair_items_for_categories(
        [RepairCategory.FORMAT, RepairCategory.CLANG, RepairCategory.BUILD],
        30100,
        "old-sha",
    )
    items = (
        replace(items[0], task_id="task-538", status=RepairItemStatus.SUCCEEDED),
        replace(items[1], task_id="task-538", status=RepairItemStatus.FAILED),
        replace(items[2], status=RepairItemStatus.PENDING),
    )
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="three failures",
        source_branch="feature/three",
        pipeline_id=30100,
        pipeline_sha="old-sha",
        original_markdown="three failures",
        repair_items=items,
        repair_card_mode="multi_select",
    )

    reminder = build_repair_progress_reminder(binding, "task-538")

    assert "所选问题自动修复未完成" in reminder.content
    assert "Build" in reminder.content
    assert "任务: task-538" in reminder.content
    assert "原始 Pipeline: #30100" in reminder.content


def test_unbound_queued_sink_enqueues_one_deduplicated_terminal_result():
    broker = Mock()
    broker.get_task_triage_card.return_value = None
    broker.enqueue_notification.return_value = True
    sink = QueuedNotificationSink(broker, task_id="task-legacy")
    common = {
        "state": TriageCardState.REPAIR_FAILED,
        "receive_id": "ou_owner",
        "content": "**MR:** [eabot/cook !538](https://gitlab.example/eabot/cook/-/merge_requests/538)\n\n修复失败",
        "title": "【eabot/cook !538】修复失败",
        "header_template": "red",
        "mr_url": "https://gitlab.example/eabot/cook/-/merge_requests/538",
    }

    first = sink.publish_triage_result(status_markdown="修复失败\n\n- 耗时: 10.0s", **common)
    replay = sink.publish_triage_result(status_markdown="修复失败\n\n- 耗时: 11.0s", **common)

    assert first is True
    assert replay is True
    notifications = [call.args[0] for call in broker.enqueue_notification.call_args_list]
    assert len(notifications) == 2
    assert notifications[0].notification_id == notifications[1].notification_id


def test_action_card_send_records_message_id(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        broker = AsyncMock()
        client = AsyncMock()
        client.send_notification.return_value = FeishuSendResult(True, "om_538", False, "")
        notification = NotificationEnvelope.new(
            task_id="pipeline-event-1",
            receive_id="ou_owner",
            recipient_email="",
            recipient_username="",
            kind="action_card",
            content='{"markdown":"build failed","actions":[]}',
            title="【eabot/cook !538】流水线失败",
            header_template="blue",
            mr_url="https://gitlab.example.com/eabot/cook/-/merge_requests/538",
            card_id="card-538",
        )

        await NotificationConsumer(broker, client, settings).process(notification)

        broker.record_card_message.assert_awaited_once_with("card-538", "om_538", "ou_owner")
        broker.complete_notification.assert_awaited_once_with(notification.notification_id, "om_538")
        lifecycle_event = broker.record_lifecycle_event.await_args.args[0]
        assert lifecycle_event.phase.value == "notification"
        assert lifecycle_event.kind.value == "end"
        assert lifecycle_event.segment_id == notification.notification_id

    asyncio.run(run_test())


def test_message_registration_failure_does_not_resend_card(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        broker = AsyncMock()
        broker.record_card_message.side_effect = RuntimeError("redis unavailable")
        client = AsyncMock()
        client.send_notification.return_value = FeishuSendResult(True, "om_538", False, "")
        notification = NotificationEnvelope.new(
            task_id="pipeline-event-1",
            receive_id="ou_owner",
            recipient_email="",
            recipient_username="",
            kind="action_card",
            content='{"markdown":"build failed","actions":[]}',
            title="【eabot/cook !538】流水线失败",
            header_template="blue",
            mr_url="https://gitlab.example.com/eabot/cook/-/merge_requests/538",
            card_id="card-538",
        )

        await NotificationConsumer(broker, client, settings).process(notification)

        client.send_notification.assert_awaited_once()
        broker.complete_notification.assert_awaited_once_with(notification.notification_id, "om_538")

    asyncio.run(run_test())


def test_action_card_only_copies_allowed_callback_fields():
    notification = NotificationEnvelope.new(
        task_id="task-1",
        receive_id="ou_owner",
        recipient_email="",
        recipient_username="",
        kind="action_card",
        content=(
            '{"markdown":"build failed","actions":[{"command":"triage","label":"修复",'
            '"card_id":"card-538","pipeline_id":29415,"category":"pipeline",'
            '"pipeline_sha":"abc123","revision":4,"secret":"must-not-leak"}]}'
        ),
        title="【eabot/cook !538】流水线失败",
        header_template="blue",
        mr_url="https://gitlab.example.com/eabot/cook/-/merge_requests/538",
    )

    payload = FeishuClient()._notification_payload(notification)
    card = json.loads(payload["content"])
    value = card["elements"][-1]["actions"][0]["value"]

    assert value == {
        "command": "triage",
        "mr_url": notification.mr_url,
        "card_id": "card-538",
        "pipeline_id": 29415,
        "category": "pipeline",
        "pipeline_sha": "abc123",
        "revision": 4,
    }


def _terminal_binding(state=TriageCardState.REPAIR_SUCCEEDED):
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        original_markdown="build failed",
    )
    status = "修复完成\n- 修复 Commit: `def456`\n- 结果 Pipeline: `#30003`"
    item = replace(
        pipeline_repair_item(29415, "abc123"),
        task_id="task-538",
        result_pipeline_id=30003,
        result_pipeline_sha="def456",
    )
    return replace(binding, state=state, status_markdown=status, repair_items=(item,))


def test_terminal_reminder_identifies_repair_attempt_and_pipelines():
    task_id = "77c0a8ec815e480da490fbf7a23c6b6f"
    original = _terminal_binding()
    binding = replace(
        original,
        task_id=task_id,
        repair_items=tuple(replace(item, task_id=task_id) for item in original.repair_items),
    )

    reminder = build_triage_terminal_reminder(
        binding,
        task_id,
        TriageCardState.REPAIR_SUCCEEDED,
        binding.status_markdown,
    )

    assert "任务: 77c0a8ec815e" in reminder.content
    assert "原始 Pipeline: #29415" in reminder.content
    assert "结果 Pipeline: #30003" in reminder.content


def test_terminal_reminder_does_not_label_evidence_pipeline_as_repair_result():
    task_id = "failed-without-repair-commit"
    item = replace(
        pipeline_repair_item(34796, "source-sha"),
        task_id=task_id,
        result_pipeline_id=0,
        result_pipeline_sha="",
    )
    binding = replace(
        _terminal_binding(TriageCardState.REPAIR_FAILED),
        task_id=task_id,
        repair_items=(item,),
        status_markdown="修复失败\n- 当前失败 Pipeline: `#34796`\n- 本次未产生修复提交",
    )

    reminder = build_triage_terminal_reminder(
        binding,
        task_id,
        TriageCardState.REPAIR_FAILED,
        binding.status_markdown,
    )

    assert "原始 Pipeline: #29415" in reminder.content
    assert "结果 Pipeline" not in reminder.content
    assert "Commit:" not in reminder.content


def test_terminal_reminders_for_same_mr_distinguish_repair_attempts():
    first_task_id = "def4a25dcd4642dea227e784cf441508"
    second_task_id = "77c0a8ec815e480da490fbf7a23c6b6f"
    binding = _terminal_binding()

    first = build_triage_terminal_reminder(
        binding,
        first_task_id,
        TriageCardState.REPAIR_FAILED,
        binding.status_markdown,
    )
    first_replay = build_triage_terminal_reminder(
        binding,
        first_task_id,
        TriageCardState.REPAIR_FAILED,
        binding.status_markdown,
    )
    second = build_triage_terminal_reminder(
        binding,
        second_task_id,
        TriageCardState.REPAIR_SUCCEEDED,
        binding.status_markdown,
    )

    assert first.notification_id == first_replay.notification_id
    assert first.notification_id != second.notification_id
    assert "任务: def4a25dcd46" in first.content
    assert "任务: 77c0a8ec815e" in second.content


def test_card_update_uses_actual_task_id_for_detail_link(monkeypatch):
    binding = replace(_terminal_binding(), task_id="", active_task_id="")
    monkeypatch.setattr(
        "pr_agent.feishu.triage_card.build_repair_details_url",
        lambda task_id: f"https://agent.example/repair-results/{task_id}?sig=safe",
    )

    update = build_card_update_notification(
        binding,
        "task-terminal-result",
        TriageCardState.REPAIR_SUCCEEDED,
        binding.status_markdown,
    )

    card = json.loads(update.content)
    assert card["elements"][-1]["actions"][0]["text"]["content"] == "查看修复详情"
    assert card["elements"][-1]["actions"][0]["url"].endswith("/task-terminal-result?sig=safe")


def test_terminal_patch_success_enqueues_short_reminder(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        binding = _terminal_binding()
        update = build_card_update_notification(
            binding,
            binding.task_id,
            binding.state,
            binding.status_markdown,
        )
        broker = AsyncMock()
        broker.get_triage_card.return_value = binding
        broker.enqueue_notification.return_value = True
        client = AsyncMock()
        client.send_notification.return_value = FeishuSendResult(True, binding.open_message_id, False, "")

        await NotificationConsumer(broker, client, settings).process(update)

        reminder = broker.enqueue_notification.await_args.args[0]
        expected = build_triage_terminal_reminder(
            binding,
            binding.task_id,
            binding.state,
            binding.status_markdown,
        )
        assert reminder.notification_id == expected.notification_id
        assert reminder.kind == expected.kind
        assert reminder.content == expected.content
        broker.enqueue_card_fallback.assert_not_awaited()
        broker.complete_notification.assert_awaited_once_with(update.notification_id, binding.open_message_id)

    asyncio.run(run_test())


def test_terminal_patch_permanent_failure_enqueues_complete_fallback(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        binding = _terminal_binding(TriageCardState.REPAIR_FAILED)
        update = build_card_update_notification(
            binding,
            binding.task_id,
            binding.state,
            binding.status_markdown,
        )
        broker = AsyncMock()
        broker.get_triage_card.return_value = binding
        broker.fail_notification_attempt.return_value = 1
        client = AsyncMock()
        client.send_notification.return_value = FeishuSendResult(False, None, False, "400:230110:deleted")

        await NotificationConsumer(broker, client, settings).process(update)

        call = broker.enqueue_card_fallback.await_args
        assert call.args[0] == binding.card_id
        fallback = call.args[1]
        assert fallback.kind == "markdown"
        assert fallback.title == "【eabot/cook !538】修复失败"
        assert "build failed" in fallback.content
        broker.enqueue_notification.assert_not_awaited()
        broker.dead_letter_notification.assert_awaited_once_with(update.notification_id, "400:230110:deleted")

    asyncio.run(run_test())


def test_terminal_patch_exhausted_retry_enqueues_complete_fallback(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        binding = _terminal_binding()
        update = build_card_update_notification(
            binding,
            binding.task_id,
            binding.state,
            binding.status_markdown,
        )
        broker = AsyncMock()
        broker.get_triage_card.return_value = binding
        broker.fail_notification_attempt.return_value = settings.notification_retry_limit
        client = AsyncMock()
        client.send_notification.return_value = FeishuSendResult(False, None, True, "timeout")

        await NotificationConsumer(broker, client, settings).process(update)

        broker.enqueue_card_fallback.assert_awaited_once()
        broker.enqueue_notification.assert_not_awaited()
        broker.dead_letter_notification.assert_awaited_once_with(update.notification_id, "timeout")

    asyncio.run(run_test())


def test_stale_card_update_is_completed_without_patching_feishu(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        current = _terminal_binding()
        waiting = replace(current, state=TriageCardState.WAITING_PIPELINE, status_markdown="等待流水线 #30003")
        stale = build_card_update_notification(
            waiting,
            waiting.task_id,
            waiting.state,
            waiting.status_markdown,
        )
        broker = AsyncMock()
        broker.get_triage_card.return_value = current
        client = AsyncMock()

        await NotificationConsumer(broker, client, settings).process(stale)

        client.send_notification.assert_not_awaited()
        broker.enqueue_notification.assert_not_awaited()
        broker.complete_notification.assert_awaited_once_with(stale.notification_id, stale.message_id)

    asyncio.run(run_test())


def test_legacy_stale_card_update_cannot_overwrite_terminal_card(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        current = _terminal_binding()
        waiting = replace(current, state=TriageCardState.WAITING_PIPELINE, status_markdown="等待流水线 #30003")
        stale = replace(
            build_card_update_notification(
                waiting,
                waiting.task_id,
                waiting.state,
                waiting.status_markdown,
            ),
            card_state="",
        )
        broker = AsyncMock()
        broker.get_triage_card.return_value = current
        client = AsyncMock()

        await NotificationConsumer(broker, client, settings).process(stale)

        client.send_notification.assert_not_awaited()
        broker.complete_notification.assert_awaited_once_with(stale.notification_id, stale.message_id)

    asyncio.run(run_test())


def test_permanent_patch_failure_does_not_create_standalone_message(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        broker = AsyncMock()
        broker.fail_notification_attempt.return_value = 1
        client = AsyncMock()
        client.send_notification.return_value = FeishuSendResult(False, None, False, "400:230110:deleted")
        update = NotificationEnvelope.new(
            task_id="task-538",
            receive_id="ou_owner",
            recipient_email="",
            recipient_username="",
            kind="card_update",
            content=json.dumps({"header": {"title": {"content": "【eabot/cook !538】修复失败"}}}),
            title="【eabot/cook !538】修复失败",
            header_template="red",
            mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
            card_id="card-538",
            message_id="om_538",
            fallback_content=(
                "【eabot/cook !538】修复失败\n"
                "MR: https://gitlab.example/eabot/cook/-/merge_requests/538"
            ),
        )

        await NotificationConsumer(broker, client, settings).process(update)

        broker.dead_letter_notification.assert_awaited_once_with(update.notification_id, "400:230110:deleted")
        broker.enqueue_card_fallback.assert_not_awaited()

    asyncio.run(run_test())


def test_retryable_patch_failure_does_not_fallback(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        broker = AsyncMock()
        broker.fail_notification_attempt.return_value = settings.notification_retry_limit
        client = AsyncMock()
        client.send_notification.return_value = FeishuSendResult(False, None, True, "timeout")
        update = NotificationEnvelope.new(
            task_id="task-538",
            receive_id="ou_owner",
            recipient_email="",
            recipient_username="",
            kind="card_update",
            content="{}",
            title="【eabot/cook !538】修复成功",
            header_template="green",
            mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
            card_id="card-538",
            message_id="om_538",
            fallback_content="result",
        )

        await NotificationConsumer(broker, client, settings).process(update)

        broker.dead_letter_notification.assert_awaited_once()
        broker.enqueue_card_fallback.assert_not_awaited()

    asyncio.run(run_test())


def test_consumer_does_not_ack_when_notification_processing_crashes(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        notification = NotificationEnvelope.new(
            task_id="task-538",
            receive_id="ou_owner",
            recipient_email="",
            recipient_username="",
            kind="card_update",
            content="{}",
            title="【eabot/cook !538】修复失败",
            header_template="red",
            mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
            card_id="card-538",
            message_id="om_538",
        )
        broker = AsyncMock()
        broker.read_notification.return_value = ("stream-1", notification)
        consumer = NotificationConsumer(broker, AsyncMock(), settings)

        async def fail(_notification):
            consumer.stop_event.set()
            raise RuntimeError("redis unavailable")

        consumer.process = AsyncMock(side_effect=fail)

        await consumer.run()

        broker.ack_notification.assert_not_awaited()

    asyncio.run(run_test())


def test_terminal_card_replay_does_not_send_standalone_fallback():
    binding = TriageCardBinding.new(
        card_id="card-538",
        task_id="task-538",
        open_message_id="om_538",
        receive_id="ou_owner",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        original_markdown="build failed",
    )
    binding = replace(binding, state=TriageCardState.REPAIR_SUCCEEDED)
    broker = Mock()
    broker.get_task_triage_card.return_value = binding
    sink = QueuedNotificationSink(broker, task_id="task-538")

    handled = sink.publish_card_update(
        state=TriageCardState.REPAIR_SUCCEEDED,
        status_markdown="修复完成",
    )

    assert handled is True
    broker.transition_triage_card_with_notification.assert_not_called()


def test_deleted_original_message_does_not_create_extra_result_card(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override="redis://localhost:6379/0")
        broker = AsyncMock()
        broker.fail_notification_attempt.return_value = 1
        client = AsyncMock()
        client.send_notification.side_effect = [
            FeishuSendResult(True, "om_538", False, ""),
            FeishuSendResult(False, None, False, "400:230110:message_not_found"),
            FeishuSendResult(True, "om_526", False, ""),
        ]
        notifications = [
            NotificationEnvelope.new(
                task_id=f"task-{iid}",
                receive_id="ou_owner",
                recipient_email="",
                recipient_username="",
                kind="card_update",
                content=json.dumps({"iid": iid}),
                title=f"【{project} !{iid}】修复成功",
                header_template="green",
                mr_url=f"https://gitlab.example/{project}/-/merge_requests/{iid}",
                card_id=f"card-{iid}",
                message_id=f"om_{iid}",
                fallback_content=f"【{project} !{iid}】修复成功",
            )
            for project, iid in (("eabot/cook", 538), ("eabot/chogori", 302), ("eabot/cook", 526))
        ]
        consumer = NotificationConsumer(broker, client, settings)

        for notification in notifications:
            await consumer.process(notification)

        assert broker.complete_notification.await_count == 2
        broker.dead_letter_notification.assert_awaited_once()
        broker.enqueue_card_fallback.assert_not_awaited()

    asyncio.run(run_test())
