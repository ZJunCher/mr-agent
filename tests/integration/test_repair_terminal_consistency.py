import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from pr_agent.distributed.broker import RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import (
    MrKey,
    PipelineEvent,
    RepairItemStatus,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.distributed.notifications import queue_repair_reconciliation
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.triage.failure_categories import pipeline_repair_item
from pr_agent.triage.pipeline_repair import PipelineRepairPhase, PipelineRepairState

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


def test_late_success_for_latest_task_sha_corrects_failed_terminal(monkeypatch):
    async def run_test():
        async with create_broker(monkeypatch) as broker:
            mr = MrKey("eabot/cook", 546)
            mr_url = "https://gitlab.example/eabot/cook/-/merge_requests/546"
            source_pipeline_id = 30385
            source_sha = "dc78f383eb6b"
            latest_sha = "ccf6ebb7"
            binding = TriageCardBinding.new(
                card_id="card-546",
                task_id="",
                open_message_id="",
                receive_id="owner-546",
                mr_url=mr_url,
                project_id=mr.project_id,
                mr_iid=mr.iid,
                mr_title="repair terminal consistency",
                source_branch="feature/repair",
                pipeline_id=source_pipeline_id,
                pipeline_sha=source_sha,
                original_markdown="pipeline failed",
                repair_items=(pipeline_repair_item(source_pipeline_id, source_sha),),
                failed_job_names=("build_release_arm64",),
            )
            task = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="feishu",
                mr=mr,
                pr_url=mr_url,
                command="/repair-pipeline",
                payload={
                    "sender_id": "owner-546",
                    "repair_category": "pipeline",
                    "source_pipeline_id": source_pipeline_id,
                    "source_pipeline_sha": source_sha,
                },
                idempotency_key="repair:546:30385",
            )
            assert await broker.save_triage_card(binding, ttl_seconds=3600)
            assert (
                await broker.enqueue_task_with_card(
                    task,
                    binding.card_id,
                    "message-546",
                    3600,
                    sender_id="owner-546",
                    category="pipeline",
                    pipeline_id=source_pipeline_id,
                    pipeline_sha=source_sha,
                    revision=0,
                )
            ).created
            lease = await broker.claim_mr(mr, "agent-1", 30)
            assert await broker.assign_to_worker(task, lease, "agent-1")
            assert await broker.transition_task(task.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease)
            state = PipelineRepairState(
                phase=PipelineRepairPhase.TRIAGE_WAITING,
                latest_pipeline_id=30390,
                latest_pipeline_sha=latest_sha,
            )
            assert await broker.record_pipeline_repair_state(task.task_id, state, lease)
            active_binding = await broker.get_task_triage_card(task.task_id)
            failed_items = tuple(
                item.__class__.from_dict({
                    **item.to_dict(),
                    "status": RepairItemStatus.FAILED.value,
                    "status_markdown": "worker timeout",
                })
                for item in active_binding.repair_items
            )
            assert await queue_repair_reconciliation(
                broker,
                task.task_id,
                failed_items,
                TriageCardState.REPAIR_FAILED,
                "worker timeout",
                30390,
                latest_sha,
            )
            assert await broker.transition_task(
                task.task_id,
                {TaskStatus.RUNNING},
                TaskStatus.FAILED,
                lease,
                {"error": "worker timeout"},
            )

            event = PipelineEvent.new(
                project_id=mr.project_id,
                pipeline_id=30391,
                sha=latest_sha,
                status="success",
                ref="feature/repair",
            )
            with patch("pr_agent.triage.terminal.save_triage_run", return_value=True):
                assert await broker.publish_pipeline_event(event) == []

            corrected_task = await broker.get_task(task.task_id)
            corrected_card = await broker.get_task_triage_card(task.task_id)
            assert corrected_task.status is TaskStatus.FAILED
            assert corrected_task.pipeline_repair_state.final_pipeline_status == "success"
            assert corrected_card.state is TriageCardState.REPAIR_SUCCEEDED
            assert corrected_card.current_pipeline_id == 30391

    asyncio.run(run_test())
