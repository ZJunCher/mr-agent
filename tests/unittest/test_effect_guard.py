import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent.distributed.broker import EffectRecord, LostLeaseError, MrLease
from pr_agent.distributed.effects import EffectGuard, IdempotentGitProvider
from pr_agent.distributed.models import MrKey
from pr_agent.distributed.runtime import ExecutionRuntime, TaskCanceled, execution_context


class MemoryEffectBroker:
    def __init__(self):
        self.effects = {}

    def claim_effect(self, key, _lease, metadata=None):
        return self.effects.setdefault(key, EffectRecord("started", metadata or {}))

    def update_effect_metadata(self, key, _lease, metadata):
        self.effects[key] = EffectRecord("started", metadata)
        return True

    def complete_effect(self, key, _lease, result):
        metadata = self.effects[key].metadata
        self.effects[key] = EffectRecord("completed", metadata, result)
        return True

    def assert_fence(self, _lease):
        return None

    def is_cancel_requested(self, _task_id):
        return False


def make_runtime(sync_broker=None):
    mr = MrKey("eabot/cook", 536)
    return ExecutionRuntime(
        "task-536",
        "worker-1",
        MrLease(mr, "worker-1", 7),
        "queue",
        AsyncMock(),
        sync_broker or MemoryEffectBroker(),
    )


def test_old_fence_cannot_publish_effect():
    async def run_test():
        runtime = make_runtime()
        runtime.broker.claim_effect.side_effect = LostLeaseError("task-536:final-comment")
        publish = AsyncMock()

        with execution_context(runtime), pytest.raises(LostLeaseError):
            await EffectGuard().run("final-comment", lambda _metadata: None, publish)

        publish.assert_not_awaited()

    asyncio.run(run_test())


def test_comment_retry_reconciles_marker_without_duplicate_publish():
    class Provider:
        max_comment_chars = 65000

        def __init__(self):
            self.comments = []

        def get_issue_comments(self):
            return self.comments

        def publish_comment(self, body, is_temporary=False):
            comment = SimpleNamespace(id=len(self.comments) + 1, body=body, is_temporary=is_temporary)
            self.comments.append(comment)
            return comment

    broker = MemoryEffectBroker()
    runtime = make_runtime(broker)
    provider = Provider()

    with execution_context(runtime):
        IdempotentGitProvider(provider).publish_comment("done")
        IdempotentGitProvider(provider).publish_comment("done")

    assert len(provider.comments) == 1
    assert "pr-agent-task:task-536" in provider.comments[0].body


def test_completed_effect_returns_saved_result_without_action():
    async def run_test():
        runtime = make_runtime()
        runtime.broker.claim_effect.return_value = EffectRecord("completed", {}, {"ok": True})
        action = Mock()

        with execution_context(runtime):
            result = await EffectGuard().run("final-comment", Mock(), action)

        assert result == {"ok": True}
        action.assert_not_called()

    asyncio.run(run_test())


def test_effect_refuses_to_start_after_cancel():
    sync_broker = Mock()
    sync_broker.is_cancel_requested.return_value = True
    runtime = make_runtime(sync_broker)

    with execution_context(runtime), pytest.raises(TaskCanceled):
        runtime.raise_if_canceled()
