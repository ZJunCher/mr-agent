import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent.distributed.broker import (
    CancelRequestResult,
    EnqueueResult,
    RollbackRequestResult,
    StaleCardActionError,
)
from pr_agent.distributed.ingress import QueueIngress
from pr_agent.distributed.models import MrKey, TaskEnvelope, TaskKind, TriageCardBinding, TriageCardState
from pr_agent.distributed.notifications import queue_triage_card_update, queue_triage_failure_notification
from pr_agent.feishu import feishu_webhook as feishu_webhook_module
from pr_agent.feishu.feishu_webhook import handle_feishu_card_action, normalize_selected_categories
from pr_agent.feishu.long_connection_worker import FeishuLongConnectionWorker, normalize_card_action_payload
from pr_agent.triage.pipeline_freshness import PipelineFreshness, PipelineFreshnessState
from pr_agent.triage.repair_card_mode import RepairCardMode


def card_payload(command="triage", mr_url="https://gitlab.example/eabot/cook/-/merge_requests/536"):
    return {
        "header": {"event_id": "event-1", "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": "ou_1"},
            "action": {"trigger_time": "123", "value": {"command": command, "mr_url": mr_url}},
        },
    }


def correlated_card_payload():
    payload = card_payload(mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538")
    payload["event"]["action"]["value"].update(
        {
            "card_id": "card-538",
            "category": "build",
            "pipeline_id": 29415,
            "pipeline_sha": "abc123",
            "revision": 0,
        }
    )
    payload["event"]["context"] = {"open_message_id": "om_538"}
    return payload


def correlated_cancel_payload():
    payload = correlated_card_payload()
    payload["event"]["action"]["value"].update(
        {
            "command": "cancel-repair",
            "category": "pipeline",
            "task_id": "task-538",
            "revision": 2,
        }
    )
    return payload


def correlated_rollback_payload():
    payload = correlated_card_payload()
    payload["event"]["action"]["value"].update({
        "command": "rollback-repair",
        "repair_task_id": "repair-task-538",
        "revision": 4,
    })
    return payload


def correlated_post_repair_ut_payload():
    payload = correlated_card_payload()
    payload["event"]["action"]["value"].update({
        "command": "supplement-unit-tests",
        "repair_task_id": "repair-task-538",
        "revision": 4,
    })
    return payload


def truncated_unified_card_payload():
    payload = correlated_card_payload()
    payload["event"]["action"]["value"]["command"] = "repair-pipeline"
    for key in ("category", "pipeline_sha", "revision"):
        payload["event"]["action"]["value"].pop(key, None)
    return payload


def multi_select_payload(categories):
    payload = correlated_card_payload()
    payload["event"]["action"]["value"].update(
        {
            "command": "repair-pipeline",
            "repair_card_mode": "multi_select",
        }
    )
    payload["event"]["action"]["form_value"] = {"selected_categories": categories}
    payload["event"]["action"]["tag"] = "button"
    payload["event"]["action"]["action_type"] = "form_submit"
    payload["event"]["action"]["name"] = "submit_pipeline_repair"
    return payload


def use_repair_card_mode(monkeypatch, mode: RepairCardMode):
    monkeypatch.setattr("pr_agent.triage.repair_card_mode.repair_card_mode", lambda: mode)


def test_long_connection_keeps_open_message_id_for_sdk_callback():
    data = SimpleNamespace(
        header=SimpleNamespace(event_id="event-1", create_time="1775433600000"),
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_1"),
            action=SimpleNamespace(
                value={
                    "command": "triage",
                    "mr_url": "https://gitlab.example/eabot/cook/-/merge_requests/538",
                    "card_id": "card-538",
                },
                trigger_time="123",
            ),
            context=SimpleNamespace(open_message_id="om_538"),
        ),
    )

    payload = normalize_card_action_payload(data)

    assert payload["event"]["context"]["open_message_id"] == "om_538"
    assert payload["header"]["create_time"] == "1775433600000"


def test_long_connection_preserves_form_submission_fields():
    payload = normalize_card_action_payload(multi_select_payload(["clang", "build"]))

    action = payload["event"]["action"]
    assert action["form_value"] == {"selected_categories": ["clang", "build"]}
    assert action["tag"] == "button"
    assert action["action_type"] == "form_submit"
    assert action["name"] == "submit_pipeline_repair"


