import asyncio
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pr_agent.config_loader import get_settings, task_settings_context
from pr_agent.distributed.broker import (
    AutoWorkflowCursor,
    EffectRecord,
    MrLease,
    RepairRollbackUnavailable,
    RollbackRequestResult,
    StoredTask,
)
from pr_agent.distributed.executor import TaskExecutor
from pr_agent.distributed.models import (
    AutoWorkflowDecision,
    MrKey,
    PipelineEvent,
    RepairCategory,
    RepairItemStatus,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardState,
)
from pr_agent.distributed.runtime import ExecutionRuntime, TaskSuspended, execution_context
from pr_agent.tools.pr_fix_format import FixFormatResult
from pr_agent.triage.failure_categories import pipeline_repair_item, repair_items_for_failed_jobs
from pr_agent.triage.failure_explanations import FailureExplanation
from pr_agent.triage.pipeline_coverage import CoverageResult
from pr_agent.triage.pipeline_repair import (
    CoverageContinuationPhase,
    PipelineRepairPhase,
    PipelineRepairState,
)
from pr_agent.triage.repair_details import RepairAction
from pr_agent.triage.repair_rollback import RepairCommitEntry, RepairCommitManifest
from ut_agent.model_failover import LLMCallOutcome, ModelAttempt


def make_task(kind=TaskKind.PR_COMMAND, command="/review", payload=None):
    return TaskEnvelope.new(
        kind=kind,
        source="gitlab",
        mr=MrKey("eabot/cook", 536),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        command=command,
        payload=payload or {},
        idempotency_key="note:executor",
    )


def repair_manifest_for_task(task: TaskEnvelope) -> RepairCommitManifest:
    base_sha = "a" * 40
    repair_sha = "b" * 40
    return RepairCommitManifest(
        repair_task_id=task.task_id,
        project_id="eabot/cook",
        mr_iid=536,
        source_branch="feature/repair",
        base_commit_sha=base_sha,
        base_tree_sha="c" * 40,
        authorized_actor_id="owner",
        entries=(RepairCommitEntry(1, repair_sha, base_sha, "d" * 40, "effect", "marker", "now"),),
    )


def test_concurrent_tasks_have_isolated_settings():
    async def read_after_yield(value: str) -> str:
        with task_settings_context() as settings:
            settings.set("config.response_language", value)
            await asyncio.sleep(0)
            return get_settings().get("config.response_language")

    async def run_test():
        result = await asyncio.gather(read_after_yield("zh-cn"), read_after_yield("en-us"))
        assert result == ["zh-cn", "en-us"]

    asyncio.run(run_test())


def test_report_child_uses_final_compare_without_mr_lease():
    async def run_test():
        base_sha = "a" * 40
        final_sha = "b" * 40
        original_task = make_task(command="/repair-pipeline")
        manifest = RepairCommitManifest(
            repair_task_id=original_task.task_id,
            project_id="eabot/cook",
            mr_iid=536,
            source_branch="fix/report",
            base_commit_sha=base_sha,
            base_tree_sha="c" * 40,
            authorized_actor_id="owner",
            entries=(RepairCommitEntry(1, final_sha, base_sha, "d" * 40, "effect", "marker", "now"),),
            frozen=True,
            frozen_at="now",
        )
        stored = StoredTask(
            original_task,
            TaskStatus.COMPLETED,
            1,
            "worker-1",
            1,
            "",
            "",
            pipeline_repair_state=PipelineRepairState(
                phase=PipelineRepairPhase.TERMINAL,
                latest_pipeline_id=10,
                latest_pipeline_sha=final_sha,
                final_pipeline_status="success",
                selected_categories=("build",),
                failed_job_names=("build",),
            ),
            repair_commit_manifest=manifest,
        )
        report_task = replace(
            make_task(kind=TaskKind.REPAIR_REPORT, command="/summarize-repair"),
            mr=None,
            payload={"repair_task_id": original_task.task_id},
        )
        broker = AsyncMock()
        broker.get_task.return_value = stored
        broker.get_task_triage_card.return_value = None
        broker.set_final_repair_report_state.return_value = True
        broker.claim_effect.return_value = EffectRecord("started", {})
        broker.complete_effect.return_value = True
        broker.complete_final_repair_report.return_value = True
        project = Mock()
        project.repository_compare.return_value = {
            "commits": [{"id": final_sha}],
            "diffs": [{
                "old_path": "src/a.cpp",
                "new_path": "src/a.cpp",
                "diff": "@@ -1 +1 @@\n-return false;\n+return true;",
            }],
        }
        provider = Mock()
        provider.gl.projects.get.return_value = project
        response = LLMCallOutcome(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "report-1",
                    "type": "function",
                    "function": {
                        "name": "submit_final_repair_report",
                        "arguments": (
                            '{"schema_version":1,'
                            '"root_cause_summary":"构建过程中返回值不正确，导致编译失败。",'
                            '"solution_summary":"将错误的返回值改为正确值。",'
                            '"rationale":"该修改直接修正了触发构建错误的返回逻辑。",'
                            '"file_explanations":[{"path":"src/a.cpp","summary":"修正函数返回值。",'
                            '"evidence":["return true;"]}]}'
                        ),
                    },
                }],
            },
            "anthropic/claude-sonnet-5",
            (ModelAttempt("anthropic/claude-sonnet-5"),),
        )
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        with (
            patch("pr_agent.git_providers.gitlab_provider.GitLabProvider", return_value=provider),
            patch("ut_agent.llm.call_tool_llm_outcome", AsyncMock(return_value=response)),
            patch("pr_agent.triage.terminal.persist_repair_terminal", AsyncMock(return_value=True)),
        ):
            await executor._run_final_repair_report(report_task)

        project.repository_compare.assert_called_once_with(base_sha, final_sha)
        broker.complete_final_repair_report.assert_awaited_once()
        assert broker.complete_final_repair_report.await_args.args[1].final_sha == final_sha
        assert broker.complete_final_repair_report.await_args.args[2].report.source == "model"

    asyncio.run(run_test())


def test_executor_runs_auto_workflow_in_frozen_order():
    async def run_test():
        broker = AsyncMock()
        broker.transition_task.return_value = True
        broker.record_task_result.return_value = True
        broker.get_auto_cursor.return_value = AutoWorkflowCursor()
        broker.has_pending_triage.return_value = False
        broker.settings.auto_pause_at_command_boundary = True
        sync_broker = Mock()
        agent = Mock()
        agent.handle_request = AsyncMock(return_value=True)
        executor = TaskExecutor(
            broker,
            sync_broker,
            "worker-1",
            max_active_tasks=4,
            agent_factory=Mock(return_value=agent),
        )
        task = make_task(
            kind=TaskKind.AUTO_WORKFLOW,
            command="/auto",
            payload={"commands": ["/describe", "/review", "/improve"]},
        )
        lease = MrLease(task.mr, "worker-1", 7)

        await executor.execute(task, lease)

        assert [call.args[1] for call in agent.handle_request.await_args_list] == ["/describe", "/review", "/improve"]
        broker.transition_task.assert_any_await(task.task_id, {TaskStatus.ASSIGNED}, TaskStatus.RUNNING, lease)
        broker.transition_task.assert_any_await(task.task_id, {TaskStatus.PUBLISHING}, TaskStatus.COMPLETED, lease)

    asyncio.run(run_test())


def test_auto_workflow_records_failure_before_improve():
    async def run_test():
        broker = AsyncMock()
        broker.get_auto_cursor.return_value = AutoWorkflowCursor()
        broker.has_pending_triage.return_value = False
        broker.settings.auto_pause_at_command_boundary = False
        jobs = Mock()
        jobs.prepare_auto_workflow = AsyncMock(side_effect=RuntimeError("settings rejected"))
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1, webhook_jobs=jobs)
        task = make_task(kind=TaskKind.AUTO_WORKFLOW, command="/auto", payload={"commands": ["/mr_create"]})
        lease = MrLease(task.mr, "worker-1", 7)
        with (
            patch("pr_agent.distributed.executor.get_review_run_for_task", return_value={"run_id": "run-1"}),
            patch("pr_agent.distributed.executor.get_review_run", return_value={"improve_started_at": None}),
            patch("pr_agent.distributed.executor.finish_review_run") as finish,
            patch("pr_agent.distributed.executor.record_review_event") as event,
            pytest.raises(RuntimeError, match="settings rejected"),
        ):
            await executor._run_auto_workflow(task, lease)
        finish.assert_called_once_with(
            "failed", "run-1", stage="startup_failed", error_code="RuntimeError",
            error_message="settings rejected",
        )
        assert event.call_args.kwargs["status"] == "failed"

    asyncio.run(run_test())


def test_auto_workflow_records_policy_skip_without_starting_agent():
    async def run_test():
        broker = AsyncMock()
        broker.get_auto_cursor.return_value = AutoWorkflowCursor()
        broker.has_pending_triage.return_value = False
        broker.settings.auto_pause_at_command_boundary = False
        jobs = Mock()
        jobs.prepare_auto_workflow = AsyncMock(return_value=AutoWorkflowDecision.skip(
            "ignored_label", "Ignored by label rule: no-ai",
        ))
        agent = Mock()
        agent.handle_request = AsyncMock(return_value=True)
        executor = TaskExecutor(
            broker,
            Mock(),
            "worker-1",
            max_active_tasks=1,
            webhook_jobs=jobs,
            agent_factory=Mock(return_value=agent),
        )
        task = make_task(kind=TaskKind.AUTO_WORKFLOW, command="/auto", payload={"commands": ["/mr_create"]})
        lease = MrLease(task.mr, "worker-1", 7)
        with (
            patch("pr_agent.distributed.executor.get_review_run_for_task", return_value={"run_id": "run-1"}),
            patch("pr_agent.distributed.executor.finish_review_run") as finish,
            patch("pr_agent.distributed.executor.record_review_event") as event,
        ):
            await executor._run_auto_workflow(task, lease)

        finish.assert_called_once_with(
            "skipped",
            "run-1",
            stage="skipped",
            error_code="ignored_label",
            error_message="Ignored by label rule: no-ai",
        )
        assert event.call_args.args[:3] == ("run-1", "workflow_skipped", "skipped")
        assert event.call_args.kwargs["status"] == "skipped"
        agent.handle_request.assert_not_awaited()

    asyncio.run(run_test())


