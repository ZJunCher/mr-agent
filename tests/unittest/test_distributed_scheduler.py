import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

from pr_agent.distributed.broker import MrLease, StoredTask, WorkerState
from pr_agent.distributed.models import IngressDelivery, MrKey, TaskEnvelope, TaskKind, TaskStatus
from pr_agent.distributed.scheduler import TaskScheduler


def make_task(task_id: str, mr: MrKey) -> TaskEnvelope:
    task = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="gitlab",
        mr=mr,
        pr_url=f"https://gitlab.example/mr/{mr.iid}",
        command="/review",
        payload={},
        idempotency_key=f"note:{task_id}",
    )
    return replace(task, task_id=task_id)


class FakeSchedulerBroker:
    def __init__(self, tasks, workers):
        self.tasks = {
            task.task_id: StoredTask(task, TaskStatus.QUEUED, 0, "", None, "", "") for task in tasks
        }
        self.workers = workers
        self.ingress = [(f"message-{index}", task.task_id) for index, task in enumerate(tasks)]
        self.leases = {}
        self.next_fence = 1
        self.acked = []
        self.dead_worker_tasks = {}
        self.revoked = []
        self.recovery_results = {}
        self.resumed_auto = []

    async def read_ingress_group(self, consumer, limit, block_ms):
        from pr_agent.distributed.models import IngressDelivery

        return [IngressDelivery(message_id, task_id) for message_id, task_id in self.ingress[:limit]]

    async def get_task(self, task_id):
        return self.tasks.get(task_id)

    async def list_live_workers(self):
        return self.workers

    async def get_mr_lease(self, mr):
        return self.leases.get(mr.redis_id)

    async def claim_mr(self, mr, worker_id, lease_seconds):
        lease = self.leases.get(mr.redis_id)
        if lease is None:
            lease = MrLease(mr, worker_id, self.next_fence)
            self.next_fence += 1
            self.leases[mr.redis_id] = lease
        return lease

    async def assign_to_worker(self, task, lease, worker_id):
        stored = self.tasks[task.task_id]
        self.tasks[task.task_id] = replace(
            stored,
            status=TaskStatus.ASSIGNED,
            worker_id=worker_id,
            fencing_token=lease.fencing_token if lease else None,
        )
        return True

    async def ack_ingress(self, message_id):
        self.acked.append(message_id)

    async def list_dead_worker_ids(self):
        return list(self.dead_worker_tasks)

    async def get_worker_task_ids(self, worker_id):
        return list(self.dead_worker_tasks.get(worker_id, []))

    async def recover_dead_worker_task(self, worker_id, task_id):
        tasks = self.dead_worker_tasks[worker_id]
        tasks.remove(task_id)
        result = self.recovery_results.get(task_id, "requeued")
        if result == "failed":
            self.tasks[task_id] = replace(self.tasks[task_id], status=TaskStatus.FAILED)
        return result

    async def revoke_mr_if_owner(self, lease):
        self.revoked.append(lease)
        return True

    async def resume_auto_after_triage(self, mr, **kwargs):
        self.resumed_auto.append((mr, kwargs))
        return True


def test_scheduler_routes_same_mr_to_same_worker():
    async def run_test():
        mr = MrKey("eabot/cook", 536)
        tasks = [make_task("task-1", mr), make_task("task-2", mr)]
        broker = FakeSchedulerBroker(
            tasks,
            [
                WorkerState("worker-1", 1.0, 1, 1, False),
                WorkerState("worker-2", 1.0, 0, 0, False),
            ],
        )
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        assert await scheduler.dispatch_available(limit=2) == 2
        assigned = [await broker.get_task(task.task_id) for task in tasks]
        assert assigned[0].worker_id == assigned[1].worker_id
        assert assigned[0].fencing_token == assigned[1].fencing_token

    asyncio.run(run_test())


def test_scheduler_selects_least_loaded_healthy_worker():
    async def run_test():
        task = make_task("task-1", MrKey("eabot/map", 1))
        broker = FakeSchedulerBroker(
            [task],
            [
                WorkerState("busy", 1.0, 3, 4, False),
                WorkerState("degraded", 1.0, 0, 0, True),
                WorkerState("idle", 1.0, 0, 1, False),
            ],
        )
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        await scheduler.dispatch_available(limit=1)

        assert (await broker.get_task(task.task_id)).worker_id == "idle"

    asyncio.run(run_test())


def test_scheduler_does_not_read_report_queue_when_normal_work_exists():
    async def run_test():
        broker = AsyncMock()
        normal = make_task("normal-1", MrKey("eabot/cook", 1))
        broker.read_ingress_group.return_value = [IngressDelivery("m-1", normal.task_id)]
        broker.get_task.return_value = StoredTask(normal, TaskStatus.QUEUED, 0, "", None, "", "")
        broker.list_live_workers.return_value = [WorkerState("worker-1", 1.0, 0, 0, False)]
        broker.get_mr_lease.return_value = None
        broker.claim_mr.return_value = MrLease(normal.mr, "worker-1", 1)
        broker.assign_to_worker.return_value = True
        scheduler = TaskScheduler(broker, "scheduler-1")

        assert await scheduler.dispatch_available(32) == 1
        broker.read_report_ingress_group.assert_not_awaited()

    asyncio.run(run_test())


