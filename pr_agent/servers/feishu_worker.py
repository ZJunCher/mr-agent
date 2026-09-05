import asyncio
import signal
import sys
from contextlib import suppress
from threading import Thread

from pr_agent.distributed.broker import RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.ingress import QueueIngress
from pr_agent.distributed.metrics import DistributedMetrics
from pr_agent.distributed.notifications import NotificationConsumer
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.feishu.feishu_client import FeishuClient
from pr_agent.feishu.long_connection_worker import FeishuLongConnectionWorker
from pr_agent.log import LoggingFormat, get_logger, setup_logger


async def _heartbeat_loop(broker: RedisBroker, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await broker.heartbeat_service("feishu", ttl_seconds=20)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5)
        except TimeoutError:
            pass


async def _watch_thread(thread: Thread, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        if not thread.is_alive():
            raise RuntimeError("Feishu long-connection thread exited")
        await asyncio.sleep(1)


async def main() -> None:
    setup_logger(fmt=LoggingFormat.JSON)
    settings = load_distributed_settings()
    if settings.execution_mode != "queue":
        raise RuntimeError("Feishu worker requires PR_AGENT_EXECUTION_MODE=queue")

    factory = RedisClientFactory(settings.redis_url)
    async_client = factory.create_async()
    broker = RedisBroker(async_client, settings)
    queue_ingress = QueueIngress(broker, DistributedMetrics(async_client))
    consumer = NotificationConsumer(broker, FeishuClient(), settings)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stop_event.set)

    long_connection = FeishuLongConnectionWorker(loop=loop, queue_ingress=queue_ingress)
    ws_thread = Thread(target=long_connection.start, daemon=True, name="feishu-ws")
    ws_thread.start()
    tasks = [
        asyncio.create_task(consumer.run()),
        asyncio.create_task(_heartbeat_loop(broker, stop_event)),
        asyncio.create_task(_watch_thread(ws_thread, stop_event)),
    ]
    for task in tasks:
        task.add_done_callback(lambda finished: stop_event.set() if not finished.cancelled() else None)
    task_error = None
    try:
        await stop_event.wait()
    finally:
        consumer.stop_event.set()
        for task in tasks:
            if task.done() and not task.cancelled() and task.exception() is not None:
                task_error = task.exception()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await async_client.aclose()
        get_logger().info("Feishu worker stopped")
    if task_error is not None:
        raise task_error


async def healthcheck() -> bool:
    settings = load_distributed_settings()
    client = RedisClientFactory(settings.redis_url).create_async()
    try:
        await client.ping()
        heartbeat = await client.hgetall(RedisBroker(client, settings).keys.service_heartbeat("feishu"))
        return bool(heartbeat)
    finally:
        await client.aclose()


def start() -> None:
    if "--healthcheck" in sys.argv:
        raise SystemExit(0 if asyncio.run(healthcheck()) else 1)
    asyncio.run(main())


if __name__ == "__main__":
    start()