def test_triage_pauses_auto_after_current_subcommand():
    async def run_test():
        broker = AsyncMock()
        broker.get_auto_cursor.return_value = AutoWorkflowCursor()
        broker.has_pending_triage.side_effect = [False, True]
        broker.active_triage_task_id.return_value = "triage-536"
        broker.record_auto_command_completed.return_value = True
        broker.pause_auto_for_triage.return_value = True
        agent = Mock()
        agent.handle_request = AsyncMock(return_value=True)
        executor = TaskExecutor(
            broker,
            Mock(),
            "worker-1",
            max_active_tasks=1,
            agent_factory=Mock(return_value=agent),
        )
        task = make_task(
            kind=TaskKind.AUTO_WORKFLOW,
            command="/auto",
            payload={"commands": ["/describe", "/mr_create"]},
        )
        lease = MrLease(task.mr, "worker-1", 7)

        with pytest.raises(TaskSuspended) as suspended:
            await executor._run_auto_workflow(task, lease)

        assert [call.args[1] for call in agent.handle_request.await_args_list] == ["/describe"]
        assert suspended.value.wait_kind == "mr_priority"
        broker.record_auto_command_completed.assert_awaited_once_with(
            task.task_id,
            1,
            ["/describe"],
            "",
            lease,
        )
        broker.pause_auto_for_triage.assert_awaited_once()
        assert broker.pause_auto_for_triage.await_args.kwargs["next_command_index"] == 1

    asyncio.run(run_test())


def test_mr_priority_suspend_does_not_queue_pipeline_wait_card(monkeypatch):
    async def run_test():
        broker = AsyncMock()
        broker.transition_task.return_value = True
        broker.get_task.return_value = Mock(status=TaskStatus.PAUSED_BY_TRIAGE)
        broker.get_auto_cursor.return_value = AutoWorkflowCursor()
        broker.has_pending_triage.return_value = False
        broker.settings.auto_pause_at_command_boundary = True
        agent = Mock()
        agent.handle_request = AsyncMock(side_effect=TaskSuspended("auto-536", "mr_priority", "eabot/cook:536"))
        card_update = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_triage_card_update", card_update)
        executor = TaskExecutor(
            broker,
            Mock(),
            "worker-1",
            max_active_tasks=1,
            agent_factory=Mock(return_value=agent),
        )
        task = make_task(kind=TaskKind.AUTO_WORKFLOW, command="/auto", payload={"commands": ["/describe"]})
        lease = MrLease(task.mr, "worker-1", 7)

        with pytest.raises(TaskSuspended):
            await executor.execute(task, lease)

        broker.resume_pipeline_if_cached.assert_not_awaited()
        card_update.assert_not_awaited()

    asyncio.run(run_test())


def test_executor_marks_failed_command():
    async def run_test():
        broker = AsyncMock()
        broker.transition_task.return_value = True
        agent = Mock()
        agent.handle_request = AsyncMock(return_value=False)
        executor = TaskExecutor(
            broker,
            Mock(),
            "worker-1",
            max_active_tasks=1,
            agent_factory=Mock(return_value=agent),
        )
        task = replace(make_task(), task_id="failed-task")
        lease = MrLease(task.mr, "worker-1", 7)

        with pytest.raises(RuntimeError, match="command failed"):
            await executor.execute(task, lease)

        failed_calls = [call for call in broker.transition_task.await_args_list if call.args[2] is TaskStatus.FAILED]
        assert len(failed_calls) == 1

    asyncio.run(run_test())


def test_suspended_triage_queues_waiting_card_after_transition(monkeypatch):
    async def run_test():
        broker = AsyncMock()
        broker.transition_task.return_value = True
        broker.resume_pipeline_if_cached.return_value = False
        agent = Mock()
        task = replace(make_task(command="/triage"), task_id="task-538", source="feishu")
        agent.handle_request = AsyncMock(
            side_effect=TaskSuspended(task.task_id, "pipeline", "eabot/cook:abc123")
        )
        card_update = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_triage_card_update", card_update)
        executor = TaskExecutor(
            broker,
            Mock(),
            "worker-1",
            max_active_tasks=1,
            agent_factory=Mock(return_value=agent),
        )
        lease = MrLease(task.mr, "worker-1", 7)

        with pytest.raises(TaskSuspended):
            await executor.execute(task, lease)

        assert card_update.await_count == 2
        assert card_update.await_args_list[-1].args == (
            broker,
            task.task_id,
            TriageCardState.WAITING_PIPELINE,
            "等待流水线：eabot/cook:abc123",
        )

    asyncio.run(run_test())


def test_resumed_triage_updates_card_before_pipeline_analysis(monkeypatch):
    async def run_test():
        call_order = []
        broker = AsyncMock()
        broker.transition_task.return_value = True
        from pr_agent.distributed.models import PipelineResumeClaim

        broker.claim_pipeline_resume.return_value = PipelineResumeClaim.CLAIMED
        broker.get_task.return_value = Mock(pipeline_attempt_id="attempt-1", pipeline_id=30032)
        task = replace(make_task(command="/triage"), task_id="task-538", source="feishu")
        card_update = AsyncMock(side_effect=lambda *_args: call_order.append("card"))
        monkeypatch.setattr("pr_agent.distributed.executor.queue_triage_card_update", card_update)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._resume_triage = AsyncMock(side_effect=lambda *_args: call_order.append("resume"))
        lease = MrLease(task.mr, "worker-1", 7)
        event = PipelineEvent.new(
            project_id="eabot/cook",
            pipeline_id=30032,
            sha="abc123",
            status="failed",
            ref="feature/lidar",
        )

        await executor.resume_pipeline(task, lease, event)

        card_update.assert_awaited_once_with(
            broker,
            task.task_id,
            TriageCardState.REPAIR_RUNNING,
            "流水线 #30032 已失败，正在分析流水线结果并决定下一步……",
        )
        assert call_order == ["card", "resume"]

    asyncio.run(run_test())


def test_duplicate_pipeline_resume_is_acknowledged_without_execution():
    async def run_test():
        from pr_agent.distributed.models import PipelineResumeClaim

        broker = AsyncMock()
        broker.claim_pipeline_resume.return_value = PipelineResumeClaim.DUPLICATE
        task = replace(make_task(command="/triage"), task_id="task-duplicate", source="feishu")
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._resume_triage = AsyncMock()
        lease = MrLease(task.mr, "worker-1", 7)
        event = PipelineEvent.new(
            project_id="eabot/cook",
            pipeline_id=30388,
            sha="4aed",
            status="success",
            ref="feature/test",
        )

        await executor.resume_pipeline(task, lease, event)

        executor._resume_triage.assert_not_awaited()
        broker.transition_task.assert_not_awaited()
        broker.record_delivery_failure.assert_not_awaited()

    asyncio.run(run_test())


def test_correlated_fix_format_waits_for_new_pipeline(monkeypatch):
    async def run_test():
        broker = AsyncMock()
        broker.transition_task.return_value = True
        broker.resume_pipeline_if_cached.return_value = False
        broker.get_task_triage_card.return_value = Mock()
        sync_broker = Mock()
        sync_broker.register_pipeline_wait.return_value = None
        sync_broker.record_lifecycle_event.return_value = True
        card_update = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_triage_card_update", card_update)
        monkeypatch.setattr(
            "pr_agent.tools.pr_fix_format.PRFixFormat.run",
            AsyncMock(
                return_value=FixFormatResult(
                    pushed_sha="8cf45166",
                    fixed_files=("src/a.cpp",),
                    status_markdown="格式已修复",
                )
            ),
        )
        monkeypatch.setattr("pr_agent.tools.pr_fix_format.get_git_provider_with_context", Mock(return_value=Mock()))
        task = replace(
            make_task(
                command="/fix-format",
                payload={"repair_category": "format", "source_pipeline_id": 30041},
            ),
            task_id="task-format",
            source="feishu",
        )
        executor = TaskExecutor(broker, sync_broker, "worker-1", max_active_tasks=1)
        executor._reconcile_format_workspace = Mock()
        lease = MrLease(task.mr, "worker-1", 7)

        with pytest.raises(TaskSuspended):
            await executor.execute(task, lease)

        sync_broker.register_pipeline_wait.assert_called_once()
        assert sync_broker.register_pipeline_wait.call_args.args[2] == "8cf45166"
        executor._reconcile_format_workspace.assert_called_once_with(task, "8cf45166")
        waiting_calls = [
            call
            for call in broker.transition_task.await_args_list
            if call.args[2] is TaskStatus.WAITING_PIPELINE
        ]
        assert len(waiting_calls) == 1

    asyncio.run(run_test())


