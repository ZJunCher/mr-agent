import time

from fastapi import status
from fastapi.responses import JSONResponse

from pr_agent.distributed.config import DistributedSettings


class DistributedHealthService:
    def __init__(self, broker, settings: DistributedSettings) -> None:
        self.broker = broker
        self.settings = settings

    async def readiness(self) -> JSONResponse:
        try:
            await self.broker.redis.ping()
            return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok", "redis": "ok"})
        except Exception:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unavailable", "redis": "unavailable"},
            )

    async def snapshot(self) -> dict:
        try:
            await self.broker.redis.ping()
            workers = await self.broker.list_live_workers()
            feishu = await self.broker.get_service_heartbeat("feishu")
            queue = await self.broker.queue_depths()
            repairs = await self.broker.repair_health()
            triage_persistence = await self.broker.triage_persistence_health()
            persistence = await self._persistence_health()
        except Exception:
            return {
                "status": "unavailable",
                "redis": "unavailable",
                "agent_workers": {"expected": self.settings.agent_workers, "live": 0, "items": []},
                "feishu": {"alive": False, "last_seen_age_seconds": None},
                "queue": {},
                "repairs": {},
                "triage_persistence": {"status": "unavailable", "task_id": "", "updated_at": "", "error": ""},
            }
        worker_items = [
            {
                "worker_id": worker.worker_id,
                "active_tasks": worker.active_tasks,
                "owned_mrs": worker.owned_mrs,
                "degraded": worker.degraded,
                "last_seen_age_seconds": max(0.0, time.time() - worker.last_seen),
            }
            for worker in workers
        ]
        healthy_agents = len(workers) == self.settings.agent_workers and not any(worker.degraded for worker in workers)
        healthy = healthy_agents and bool(feishu.get("alive")) and persistence == "ok"
        return {
            "status": "ok" if healthy else "degraded",
            "redis": "ok",
            "redis_persistence": persistence,
            "agent_workers": {
                "expected": self.settings.agent_workers,
                "live": len(workers),
                "items": worker_items,
            },
            "feishu": feishu,
            "queue": queue,
            "repairs": repairs,
            "triage_persistence": triage_persistence,
        }

    async def _persistence_health(self) -> str:
        info = await self.broker.redis.info("persistence")
        if int(info.get("aof_enabled", 0)) != 1:
            return "degraded"
        if str(info.get("aof_last_write_status", "ok")) != "ok":
            return "degraded"
        return "ok"
