import asyncio
import os
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from pr_agent.distributed.broker import RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import (
    MrKey,
    NotificationEnvelope,
    TaskEnvelope,
    TaskKind,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.distributed.notifications import NotificationConsumer, build_card_update_notification
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.feishu.feishu_client import FeishuSendResult

pytestmark = pytest.mark.skipif(not os.getenv("PR_AGENT_TEST_REDIS_URL"), reason="PR_AGENT_TEST_REDIS_URL is not set")


def _binding() -> TriageCardBinding:
    return TriageCardBinding.new(
        card_id="card-538",
        task_id="",
        open_message_id="",
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


def _task(task_id: str) -> TaskEnvelope:
    return replace(
        TaskEnvelope.new(
            kind=TaskKind.PR_COMMAND,
            source="feishu",
            mr=MrKey("eabot/cook", 538),
            pr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
            command="/triage",
            payload={"sender_id": "ou_owner"},
            idempotency_key="feishu-card:event-538",
        ),
        task_id=task_id,
    )


def test_binding_survives_restart_and_duplicate_click(monkeypatch):
    async def run_test():
        redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override=redis_url)
        first_client = RedisClientFactory(redis_url).create_async()
        await first_client.flushdb()
        try:
            first = RedisBroker(first_client, settings)
            await first.save_triage_card(_binding(), ttl_seconds=2_592_000)
            results = await asyncio.gather(
                first.enqueue_task_with_card(_task("task-a"), "card-538", "om_538", 2_592_000),
                first.enqueue_task_with_card(_task("task-b"), "card-538", "om_538", 2_592_000),
            )

            assert sum(result.created for result in results) == 1
            assert len({result.task_id for result in results}) == 1
            assert await first_client.xlen(first.keys.ingress_stream) == 1

            assert await first.save_triage_card(_binding(), ttl_seconds=2_592_000) is False

            second = RedisBroker(RedisClientFactory(redis_url).create_async(), settings)
            try:
                task_id = results[0].task_id
                restored = await second.get_task_triage_card(task_id)
                assert restored is not None
                assert restored.card_id == "card-538"
                assert restored.open_message_id == "om_538"
                assert restored.status_markdown == ""

                changed = await second.transition_triage_card(
                    task_id,
                    {TriageCardState.PIPELINE_FAILED},
                    TriageCardState.REPAIR_QUEUED,
                    "已进入修复队列",
                )
                assert changed is not None
                assert changed.state is TriageCardState.REPAIR_QUEUED
                assert changed.status_markdown == "已进入修复队列"
            finally:
                await second.redis.aclose()
        finally:
            await first_client.flushdb()
            await first_client.aclose()

    asyncio.run(run_test())


def test_card_binding_rejects_different_recipient(monkeypatch):
    async def run_test():
        redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override=redis_url)
        client = RedisClientFactory(redis_url).create_async()
        await client.flushdb()
        broker = RedisBroker(client, settings)
        try:
            await broker.save_triage_card(_binding(), ttl_seconds=2_592_000)

            with pytest.raises(ValueError, match="recipient mismatch"):
                await broker.enqueue_task_with_card(
                    _task("task-attacker"),
                    "card-538",
                    "om_538",
                    2_592_000,
                    sender_id="ou_attacker",
                )

            assert await client.xlen(broker.keys.ingress_stream) == 0
            assert await client.get(broker.keys.task_triage_card("task-attacker")) is None
        finally:
            await client.flushdb()
            await client.aclose()

    asyncio.run(run_test())


def test_newer_card_rejects_older_card_action(monkeypatch):
    async def run_test():
        redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override=redis_url)
        client = RedisClientFactory(redis_url).create_async()
        await client.flushdb()
        broker = RedisBroker(client, settings)
        old_binding = _binding()
        new_binding = replace(
            _binding(),
            card_id="card-539",
            pipeline_id=29416,
            pipeline_sha="def456",
            current_pipeline_id=29416,
            current_pipeline_sha="def456",
        )
        try:
            await broker.save_triage_card(old_binding, ttl_seconds=2_592_000)
            await broker.save_triage_card(new_binding, ttl_seconds=2_592_000)

            with pytest.raises(ValueError, match="stale"):
                await broker.enqueue_task_with_card(
                    replace(_task("task-old"), command="/repair-pipeline"),
                    old_binding.card_id,
                    "om_old",
                    2_592_000,
                    sender_id="ou_owner",
                    category="pipeline",
                    pipeline_id=old_binding.pipeline_id,
                    pipeline_sha=old_binding.pipeline_sha,
                    revision=0,
                )
        finally:
            await client.flushdb()
            await client.aclose()

    asyncio.run(run_test())


def test_card_fallback_is_enqueued_once_atomically(monkeypatch):
    async def run_test():
        redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override=redis_url)
        client = RedisClientFactory(redis_url).create_async()
        await client.flushdb()
        broker = RedisBroker(client, settings)
        try:
            await broker.save_triage_card(_binding(), ttl_seconds=2_592_000)
            fallback = NotificationEnvelope.new(
                task_id="task-538",
                receive_id="ou_owner",
                recipient_email="",
                recipient_username="",
                kind="markdown",
                content="【eabot/cook !538】修复失败",
                title="【eabot/cook !538】修复失败",
                header_template="red",
                mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
                notification_id="fallback-538",
            )

            results = await asyncio.gather(
                *(broker.enqueue_card_fallback("card-538", fallback) for _ in range(20))
            )

            assert sum(results) == 1
            assert await client.xlen(broker.keys.notification_stream) == 1
            stored = await broker.get_triage_card("card-538")
            assert stored is not None
            assert stored.fallback_sent is True
        finally:
            await client.flushdb()
            await client.aclose()

    asyncio.run(run_test())


def test_terminal_patch_failure_enqueues_one_fallback_across_replay(monkeypatch):
    async def run_test():
        redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override=redis_url)
        client = RedisClientFactory(redis_url).create_async()
        await client.flushdb()
        broker = RedisBroker(client, settings)
        try:
            await broker.save_triage_card(_binding(), ttl_seconds=2_592_000)
            enqueue_result = await broker.enqueue_task_with_card(
                _task("task-538"),
                "card-538",
                "om_538",
                2_592_000,
                sender_id="ou_owner",
            )
            await broker.transition_triage_card(
                enqueue_result.task_id,
                {TriageCardState.PIPELINE_FAILED},
                TriageCardState.REPAIR_QUEUED,
                "queued",
            )
            terminal = await broker.transition_triage_card(
                enqueue_result.task_id,
                {TriageCardState.REPAIR_QUEUED},
                TriageCardState.REPAIR_FAILED,
                "修复失败\n- Commit: `def456`\n- Pipeline: `30003`",
            )
            assert terminal is not None
            update = build_card_update_notification(
                terminal,
                enqueue_result.task_id,
                terminal.state,
                terminal.status_markdown,
            )
            feishu = AsyncMock()
            feishu.send_notification.return_value = FeishuSendResult(
                False,
                None,
                False,
                "400:230110:deleted",
            )
            consumer = NotificationConsumer(broker, feishu, settings)

            await consumer.process(update)
            await consumer.process(update)

            assert await client.xlen(broker.keys.notification_stream) == 1
            stored = await broker.get_triage_card("card-538")
            assert stored is not None
            assert stored.fallback_sent is True
        finally:
            await client.flushdb()
            await client.aclose()

    asyncio.run(run_test())
