import asyncio
from dataclasses import replace
from typing import Any, Callable

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.config_loader import task_settings_context
from pr_agent.distributed.broker import (
    LostLeaseError,
    MrLease,
    RedisBroker,
    RepairAlreadyRunningError,
    RepairRollbackUnavailable,
    StaleCardActionError,
    SyncRedisBroker,
    UnauthorizedRepairRollback,
)
from pr_agent.distributed.models import (
    AutoWorkflowDecision,
    PipelineEvent,
    PipelineResumeClaim,
    PostRepairUTState,
    PostRepairUTStatus,
    RepairCategory,
    RepairItem,
    RepairItemStatus,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardState,
)
from pr_agent.distributed.notifications import (
    queue_pipeline_repair_progress,
    queue_post_repair_ut_progress,
    queue_repair_reconciliation,
    queue_triage_card_update,
    queue_triage_failure_notification,
)
from pr_agent.distributed.runtime import (
    ExecutionRuntime,
    TaskCanceled,
    TaskSuspended,
    execution_context,
    get_execution_runtime,
)
from pr_agent.log import get_logger
from pr_agent.suggestions.review_tracking import (
    activate_review_run,
    finish_review_run,
    get_review_run,
    get_review_run_for_task,
    record_review_event,
    update_review_run,
)
from pr_agent.triage.failure_explanations import (
    FailureExplanation,
    collect_gitlab_failure_explanations,
    merge_failure_explanations,
    sanitize_failure_text,
)
from pr_agent.triage.pipeline_coverage import CoverageResult
from pr_agent.triage.pipeline_repair import (
    CoverageContinuationPhase,
    PipelineRepairPhase,
    PipelineRepairState,
    PipelineRepairStep,
    initial_repair_step,
    next_step_after_triage,
)
from pr_agent.triage.repair_details import RepairAction, RepairProgressEvent, merge_repair_actions


