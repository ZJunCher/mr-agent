import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent.distributed.broker import RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.executor import TaskExecutor
from pr_agent.distributed.lifecycle import LifecycleEvent, pipeline_wait_segment, summarize_lifecycle
from pr_agent.distributed.models import (
    DeliveryKind,
    MrKey,
    PipelineEvent,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.distributed.notifications import NotificationConsumer, queue_triage_terminal_notifications
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.distributed.runtime import TaskSuspended
from pr_agent.feishu.feishu_client import FeishuSendResult

pytestmark = pytest.mark.skipif(not os.getenv("PR_AGENT_TEST_REDIS_URL"), reason="PR_AGENT_TEST_REDIS_URL is not set")


@asynccontextmanager
async def create_broker(monkeypatch):
    redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
    settings = load_distributed_settings(redis_url_override=redis_url)
    client = RedisClientFactory(redis_url).create_async()
    await client.flushdb()
    try:
        yield RedisBroker(client, settings)
    finally:
        await client.flushdb()
        await client.aclose()


def _created_at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _tasks(created_at: str) -> tuple[TaskEnvelope, TaskEnvelope, TriageCardBinding]:
    mr = MrKey("eabot/cook", 541)
    mr_url = "https://gitlab.example/eabot/cook/-/merge_requests/541"
    auto = TaskEnvelope.new(
        kind=TaskKind.AUTO_WORKFLOW,
        source="gitlab",
        mr=mr,
        pr_url=mr_url,
        command="/auto",
        payload={"commands": ["/describe", "/mr_create"], "head_sha": "base-sha"},
        idempotency_key="auto:e2e:541",
    )
    triage = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="feishu",
        mr=mr,
        pr_url=mr_url,
        command="/triage",
        payload={"sender_id": "ou_owner"},
        idempotency_key="triage:e2e:541",
    )
    binding = TriageCardBinding.new(
        card_id="card-e2e-541",
        task_id="",
        open_message_id="",
        receive_id="ou_owner",
        mr_url=mr_url,
        project_id=mr.project_id,
        mr_iid=mr.iid,
        mr_title="Revert intrinsic_reader",
        source_branch="feature/lidar",
        pipeline_id=29907,
        pipeline_sha="base-sha",
        original_markdown="build_release_arm64 failed",
    )
    return replace(auto, created_at=created_at), replace(triage, created_at=created_at), binding


