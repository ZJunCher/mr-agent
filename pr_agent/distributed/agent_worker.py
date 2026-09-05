import asyncio
import os
import signal
import sys
from contextlib import suppress
from socket import gethostname
from typing import Any

from pr_agent.distributed.broker import LostLeaseError, RedisBroker
from pr_agent.distributed.config import DistributedSettings, load_distributed_settings
from pr_agent.distributed.metrics import DistributedMetrics
from pr_agent.distributed.models import TERMINAL_TASK_STATUSES, DeliveryKind, InboxDelivery, PipelineEvent, TaskKind
from pr_agent.distributed.process_executor import ProcessTaskExecutor
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.distributed.runtime import TaskSuspended
from pr_agent.distributed.scheduler import TaskScheduler
from pr_agent.distributed.session import MrSessionManager
from pr_agent.log import LoggingFormat, get_logger, setup_logger


class AgentWorker:
    def __init__(
        self,
        worker_id: str,
        broker: RedisBroker,
        sessions: Any,
        settings: DistributedSettings,
        metrics: DistributedMetrics | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.broker = broker
        self.sessions = sessions
        self.settings = settings
        self.metrics = metrics
        self.stop_event = asyncio.Event()
        self.scheduler = TaskScheduler(broker, worker_id, stop_event=self.stop_event)
        self.active_deliveries: set[asyncio.Task] = set()
        self.active_report_deliveries: set[asyncio.Task] = set()
        self._degraded = False

    async def run(self) -> None:
        self._install_signal_handlers()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._supervise("heartbeat", self._heartbeat_loop))
            group.create_task(self._supervise("scheduler", self.scheduler.run))
            group.create_task(self._supervise("inbox", self._consume_inbox))
            group.create_task(self._supervise("pipeline-fallback", self._fallback_pipeline_scan))
            await self.stop_event.wait()
            self.scheduler.stop_event.set()
        await self._drain()

    async def _supervise(self, name: str, operation) -> None:
        while not self.stop_event.is_set():
            try:
                await operation()
                if not self.stop_event.is_set():
                    raise RuntimeError(f"{name} loop exited unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception:
                self._degraded = True
                get_logger().exception(f"Agent worker control loop failed and will restart: loop={name}")
                if await self._wait_for_stop(1):
                    return

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(signum, self.stop_event.set)

    async def _heartbeat_loop(self) -> None:
        interval = self.settings.worker_heartbeat_seconds
        expected_wakeup = asyncio.get_running_loop().time() + interval
        high_lag_count = 0
        normal_lag_count = 0
        while not self.stop_event.is_set():
            renew_leases = getattr(self.sessions, "renew_leases", None)
            if renew_leases is not None:
                try:
                    await renew_leases()
                except LostLeaseError:
                    self._degraded = True
                    get_logger().exception(f"Agent worker lost an MR lease: worker_id={self.worker_id}")
            await self.broker.heartbeat_worker(
                self.worker_id,
                active_tasks=len(self.active_deliveries),
                owned_mrs=getattr(self.sessions, "owned_mr_count", 0),
                degraded=self._degraded,
                active_report_tasks=len(self.active_report_deliveries),
            )
            if await self._wait_for_stop(interval):
                return
            now = asyncio.get_running_loop().time()
            lag = max(0.0, now - expected_wakeup)
            expected_wakeup = now + interval
            if self.metrics is not None:
                await self.metrics.observe_ms("worker_event_loop_lag_ms", lag * 1000)
            if lag > self.settings.worker_degraded_lag_seconds:
                high_lag_count += 1
                normal_lag_count = 0
            else:
                normal_lag_count += 1
                high_lag_count = 0
            if high_lag_count >= 2:
                self._degraded = True
            elif normal_lag_count >= 3:
                self._degraded = False

    async def _consume_inbox(self) -> None:
        while not self.stop_event.is_set():
            if len(self.active_deliveries) >= self.settings.worker_inbox_prefetch:
                await asyncio.sleep(0.05)
                continue
            delivery = await self.broker.read_worker_inbox(self.worker_id, block_ms=1000)
            if delivery is None:
                continue
            execution = asyncio.create_task(self._run_delivery(delivery))
            self.active_deliveries.add(execution)
            execution.add_done_callback(self.active_deliveries.discard)
            if delivery.task.kind is TaskKind.REPAIR_REPORT:
                self.active_report_deliveries.add(execution)
                execution.add_done_callback(self.active_report_deliveries.discard)

    async def _run_delivery(self, delivery: InboxDelivery) -> None:
        handled = False
        try:
            if delivery.kind is DeliveryKind.RESUME_PIPELINE:
                await self.sessions.resume_pipeline(delivery.task, PipelineEvent.from_dict(delivery.payload))
            elif delivery.kind is DeliveryKind.RESUME_AUTO:
                await self.sessions.resume_auto(delivery.task)
            else:
                await self.sessions.submit(delivery.task)
            handled = True
        except TaskSuspended:
            handled = True
        except Exception as error:
            get_logger().exception(f"Agent worker delivery failed: task_id={delivery.task.task_id}")
            try:
                await self.broker.record_delivery_failure(
                    delivery.task.task_id,
                    delivery.message_id,
                    str(error) or type(error).__name__,
                )
            except Exception:
                get_logger().exception(
                    f"Failed to record worker delivery failure: task_id={delivery.task.task_id}"
                )
        finally:
            try:
                task = await self.broker.get_task(delivery.task.task_id)
                if handled or (task and task.status in TERMINAL_TASK_STATUSES):
                    await self.broker.ack_worker_inbox(self.worker_id, delivery.message_id)
            except Exception:
                get_logger().exception(
                    f"Failed to finalize worker inbox delivery: task_id={delivery.task.task_id}"
                )

    async def _fallback_pipeline_scan(self) -> None:
        while not self.stop_event.is_set():
            if await self._wait_for_stop(self.settings.pipeline_fallback_scan_seconds):
                return
            scan = getattr(self.sessions, "fallback_pipeline_scan", None)
            if scan is not None:
                await scan()

    async def _wait_for_stop(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def _drain(self) -> None:
        if not self.active_deliveries:
            return
        done, pending = await asyncio.wait(
            self.active_deliveries,
            timeout=self.settings.worker_shutdown_grace_seconds,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        get_logger().info(f"Agent worker drained: completed={len(done)}, canceled={len(pending)}")


def worker_id() -> str:
    return (os.getenv("PR_AGENT_WORKER_ID") or gethostname()).strip()


async def healthcheck() -> bool:
    settings = load_distributed_settings()
    client = RedisClientFactory(settings.redis_url).create_async()
    try:
        await client.ping()
        value = await client.hgetall(RedisBroker(client, settings).keys.worker(worker_id()))
        return bool(value)
    finally:
        await client.aclose()


async def main() -> None:
    setup_logger(fmt=LoggingFormat.JSON)
    settings = load_distributed_settings()
    if settings.execution_mode != "queue":
        raise RuntimeError("Agent worker requires PR_AGENT_EXECUTION_MODE=queue")
    factory = RedisClientFactory(settings.redis_url)
    async_client = factory.create_async()
    broker = RedisBroker(async_client, settings)
    executor = ProcessTaskExecutor(
        broker,
        worker_id(),
        max_active_tasks=settings.worker_max_active_tasks,
        max_active_report_tasks=settings.report_max_active_per_worker,
    )
    sessions = MrSessionManager(
        broker,
        executor,
        worker_id(),
        lease_seconds=settings.mr_lease_seconds,
    )
    worker = AgentWorker(worker_id(), broker, sessions, settings, DistributedMetrics(async_client))
    try:
        await async_client.ping()
        await worker.run()
    finally:
        await async_client.aclose()


def start() -> None:
    if "--healthcheck" in sys.argv:
        raise SystemExit(0 if asyncio.run(healthcheck()) else 1)
    asyncio.run(main())


if __name__ == "__main__":
    start()
