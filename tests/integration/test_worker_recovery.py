import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from pr_agent.distributed.broker import RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import (
    DeliveryKind,
    MrKey,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.distributed.notifications import queue_triage_card_update
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.distributed.scheduler import TaskScheduler
from pr_agent.triage.failure_categories import pipeline_repair_item

pytestmark = pytest.mark.skipif(not os.getenv("PR_AGENT_TEST_REDIS_URL"), reason="PR_AGENT_TEST_REDIS_URL is not set")


@asynccontextmanager
async def create_broker(monkeypatch):
    redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
    settings = load_distributed_settings(redis_url_override=redis_url)
    client = RedisClientFactory(redis_url).create_async()
    await client.flushdb()
    try:
        yield RedisBroker(client, settings)
    finally:
        await client.flushdb()
        await client.aclose()


def test_dead_owner_task_is_requeued_once_with_higher_next_fence(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            task = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="gitlab",
                mr=MrKey("eabot/cook", 536),
                pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
                command="/review",
                payload={},
                idempotency_key="note:worker-recovery",
            )
            await broker.heartbeat_worker("worker-dead", 0, 0)
            await broker.heartbeat_worker("worker-live", 0, 0)
            await broker.enqueue_task(task)
            old_lease = await broker.claim_mr(task.mr, "worker-dead", lease_seconds=30)
            assert await broker.assign_to_worker(task, old_lease, "worker-dead") is True
            assert await broker.transition_task(
                task.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, old_lease
            ) is True
            await broker.expire_worker_for_test("worker-dead")

            scheduler = TaskScheduler(broker, "worker-live")
            assert await scheduler.recover_dead_workers() == [task.task_id]
            assert await scheduler.recover_dead_workers() == []
            recovered = await broker.get_task(task.task_id)
            assert recovered.status is TaskStatus.QUEUED
            assert recovered.attempt == 1

            new_lease = await broker.claim_mr(task.mr, "worker-live", lease_seconds=30)
            assert new_lease.fencing_token > old_lease.fencing_token

    asyncio.run(run_test())


def test_card_binding_survives_worker_recovery_and_deduplicates_terminal_update(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            mr_url = "https://gitlab.example/eabot/cook/-/merge_requests/538"
            binding = TriageCardBinding.new(
                card_id="card-538",
                task_id="",
                open_message_id="",
                receive_id="ou_owner",
                mr_url=mr_url,
                project_id="eabot/cook",
                mr_iid=538,
                mr_title="lidar udp",
                source_branch="feature/lidar",
                pipeline_id=29415,
                pipeline_sha="abc123",
                original_markdown="build failed",
            )
            task = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="feishu",
                mr=MrKey("eabot/cook", 538),
                pr_url=mr_url,
                command="/triage",
                payload={"sender_id": "ou_owner"},
                idempotency_key="feishu-card:event-538",
            )
            await broker.save_triage_card(binding, ttl_seconds=2_592_000)
            result = await broker.enqueue_task_with_card(
                task,
                binding.card_id,
                "om_538",
                2_592_000,
                sender_id="ou_owner",
            )
            await broker.transition_triage_card(
                result.task_id,
                {TriageCardState.PIPELINE_FAILED},
                TriageCardState.REPAIR_QUEUED,
                "queued",
            )
            await broker.transition_triage_card(
                result.task_id,
                {TriageCardState.REPAIR_QUEUED},
                TriageCardState.REPAIR_RUNNING,
                "running",
            )
            restarted = RedisBroker(broker.redis, broker.settings)

            first = await queue_triage_card_update(
                restarted,
                result.task_id,
                TriageCardState.REPAIR_SUCCEEDED,
                "流水线通过",
            )
            duplicate = await queue_triage_card_update(
                restarted,
                result.task_id,
                TriageCardState.REPAIR_SUCCEEDED,
                "流水线通过",
            )

            assert first is True
            assert duplicate is False
            assert await broker.redis.xlen(broker.keys.notification_stream) == 1
            restored = await restarted.get_task_triage_card(result.task_id)
            assert restored is not None
            assert restored.open_message_id == "om_538"
            assert restored.state is TriageCardState.REPAIR_SUCCEEDED

    asyncio.run(run_test())


def test_paused_auto_resumes_once_after_lease_moves_to_new_worker(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            mr = MrKey("eabot/cook", 541)
            auto = TaskEnvelope.new(
                kind=TaskKind.AUTO_WORKFLOW,
                source="gitlab",
                mr=mr,
                pr_url="https://gitlab.example/eabot/cook/-/merge_requests/541",
                command="/auto",
                payload={"commands": ["/describe", "/mr_create"]},
                idempotency_key="auto:541",
            )
            triage = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="feishu",
                mr=mr,
                pr_url=auto.pr_url,
                command="/triage",
                payload={"sender_id": "ou_1"},
                idempotency_key="triage:541",
            )
            await broker.enqueue_task(auto)
            await broker.enqueue_task(triage)
            old_lease = await broker.claim_mr(mr, "worker-dead", 30)
            await broker.assign_to_worker(auto, old_lease, "worker-dead")
            await broker.transition_task(auto.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, old_lease)
            await broker.pause_auto_for_triage(
                auto.task_id,
                mr,
                triage_task_id=triage.task_id,
                next_command_index=1,
                completed_commands=["/describe"],
                workflow_head_sha="abc123",
                lease=old_lease,
            )
            await broker.heartbeat_worker("worker-dead", 1, 1)
            await broker.heartbeat_worker("worker-live", 0, 0)
            await broker.expire_worker_for_test("worker-dead")

            scheduler = TaskScheduler(broker, "worker-live")
            assert await scheduler.recover_dead_workers() == [auto.task_id]
            assert await broker.get_mr_lease(mr) is None
            assert await scheduler.dispatch_available(limit=4) == 1
            assigned_triage = await broker.get_task(triage.task_id)
            assert assigned_triage.worker_id == "worker-live"
            new_lease = await broker.get_mr_lease(mr)
            assert new_lease is not None
            await broker.transition_task(triage.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, new_lease)
            await broker.transition_task(triage.task_id, {TaskStatus.RUNNING}, TaskStatus.PUBLISHING, new_lease)
            await broker.transition_task(triage.task_id, {TaskStatus.PUBLISHING}, TaskStatus.COMPLETED, new_lease)

            assert await broker.resume_auto_after_triage(
                mr,
                triage_task_id=triage.task_id,
                worker_id=new_lease.worker_id,
                fencing_token=new_lease.fencing_token,
            ) is True
            assert await broker.resume_auto_after_triage(
                mr,
                triage_task_id=triage.task_id,
                worker_id=new_lease.worker_id,
                fencing_token=new_lease.fencing_token,
            ) is False

            restored = await broker.get_task(auto.task_id)
            assert restored.status is TaskStatus.ASSIGNED
            assert restored.worker_id == "worker-live"
            assert not await broker.redis.sismember(broker.keys.worker_tasks("worker-dead"), auto.task_id)
            entries = await broker.redis.xrange(broker.keys.worker_inbox("worker-live"), min="-", max="+")
            assert sum(fields.get("delivery_kind") == DeliveryKind.RESUME_AUTO for _, fields in entries) == 1

    asyncio.run(run_test())


def test_cancel_waiting_repair_releases_gate_and_reopens_card(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            mr = MrKey("eabot/cook", 545)
            mr_url = "https://gitlab.example/eabot/cook/-/merge_requests/545"
            binding = TriageCardBinding.new(
                card_id="card-545",
                task_id="",
                open_message_id="om-545",
                receive_id="ou-owner",
                mr_url=mr_url,
                project_id=mr.project_id,
                mr_iid=mr.iid,
                mr_title="cancel repair",
                source_branch="feature/cancel",
                pipeline_id=30200,
                pipeline_sha="old-sha",
                original_markdown="build failed",
                repair_items=(pipeline_repair_item(30200, "old-sha"),),
            )
            task = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="feishu",
                mr=mr,
                pr_url=mr_url,
                command="/repair-pipeline",
                payload={"sender_id": "ou-owner"},
                idempotency_key="repair:545",
            )
            await broker.save_triage_card(binding, 3600)
            result = await broker.enqueue_task_with_card(
                task,
                binding.card_id,
                binding.open_message_id,
                3600,
                sender_id=binding.receive_id,
                category="pipeline",
                pipeline_id=binding.pipeline_id,
                pipeline_sha=binding.pipeline_sha,
                revision=0,
            )
            lease = await broker.claim_mr(mr, "worker-1", 30)
            await broker.assign_to_worker(task, lease, "worker-1")
            await broker.transition_task(task.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease)
            await broker.transition_task(task.task_id, {TaskStatus.RUNNING}, TaskStatus.WAITING_PIPELINE, lease)

            requested = await broker.request_repair_cancel(
                result.task_id,
                binding.card_id,
                binding.open_message_id,
                binding.receive_id,
                1,
            )
            assert requested.accepted is True
            assert await broker.finalize_repair_cancel(
                task,
                lease,
                "修复已取消。已提交的代码不会自动回退。",
            )

            stored = await broker.get_task(task.task_id)
            card = await broker.get_triage_card(binding.card_id)
            assert stored.status is TaskStatus.CANCELED
            assert card.state is TriageCardState.CANCELED
            assert card.active_task_id == ""
            assert await broker.active_triage_task_id(mr) == ""

    asyncio.run(run_test())