def test_auto_triage_two_attempts_child_pipelines_terminal_notification_and_resume(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            base = time.time() - 30
            auto, triage, binding = _tasks(_created_at(base))
            await broker.save_triage_card(binding, ttl_seconds=3600)
            await broker.heartbeat_worker("agent-1", 0, 0)
            await broker.enqueue_task(auto)
            lease = await broker.claim_mr(auto.mr, "agent-1", 30)
            await broker.assign_to_worker(auto, lease, "agent-1")
            await broker.transition_task(auto.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease)
            initial_delivery = await broker.read_worker_inbox("agent-1", block_ms=10)
            await broker.ack_worker_inbox("agent-1", initial_delivery.message_id)

            async def run_describe(_pr_url, command, **_kwargs):
                assert command == "/describe"
                await broker.enqueue_task_with_card(
                    triage,
                    binding.card_id,
                    "om_541",
                    3600,
                    sender_id="ou_owner",
                )
                await broker.transition_triage_card(
                    triage.task_id,
                    {TriageCardState.PIPELINE_FAILED},
                    TriageCardState.REPAIR_QUEUED,
                    "queued",
                )
                return True

            first_agent = Mock(handle_request=AsyncMock(side_effect=run_describe))
            executor = TaskExecutor(
                broker,
                Mock(),
                "agent-1",
                max_active_tasks=4,
                agent_factory=Mock(return_value=first_agent),
            )
            with pytest.raises(TaskSuspended) as suspended:
                await executor._run_auto_workflow(auto, lease)
            assert suspended.value.wait_kind == "mr_priority"
            assert (await broker.get_auto_cursor(auto.task_id)).next_command_index == 1
            assert [call.args[1] for call in first_agent.handle_request.await_args_list] == ["/describe"]

            await broker.assign_to_worker(triage, lease, "agent-1")
            triage_delivery = await broker.read_worker_inbox("agent-1", block_ms=10)
            await broker.ack_worker_inbox("agent-1", triage_delivery.message_id)
            await broker.transition_task(triage.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease)
            await broker.transition_triage_card(
                triage.task_id,
                {TriageCardState.REPAIR_QUEUED},
                TriageCardState.REPAIR_RUNNING,
                "running",
            )

            attempts = [
                {"attempt_id": "attempt-1-diff-a", "commit_sha": "commit-sha-1", "pipeline_id": 30001},
                {"attempt_id": "attempt-2-diff-b", "commit_sha": "commit-sha-2", "pipeline_id": 30003},
            ]
            child_statuses = ["failed", "success"]
            for index, (attempt, child_status) in enumerate(zip(attempts, child_statuses, strict=True)):
                effect_key = f"{triage.task_id}:push:{attempt['attempt_id']}"
                effect = await broker.claim_effect(effect_key, lease, attempt)
                assert effect.status == "started"
                assert await broker.complete_effect(effect_key, lease, {"commit_sha": attempt["commit_sha"]})

                segment = pipeline_wait_segment(
                    attempt["attempt_id"],
                    attempt["commit_sha"],
                    attempt["pipeline_id"],
                )
                await broker.record_lifecycle_event(
                    LifecycleEvent.new(
                        triage.task_id,
                        "pipeline_wait",
                        "start",
                        segment_id=segment,
                        occurred_at=base + 2 + index * 5,
                    )
                )
                assert await broker.register_pipeline_wait(
                    triage.task_id,
                    triage.mr.project_id,
                    attempt["commit_sha"],
                    attempt_id=attempt["attempt_id"],
                    pipeline_id=attempt["pipeline_id"],
                ) is None
                await broker.transition_task(triage.task_id, {TaskStatus.RUNNING}, TaskStatus.WAITING_PIPELINE, lease)

                parent = PipelineEvent.new(
                    project_id=triage.mr.project_id,
                    pipeline_id=attempt["pipeline_id"] - 1,
                    sha=attempt["commit_sha"],
                    status="success",
                    ref="feature/lidar",
                )
                child = PipelineEvent.new(
                    project_id=triage.mr.project_id,
                    pipeline_id=attempt["pipeline_id"],
                    sha=attempt["commit_sha"],
                    status=child_status,
                    ref="feature/lidar",
                    source="parent_pipeline",
                )
                assert await broker.publish_pipeline_event(parent) == []
                assert await broker.publish_pipeline_event(child) == [triage.task_id]
                pipeline_delivery = await broker.read_worker_inbox("agent-1", block_ms=10)
                assert pipeline_delivery.kind is DeliveryKind.RESUME_PIPELINE
                assert pipeline_delivery.payload["pipeline_id"] == attempt["pipeline_id"]
                await broker.ack_worker_inbox("agent-1", pipeline_delivery.message_id)
                await broker.record_lifecycle_event(
                    LifecycleEvent.new(
                        triage.task_id,
                        "pipeline_wait",
                        "end",
                        segment_id=segment,
                        occurred_at=base + 5 + index * 5,
                    )
                )
                await broker.transition_task(
                    triage.task_id,
                    {TaskStatus.WAITING_PIPELINE},
                    TaskStatus.RUNNING,
                    lease,
                )

            assert attempts[0]["attempt_id"] != attempts[1]["attempt_id"]
            assert attempts[0]["commit_sha"] != attempts[1]["commit_sha"]
            restarted = RedisBroker(broker.redis, broker.settings)
            recovered_effect = await restarted.claim_effect(
                f"{triage.task_id}:push:{attempts[1]['attempt_id']}",
                lease,
                attempts[1],
            )
            assert recovered_effect.status == "completed"
            assert recovered_effect.result == {"commit_sha": "commit-sha-2"}

            await broker.transition_task(triage.task_id, {TaskStatus.RUNNING}, TaskStatus.PUBLISHING, lease)
            await broker.record_task_result(
                triage.task_id,
                {
                    "success": True,
                    "push_attempts": attempts,
                    "pipeline_groups": [
                        {"root_pipeline_id": 30000, "validation_pipeline_id": 30001, "status": "failed"},
                        {"root_pipeline_id": 30002, "validation_pipeline_id": 30003, "status": "success"},
                    ],
                },
                lease,
            )
            await broker.transition_task(triage.task_id, {TaskStatus.PUBLISHING}, TaskStatus.COMPLETED, lease)
            await broker.record_lifecycle_event(
                LifecycleEvent.new(triage.task_id, "terminal", "point", occurred_at=base + 12)
            )

            await broker.transition_triage_card(
                triage.task_id,
                {TriageCardState.REPAIR_RUNNING},
                TriageCardState.WAITING_PIPELINE,
                "waiting",
            )
            terminal_markdown = "FINISHED: success=True\n\nCommit: `commit-sha-2`\nPipeline: `30003`"
            assert await queue_triage_terminal_notifications(
                broker,
                triage.task_id,
                TriageCardState.REPAIR_SUCCEEDED,
                terminal_markdown,
            )
            assert await queue_triage_terminal_notifications(
                restarted,
                triage.task_id,
                TriageCardState.REPAIR_SUCCEEDED,
                terminal_markdown,
            )
            assert await broker.redis.xlen(broker.keys.notification_stream) == 1

            notifications = []
            client = AsyncMock()
            client.send_notification.side_effect = [
                FeishuSendResult(True, "om_541", False, ""),
                FeishuSendResult(True, "om-reminder", False, ""),
            ]
            consumer = NotificationConsumer(broker, client, broker.settings, consumer_id="feishu-e2e")
            for _ in range(2):
                stream_id, notification = await broker.read_notification("feishu-e2e", block_ms=10)
                notifications.append(notification)
                await consumer.process(notification)
                await broker.ack_notification(stream_id)
            assert sum(notification.kind == "text" for notification in notifications) == 1
            assert sum(notification.kind == "card_update" for notification in notifications) == 1
            assert await broker.redis.xlen(broker.keys.notification_stream) == 0

            assert await broker.resume_auto_after_triage(
                auto.mr,
                triage_task_id=triage.task_id,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
            )
            assert not await broker.resume_auto_after_triage(
                auto.mr,
                triage_task_id=triage.task_id,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
            )
            resume_delivery = await broker.read_worker_inbox("agent-1", block_ms=10)
            assert resume_delivery.kind is DeliveryKind.RESUME_AUTO
            await broker.ack_worker_inbox("agent-1", resume_delivery.message_id)

            resumed_agent = Mock(handle_request=AsyncMock(return_value=True))
            resumed_executor = TaskExecutor(
                broker,
                Mock(),
                "agent-1",
                max_active_tasks=4,
                agent_factory=Mock(return_value=resumed_agent),
            )
            await broker.transition_task(auto.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease)
            await resumed_executor._run_auto_workflow(auto, lease)
            assert [call.args[1] for call in resumed_agent.handle_request.await_args_list] == ["/mr_create"]

            summary = summarize_lifecycle(await broker.get_lifecycle_events(triage.task_id))
            assert summary.pipeline_wait_duration_ms == 6000
            assert summary.processing_total_ms == pytest.approx(12000, abs=1)
            assert summary.delivery_total_ms is not None
            assert summary.delivery_total_ms >= summary.processing_total_ms

    asyncio.run(run_test())
