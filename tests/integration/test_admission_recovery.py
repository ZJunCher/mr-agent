import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest

from pr_agent.distributed.broker import RedisBroker, RedisKeys
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import (
    MrKey,
    RepairCategory,
    RepairItem,
    RepairItemStatus,
    TaskEnvelope,
    TaskKind,
    TriageCardBinding,
)
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.triage.failure_categories import repair_items_for_failed_jobs

pytestmark = pytest.mark.skipif(not os.getenv("PR_AGENT_TEST_REDIS_URL"), reason="PR_AGENT_TEST_REDIS_URL is not set")


async def _delete_prefix(client, prefix: str) -> None:
    cursor = 0
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=f"{prefix}:*", count=100)
        if keys:
            await client.delete(*keys)
        if int(cursor) == 0:
            return


def _repair_item() -> RepairItem:
    return RepairItem(
        category=RepairCategory.PIPELINE,
        command="/repair-pipeline",
        label="修复流水线",
        display_name="Pipeline",
        button_type="primary",
        status=RepairItemStatus.PENDING,
    )


def _task(task_id: str, idempotency_key: str) -> TaskEnvelope:
    return replace(
        TaskEnvelope.new(
            kind=TaskKind.PR_COMMAND,
            source="feishu",
            mr=MrKey("eabot/cook", 530),
            pr_url="https://gitlab.example/eabot/cook/-/merge_requests/530",
            command="/repair-pipeline",
            payload={
                "sender_id": "ou_owner",
                "repair_category": "pipeline",
                "card_revision": 0,
                "source_pipeline_id": 30305,
                "source_pipeline_sha": "fb004e58e873",
            },
            idempotency_key=idempotency_key,
        ),
        task_id=task_id,
    )


def test_card_retry_recovers_partial_admission_without_second_task(monkeypatch):
    async def run_test():
        redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override=redis_url)
        prefix = f"pr-agent:test:admission:{uuid4().hex}"
        client = RedisClientFactory(redis_url).create_async()
        broker = RedisBroker(client, settings, RedisKeys(prefix))
        mr = MrKey("eabot/cook", 530)
        old_task = _task("ghost-task", "first-click")
        retry_task = _task("retry-task", "second-click")
        binding = TriageCardBinding.new(
            card_id="card-530",
            task_id="",
            open_message_id="",
            receive_id="ou_owner",
            mr_url=old_task.pr_url,
            project_id=mr.project_id,
            mr_iid=mr.iid,
            mr_title="repair admission",
            source_branch="test/admission",
            pipeline_id=30305,
            pipeline_sha="fb004e58e873",
            original_markdown="pipeline failed",
            repair_items=(_repair_item(),),
        )
        try:
            await broker.save_triage_card(binding, ttl_seconds=3600)
            await client.hset(
                broker.keys.task(old_task.task_id),
                mapping={
                    "payload": old_task.to_json(),
                    "status": "queued",
                    "attempt": "0",
                    "created_at": "1786155734.12",
                    "updated_at": "1786155734.12",
                },
            )
            await client.set(broker.keys.mr_triage_active(mr), old_task.task_id)

            result = await broker.enqueue_task_with_card(
                retry_task,
                binding.card_id,
                "om_530",
                3600,
                sender_id="ou_owner",
                category="pipeline",
                pipeline_id=30305,
                pipeline_sha="fb004e58e873",
                revision=0,
            )

            assert result.created is False
            assert result.task_id == old_task.task_id
            assert result.recovered is True
            stored = await broker.get_task(old_task.task_id)
            assert stored is not None and stored.admission_complete
            assert await client.xlen(broker.keys.ingress_stream) == 1
            assert await client.get(broker.keys.mr_triage_active(mr)) == old_task.task_id
            assert await client.zscore(broker.keys.active_repairs, old_task.task_id) is not None
            assert await client.get(broker.keys.task_triage_card(old_task.task_id)) == binding.card_id
            restored_card = await broker.get_triage_card(binding.card_id)
            assert restored_card is not None and restored_card.active_task_id == old_task.task_id
        finally:
            await _delete_prefix(client, prefix)
            await client.aclose()

    asyncio.run(run_test())


