import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from pr_agent.distributed.broker import RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import DeliveryKind, MrKey, PipelineEvent, TaskEnvelope, TaskKind, TaskStatus
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


def test_pipeline_event_resumes_original_waiting_task(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            task = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="gitlab",
                mr=MrKey("eabot/cook", 536),
                pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
                command="/triage",
                payload={},
                idempotency_key="triage-resume",
            )
            await broker.enqueue_task(task)
            lease = await broker.claim_mr(task.mr, "worker-1", 30)
            await broker.assign_to_worker(task, lease, "worker-1")
            await broker.transition_task(task.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease)
            execute_delivery = await broker.read_worker_inbox("worker-1", block_ms=10)
            await broker.ack_worker_inbox("worker-1", execute_delivery.message_id)
            assert await broker.register_pipeline_wait(task.task_id, "eabot/cook", "abc") is None
            await broker.transition_task(task.task_id, {TaskStatus.RUNNING}, TaskStatus.WAITING_PIPELINE, lease)
            event = PipelineEvent.new(
                project_id="eabot/cook",
                pipeline_id=29415,
                sha="abc",
                status="success",
                ref="feature/x",
            )

            assert await broker.publish_pipeline_event(event) == [task.task_id]
            delivery = await broker.read_worker_inbox("worker-1", block_ms=10)
            assert delivery.task.task_id == task.task_id
            assert delivery.kind is DeliveryKind.RESUME_PIPELINE
            assert delivery.payload["pipeline_id"] == 29415

    asyncio.run(run_test())


def test_pipeline_event_between_registration_and_waiting_is_not_lost(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            task = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="gitlab",
                mr=MrKey("eabot/cook", 536),
                pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
                command="/triage",
                payload={},
                idempotency_key="triage-resume-race",
            )
            await broker.enqueue_task(task)
            lease = await broker.claim_mr(task.mr, "worker-1", 30)
            await broker.assign_to_worker(task, lease, "worker-1")
            await broker.transition_task(task.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease)
            execute_delivery = await broker.read_worker_inbox("worker-1", block_ms=10)
            await broker.ack_worker_inbox("worker-1", execute_delivery.message_id)
            assert await broker.register_pipeline_wait(task.task_id, "eabot/cook", "abc") is None
            event = PipelineEvent.new(
                project_id="eabot/cook",
                pipeline_id=29415,
                sha="abc",
                status="success",
                ref="feature/x",
            )

            assert await broker.publish_pipeline_event(event) == []
            await broker.transition_task(task.task_id, {TaskStatus.RUNNING}, TaskStatus.WAITING_PIPELINE, lease)
            assert await broker.resume_pipeline_if_cached(task.task_id) is True
            delivery = await broker.read_worker_inbox("worker-1", block_ms=10)
            assert delivery.task.task_id == task.task_id
            assert delivery.kind is DeliveryKind.RESUME_PIPELINE

    asyncio.run(run_test())


def test_exact_child_wait_ignores_parent_and_running_events(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            task = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="gitlab",
                mr=MrKey("eabot/cook", 536),
                pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
                command="/triage",
                payload={},
                idempotency_key="triage-exact-child",
            )
            await broker.enqueue_task(task)
            lease = await broker.claim_mr(task.mr, "worker-1", 30)
            await broker.assign_to_worker(task, lease, "worker-1")
            await broker.transition_task(task.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease)
            execute_delivery = await broker.read_worker_inbox("worker-1", block_ms=10)
            await broker.ack_worker_inbox("worker-1", execute_delivery.message_id)
            assert await broker.register_pipeline_wait(
                task.task_id,
                "eabot/cook",
                "abc",
                attempt_id="attempt-1",
                pipeline_id=29921,
            ) is None
            await broker.transition_task(task.task_id, {TaskStatus.RUNNING}, TaskStatus.WAITING_PIPELINE, lease)

            running_child = PipelineEvent.new(
                project_id="eabot/cook",
                pipeline_id=29921,
                sha="abc",
                status="running",
                ref="feature/x",
                source="parent_pipeline",
            )
            parent = PipelineEvent.new(
                project_id="eabot/cook",
                pipeline_id=29920,
                sha="abc",
                status="success",
                ref="feature/x",
            )
            terminal_child = PipelineEvent.new(
                project_id="eabot/cook",
                pipeline_id=29921,
                sha="abc",
                status="failed",
                ref="feature/x",
                source="parent_pipeline",
            )

            assert await broker.publish_pipeline_event(running_child) == []
            assert await broker.publish_pipeline_event(parent) == []
            assert await broker.publish_pipeline_event(terminal_child) == [task.task_id]
            delivery = await broker.read_worker_inbox("worker-1", block_ms=10)
            assert delivery.payload["pipeline_id"] == 29921
            assert delivery.payload["source"] == "parent_pipeline"

    asyncio.run(run_test())
