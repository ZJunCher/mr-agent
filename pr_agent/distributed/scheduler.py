import asyncio
import time
from dataclasses import dataclass

from pr_agent.distributed.broker import MrLease, RedisBroker, StoredTask, WorkerState
from pr_agent.distributed.models import TaskKind, TaskStatus
from pr_agent.distributed.notifications import (
    queue_repair_canceled_notification,
    queue_triage_failure_notification,
)
from pr_agent.suggestions.review_tracking import (
    finish_review_run,
    get_review_run_for_task,
    record_review_event,
)


@dataclass(frozen=True)
class WorkerSelection:
    worker: WorkerState
    lease: MrLease | None


class TaskScheduler:
    def __init__(
        self,
        broker: RedisBroker,
        scheduler_id: str,
        *,
        mr_lease_seconds: int | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.broker = broker
        self.scheduler_id = scheduler_id
        self.mr_lease_seconds = mr_lease_seconds or broker.settings.mr_lease_seconds
        self.stop_event = stop_event or asyncio.Event()
        self._last_reconcile_at = 0.0
        self._repair_gate_cursor = 0
        self._report_reservations: dict[str, str] = {}

    async def run(self) -> None:
        while not self.stop_event.is_set():
            if await self.broker.acquire_scheduler_leader(self.scheduler_id, ttl_seconds=10):
                await self.dispatch_available(limit=32)
                await self.recover_dead_workers()
                if time.monotonic() - self._last_reconcile_at >= self.broker.settings.repair_reconcile_seconds:
                    await self.reconcile_repairs()
                    self._last_reconcile_at = time.monotonic()
            else:
                await asyncio.sleep(1)

    async def dispatch_available(self, limit: int) -> int:
        deliveries = await self.broker.read_ingress_group(self.scheduler_id, limit=limit, block_ms=1000)
        dispatched = 0
        for delivery in deliveries:
            task = await self.broker.get_task(delivery.task_id)
            if task is None or task.status is not TaskStatus.QUEUED:
                await self.broker.ack_ingress(delivery.message_id)
                continue
            selection = await self._owner_or_least_loaded_worker(task)
            if selection is None:
                continue
            await self.broker.assign_to_worker(task.envelope, selection.lease, selection.worker.worker_id)
            await self.broker.ack_ingress(delivery.message_id)
            dispatched += 1
        if not deliveries:
            dispatched += await self.dispatch_reports(limit=1)
        return dispatched

    async def dispatch_reports(self, limit: int = 1) -> int:
        await self._refresh_report_reservations()
        deliveries = await self.broker.read_report_ingress_group(
            self.scheduler_id,
            limit=max(1, min(limit, 1)),
            block_ms=0,
        )
        dispatched = 0
        for delivery in deliveries:
            task = await self.broker.get_task(delivery.task_id)
            if task is None or task.status is not TaskStatus.QUEUED:
                await self.broker.ack_report_ingress(delivery.message_id)
                continue
            workers = [worker for worker in await self.broker.list_live_workers() if not worker.degraded]
            reserved = {worker_id: 0 for worker_id in (worker.worker_id for worker in workers)}
            for worker_id in self._report_reservations.values():
                reserved[worker_id] = reserved.get(worker_id, 0) + 1
            candidates = [
                worker
                for worker in workers
                if worker.active_report_tasks + reserved.get(worker.worker_id, 0)
                < self.broker.settings.report_max_active_per_worker
            ]
            if not candidates:
                continue
            worker = min(candidates, key=lambda item: (item.active_report_tasks, item.active_tasks, item.worker_id))
            if await self.broker.assign_to_worker(task.envelope, None, worker.worker_id):
                self._report_reservations[task.task_id] = worker.worker_id
                await self.broker.ack_report_ingress(delivery.message_id)
                dispatched += 1
        return dispatched

    async def _refresh_report_reservations(self) -> None:
        for task_id in tuple(self._report_reservations):
            task = await self.broker.get_task(task_id)
            if task is None or task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED}:
                self._report_reservations.pop(task_id, None)

    async def _owner_or_least_loaded_worker(self, task: StoredTask) -> WorkerSelection | None:
        workers = [worker for worker in await self.broker.list_live_workers() if not worker.degraded]
        if not workers:
            return None
        workers_by_id = {worker.worker_id: worker for worker in workers}

        if task.mr is None:
            return WorkerSelection(worker=self._least_loaded(workers), lease=None)

        current_lease = await self.broker.get_mr_lease(task.mr)
        if current_lease is not None:
            owner = workers_by_id.get(current_lease.worker_id)
            return WorkerSelection(owner, current_lease) if owner else None

        candidate = self._least_loaded(workers)
        lease = await self.broker.claim_mr(task.mr, candidate.worker_id, self.mr_lease_seconds)
        owner = workers_by_id.get(lease.worker_id)
        return WorkerSelection(owner, lease) if owner else None

    @staticmethod
    def _least_loaded(workers: list[WorkerState]) -> WorkerState:
        return min(workers, key=lambda worker: (worker.active_tasks, worker.owned_mrs, worker.worker_id))

    async def recover_dead_workers(self) -> list[str]:
        recovered: list[str] = []
        for worker_id in await self.broker.list_dead_worker_ids():
            for task_id in await self.broker.get_worker_task_ids(worker_id):
                task = await self.broker.get_task(task_id)
                result = await self.broker.recover_dead_worker_task(worker_id, task_id)
                if result in {"requeued", "failed"}:
                    if task is not None and task.mr is not None and task.fencing_token is not None:
                        await self.broker.revoke_mr_if_owner(
                            MrLease(task.mr, worker_id, task.fencing_token)
                        )
                    recovered.append(task_id)
                    if task is not None and task.envelope.kind is TaskKind.AUTO_WORKFLOW:
                        self._persist_auto_workflow_recovery(task, result)
                        continue
                    if result == "failed" and task is not None:
                        if task.envelope.kind is TaskKind.REPAIR_REPORT:
                            await self._complete_failed_report(task, "worker 多次异常退出，报告已使用 Diff 兜底")
                            continue
                        if task.envelope.kind is TaskKind.REPAIR_ROLLBACK:
                            await self._complete_failed_rollback(task, None, "worker 多次异常退出，已超过自动重试次数")
                        await self._resume_auto_after_failed_triage(task)
                        await queue_triage_failure_notification(
                            self.broker,
                            task.envelope,
                            "worker 多次异常退出，已超过自动重试次数",
                        )
                        await self._persist_repair_terminal(
                            task,
                            error="worker 多次异常退出，已超过自动重试次数",
                        )
                elif result in {"waiting_pipeline", "publishing"}:
                    if await self._transfer_orphaned_task(worker_id, task_id, result):
                        recovered.append(task_id)
                elif result == "paused_by_triage":
                    if task is not None and task.mr is not None and task.fencing_token is not None:
                        await self.broker.revoke_mr_if_owner(
                            MrLease(task.mr, worker_id, task.fencing_token)
                        )
                    recovered.append(task_id)
        return recovered

    @staticmethod
    def _persist_auto_workflow_recovery(task: StoredTask, result: str) -> None:
        run = get_review_run_for_task(task.task_id)
        run_id = str(run.get("run_id") or "")
        if not run_id:
            return
        if result == "requeued":
            retry_count = task.attempt + 1
            record_review_event(
                run_id,
                f"workflow_requeued:{retry_count}",
                str(run.get("stage") or "workflow_started"),
                error_code="WorkerLost",
                error_message="Automatic workflow worker lost; retrying",
                details={"retry_count": retry_count},
            )
            return
        failure_stage = "execution_failed" if run.get("improve_started_at") else "startup_failed"
        message = "Automatic workflow worker lost and retry limit was exhausted"
        finish_review_run(
            "failed",
            run_id,
            stage=failure_stage,
            error_code="WorkerLost",
            error_message=message,
        )
        record_review_event(
            run_id,
            "worker_retry_exhausted",
            failure_stage,
            status="failed",
            error_code="WorkerLost",
            error_message=message,
            details={"retry_count": task.attempt},
        )

    async def _complete_failed_report(self, task: StoredTask, message: str) -> bool:
        from datetime import datetime, timezone

        from pr_agent.triage.final_repair_report import (
            FinalRepairReportState,
            RepairReportStatus,
            build_diff_fallback,
        )

        repair_task_id = str(task.envelope.payload.get("repair_task_id") or "")
        value = await self.broker.get_final_repair_report_input(repair_task_id)
        now = datetime.now(timezone.utc).isoformat()
        state = FinalRepairReportState(
            RepairReportStatus.FALLBACK,
            report_task_id=task.task_id,
            input_digest=value.digest() if value is not None else "",
            report=build_diff_fallback(value, message) if value is not None else None,
            failure_reason=message,
            created_at=(task.final_repair_report_state.created_at if task.final_repair_report_state else now),
            updated_at=now,
        )
        completed = await self.broker.complete_final_repair_report(task.envelope, value, state)
        if completed:
            from pr_agent.triage.terminal import persist_repair_terminal

            await persist_repair_terminal(self.broker, repair_task_id)
        return completed

    async def reconcile_repairs(self, limit: int = 32) -> list[str]:
        repaired: list[str] = []
        now = time.time()
        live_worker_ids = {worker.worker_id for worker in await self.broker.list_live_workers()}
        for task in await self.broker.list_active_repairs(limit=limit):
            if task.cancel_requested:
                if await queue_repair_canceled_notification(
                    self.broker,
                    task.envelope,
                    "修复已取消。已提交的代码不会自动回退。",
                ):
                    repaired.append(task.task_id)
                continue
            if task.delivery_attempt >= self.broker.settings.task_retry_limit:
                if task.mr is None:
                    continue
                lease = await self.broker.get_mr_lease(task.mr)
                if task.envelope.kind is TaskKind.REPAIR_ROLLBACK and lease is not None:
                    if await self._complete_failed_rollback(task, lease, "任务恢复消息连续处理失败，已转为人工处理"):
                        await self._resume_auto_after_failed_triage(task)
                        repaired.append(task.task_id)
                    continue
                if (
                    lease is not None
                    and lease.worker_id == task.worker_id
                    and lease.fencing_token == task.fencing_token
                    and await self.broker.transition_task(
                        task.task_id,
                        {TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.WAITING_PIPELINE},
                        TaskStatus.FAILED,
                        lease,
                        {"error": "任务恢复消息连续处理失败，已转为人工处理"},
                    )
                ):
                    await queue_triage_failure_notification(
                        self.broker,
                        task.envelope,
                        "任务恢复消息连续处理失败，已转为人工处理",
                    )
                    await self._persist_repair_terminal(
                        task,
                        error="任务恢复消息连续处理失败，已转为人工处理",
                    )
                    await self._resume_auto_after_failed_triage(task)
                    repaired.append(task.task_id)
                continue
            if task.status is TaskStatus.WAITING_PIPELINE:
                if await self.broker.resume_pipeline_if_cached(task.task_id):
                    repaired.append(task.task_id)
                continue
            state_age = max(0.0, now - (task.updated_at or task.created_at or now))
            if task.status is TaskStatus.QUEUED and state_age >= self.broker.settings.queued_dispatch_seconds:
                if await self.broker.requeue_stale_repair(
                    task,
                    self.broker.settings.queued_dispatch_seconds,
                ):
                    repaired.append(task.task_id)
                continue
            if task.status in {TaskStatus.RUNNING, TaskStatus.PUBLISHING}:
                heartbeat_age = max(0.0, now - (task.heartbeat_at or task.updated_at or task.created_at or now))
                if (
                    heartbeat_age >= self.broker.settings.running_orphan_seconds
                    and task.worker_id in live_worker_ids
                    and task.mr is not None
                ):
                    lease = await self.broker.get_mr_lease(task.mr)
                    if task.envelope.kind is TaskKind.REPAIR_ROLLBACK and lease is not None:
                        if await self._complete_failed_rollback(task, lease, "撤回子进程心跳超时，任务已自动结束"):
                            await self._resume_auto_after_failed_triage(task)
                            repaired.append(task.task_id)
                        continue
                    if (
                        lease is not None
                        and lease.worker_id == task.worker_id
                        and lease.fencing_token == task.fencing_token
                        and await self.broker.fail_stale_running_task(
                            task.task_id,
                            lease,
                            now - self.broker.settings.running_orphan_seconds,
                            "修复子进程心跳超时，任务已自动结束",
                        )
                    ):
                        await queue_triage_failure_notification(
                            self.broker,
                            task.envelope,
                            "修复子进程心跳超时，任务已自动结束",
                        )
                        await self._persist_repair_terminal(
                            task,
                            error="修复子进程心跳超时，任务已自动结束",
                        )
                        await self._resume_auto_after_failed_triage(task)
                        repaired.append(task.task_id)
        self._repair_gate_cursor, gates = await self.broker.scan_repair_gates(
            self._repair_gate_cursor,
            limit,
        )
        for mr, task_id in gates:
            outcome = await self.broker.reconcile_admission_gate(mr, task_id)
            if outcome != "healthy" and task_id not in repaired:
                repaired.append(task_id)
        return repaired

    async def _persist_repair_terminal(self, task: StoredTask, *, error: str = "") -> None:
        command = task.envelope.command.strip().split(maxsplit=1)
        if not command or command[0].lower() not in {"/triage", "/fix-format", "/fix_format", "/repair-pipeline"}:
            return
        from pr_agent.triage.terminal import persist_repair_terminal

        await persist_repair_terminal(self.broker, task.task_id, error=error)

    async def _complete_failed_rollback(
        self,
        task: StoredTask,
        lease: MrLease | None,
        message: str,
    ) -> bool:
        from datetime import datetime, timezone

        from pr_agent.triage.repair_rollback import (
            RepairRollbackState,
            RepairRollbackStatus,
            RollbackFailureCode,
        )

        current = task.repair_rollback_state
        state = RepairRollbackState(
            rollback_task_id=task.task_id,
            repair_task_id=str(task.envelope.payload.get("repair_task_id") or ""),
            status=RepairRollbackStatus.FAILED,
            trigger=str(task.envelope.payload.get("trigger") or "post_repair"),
            requested_by=str(task.envelope.payload.get("requested_by") or ""),
            expected_remote_head=current.expected_remote_head if current else "",
            manifest_digest=str(task.envelope.payload.get("manifest_digest") or ""),
            failure_code=RollbackFailureCode.INFRASTRUCTURE_ERROR,
            failure_message=message,
            retryable=True,
            created_at=current.created_at if current else "",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        return await self.broker.complete_repair_rollback(task.envelope, lease, state)

    async def _resume_auto_after_failed_triage(self, task: StoredTask) -> None:
        command = task.envelope.command.strip().split(maxsplit=1)
        if task.mr is None or not command or command[0].lower() not in {
            "/triage",
            "/fix-format",
            "/fix_format",
            "/repair-pipeline",
            "/rollback-repair",
        }:
            return
        workers = [worker for worker in await self.broker.list_live_workers() if not worker.degraded]
        if not workers:
            return
        candidate = self._least_loaded(workers)
        lease = await self.broker.claim_mr(task.mr, candidate.worker_id, self.mr_lease_seconds)
        await self.broker.resume_auto_after_triage(
            task.mr,
            triage_task_id=task.task_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
        )

    async def _transfer_orphaned_task(self, old_worker_id: str, task_id: str, status: str) -> bool:
        task = await self.broker.get_task(task_id)
        if task is None or task.mr is None or task.fencing_token is None:
            return False
        old_lease = MrLease(task.mr, old_worker_id, task.fencing_token)
        if not await self.broker.revoke_mr_if_owner(old_lease):
            return False
        workers = [worker for worker in await self.broker.list_live_workers() if not worker.degraded]
        if not workers:
            return False
        candidate = self._least_loaded(workers)
        new_lease = await self.broker.claim_mr(task.mr, candidate.worker_id, self.mr_lease_seconds)
        event = None
        if status == "waiting_pipeline" and task.pipeline_project_id and task.pipeline_sha:
            event = await self.broker.get_cached_pipeline_event(
                task.pipeline_project_id,
                task.pipeline_sha,
                task.pipeline_id,
            )
        return await self.broker.transfer_orphaned_task(task, old_worker_id, new_lease, event)