def test_gate_reconciliation_terminalizes_legacy_ghost(monkeypatch):
    async def run_test():
        redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override=redis_url)
        prefix = f"pr-agent:test:admission:{uuid4().hex}"
        client = RedisClientFactory(redis_url).create_async()
        broker = RedisBroker(client, settings, RedisKeys(prefix))
        task = _task("legacy-ghost", "legacy-click")
        try:
            await client.hset(
                broker.keys.task(task.task_id),
                mapping={
                    "payload": task.to_json(),
                    "status": "queued",
                    "attempt": "0",
                    "created_at": "1786155734.12",
                    "updated_at": "1786155734.12",
                },
            )
            await client.set(broker.keys.mr_triage_active(task.mr), task.task_id)

            assert await broker.reconcile_admission_gate(task.mr, task.task_id) == "failed"
            assert await client.get(broker.keys.mr_triage_active(task.mr)) is None
            assert await client.hget(broker.keys.task(task.task_id), "status") == "failed"
            assert await client.hget(broker.keys.task(task.task_id), "error") == "admission_incomplete"
        finally:
            await _delete_prefix(client, prefix)
            await client.aclose()

    asyncio.run(run_test())


def test_multi_select_admission_binds_all_selected_items_once(monkeypatch):
    async def run_test():
        redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override=redis_url)
        prefix = f"pr-agent:test:admission:{uuid4().hex}"
        client = RedisClientFactory(redis_url).create_async()
        broker = RedisBroker(client, settings, RedisKeys(prefix))
        mr = MrKey("eabot/cook", 530)
        task = replace(
            _task("batch-task", "batch-click"),
            payload={
                "sender_id": "ou_owner",
                "repair_category": "batch",
                "selected_categories": ["format", "build"],
                "card_revision": 0,
                "source_pipeline_id": 30305,
                "source_pipeline_sha": "fb004e58e873",
            },
        )
        binding = TriageCardBinding.new(
            card_id="card-batch-530",
            task_id="",
            open_message_id="",
            receive_id="ou_owner",
            mr_url=task.pr_url,
            project_id=mr.project_id,
            mr_iid=mr.iid,
            mr_title="batch repair admission",
            source_branch="test/admission",
            pipeline_id=30305,
            pipeline_sha="fb004e58e873",
            original_markdown="pipeline failed",
            repair_items=repair_items_for_failed_jobs(
                [{"name": "code_format_check"}, {"name": "build_release_arm64"}],
                30305,
                "fb004e58e873",
            ),
            repair_card_mode="multi_select",
        )
        try:
            assert await broker.save_triage_card(binding, ttl_seconds=3600) is True

            first = await broker.enqueue_task_with_card(
                task,
                binding.card_id,
                "om_batch_530",
                3600,
                sender_id="ou_owner",
                category="batch",
                selected_categories=("format", "build"),
                pipeline_id=30305,
                pipeline_sha="fb004e58e873",
                revision=0,
            )
            replay = await broker.enqueue_task_with_card(
                replace(task, task_id="replayed-task"),
                binding.card_id,
                "om_batch_530",
                3600,
                sender_id="ou_owner",
                category="batch",
                selected_categories=("format", "build"),
                pipeline_id=30305,
                pipeline_sha="fb004e58e873",
                revision=0,
            )

            restored = await broker.get_triage_card(binding.card_id)
            assert first.created is True
            assert replay.created is False
            assert replay.task_id == task.task_id
            assert restored is not None
            assert restored.active_category == "batch"
            assert {item.task_id for item in restored.repair_items} == {task.task_id}
            assert {item.status for item in restored.repair_items} == {RepairItemStatus.QUEUED}
            assert await client.xlen(broker.keys.ingress_stream) == 1
        finally:
            await _delete_prefix(client, prefix)
            await client.aclose()

    asyncio.run(run_test())
