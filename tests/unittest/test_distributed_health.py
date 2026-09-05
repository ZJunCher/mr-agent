import asyncio
import json
import time
from unittest.mock import AsyncMock

import redis

from pr_agent.distributed.broker import WorkerState
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.health import DistributedHealthService


def make_service(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
    settings = load_distributed_settings(redis_url_override="redis://pr_agent:secret@redis:6379/0")
    broker = AsyncMock()
    broker.redis.info.return_value = {"aof_enabled": 1, "aof_last_write_status": "ok"}
    broker.get_service_heartbeat.return_value = {"alive": True, "last_seen_age_seconds": 1.0}
    broker.queue_depths.return_value = {"ingress": 0, "agent_inboxes": 0, "notifications": 0}
    broker.repair_health.return_value = {
        "active": 0,
        "status_counts": {},
        "oldest_state_seconds": {},
        "cancel_requested": 0,
        "mr_gate_mismatches": 0,
    }
    broker.triage_persistence_health.return_value = {
        "status": "ok",
        "task_id": "task-1",
        "updated_at": "1710000000.0",
        "error": "",
    }
    return DistributedHealthService(broker, settings), broker


def test_queue_readiness_fails_when_redis_is_down(monkeypatch):
    async def run_test():
        service, broker = make_service(monkeypatch)
        broker.redis.ping.side_effect = redis.ConnectionError("down")

        result = await service.readiness()

        assert result.status_code == 503
        assert b"secret" not in result.body

    asyncio.run(run_test())


def test_distributed_health_requires_three_live_agents(monkeypatch):
    async def run_test():
        service, broker = make_service(monkeypatch)
        broker.list_live_workers.return_value = [
            WorkerState("agent-1", time.time(), 1, 1, False),
            WorkerState("agent-2", time.time(), 0, 1, False),
        ]

        snapshot = await service.snapshot()

        assert snapshot["status"] == "degraded"
        assert snapshot["agent_workers"]["expected"] == 3
        assert snapshot["agent_workers"]["live"] == 2
        assert "secret" not in json.dumps(snapshot)

    asyncio.run(run_test())


def test_distributed_health_is_ok_with_three_agents_and_feishu(monkeypatch):
    async def run_test():
        service, broker = make_service(monkeypatch)
        broker.list_live_workers.return_value = [
            WorkerState(f"agent-{index}", time.time(), 0, 1, False) for index in range(1, 4)
        ]

        snapshot = await service.snapshot()

        assert snapshot["status"] == "ok"
        assert snapshot["redis_persistence"] == "ok"
        assert snapshot["repairs"]["active"] == 0
        assert snapshot["triage_persistence"]["status"] == "ok"

    asyncio.run(run_test())