def test_report_dispatch_uses_no_mr_lease_and_respects_report_capacity():
    async def run_test():
        report = replace(
            make_task("report-1", MrKey("eabot/cook", 1)),
            kind=TaskKind.REPAIR_REPORT,
            mr=None,
            command="/summarize-repair",
        )
        broker = AsyncMock()
        broker.settings.report_max_active_per_worker = 1
        broker.read_report_ingress_group.return_value = [IngressDelivery("r-1", report.task_id)]
        broker.get_task.return_value = StoredTask(report, TaskStatus.QUEUED, 0, "", None, "", "")
        broker.list_live_workers.return_value = [WorkerState("worker-1", 1.0, 0, 0, False, 0)]
        broker.assign_to_worker.return_value = True
        scheduler = TaskScheduler(broker, "scheduler-1")

        assert await scheduler.dispatch_reports() == 1
        broker.claim_mr.assert_not_awaited()
        broker.assign_to_worker.assert_awaited_once_with(report, None, "worker-1")

    asyncio.run(run_test())


def test_dead_worker_tasks_are_requeued_once():
    async def run_test():
        broker = FakeSchedulerBroker([], [])
        broker.dead_worker_tasks = {"worker-dead": ["task-1"]}
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        assert await scheduler.recover_dead_workers() == ["task-1"]
        assert await scheduler.recover_dead_workers() == []

    asyncio.run(run_test())


def test_requeued_running_task_releases_dead_mr_owner():
    async def run_test():
        mr = MrKey("eabot/cook", 536)
        task = make_task("task-1", mr)
        broker = FakeSchedulerBroker([task], [])
        broker.tasks[task.task_id] = replace(
            broker.tasks[task.task_id],
            status=TaskStatus.RUNNING,
            worker_id="worker-dead",
            fencing_token=7,
        )
        broker.dead_worker_tasks = {"worker-dead": [task.task_id]}
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        assert await scheduler.recover_dead_workers() == [task.task_id]
        assert broker.revoked == [MrLease(mr, "worker-dead", 7)]

    asyncio.run(run_test())


def test_paused_auto_releases_dead_mr_owner_without_requeueing_auto():
    async def run_test():
        mr = MrKey("eabot/cook", 536)
        task = make_task("auto-1", mr)
        broker = FakeSchedulerBroker([task], [])
        broker.tasks[task.task_id] = replace(
            broker.tasks[task.task_id],
            status=TaskStatus.PAUSED_BY_TRIAGE,
            worker_id="worker-dead",
            fencing_token=7,
        )
        broker.dead_worker_tasks = {"worker-dead": [task.task_id]}
        broker.recovery_results[task.task_id] = "paused_by_triage"
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        assert await scheduler.recover_dead_workers() == [task.task_id]
        assert broker.revoked == [MrLease(mr, "worker-dead", 7)]
        assert broker.tasks[task.task_id].status is TaskStatus.PAUSED_BY_TRIAGE

    asyncio.run(run_test())


def test_retry_exhaustion_publishes_terminal_failure(monkeypatch):
    async def run_test():
        mr = MrKey("eabot/cook", 536)
        task = replace(make_task("task-1", mr), source="feishu", command="/triage", payload={"sender_id": "ou_1"})
        broker = FakeSchedulerBroker([task], [WorkerState("worker-live", 1.0, 0, 0, False)])
        broker.tasks[task.task_id] = replace(
            broker.tasks[task.task_id],
            status=TaskStatus.RUNNING,
            worker_id="worker-dead",
            fencing_token=7,
        )
        broker.dead_worker_tasks = {"worker-dead": [task.task_id]}
        broker.recovery_results[task.task_id] = "failed"
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.scheduler.queue_triage_failure_notification", notify)
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        assert await scheduler.recover_dead_workers() == [task.task_id]
        notify.assert_awaited_once_with(
            broker,
            task,
            "worker 多次异常退出，已超过自动重试次数",
        )
        assert broker.resumed_auto == [
            (
                mr,
                {
                    "triage_task_id": task.task_id,
                    "worker_id": "worker-live",
                    "fencing_token": 1,
                },
            )
        ]

    asyncio.run(run_test())