def test_batch_format_reconciles_workspace_before_pipeline_wait(monkeypatch):
    async def run_test():
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = None
        broker.record_pipeline_repair_state.return_value = True
        sync_broker = Mock()
        sync_broker.register_pipeline_wait.return_value = None
        sync_broker.record_lifecycle_event.return_value = True
        sync_broker.is_cancel_requested.return_value = False
        monkeypatch.setattr(
            "pr_agent.tools.pr_fix_format.PRFixFormat.run",
            AsyncMock(return_value=FixFormatResult(pushed_sha="format-sha", fixed_files=("src/a.cpp",))),
        )
        monkeypatch.setattr("pr_agent.tools.pr_fix_format.get_git_provider_with_context", Mock(return_value=Mock()))
        task = replace(make_task(command="/repair-pipeline"), task_id="task-batch-format", source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)
        executor = TaskExecutor(broker, sync_broker, "worker-1", max_active_tasks=1)
        executor._queue_pipeline_repair_progress = AsyncMock()
        executor._reconcile_format_workspace = Mock()
        runtime = ExecutionRuntime(task.task_id, "worker-1", lease, "execute", broker, sync_broker)

        with execution_context(runtime), pytest.raises(TaskSuspended):
            await executor._start_pipeline_format(
                task,
                lease,
                30703,
                PipelineRepairState(
                    phase=PipelineRepairPhase.TRIAGE_RUNNING,
                    latest_pipeline_sha="source-sha",
                    failed_job_names=("code_format_check",),
                ),
            )

        executor._reconcile_format_workspace.assert_called_once_with(task, "format-sha")
        sync_broker.register_pipeline_wait.assert_called_once()
        assert sync_broker.register_pipeline_wait.call_args.args[2] == "format-sha"
        persisted = [call.args[1] for call in broker.record_pipeline_repair_state.await_args_list]
        assert persisted[-1].repair_actions[0].categories == ("format",)
        assert persisted[-1].repair_actions[0].changed_files == ("src/a.cpp",)
        assert persisted[-1].repair_actions[0].commit_sha == "format-sha"

    asyncio.run(run_test())


def test_batch_format_records_structured_ci_job_failure(monkeypatch):
    async def run_test():
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = None
        broker.record_pipeline_repair_state.return_value = True
        monkeypatch.setattr(
            "pr_agent.tools.pr_fix_format.PRFixFormat.run",
            AsyncMock(return_value=FixFormatResult(
                failure_kind="ci_job_configuration",
                failure_summary="CI 传给 git diff 的基准 Commit 为空，格式检查尚未开始。",
                suggested_action="请修正 CI 模板中的 diff 基准变量后重新运行流水线。",
                job_url="https://gitlab.example/jobs/108359",
                status_markdown="Format Job 自身执行失败。",
            )),
        )
        monkeypatch.setattr("pr_agent.tools.pr_fix_format.get_git_provider_with_context", Mock(return_value=Mock()))
        task = replace(make_task(command="/repair-pipeline"), task_id="task-format-preflight", source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._queue_pipeline_repair_progress = AsyncMock()
        executor._record_owner_progress = AsyncMock()
        executor._inspect_pipeline = AsyncMock(
            return_value=(
                [RepairCategory.FORMAT],
                [{"name": "code_format_check"}],
                CoverageResult(status="not_configured"),
                (),
            )
        )
        executor._finish_pipeline_repair = AsyncMock()

        await executor._start_pipeline_format(
            task,
            lease,
            33603,
            PipelineRepairState(
                phase=PipelineRepairPhase.TRIAGE_RUNNING,
                latest_pipeline_sha="source-sha",
                failed_job_names=("code_format_check",),
            ),
        )

        terminal_state = executor._finish_pipeline_repair.await_args.args[2]
        action = terminal_state.repair_actions[0]
        assert action.root_cause == "CI 传给 git diff 的基准 Commit 为空，格式检查尚未开始。"
        assert action.measures == ("请修正 CI 模板中的 diff 基准变量后重新运行流水线。",)
        assert action.status == "no_changes"

    asyncio.run(run_test())


def test_failed_triage_queues_failed_card_without_hiding_error(monkeypatch):
    async def run_test():
        broker = AsyncMock()
        broker.transition_task.return_value = True
        agent = Mock()
        task = replace(make_task(command="/triage"), task_id="task-538", source="feishu")
        agent.handle_request = AsyncMock(side_effect=RuntimeError("model unavailable"))
        failure_notification = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "pr_agent.distributed.executor.queue_triage_failure_notification",
            failure_notification,
        )
        executor = TaskExecutor(
            broker,
            Mock(),
            "worker-1",
            max_active_tasks=1,
            agent_factory=Mock(return_value=agent),
        )
        lease = MrLease(task.mr, "worker-1", 7)

        with pytest.raises(RuntimeError, match="model unavailable"):
            await executor.execute(task, lease)

        failure_notification.assert_awaited_once_with(broker, task, "model unavailable")

    asyncio.run(run_test())


def test_unified_format_only_skips_triage():
    async def run_test():
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = Mock(
            current_pipeline_id=30100,
            current_pipeline_sha="source-sha",
            pipeline_id=30100,
            pipeline_sha="source-sha",
        )
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._inspect_pipeline = AsyncMock(
            return_value=([RepairCategory.FORMAT], [], CoverageResult(status="not_configured"), ())
        )
        executor._start_pipeline_format = AsyncMock()
        executor._start_pipeline_triage = AsyncMock()
        task = replace(
            make_task(command="/repair-pipeline", payload={"source_pipeline_id": 30100}),
            source="feishu",
        )
        lease = MrLease(task.mr, "worker-1", 7)

        with (
            patch("ut_agent.repair_memory.audit.initialize_retrieval_audit", return_value=True) as initialize_audit,
            patch("ut_agent.repair_memory.audit.mark_retrieval_not_attempted", return_value=True) as mark_not_attempted,
        ):
            await executor._run_pipeline_repair(task, lease)

        executor._start_pipeline_triage.assert_not_awaited()
        executor._start_pipeline_format.assert_awaited_once()
        assert executor._start_pipeline_format.await_args.args[2] == 30100
        initialize_audit.assert_called_once()
        assert initialize_audit.call_args.kwargs["reason_code"] == "repair_session_not_reached"
        mark_not_attempted.assert_called_once()
        assert mark_not_attempted.call_args.kwargs["reason_code"] == "format_only_repair"

    asyncio.run(run_test())


def test_unified_build_failure_starts_triage():
    async def run_test():
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = Mock(
            current_pipeline_id=30100,
            current_pipeline_sha="source-sha",
            pipeline_id=30100,
            pipeline_sha="source-sha",
        )
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        source = FailureExplanation(
            job_name="build_release_arm64",
            job_id=105279,
            job_url="https://gitlab.example/eabot/cook/-/jobs/105279",
            trace_line=1837,
            confirmed_reason="fatal error: missing.hpp",
            confidence="confirmed",
        )
        executor._inspect_pipeline = AsyncMock(return_value=(
            [RepairCategory.BUILD],
            [{"name": "build_release_arm64", "id": 105279}],
            CoverageResult(status="not_configured"),
            (source,),
        ))
        executor._start_pipeline_format = AsyncMock()
        executor._start_pipeline_triage = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)

        with patch(
            "ut_agent.repair_memory.audit.initialize_retrieval_audit", return_value=True
        ) as initialize_audit:
            await executor._run_pipeline_repair(task, lease)

        executor._start_pipeline_triage.assert_awaited_once()
        state = executor._start_pipeline_triage.await_args.args[2]
        assert state.root_pipeline_id == 30100
        assert state.latest_pipeline_id == 30100
        assert state.failed_job_names == ("build_release_arm64",)
        assert state.source_failure_explanations == (source,)
        assert state.failure_explanations == (source,)
        executor._start_pipeline_format.assert_not_awaited()
        initialize_audit.assert_called_once()
        assert initialize_audit.call_args.kwargs["task_id"] == task.task_id
        assert initialize_audit.call_args.kwargs["source_pipeline_id"] == 30100

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("selected", "failed_categories", "triage_scope"),
    [
        (["clang", "build"], [RepairCategory.CLANG, RepairCategory.BUILD], ("clang", "build")),
        (["format", "build"], [RepairCategory.FORMAT, RepairCategory.BUILD], ("build",)),
    ],
)
def test_batch_selection_starts_one_scoped_triage(monkeypatch, selected, failed_categories, triage_scope):
    async def run_test():
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = Mock(
            current_pipeline_id=30100,
            current_pipeline_sha="source-sha",
            pipeline_id=30100,
            pipeline_sha="source-sha",
        )
        broker.record_pipeline_repair_state.return_value = True
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._inspect_pipeline = AsyncMock(
            return_value=(failed_categories, [], CoverageResult(status="not_configured"), ())
        )
        executor._queue_pipeline_repair_progress = AsyncMock()
        fake_triage = Mock()
        fake_triage.run = AsyncMock(return_value={"result": {"final_pipeline_status": "failed"}})
        triage_factory = Mock(return_value=fake_triage)
        monkeypatch.setattr("pr_agent.tools.pr_triage.PRTriage", triage_factory)
        executor._continue_after_triage_without_resume = AsyncMock()
        task = replace(
            make_task(
                command="/repair-pipeline",
                payload={
                    "selected_categories": selected,
                    "source_pipeline_id": 30100,
                    "source_pipeline_sha": "source-sha",
                },
            ),
            source="feishu",
        )
        lease = MrLease(task.mr, "worker-1", 7)

        await executor._run_pipeline_repair(task, lease)

        triage_factory.assert_called_once_with(
            task.pr_url,
            pipeline_id=30100,
            pipeline_sha="source-sha",
            selected_categories=triage_scope,
        )
        fake_triage.run.assert_awaited_once_with(publish_result=False, persist_result=False)

    asyncio.run(run_test())


