import asyncio

import pytest
from unittest.mock import AsyncMock

from pr_agent.suggestions.prompt_evolution.lease import (
    EvolutionLease,
    LostEvolutionLease,
    PromptEvolutionLeaseManager,
)


def test_new_owner_gets_higher_fencing_token():
    async def run():
        redis = AsyncMock()
        redis.eval.side_effect = [["worker-a", "7"], ["worker-b", "8"]]
        manager = PromptEvolutionLeaseManager(redis, prefix="pr-agent")
        first = await manager.acquire("group/pr-agent", "worker-a", 120)
        second = await manager.acquire("group/pr-agent", "worker-b", 120)
        assert first.fencing_token == 7
        assert second.fencing_token == 8
    asyncio.run(run())


def test_stale_owner_fails_fence_assertion():
    async def run():
        redis = AsyncMock()
        redis.eval.return_value = 0
        manager = PromptEvolutionLeaseManager(redis)
        with pytest.raises(LostEvolutionLease):
            await manager.assert_current(EvolutionLease("scope", "old", 4))
    asyncio.run(run())


def test_renew_extends_lease_for_current_owner():
    async def run():
        redis = AsyncMock()
        redis.eval.return_value = 1
        manager = PromptEvolutionLeaseManager(redis)
        lease = EvolutionLease("group/pr-agent", "worker-a", 7)
        result = await manager.renew(lease, 120)
        assert result is True
    asyncio.run(run())


def test_release_deletes_only_current_owner_lease():
    async def run():
        redis = AsyncMock()
        redis.eval.return_value = 1
        manager = PromptEvolutionLeaseManager(redis)
        lease = EvolutionLease("group/pr-agent", "worker-a", 7)
        await manager.release(lease)
        assert redis.eval.await_count == 1
    asyncio.run(run())
