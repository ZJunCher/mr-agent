import asyncio
import os
from dataclasses import replace

import pytest

from pr_agent.distributed.broker import RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import (
    MrKey,
    TaskEnvelope,
    TaskKind,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.distributed.notifications import queue_triage_card_update
from pr_agent.distributed.redis_client import RedisClientFactory

pytestmark = pytest.mark.skipif(not os.getenv("PR_AGENT_TEST_REDIS_URL"), reason="PR_AGENT_TEST_REDIS_URL is not set")


def test_three_mrs_complete_out_of_order_without_cross_updates(monkeypatch):
    async def run_test():
        redis_url = os.environ["PR_AGENT_TEST_REDIS_URL"]
        monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
        settings = load_distributed_settings(redis_url_override=redis_url)
        client = RedisClientFactory(redis_url).create_async()
        await client.flushdb()
        broker = RedisBroker(client, settings)
        specs = [("eabot/cook", 538), ("eabot/chogori", 302), ("eabot/cook", 526)]
        records = []
        try:
            for index, (project_id, mr_iid) in enumerate(specs):
                mr_url = f"https://gitlab.example.com/{project_id}/-/merge_requests/{mr_iid}"
                binding = TriageCardBinding.new(
                    card_id=f"card-{index}",
                    task_id="",
                    open_message_id="",
                    receive_id="ou_owner",
                    mr_url=mr_url,
                    project_id=project_id,
                    mr_iid=mr_iid,
                    mr_title=f"MR {mr_iid}",
                    source_branch=f"feature/{mr_iid}",
                    pipeline_id=30_000 + index,
                    pipeline_sha=f"sha-{index}",
                    original_markdown=f"failed job {index}",
                )
                task = replace(
                    TaskEnvelope.new(
                        kind=TaskKind.PR_COMMAND,
                        source="feishu",
                        mr=MrKey(project_id, mr_iid),
                        pr_url=mr_url,
                        command="/triage",
                        payload={"sender_id": "ou_owner"},
                        idempotency_key=f"feishu-card:event-{index}",
                    ),
                    task_id=f"task-{index}",
                )
                await broker.save_triage_card(binding, ttl_seconds=2_592_000)
                result = await broker.enqueue_task_with_card(
                    task,
                    binding.card_id,
                    f"om_{index}",
                    ttl_seconds=2_592_000,
                    sender_id="ou_owner",
                )
                assert await broker.transition_triage_card(
                    result.task_id,
                    {TriageCardState.PIPELINE_FAILED},
                    TriageCardState.REPAIR_QUEUED,
                    "queued",
                )
                assert await broker.transition_triage_card(
                    result.task_id,
                    {TriageCardState.REPAIR_QUEUED},
                    TriageCardState.REPAIR_RUNNING,
                    "running",
                )
                records.append((result.task_id, binding))

            for index in (2, 0, 1):
                task_id, _ = records[index]
                assert await queue_triage_card_update(
                    broker,
                    task_id,
                    TriageCardState.REPAIR_SUCCEEDED,
                    "流水线通过",
                )

            observed = []
            for _ in records:
                delivery = await broker.read_notification("test-feishu", block_ms=10)
                assert delivery is not None
                stream_id, notification = delivery
                observed.append((notification.message_id, notification.title, notification.mr_url))
                await broker.ack_notification(stream_id)

            assert [item[0] for item in observed] == ["om_2", "om_0", "om_1"]
            for observed_item, index in zip(observed, (2, 0, 1), strict=True):
                _, binding = records[index]
                assert observed_item[1] == f"【{binding.project_id} !{binding.mr_iid}】修复成功"
                assert observed_item[2] == binding.mr_url
        finally:
            await client.flushdb()
            await client.aclose()

    asyncio.run(run_test())