def test_unified_triage_wait_persists_before_suspension(monkeypatch):
    async def run_test():
        broker = AsyncMock()
        broker.record_pipeline_repair_state.return_value = True
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._wait_for_complete_pipeline_group = AsyncMock()
        executor._queue_repair_state = AsyncMock()
        executor._queue_pipeline_repair_progress = AsyncMock()
        fake_triage = Mock()
        fake_triage.run = AsyncMock(side_effect=TaskSuspended("task-repair", "pipeline", "sha-1"))
        monkeypatch.setattr("pr_agent.tools.pr_triage.PRTriage", Mock(return_value=fake_triage))
        task = replace(make_task(command="/repair-pipeline"), task_id="task-repair", source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)

        with pytest.raises(TaskSuspended):
            await executor._start_pipeline_triage(task, lease, PipelineRepairState())

        persisted = [call.args[1] for call in broker.record_pipeline_repair_state.await_args_list]
        assert [state.phase for state in persisted] == [
            PipelineRepairPhase.TRIAGE_RUNNING,
            PipelineRepairPhase.TRIAGE_WAITING,
        ]
        fake_triage.run.assert_awaited_once_with(publish_result=False, persist_result=False)

    asyncio.run(run_test())


def test_post_triage_format_failure_starts_formatter():
    async def run_test():
        broker = AsyncMock()
        broker.get_task.return_value = Mock(
            pipeline_repair_state=PipelineRepairState(phase=PipelineRepairPhase.TRIAGE_WAITING)
        )
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._wait_for_complete_pipeline_group = AsyncMock()
        executor._queue_repair_state = AsyncMock()
        executor._queue_pipeline_repair_progress = AsyncMock()
        executor._resume_triage = AsyncMock(
            return_value={"result": {"success": False, "iterations": 11, "max_iterations": 30}}
        )
        executor._inspect_pipeline = AsyncMock(
            return_value=(
                [RepairCategory.FORMAT, RepairCategory.BUILD],
                [{"name": "code_format_check"}],
                CoverageResult(status="not_configured"),
                (),
            )
        )
        executor._start_pipeline_format = AsyncMock()
        executor._finish_pipeline_repair = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)
        event = PipelineEvent.new(
            project_id="eabot/cook", pipeline_id=30101, sha="triage-sha", status="failed", ref="feature"
        )

        await executor._resume_pipeline_repair(task, lease, event)

        format_call = executor._start_pipeline_format.await_args
        assert format_call.args[:3] == (task, lease, 30101)
        assert format_call.args[3].latest_pipeline_sha == "triage-sha"
        assert format_call.args[3].iterations == 11
        assert format_call.args[3].max_iterations == 30
        executor._finish_pipeline_repair.assert_not_awaited()

    asyncio.run(run_test())


def test_build_only_adds_visible_auto_format_cleanup_after_validation():
    async def run_test():
        broker = AsyncMock()
        broker.get_task.return_value = Mock(
            pipeline_repair_state=PipelineRepairState(
                phase=PipelineRepairPhase.TRIAGE_WAITING,
                selected_categories=("build",),
                effective_categories=("build",),
            )
        )
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._wait_for_complete_pipeline_group = AsyncMock()
        executor._queue_pipeline_repair_progress = AsyncMock()
        executor._resume_triage = AsyncMock(return_value={"result": {"success": False}})
        executor._inspect_pipeline = AsyncMock(
            return_value=(
                [RepairCategory.FORMAT],
                [{"name": "code_format_check"}],
                CoverageResult(status="not_configured"),
                (),
            )
        )
        executor._start_pipeline_format = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)
        event = PipelineEvent.new(
            project_id="eabot/cook",
            pipeline_id=30101,
            sha="triage-sha",
            status="failed",
            ref="feature",
        )

        await executor._resume_pipeline_repair(task, lease, event)

        format_state = executor._start_pipeline_format.await_args.args[3]
        assert format_state.auto_format_cleanup is True
        assert format_state.effective_categories == ("build", "format")

    asyncio.run(run_test())


def test_post_triage_success_skips_formatter():
    async def run_test():
        broker = AsyncMock()
        broker.get_task.return_value = Mock(
            pipeline_repair_state=PipelineRepairState(phase=PipelineRepairPhase.TRIAGE_WAITING)
        )
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._wait_for_complete_pipeline_group = AsyncMock()
        executor._queue_repair_state = AsyncMock()
        executor._queue_pipeline_repair_progress = AsyncMock()
        executor._resume_triage = AsyncMock(
            return_value={"result": {"success": True, "iterations": 9, "max_iterations": 30}}
        )
        executor._inspect_pipeline = AsyncMock(
            return_value=([], [], CoverageResult(63.04, "changed_lines", "reported", 107440), ())
        )
        executor._start_pipeline_format = AsyncMock()
        executor._finish_pipeline_repair = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)
        event = PipelineEvent.new(
            project_id="eabot/cook", pipeline_id=30102, sha="green-sha", status="success", ref="feature"
        )

        await executor._resume_pipeline_repair(task, lease, event)

        executor._start_pipeline_format.assert_not_awaited()
        executor._finish_pipeline_repair.assert_awaited_once()
        assert executor._finish_pipeline_repair.await_args.args[5] == "success"
        assert executor._finish_pipeline_repair.await_args.args[2].iterations == 9
        assert executor._finish_pipeline_repair.await_args.args[2].max_iterations == 30

    asyncio.run(run_test())


def test_direct_triage_result_retains_cumulative_iterations():
    async def run_test():
        executor = TaskExecutor(AsyncMock(), Mock(), "worker-1", max_active_tasks=1)
        executor._inspect_pipeline = AsyncMock(
            return_value=([RepairCategory.BUILD], [], CoverageResult(status="not_configured"), ())
        )
        executor._start_pipeline_format = AsyncMock()
        executor._finish_pipeline_repair = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)

        await executor._continue_after_triage_without_resume(
            task,
            lease,
            PipelineRepairState(latest_pipeline_id=30100, latest_pipeline_sha="source-sha"),
            {
                "result": {
                    "success": False,
                    "iterations": 7,
                    "max_iterations": 30,
                    "final_pipeline_status": "failed",
                    "terminal_failure_kind": "provider_unavailable",
                    "terminal_validation_error_code": "diagnostic_identity_mismatch",
                    "terminal_validation_summary": "缺少 16 条诊断身份，存在 16 条未知身份。",
                    "normalized_diagnostic_alias_count": 0,
                    "repair_actions": [{
                        "action_id": "root-build",
                        "categories": ["build"],
                        "root_cause": "missing dependency",
                        "changed_files": ["CMakeLists.txt"],
                        "status": "committed",
                    }],
                }
            },
        )

        terminal_state = executor._finish_pipeline_repair.await_args.args[2]
        assert terminal_state.iterations == 7
        assert terminal_state.max_iterations == 30
        assert terminal_state.terminal_failure_kind == "provider_unavailable"
        assert terminal_state.terminal_validation_error_code == "diagnostic_identity_mismatch"
        assert terminal_state.terminal_validation_summary == "缺少 16 条诊断身份，存在 16 条未知身份。"
        assert terminal_state.normalized_diagnostic_alias_count == 0
        assert terminal_state.repair_actions == (
            RepairAction.from_dict({
                "action_id": "root-build",
                "categories": ["build"],
                "root_cause": "missing dependency",
                "changed_files": ["CMakeLists.txt"],
                "status": "committed",
            }),
        )

    asyncio.run(run_test())


def test_downstream_failure_without_push_keeps_root_card_retryable(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-repair", source="feishu")
        items = repair_items_for_failed_jobs(
            [{"name": "build_release_arm64"}],
            30959,
            "same-sha",
        )
        binding = Mock(
            repair_items=items,
            repair_card_mode="multi_select",
            current_pipeline_id=30959,
            current_pipeline_sha="same-sha",
        )
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._inspect_pipeline = AsyncMock(
            return_value=(
                [RepairCategory.BUILD],
                [{"name": "build_release_arm64"}],
                CoverageResult(status="not_configured"),
                (),
            )
        )

        await executor._continue_after_triage_without_resume(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                root_pipeline_id=30959,
                latest_pipeline_id=30959,
                latest_pipeline_sha="same-sha",
                selected_categories=("build",),
                effective_categories=("build",),
            ),
            {
                "result": {
                    "success": False,
                    "final_pipeline_status": "failed",
                    "pipeline_groups": [
                        {
                            "root_pipeline_id": 30959,
                            "validation_pipeline_id": 30960,
                            "status": "failed",
                        }
                    ],
                }
            },
        )

        executor._inspect_pipeline.assert_awaited_once_with(task, 30960)
        call = reconcile.await_args
        assert call.args[5:7] == (30959, "same-sha")
        assert call.args[2][0].pipeline_id == 30960
        assert call.args[2][0].result_pipeline_id == 0
        assert call.args[2][0].result_pipeline_sha == ""
        assert call.args[2][0].status is RepairItemStatus.FAILED

    asyncio.run(run_test())