def test_long_connection_keeps_open_message_id_for_dict_callback():
    payload = normalize_card_action_payload(correlated_card_payload())

    assert payload["event"]["context"]["open_message_id"] == "om_538"


def test_long_connection_callback_wait_stays_within_feishu_deadline(monkeypatch):
    observed = {}

    class Future:
        def result(self, *, timeout):
            observed["timeout"] = timeout
            return {"toast": {"type": "success"}}

    def run_coroutine_threadsafe(coro, _loop):
        coro.close()
        return Future()

    worker = FeishuLongConnectionWorker.__new__(FeishuLongConnectionWorker)
    worker.loop = Mock()
    worker.callback_timeout_seconds = 2.5
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", run_coroutine_threadsafe)

    result = worker._run_async(asyncio.sleep(0), wait=True)

    assert result["toast"]["type"] == "success"
    assert 0 < observed["timeout"] < 3


def test_card_action_enqueues_without_running_pr_agent(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        queue_ingress = Mock()
        queue_ingress.enqueue_feishu_command = AsyncMock(return_value=EnqueueResult(True, "task-1"))

        result = await handle_feishu_card_action(card_payload(), queue_ingress=queue_ingress)

        assert result["toast"]["type"] == "success"
        queue_ingress.enqueue_feishu_command.assert_awaited_once()

    asyncio.run(run_test())


def test_queue_mode_card_action_ignores_project_allowlist(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("PR_AGENT_QUEUE_ALLOWLISTED_PROJECTS", "eabot/cook")
        queue_ingress = Mock()
        queue_ingress.enqueue_feishu_command = AsyncMock(return_value=EnqueueResult(True, "task-305"))
        inline_trigger = Mock()
        send_message = AsyncMock()
        monkeypatch.setattr(feishu_webhook_module.FeishuClient, "send_message", send_message)
        monkeypatch.setattr(feishu_webhook_module, "trigger_pr_agent_command", inline_trigger)

        payload = card_payload(mr_url="https://gitlab.example/eabot/chogori/-/merge_requests/305")
        result = await handle_feishu_card_action(payload, queue_ingress=queue_ingress)

        assert result["toast"]["type"] == "success"
        queue_ingress.enqueue_feishu_command.assert_awaited_once()
        send_message.assert_not_awaited()
        inline_trigger.assert_not_called()

    asyncio.run(run_test())


def test_inline_triage_card_action_does_not_send_received_command_message(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        send_message = AsyncMock()
        inline_trigger = Mock()
        monkeypatch.setattr(feishu_webhook_module.FeishuClient, "send_message", send_message)
        monkeypatch.setattr(feishu_webhook_module, "trigger_pr_agent_command", inline_trigger)
        payload = card_payload()

        result = await handle_feishu_card_action(payload)

        assert result["toast"]["type"] == "success"
        send_message.assert_not_awaited()
        inline_trigger.assert_called_once_with("triage", payload["event"]["action"]["value"]["mr_url"], "ou_1")

    asyncio.run(run_test())


def test_card_action_queue_failure_returns_error_toast(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        queue_ingress = Mock()
        queue_ingress.enqueue_feishu_command = AsyncMock(side_effect=RuntimeError("redis down"))

        result = await handle_feishu_card_action(card_payload(), queue_ingress=queue_ingress)

        assert result["toast"]["type"] == "error"

    asyncio.run(run_test())


def test_stale_card_action_is_rejected_before_enqueue(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        queue_ingress = Mock()
        queue_ingress.enqueue_feishu_command = AsyncMock()
        payload = card_payload()
        payload["header"]["create_time"] = str(int((time.time() - 3600) * 1000))

        result = await handle_feishu_card_action(payload, queue_ingress=queue_ingress)

        assert result["toast"]["type"] == "error"
        assert "已过期" in result["toast"]["content"]
        queue_ingress.enqueue_feishu_command.assert_not_awaited()

    asyncio.run(run_test())


def test_fresh_card_action_is_enqueued(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        queue_ingress = Mock()
        queue_ingress.enqueue_feishu_command = AsyncMock(return_value=EnqueueResult(True, "task-fresh"))
        payload = card_payload()
        payload["header"]["create_time"] = str(int(time.time() * 1000))

        result = await handle_feishu_card_action(payload, queue_ingress=queue_ingress)

        assert result["toast"]["type"] == "success"
        queue_ingress.enqueue_feishu_command.assert_awaited_once()

    asyncio.run(run_test())


def test_recovered_card_action_reports_real_queue_recovery(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        queue_ingress = Mock()
        queue_ingress.enqueue_feishu_command = AsyncMock(
            return_value=EnqueueResult(False, "task-recovered", recovered=True)
        )
        queue_ingress.queue_triage_card_update = AsyncMock(return_value=True)
        payload = card_payload()
        payload["header"]["create_time"] = str(int(time.time() * 1000))

        result = await handle_feishu_card_action(payload, queue_ingress=queue_ingress)

        assert result["toast"] == {
            "type": "success",
            "content": "任务状态已恢复，已重新进入修复队列",
        }

    asyncio.run(run_test())


def test_legacy_repair_card_remains_usable_after_mode_switch(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        ingress = Mock()
        ingress.enqueue_feishu_command = AsyncMock(return_value=EnqueueResult(True, "task-538"))
        ingress.queue_triage_card_update = AsyncMock(return_value=True)
        payload = correlated_card_payload()

        result = await handle_feishu_card_action(payload, queue_ingress=ingress)

        assert result["toast"]["type"] == "success"
        ingress.enqueue_feishu_command.assert_awaited_once()
        ingress.queue_triage_card_update.assert_awaited_once()

    asyncio.run(run_test())


def test_unified_card_click_enqueues_correlated_repair_workflow(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        use_repair_card_mode(monkeypatch, RepairCardMode.UNIFIED)
        ingress = Mock()
        ingress.enqueue_feishu_command = AsyncMock(return_value=EnqueueResult(True, "task-538"))
        ingress.queue_triage_card_update = AsyncMock(return_value=True)
        payload = correlated_card_payload()
        payload["event"]["action"]["value"].update(
            {"command": "repair-pipeline", "category": "pipeline", "revision": 4}
        )

        result = await handle_feishu_card_action(payload, queue_ingress=ingress)

        call = ingress.enqueue_feishu_command.await_args
        assert call.kwargs["command"] == "repair-pipeline"
        assert call.kwargs["category"] == "pipeline"
        assert call.kwargs["pipeline_id"] == 29415
        assert call.kwargs["pipeline_sha"] == "abc123"
        assert call.kwargs["revision"] == 4
        ingress.queue_triage_card_update.assert_awaited_once_with(
            "task-538", TriageCardState.REPAIR_QUEUED, "已进入修复队列"
        )
        assert result["toast"]["type"] == "success"

    asyncio.run(run_test())


def test_truncated_unified_card_recovers_validation_fields(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        use_repair_card_mode(monkeypatch, RepairCardMode.UNIFIED)
        ingress = Mock()
        ingress.resolve_unified_repair_card_action = AsyncMock(return_value=("pipeline", "abc123", 4))
        ingress.enqueue_feishu_command = AsyncMock(return_value=EnqueueResult(True, "task-538"))
        ingress.queue_triage_card_update = AsyncMock(return_value=True)

        result = await handle_feishu_card_action(truncated_unified_card_payload(), queue_ingress=ingress)

        ingress.resolve_unified_repair_card_action.assert_awaited_once_with(
            mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
            card_id="card-538",
            pipeline_id=29415,
        )
        call = ingress.enqueue_feishu_command.await_args
        assert call.kwargs["category"] == "pipeline"
        assert call.kwargs["pipeline_sha"] == "abc123"
        assert call.kwargs["revision"] == 4
        assert result["toast"]["type"] == "success"

    asyncio.run(run_test())


def test_truncated_unified_card_rejects_stale_binding(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        use_repair_card_mode(monkeypatch, RepairCardMode.UNIFIED)
        ingress = Mock()
        ingress.resolve_unified_repair_card_action = AsyncMock(side_effect=StaleCardActionError("stale"))
        ingress.enqueue_feishu_command = AsyncMock()

        result = await handle_feishu_card_action(truncated_unified_card_payload(), queue_ingress=ingress)

        assert result["toast"] == {"type": "error", "content": "卡片状态已更新，请使用最新按钮"}
        ingress.enqueue_feishu_command.assert_not_awaited()

    asyncio.run(run_test())


def test_multi_select_submit_enqueues_one_scoped_task(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        ingress = Mock()
        ingress.enqueue_feishu_command = AsyncMock(return_value=EnqueueResult(True, "task-538"))
        ingress.queue_triage_card_update = AsyncMock(return_value=True)

        result = await handle_feishu_card_action(
            multi_select_payload(["build", "clang"]),
            queue_ingress=ingress,
        )

        call = ingress.enqueue_feishu_command.await_args
        assert call.kwargs["command"] == "repair-pipeline"
        assert call.kwargs["category"] == "batch"
        assert call.kwargs["selected_categories"] == ("clang", "build")
        assert result["toast"] == {"type": "success", "content": "已提交所选问题，正在进入修复队列"}

    asyncio.run(run_test())


def test_post_repair_ut_card_enqueues_isolated_task(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        ingress = Mock()
        ingress.enqueue_post_repair_ut = AsyncMock(return_value=EnqueueResult(True, "ut-task"))

        result = await handle_feishu_card_action(correlated_post_repair_ut_payload(), queue_ingress=ingress)

        assert result["toast"] == {"type": "success", "content": "已进入单元测试补充队列"}
        ingress.enqueue_post_repair_ut.assert_awaited_once()
        assert ingress.enqueue_post_repair_ut.await_args.kwargs["repair_task_id"] == "repair-task-538"

    asyncio.run(run_test())


def test_empty_multi_select_submit_does_not_enqueue(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        ingress = Mock()
        ingress.enqueue_feishu_command = AsyncMock()

        result = await handle_feishu_card_action(multi_select_payload([]), queue_ingress=ingress)

        assert result["toast"] == {"type": "error", "content": "请至少选择一个修复类别"}
        ingress.enqueue_feishu_command.assert_not_awaited()

    asyncio.run(run_test())


@pytest.mark.parametrize("categories", [["build", "build"], ["pipeline"], {"build": True}])
def test_multi_select_rejects_duplicate_or_unsupported_categories(categories):
    with pytest.raises(ValueError):
        normalize_selected_categories(categories)


def test_cancel_card_callback_returns_immediately(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        ingress = Mock()
        ingress.cancel_feishu_repair = AsyncMock(
            return_value=CancelRequestResult("task-538", True, "running")
        )

        result = await handle_feishu_card_action(correlated_cancel_payload(), queue_ingress=ingress)

        assert result["toast"] == {"type": "success", "content": "正在取消修复"}
        ingress.cancel_feishu_repair.assert_awaited_once_with(
            task_id="task-538",
            card_id="card-538",
            open_message_id="om_538",
            sender_id="ou_1",
            revision=2,
        )

    asyncio.run(run_test())


def test_rollback_card_callback_only_enqueues(monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
        monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
        ingress = Mock()
        ingress.rollback_feishu_repair = AsyncMock(
            return_value=RollbackRequestResult("repair-task-538", "rollback-task-538", True, "queued")
        )

        result = await handle_feishu_card_action(correlated_rollback_payload(), queue_ingress=ingress)

        assert result["toast"] == {"type": "success", "content": "已进入撤回队列"}
        ingress.rollback_feishu_repair.assert_awaited_once_with(
            repair_task_id="repair-task-538",
            card_id="card-538",
            open_message_id="om_538",
            sender_id="ou_1",
            revision=4,
        )

    asyncio.run(run_test())


def test_queue_ingress_uses_atomic_card_binding():
    async def run_test():
        broker = AsyncMock()
        broker.enqueue_task_with_card.return_value = EnqueueResult(True, "task-538")
        ingress = QueueIngress(broker)

        result = await ingress.enqueue_feishu_command(
            command="triage",
            mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
            sender_id="ou_1",
            idempotency_key="feishu-card:event-1",
            card_id="card-538",
            open_message_id="om_538",
        )

        assert result.task_id == "task-538"
        call = broker.enqueue_task_with_card.await_args
        assert call.args[1:4] == ("card-538", "om_538", 2_592_000)
        assert call.kwargs == {
            "sender_id": "ou_1",
            "category": "",
            "selected_categories": (),
            "pipeline_id": None,
            "pipeline_sha": "",
            "revision": None,
        }
        assert call.args[0].pr_url == "https://gitlab.example/eabot/cook/-/merge_requests/538"

    asyncio.run(run_test())


def test_queue_ingress_persists_selected_categories():
    async def run_test():
        binding = TriageCardBinding.new(
            card_id="card-538",
            task_id="",
            open_message_id="om_538",
            receive_id="ou_1",
            mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
            project_id="eabot/cook",
            mr_iid=538,
            mr_title="test",
            source_branch="feature/test",
            pipeline_id=29415,
            pipeline_sha="current-sha",
            original_markdown="failed",
            repair_card_mode="multi_select",
        )
        broker = AsyncMock()
        broker.resolve_repair_card_selection.return_value = binding
        broker.enqueue_task_with_card.return_value = EnqueueResult(True, "task-538")
        checker = AsyncMock(return_value=PipelineFreshness(PipelineFreshnessState.CURRENT))
        ingress = QueueIngress(broker, pipeline_freshness_checker=checker)

        await ingress.enqueue_feishu_command(
            command="repair-pipeline",
            mr_url=binding.mr_url,
            sender_id="ou_1",
            idempotency_key="event-1",
            card_id=binding.card_id,
            open_message_id=binding.open_message_id,
            category="batch",
            selected_categories=("clang", "build"),
            pipeline_id=binding.pipeline_id,
            pipeline_sha=binding.pipeline_sha,
            revision=0,
        )

        task = broker.enqueue_task_with_card.await_args.args[0]
        assert task.payload["selected_categories"] == ["clang", "build"]
        assert broker.enqueue_task_with_card.await_args.kwargs["selected_categories"] == ("clang", "build")

    asyncio.run(run_test())


def test_queue_ingress_rejects_pipeline_card_when_gitlab_head_is_newer():
    async def run_test():
        binding = TriageCardBinding.new(
            card_id="card-538",
            task_id="",
            open_message_id="om_538",
            receive_id="ou_1",
            mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
            project_id="eabot/cook",
            mr_iid=538,
            mr_title="test",
            source_branch="feature/test",
            pipeline_id=29415,
            pipeline_sha="old-sha",
            original_markdown="failed",
        )
        broker = AsyncMock()
        broker.resolve_unified_repair_card.return_value = binding
        checker = AsyncMock(
            return_value=PipelineFreshness(
                PipelineFreshnessState.STALE_HEAD,
                head_sha="new-sha",
                reason="head_sha_changed",
            )
        )
        ingress = QueueIngress(broker, pipeline_freshness_checker=checker)

        with pytest.raises(StaleCardActionError):
            await ingress.enqueue_feishu_command(
                command="repair-pipeline",
                mr_url=binding.mr_url,
                sender_id="ou_1",
                idempotency_key="event-1",
                card_id=binding.card_id,
                open_message_id=binding.open_message_id,
                category="pipeline",
                pipeline_id=binding.pipeline_id,
                pipeline_sha=binding.pipeline_sha,
                revision=0,
            )

        checker.assert_awaited_once_with(binding)
        broker.enqueue_task_with_card.assert_not_awaited()

    asyncio.run(run_test())


def test_queue_ingress_admits_current_pipeline_card_after_gitlab_check():
    async def run_test():
        binding = TriageCardBinding.new(
            card_id="card-538",
            task_id="",
            open_message_id="om_538",
            receive_id="ou_1",
            mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
            project_id="eabot/cook",
            mr_iid=538,
            mr_title="test",
            source_branch="feature/test",
            pipeline_id=29415,
            pipeline_sha="current-sha",
            original_markdown="failed",
        )
        broker = AsyncMock()
        broker.resolve_unified_repair_card.return_value = binding
        broker.enqueue_task_with_card.return_value = EnqueueResult(True, "task-538")
        checker = AsyncMock(
            return_value=PipelineFreshness(
                PipelineFreshnessState.CURRENT,
                head_sha="current-sha",
                latest_pipeline_id=29415,
                latest_pipeline_status="failed",
            )
        )
        ingress = QueueIngress(broker, pipeline_freshness_checker=checker)

        result = await ingress.enqueue_feishu_command(
            command="repair-pipeline",
            mr_url=binding.mr_url,
            sender_id="ou_1",
            idempotency_key="event-1",
            card_id=binding.card_id,
            open_message_id=binding.open_message_id,
            category="pipeline",
            pipeline_id=binding.pipeline_id,
            pipeline_sha=binding.pipeline_sha,
            revision=0,
        )

        assert result.task_id == "task-538"
        broker.enqueue_task_with_card.assert_awaited_once()

    asyncio.run(run_test())


def test_card_update_transition_and_notification_are_one_broker_call():
    async def run_test():
        binding = TriageCardBinding.new(
            card_id="card-538",
            task_id="task-538",
            open_message_id="om_538",
            receive_id="ou_1",
            mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
            project_id="eabot/cook",
            mr_iid=538,
            mr_title="lidar udp",
            source_branch="feature/lidar",
            pipeline_id=29415,
            pipeline_sha="abc123",
            original_markdown="build failed",
        )
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.transition_triage_card_with_notification.return_value = True

        changed = await queue_triage_card_update(
            broker,
            "task-538",
            TriageCardState.REPAIR_QUEUED,
            "已进入修复队列",
        )

        assert changed is True
        call = broker.transition_triage_card_with_notification.await_args
        notification = call.args[4]
        assert notification.kind == "card_update"
        assert notification.card_state == TriageCardState.REPAIR_QUEUED.value
        assert notification.message_id == "om_538"
        assert notification.title == "【eabot/cook !538】已进入修复队列"
        assert json.loads(notification.content)["elements"][0]["content"].endswith("已进入修复队列")

    asyncio.run(run_test())


def test_terminal_failure_falls_back_to_identified_message_without_card_binding():
    async def run_test():
        task = TaskEnvelope.new(
            kind=TaskKind.PR_COMMAND,
            source="feishu",
            mr=MrKey("eabot/chogori", 305),
            pr_url="https://gitlab.example/eabot/chogori/-/merge_requests/305",
            command="/triage",
            payload={"sender_id": "ou_1"},
            idempotency_key="feishu-card:event-305",
        )
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = None
        broker.enqueue_notification.return_value = True

        assert await queue_triage_failure_notification(broker, task, "worker lost") is True

        notification = broker.enqueue_notification.await_args.args[0]
        assert notification.title == "【eabot/chogori !305】修复失败"
        assert "eabot/chogori !305" in notification.content
        assert task.task_id[:12] in notification.content
        assert "Coverage: 未提供" in notification.content

    asyncio.run(run_test())


def test_terminal_failure_updates_bound_card_without_duplicate_result_card():
    async def run_test():
        task = TaskEnvelope.new(
            kind=TaskKind.PR_COMMAND,
            source="feishu",
            mr=MrKey("eabot/chogori", 305),
            pr_url="https://gitlab.example/eabot/chogori/-/merge_requests/305",
            command="/triage",
            payload={"sender_id": "ou_1"},
            idempotency_key="feishu-card:event-305",
        )
        binding = TriageCardBinding.new(
            card_id="card-305",
            task_id=task.task_id,
            open_message_id="om_305",
            receive_id="ou_1",
            mr_url=task.pr_url,
            project_id="eabot/chogori",
            mr_iid=305,
            mr_title="stable hash ids",
            source_branch="feature/sync-0.1.2",
            pipeline_id=29768,
            pipeline_sha="c8dec150cf68",
            original_markdown="build_release_arm64 failed",
        )
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.transition_triage_card_with_notification.return_value = True
        broker.enqueue_notification.return_value = True

        assert await queue_triage_failure_notification(broker, task, "worker lost") is True

        broker.transition_triage_card_with_notification.assert_awaited_once()
        broker.enqueue_notification.assert_not_awaited()

    asyncio.run(run_test())
