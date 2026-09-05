import asyncio
from unittest.mock import AsyncMock, MagicMock

from pr_agent.distributed.broker import RedisBroker, RedisKeys
from pr_agent.feishu.feishu_client import FeishuClient


def _patch_get_session(monkeypatch, *, status: int, payload: dict):
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.get.return_value = response
    monkeypatch.setattr("pr_agent.feishu.feishu_client.aiohttp.ClientSession", lambda: session)
    return session


def test_get_user_display_name_uses_open_id_contact_endpoint(monkeypatch):
    async def run_test():
        session = _patch_get_session(
            monkeypatch,
            status=200,
            payload={"code": 0, "data": {"user": {"name": "赵军"}}},
        )
        client = FeishuClient()
        client.get_tenant_access_token = AsyncMock(return_value="tenant-token")

        assert await client.get_user_display_name("ou/a") == "赵军"
        assert session.get.call_args.args[0].endswith("/contact/v3/users/ou%2Fa?user_id_type=open_id")

    asyncio.run(run_test())


def test_get_user_display_name_failure_returns_empty(monkeypatch):
    async def run_test():
        _patch_get_session(monkeypatch, status=403, payload={"code": 99991672, "msg": "forbidden"})
        client = FeishuClient()
        client.get_tenant_access_token = AsyncMock(return_value="tenant-token")

        assert await client.get_user_display_name("ou_actor") == ""

    asyncio.run(run_test())


def test_redis_feishu_name_cache_uses_ttl():
    async def run_test():
        redis = AsyncMock()
        redis.get.return_value = "赵军"
        broker = RedisBroker(redis, MagicMock())

        await broker.cache_feishu_user_name("ou/a", "赵军", 3600)

        redis.set.assert_awaited_once_with(
            RedisKeys().feishu_user_name("ou/a"),
            "赵军",
            ex=3600,
        )
        assert await broker.get_feishu_user_name("ou/a") == "赵军"

    asyncio.run(run_test())