def test_pipeline_repair_surfaces_workspace_preparation_error():
    async def run_test():
        executor = TaskExecutor(AsyncMock(), Mock(), "worker-1", max_active_tasks=1)
        executor._inspect_pipeline = AsyncMock(
            return_value=(
                [RepairCategory.BUILD],
                [{"name": "build_release_arm64"}],
                CoverageResult(status="not_configured"),
                (),
            )
        )
        executor._start_pipeline_format = AsyncMock()
        executor._finish_pipeline_repair = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")

        await executor._continue_after_triage_without_resume(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(latest_pipeline_id=30786, latest_pipeline_sha="source-sha"),
            {
                "result": {
                    "success": False,
                    "error": "工作区准备失败（workspace_dirty）",
                    "iterations": 0,
                    "max_iterations": 30,
                }
            },
        )

        final_call = executor._finish_pipeline_repair.await_args
        assert final_call.args[2].completed_steps == ()
        assert final_call.args[2].max_iterations == 30
        assert final_call.args[5] == "failed"
        assert final_call.args[9] == "工作区准备失败（workspace_dirty）"
        executor._start_pipeline_format.assert_not_awaited()

    asyncio.run(run_test())


def test_format_pipeline_is_terminal_and_never_restarts_triage():
    async def run_test():
        broker = AsyncMock()
        broker.get_task.return_value = Mock(
            pipeline_repair_state=PipelineRepairState(phase=PipelineRepairPhase.FORMAT_WAITING)
        )
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._wait_for_complete_pipeline_group = AsyncMock()
        executor._queue_repair_state = AsyncMock()
        executor._queue_pipeline_repair_progress = AsyncMock()
        executor._inspect_pipeline = AsyncMock(
            return_value=(
                [RepairCategory.BUILD],
                [{"name": "build_release_arm64"}],
                CoverageResult(status="not_configured"),
                (),
            )
        )
        executor._start_pipeline_triage = AsyncMock()
        executor._finish_pipeline_repair = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)
        event = PipelineEvent.new(
            project_id="eabot/cook", pipeline_id=30103, sha="format-sha", status="failed", ref="feature"
        )

        await executor._resume_pipeline_repair(task, lease, event)

        executor._start_pipeline_triage.assert_not_awaited()
        executor._finish_pipeline_repair.assert_awaited_once()

    asyncio.run(run_test())


def test_format_pipeline_retries_when_format_still_fails():
    async def run_test():
        broker = AsyncMock()
        broker.get_task.return_value = Mock(
            pipeline_repair_state=PipelineRepairState(
                phase=PipelineRepairPhase.FORMAT_WAITING,
                selected_categories=("format",),
                effective_categories=("format",),
                source_failed_job_names=("code_format_check",),
                format_round=1,
                format_report_fingerprints=("first-report",),
                format_last_exact_report_applied=True,
            )
        )
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._wait_for_complete_pipeline_group = AsyncMock()
        executor._queue_pipeline_repair_progress = AsyncMock()
        executor._record_owner_progress = AsyncMock()
        executor._inspect_pipeline = AsyncMock(
            return_value=(
                [RepairCategory.FORMAT],
                [{"name": "code_format_check"}],
                CoverageResult(status="not_configured"),
                (),
            )
        )
        executor._start_pipeline_format = AsyncMock()
        executor._finish_pipeline_repair = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)
        event = PipelineEvent.new(
            project_id="eabot/cook",
            pipeline_id=30104,
            sha="second-format-sha",
            status="failed",
            ref="feature",
        )

        await executor._resume_pipeline_repair(task, lease, event)

        executor._start_pipeline_format.assert_awaited_once()
        next_state = executor._start_pipeline_format.await_args.args[3]
        assert next_state.format_round == 1
        assert next_state.format_report_fingerprints == ("first-report",)
        executor._finish_pipeline_repair.assert_not_awaited()

    asyncio.run(run_test())


def test_format_pipeline_stops_at_configured_round_limit():
    async def run_test():
        broker = AsyncMock()
        broker.get_task.return_value = Mock(
            pipeline_repair_state=PipelineRepairState(
                phase=PipelineRepairPhase.FORMAT_WAITING,
                selected_categories=("format",),
                effective_categories=("format",),
                source_failed_job_names=("code_format_check",),
                format_round=3,
            )
        )
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._wait_for_complete_pipeline_group = AsyncMock()
        executor._queue_pipeline_repair_progress = AsyncMock()
        executor._record_owner_progress = AsyncMock()
        executor._inspect_pipeline = AsyncMock(
            return_value=(
                [RepairCategory.FORMAT],
                [{"name": "code_format_check"}],
                CoverageResult(status="not_configured"),
                (),
            )
        )
        executor._start_pipeline_format = AsyncMock()
        executor._finish_pipeline_repair = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)
        event = PipelineEvent.new(
            project_id="eabot/cook",
            pipeline_id=30104,
            sha="third-format-sha",
            status="failed",
            ref="feature",
        )

        await executor._resume_pipeline_repair(task, lease, event)

        executor._start_pipeline_format.assert_not_awaited()
        executor._finish_pipeline_repair.assert_awaited_once()
        assert "3 轮上限" in executor._finish_pipeline_repair.await_args.args[9]

    asyncio.run(run_test())


