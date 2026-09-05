import asyncio
import json
import sys

from pr_agent.distributed.broker import MrLease, RedisBroker, SyncRedisBroker
from pr_agent.distributed.checkpoint import CoreRedisCheckpointSaver
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.executor import TaskExecutor
from pr_agent.distributed.ingress import GitLabEventJobs
from pr_agent.distributed.models import PipelineEvent, TaskEnvelope
from pr_agent.distributed.process_executor import TASK_CANCELED_EXIT_CODE, TASK_SUSPENDED_EXIT_CODE
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.distributed.runtime import TaskCanceled, TaskSuspended
from pr_agent.log import LoggingFormat, get_logger, setup_logger


async def run_task(payload: dict) -> int:
    settings = load_distributed_settings()
    task = TaskEnvelope.from_dict(payload["task"])
    worker_id = str(payload["worker_id"])
    fencing_token = payload.get("fencing_token")
    if task.mr is not None and fencing_token is None:
        raise ValueError("MR task requires a fencing token")
    lease = MrLease(task.mr, worker_id, int(fencing_token)) if task.mr is not None else None
    pipeline_payload = payload.get("pipeline_event")
    pipeline_event = PipelineEvent.from_dict(pipeline_payload) if pipeline_payload else None

    factory = RedisClientFactory(settings.redis_url)
    async_client = factory.create_async()
    sync_client = factory.create_sync()
    broker = RedisBroker(async_client, settings)
    checkpointer = CoreRedisCheckpointSaver(sync_client, async_client)
    executor = TaskExecutor(
        broker,
        SyncRedisBroker(sync_client, settings),
        worker_id,
        max_active_tasks=1,
        checkpointer=checkpointer,
        webhook_jobs=GitLabEventJobs(),
    )
    exit_code = 0
    try:
        if pipeline_event is None:
            await executor.execute(task, lease)
        else:
            await executor.resume_pipeline(task, lease, pipeline_event)
    except TaskSuspended:
        exit_code = TASK_SUSPENDED_EXIT_CODE
    except TaskCanceled:
        exit_code = TASK_CANCELED_EXIT_CODE
    except Exception:
        get_logger().exception(f"Isolated task execution failed: task_id={task.task_id}")
        exit_code = 1
    try:
        await checkpointer.aclose()
    except Exception:
        get_logger().exception(f"Failed to close isolated task Redis clients: task_id={task.task_id}")
        if exit_code == 0:
            exit_code = 1
    return exit_code


def main() -> None:
    setup_logger(fmt=LoggingFormat.JSON)
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        exit_code = asyncio.run(run_task(payload))
    except Exception:
        get_logger().exception("Isolated task runner failed before execution")
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
