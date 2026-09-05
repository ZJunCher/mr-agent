import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from pr_agent.distributed.broker import LostLeaseError, RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import DeliveryKind, MrKey, TaskEnvelope, TaskKind, TaskStatus
from pr_agent.distributed.redis_client import RedisClientFactory

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


def make_task(task_id: str | None = None) -> TaskEnvelope:
    task = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="gitlab",
        mr=MrKey("eabot/cook", 536),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        command="/triage",
        payload={},
        idempotency_key="note:1:536:99:create",
    )
    return replace(task, task_id=task_id) if task_id else task


def test_concurrent_duplicate_enqueue_is_atomic(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            results = await asyncio.gather(*(broker.enqueue_task(make_task(f"task-{index}")) for index in range(100)))

            assert sum(result.created for result in results) == 1
            assert len({result.task_id for result in results}) == 1
            assert await broker.redis.xlen(broker.keys.ingress_stream) == 1

    asyncio.run(run_test())


def test_expired_owner_cannot_transition_task(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            task = make_task()
            await broker.enqueue_task(task)
            first = await broker.claim_mr(task.mr, "worker-1", lease_seconds=30)
            await broker.force_expire_mr_for_test(task.mr)
            second = await broker.claim_mr(task.mr, "worker-2", lease_seconds=30)

            assert second.fencing_token > first.fencing_token
            with pytest.raises(LostLeaseError):
                await broker.transition_task(task.task_id, {TaskStatus.QUEUED}, TaskStatus.RUNNING, first)
            assert await broker.transition_task(
                task.task_id, {TaskStatus.QUEUED}, TaskStatus.RUNNING, second
            ) is True

    asyncio.run(run_test())


def test_triage_priority_pauses_and_resumes_auto_once(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            mr = MrKey("eabot/cook", 536)
            auto = replace(
                make_task("auto-536"),
                kind=TaskKind.AUTO_WORKFLOW,
                command="/auto",
                payload={"commands": ["/describe", "/mr_create"]},
                idempotency_key="auto:536",
            )
            triage = replace(make_task("triage-536"), idempotency_key="triage:536:first")
            duplicate = replace(make_task("triage-duplicate"), idempotency_key="triage:536:second")
            await broker.enqueue_task(auto)
            lease = await broker.claim_mr(mr, "worker-1", lease_seconds=30)
            assert await broker.assign_to_worker(auto, lease, "worker-1") is True
            assert await broker.transition_task(auto.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease) is True

            first = await broker.enqueue_task(triage)
            second = await broker.enqueue_task(duplicate)
            assert first.created is True
            assert second.created is False
            assert second.task_id == triage.task_id
            assert await broker.has_pending_triage(mr) is True

            assert await broker.pause_auto_for_triage(
                auto.task_id,
                mr,
                triage_task_id=triage.task_id,
                next_command_index=1,
                completed_commands=["/describe"],
                workflow_head_sha="abc123",
                lease=lease,
            ) is True
            paused = await broker.get_task(auto.task_id)
            assert paused.status is TaskStatus.PAUSED_BY_TRIAGE
            assert paused.auto_next_command_index == 1
            assert paused.auto_completed_commands == ("/describe",)

            assert await broker.assign_to_worker(triage, lease, "worker-1") is True
            assert await broker.transition_task(
                triage.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease
            ) is True
            assert await broker.transition_task(
                triage.task_id, {TaskStatus.RUNNING}, TaskStatus.PUBLISHING, lease
            ) is True
            assert await broker.transition_task(
                triage.task_id, {TaskStatus.PUBLISHING}, TaskStatus.COMPLETED, lease
            ) is True
            assert await broker.resume_auto_after_triage(
                mr,
                triage_task_id=triage.task_id,
                worker_id="worker-1",
                fencing_token=lease.fencing_token,
            ) is True
            assert await broker.resume_auto_after_triage(
                mr,
                triage_task_id=triage.task_id,
                worker_id="worker-1",
                fencing_token=lease.fencing_token,
            ) is False

            resumed = await broker.get_task(auto.task_id)
            assert resumed.status is TaskStatus.ASSIGNED
            entries = await broker.redis.xrange(broker.keys.worker_inbox("worker-1"), min="-", max="+")
            resume_entries = [
                fields for _, fields in entries if fields.get("delivery_kind") == DeliveryKind.RESUME_AUTO
            ]
            assert len(resume_entries) == 1
            assert await broker.has_pending_triage(mr) is False

    asyncio.run(run_test())
