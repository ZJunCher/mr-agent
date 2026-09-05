import asyncio
from unittest.mock import AsyncMock, Mock

from pr_agent.distributed.agent_worker import AgentWorker
from pr_agent.distributed.models import DeliveryKind, InboxDelivery, MrKey, PipelineEvent, TaskEnvelope, TaskKind


def _delivery() -> InboxDelivery:
    task = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="gitlab",
        mr=MrKey("eabot/cook", 536),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        command="/repair-pipeline",
        payload={},
        idempotency_key="worker-delivery:536",
    )
    event = PipelineEvent.new(
        project_id="eabot/cook",
        pipeline_id=30100,
        sha="abc123",
        status="failed",
        ref="feature/test",
    )
    return InboxDelivery("1710000000000-0", task, DeliveryKind.RESUME_PIPELINE, event.to_dict())


def test_unexpected_resume_error_is_not_acked():
    async def run_test():
        broker = AsyncMock()
        sessions = Mock()
        sessions.resume_pipeline = AsyncMock(side_effect=RuntimeError("session conflict"))
        settings = Mock()
        worker = AgentWorker("worker-1", broker, sessions, settings)
        delivery = _delivery()

        await worker._run_delivery(delivery)

        broker.ack_worker_inbox.assert_not_awaited()
        broker.record_delivery_failure.assert_awaited_once_with(
            delivery.task.task_id,
            delivery.message_id,
            "session conflict",
        )

    asyncio.run(run_test())
