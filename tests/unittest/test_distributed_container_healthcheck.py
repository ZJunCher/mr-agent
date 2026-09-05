from unittest.mock import MagicMock, patch

from redis.exceptions import ConnectionError

from pr_agent.distributed.container_healthcheck import redis_is_ready


def test_redis_is_ready_requires_url(monkeypatch):
    monkeypatch.delenv("PR_AGENT_REDIS_URL", raising=False)

    assert redis_is_ready() is False


def test_redis_is_ready_pings_and_closes(monkeypatch):
    monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://redis:6379/0")
    client = MagicMock()
    client.ping.return_value = True

    with patch("pr_agent.distributed.container_healthcheck.Redis.from_url", return_value=client) as from_url:
        assert redis_is_ready() is True

    from_url.assert_called_once_with("redis://redis:6379/0", socket_connect_timeout=2, socket_timeout=2)
    client.close.assert_called_once_with()


def test_redis_is_ready_handles_connection_failure(monkeypatch):
    monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://redis:6379/0")

    with patch(
        "pr_agent.distributed.container_healthcheck.Redis.from_url",
        side_effect=ConnectionError("unavailable"),
    ):
        assert redis_is_ready() is False
