import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from pr_agent.distributed.broker import LostLeaseError, RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import (
    DeliveryKind,
    MrKey,
    PipelineEvent,
    PipelineResumeClaim,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
)
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.distributed.scheduler import TaskScheduler

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


def make_task():
    return TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="gitlab",
        mr=MrKey("eabot/cook", 536),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        command="/review",
        payload={},
        idempotency_key="note:chaos",
    )


def test_dead_running_owner_releases_mr_and_task_is_reassignable(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            task = make_task()
            await broker.heartbeat_worker("agent-dead", 0, 0)
            await broker.heartbeat_worker("agent-live", 0, 0)
            await broker.enqueue_task(task)
            old_lease = await broker.claim_mr(task.mr, "agent-dead", 30)
            await broker.assign_to_worker(task, old_lease, "agent-dead")
            await broker.transition_task(task.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, old_lease)
            await broker.expire_worker_for_test("agent-dead")

            scheduler = TaskScheduler(broker, "agent-live")
            assert await scheduler.recover_dead_workers() == [task.task_id]
            new_lease = await broker.claim_mr(task.mr, "agent-live", 30)

            assert new_lease.worker_id == "agent-live"
            assert new_lease.fencing_token > old_lease.fencing_token

    asyncio.run(run_test())


def test_old_fence_cannot_complete_effect_after_takeover(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            task = make_task()
            old_lease = await broker.claim_mr(task.mr, "agent-old", 30)
            await broker.claim_effect(f"{task.task_id}:comment", old_lease)
            assert await broker.revoke_mr_if_owner(old_lease) is True
            new_lease = await broker.claim_mr(task.mr, "agent-new", 30)

            with pytest.raises(LostLeaseError):
                await broker.complete_effect(f"{task.task_id}:comment", old_lease, {"comment_id": "1"})
            assert new_lease.fencing_token > old_lease.fencing_token

    asyncio.run(run_test())


def test_push_attempt_metadata_is_reconciled_after_worker_takeover(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            task = make_task()
            old_lease = await broker.claim_mr(task.mr, "agent-old", 30)
            effect_key = f"{task.task_id}:push:attempt-1-diff-a"
            metadata = {
                "attempt_id": "attempt-1-diff-a",
                "attempt_sequence": 1,
                "diff_fingerprint": "diff-a",
                "commit_sha": "commit-sha-1",
            }
            started = await broker.claim_effect(effect_key, old_lease, metadata)
            assert started.status == "started"

            assert await broker.revoke_mr_if_owner(old_lease)
            new_lease = await broker.claim_mr(task.mr, "agent-new", 30)
            recovered = await broker.claim_effect(effect_key, new_lease, {"unexpected": "replacement"})
            assert recovered.status == "started"
            assert recovered.metadata == metadata
            assert await broker.complete_effect(effect_key, new_lease, {"commit_sha": "commit-sha-1"})

            completed = await broker.claim_effect(effect_key, new_lease, metadata)
            assert completed.status == "completed"
            assert completed.result == {"commit_sha": "commit-sha-1"}
            assert len(await broker.redis.keys(f"{broker.keys.prefix}:effect:*")) == 1

    asyncio.run(run_test())


def test_pipeline_wait_moves_to_live_worker_and_duplicate_event_resumes_once(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            task = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="feishu",
                mr=MrKey("eabot/cook", 541),
                pr_url="https://gitlab.example/eabot/cook/-/merge_requests/541",
                command="/triage",
                payload={"sender_id": "ou_1"},
                idempotency_key="triage:chaos:pipeline-wait",
            )
            await broker.heartbeat_worker("agent-dead", 0, 0)
            await broker.heartbeat_worker("agent-live", 0, 0)
            await broker.enqueue_task(task)
            old_lease = await broker.claim_mr(task.mr, "agent-dead", 30)
            await broker.assign_to_worker(task, old_lease, "agent-dead")
            await broker.transition_task(task.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, old_lease)
            old_delivery = await broker.read_worker_inbox("agent-dead", block_ms=10)
            await broker.ack_worker_inbox("agent-dead", old_delivery.message_id)
            await broker.register_pipeline_wait(
                task.task_id,
                task.mr.project_id,
                "commit-sha-1",
                attempt_id="attempt-1-diff-a",
                pipeline_id=30001,
            )
            await broker.transition_task(task.task_id, {TaskStatus.RUNNING}, TaskStatus.WAITING_PIPELINE, old_lease)
            await broker.expire_worker_for_test("agent-dead")

            scheduler = TaskScheduler(broker, "agent-live")
            assert await scheduler.recover_dead_workers() == [task.task_id]
            recovered = await broker.get_task(task.task_id)
            assert recovered.status is TaskStatus.WAITING_PIPELINE
            assert recovered.worker_id == "agent-live"
            assert recovered.fencing_token > old_lease.fencing_token
            new_lease = await broker.get_mr_lease(task.mr)
            assert new_lease is not None

            child_event = PipelineEvent.new(
                project_id=task.mr.project_id,
                pipeline_id=30002,
                sha="commit-sha-1",
                status="success",
                ref="feature/lidar",
                source="parent_pipeline",
            )
            parent_event = PipelineEvent.new(
                project_id=task.mr.project_id,
                pipeline_id=30001,
                sha="commit-sha-1",
                status="success",
                ref="feature/lidar",
                source="merge_request_event",
            )
            assert await broker.publish_pipeline_event(child_event) == [task.task_id]
            assert await broker.publish_pipeline_event(parent_event) == []
            delivery = await broker.read_worker_inbox("agent-live", block_ms=10)
            assert delivery.kind is DeliveryKind.RESUME_PIPELINE
            assert delivery.task.task_id == task.task_id
            before_claim = await broker.get_task(task.task_id)
            claim = await broker.claim_pipeline_resume(task.task_id, child_event, new_lease)
            assert claim is PipelineResumeClaim.CLAIMED
            after_claim = await broker.get_task(task.task_id)
            assert after_claim.status is TaskStatus.RUNNING
            assert after_claim.heartbeat_at > before_claim.heartbeat_at
            replay = await broker.claim_pipeline_resume(task.task_id, child_event, new_lease)
            assert replay is PipelineResumeClaim.DUPLICATE
            entries = await broker.redis.xrange(broker.keys.worker_inbox("agent-live"), min="-", max="+")
            assert sum(fields.get("delivery_kind") == DeliveryKind.RESUME_PIPELINE for _, fields in entries) == 1

    asyncio.run(run_test())