def test_unified_terminal_failure_reports_latest_jobs_and_coverage(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-repair", source="feishu")
        item = replace(pipeline_repair_item(30100, "source-sha"), task_id=task.task_id)
        binding = Mock(repair_items=(item,))
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        lease = MrLease(task.mr, "worker-1", 7)
        state = PipelineRepairState(
            phase=PipelineRepairPhase.FORMAT_WAITING,
            completed_steps=("诊断修复已完成", "代码格式修复已完成"),
            failure_explanations=(
                FailureExplanation(
                    job_name="build_release_arm64",
                    possible_reason="依赖声明可能缺失",
                    suggested_action="检查 package.xml",
                    confidence="inferred",
                ),
            ),
        )

        await executor._finish_pipeline_repair(
            task,
            lease,
            state,
            30103,
            "final-sha",
            "failed",
            [RepairCategory.BUILD],
            [{"name": "build_release_arm64"}],
            CoverageResult(62.5, "changed_lines", "reported", 88),
            confirmed_explanations=(
                FailureExplanation(
                    job_name="build_release_arm64",
                    job_url="https://gitlab.example/jobs/88",
                    confirmed_reason="fatal error: missing.hpp",
                    confidence="confirmed",
                ),
            ),
        )

        call = reconcile.await_args
        assert call.args[3] is TriageCardState.REPAIR_FAILED
        assert "build_release_arm64" in call.args[4]
        assert "62.5%" in call.args[4]
        assert call.args[5:7] == (30103, "final-sha")
        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.phase is PipelineRepairPhase.TERMINAL
        assert terminal.final_coverage == 62.5
        assert terminal.final_coverage_source == "changed_lines"
        assert terminal.final_coverage_status == "reported"
        assert terminal.failure_explanations[0].confirmed_reason == "fatal error: missing.hpp"
        assert terminal.failure_explanations[0].possible_reason == "依赖声明可能缺失"
        assert call.args[2][0].failure_explanations == terminal.failure_explanations

    asyncio.run(run_test())


def test_model_unavailable_terminal_uses_dedicated_state_and_safe_copy(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-model-outage", source="feishu")
        items = repair_items_for_failed_jobs([{"name": "build_release_arm64"}], 30100, "source-sha")
        items = tuple(replace(item, task_id=task.task_id) for item in items)
        binding = Mock(repair_items=items, repair_card_mode="multi_select")
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.get_task.return_value = None
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        lease = MrLease(task.mr, "worker-1", 7)
        state = PipelineRepairState(
            root_pipeline_id=30100,
            latest_pipeline_id=30100,
            latest_pipeline_sha="source-sha",
            selected_categories=("build",),
            source_failed_job_names=("build_release_arm64",),
            terminal_failure_kind="provider_unavailable",
        )
        provider_error = (
            "模型服务暂时不可用；已尝试模型：claude-opus-4-8；"
            "request-id=req-secret；原因：http_503。"
        )

        await executor._finish_pipeline_repair(
            task,
            lease,
            state,
            30100,
            "source-sha",
            "failed",
            [RepairCategory.BUILD],
            [{"name": "build_release_arm64"}],
            CoverageResult(status="not_reported"),
            provider_error,
        )

        call = reconcile.await_args
        assert call.args[3] is TriageCardState.REPAIR_MODEL_UNAVAILABLE
        assert "模型服务不可用，建议稍后重试。" in call.args[4]
        assert "claude-opus" not in call.args[4]
        assert "request-id" not in call.args[4]
        assert all("request-id" not in item.status_markdown for item in call.args[2])
        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.terminal_error == provider_error
        assert terminal.terminal_failure_kind == "provider_unavailable"
        assert terminal.repair_outcome == "failed"
        assert terminal.auto_rollback_required is False

    asyncio.run(run_test())


def test_non_provider_terminal_failure_remains_generic(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-search-loop", source="feishu")
        item = replace(pipeline_repair_item(30100, "source-sha"), task_id=task.task_id)
        binding = Mock(repair_items=(item,), repair_card_mode="")
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.get_task.return_value = None
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)

        await executor._finish_pipeline_repair(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                selected_categories=("build",),
                source_failed_job_names=("build_release_arm64",),
                terminal_failure_kind="search_loop",
            ),
            30100,
            "source-sha",
            "failed",
            [RepairCategory.BUILD],
            [{"name": "build_release_arm64"}],
            CoverageResult(status="not_reported"),
            "自动调查达到执行上限。",
        )

        assert reconcile.await_args.args[3] is TriageCardState.REPAIR_FAILED

    asyncio.run(run_test())


def test_terminal_result_for_older_pipeline_is_rejected_before_rollback(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-repair", source="feishu")
        item = replace(pipeline_repair_item(30100, "source-sha"), task_id=task.task_id)
        binding = Mock(
            repair_items=(item,),
            repair_card_mode="unified",
            failed_job_names=("build_release_arm64",),
            current_pipeline_id=30100,
            current_pipeline_sha="source-sha",
            pipeline_id=30100,
        )
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.get_task.return_value = Mock(repair_commit_manifest=repair_manifest_for_task(task))
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        state = PipelineRepairState(
            selected_categories=("build",),
            effective_categories=("build",),
            source_failed_job_names=("build_release_arm64",),
            terminal_attempt_id="attempt-2",
            terminal_proof_sha="a" * 40,
            terminal_proof_pipeline_id=34700,
            terminal_proof_status="failed",
        )

        with pytest.raises(RuntimeError, match="terminal_pipeline_proof_mismatch"):
            await executor._finish_pipeline_repair(
                task,
                MrLease(task.mr, "worker-1", 7),
                state,
                34700,
                "a" * 40,
                "failed",
                [RepairCategory.BUILD],
                [{"name": "build_release_arm64"}],
                CoverageResult(status="not_configured"),
            )

        broker.record_pipeline_repair_state.assert_not_awaited()
        reconcile.assert_not_awaited()

    asyncio.run(run_test())


def test_triage_terminal_proof_is_copied_into_durable_state():
    executor = TaskExecutor(AsyncMock(), Mock(), "worker-1", max_active_tasks=1)

    state = executor._state_with_triage_terminal_proof(
        PipelineRepairState(),
        {
            "result": {
                "terminal_proof": {
                    "attempt_id": "attempt-3",
                    "commit_sha": "f" * 40,
                    "pipeline_id": 34713,
                    "status": "failed",
                }
            }
        },
    )

    assert state.terminal_attempt_id == "attempt-3"
    assert state.terminal_proof_sha == "f" * 40
    assert state.terminal_proof_pipeline_id == 34713
    assert state.terminal_proof_status == "failed"


def test_inspect_pipeline_resolves_changed_lines_coverage(monkeypatch):
    class Collection:
        def __init__(self, values):
            self.values = list(values)

        def list(self, **_kwargs):
            return list(self.values)

    summary_job = Mock(
        id=107440,
        name="x86_64_ut_coverage_check",
        status="success",
        attributes={"id": 107440, "name": "x86_64_ut_coverage_check", "status": "success"},
    )
    loaded_job = Mock()
    loaded_job.artifact.return_value = "<div>覆盖率</div><strong>63.04%</strong>"
    loaded_job.trace.return_value = b"Coverage: 63.04%\nThreshold: 80.0%"
    pipeline = Mock(
        id=33334,
        sha="final-sha",
        status="success",
        source="push",
        coverage=None,
        attributes={"coverage": None},
        jobs=Collection([summary_job]),
        bridges=Collection([]),
    )

    class Pipelines(Collection):
        def get(self, _pipeline_id):
            return pipeline

    project = Mock(
        pipelines=Pipelines([pipeline]),
        jobs=Mock(get=Mock(return_value=loaded_job)),
    )
    provider = Mock(id_project="eabot/cook", gl=Mock(projects=Mock(get=Mock(return_value=project))))
    monkeypatch.setattr(
        "pr_agent.git_providers.gitlab_provider.GitLabProvider",
        lambda _pr_url: provider,
    )
    executor = TaskExecutor(AsyncMock(), Mock(), "worker-1", max_active_tasks=1)

    categories, failed_jobs, coverage, explanations = asyncio.run(
        executor._inspect_pipeline(make_task(command="/repair-pipeline"), 33334)
    )

    assert categories == []
    assert failed_jobs == []
    assert explanations == ()
    assert coverage == CoverageResult(63.04, "changed_lines", "reported", 107440, 80.0)


def test_unified_terminal_success_clears_current_failures_but_preserves_source_evidence(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-repair", source="feishu")
        item = replace(pipeline_repair_item(30100, "source-sha"), task_id=task.task_id)
        binding = Mock(repair_items=(item,))
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        lease = MrLease(task.mr, "worker-1", 7)
        source = FailureExplanation(
            job_name="build_release_arm64",
            job_id=105279,
            job_url="https://gitlab.example/eabot/cook/-/jobs/105279",
            trace_line=1837,
            confirmed_reason="fatal error: missing.hpp",
            confidence="confirmed",
        )
        state = PipelineRepairState(
            phase=PipelineRepairPhase.TRIAGE_WAITING,
            source_failure_explanations=(source,),
            failure_explanations=(source,),
        )

        await executor._finish_pipeline_repair(
            task,
            lease,
            state,
            30103,
            "final-sha",
            "success",
            [],
            [],
            CoverageResult(63.04, "changed_lines", "reported", 107440),
        )

        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.final_pipeline_status == "success"
        assert terminal.final_coverage == 63.04
        assert terminal.final_coverage_source == "changed_lines"
        assert terminal.final_coverage_status == "reported"
        assert terminal.failed_job_names == ()
        assert terminal.failure_explanations == ()
        assert terminal.source_failure_explanations == (source,)

    asyncio.run(run_test())


def test_multi_select_terminal_reopens_unselected_remaining_category(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-repair", source="feishu")
        items = repair_items_for_failed_jobs(
            [{"name": "clang_tidy_check"}, {"name": "build_release_arm64"}],
            30100,
            "source-sha",
        )
        items = tuple(
            replace(item, task_id=task.task_id) if item.category is RepairCategory.CLANG else item
            for item in items
        )
        binding = Mock(repair_items=items, repair_card_mode="multi_select")
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        lease = MrLease(task.mr, "worker-1", 7)
        state = PipelineRepairState(
            phase=PipelineRepairPhase.TRIAGE_WAITING,
            selected_categories=("clang",),
            effective_categories=("clang",),
        )

        await executor._finish_pipeline_repair(
            task,
            lease,
            state,
            30101,
            "validation-sha",
            "failed",
            [RepairCategory.BUILD],
            [{"name": "build_release_arm64"}],
            CoverageResult(status="not_configured"),
        )

        call = reconcile.await_args
        reconciled = call.args[2]
        assert call.args[3] is TriageCardState.REPAIR_SUCCEEDED
        assert reconciled[0].category is RepairCategory.CLANG
        assert reconciled[0].status is RepairItemStatus.SUCCEEDED
        assert reconciled[1].category is RepairCategory.BUILD
        assert reconciled[1].status is RepairItemStatus.PENDING

    asyncio.run(run_test())


def test_multi_select_terminal_distinguishes_partial_success(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-partial", source="feishu")
        items = repair_items_for_failed_jobs(
            [{"name": "code_format_check"}, {"name": "clang_tidy_check"}],
            30100,
            "source-sha",
        )
        binding = Mock(repair_items=items, repair_card_mode="multi_select")
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)

        await executor._finish_pipeline_repair(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                selected_categories=("format", "clang"),
                effective_categories=("format", "clang"),
                source_failed_job_names=("code_format_check", "clang_tidy_check"),
            ),
            30101,
            "validation-sha",
            "failed",
            [RepairCategory.CLANG],
            [{"name": "clang_tidy_check"}],
            CoverageResult(status="not_configured"),
        )

        call = reconcile.await_args
        assert call.args[3] is TriageCardState.REPAIR_PARTIAL
        assert "Format：修复成功" in call.args[4]
        assert "Clang：修复失败" in call.args[4]
        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.repair_outcome == "partial_success"

    asyncio.run(run_test())


def test_zero_selected_success_with_commits_defers_terminal_card(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-zero-benefit", source="feishu")
        items = repair_items_for_failed_jobs([{"name": "build_release_arm64"}], 30100, "source-sha")
        binding = Mock(repair_items=items, repair_card_mode="multi_select")
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = binding
        manifest = repair_manifest_for_task(task)
        broker.get_task.return_value = Mock(repair_commit_manifest=manifest)
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)

        await executor._finish_pipeline_repair(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                selected_categories=("build",),
                effective_categories=("build",),
                source_failed_job_names=("build_release_arm64",),
            ),
            30101,
            manifest.final_repair_sha,
            "failed",
            [RepairCategory.BUILD],
            [{"name": "build_release_arm64"}],
            CoverageResult(status="not_configured"),
        )

        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.verified_selected_success_count == 0
        assert terminal.auto_rollback_required is True
        assert reconcile.await_args.args[3] is TriageCardState.REPAIR_RUNNING
        assert reconcile.await_args.args[4] == "修复未成功，正在撤回本次自动修改"

    asyncio.run(run_test())


def test_canceled_validation_with_commits_is_unverified_and_defers_to_rollback(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-canceled", source="feishu")
        items = repair_items_for_failed_jobs([{"name": "build_release_arm64"}], 30100, "source-sha")
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = Mock(repair_items=items, repair_card_mode="multi_select")
        manifest = repair_manifest_for_task(task)
        broker.get_task.return_value = Mock(repair_commit_manifest=manifest)
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)

        await executor._finish_pipeline_repair(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                selected_categories=("build",),
                effective_categories=("build",),
                source_failed_job_names=("build_release_arm64",),
            ),
            30101,
            manifest.final_repair_sha,
            "canceled",
            [],
            [],
            CoverageResult(status="not_configured"),
        )

        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.category_results[0].outcome.value == "unverified"
        assert terminal.verified_selected_success_count == 0
        assert terminal.auto_rollback_required is True
        assert reconcile.await_args.args[3] is TriageCardState.REPAIR_RUNNING

    asyncio.run(run_test())


def test_zero_selected_success_without_commits_publishes_failure(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-no-commit", source="feishu")
        items = repair_items_for_failed_jobs([{"name": "build_release_arm64"}], 30100, "source-sha")
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = Mock(repair_items=items, repair_card_mode="multi_select")
        broker.get_task.return_value = Mock(repair_commit_manifest=None)
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)

        await executor._finish_pipeline_repair(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                selected_categories=("build",),
                effective_categories=("build",),
                source_failed_job_names=("build_release_arm64",),
            ),
            30101,
            "validation-sha",
            "failed",
            [RepairCategory.BUILD],
            [{"name": "build_release_arm64"}],
            CoverageResult(status="not_configured"),
        )

        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.auto_rollback_required is False
        assert reconcile.await_args.args[3] is TriageCardState.REPAIR_FAILED

    asyncio.run(run_test())