def test_auto_workflow_retry_exhaustion_updates_review_without_triage_notification(monkeypatch):
    async def run_test():
        mr = MrKey("eabot/cook", 536)
        task = replace(make_task("auto-1", mr), kind=TaskKind.AUTO_WORKFLOW)
        broker = FakeSchedulerBroker([task], [])
        broker.tasks[task.task_id] = replace(
            broker.tasks[task.task_id],
            status=TaskStatus.RUNNING,
            attempt=1,
            worker_id="worker-dead",
            fencing_token=7,
        )
        broker.dead_worker_tasks = {"worker-dead": [task.task_id]}
        broker.recovery_results[task.task_id] = "failed"
        finish = Mock(return_value=True)
        event = Mock(return_value=True)
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "pr_agent.distributed.scheduler.get_review_run_for_task",
            lambda _task_id: {"run_id": "run-1", "improve_started_at": None},
        )
        monkeypatch.setattr("pr_agent.distributed.scheduler.finish_review_run", finish)
        monkeypatch.setattr("pr_agent.distributed.scheduler.record_review_event", event)
        monkeypatch.setattr("pr_agent.distributed.scheduler.queue_triage_failure_notification", notify)
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        assert await scheduler.recover_dead_workers() == [task.task_id]
        finish.assert_called_once_with(
            "failed",
            "run-1",
            stage="startup_failed",
            error_code="WorkerLost",
            error_message="Automatic workflow worker lost and retry limit was exhausted",
        )
        event.assert_called_once()
        notify.assert_not_awaited()
        assert broker.resumed_auto == []

    asyncio.run(run_test())


def test_terminal_pipeline_requeues_waiting_repair():
    async def run_test():
        mr = MrKey("eabot/cook", 536)
        envelope = replace(make_task("repair-1", mr), command="/repair-pipeline")
        waiting = StoredTask(
            envelope,
            TaskStatus.WAITING_PIPELINE,
            0,
            "worker-1",
            7,
            "",
            "",
        )
        broker = AsyncMock()
        broker.settings.repair_reconcile_seconds = 120
        broker.settings.task_retry_limit = 3
        broker.settings.queued_dispatch_seconds = 300
        broker.settings.running_orphan_seconds = 120
        broker.list_live_workers.return_value = []
        broker.list_active_repairs.return_value = [waiting]
        broker.resume_pipeline_if_cached.return_value = True
        broker.scan_repair_gates.return_value = (0, [])
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        assert await scheduler.reconcile_repairs() == [waiting.task_id]
        broker.resume_pipeline_if_cached.assert_awaited_once_with(waiting.task_id)

    asyncio.run(run_test())


def test_live_running_heartbeat_is_not_recovered():
    async def run_test():
        mr = MrKey("eabot/cook", 536)
        envelope = replace(make_task("repair-1", mr), command="/repair-pipeline")
        running = StoredTask(
            envelope,
            TaskStatus.RUNNING,
            0,
            "worker-1",
            7,
            "",
            "",
            updated_at=time.time(),
            heartbeat_at=time.time(),
        )
        broker = AsyncMock()
        broker.settings.task_retry_limit = 3
        broker.settings.queued_dispatch_seconds = 300
        broker.settings.running_orphan_seconds = 120
        broker.list_live_workers.return_value = [WorkerState("worker-1", time.time(), 1, 1, False)]
        broker.list_active_repairs.return_value = [running]
        broker.scan_repair_gates.return_value = (0, [])
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        assert await scheduler.reconcile_repairs() == []
        broker.transition_task.assert_not_awaited()

    asyncio.run(run_test())


def test_refreshed_heartbeat_wins_over_stale_scheduler_snapshot(monkeypatch):
    async def run_test():
        mr = MrKey("eabot/cook", 536)
        envelope = replace(make_task("repair-race", mr), source="feishu", command="/repair-pipeline")
        running = StoredTask(
            envelope,
            TaskStatus.RUNNING,
            0,
            "worker-1",
            7,
            "",
            "",
            updated_at=time.time() - 600,
            heartbeat_at=time.time() - 600,
        )
        broker = AsyncMock()
        broker.settings.task_retry_limit = 3
        broker.settings.queued_dispatch_seconds = 300
        broker.settings.running_orphan_seconds = 120
        broker.list_live_workers.return_value = [WorkerState("worker-1", time.time(), 1, 1, False)]
        broker.list_active_repairs.return_value = [running]
        broker.get_mr_lease.return_value = MrLease(mr, "worker-1", 7)
        broker.fail_stale_running_task.return_value = False
        broker.scan_repair_gates.return_value = (0, [])
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.scheduler.queue_triage_failure_notification", notify)
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        assert await scheduler.reconcile_repairs() == []
        broker.fail_stale_running_task.assert_awaited_once()
        notify.assert_not_awaited()

    asyncio.run(run_test())


def test_gate_scan_recovers_task_missing_from_active_index():
    async def run_test():
        mr = MrKey("eabot/cook", 530)
        broker = AsyncMock()
        broker.settings.task_retry_limit = 3
        broker.settings.queued_dispatch_seconds = 300
        broker.settings.running_orphan_seconds = 120
        broker.list_live_workers.return_value = []
        broker.list_active_repairs.return_value = []
        broker.scan_repair_gates.return_value = (17, [(mr, "ghost-task")])
        broker.reconcile_admission_gate.return_value = "recovered"
        scheduler = TaskScheduler(broker, "scheduler-1", mr_lease_seconds=30)

        assert await scheduler.reconcile_repairs() == ["ghost-task"]
        assert scheduler._repair_gate_cursor == 17
        broker.reconcile_admission_gate.assert_awaited_once_with(mr, "ghost-task")

    asyncio.run(run_test())