class TaskExecutor:
    def __init__(
        self,
        broker: RedisBroker,
        sync_broker: SyncRedisBroker,
        worker_id: str,
        *,
        max_active_tasks: int,
        checkpointer: Any = None,
        agent_factory: Callable[[], PRAgent] = PRAgent,
        webhook_jobs: Any = None,
    ) -> None:
        self.broker = broker
        self.sync_broker = sync_broker
        self.worker_id = worker_id
        self.checkpointer = checkpointer
        self.agent_factory = agent_factory
        self.webhook_jobs = webhook_jobs
        self.active_task_slots = asyncio.Semaphore(max_active_tasks)

    async def execute(self, task: TaskEnvelope, lease: MrLease | None) -> None:
        await self._execute(task, lease, expected={TaskStatus.ASSIGNED})

    async def resume_pipeline(self, task: TaskEnvelope, lease: MrLease | None, event: PipelineEvent) -> None:
        await self._execute(task, lease, expected={TaskStatus.WAITING_PIPELINE}, pipeline_event=event)

    async def _execute(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        *,
        expected: set[TaskStatus],
        pipeline_event: PipelineEvent | None = None,
    ) -> None:
        runtime = ExecutionRuntime(
            task_id=task.task_id,
            worker_id=self.worker_id,
            lease=lease,
            mode="queue",
            broker=self.broker,
            sync_broker=self.sync_broker,
            checkpointer=self.checkpointer,
            pipeline_event=pipeline_event,
        )
        async with self.active_task_slots:
            with task_settings_context(), execution_context(runtime):
                try:
                    execution_result = None
                    await runtime.raise_if_canceled_async()
                    if pipeline_event is not None:
                        claim = await self.broker.claim_pipeline_resume(task.task_id, pipeline_event, lease)
                        if claim in {PipelineResumeClaim.DUPLICATE, PipelineResumeClaim.STALE}:
                            return
                        if claim is PipelineResumeClaim.LOST_LEASE:
                            raise LostLeaseError(task.task_id)
                    if pipeline_event is not None:
                        from pr_agent.distributed.lifecycle import LifecycleEvent, pipeline_wait_segment

                        stored = await self.broker.get_task(task.task_id)
                        segment_id = pipeline_wait_segment(
                            (stored.pipeline_attempt_id if stored else ""),
                            pipeline_event.sha,
                            (stored.pipeline_id if stored else None),
                        )
                        await self.broker.record_lifecycle_event(
                            LifecycleEvent.new(task.task_id, "pipeline_wait", "end", segment_id=segment_id)
                        )
                    if pipeline_event is None:
                        transitioned = await self.broker.transition_task(
                            task.task_id,
                            expected,
                            TaskStatus.RUNNING,
                            lease,
                        )
                        if not transitioned:
                            statuses = sorted(status.value for status in expected)
                            raise RuntimeError(f"task is not executable from {statuses}")
                    if task.kind is TaskKind.REPAIR_ROLLBACK:
                        await queue_triage_card_update(
                            self.broker,
                            task.task_id,
                            TriageCardState.ROLLBACK_RUNNING,
                            "正在校验分支和本次修复提交，确认安全后执行撤回。",
                        )
                        await self._run_repair_rollback(task, lease)
                        return
                    if task.kind is TaskKind.REPAIR_REPORT:
                        await self._run_final_repair_report(task)
                        return
                    if self._is_repair_command(task):
                        await self._queue_repair_state(
                            task,
                            TriageCardState.REPAIR_RUNNING,
                            self._pipeline_resume_markdown(pipeline_event)
                            if pipeline_event is not None
                            else "正在读取失败信息并准备修复……",
                        )
                    elif self._is_post_repair_ut(task):
                        await self._queue_post_repair_ut_state(
                            task,
                            PostRepairUTStatus.RUNNING,
                            "正在分析 MR 变更并补充单元测试",
                        )
                    with self._provider_context(task):
                        await runtime.raise_if_canceled_async()
                        if pipeline_event is not None and self._is_pipeline_repair(task):
                            await self._resume_pipeline_repair(task, lease, pipeline_event)
                        elif pipeline_event is not None and self._is_post_repair_ut(task):
                            execution_result = await self._run_post_repair_ut(task, pipeline_event)
                        elif pipeline_event is not None and self._is_triage(task):
                            await self._resume_triage(task, pipeline_event)
                        elif pipeline_event is not None and self._is_fix_format(task):
                            await self._resume_fix_format(task, pipeline_event)
                        else:
                            execution_result = await self._run_task(task, lease)
                    await runtime.raise_if_canceled_async()
                    if self._is_repair_command(task):
                        manifest = await self.broker.freeze_repair_commit_manifest(task.task_id, lease)
                        if await self._enqueue_automatic_failure_rollback(task, manifest):
                            await self._persist_repair_terminal(task)
                            return
                        await self.broker.publish_repair_rollback_eligibility(task.task_id, manifest)
                    elif self._is_post_repair_ut(task):
                        manifest = await self.broker.freeze_repair_commit_manifest(task.task_id, lease)
                        if await self._settle_post_repair_ut(task, lease, execution_result, manifest):
                            return
                    await self.broker.transition_task(
                        task.task_id, {TaskStatus.RUNNING}, TaskStatus.PUBLISHING, lease
                    )
                    await self.broker.record_task_result(task.task_id, execution_result or {"ok": True}, lease)
                    completed = await self.broker.transition_task(
                        task.task_id, {TaskStatus.PUBLISHING}, TaskStatus.COMPLETED, lease
                    )
                    if completed:
                        await self._persist_repair_terminal(task)
                        await self._persist_post_repair_ut_terminal(task)
                        await self._resume_paused_auto_if_triage(task, lease)
                except TaskCanceled:
                    if self._is_post_repair_ut(task):
                        await self._cancel_post_repair_ut(task, lease)
                        raise
                    rollback = await self.broker.finalize_cancel_or_enqueue_rollback(task, lease)
                    if rollback is None:
                        await self._persist_repair_terminal(task, error="用户取消修复")
                    raise

                except TaskSuspended as suspended:
                    if suspended.wait_kind == "mr_priority":
                        stored = await self.broker.get_task(task.task_id)
                        if stored is None or stored.status is not TaskStatus.PAUSED_BY_TRIAGE:
                            await self.broker.transition_task(
                                task.task_id,
                                {TaskStatus.RUNNING},
                                TaskStatus.PAUSED_BY_TRIAGE,
                                lease,
                                {"wait_kind": suspended.wait_kind, "wait_identity": suspended.wait_identity},
                            )
                    else:
                        await self.broker.transition_task(
                            task.task_id,
                            {TaskStatus.RUNNING},
                            TaskStatus.WAITING_PIPELINE,
                            lease,
                            {"wait_kind": suspended.wait_kind, "wait_identity": suspended.wait_identity},
                        )
                        wait_status = f"等待流水线：{suspended.wait_identity}"
                        if self._is_pipeline_repair(task):
                            wait_status = await self._pipeline_repair_wait_status(task, suspended.wait_identity)
                        await self._queue_repair_state(
                            task,
                            TriageCardState.WAITING_PIPELINE,
                            wait_status,
                        )
                        if self._is_post_repair_ut(task):
                            await self._queue_post_repair_ut_state(
                                task,
                                PostRepairUTStatus.WAITING_PIPELINE,
                                "测试代码已提交，正在等待验证流水线",
                            )
                        if suspended.wait_kind != "pipeline_group":
                            await self.broker.resume_pipeline_if_cached(task.task_id)
                    raise
                except LostLeaseError:
                    raise
                except Exception as error:
                    if self._is_post_repair_ut(task):
                        manifest = await self.broker.freeze_repair_commit_manifest(task.task_id, lease)
                        if await self._fail_post_repair_ut(task, lease, manifest, str(error)):
                            raise
                    if self._is_repair_command(task):
                        manifest = await self.broker.freeze_repair_commit_manifest(task.task_id, lease)
                        await self.broker.publish_repair_rollback_eligibility(task.task_id, manifest)
                    failed = await self.broker.transition_task(
                        task.task_id,
                        {TaskStatus.RUNNING, TaskStatus.PUBLISHING},
                        TaskStatus.FAILED,
                        lease,
                        {"error": str(error)},
                    )
                    if failed:
                        if task.kind not in {TaskKind.REPAIR_REPORT, TaskKind.POST_REPAIR_UT}:
                            await queue_triage_failure_notification(self.broker, task, str(error))
                        await self._persist_repair_terminal(task, error=str(error))
                        await self._resume_paused_auto_if_triage(task, lease)
                    raise

    async def _enqueue_automatic_failure_rollback(self, task: TaskEnvelope, manifest) -> bool:
        """Atomically hand a zero-benefit repair to its rollback child."""
        stored = await self.broker.get_task(task.task_id)
        if stored is None or not stored.pipeline_repair_state.auto_rollback_required:
            return False
        if manifest is None or not manifest.entries:
            return False
        binding = await self.broker.get_task_triage_card(task.task_id)
        if binding is None:
            return False
        try:
            await self.broker.request_repair_rollback(
                task.task_id,
                binding.card_id,
                binding.open_message_id,
                binding.receive_id,
                binding.revision,
                trigger="auto_failure",
            )
            return True
        except (
            RepairAlreadyRunningError,
            RepairRollbackUnavailable,
            StaleCardActionError,
            UnauthorizedRepairRollback,
        ) as error:
            safe_stop_markdown = f"修复失败，自动撤回未完成：{error}"
            await queue_repair_reconciliation(
                self.broker,
                task.task_id,
                binding.repair_items,
                TriageCardState.REPAIR_FAILED,
                safe_stop_markdown,
                binding.current_pipeline_id,
                binding.current_pipeline_sha,
            )
            return False

    async def _persist_repair_terminal(self, task: TaskEnvelope, *, error: str = "") -> None:
        if not self._is_repair_command(task):
            return
        from pr_agent.triage.terminal import persist_repair_terminal

        await persist_repair_terminal(self.broker, task.task_id, error=error)

    async def _persist_post_repair_ut_terminal(self, task: TaskEnvelope) -> None:
        if not self._is_post_repair_ut(task):
            return
        from pr_agent.triage.terminal import persist_post_repair_ut_terminal

        await persist_post_repair_ut_terminal(self.broker, task.task_id)

    async def _queue_repair_state(
        self,
        task: TaskEnvelope,
        state: TriageCardState,
        status_markdown: str,
    ) -> None:
        if task.source != "feishu" or not self._is_repair_command(task):
            return
        try:
            await queue_triage_card_update(self.broker, task.task_id, state, status_markdown)
        except Exception:
            get_logger().exception(f"Failed to queue triage card update task_id={task.task_id} state={state.value}")

    async def _run_task(self, task: TaskEnvelope, lease: MrLease | None) -> dict | None:
        if task.kind is TaskKind.AUTO_WORKFLOW:
            await self._run_auto_workflow(task, lease)
        elif task.kind is TaskKind.POST_REPAIR_UT:
            return await self._run_post_repair_ut(task)
        elif task.kind is TaskKind.PR_COMMAND:
            if self._is_pipeline_repair(task):
                await self._run_pipeline_repair(task, lease)
                return
            if self._is_fix_format(task) and await self.broker.get_task_triage_card(task.task_id) is not None:
                await self._run_fix_format(task)
                return
            agent = self.agent_factory()
            if self.webhook_jobs is not None:
                await self.webhook_jobs.before_command(task)
            await self._run_command(agent, task, task.command)
        elif self.webhook_jobs is not None:
            await self.webhook_jobs.execute(task)
        else:
            raise RuntimeError("GitLab event executor is not configured")
        return None

    async def _run_post_repair_ut(
        self,
        task: TaskEnvelope,
        pipeline_event: PipelineEvent | None = None,
    ) -> dict:
        from pr_agent.distributed.post_repair_ut_runner import PostRepairUTRunner

        runner = PostRepairUTRunner(checkpointer=self.checkpointer)
        return await (runner.resume(task, pipeline_event) if pipeline_event is not None else runner.run(task))

    async def _queue_post_repair_ut_state(
        self,
        task: TaskEnvelope,
        status: PostRepairUTStatus,
        status_markdown: str,
    ) -> None:
        binding = await self.broker.get_task_triage_card(task.task_id)
        if binding is None:
            return
        current = binding.post_repair_ut
        state = PostRepairUTState(
            **{
                **current.to_dict(),
                "status": status,
                "status_markdown": status_markdown,
            }
        )
        await queue_post_repair_ut_progress(self.broker, task.task_id, state)

    async def _settle_post_repair_ut(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        execution_result: dict | None,
        manifest,
    ) -> bool:
        from pr_agent.distributed.notifications import build_post_repair_ut_terminal_reminder
        from pr_agent.triage.post_repair_ut_terminal import classify_post_repair_ut_result

        outcome = classify_post_repair_ut_result(execution_result)
        if not outcome.keeps_commits:
            await self.broker.record_task_result(task.task_id, execution_result or {"ok": False}, lease)
            return await self._fail_post_repair_ut(task, lease, manifest, outcome.reason)
        binding = await self.broker.get_task_triage_card(task.task_id)
        if binding is None:
            raise RuntimeError("post-repair UT card is unavailable")
        current = binding.post_repair_ut
        state = PostRepairUTState(
            **{
                **current.to_dict(),
                "status": outcome.status,
                "current_pipeline_id": outcome.pipeline_id,
                "current_sha": outcome.commit_sha,
                "coverage_after": outcome.coverage,
                "status_markdown": outcome.reason,
                "outcome_reason": outcome.reason,
            }
        )
        await queue_post_repair_ut_progress(self.broker, task.task_id, state, terminal=True)
        updated = await self.broker.get_task_triage_card(task.task_id)
        if updated is not None:
            await self.broker.enqueue_notification(build_post_repair_ut_terminal_reminder(updated, task.task_id))
        return False

    async def _fail_post_repair_ut(self, task: TaskEnvelope, lease: MrLease | None, manifest, reason: str) -> bool:
        binding = await self.broker.get_task_triage_card(task.task_id)
        if binding is None:
            return False
        if manifest is not None and manifest.entries:
            if manifest.base_commit_sha != str(task.payload.get("baseline_sha") or ""):
                reason = "补测提交清单越过已修复基线，系统拒绝自动撤回，请人工检查"
                await self._finish_post_repair_ut_without_rollback(
                    task, lease, PostRepairUTStatus.ROLLBACK_FAILED, reason, TaskStatus.FAILED
                )
                return True
            await self.broker.request_repair_rollback(
                task.task_id,
                binding.card_id,
                binding.open_message_id,
                binding.receive_id,
                binding.revision,
                trigger="post_repair_ut_failure",
            )
            return True
        await self._finish_post_repair_ut_without_rollback(
            task, lease, PostRepairUTStatus.FAILED, reason or "补测未完成", TaskStatus.FAILED
        )
        return True

    async def _cancel_post_repair_ut(self, task: TaskEnvelope, lease: MrLease | None) -> None:
        manifest = await self.broker.freeze_repair_commit_manifest(task.task_id, lease)
        binding = await self.broker.get_task_triage_card(task.task_id)
        if manifest is not None and manifest.entries and binding is not None:
            if manifest.base_commit_sha == str(task.payload.get("baseline_sha") or ""):
                await self.broker.request_repair_rollback(
                    task.task_id,
                    binding.card_id,
                    binding.open_message_id,
                    binding.receive_id,
                    binding.revision,
                    trigger="post_repair_ut_cancel",
                )
                return
        await self._finish_post_repair_ut_without_rollback(
            task, lease, PostRepairUTStatus.CANCELED, "补测已取消，未产生需要撤回的提交", TaskStatus.CANCELED
        )

    async def _finish_post_repair_ut_without_rollback(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        status: PostRepairUTStatus,
        reason: str,
        task_status: TaskStatus,
    ) -> None:
        from pr_agent.distributed.notifications import build_post_repair_ut_terminal_reminder

        binding = await self.broker.get_task_triage_card(task.task_id)
        if binding is not None:
            current = binding.post_repair_ut
            state = PostRepairUTState(
                **{
                    **current.to_dict(),
                    "status": status,
                    "status_markdown": reason,
                    "outcome_reason": reason,
                }
            )
            await queue_post_repair_ut_progress(self.broker, task.task_id, state, terminal=True)
            updated = await self.broker.get_task_triage_card(task.task_id)
            if updated is not None:
                await self.broker.enqueue_notification(build_post_repair_ut_terminal_reminder(updated, task.task_id))
        await self.broker.transition_task(
            task.task_id,
            {TaskStatus.RUNNING, TaskStatus.PUBLISHING, TaskStatus.WAITING_PIPELINE},
            task_status,
            lease,
            {"error": reason},
        )
        await self._persist_post_repair_ut_terminal(task)
        await self._resume_paused_auto_if_triage(task, lease)

    async def _run_repair_rollback(self, task: TaskEnvelope, lease: MrLease | None) -> None:
        from datetime import datetime, timezone

        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        from pr_agent.triage.repair_rollback import (
            RepairRollbackState,
            RepairRollbackStatus,
            RollbackFailureCode,
        )
        from ut_agent.agent import UT_WORKSPACE
        from ut_agent.repair_rollback import RollbackRequest, RollbackResult, execute_repair_rollback

        repair_task_id = str(task.payload.get("repair_task_id") or "")
        original = await self.broker.get_task(repair_task_id)
        manifest = original.repair_commit_manifest if original is not None else None
        now = datetime.now(timezone.utc).isoformat()
        if task.mr is None or manifest is None or not manifest.validate_static().ok:
            result = RollbackResult(
                RepairRollbackStatus.FAILED,
                failure_code=RollbackFailureCode.MANIFEST_INCOMPLETE,
                message="本次修复缺少完整提交记录，无法自动撤回",
            )
        elif manifest.digest() != str(task.payload.get("manifest_digest") or ""):
            result = RollbackResult(
                RepairRollbackStatus.FAILED,
                failure_code=RollbackFailureCode.MANIFEST_INCOMPLETE,
                message="撤回任务与冻结提交清单不一致",
            )
        else:
            provider = GitLabProvider(task.pr_url)
            mr_state = str(getattr(provider.mr, "state", "") or "")
            source_branch = str(getattr(provider.mr, "source_branch", "") or "")
            if mr_state != "opened":
                result = RollbackResult(
                    RepairRollbackStatus.FAILED,
                    failure_code=RollbackFailureCode.MR_NOT_OPEN,
                    message="MR 已关闭，未自动撤回",
                )
            elif source_branch != manifest.source_branch:
                result = RollbackResult(
                    RepairRollbackStatus.FAILED,
                    failure_code=RollbackFailureCode.SOURCE_BRANCH_CHANGED,
                    message="MR 源分支已变化，未自动撤回",
                )
            else:
                source_project_id = getattr(provider.mr, "source_project_id", None) or provider.id_project
                source_project = provider.gl.projects.get(source_project_id)
                repository_url = str(getattr(source_project, "http_url_to_repo", "") or "")
                authenticated_url = provider._prepare_clone_url_with_token(repository_url)
                request = RollbackRequest(
                    project_id=task.mr.project_id,
                    mr_iid=task.mr.iid,
                    mr_url=task.pr_url,
                    source_branch=source_branch,
                    manifest=manifest,
                    rollback_task_id=task.task_id,
                    repository_url=str(authenticated_url or ""),
                )
                effect_key = f"{task.task_id}:repair-rollback:{repair_task_id}:{manifest.digest()}"
                claim = await self.broker.claim_effect(
                    effect_key,
                    lease,
                    {"expected_remote_head": manifest.final_repair_sha, "manifest_digest": manifest.digest()},
                )
                if claim.status == "completed" and isinstance(claim.result, dict):
                    result = RollbackResult.from_dict(claim.result)
                else:
                    result = await asyncio.to_thread(
                        execute_repair_rollback,
                        request,
                        UT_WORKSPACE,
                        lambda: self.sync_broker.assert_fence(lease) if lease is not None else None,
                    )
                    await self.broker.complete_effect(effect_key, lease, result.to_dict())
        state = RepairRollbackState(
            rollback_task_id=task.task_id,
            repair_task_id=repair_task_id,
            status=result.status,
            trigger=str(task.payload.get("trigger") or "post_repair"),
            requested_by=str(task.payload.get("requested_by") or ""),
            expected_remote_head=manifest.final_repair_sha if manifest is not None else "",
            manifest_digest=manifest.digest() if manifest is not None else "",
            rollback_commit_sha=result.rollback_commit_sha,
            failure_code=result.failure_code,
            failure_message=result.message,
            retryable=result.retryable,
            created_at=(
                original.repair_rollback_state.created_at
                if original and original.repair_rollback_state
                else now
            ),
            updated_at=now,
        )
        completed = await self.broker.complete_repair_rollback(task, lease, state)
        if not completed:
            raise RuntimeError("无法保存撤回任务结果")
        if state.trigger.startswith("post_repair_ut_"):
            from pr_agent.triage.terminal import persist_post_repair_ut_terminal

            await persist_post_repair_ut_terminal(self.broker, repair_task_id)
        else:
            from pr_agent.triage.terminal import persist_repair_rollback

            await persist_repair_rollback(self.broker, repair_task_id)
        if task.mr is not None and lease is not None:
            await self.broker.resume_auto_after_triage(
                task.mr,
                triage_task_id=task.task_id,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
            )

    async def _run_final_repair_report(self, task: TaskEnvelope) -> None:
        from dataclasses import replace
        from datetime import datetime, timezone

        from pr_agent.distributed.repair_report_tasks import (
            build_final_report_input,
            generate_final_repair_report,
        )
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        from pr_agent.triage.final_repair_report import (
            FinalRepairReportState,
            RepairReportStatus,
        )

        repair_task_id = str(task.payload.get("repair_task_id") or "")
        original = await self.broker.get_task(repair_task_id)
        now = datetime.now(timezone.utc).isoformat()
        if original is None or original.repair_commit_manifest is None:
            state = FinalRepairReportState(
                RepairReportStatus.FALLBACK,
                report_task_id=task.task_id,
                failure_reason="原修复任务或提交边界不存在",
                created_at=now,
                updated_at=now,
            )
            await self.broker.complete_final_repair_report(task, None, state)
            return

        provider = GitLabProvider(original.envelope.pr_url)
        project = provider.gl.projects.get(original.repair_commit_manifest.project_id)
        binding = await self.broker.get_task_triage_card(repair_task_id)
        value = build_final_report_input(original, binding, project, report_task_id=task.task_id)
        if isinstance(value, FinalRepairReportState):
            await self.broker.complete_final_repair_report(task, None, value)
            from pr_agent.triage.terminal import persist_repair_terminal

            await persist_repair_terminal(self.broker, repair_task_id)
            return

        generating = FinalRepairReportState(
            RepairReportStatus.GENERATING,
            report_task_id=task.task_id,
            input_digest=value.digest(),
            created_at=now,
            updated_at=now,
        )
        await self.broker.set_final_repair_report_state(repair_task_id, task.task_id, generating)
        effect_key = f"{task.task_id}:model-summary:{value.digest()}"
        claim = await self.broker.claim_effect(effect_key, None, {"input_digest": value.digest()})
        if (
            claim.status == "completed"
            and isinstance(claim.result, dict)
            and isinstance(claim.result.get("state"), dict)
        ):
            generated = FinalRepairReportState.from_dict(claim.result["state"])
        elif claim.status == "completed" and isinstance(claim.result, dict):
            from ut_agent.llm import LLMTextOutcome
            from ut_agent.model_failover import ModelAttempt

            attempts = tuple(
                ModelAttempt(
                    str(item.get("model") or ""),
                    str(item.get("failure_code") or ""),
                    str(item.get("reason") or ""),
                )
                for item in claim.result.get("attempts") or ()
                if isinstance(item, dict)
            )
            legacy_outcome = LLMTextOutcome(
                str(claim.result.get("text") or ""),
                str(claim.result.get("model") or ""),
                attempts,
                str(claim.result.get("terminal_error") or ""),
            )
            generated = await generate_final_repair_report(value, outcome=legacy_outcome)
        else:
            generated = await generate_final_repair_report(value)
            await self.broker.complete_effect(effect_key, None, {"state": generated.to_dict()})
        state = replace(
            generated,
            report_task_id=task.task_id,
            created_at=generating.created_at,
        )
        if not await self.broker.complete_final_repair_report(task, value, state):
            raise RuntimeError("无法保存最终修复说明")
        from pr_agent.triage.terminal import persist_repair_terminal

        await persist_repair_terminal(self.broker, repair_task_id)
        try:
            from ut_agent.repair_memory.episodes import record_verified_repair_episodes

            await asyncio.to_thread(
                record_verified_repair_episodes,
                value,
                state,
                original.repair_commit_manifest,
                original.pipeline_repair_state,
            )
        except Exception:
            get_logger().exception(f"Repair memory capture failed: task_id={repair_task_id}")

    async def _run_auto_workflow(self, task: TaskEnvelope, lease: MrLease | None) -> None:
        run = get_review_run_for_task(task.task_id) or {}
        run_id = str(run.get("run_id") or "") or None
        with activate_review_run(run_id):
            if run_id:
                update_review_run(run_id, stage="workflow_started")
                record_review_event(run_id, "workflow_started", "workflow_started")
            try:
                decision = await self._run_auto_workflow_commands(task, lease)
            except TaskSuspended:
                raise
            except Exception as exc:
                if run_id:
                    current = get_review_run(run_id)
                    failure_stage = "execution_failed" if current.get("improve_started_at") else "startup_failed"
                    finish_review_run(
                        "failed", run_id, stage=failure_stage,
                        error_code=type(exc).__name__, error_message=str(exc),
                    )
                    record_review_event(
                        run_id, failure_stage, failure_stage, status="failed",
                        error_code=type(exc).__name__, error_message=str(exc),
                    )
                raise
            if not run_id:
                return
            if not decision.allowed:
                finish_review_run(
                    "skipped",
                    run_id,
                    stage="skipped",
                    error_code=decision.reason_code,
                    error_message=decision.reason,
                )
                record_review_event(
                    run_id,
                    "workflow_skipped",
                    "skipped",
                    status="skipped",
                    error_code=decision.reason_code,
                    error_message=decision.reason,
                )
                return
            current = get_review_run(run_id)
            if not current.get("improve_started_at"):
                reason = "Automatic workflow stopped before /improve"
                finish_review_run(
                    "failed", run_id, stage="startup_failed",
                    error_code="ImproveNotStarted", error_message=reason,
                )
                record_review_event(
                    run_id, "startup_failed", "startup_failed", status="failed",
                    error_code="ImproveNotStarted", error_message=reason,
                )
            elif current.get("status") == "failed":
                finish_review_run(
                    "failed", run_id, stage=str(current.get("stage") or "execution_failed"),
                    error_code=str(current.get("error_code") or ""),
                    error_message=str(current.get("error_message") or ""),
                )
            elif current.get("status") == "running":
                finish_review_run("completed", run_id, stage=str(current.get("stage") or "completed"))
                record_review_event(run_id, "workflow_completed", "completed", status="completed")

    async def _run_auto_workflow_commands(
        self, task: TaskEnvelope, lease: MrLease | None,
    ) -> AutoWorkflowDecision:
        if task.mr is None or lease is None:
            raise RuntimeError("auto workflow requires an MR lease")
        raw_commands = task.payload.get("commands")
        if not isinstance(raw_commands, list):
            raise ValueError("auto workflow commands must be a list")
        commands = [str(command) for command in raw_commands]
        cursor = await self.broker.get_auto_cursor(task.task_id)
        next_index = min(max(cursor.next_command_index, 0), len(commands))
        completed_commands = list(cursor.completed_commands)
        workflow_head_sha = cursor.workflow_head_sha or self._workflow_head_sha(task)

        if await self._pause_auto_if_triage_pending(
            task, lease, next_index, completed_commands, workflow_head_sha
        ):
            raise TaskSuspended(task.task_id, "mr_priority", task.mr.redis_id)
        if next_index == 0 and self.webhook_jobs is not None:
            decision = await self.webhook_jobs.prepare_auto_workflow(task)
            if not decision.allowed:
                return decision

        agent = self.agent_factory()
        for index in range(next_index, len(commands)):
            command = commands[index]
            await self._run_command(agent, task, command)
            completed_commands.append(command)
            await self.broker.record_auto_command_completed(
                task.task_id,
                index + 1,
                completed_commands,
                workflow_head_sha,
                lease,
            )
            if await self._pause_auto_if_triage_pending(
                task, lease, index + 1, completed_commands, workflow_head_sha
            ):
                raise TaskSuspended(task.task_id, "mr_priority", task.mr.redis_id)
        return AutoWorkflowDecision.allow()

    async def _pause_auto_if_triage_pending(
        self,
        task: TaskEnvelope,
        lease: MrLease,
        next_command_index: int,
        completed_commands: list[str],
        workflow_head_sha: str,
    ) -> bool:
        if not self.broker.settings.auto_pause_at_command_boundary:
            return False
        if not await self.broker.has_pending_triage(task.mr):
            return False
        triage_task_id = await self.broker.active_triage_task_id(task.mr)
        if not triage_task_id:
            return False
        return await self.broker.pause_auto_for_triage(
            task.task_id,
            task.mr,
            triage_task_id=triage_task_id,
            next_command_index=next_command_index,
            completed_commands=completed_commands,
            workflow_head_sha=workflow_head_sha,
            lease=lease,
        )

    async def _resume_paused_auto_if_triage(self, task: TaskEnvelope, lease: MrLease | None) -> None:
        if lease is None or task.mr is None or not (self._is_repair_command(task) or self._is_post_repair_ut(task)):
            return
        await self.broker.resume_auto_after_triage(
            task.mr,
            triage_task_id=task.task_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
        )

    @staticmethod
    def _workflow_head_sha(task: TaskEnvelope) -> str:
        event = task.payload.get("event")
        if not isinstance(event, dict):
            return str(task.payload.get("head_sha") or "")
        attributes = event.get("object_attributes") or {}
        last_commit = attributes.get("last_commit") or {}
        return str(
            last_commit.get("id")
            or last_commit.get("sha")
            or attributes.get("last_commit_id")
            or attributes.get("sha")
            or ""
        )

    @staticmethod
    async def _run_command(agent: PRAgent, task: TaskEnvelope, command: str) -> None:
        runtime = get_execution_runtime()
        if runtime is not None:
            await runtime.raise_if_canceled_async()
        ok = await agent.handle_request(
            task.pr_url,
            command,
            reviewer_user=task.payload.get("reviewer_user"),
        )
        if not ok:
            raise RuntimeError(f"PR-Agent command failed: {command}")

    def _provider_context(self, task: TaskEnvelope):
        from pr_agent.config_loader import get_settings
        from pr_agent.distributed.effects import IdempotentGitProvider
        from pr_agent.distributed.notifications import QueuedNotificationSink
        from pr_agent.distributed.runtime import get_execution_runtime
        from pr_agent.feishu.feishu_git_provider import FeishuGitProvider
        from pr_agent.git_providers import git_provider_factory_context
        from pr_agent.git_providers.gitlab_provider import GitLabProvider

        runtime = get_execution_runtime(required=True)
        if task.source != "feishu":
            return git_provider_factory_context(
                lambda pr_url=None: IdempotentGitProvider(GitLabProvider(pr_url), runtime)
            )

        sender_id = str(task.payload.get("sender_id") or "")
        sink = QueuedNotificationSink(self.sync_broker, task.task_id)
        get_settings().set("CONFIG.GIT_PROVIDER", "gitlab")
        get_settings().set("PR_DESCRIPTION.PERSISTENT_COMMENT", False)
        get_settings().set("PR_REVIEW.PERSISTENT_COMMENT", False)
        get_settings().set("PR_CODE_SUGGESTIONS.PERSISTENT_COMMENT", False)

        def get_proxy_provider(pr_url=None):
            original = GitLabProvider(pr_url)
            return FeishuGitProvider(
                original,
                sender_id,
                mr_url=task.pr_url,
                notification_sink=sink,
                task_id=task.task_id,
                correlate_triage=self._is_repair_command(task),
            )

        return git_provider_factory_context(get_proxy_provider)

    @staticmethod
    def _is_triage(task: TaskEnvelope) -> bool:
        return bool(task.command) and task.command.split()[0].lower() == "/triage"

    @staticmethod
    def _is_fix_format(task: TaskEnvelope) -> bool:
        return bool(task.command) and task.command.split()[0].lower() in {"/fix-format", "/fix_format"}

    @staticmethod
    def _is_pipeline_repair(task: TaskEnvelope) -> bool:
        return bool(task.command) and task.command.split()[0].lower() == "/repair-pipeline"

    @staticmethod
    def _is_post_repair_ut(task: TaskEnvelope) -> bool:
        return task.kind is TaskKind.POST_REPAIR_UT

    @classmethod
    def _is_repair_command(cls, task: TaskEnvelope) -> bool:
        return cls._is_triage(task) or cls._is_fix_format(task) or cls._is_pipeline_repair(task)

    async def _run_fix_format(self, task: TaskEnvelope) -> None:
        from pr_agent.config_loader import get_settings
        from pr_agent.distributed.runtime import get_execution_runtime
        from pr_agent.tools.pr_fix_format import PRFixFormat

        source_pipeline_id = task.payload.get("source_pipeline_id")
        if source_pipeline_id:
            get_settings().set("PR_FIX_FORMAT.PIPELINE_ID", str(source_pipeline_id))
        result = await PRFixFormat(task.pr_url).run(publish_result=False)
        if not result.pushed_sha:
            await self._fail_active_repair(task, result.status_markdown or "未产生可提交的格式修复。")
            return
        self._reconcile_format_workspace(task, result.pushed_sha)
        runtime = get_execution_runtime(required=True)
        project_id = task.mr.project_id if task.mr is not None else ""
        attempt_id = f"fix-format:{result.pushed_sha}"
        event = runtime.register_pipeline_wait_sync(project_id, result.pushed_sha, attempt_id=attempt_id)
        if event is not None and event.terminal:
            await self._resume_fix_format(task, event)
            return
        raise TaskSuspended(task.task_id, "pipeline", f"{project_id}:{result.pushed_sha}")

    @staticmethod
    def _reconcile_format_workspace(task: TaskEnvelope, pushed_sha: str) -> None:
        """Reconcile a dirty Agent workspace after GitLab accepted a format commit."""
        try:
            from pr_agent.git_providers import get_git_provider_with_context
            from ut_agent.agent import UT_WORKSPACE
            from ut_agent.tools.context import workspace_path
            from ut_agent.workspace import reconcile_workspace_after_remote_commit

            if task.mr is None:
                return
            provider = get_git_provider_with_context(task.pr_url)
            source_branch = str(provider.get_pr_branch() or "")
            repo_dir = workspace_path(UT_WORKSPACE, task.mr.project_id, task.mr.iid, "repo")
            result = reconcile_workspace_after_remote_commit(repo_dir, source_branch, pushed_sha)
            if result.status in {"blocked", "error"}:
                get_logger().warning(
                    f"Format workspace reconciliation incomplete: task_id={task.task_id}, "
                    f"status={result.status}, error_code={result.error_code}"
                )
        except Exception:
            get_logger().exception(f"Format workspace reconciliation failed: task_id={task.task_id}")

    async def _run_pipeline_repair(self, task: TaskEnvelope, lease: MrLease | None) -> None:
        binding = await self.broker.get_task_triage_card(task.task_id)
        pipeline_id = int(
            task.payload.get("source_pipeline_id")
            or (binding.current_pipeline_id if binding is not None else 0)
            or (binding.pipeline_id if binding is not None else 0)
        )
        pipeline_sha = str(
            task.payload.get("source_pipeline_sha")
            or (binding.current_pipeline_sha if binding is not None else "")
            or (binding.pipeline_sha if binding is not None else "")
        )
        memory_mode = None
        memory_settings_error = None
        try:
            from ut_agent.repair_memory.audit import initialize_retrieval_audit, record_retrieval_error
            from ut_agent.repair_memory.config import load_repair_memory_settings
            from ut_agent.repair_memory.models import RetrievalMode

            try:
                memory_mode = load_repair_memory_settings().retrieval_mode
            except Exception as error:
                memory_mode = RetrievalMode.OFF
                memory_settings_error = error
            await asyncio.to_thread(
                initialize_retrieval_audit,
                task_id=task.task_id,
                project=task.mr.project_id if task.mr is not None else "",
                mr_iid=task.mr.iid if task.mr is not None else 0,
                source_pipeline_id=pipeline_id,
                source_sha=pipeline_sha,
                mode=memory_mode,
                reason_code="repair_session_not_reached",
            )
            if memory_settings_error is not None:
                await asyncio.to_thread(
                    record_retrieval_error,
                    task.task_id,
                    error_code=type(memory_settings_error).__name__,
                )
        except Exception as error:
            get_logger().warning(
                f"Failed to initialize retrieval audit: task_id={task.task_id}, error={type(error).__name__}"
            )
        categories, failed_jobs, _, source_explanations = await self._inspect_pipeline(task, pipeline_id)
        raw_selection = task.payload.get("selected_categories") or ()
        selected = tuple(RepairCategory(str(category)) for category in raw_selection)
        if not selected:
            selected = tuple(categories)
        if len(set(selected)) != len(selected) or RepairCategory.PIPELINE in selected:
            raise ValueError("流水线修复类别无效")
        missing = [category.value for category in selected if category not in categories]
        if missing:
            raise RuntimeError(f"所选失败类别已不在源流水线中: {', '.join(missing)}")
        selected_values = tuple(category.value for category in selected)
        failed_job_names = tuple(str((job or {}).get("name") or "") for job in failed_jobs)
        failed_job_names = tuple(name for name in failed_job_names if name)
        state = PipelineRepairState(
            root_pipeline_id=pipeline_id,
            latest_pipeline_id=pipeline_id,
            latest_pipeline_sha=pipeline_sha,
            failed_job_names=failed_job_names,
            source_failed_job_names=failed_job_names,
            selected_categories=selected_values,
            effective_categories=selected_values,
            source_failure_explanations=source_explanations,
            failure_explanations=source_explanations,
        )
        if initial_repair_step(selected) is PipelineRepairStep.FORMAT:
            if memory_mode is not None:
                try:
                    from ut_agent.repair_memory.audit import mark_retrieval_not_attempted

                    await asyncio.to_thread(
                        mark_retrieval_not_attempted,
                        task.task_id,
                        mode=memory_mode,
                        reason_code="format_only_repair",
                    )
                except Exception as error:
                    get_logger().warning(
                        f"Failed to mark format-only retrieval audit: "
                        f"task_id={task.task_id}, error={type(error).__name__}"
                    )
            await self._start_pipeline_format(task, lease, pipeline_id, state)
            return
        await self._start_pipeline_triage(task, lease, state)

    async def _start_pipeline_triage(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        state: PipelineRepairState,
    ) -> None:
        from pr_agent.tools.pr_triage import PRTriage

        running = replace(state, phase=PipelineRepairPhase.TRIAGE_RUNNING)
        await self._record_pipeline_repair_state(task, lease, running)
        await self._record_owner_progress(
            task,
            "diagnosing",
            "正在诊断并修复所选流水线问题",
            categories=running.selected_categories,
            job_names=running.failed_job_names,
            metadata={"pipeline_id": running.latest_pipeline_id, "commit_sha": running.latest_pipeline_sha},
        )
        await self._queue_pipeline_repair_progress(
            task,
            TriageCardState.REPAIR_RUNNING,
            "正在诊断并修复流水线问题",
            running.latest_pipeline_id,
            running.latest_pipeline_sha,
        )
        try:
            selected_non_format = tuple(
                category
                for category in running.selected_categories
                if category != RepairCategory.FORMAT.value
            )
            triage_options = {}
            if selected_non_format:
                triage_options = {
                    "pipeline_id": running.latest_pipeline_id,
                    "pipeline_sha": running.latest_pipeline_sha,
                    "selected_categories": selected_non_format,
                }
            result = await PRTriage(task.pr_url, **triage_options).run(publish_result=False, persist_result=False)
        except TaskSuspended:
            stored = await self.broker.get_task(task.task_id)
            pushed_sha = stored.pipeline_sha if stored is not None else ""
            waiting = replace(
                running,
                phase=PipelineRepairPhase.TRIAGE_WAITING,
                root_pipeline_id=(
                    running.root_pipeline_id
                    if not pushed_sha or pushed_sha == running.latest_pipeline_sha
                    else 0
                ),
                latest_pipeline_sha=pushed_sha or running.latest_pipeline_sha,
            )
            await self._record_pipeline_repair_state(task, lease, waiting)
            await self._queue_pipeline_repair_progress(
                task,
                TriageCardState.WAITING_PIPELINE,
                "等待修复提交触发的新流水线",
                waiting.latest_pipeline_id,
                waiting.latest_pipeline_sha,
            )
            raise
        await self._continue_after_triage_without_resume(task, lease, running, result)

    async def _continue_after_triage_without_resume(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        state: PipelineRepairState,
        result: dict | None,
    ) -> None:
        result_fields = result.get("result", {}) if isinstance(result, dict) else {}
        triage_error = str(result_fields.get("error") or "").strip()
        state = self._state_with_triage_terminal_proof(
            self._state_with_triage_iterations(state, result),
            result,
        )
        state = self._state_with_failure_explanations(state, result_fields.get("failure_explanations"))
        state = self._state_with_repair_actions(state, result_fields.get("repair_actions"))
        state = self._state_with_dependency_blockers(state, result_fields.get("dependency_blockers"))
        state = replace(
            state,
            terminal_failure_kind=str(result_fields.get("terminal_failure_kind") or ""),
            terminal_validation_error_code=str(
                result_fields.get("terminal_validation_error_code")
                or state.terminal_validation_error_code
            ),
            terminal_validation_summary=str(
                result_fields.get("terminal_validation_summary")
                or state.terminal_validation_summary
            ),
            normalized_diagnostic_alias_count=max(
                state.normalized_diagnostic_alias_count,
                int(result_fields.get("normalized_diagnostic_alias_count") or 0),
            ),
        )
        pipeline_groups = result_fields.get("pipeline_groups") or []
        last_group = pipeline_groups[-1] if pipeline_groups and isinstance(pipeline_groups[-1], dict) else {}
        root_pipeline_id = int(
            last_group.get("root_pipeline_id")
            or state.root_pipeline_id
            or state.latest_pipeline_id
        )
        pipeline_id = int(
            last_group.get("validation_pipeline_id")
            or root_pipeline_id
            or state.latest_pipeline_id
        )
        pipeline_sha = str(result_fields.get("pushed_sha") or state.latest_pipeline_sha)
        status = str(result_fields.get("final_pipeline_status") or "failed").lower()
        categories, failed_jobs, coverage, confirmed_explanations = await self._inspect_pipeline(task, pipeline_id)
        if triage_error:
            failed = replace(
                state,
                root_pipeline_id=root_pipeline_id,
                latest_pipeline_id=pipeline_id,
                latest_pipeline_sha=pipeline_sha,
                failed_job_names=tuple(str((job or {}).get("name") or "") for job in failed_jobs),
            )
            await self._finish_pipeline_repair(
                task,
                lease,
                failed,
                pipeline_id,
                pipeline_sha,
                "failed",
                categories,
                failed_jobs,
                coverage,
                triage_error,
                confirmed_explanations=confirmed_explanations,
            )
            return
        completed = self._append_step(state.completed_steps, "诊断修复已完成")
        inspected = replace(
            state,
            completed_steps=completed,
            root_pipeline_id=root_pipeline_id,
            latest_pipeline_id=pipeline_id,
            latest_pipeline_sha=pipeline_sha,
            failed_job_names=tuple(str((job or {}).get("name") or "") for job in failed_jobs),
        )
        failed_job_names = {
            str((job or {}).get("name") or "")
            for job in failed_jobs
            if str((job or {}).get("name") or "")
        }
        all_failed_jobs_blocked = (
            bool(failed_job_names)
            and failed_job_names <= set(inspected.blocked_job_names)
            and not str(result_fields.get("pushed_sha") or "")
        )
        if all_failed_jobs_blocked:
            await self._finish_pipeline_repair(
                task,
                lease,
                inspected,
                pipeline_id,
                pipeline_sha,
                status,
                categories,
                failed_jobs,
                coverage,
                confirmed_explanations=confirmed_explanations,
            )
            return
        if next_step_after_triage(categories) is PipelineRepairStep.FORMAT:
            await self._start_pipeline_format(task, lease, pipeline_id, self._with_format_cleanup(inspected))
            return
        coverage_state = await self._maybe_start_coverage_continuation(
            task,
            lease,
            inspected,
            pipeline_id,
            pipeline_sha,
            failed_jobs,
            coverage,
            result.get("state", {}) if isinstance(result, dict) else {},
        )
        if coverage_state is None:
            return
        inspected = coverage_state
        await self._finish_pipeline_repair(
            task,
            lease,
            inspected,
            pipeline_id,
            pipeline_sha,
            status,
            categories,
            failed_jobs,
            coverage,
            confirmed_explanations=confirmed_explanations,
        )

    async def _resume_pipeline_repair(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        event: PipelineEvent,
    ) -> None:
        await self._wait_for_complete_pipeline_group(task, event)
        stored = await self.broker.get_task(task.task_id)
        state = stored.pipeline_repair_state if stored is not None else PipelineRepairState()
        if state.phase is PipelineRepairPhase.COVERAGE_WAITING:
            await self._resume_coverage_continuation(task, lease, event, state)
            return
        if state.phase is PipelineRepairPhase.COVERAGE_ROLLBACK_WAITING:
            await self._resume_coverage_rollback(task, lease, event, state)
            return
        if state.phase is PipelineRepairPhase.TRIAGE_WAITING:
            await self._record_owner_progress(
                task,
                "validating",
                "新流水线已结束，正在核对修复结果",
                metadata={"pipeline_id": event.pipeline_id, "commit_sha": event.sha},
            )
            await self._queue_pipeline_repair_progress(
                task,
                TriageCardState.REPAIR_RUNNING,
                "正在检查最新流水线",
                event.pipeline_id,
                event.sha,
            )
            try:
                result = await self._resume_triage(task, event, publish_result=False, persist_result=False)
            except TaskSuspended:
                waiting = replace(
                    state,
                    phase=PipelineRepairPhase.TRIAGE_WAITING,
                    latest_pipeline_id=event.pipeline_id,
                    latest_pipeline_sha=event.sha,
                )
                await self._record_pipeline_repair_state(task, lease, waiting)
                raise
            result_fields = result.get("result", {}) if isinstance(result, dict) else {}
            state = self._state_with_triage_terminal_proof(
                self._state_with_triage_iterations(state, result),
                result,
            )
            state = self._state_with_failure_explanations(state, result_fields.get("failure_explanations"))
            state = self._state_with_repair_actions(state, result_fields.get("repair_actions"))
            state = self._state_with_dependency_blockers(state, result_fields.get("dependency_blockers"))
            pipeline_groups = result_fields.get("pipeline_groups") or []
            last_group = pipeline_groups[-1] if pipeline_groups and isinstance(pipeline_groups[-1], dict) else {}
            root_pipeline_id = int(
                last_group.get("root_pipeline_id") or state.root_pipeline_id or event.pipeline_id
            )
            validation_pipeline_id = int(
                last_group.get("validation_pipeline_id") or root_pipeline_id or event.pipeline_id
            )
            categories, failed_jobs, coverage, confirmed_explanations = await self._inspect_pipeline(
                task,
                validation_pipeline_id,
            )
            completed = self._append_step(state.completed_steps, "诊断修复已完成")
            inspected = replace(
                state,
                completed_steps=completed,
                root_pipeline_id=root_pipeline_id,
                latest_pipeline_id=validation_pipeline_id,
                latest_pipeline_sha=event.sha,
                failed_job_names=tuple(str((job or {}).get("name") or "") for job in failed_jobs),
            )
            if event.status != "success" and next_step_after_triage(categories) is PipelineRepairStep.FORMAT:
                await self._start_pipeline_format(
                    task,
                    lease,
                    validation_pipeline_id,
                    self._with_format_cleanup(inspected),
                )
                return
            coverage_state = await self._maybe_start_coverage_continuation(
                task,
                lease,
                inspected,
                validation_pipeline_id,
                event.sha,
                failed_jobs,
                coverage,
                result.get("state", {}) if isinstance(result, dict) else {},
            )
            if coverage_state is None:
                return
            inspected = coverage_state
            await self._finish_pipeline_repair(
                task,
                lease,
                inspected,
                validation_pipeline_id,
                event.sha,
                event.status,
                categories,
                failed_jobs,
                coverage,
                confirmed_explanations=confirmed_explanations,
            )
            return
        if state.phase is PipelineRepairPhase.FORMAT_WAITING:
            await self._record_owner_progress(
                task,
                "validating",
                "新流水线已结束，正在核对格式修复结果",
                categories=(RepairCategory.FORMAT.value,),
                metadata={"pipeline_id": event.pipeline_id, "commit_sha": event.sha},
            )
            await self._queue_pipeline_repair_progress(
                task,
                TriageCardState.REPAIR_RUNNING,
                "正在检查最新流水线",
                event.pipeline_id,
                event.sha,
            )
            categories, failed_jobs, coverage, confirmed_explanations = await self._inspect_pipeline(
                task,
                event.pipeline_id,
            )
            completed = self._append_step(state.completed_steps, "代码格式修复已完成")
            if RepairCategory.FORMAT in categories:
                from pr_agent.config_loader import get_settings

                max_rounds = max(1, int(get_settings().get("triage.format_max_rounds", 3)))
                if state.format_round < max_rounds and state.format_last_exact_report_applied:
                    await self._start_pipeline_format(
                        task,
                        lease,
                        event.pipeline_id,
                        replace(
                            state,
                            completed_steps=completed,
                            latest_pipeline_id=event.pipeline_id,
                            latest_pipeline_sha=event.sha,
                            failed_job_names=tuple(
                                str((job or {}).get("name") or "") for job in failed_jobs
                                if str((job or {}).get("name") or "")
                            ),
                        ),
                    )
                    return
                error = (
                    f"格式自动修复已达到 {max_rounds} 轮上限，仍有 Format 失败任务。"
                    if state.format_round >= max_rounds
                    else "上一轮格式报告未被完整应用，已停止自动重试。"
                )
            else:
                error = ""
            await self._finish_pipeline_repair(
                task,
                lease,
                replace(state, completed_steps=completed),
                event.pipeline_id,
                event.sha,
                event.status,
                categories,
                failed_jobs,
                coverage,
                error,
                confirmed_explanations=confirmed_explanations,
            )
            return
        raise RuntimeError(f"无法从阶段 {state.phase.value} 恢复流水线修复任务")

    async def _maybe_start_coverage_continuation(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        state: PipelineRepairState,
        pipeline_id: int,
        pipeline_sha: str,
        failed_jobs: list[dict],
        coverage: CoverageResult,
        agent_state: dict,
    ) -> PipelineRepairState | None:
        """Run at most one report-driven test enhancement before terminal completion."""
        from pr_agent.config_loader import get_settings
        from pr_agent.git_providers import get_git_provider_with_context
        from pr_agent.triage.coverage_continuation import (
            decide_coverage_continuation,
            is_coverage_job,
            non_coverage_jobs,
        )
        from ut_agent.agent import UT_WORKSPACE
        from ut_agent.coverage_enhancement import CoverageEnhancementRequest, run_coverage_enhancement
        from ut_agent.tools.context import init_context
        from ut_agent.tools.fetch_coverage_report import fetch_changed_lines_report
        from ut_agent.workspace import prepare_workspace

        selected = {str(category) for category in state.selected_categories}
        coverage_jobs = [job for job in failed_jobs if is_coverage_job(job)]
        if (
            state.coverage_phase is not CoverageContinuationPhase.NOT_STARTED
            or state.coverage_attempts
            or not selected
            or not selected.issubset({RepairCategory.CLANG.value, RepairCategory.BUILD.value})
            or not coverage_jobs
            or non_coverage_jobs(failed_jobs)
        ):
            return state

        baseline = replace(
            state,
            coverage_baseline_pipeline_id=pipeline_id,
            coverage_baseline_sha=pipeline_sha,
            coverage_before=coverage.value,
            coverage_threshold=coverage.threshold,
            coverage_job_id=int(coverage.job_id or coverage_jobs[0].get("id") or coverage_jobs[0].get("job_id") or 0),
        )
        provider = get_git_provider_with_context(task.pr_url)
        init_context(git_provider=provider, output_dir=UT_WORKSPACE)
        report = await asyncio.to_thread(fetch_changed_lines_report, baseline.coverage_job_id)
        uncovered_count = sum(
            len(record.get("uncovered") or ())
            for record in report.get("files") or ()
            if isinstance(record, dict)
        )
        enabled = bool(get_settings().get("triage.coverage_continuation_enabled", True))
        max_attempts = min(1, max(0, int(
            get_settings().get("triage.coverage_continuation_max_attempts", 1)
        )))
        decision = decide_coverage_continuation(
            state=baseline,
            failed_jobs=failed_jobs,
            coverage=coverage,
            report_available=bool(report.get("available")),
            uncovered_line_count=uncovered_count,
            enabled=enabled,
            max_attempts=max_attempts,
        )
        if not decision.eligible:
            return replace(
                baseline,
                coverage_phase=CoverageContinuationPhase.COMPLETED,
                coverage_result="skipped",
                coverage_skip_reason=decision.message,
            )

        source_branch = str(provider.get_pr_branch() or "")
        mr_iid = task.mr.iid if task.mr is not None else int(agent_state.get("mr_id") or 0)
        snapshot = await asyncio.to_thread(
            prepare_workspace, provider, UT_WORKSPACE, mr_iid, source_branch
        )
        if snapshot.status != "ready":
            return replace(
                baseline,
                coverage_phase=CoverageContinuationPhase.COMPLETED,
                coverage_result="skipped",
                coverage_skip_reason=f"补测工作区未准备完成：{snapshot.message}",
            )

        running = replace(
            baseline,
            phase=PipelineRepairPhase.COVERAGE_RUNNING,
            coverage_phase=CoverageContinuationPhase.ENHANCING,
            coverage_attempts=1,
        )
        await self._record_pipeline_repair_state(task, lease, running)
        await self._record_owner_progress(
            task,
            "editing",
            "编译问题已修复，正在根据未覆盖代码补充单元测试",
            categories=tuple(state.selected_categories),
            job_names=(str(coverage_jobs[0].get("name") or ""),),
            metadata={"pipeline_id": pipeline_id, "coverage": coverage.value, "threshold": coverage.threshold},
        )
        await self._queue_pipeline_repair_progress(
            task,
            TriageCardState.REPAIR_RUNNING,
            "正在根据未覆盖代码补充单元测试（最多尝试 1 次）",
            pipeline_id,
            pipeline_sha,
        )
        tool_state = {
            **(agent_state or {}),
            "task_id": task.task_id,
            "project_id": task.mr.project_id if task.mr is not None else "",
            "mr_id": mr_iid,
            "source_branch": source_branch,
            "workspace_snapshot": snapshot.to_dict(),
            "require_workspace_snapshot": True,
        }
        request = CoverageEnhancementRequest(
            baseline.coverage_job_id,
            str(coverage_jobs[0].get("name") or "x86_64_ut_coverage_check"),
            float(coverage.value),
            float(coverage.threshold),
            mr_iid,
            source_branch,
        )
        result = await asyncio.to_thread(
            run_coverage_enhancement,
            request,
            tool_state,
            fetch_report=lambda _job_id: report,
        )
        if result.status != "pushed":
            return replace(
                running,
                phase=state.phase,
                coverage_phase=CoverageContinuationPhase.COMPLETED,
                coverage_result="skipped" if result.status == "skipped" else "failed",
                coverage_skip_reason=result.reason if result.status == "skipped" else "",
                coverage_failure_reason=result.reason if result.status != "skipped" else "",
            )

        waiting = replace(
            running,
            phase=PipelineRepairPhase.COVERAGE_WAITING,
            coverage_phase=CoverageContinuationPhase.WAITING,
            coverage_enhancement_sha=result.commit_sha,
            latest_pipeline_id=0,
            latest_pipeline_sha=result.commit_sha,
        )
        await self._record_pipeline_repair_state(task, lease, waiting)
        await self._queue_pipeline_repair_progress(
            task,
            TriageCardState.WAITING_PIPELINE,
            "补测提交已推送，正在等待新流水线",
            pipeline_id,
            result.commit_sha,
        )
        runtime = get_execution_runtime(required=True)
        project_id = task.mr.project_id if task.mr is not None else ""
        event = runtime.register_pipeline_wait_sync(
            project_id, result.commit_sha, attempt_id=f"repair-coverage:{result.commit_sha}"
        )
        if event is not None and event.terminal:
            await self._wait_for_complete_pipeline_group(task, event)
            await self._resume_coverage_continuation(task, lease, event, waiting)
            return None
        raise TaskSuspended(task.task_id, "pipeline", f"{project_id}:{result.commit_sha}")

    async def _resume_coverage_continuation(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        event: PipelineEvent,
        state: PipelineRepairState,
    ) -> None:
        categories, failed_jobs, coverage, explanations = await self._inspect_pipeline(task, event.pipeline_id)
        if event.status == "success" and not failed_jobs:
            completed = replace(
                state,
                coverage_phase=CoverageContinuationPhase.COMPLETED,
                coverage_result="succeeded",
                coverage_after=coverage.value,
            )
            await self._finish_pipeline_repair(
                task, lease, completed, event.pipeline_id, event.sha, event.status,
                categories, failed_jobs, coverage, confirmed_explanations=explanations,
            )
            return
        await self._start_coverage_rollback(
            task, lease, event, state, categories, failed_jobs, coverage, explanations
        )

    async def _start_coverage_rollback(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        event: PipelineEvent,
        state: PipelineRepairState,
        categories: list[RepairCategory],
        failed_jobs: list[dict],
        coverage: CoverageResult,
        explanations: tuple[FailureExplanation, ...],
    ) -> None:
        from datetime import datetime, timezone

        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        from pr_agent.triage.repair_rollback import RepairCommitEntry, RepairRollbackStatus
        from ut_agent.agent import UT_WORKSPACE
        from ut_agent.repair_rollback import TargetedRevertRequest, execute_targeted_commit_revert

        rolling = replace(
            state,
            phase=PipelineRepairPhase.COVERAGE_ROLLBACK_RUNNING,
            coverage_phase=CoverageContinuationPhase.ROLLING_BACK,
            coverage_after=coverage.value,
            coverage_failure_reason="补测提交触发的新流水线未通过。",
        )
        await self._record_pipeline_repair_state(task, lease, rolling)
        await self._queue_pipeline_repair_progress(
            task, TriageCardState.REPAIR_RUNNING, "补测未通过，正在撤回补测提交",
            event.pipeline_id, event.sha,
        )
        stored = await self.broker.get_task(task.task_id)
        manifest = stored.repair_commit_manifest if stored is not None else None
        target = next((
            entry for entry in (manifest.entries if manifest is not None else ())
            if entry.commit_sha == state.coverage_enhancement_sha
        ), None)
        if task.mr is None or target is None:
            await self._finish_coverage_manual_cleanup(
                task, lease, rolling, event, categories, failed_jobs, coverage, explanations,
                "缺少补测提交的可信记录，无法自动撤回。",
            )
            return
        provider = GitLabProvider(task.pr_url)
        source_project_id = getattr(provider.mr, "source_project_id", None) or provider.id_project
        source_project = provider.gl.projects.get(source_project_id)
        repository_url = str(getattr(source_project, "http_url_to_repo", "") or "")
        request = TargetedRevertRequest(
            task.mr.project_id,
            task.mr.iid,
            str(getattr(provider.mr, "source_branch", "") or ""),
            str(provider._prepare_clone_url_with_token(repository_url) or ""),
            task.task_id,
            target.commit_sha,
            target.parent_sha,
            target.task_marker,
        )
        result = await asyncio.to_thread(
            execute_targeted_commit_revert,
            request,
            UT_WORKSPACE,
            lambda: self.sync_broker.assert_fence(lease) if lease is not None else None,
        )
        if result.status is not RepairRollbackStatus.SUCCEEDED:
            await self._finish_coverage_manual_cleanup(
                task, lease, rolling, event, categories, failed_jobs, coverage, explanations,
                result.message or "补测提交无法安全撤回。",
            )
            return

        runtime = get_execution_runtime(required=True)
        if result.parent_sha == target.commit_sha:
            runtime.record_repair_commit_sync(
                RepairCommitEntry(
                    sequence=runtime.next_repair_commit_sequence_sync(),
                    commit_sha=result.rollback_commit_sha,
                    parent_sha=result.parent_sha,
                    tree_sha=result.tree_sha,
                    effect_id=f"coverage-revert:{target.commit_sha}",
                    task_marker=result.task_marker,
                    pushed_at=datetime.now(timezone.utc).isoformat(),
                ),
                parent_tree_sha=target.tree_sha,
                source_branch=request.source_branch,
            )
        waiting = replace(
            rolling,
            phase=PipelineRepairPhase.COVERAGE_ROLLBACK_WAITING,
            coverage_phase=CoverageContinuationPhase.ROLLBACK_WAITING,
            coverage_rollback_sha=result.rollback_commit_sha,
            latest_pipeline_id=0,
            latest_pipeline_sha=result.rollback_commit_sha,
        )
        await self._record_pipeline_repair_state(task, lease, waiting)
        await self._queue_pipeline_repair_progress(
            task, TriageCardState.WAITING_PIPELINE, "补测提交已撤回，正在等待撤回流水线",
            event.pipeline_id, result.rollback_commit_sha,
        )
        project_id = task.mr.project_id
        next_event = runtime.register_pipeline_wait_sync(
            project_id,
            result.rollback_commit_sha,
            attempt_id=f"repair-coverage-revert:{result.rollback_commit_sha}",
        )
        if next_event is not None and next_event.terminal:
            await self._wait_for_complete_pipeline_group(task, next_event)
            await self._resume_coverage_rollback(task, lease, next_event, waiting)
            return
        raise TaskSuspended(task.task_id, "pipeline", f"{project_id}:{result.rollback_commit_sha}")

    async def _resume_coverage_rollback(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        event: PipelineEvent,
        state: PipelineRepairState,
    ) -> None:
        categories, failed_jobs, coverage, explanations = await self._inspect_pipeline(task, event.pipeline_id)
        completed = replace(
            state,
            coverage_phase=CoverageContinuationPhase.COMPLETED,
            coverage_result="rolled_back",
            coverage_after=coverage.value,
        )
        await self._finish_pipeline_repair(
            task, lease, completed, event.pipeline_id, event.sha, event.status,
            categories, failed_jobs, coverage, confirmed_explanations=explanations,
        )

    async def _finish_coverage_manual_cleanup(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        state: PipelineRepairState,
        event: PipelineEvent,
        categories: list[RepairCategory],
        failed_jobs: list[dict],
        coverage: CoverageResult,
        explanations: tuple[FailureExplanation, ...],
        reason: str,
    ) -> None:
        completed = replace(
            state,
            coverage_phase=CoverageContinuationPhase.COMPLETED,
            coverage_result="manual_cleanup",
            coverage_failure_reason=reason,
        )
        await self._finish_pipeline_repair(
            task, lease, completed, event.pipeline_id, event.sha, event.status,
            categories, failed_jobs, coverage, confirmed_explanations=explanations,
        )

    async def _start_pipeline_format(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        source_pipeline_id: int,
        state: PipelineRepairState,
    ) -> None:
        from pr_agent.config_loader import get_settings
        from pr_agent.distributed.runtime import get_execution_runtime
        from pr_agent.tools.pr_fix_format import PRFixFormat

        max_rounds = max(1, int(get_settings().get("triage.format_max_rounds", 3)))
        if state.format_round >= max_rounds:
            categories, failed_jobs, coverage, confirmed_explanations = await self._inspect_pipeline(
                task,
                source_pipeline_id,
            )
            await self._finish_pipeline_repair(
                task,
                lease,
                state,
                source_pipeline_id,
                state.latest_pipeline_sha,
                "failed",
                categories,
                failed_jobs,
                coverage,
                f"格式自动修复已达到 {max_rounds} 轮上限。",
                confirmed_explanations=confirmed_explanations,
            )
            return
        running = replace(state, phase=PipelineRepairPhase.FORMAT_RUNNING, format_round=state.format_round + 1)
        await self._record_pipeline_repair_state(task, lease, running)
        await self._record_owner_progress(
            task,
            "editing",
            "正在读取格式报告并应用修复",
            categories=(RepairCategory.FORMAT.value,),
            job_names=running.failed_job_names,
            metadata={"pipeline_id": source_pipeline_id},
        )
        repair_items = None
        progress_markdown = "正在修复代码格式"
        if state.auto_format_cleanup:
            from pr_agent.triage.failure_categories import bind_auto_format_cleanup

            binding = await self.broker.get_task_triage_card(task.task_id)
            if binding is not None:
                failed_jobs = [{"name": name} for name in state.failed_job_names]
                repair_items = bind_auto_format_cleanup(
                    binding.repair_items,
                    task_id=task.task_id,
                    failed_jobs=failed_jobs,
                    pipeline_id=source_pipeline_id,
                    pipeline_sha=state.latest_pipeline_sha,
                )
            progress_markdown = "检测到格式问题，正在自动修复"
        await self._queue_pipeline_repair_progress(
            task,
            TriageCardState.REPAIR_RUNNING,
            progress_markdown,
            source_pipeline_id,
            state.latest_pipeline_sha,
            repair_items,
        )
        if source_pipeline_id:
            get_settings().set("PR_FIX_FORMAT.PIPELINE_ID", str(source_pipeline_id))
        result = await PRFixFormat(
            task.pr_url,
            seen_report_fingerprints=running.format_report_fingerprints,
        ).run(publish_result=False)
        format_jobs = tuple(name for name in state.failed_job_names if "format" in name.lower())
        format_action = RepairAction.from_dict({
            "action_id": f"format:{source_pipeline_id}:{result.pushed_sha or state.latest_pipeline_sha}",
            "categories": [RepairCategory.FORMAT.value],
            "job_names": format_jobs or ("code_format_check",),
            "root_cause": (
                "代码格式检查报告包含可自动应用的格式差异。"
                if result.fixed_files
                else result.failure_summary or "代码格式检查未产生可提交的修复。"
            ),
            "confidence": "confirmed",
            "measures": (
                ["应用流水线格式报告中的代码格式修复。"]
                if result.fixed_files
                else [result.suggested_action or "读取并检查流水线格式报告。"]
            ),
            "changed_files": result.fixed_files,
            "commit_sha": result.pushed_sha or "",
            "status": "committed" if result.pushed_sha else "no_changes",
            "failure_reason": "" if result.pushed_sha else result.status_markdown,
        })
        running = replace(
            running,
            repair_actions=merge_repair_actions(running.repair_actions, (format_action,)),
        )
        await self._record_owner_progress(
            task,
            "committing" if result.pushed_sha else "terminal",
            "格式修复提交已推送" if result.pushed_sha else "格式修复未产生可提交改动",
            categories=(RepairCategory.FORMAT.value,),
            job_names=format_jobs,
            metadata={
                "commit_sha": result.pushed_sha or "",
                "changed_files_count": len(result.fixed_files),
            },
        )
        if not result.pushed_sha:
            categories, failed_jobs, coverage, confirmed_explanations = await self._inspect_pipeline(
                task,
                source_pipeline_id,
            )
            await self._finish_pipeline_repair(
                task,
                lease,
                running,
                source_pipeline_id,
                state.latest_pipeline_sha,
                "failed" if categories else "success",
                categories,
                failed_jobs,
                coverage,
                result.status_markdown or "未产生可提交的格式修复。",
                confirmed_explanations=confirmed_explanations,
            )
            return
        self._reconcile_format_workspace(task, result.pushed_sha)
        waiting = replace(
            running,
            phase=PipelineRepairPhase.FORMAT_WAITING,
            root_pipeline_id=0,
            latest_pipeline_id=0,
            latest_pipeline_sha=result.pushed_sha,
            format_report_fingerprints=(
                (*running.format_report_fingerprints, result.report_fingerprint)
                if result.report_fingerprint
                else running.format_report_fingerprints
            ),
            format_last_exact_report_applied=result.exact_report_applied,
        )
        await self._record_pipeline_repair_state(task, lease, waiting)
        await self._record_owner_progress(
            task,
            "waiting_pipeline",
            "修复提交已推送，正在等待新流水线",
            categories=(RepairCategory.FORMAT.value,),
            metadata={"commit_sha": result.pushed_sha},
        )
        await self._queue_pipeline_repair_progress(
            task,
            TriageCardState.WAITING_PIPELINE,
            "等待格式修复触发的新流水线",
            source_pipeline_id,
            result.pushed_sha,
        )
        runtime = get_execution_runtime(required=True)
        project_id = task.mr.project_id if task.mr is not None else ""
        attempt_id = f"repair-format:{result.pushed_sha}"
        event = runtime.register_pipeline_wait_sync(project_id, result.pushed_sha, attempt_id=attempt_id)
        if event is not None and event.terminal:
            await self._resume_pipeline_repair(task, lease, event)
            return
        raise TaskSuspended(task.task_id, "pipeline", f"{project_id}:{result.pushed_sha}")

    async def _finish_pipeline_repair(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        state: PipelineRepairState,
        pipeline_id: int,
        pipeline_sha: str,
        pipeline_status: str,
        failed_categories: list[RepairCategory],
        failed_jobs: list[dict],
        coverage: CoverageResult,
        error: str = "",
        *,
        confirmed_explanations: tuple[FailureExplanation, ...] = (),
    ) -> None:
        binding = await self.broker.get_task_triage_card(task.task_id)
        if binding is None:
            return
        from pr_agent.triage.repair_outcome import (
            RepairOutcome,
            evaluate_repair_outcome,
            verified_selected_success_count,
        )
        from pr_agent.triage.repair_result_identity import resolve_repair_result_identity
        from pr_agent.triage.repair_rollback import RepairCommitManifest

        source_failed_names = tuple(state.source_failed_job_names)
        binding_failed_names = getattr(binding, "failed_job_names", ())
        if not source_failed_names and isinstance(binding_failed_names, (list, tuple, set)):
            source_failed_names = tuple(str(name) for name in binding_failed_names if str(name))
        if not source_failed_names:
            source_failed_names = tuple(
                name
                for item in binding.repair_items
                for name in item.failed_job_names
                if name
            )
        if not source_failed_names:
            source_failed_names = tuple(
                record.job_name for record in state.source_failure_explanations if record.job_name
            )
        selected_categories = tuple(state.selected_categories or state.effective_categories)
        if not selected_categories:
            from pr_agent.triage.failure_categories import classify_failed_jobs

            selected_categories = tuple(
                category.value
                for category in classify_failed_jobs({"name": name} for name in source_failed_names)
            )
        if not selected_categories:
            selected_categories = tuple(category.value for category in failed_categories)
        baseline_verified = bool(state.coverage_baseline_sha and state.coverage_result)
        validation_reliable = baseline_verified or (
            bool(pipeline_id)
            and pipeline_status in {"success", "failed"}
            and coverage.status not in {
                "validation_pipeline_missing",
                "fetch_failed",
            }
        )
        outcome_failed_jobs = [] if baseline_verified else failed_jobs
        verdict = evaluate_repair_outcome(
            source_failed_job_names=source_failed_names,
            validation_failed_jobs=outcome_failed_jobs,
            selected_categories=selected_categories,
            effective_categories=state.effective_categories,
            validation_reliable=validation_reliable,
            blocked_job_names=state.blocked_job_names,
        )
        selected_success_count = verified_selected_success_count(verdict.category_results)
        stored = await self.broker.get_task(task.task_id)
        manifest = getattr(stored, "repair_commit_manifest", None) if stored is not None else None
        has_repair_commits = isinstance(manifest, RepairCommitManifest) and bool(manifest.entries)
        result_identity = resolve_repair_result_identity(
            manifest,
            state.repair_actions,
            current_pipeline_id=pipeline_id,
            current_pipeline_sha=pipeline_sha,
            current_pipeline_status=pipeline_status,
        )
        result_pipeline_id = result_identity.pipeline_id
        result_pipeline_sha = result_identity.commit_sha
        proof_sha = state.terminal_proof_sha or pipeline_sha
        proof_pipeline_id = state.terminal_proof_pipeline_id or pipeline_id
        proof_status = state.terminal_proof_status or pipeline_status
        if has_repair_commits and (
            proof_sha != manifest.final_repair_sha
            or pipeline_sha != manifest.final_repair_sha
            or proof_pipeline_id != pipeline_id
            or proof_status != pipeline_status
        ):
            raise RuntimeError("terminal_pipeline_proof_mismatch")
        legacy_pipeline_success = not selected_categories and pipeline_status == "success" and not failed_jobs
        auto_rollback_required = selected_success_count == 0 and has_repair_commits and not legacy_pipeline_success
        success = verdict.outcome is RepairOutcome.SUCCESS or legacy_pipeline_success
        partial = verdict.outcome is RepairOutcome.PARTIAL_SUCCESS
        blocked = verdict.outcome is RepairOutcome.BLOCKED
        from pr_agent.triage.model_availability import (
            MODEL_SERVICE_UNAVAILABLE_MESSAGE,
            is_model_service_unavailable,
        )

        model_unavailable = (
            not success
            and not partial
            and not blocked
            and not has_repair_commits
            and is_model_service_unavailable(state.terminal_failure_kind, error)
        )
        public_error = MODEL_SERVICE_UNAVAILABLE_MESSAGE if model_unavailable else error
        repair_outcome_value = RepairOutcome.SUCCESS.value if legacy_pipeline_success else verdict.outcome.value
        card_pipeline_id = state.root_pipeline_id
        if not card_pipeline_id and pipeline_sha == binding.current_pipeline_sha:
            card_pipeline_id = binding.current_pipeline_id
        card_pipeline_id = card_pipeline_id or pipeline_id
        failed_names = [str((job or {}).get("name") or "") for job in failed_jobs]
        current_failed_names = {name for name in failed_names if name}
        explanations = tuple(
            record
            for record in merge_failure_explanations(confirmed_explanations, state.failure_explanations)
            if record.job_name in current_failed_names
        )
        if binding.repair_card_mode == "multi_select":
            from pr_agent.triage.failure_categories import reconcile_batch_repair_items

            items = reconcile_batch_repair_items(
                binding.repair_items,
                state.selected_categories,
                state.effective_categories,
                outcome_failed_jobs,
                pipeline_id,
                pipeline_sha,
                public_error,
                failure_explanations=explanations,
                category_results=verdict.category_results,
                result_pipeline_id=result_pipeline_id,
                result_pipeline_sha=result_pipeline_sha,
            )
            card_state = (
                TriageCardState.REPAIR_SUCCEEDED
                if success
                else TriageCardState.REPAIR_PARTIAL
                if partial
                else TriageCardState.REPAIR_BLOCKED
                if blocked
                else TriageCardState.REPAIR_MODEL_UNAVAILABLE
                if model_unavailable
                else TriageCardState.REPAIR_FAILED
            )
        else:
            item_status = (
                RepairItemStatus.SUCCEEDED
                if success
                else RepairItemStatus.BLOCKED
                if blocked
                else RepairItemStatus.FAILED
            )
            item_summary = (
                "编译修复成功，补测失败已撤回"
                if state.coverage_result == "rolled_back"
                else "流水线已通过"
                if success
                else "当前任务被外部依赖阻塞"
                if blocked
                else (public_error or "流水线仍有失败任务")
            )
            items = tuple(
                replace(
                    item,
                    status=item_status,
                    result_pipeline_id=result_pipeline_id,
                    result_pipeline_sha=result_pipeline_sha,
                    status_markdown=item_summary,
                    failure_explanations=tuple(
                        record
                        for record in explanations
                        if item.category is RepairCategory.PIPELINE or record.job_name in item.failed_job_names
                    ),
                )
                if item.task_id == task.task_id or item.category is RepairCategory.PIPELINE
                else item
                for item in binding.repair_items
            )
            card_state = (
                TriageCardState.REPAIR_SUCCEEDED
                if success
                else TriageCardState.REPAIR_PARTIAL
                if partial
                else TriageCardState.REPAIR_BLOCKED
                if blocked
                else TriageCardState.REPAIR_MODEL_UNAVAILABLE
                if model_unavailable
                else TriageCardState.REPAIR_FAILED
            )
        coverage_value = coverage.value
        coverage_headlines = {
            "succeeded": "编译修复成功，覆盖率补测成功",
            "skipped": "编译修复成功，未执行覆盖率补测",
            "failed": "编译修复成功，覆盖率补测未完成",
            "rolled_back": "编译修复成功，覆盖率补测失败，已撤回补测提交",
            "manual_cleanup": "编译修复成功，但补测提交无法安全撤回，请人工处理",
        }
        lines = [coverage_headlines.get(
            state.coverage_result,
            "所选问题修复成功"
            if success
            else "所选问题部分修复成功"
            if partial
            else "外部依赖阻塞"
            if blocked
            else MODEL_SERVICE_UNAVAILABLE_MESSAGE
            if model_unavailable
            else "所选问题修复失败",
        )]
        selected_results = [result for result in verdict.category_results if result.selection == "selected"]
        unselected_results = [
            result
            for result in verdict.category_results
            if result.selection == "not_selected" and result.validation_failed_job_names
        ]
        if selected_results:
            lines.extend(["", "**本次选择**"])
            labels = {"format": "Format", "clang": "Clang", "build": "Build", "unknown": "Unknown"}
            result_labels = {
                "succeeded": "修复成功",
                "blocked": "外部依赖阻塞",
                "failed": "模型服务不可用" if model_unavailable else "修复失败",
                "unverified": "无法确认",
            }
            lines.extend(
                f"- {labels.get(result.category, result.category)}："
                f"{result_labels.get(result.outcome.value, result.outcome.value)}"
                for result in selected_results
            )
        if unselected_results:
            lines.extend(["", "**本次未选择**"])
            lines.extend(
                f"- {labels.get(result.category, result.category)}：仍然失败"
                for result in unselected_results
            )
        if verdict.introduced_failed_job_names:
            lines.extend(["", "**修复过程出现新问题**"])
            lines.append(f"- {', '.join(verdict.introduced_failed_job_names)}")
        lines.extend(["", f"**整体流水线**：{'已通过' if pipeline_status == 'success' else '仍未通过'}"])
        if state.completed_steps:
            lines.append(f"- 已完成步骤: {'、'.join(state.completed_steps)}")
        if result_pipeline_id:
            lines.extend([
                f"- 结果 Pipeline: `#{result_pipeline_id}`",
                f"- 修复 Commit: `{result_pipeline_sha[:12]}`",
            ])
        else:
            lines.extend([
                f"- 当前失败 Pipeline: `#{pipeline_id}`" if pipeline_id else "- 当前失败 Pipeline: `unknown`",
                "- 本次未产生修复提交",
            ])
        lines.append(f"- Coverage: {coverage_value:g}%" if coverage_value is not None else "- Coverage: 未提供")
        if state.coverage_threshold is not None:
            lines.append(f"- Coverage threshold: {state.coverage_threshold:g}%")
        coverage_reason = state.coverage_failure_reason or state.coverage_skip_reason
        if coverage_reason:
            lines.append(f"- 补测说明: {coverage_reason}")
        if failed_names:
            lines.append(f"- 剩余失败 jobs: {', '.join(failed_names)}")
        if public_error and not model_unavailable:
            lines.append(f"- 原因: {public_error}")
        terminal = replace(
            state,
            phase=PipelineRepairPhase.TERMINAL,
            root_pipeline_id=card_pipeline_id,
            latest_pipeline_id=pipeline_id,
            latest_pipeline_sha=pipeline_sha,
            terminal_attempt_id=state.terminal_attempt_id,
            terminal_proof_sha=proof_sha,
            terminal_proof_pipeline_id=proof_pipeline_id,
            terminal_proof_status=proof_status,
            final_pipeline_status=pipeline_status,
            final_coverage=coverage_value,
            final_coverage_source=coverage.source,
            final_coverage_status=coverage.status,
            failed_job_names=tuple(name for name in failed_names if name),
            terminal_error=error,
            source_failed_job_names=source_failed_names,
            selected_categories=selected_categories,
            failure_explanations=explanations,
            repair_outcome=repair_outcome_value,
            category_results=verdict.category_results,
            introduced_failure_categories=verdict.introduced_failure_categories,
            introduced_failed_job_names=verdict.introduced_failed_job_names,
            verified_selected_success_count=selected_success_count,
            auto_rollback_required=auto_rollback_required,
            repair_actions=self._validated_repair_actions(
                state.repair_actions,
                pipeline_id,
                pipeline_sha,
                "success" if baseline_verified else pipeline_status,
                [] if baseline_verified else failed_categories,
                error,
            ),
        )
        await self._record_pipeline_repair_state(task, lease, terminal)
        await self._record_owner_progress(
            task,
            "terminal",
            (
                "所选问题修复成功"
                if success
                else "所选问题部分修复成功"
                if partial
                else "外部依赖阻塞"
                if blocked
                else "模型服务不可用"
                if model_unavailable
                else "所选问题修复失败"
            ),
            categories=tuple(category.value for category in failed_categories),
            job_names=tuple(name for name in failed_names if name),
            metadata={
                "pipeline_id": pipeline_id,
                "commit_sha": pipeline_sha,
                "pipeline_status": pipeline_status,
                "repair_outcome": repair_outcome_value,
                "coverage": coverage_value if coverage_value is not None else "",
                "coverage_source": coverage.source,
                "coverage_status": coverage.status,
            },
        )
        reconciliation_state = TriageCardState.REPAIR_RUNNING if auto_rollback_required else card_state
        reconciliation_markdown = (
            "修复未成功，正在撤回本次自动修改"
            if auto_rollback_required
            else "\n".join(lines)
        )
        await queue_repair_reconciliation(
            self.broker,
            task.task_id,
            items,
            reconciliation_state,
            reconciliation_markdown,
            card_pipeline_id,
            pipeline_sha,
            post_repair_ut_coverage=coverage_value,
            post_repair_ut_coverage_status=coverage.status,
        )

    async def _inspect_pipeline(self, task: TaskEnvelope, pipeline_id: int):
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        from pr_agent.triage.failure_categories import classify_failed_jobs, collect_failed_jobs
        from ut_agent.tools.pipeline_group import required_pipeline_job_patterns, resolve_pipeline_group

        if not pipeline_id:
            return [RepairCategory.UNKNOWN], [], CoverageResult(status="validation_pipeline_missing"), ()
        try:
            provider = GitLabProvider(task.pr_url)
            project = provider.gl.projects.get(provider.id_project)
            pipeline = project.pipelines.get(pipeline_id)
            group = resolve_pipeline_group(
                project,
                pipeline,
                required_job_patterns=required_pipeline_job_patterns(),
                exact_sha=str(getattr(pipeline, "sha", "") or ""),
            )
            validation_pipeline_id = int(group.validation_pipeline_id or 0)
            if not validation_pipeline_id:
                return [RepairCategory.UNKNOWN], [], CoverageResult(status="validation_pipeline_missing"), ()
            failed_jobs = collect_failed_jobs(project, validation_pipeline_id)
            explanations = collect_gitlab_failure_explanations(project, failed_jobs, validation_pipeline_id)
            from pr_agent.triage.pipeline_coverage import resolve_pipeline_coverage

            coverage = resolve_pipeline_coverage(
                project,
                group.validation_pipeline,
                group.jobs,
                required_pipeline_job_patterns(),
            )
            return classify_failed_jobs(failed_jobs), failed_jobs, coverage, explanations
        except Exception as error:
            get_logger().warning(f"无法读取流水线 #{pipeline_id} 的失败任务: {error}")
            return [RepairCategory.UNKNOWN], [], CoverageResult(status="fetch_failed"), ()

    @staticmethod
    def _pipeline_completion_snapshot(task: TaskEnvelope, pipeline_id: int, pipeline_sha: str):
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        from pr_agent.triage.pipeline_completion import inspect_pipeline_completion
        from ut_agent.tools.pipeline_group import required_pipeline_job_patterns

        provider = GitLabProvider(task.pr_url)
        project = provider.gl.projects.get(provider.id_project)
        pipeline = project.pipelines.get(pipeline_id)
        return inspect_pipeline_completion(
            project,
            pipeline,
            required_job_patterns=required_pipeline_job_patterns(),
            exact_sha=pipeline_sha,
        )

    async def _wait_for_complete_pipeline_group(self, task: TaskEnvelope, event: PipelineEvent) -> None:
        """Treat one terminal webhook as a wake-up, then verify the whole causal pipeline group."""
        try:
            first = await asyncio.to_thread(
                self._pipeline_completion_snapshot,
                task,
                event.pipeline_id,
                event.sha,
            )
        except Exception as error:
            get_logger().warning(
                f"Unable to inspect complete pipeline group: task_id={task.task_id}, "
                f"pipeline_id={event.pipeline_id}, error={error}"
            )
            await self._queue_pipeline_repair_progress(
                task,
                TriageCardState.WAITING_PIPELINE,
                "正在等待完整流水线状态，暂不发送最终结果",
                event.pipeline_id,
                event.sha,
            )
            raise TaskSuspended(task.task_id, "pipeline_group", f"{event.project_id}:{event.sha}") from error

        if first.terminal:
            from pr_agent.config_loader import get_settings

            delay = max(0.0, float(get_settings().get("triage.pipeline_terminal_stabilization_seconds", 2.0)))
            if delay:
                await asyncio.sleep(delay)
            second = await asyncio.to_thread(
                self._pipeline_completion_snapshot,
                task,
                event.pipeline_id,
                event.sha,
            )
            if second.terminal and second.digest == first.digest:
                return
            first = second

        if first.validation_pipeline_id and first.validation_pipeline_id != event.pipeline_id:
            runtime = get_execution_runtime(required=True)
            cached = runtime.register_pipeline_wait_sync(
                event.project_id,
                event.sha,
                attempt_id=f"pipeline-group:{first.validation_pipeline_id}:{first.digest}",
                pipeline_id=first.validation_pipeline_id,
            )
            if cached is not None and cached.terminal:
                await self._wait_for_complete_pipeline_group(task, cached)
                return
        await self._queue_pipeline_repair_progress(
            task,
            TriageCardState.WAITING_PIPELINE,
            first.reason or "仍有流水线或关键 Job 正在运行，继续等待",
            first.validation_pipeline_id or event.pipeline_id,
            event.sha,
        )
        raise TaskSuspended(
            task.task_id,
            "pipeline_group",
            f"{event.project_id}:{event.sha}:{first.validation_pipeline_id or event.pipeline_id}",
        )

    async def _record_pipeline_repair_state(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        state: PipelineRepairState,
    ) -> None:
        changed = await self.broker.record_pipeline_repair_state(task.task_id, state, lease)
        if not changed:
            raise RuntimeError(f"无法保存流水线修复阶段: {state.phase.value}")

    @staticmethod
    def _with_format_cleanup(state: PipelineRepairState) -> PipelineRepairState:
        effective = list(state.effective_categories or state.selected_categories)
        if RepairCategory.FORMAT.value not in effective:
            effective.append(RepairCategory.FORMAT.value)
        return replace(
            state,
            effective_categories=tuple(effective),
            auto_format_cleanup=RepairCategory.FORMAT.value not in state.selected_categories,
        )

    @staticmethod
    def _state_with_failure_explanations(
        state: PipelineRepairState,
        raw_records,
    ) -> PipelineRepairState:
        by_job = {record.job_name: record for record in state.failure_explanations}
        for value in raw_records or ():
            if not isinstance(value, dict):
                continue
            record = FailureExplanation.from_dict(value)
            by_job[record.job_name] = record
        return replace(state, failure_explanations=tuple(by_job.values()))

    @staticmethod
    def _state_with_repair_actions(
        state: PipelineRepairState,
        raw_records,
    ) -> PipelineRepairState:
        records = [record for record in raw_records or () if isinstance(record, dict)]
        return replace(state, repair_actions=merge_repair_actions(state.repair_actions, records))

    @staticmethod
    def _state_with_dependency_blockers(
        state: PipelineRepairState,
        raw_records,
    ) -> PipelineRepairState:
        allowed_evidence_keys = {
            "project_path",
            "declared_branch",
            "declared_sha",
            "package_path",
            "queries",
            "current_branch",
            "candidate_kind",
            "verified_candidates",
            "partial_candidates",
            "checked_branch_count",
            "catalog_truncated",
        }
        def clean_candidate(value: object) -> dict[str, Any] | None:
            if not isinstance(value, dict):
                return None
            paths = value.get("file_paths") if isinstance(value.get("file_paths"), dict) else {}
            return {
                "branch": sanitize_failure_text(value.get("branch"), 300),
                "resolved_sha": sanitize_failure_text(value.get("resolved_sha"), 100),
                "verification_complete": bool(value.get("verification_complete")),
                "matched_queries": [
                    sanitize_failure_text(item, 200) for item in value.get("matched_queries") or ()
                ][:20],
                "missing_queries": [
                    sanitize_failure_text(item, 200) for item in value.get("missing_queries") or ()
                ][:20],
                "file_paths": {
                    sanitize_failure_text(name, 200): sanitize_failure_text(path, 1_000)
                    for name, path in list(paths.items())[:20]
                },
            }

        def clean_evidence(value: object) -> dict[str, Any] | None:
            if not isinstance(value, dict):
                return None
            evidence = {
                key: value[key]
                for key in allowed_evidence_keys
                if key in value
            }
            for key in ("project_path", "declared_branch", "declared_sha", "package_path", "candidate_kind"):
                evidence[key] = sanitize_failure_text(evidence.get(key), 1_000)
            evidence["queries"] = [
                {"filename": sanitize_failure_text(item.get("filename"), 200)}
                for item in evidence.get("queries") or ()
                if isinstance(item, dict) and sanitize_failure_text(item.get("filename"), 200)
            ][:20]
            evidence["current_branch"] = clean_candidate(evidence.get("current_branch")) or {}
            for key in ("verified_candidates", "partial_candidates"):
                evidence[key] = [
                    candidate
                    for candidate in (clean_candidate(item) for item in evidence.get(key) or ())
                    if candidate is not None
                ][:5]
            evidence["checked_branch_count"] = min(
                max(int(evidence.get("checked_branch_count") or 0), 0),
                10_000,
            )
            evidence["catalog_truncated"] = bool(evidence.get("catalog_truncated"))
            return evidence

        summaries = [state.blocker_summary] if state.blocker_summary else []
        suggested_actions = [state.blocker_suggested_action] if state.blocker_suggested_action else []
        blocked_job_names = list(state.blocked_job_names)
        evidence_records = list(state.dependency_evidence)
        for value in list(raw_records or ())[:20]:
            if not isinstance(value, dict) or value.get("blocker_type") != "external_dependency":
                continue
            root_cause_id = sanitize_failure_text(value.get("root_cause_id"), 200)
            job_name = sanitize_failure_text(value.get("job_name"), 120)
            root_cause = sanitize_failure_text(value.get("root_cause"), 1_000)
            suggested_action = sanitize_failure_text(value.get("suggested_action"), 1_000)
            if not root_cause_id or not job_name or not root_cause or not suggested_action:
                continue
            if job_name not in blocked_job_names:
                blocked_job_names.append(job_name)
            if root_cause not in summaries:
                summaries.append(root_cause)
            if suggested_action not in suggested_actions:
                suggested_actions.append(suggested_action)
            evidence = clean_evidence(value.get("dependency_evidence"))
            if evidence is not None and evidence not in evidence_records and len(evidence_records) < 20:
                evidence_records.append(evidence)
        if not blocked_job_names:
            return state
        return replace(
            state,
            blocker_type="external_dependency",
            blocker_summary=sanitize_failure_text("；".join(summaries), 2_000),
            blocker_suggested_action=sanitize_failure_text("；".join(suggested_actions), 2_000),
            blocked_job_names=tuple(blocked_job_names),
            dependency_evidence=tuple(evidence_records),
        )

    @staticmethod
    def _validated_repair_actions(
        actions: tuple[RepairAction, ...],
        pipeline_id: int,
        pipeline_sha: str,
        pipeline_status: str,
        failed_categories: list[RepairCategory],
        error: str,
    ) -> tuple[RepairAction, ...]:
        failed_values = {category.value for category in failed_categories}
        output = []
        for action in actions:
            if action.status not in {"editing", "committed", "verified"}:
                output.append(action)
                continue
            action_failed = bool(set(action.categories) & failed_values)
            verified = pipeline_status == "success" or not action_failed
            output.append(replace(
                action,
                validation_pipeline_id=pipeline_id,
                validation_status=pipeline_status,
                status="verified" if verified else "failed",
                failure_reason="" if verified else (error or "验证流水线仍有该类失败任务。"),
                commit_sha=action.commit_sha or pipeline_sha,
            ))
        return tuple(output)

    async def _pipeline_repair_wait_status(self, task: TaskEnvelope, wait_identity: str) -> str:
        stored = await self.broker.get_task(task.task_id)
        state = stored.pipeline_repair_state if stored is not None else PipelineRepairState()
        if state.phase is PipelineRepairPhase.FORMAT_WAITING:
            return f"等待格式修复触发的新流水线\n\n`{wait_identity}`"
        if state.phase is PipelineRepairPhase.COVERAGE_WAITING:
            return f"等待覆盖率补测触发的新流水线\n\n`{wait_identity}`"
        if state.phase is PipelineRepairPhase.COVERAGE_ROLLBACK_WAITING:
            return f"等待补测撤回触发的新流水线\n\n`{wait_identity}`"
        return f"等待修复提交触发的新流水线\n\n`{wait_identity}`"

    async def _queue_pipeline_repair_progress(
        self,
        task: TaskEnvelope,
        state: TriageCardState,
        status_markdown: str,
        current_pipeline_id: int,
        current_pipeline_sha: str,
        repair_items: tuple[RepairItem, ...] | None = None,
    ) -> None:
        if task.source != "feishu":
            return
        try:
            await queue_pipeline_repair_progress(
                self.broker,
                task.task_id,
                state,
                status_markdown,
                current_pipeline_id,
                current_pipeline_sha,
                repair_items,
            )
        except Exception:
            get_logger().exception(f"Failed to queue pipeline repair progress task_id={task.task_id}")

    async def _record_owner_progress(
        self,
        task: TaskEnvelope,
        phase: str,
        summary: str,
        *,
        categories: tuple[str, ...] = (),
        job_names: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> None:
        recorder = getattr(self.broker, "append_repair_progress", None)
        if not callable(recorder):
            return
        try:
            await recorder(RepairProgressEvent.new(
                task.task_id,
                phase,
                summary,
                categories=categories,
                job_names=job_names,
                metadata=metadata,
            ))
        except Exception:
            get_logger().warning(f"Failed to record owner repair progress task_id={task.task_id}")

    @staticmethod
    def _append_step(steps: tuple[str, ...], step: str) -> tuple[str, ...]:
        return steps if step in steps else (*steps, step)

    @staticmethod
    def _state_with_triage_iterations(
        state: PipelineRepairState,
        result: dict | None,
    ) -> PipelineRepairState:
        fields = result.get("result", {}) if isinstance(result, dict) else {}
        return replace(
            state,
            iterations=int(fields.get("iterations") or state.iterations or 0),
            max_iterations=int(fields.get("max_iterations") or state.max_iterations or 0),
        )

    @staticmethod
    def _state_with_triage_terminal_proof(
        state: PipelineRepairState,
        result: dict | None,
    ) -> PipelineRepairState:
        fields = result.get("result", {}) if isinstance(result, dict) else {}
        proof = fields.get("terminal_proof") if isinstance(fields.get("terminal_proof"), dict) else {}
        if not proof:
            return state
        return replace(
            state,
            terminal_attempt_id=str(proof.get("attempt_id") or ""),
            terminal_proof_sha=str(proof.get("commit_sha") or ""),
            terminal_proof_pipeline_id=int(proof.get("pipeline_id") or 0),
            terminal_proof_status=str(proof.get("status") or ""),
        )

    async def _resume_fix_format(self, task: TaskEnvelope, event: PipelineEvent) -> None:
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        from pr_agent.triage.failure_categories import (
            classify_failed_jobs,
            collect_failed_jobs,
            reconcile_repair_items,
        )

        binding = await self.broker.get_task_triage_card(task.task_id)
        if binding is None:
            return
        target = RepairCategory(task.payload.get("repair_category") or binding.active_category or "format")
        failed_categories = []
        error = ""
        if event.status == "failed":
            try:
                provider = GitLabProvider(task.pr_url)
                project = provider.gl.projects.get(provider.id_project)
                failed_categories = classify_failed_jobs(collect_failed_jobs(project, event.pipeline_id))
            except Exception as exc:
                failed_categories = [target]
                error = f"无法读取验证流水线失败任务：{exc}"
        elif event.status != "success":
            failed_categories = [target]
            error = f"验证流水线状态为 {event.status}"
        items = reconcile_repair_items(
            binding.repair_items,
            target,
            failed_categories,
            event.pipeline_id,
            event.sha,
            error,
        )
        if not failed_categories:
            state = TriageCardState.REPAIR_SUCCEEDED
            status = f"格式修复完成，流水线 #{event.pipeline_id} 已通过。"
        elif target not in failed_categories:
            state = TriageCardState.PIPELINE_FAILED
            remaining = "、".join(category.value for category in failed_categories)
            status = f"格式修复完成，但流水线仍有其他失败：{remaining}。"
        else:
            state = TriageCardState.PIPELINE_FAILED
            status = error or f"格式修复后流水线 #{event.pipeline_id} 仍未通过格式检查。"
        await queue_repair_reconciliation(
            self.broker,
            task.task_id,
            items,
            state,
            status,
            event.pipeline_id,
            event.sha,
        )

    async def _fail_active_repair(self, task: TaskEnvelope, error: str) -> None:
        binding = await self.broker.get_task_triage_card(task.task_id)
        if binding is None:
            return
        items = tuple(
            replace(item, status=RepairItemStatus.FAILED, status_markdown=error)
            if item.task_id == task.task_id
            else item
            for item in binding.repair_items
        )
        await queue_repair_reconciliation(
            self.broker,
            task.task_id,
            items,
            TriageCardState.PIPELINE_FAILED,
            error,
            binding.current_pipeline_id,
            binding.current_pipeline_sha,
        )

    @staticmethod
    def _pipeline_resume_markdown(event: PipelineEvent) -> str:
        localized_statuses = {
            "failed": "已失败",
            "success": "已成功",
            "canceled": "已取消",
            "skipped": "已跳过",
        }
        status = localized_statuses.get(event.status, f"状态为 {event.status}")
        return f"流水线 #{event.pipeline_id} {status}，正在分析流水线结果并决定下一步……"

    @staticmethod
    async def _resume_triage(
        task: TaskEnvelope,
        event: PipelineEvent,
        *,
        publish_result: bool = True,
        persist_result: bool = True,
    ):
        from pr_agent.tools.pr_triage import PRTriage

        return await PRTriage(task.pr_url).resume(
            task.task_id,
            event,
            publish_result=publish_result,
            persist_result=persist_result,
        )
