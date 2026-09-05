import asyncio
from collections import defaultdict
from dataclasses import replace

import pytest

from pr_agent.distributed.broker import LostLeaseError, StoredTask
from pr_agent.distributed.models import MrKey, PipelineEvent, TaskEnvelope, TaskKind, TaskStatus
from pr_agent.distributed.runtime import TaskSuspended
from pr_agent.distributed.session import MrSessionManager, is_triage_task


def make_task(task_id: str, mr: MrKey, command: str) -> TaskEnvelope:
    task = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="gitlab",
        mr=mr,
        pr_url=f"https://gitlab.example/mr/{mr.iid}",
        command=command,
        payload={},
        idempotency_key=f"note:{task_id}",
    )
    return replace(task, task_id=task_id)


def test_repair_pipeline_is_exclusive_triage_task():
    assert is_triage_task(make_task("repair", MrKey("eabot/cook", 536), "/repair-pipeline"))


class FakeBroker:
    def __init__(self, tasks):
        self.tasks = {
            task.task_id: StoredTask(task, TaskStatus.ASSIGNED, 0, "worker-1", 7, "", "") for task in tasks
        }
        self.renewed = []
        self.lost_mrs = set()

    async def get_task(self, task_id):
        return self.tasks[task_id]

    async def renew_mr(self, mr, worker_id, fencing_token, lease_seconds):
        self.renewed.append(mr.redis_id)
        return mr.redis_id not in self.lost_mrs and worker_id == "worker-1" and fencing_token == 7


class ControllableExecutor:
    def __init__(self):
        self.triage_started = asyncio.Event()
        self.finish_triage = asyncio.Event()
        self.normal_barrier = asyncio.Event()
        self.running = defaultdict(int)
        self.maximum = defaultdict(int)

    async def execute(self, task, lease):
        if task.command == "/triage":
            self.triage_started.set()
            await self.finish_triage.wait()
            return
        key = task.mr.redis_id
        self.running[key] += 1
        self.maximum[key] = max(self.maximum[key], self.running[key])
        self.normal_barrier.set()
        await asyncio.sleep(0)
        self.running[key] -= 1

    async def resume_pipeline(self, task, lease, event):
        self.finish_triage.set()


def test_triage_blocks_only_its_mr():
    async def run_test():
        mr1 = MrKey("eabot/cook", 536)
        mr2 = MrKey("eabot/map", 12)
        tasks = [
            make_task("triage", mr1, "/triage"),
            make_task("mr1-review", mr1, "/review"),
            make_task("mr2-review", mr2, "/review"),
        ]
        executor = ControllableExecutor()
        manager = MrSessionManager(FakeBroker(tasks), executor, "worker-1", lease_seconds=30)

        triage = asyncio.create_task(manager.submit(tasks[0]))
        await executor.triage_started.wait()
        mr1_review = asyncio.create_task(manager.submit(tasks[1]))
        mr2_review = asyncio.create_task(manager.submit(tasks[2]))
        await asyncio.wait_for(mr2_review, timeout=0.2)
        assert not mr1_review.done()
        executor.finish_triage.set()
        await asyncio.gather(triage, mr1_review)

    asyncio.run(run_test())


def test_normal_commands_same_mr_can_yield_to_each_other():
    async def run_test():
        mr = MrKey("eabot/cook", 536)
        tasks = [make_task("review", mr, "/review"), make_task("improve", mr, "/improve")]
        executor = ControllableExecutor()
        manager = MrSessionManager(FakeBroker(tasks), executor, "worker-1", lease_seconds=30)

        await asyncio.gather(*(manager.submit(task) for task in tasks))

        assert executor.maximum[mr.redis_id] == 2

    asyncio.run(run_test())


def test_new_normal_command_waits_once_triage_is_pending():
    class OrderedExecutor(ControllableExecutor):
        def __init__(self):
            super().__init__()
            self.first_normal_started = asyncio.Event()
            self.release_first_normal = asyncio.Event()
            self.second_normal_started = asyncio.Event()

        async def execute(self, task, lease):
            if task.command == "/triage":
                self.triage_started.set()
                await self.finish_triage.wait()
            elif task.command == "/review":
                self.first_normal_started.set()
                await self.release_first_normal.wait()
            else:
                self.second_normal_started.set()

    async def run_test():
        mr = MrKey("eabot/cook", 536)
        review = make_task("review", mr, "/review")
        triage = make_task("triage", mr, "/triage")
        improve = make_task("improve", mr, "/improve")
        executor = OrderedExecutor()
        manager = MrSessionManager(FakeBroker([review, triage, improve]), executor, "worker-1", lease_seconds=30)

        first = asyncio.create_task(manager.submit(review))
        await executor.first_normal_started.wait()
        priority = asyncio.create_task(manager.submit(triage))
        await asyncio.sleep(0)
        second = asyncio.create_task(manager.submit(improve))
        await asyncio.sleep(0)
        assert not executor.second_normal_started.is_set()

        executor.release_first_normal.set()
        await first
        await asyncio.wait_for(executor.triage_started.wait(), timeout=0.2)
        assert not executor.second_normal_started.is_set()
        executor.finish_triage.set()
        await priority
        await asyncio.wait_for(second, timeout=0.2)
        assert executor.second_normal_started.is_set()

    asyncio.run(run_test())


def test_suspended_triage_keeps_exclusive_until_resume():
    class SuspendedExecutor(ControllableExecutor):
        async def execute(self, task, lease):
            if task.command == "/triage":
                raise TaskSuspended(task.task_id, "pipeline", "sha-1")
            await super().execute(task, lease)

    async def run_test():
        mr = MrKey("eabot/cook", 536)
        triage = make_task("triage", mr, "/triage")
        review = make_task("review", mr, "/review")
        executor = SuspendedExecutor()
        manager = MrSessionManager(FakeBroker([triage, review]), executor, "worker-1", lease_seconds=30)

        with pytest.raises(TaskSuspended):
            await manager.submit(triage)
        blocked_review = asyncio.create_task(manager.submit(review))
        await asyncio.sleep(0)
        assert not blocked_review.done()
        await manager.resume_pipeline(
            triage,
            PipelineEvent.new(project_id=mr.project_id, pipeline_id=1, sha="sha-1", status="success", ref="main"),
        )
        await asyncio.wait_for(blocked_review, timeout=0.2)

    asyncio.run(run_test())


def test_lease_renewal_drops_lost_session_without_skipping_other_mrs():
    async def run_test():
        lost_mr = MrKey("eabot/chogori", 302)
        healthy_mr = MrKey("eabot/cook", 541)
        tasks = [make_task("lost", lost_mr, "/review"), make_task("healthy", healthy_mr, "/review")]
        broker = FakeBroker(tasks)
        manager = MrSessionManager(broker, ControllableExecutor(), "worker-1", lease_seconds=30)
        await asyncio.gather(*(manager.submit(task) for task in tasks))
        broker.lost_mrs.add(lost_mr.redis_id)

        with pytest.raises(LostLeaseError, match=lost_mr.redis_id):
            await manager.renew_leases()

        assert broker.renewed == [lost_mr.redis_id, healthy_mr.redis_id]
        assert manager.owned_mr_count == 1
        await manager.renew_leases()
        assert broker.renewed[-1] == healthy_mr.redis_id

    asyncio.run(run_test())
