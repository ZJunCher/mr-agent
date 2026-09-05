import asyncio
import os
import time
from contextlib import asynccontextmanager

import pytest

from pr_agent.distributed.broker import RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import DeliveryKind, MrKey, PipelineEvent, TaskEnvelope, TaskKind, TaskStatus
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


def make_task(idempotency_key="note:e2e"):
    return TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="gitlab",
        mr=MrKey("eabot/cook", 536),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        command="/triage",
        payload={},
        idempotency_key=idempotency_key,
    )


def test_three_agents_register_and_duplicate_webhook_is_deduplicated(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            for worker_id in ("agent-1", "agent-2", "agent-3"):
                await broker.heartbeat_worker(worker_id, 0, 0)

            task = make_task()
            first = await broker.enqueue_task(task)
            second = await broker.enqueue_task(make_task())

            assert len(await broker.list_live_workers()) == 3
            assert first.created is True
            assert second.created is False
            assert second.task_id == first.task_id

    asyncio.run(run_test())


def test_pipeline_terminal_resumes_same_task_under_ten_seconds(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            task = make_task("note:pipeline-e2e")
            await broker.heartbeat_worker("agent-1", 0, 0)
            await broker.enqueue_task(task)
            lease = await broker.claim_mr(task.mr, "agent-1", 30)
            await broker.assign_to_worker(task, lease, "agent-1")
            await broker.transition_task(task.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease)
            execute_delivery = await broker.read_worker_inbox("agent-1", block_ms=10)
            await broker.ack_worker_inbox("agent-1", execute_delivery.message_id)
            await broker.register_pipeline_wait(task.task_id, task.mr.project_id, "abc")
            await broker.transition_task(task.task_id, {TaskStatus.RUNNING}, TaskStatus.WAITING_PIPELINE, lease)
            event = PipelineEvent.new(
                project_id=task.mr.project_id,
                pipeline_id=29415,
                sha="abc",
                status="success",
                ref="feature/fix",
            )

            started = time.monotonic()
            assert await broker.publish_pipeline_event(event) == [task.task_id]
            delivery = await broker.read_worker_inbox("agent-1", block_ms=1000)
            elapsed = time.monotonic() - started

            assert delivery.task.task_id == task.task_id
            assert delivery.kind is DeliveryKind.RESUME_PIPELINE
            assert elapsed < 10

    asyncio.run(run_test())


def test_scheduler_uses_other_worker_when_one_is_full_and_fills_remaining_slots(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            await broker.heartbeat_worker("agent-full", 4, 4)
            await broker.heartbeat_worker("agent-spare", 0, 0)
            first = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="gitlab",
                mr=MrKey("eabot/chogori", 305),
                pr_url="https://gitlab.example/eabot/chogori/-/merge_requests/305",
                command="/triage",
                payload={},
                idempotency_key="slot:e2e:first",
            )
            await broker.enqueue_task(first)
            scheduler = TaskScheduler(broker, "scheduler-e2e")
            assert await scheduler.dispatch_available(limit=1) == 1
            assert (await broker.get_task(first.task_id)).worker_id == "agent-spare"

        async with create_broker(monkeypatch) as broker:
            await broker.heartbeat_worker("agent-flex", 1, 1)
            tasks = []
            for iid in (306, 307, 308):
                task = TaskEnvelope.new(
                    kind=TaskKind.PR_COMMAND,
                    source="gitlab",
                    mr=MrKey("eabot/chogori", iid),
                    pr_url=f"https://gitlab.example/eabot/chogori/-/merge_requests/{iid}",
                    command="/review",
                    payload={},
                    idempotency_key=f"slot:e2e:{iid}",
                )
                tasks.append(task)
                await broker.enqueue_task(task)
            scheduler = TaskScheduler(broker, "scheduler-e2e")
            assert await scheduler.dispatch_available(limit=3) == 3
            assert {(await broker.get_task(task.task_id)).worker_id for task in tasks} == {"agent-flex"}

    asyncio.run(run_test())
