import asyncio
import json
import os
import signal
import sys
import time

from pr_agent.distributed.broker import LostLeaseError, MrLease, RedisBroker
from pr_agent.distributed.models import PipelineEvent, TaskEnvelope, TaskKind, TaskStatus
from pr_agent.distributed.notifications import queue_triage_failure_notification
from pr_agent.distributed.runtime import TaskSuspended, resolve_repair_manifest_base_tree
from pr_agent.log import get_logger

TASK_SUSPENDED_EXIT_CODE = 75
TASK_CANCELED_EXIT_CODE = 76


class ProcessTaskExecutor:
    """Run each heavy PR-Agent task in a child process so the worker control loop stays responsive."""

    def __init__(
        self,
        broker: RedisBroker,
        worker_id: str,
        *,
        max_active_tasks: int,
        max_active_report_tasks: int = 1,
        cancel_poll_seconds: float = 1.0,
        task_heartbeat_seconds: float | None = None,
        effect_cancel_grace_seconds: float = 30.0,
    ) -> None:
        self.broker = broker
        self.worker_id = worker_id
        self.active_task_slots = asyncio.Semaphore(max_active_tasks)
        self.active_report_slots = asyncio.Semaphore(max_active_report_tasks)
        self.cancel_poll_seconds = cancel_poll_seconds
        configured_heartbeat = getattr(broker.settings, "task_heartbeat_seconds", 15)
        self.task_heartbeat_seconds = (
            float(task_heartbeat_seconds)
            if task_heartbeat_seconds is not None
            else float(configured_heartbeat) if isinstance(configured_heartbeat, (int, float)) else 15.0
        )
        self.effect_cancel_grace_seconds = effect_cancel_grace_seconds

    async def execute(self, task: TaskEnvelope, lease: MrLease | None) -> None:
        await self._execute(task, lease)

    async def resume_pipeline(self, task: TaskEnvelope, lease: MrLease | None, event: PipelineEvent) -> None:
        await self._execute(task, lease, event)

    async def _execute(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        pipeline_event: PipelineEvent | None = None,
    ) -> None:
        payload = {
            "task": task.to_dict(),
            "worker_id": self.worker_id,
            "fencing_token": lease.fencing_token if lease else None,
            "pipeline_event": pipeline_event.to_dict() if pipeline_event else None,
        }
        slots = self.active_report_slots if task.kind is TaskKind.REPAIR_REPORT else self.active_task_slots
        async with slots:
            process = None
            communication = None
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "pr_agent.distributed.task_runner",
                    stdin=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                communication = asyncio.create_task(
                    process.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                )
                await self.broker.heartbeat_task(
                    task.task_id,
                    self.worker_id,
                    lease.fencing_token if lease else None,
                )
                next_heartbeat = time.monotonic() + self.task_heartbeat_seconds
                cancel_deferred_at = None
                while not communication.done():
                    done, _ = await asyncio.wait(
                        {communication},
                        timeout=self.cancel_poll_seconds,
                    )
                    if done:
                        break
                    if time.monotonic() >= next_heartbeat:
                        await self.broker.heartbeat_task(
                            task.task_id,
                            self.worker_id,
                            lease.fencing_token if lease else None,
                        )
                        next_heartbeat = time.monotonic() + self.task_heartbeat_seconds
                    if await self.broker.is_cancel_requested(task.task_id) is True:
                        if await self.broker.has_inflight_effect(task.task_id) is True:
                            cancel_deferred_at = cancel_deferred_at or time.monotonic()
                            if time.monotonic() - cancel_deferred_at < self.effect_cancel_grace_seconds:
                                continue
                        await self._terminate(process)
                        if not communication.done():
                            communication.cancel()
                        await asyncio.gather(communication, return_exceptions=True)
                        await self._finalize_cancel(task, lease)
                        return
                await communication
            except asyncio.CancelledError:
                if process is not None:
                    await self._terminate(process)
                if communication is not None and not communication.done():
                    communication.cancel()
                    await asyncio.gather(communication, return_exceptions=True)
                raise
            except Exception as error:
                if process is not None and process.returncode is None:
                    await self._terminate(process)
                if communication is not None and not communication.done():
                    communication.cancel()
                    await asyncio.gather(communication, return_exceptions=True)
                await self._mark_failed(task, lease, f"无法启动隔离任务进程：{error}")
                raise

        if process.returncode == 0:
            return
        if process.returncode == TASK_SUSPENDED_EXIT_CODE:
            stored = await self.broker.get_task(task.task_id)
            if stored is not None and stored.status in {TaskStatus.WAITING_PIPELINE, TaskStatus.PAUSED_BY_TRIAGE}:
                raise TaskSuspended(
                    task.task_id,
                    stored.wait_kind or ("mr_priority" if stored.status is TaskStatus.PAUSED_BY_TRIAGE else "pipeline"),
                    stored.wait_identity or "child-process",
                )
            raise TaskSuspended(task.task_id, "pipeline", "child-process")
        if process.returncode == TASK_CANCELED_EXIT_CODE:
            await self._finalize_cancel(task, lease)
            return
        error = f"隔离任务进程异常退出（exit={process.returncode}）"
        await self._mark_failed(task, lease, error)
        raise RuntimeError(error)

    async def _finalize_cancel(self, task: TaskEnvelope, lease: MrLease | None) -> None:
        if not await self._reconcile_interrupted_commit(task, lease):
            await self.broker.finalize_repair_cancel(
                task,
                lease,
                "修复已停止，但提交操作的远端结果无法可靠确认，系统未自动撤回；请人工检查源分支。",
            )
            await self._persist_repair_terminal(task, error="取消时无法确认提交结果")
            return
        if task.kind is TaskKind.POST_REPAIR_UT:
            stored = await self.broker.get_task(task.task_id)
            if stored is not None and stored.status in {TaskStatus.CANCELED, TaskStatus.FAILED}:
                return
            manifest = await self.broker.freeze_repair_commit_manifest(task.task_id, lease)
            if manifest is None or not manifest.entries:
                from dataclasses import replace

                from pr_agent.distributed.models import PostRepairUTStatus
                from pr_agent.distributed.notifications import (
                    build_post_repair_ut_terminal_reminder,
                    queue_post_repair_ut_progress,
                )

                binding = await self.broker.get_task_triage_card(task.task_id)
                if binding is not None:
                    state = replace(
                        binding.post_repair_ut,
                        status=PostRepairUTStatus.CANCELED,
                        status_markdown="补测已取消，未产生需要撤回的提交",
                        outcome_reason="补测已取消，未产生需要撤回的提交",
                    )
                    await queue_post_repair_ut_progress(self.broker, task.task_id, state, terminal=True)
                    updated = await self.broker.get_task_triage_card(task.task_id)
                    if updated is not None:
                        await self.broker.enqueue_notification(
                            build_post_repair_ut_terminal_reminder(updated, task.task_id)
                        )
                await self.broker.transition_task(
                    task.task_id,
                    {TaskStatus.RUNNING, TaskStatus.ASSIGNED, TaskStatus.WAITING_PIPELINE},
                    TaskStatus.CANCELED,
                    lease,
                    {"error": "用户取消补测"},
                )
                if task.mr is not None and lease is not None:
                    await self.broker.resume_auto_after_triage(
                        task.mr,
                        triage_task_id=task.task_id,
                        worker_id=lease.worker_id,
                        fencing_token=lease.fencing_token,
                    )
                return
            if manifest.base_commit_sha != str(task.payload.get("baseline_sha") or ""):
                from dataclasses import replace

                from pr_agent.distributed.models import PostRepairUTStatus
                from pr_agent.distributed.notifications import (
                    build_post_repair_ut_terminal_reminder,
                    queue_post_repair_ut_progress,
                )

                binding = await self.broker.get_task_triage_card(task.task_id)
                if binding is not None:
                    reason = "补测提交清单越过已修复基线，系统拒绝自动撤回，请人工检查"
                    state = replace(
                        binding.post_repair_ut,
                        status=PostRepairUTStatus.ROLLBACK_FAILED,
                        status_markdown=reason,
                        outcome_reason=reason,
                    )
                    await queue_post_repair_ut_progress(self.broker, task.task_id, state, terminal=True)
                    updated = await self.broker.get_task_triage_card(task.task_id)
                    if updated is not None:
                        await self.broker.enqueue_notification(
                            build_post_repair_ut_terminal_reminder(updated, task.task_id)
                        )
                await self.broker.transition_task(
                    task.task_id,
                    {TaskStatus.RUNNING, TaskStatus.ASSIGNED, TaskStatus.WAITING_PIPELINE},
                    TaskStatus.FAILED,
                    lease,
                    {"error": "post-repair UT rollback baseline mismatch"},
                )
                if task.mr is not None and lease is not None:
                    await self.broker.resume_auto_after_triage(
                        task.mr,
                        triage_task_id=task.task_id,
                        worker_id=lease.worker_id,
                        fencing_token=lease.fencing_token,
                    )
                return
        rollback = await self.broker.finalize_cancel_or_enqueue_rollback(task, lease)
        if rollback is None:
            await self._persist_repair_terminal(task, error="用户取消修复")

    async def _reconcile_interrupted_commit(self, task: TaskEnvelope, lease: MrLease | None) -> bool:
        active = await self.broker.get_active_effect(task.task_id)
        if active is None:
            return True
        effect_key, effect = active
        metadata = effect.metadata
        code_effect = ":commit-push:" in effect_key or ":format-commit:" in effect_key
        if not code_effect:
            await self.broker.complete_effect(effect_key, lease, {"status": "canceled"})
            return True
        base_sha = str(metadata.get("previous_remote_sha") or metadata.get("base_sha") or "")
        commit_sha = str(metadata.get("commit_sha") or "")
        source_branch = str(metadata.get("source_branch") or "")
        if not base_sha or not source_branch:
            return False

        def remote_head() -> str:
            from pr_agent.git_providers.gitlab_provider import GitLabProvider

            provider = GitLabProvider(task.pr_url)
            project_id = getattr(provider.mr, "source_project_id", None) or provider.id_project
            project = provider.gl.projects.get(project_id)
            branch = project.branches.get(source_branch)
            return str((getattr(branch, "commit", {}) or {}).get("id") or "")

        head_sha = await asyncio.to_thread(remote_head)
        if head_sha == base_sha:
            await self.broker.complete_effect(effect_key, lease, {"status": "canceled_before_push"})
            return True
        required = {"parent_sha", "tree_sha", "base_tree_sha", "task_marker", "pushed_at"}
        if not commit_sha or head_sha != commit_sha or not required.issubset(metadata):
            return False
        from pr_agent.triage.repair_rollback import RepairCommitEntry

        binding = await self.broker.get_task_triage_card(task.task_id)
        if binding is None or not binding.receive_id:
            return False
        entry = RepairCommitEntry(
            sequence=int(metadata.get("attempt_sequence") or 0),
            commit_sha=commit_sha,
            parent_sha=str(metadata["parent_sha"]),
            tree_sha=str(metadata["tree_sha"]),
            effect_id=effect_key,
            task_marker=str(metadata["task_marker"]),
            pushed_at=str(metadata["pushed_at"]),
        )
        stored = await self.broker.get_task(task.task_id)
        manifest = stored.repair_commit_manifest if stored is not None else None
        base_tree_sha = resolve_repair_manifest_base_tree(
            manifest,
            entry,
            str(metadata["base_tree_sha"]),
        )
        await self.broker.append_repair_commit(
            task.task_id,
            entry,
            base_tree_sha=base_tree_sha,
            source_branch=source_branch,
            authorized_actor_id=binding.receive_id,
            lease=lease,
        )
        await self.broker.complete_effect(effect_key, lease, {"status": "push_confirmed_during_cancel"})
        return True

    async def _mark_failed(self, task: TaskEnvelope, lease: MrLease | None, error: str) -> None:
        try:
            if task.kind is TaskKind.POST_REPAIR_UT:
                manifest = await self.broker.freeze_repair_commit_manifest(task.task_id, lease)
                binding = await self.broker.get_task_triage_card(task.task_id)
                if (
                    manifest is not None
                    and manifest.entries
                    and binding is not None
                    and manifest.base_commit_sha == str(task.payload.get("baseline_sha") or "")
                ):
                    await self.broker.request_repair_rollback(
                        task.task_id,
                        binding.card_id,
                        binding.open_message_id,
                        binding.receive_id,
                        binding.revision,
                        trigger="post_repair_ut_failure",
                    )
                    return
                from dataclasses import replace

                from pr_agent.distributed.models import PostRepairUTStatus
                from pr_agent.distributed.notifications import (
                    build_post_repair_ut_terminal_reminder,
                    queue_post_repair_ut_progress,
                )

                if binding is not None:
                    reason = error or "补测子进程异常终止"
                    state = replace(
                        binding.post_repair_ut,
                        status=PostRepairUTStatus.FAILED,
                        status_markdown=reason,
                        outcome_reason=reason,
                    )
                    await queue_post_repair_ut_progress(self.broker, task.task_id, state, terminal=True)
                    updated = await self.broker.get_task_triage_card(task.task_id)
                    if updated is not None:
                        await self.broker.enqueue_notification(
                            build_post_repair_ut_terminal_reminder(updated, task.task_id)
                        )
            changed = await self.broker.transition_task(
                task.task_id,
                {TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.PUBLISHING},
                TaskStatus.FAILED,
                lease,
                {"error": error},
            )
            if changed:
                if task.kind is not TaskKind.POST_REPAIR_UT:
                    await queue_triage_failure_notification(self.broker, task, error)
                await self._persist_repair_terminal(task, error=error)
                if task.kind is TaskKind.POST_REPAIR_UT:
                    from pr_agent.triage.terminal import persist_post_repair_ut_terminal

                    await persist_post_repair_ut_terminal(self.broker, task.task_id)
                if lease is not None and (self._is_repair_command(task) or task.kind is TaskKind.POST_REPAIR_UT):
                    await self.broker.resume_auto_after_triage(
                        task.mr,
                        triage_task_id=task.task_id,
                        worker_id=lease.worker_id,
                        fencing_token=lease.fencing_token,
                    )
        except LostLeaseError:
            get_logger().warning(f"Task process failed after lease loss: task_id={task.task_id}")
        except Exception:
            get_logger().exception(f"Failed to persist child process failure: task_id={task.task_id}")

    async def _persist_repair_terminal(self, task: TaskEnvelope, *, error: str = "") -> None:
        if not self._is_repair_command(task):
            return
        from pr_agent.triage.terminal import persist_repair_terminal

        await persist_repair_terminal(self.broker, task.task_id, error=error)

    @staticmethod
    def _is_triage(task: TaskEnvelope) -> bool:
        return bool(task.command) and task.command.split()[0].lower() == "/triage"

    @staticmethod
    def _is_repair_command(task: TaskEnvelope) -> bool:
        return bool(task.command) and task.command.split()[0].lower() in {
            "/triage",
            "/fix-format",
            "/fix_format",
            "/repair-pipeline",
        }

    @staticmethod
    async def _terminate(process) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            await process.wait()