def test_dependency_blocker_ingestion_is_bounded_and_rejects_unknown_fields():
    executor = TaskExecutor(AsyncMock(), Mock(), "worker-1", max_active_tasks=1)
    state = executor._state_with_dependency_blockers(
        PipelineRepairState(),
        [{
            "root_cause_id": "root-prism",
            "job_name": "build_release_arm64",
            "blocker_type": "external_dependency",
            "root_cause": "当前声明分支缺少接口。",
            "suggested_action": "请维护者确认候选分支。",
            "dependency_evidence": {
                "project_path": "eabot/lhotse",
                "declared_branch": "dev",
                "declared_sha": "dev-sha",
                "candidate_kind": "unique_verified_candidate",
                "checked_branch_count": 300,
                "verified_candidates": [{
                    "branch": "TwoEndAreas/phase1/0820",
                    "resolved_sha": "candidate-sha",
                    "file_paths": {"PlanningStatus.msg": "eabot_msgs/msg/PlanningStatus.msg"},
                    "raw_log": "must not persist",
                }],
                "raw_log": "must not persist",
                "unknown": "must not persist",
            },
            "unknown": "must not persist",
        }],
    )

    assert state.blocker_type == "external_dependency"
    assert state.blocked_job_names == ("build_release_arm64",)
    assert state.blocker_summary == "当前声明分支缺少接口。"
    assert state.dependency_evidence[0]["project_path"] == "eabot/lhotse"
    assert state.dependency_evidence[0]["checked_branch_count"] == 300
    assert "raw_log" not in str(state.dependency_evidence)
    assert "unknown" not in str(state.dependency_evidence)


def test_all_blocked_without_push_skips_format_and_coverage_continuation():
    async def run_test():
        executor = TaskExecutor(AsyncMock(), Mock(), "worker-1", max_active_tasks=1)
        executor._inspect_pipeline = AsyncMock(return_value=(
            [RepairCategory.FORMAT, RepairCategory.BUILD],
            [{"name": "code_format_check"}, {"name": "build_release_arm64"}],
            CoverageResult(status="not_configured"),
            (),
        ))
        executor._start_pipeline_format = AsyncMock()
        executor._maybe_start_coverage_continuation = AsyncMock()
        executor._finish_pipeline_repair = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")

        await executor._continue_after_triage_without_resume(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                latest_pipeline_id=30100,
                latest_pipeline_sha="source-sha",
                selected_categories=("format", "build"),
                effective_categories=("format", "build"),
            ),
            {
                "result": {
                    "success": False,
                    "final_pipeline_status": "failed",
                    "dependency_blockers": [
                        {
                            "root_cause_id": "root-format",
                            "job_name": "code_format_check",
                            "blocker_type": "external_dependency",
                            "root_cause": "格式任务依赖的外部配置缺失。",
                            "suggested_action": "请维护者补齐外部配置。",
                            "dependency_evidence": {"project_path": "eabot/lhotse"},
                        },
                        {
                            "root_cause_id": "root-build",
                            "job_name": "build_release_arm64",
                            "blocker_type": "external_dependency",
                            "root_cause": "构建任务依赖的上游接口缺失。",
                            "suggested_action": "请维护者确认依赖分支。",
                            "dependency_evidence": {"project_path": "eabot/lhotse"},
                        },
                    ],
                },
            },
        )

        executor._start_pipeline_format.assert_not_awaited()
        executor._maybe_start_coverage_continuation.assert_not_awaited()
        executor._finish_pipeline_repair.assert_awaited_once()
        terminal_state = executor._finish_pipeline_repair.await_args.args[2]
        assert terminal_state.blocked_job_names == ("code_format_check", "build_release_arm64")

    asyncio.run(run_test())


def test_all_selected_dependency_blockers_publish_blocked_without_rollback(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-blocked", source="feishu")
        items = repair_items_for_failed_jobs([{"name": "build_release_arm64"}], 30100, "source-sha")
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = Mock(repair_items=items, repair_card_mode="multi_select")
        broker.get_task.return_value = Mock(repair_commit_manifest=None)
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        state = PipelineRepairState(
            selected_categories=("build",),
            effective_categories=("build",),
            source_failed_job_names=("build_release_arm64",),
            blocker_type="external_dependency",
            blocker_summary="当前声明分支缺少接口。",
            blocker_suggested_action="请维护者确认候选分支。",
            blocked_job_names=("build_release_arm64",),
            dependency_evidence=({"project_path": "eabot/lhotse"},),
        )

        await executor._finish_pipeline_repair(
            task,
            MrLease(task.mr, "worker-1", 7),
            state,
            30101,
            "source-sha",
            "failed",
            [RepairCategory.BUILD],
            [{"name": "build_release_arm64"}],
            CoverageResult(status="not_configured"),
        )

        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.repair_outcome == "blocked"
        assert terminal.final_pipeline_status == "failed"
        assert terminal.latest_pipeline_sha == "source-sha"
        assert terminal.blocker_type == "external_dependency"
        assert terminal.blocked_job_names == ("build_release_arm64",)
        assert terminal.repair_actions == ()
        assert terminal.dependency_evidence == ({"project_path": "eabot/lhotse"},)
        assert terminal.auto_rollback_required is False
        broker.freeze_repair_commit_manifest.assert_not_awaited()
        assert reconcile.await_args.args[2][0].status is RepairItemStatus.BLOCKED
        assert reconcile.await_args.args[3] is TriageCardState.REPAIR_BLOCKED
        assert "外部依赖阻塞" in reconcile.await_args.args[4]

    asyncio.run(run_test())


def test_success_plus_dependency_blocker_is_partial_success(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-blocked-partial", source="feishu")
        items = repair_items_for_failed_jobs(
            [{"name": "code_format_check"}, {"name": "build_release_arm64"}],
            30100,
            "source-sha",
        )
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = Mock(repair_items=items, repair_card_mode="multi_select")
        broker.get_task.return_value = Mock(repair_commit_manifest=None)
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)

        await executor._finish_pipeline_repair(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                selected_categories=("format", "build"),
                effective_categories=("format", "build"),
                source_failed_job_names=("code_format_check", "build_release_arm64"),
                blocked_job_names=("build_release_arm64",),
            ),
            30101,
            "validation-sha",
            "failed",
            [RepairCategory.BUILD],
            [{"name": "build_release_arm64"}],
            CoverageResult(status="not_configured"),
        )

        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.repair_outcome == "partial_success"
        assert reconcile.await_args.args[3] is TriageCardState.REPAIR_PARTIAL
        assert {item.category: item.status for item in reconcile.await_args.args[2]} == {
            RepairCategory.FORMAT: RepairItemStatus.SUCCEEDED,
            RepairCategory.BUILD: RepairItemStatus.BLOCKED,
        }

    asyncio.run(run_test())


def test_same_category_partial_benefit_with_blocker_retains_repair_commits(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-build-partial", source="feishu")
        items = repair_items_for_failed_jobs(
            [{"name": "build_release_arm64"}, {"name": "x86_64_ut_coverage_check"}],
            30100,
            "source-sha",
        )
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = Mock(repair_items=items, repair_card_mode="multi_select")
        manifest = repair_manifest_for_task(task)
        broker.get_task.return_value = Mock(repair_commit_manifest=manifest)
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)

        await executor._finish_pipeline_repair(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                selected_categories=("build",),
                effective_categories=("build",),
                source_failed_job_names=("build_release_arm64", "x86_64_ut_coverage_check"),
                blocked_job_names=("x86_64_ut_coverage_check",),
            ),
            30101,
            manifest.final_repair_sha,
            "failed",
            [RepairCategory.BUILD],
            [{"name": "x86_64_ut_coverage_check"}],
            CoverageResult(status="not_configured"),
        )

        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.repair_outcome == "partial_success"
        assert terminal.verified_selected_success_count == 1
        assert terminal.auto_rollback_required is False
        assert reconcile.await_args.args[3] is TriageCardState.REPAIR_PARTIAL

    asyncio.run(run_test())


def test_dependency_blocker_plus_nonblocked_failure_remains_failed(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-blocked-failed", source="feishu")
        items = repair_items_for_failed_jobs(
            [{"name": "build_release_arm64"}, {"name": "clang_tidy_check"}],
            30100,
            "source-sha",
        )
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = Mock(repair_items=items, repair_card_mode="multi_select")
        broker.get_task.return_value = Mock(repair_commit_manifest=None)
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)

        await executor._finish_pipeline_repair(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                selected_categories=("build", "clang"),
                effective_categories=("build", "clang"),
                source_failed_job_names=("build_release_arm64", "clang_tidy_check"),
                blocked_job_names=("build_release_arm64",),
            ),
            30101,
            "validation-sha",
            "failed",
            [RepairCategory.BUILD, RepairCategory.CLANG],
            [{"name": "build_release_arm64"}, {"name": "clang_tidy_check"}],
            CoverageResult(status="not_configured"),
        )

        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.repair_outcome == "failed"
        assert reconcile.await_args.args[3] is TriageCardState.REPAIR_FAILED

    asyncio.run(run_test())


