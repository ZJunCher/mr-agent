"""Redis lease and monotonically increasing fencing token for Prompt evolution.

Acquire/renew/assert/release are atomic Lua scripts so two workers can never
both believe they hold the lease. ``assert_current()`` raises
``LostEvolutionLease`` on owner/token mismatch so the runner can fail closed
before any GitLab write. Release deletes only the current owner's lease and
never decrements the fence counter.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class EvolutionLease:
    scope: str
    owner: str
    fencing_token: int


class LostEvolutionLease(RuntimeError):
    """Raised when the caller no longer holds the lease."""


_ACQUIRE_LUA = """
local owner = redis.call('HGET', KEYS[1], 'owner')
if owner and owner ~= ARGV[1] then return {'', '0'} end
local token = redis.call('HGET', KEYS[1], 'fencing_token')
if not token then token = redis.call('INCR', KEYS[2]) end
redis.call('HSET', KEYS[1], 'owner', ARGV[1], 'fencing_token', token)
redis.call('EXPIRE', KEYS[1], ARGV[2])
return {ARGV[1], token}
"""

_RENEW_LUA = """
local owner = redis.call('HGET', KEYS[1], 'owner')
local token = redis.call('HGET', KEYS[1], 'fencing_token')
if owner ~= ARGV[1] or token ~= ARGV[2] then return 0 end
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""

_ASSERT_LUA = """
local owner = redis.call('HGET', KEYS[1], 'owner')
local token = redis.call('HGET', KEYS[1], 'fencing_token')
if owner ~= ARGV[1] or token ~= ARGV[2] then return 0 end
return 1
"""

_RELEASE_LUA = """
local owner = redis.call('HGET', KEYS[1], 'owner')
local token = redis.call('HGET', KEYS[1], 'fencing_token')
if owner ~= ARGV[1] or token ~= ARGV[2] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


class PromptEvolutionLeaseManager:
    def __init__(self, redis_client, prefix: str = "pr-agent") -> None:
        self.redis = redis_client
        self.prefix = prefix

    def _keys(self, scope: str) -> tuple[str, str]:
        encoded = quote(scope, safe="")
        return (
            f"{self.prefix}:prompt-evolution:{encoded}:lease",
            f"{self.prefix}:prompt-evolution:{encoded}:fence",
        )

    async def acquire(self, scope: str, owner: str, lease_seconds: int) -> EvolutionLease:
        lease_key, fence_key = self._keys(scope)
        result = await self.redis.eval(
            _ACQUIRE_LUA, 2, lease_key, fence_key, owner, str(lease_seconds)
        )
        result_owner, token = result[0], result[1]
        if not result_owner or not token or int(token) == 0:
            # Another worker holds the lease; surface the current owner's token.
            raise LostEvolutionLease(f"lease for {scope} held by another owner")
        return EvolutionLease(scope, owner, int(token))

    async def renew(self, lease: EvolutionLease, lease_seconds: int) -> bool:
        lease_key, _ = self._keys(lease.scope)
        result = await self.redis.eval(
            _RENEW_LUA, 1, lease_key, lease.owner, str(lease.fencing_token), str(lease_seconds)
        )
        return bool(int(result))

    async def assert_current(self, lease: EvolutionLease) -> None:
        lease_key, _ = self._keys(lease.scope)
        result = await self.redis.eval(
            _ASSERT_LUA, 1, lease_key, lease.owner, str(lease.fencing_token)
        )
        if not int(result):
            raise LostEvolutionLease(f"lease for {lease.scope} lost by {lease.owner}")

    async def release(self, lease: EvolutionLease) -> bool:
        lease_key, _ = self._keys(lease.scope)
        result = await self.redis.eval(
            _RELEASE_LUA, 1, lease_key, lease.owner, str(lease.fencing_token)
        )
        return bool(int(result))