def test_one_selected_success_with_new_failure_retains_commits(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-partial-benefit", source="feishu")
        items = repair_items_for_failed_jobs(
            [{"name": "code_format_check"}, {"name": "clang_tidy_check"}], 30100, "source-sha"
        )
        broker = AsyncMock()
        broker.get_task_triage_card.return_value = Mock(repair_items=items, repair_card_mode="multi_select")
        manifest = repair_manifest_for_task(task)
        broker.get_task.return_value = Mock(repair_commit_manifest=manifest)
        broker.record_pipeline_repair_state.return_value = True
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)

        await executor._finish_pipeline_repair(
            task,
            MrLease(task.mr, "worker-1", 7),
            PipelineRepairState(
                selected_categories=("format", "clang"),
                effective_categories=("format", "clang"),
                source_failed_job_names=("code_format_check", "clang_tidy_check"),
            ),
            30101,
            manifest.final_repair_sha,
            "failed",
            [RepairCategory.CLANG, RepairCategory.BUILD],
            [{"name": "clang_tidy_check"}, {"name": "build_release_arm64"}],
            CoverageResult(status="not_configured"),
        )

        terminal = broker.record_pipeline_repair_state.await_args.args[1]
        assert terminal.verified_selected_success_count == 1
        assert terminal.auto_rollback_required is False
        assert reconcile.await_args.args[3] is TriageCardState.REPAIR_FAILED

    asyncio.run(run_test())


def test_execute_enqueues_auto_failure_rollback_and_skips_normal_completion():
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-auto-rollback", source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)
        manifest = repair_manifest_for_task(task)
        state = PipelineRepairState(
            phase=PipelineRepairPhase.TERMINAL,
            auto_rollback_required=True,
            verified_selected_success_count=0,
        )
        binding = Mock(
            card_id="card-1",
            open_message_id="om-1",
            receive_id="owner",
            revision=3,
            repair_items=(),
            current_pipeline_id=30101,
            current_pipeline_sha="b" * 40,
        )
        broker = AsyncMock()
        broker.transition_task.return_value = True
        broker.freeze_repair_commit_manifest.return_value = manifest
        broker.get_task.return_value = StoredTask(
            task,
            TaskStatus.RUNNING,
            0,
            "worker-1",
            7,
            "",
            "",
            pipeline_repair_state=state,
            repair_commit_manifest=manifest,
        )
        broker.get_task_triage_card.return_value = binding
        broker.request_repair_rollback.return_value = RollbackRequestResult(
            task.task_id, "rollback-task", True, "queued"
        )
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)
        executor._queue_repair_state = AsyncMock()
        executor._run_task = AsyncMock()
        executor._provider_context = Mock(return_value=nullcontext())
        executor._persist_repair_terminal = AsyncMock()

        await executor.execute(task, lease)

        broker.request_repair_rollback.assert_awaited_once_with(
            task.task_id, "card-1", "om-1", "owner", 3, trigger="auto_failure"
        )
        broker.publish_repair_rollback_eligibility.assert_not_awaited()
        broker.record_task_result.assert_not_awaited()
        assert not any(call.args[2] is TaskStatus.PUBLISHING for call in broker.transition_task.await_args_list)
        executor._persist_repair_terminal.assert_awaited_once_with(task)

    asyncio.run(run_test())


def test_auto_failure_admission_refusal_publishes_safe_stop(monkeypatch):
    async def run_test():
        task = replace(make_task(command="/repair-pipeline"), task_id="task-auto-refused", source="feishu")
        manifest = repair_manifest_for_task(task)
        binding = Mock(
            card_id="card-1",
            open_message_id="om-1",
            receive_id="owner",
            revision=3,
            repair_items=(),
            current_pipeline_id=30101,
            current_pipeline_sha="b" * 40,
        )
        broker = AsyncMock()
        broker.get_task.return_value = Mock(
            pipeline_repair_state=PipelineRepairState(auto_rollback_required=True)
        )
        broker.get_task_triage_card.return_value = binding
        broker.request_repair_rollback.side_effect = RepairRollbackUnavailable("manifest mismatch")
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
        executor = TaskExecutor(broker, Mock(), "worker-1", max_active_tasks=1)

        handled = await executor._enqueue_automatic_failure_rollback(task, manifest)

        assert handled is False
        assert reconcile.await_args.args[3] is TriageCardState.REPAIR_FAILED
        assert reconcile.await_args.args[4] == "修复失败，自动撤回未完成：manifest mismatch"

    asyncio.run(run_test())


def test_coverage_continuation_persists_attempt_and_waits_for_b(monkeypatch):
    async def run_test():
        from ut_agent.coverage_enhancement import CoverageEnhancementResult

        broker = AsyncMock()
        broker.record_pipeline_repair_state.return_value = True
        sync_broker = Mock()
        sync_broker.register_pipeline_wait.return_value = None
        sync_broker.record_lifecycle_event.return_value = True
        sync_broker.is_cancel_requested.return_value = False
        provider = Mock()
        provider.get_pr_branch.return_value = "feature"
        monkeypatch.setattr("pr_agent.git_providers.get_git_provider_with_context", Mock(return_value=provider))
        monkeypatch.setattr(
            "ut_agent.tools.fetch_coverage_report.fetch_changed_lines_report",
            Mock(return_value={
                "available": True,
                "files": [{"path": "src/a.cpp", "uncovered": [{"line": 10, "code": "branch"}]}],
            }),
        )
        snapshot = Mock(status="ready", message="")
        snapshot.to_dict.return_value = {"status": "ready"}
        monkeypatch.setattr("ut_agent.workspace.prepare_workspace", Mock(return_value=snapshot))
        monkeypatch.setattr(
            "ut_agent.coverage_enhancement.run_coverage_enhancement",
            Mock(return_value=CoverageEnhancementResult("pushed", commit_sha="b" * 40)),
        )
        executor = TaskExecutor(broker, sync_broker, "worker-1", max_active_tasks=1)
        executor._record_owner_progress = AsyncMock()
        executor._queue_pipeline_repair_progress = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), task_id="coverage-task", source="feishu")
        lease = MrLease(task.mr, "worker-1", 7)
        runtime = ExecutionRuntime(task.task_id, "worker-1", lease, "queue", broker, sync_broker)
        state = PipelineRepairState(selected_categories=("build",), latest_pipeline_sha="a" * 40)
        failed_jobs = [{"id": 17, "name": "x86_64_ut_coverage_check"}]
        coverage = CoverageResult(63.04, "changed_lines", "reported", 17, 80.0)

        with execution_context(runtime), pytest.raises(TaskSuspended):
            await executor._maybe_start_coverage_continuation(
                task, lease, state, 30101, "a" * 40, failed_jobs, coverage, {}
            )

        persisted = [call.args[1] for call in broker.record_pipeline_repair_state.await_args_list]
        assert persisted[0].coverage_attempts == 1
        assert persisted[0].coverage_baseline_sha == "a" * 40
        assert persisted[-1].phase is PipelineRepairPhase.COVERAGE_WAITING
        assert persisted[-1].coverage_enhancement_sha == "b" * 40
        assert sync_broker.register_pipeline_wait.call_args.args[2] == "b" * 40

    asyncio.run(run_test())

def test_coverage_b_success_finishes_without_rollback():
    async def run_test():
        executor = TaskExecutor(AsyncMock(), Mock(), "worker-1", max_active_tasks=1)
        executor._inspect_pipeline = AsyncMock(return_value=(
            [], [], CoverageResult(82.0, "changed_lines", "reported", 18, 80.0), ()
        ))
        executor._finish_pipeline_repair = AsyncMock()
        executor._start_coverage_rollback = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")
        event = PipelineEvent.new(
            project_id="eabot/cook", pipeline_id=30102, sha="b" * 40, status="success", ref="feature"
        )
        state = PipelineRepairState(
            phase=PipelineRepairPhase.COVERAGE_WAITING,
            coverage_phase=CoverageContinuationPhase.WAITING,
            coverage_attempts=1,
            coverage_baseline_sha="a" * 40,
            coverage_enhancement_sha="b" * 40,
        )

        await executor._resume_coverage_continuation(task, None, event, state)

        terminal_state = executor._finish_pipeline_repair.await_args.args[2]
        assert terminal_state.coverage_result == "succeeded"
        assert terminal_state.coverage_after == 82.0
        executor._start_coverage_rollback.assert_not_awaited()

    asyncio.run(run_test())


def test_coverage_b_failure_starts_targeted_rollback():
    async def run_test():
        executor = TaskExecutor(AsyncMock(), Mock(), "worker-1", max_active_tasks=1)
        failed_jobs = [{"name": "build_release_arm64"}]
        executor._inspect_pipeline = AsyncMock(return_value=(
            [RepairCategory.BUILD],
            failed_jobs,
            CoverageResult(status="job_failed"),
            (),
        ))
        executor._start_coverage_rollback = AsyncMock()
        task = replace(make_task(command="/repair-pipeline"), source="feishu")
        event = PipelineEvent.new(
            project_id="eabot/cook", pipeline_id=30102, sha="b" * 40, status="failed", ref="feature"
        )
        state = PipelineRepairState(
            phase=PipelineRepairPhase.COVERAGE_WAITING,
            coverage_phase=CoverageContinuationPhase.WAITING,
            coverage_attempts=1,
            coverage_baseline_sha="a" * 40,
            coverage_enhancement_sha="b" * 40,
        )

        await executor._resume_coverage_continuation(task, None, event, state)

        executor._start_coverage_rollback.assert_awaited_once()
        assert executor._start_coverage_rollback.await_args.args[5] == failed_jobs

    asyncio.run(run_test())
