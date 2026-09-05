import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.distributed.broker import EffectRecord, MrLease
from pr_agent.distributed.models import MrKey, TaskEnvelope, TaskKind, TaskStatus
from pr_agent.distributed.process_executor import TASK_SUSPENDED_EXIT_CODE, ProcessTaskExecutor
from pr_agent.distributed.runtime import TaskSuspended
from pr_agent.triage.repair_rollback import RepairCommitEntry, RepairCommitManifest


def make_task() -> TaskEnvelope:
    task = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="feishu",
        mr=MrKey("eabot/cook", 541),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/541",
        command="/triage",
        payload={"sender_id": "ou_1"},
        idempotency_key="card:event-1",
    )
    return replace(task, task_id="task-541")


class FakeProcess:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.pid = 123
        self.input = None

    async def communicate(self, value):
        self.input = value


class BlockingProcess(FakeProcess):
    def __init__(self):
        super().__init__(None)
        self.started = asyncio.Event()

    async def communicate(self, value):
        self.input = value
        self.started.set()
        await asyncio.Event().wait()


def test_process_executor_returns_after_success(monkeypatch):
    async def run_test():
        process = FakeProcess(0)
        spawn = AsyncMock(return_value=process)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        broker = AsyncMock()
        task = make_task()
        executor = ProcessTaskExecutor(broker, "worker-1", max_active_tasks=2)

        await executor.execute(task, MrLease(task.mr, "worker-1", 7))

        assert b"task-541" in process.input
        broker.transition_task.assert_not_awaited()
        broker.heartbeat_task.assert_awaited_once_with(task.task_id, "worker-1", 7)

    asyncio.run(run_test())


def test_report_uses_a_separate_execution_slot(monkeypatch):
    async def run_test():
        normal_process = BlockingProcess()
        report_process = BlockingProcess()
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            AsyncMock(side_effect=[normal_process, report_process]),
        )
        broker = AsyncMock()
        normal = make_task()
        report = replace(
            make_task(),
            task_id="report-541",
            kind=TaskKind.REPAIR_REPORT,
            mr=None,
            command="/summarize-repair",
            payload={"repair_task_id": normal.task_id},
        )
        executor = ProcessTaskExecutor(broker, "worker-1", max_active_tasks=1, max_active_report_tasks=1)
        executions = [
            asyncio.create_task(executor.execute(normal, MrLease(normal.mr, "worker-1", 7))),
            asyncio.create_task(executor.execute(report, None)),
        ]
        await asyncio.wait_for(asyncio.gather(normal_process.started.wait(), report_process.started.wait()), timeout=1)
        for execution in executions:
            execution.cancel()
        await asyncio.gather(*executions, return_exceptions=True)
        assert normal_process.started.is_set() and report_process.started.is_set()

    asyncio.run(run_test())


def test_process_executor_propagates_suspend(monkeypatch):
    async def run_test():
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=FakeProcess(TASK_SUSPENDED_EXIT_CODE)),
        )
        task = make_task()
        executor = ProcessTaskExecutor(AsyncMock(), "worker-1", max_active_tasks=1)

        with pytest.raises(TaskSuspended):
            await executor.execute(task, MrLease(task.mr, "worker-1", 7))

    asyncio.run(run_test())


def test_process_executor_preserves_mr_priority_suspend_kind(monkeypatch):
    async def run_test():
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=FakeProcess(TASK_SUSPENDED_EXIT_CODE)),
        )
        task = make_task()
        broker = AsyncMock()
        broker.get_task.return_value = SimpleNamespace(
            status=TaskStatus.PAUSED_BY_TRIAGE,
            wait_kind="mr_priority",
            wait_identity=task.mr.redis_id,
        )
        executor = ProcessTaskExecutor(broker, "worker-1", max_active_tasks=1)

        with pytest.raises(TaskSuspended) as suspended:
            await executor.execute(task, MrLease(task.mr, "worker-1", 7))

        assert suspended.value.wait_kind == "mr_priority"
        assert suspended.value.wait_identity == task.mr.redis_id

    asyncio.run(run_test())


def test_process_executor_marks_unhandled_child_crash_failed(monkeypatch):
    async def run_test():
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess(9)))
        broker = AsyncMock()
        broker.transition_task.return_value = True
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr("pr_agent.distributed.process_executor.queue_triage_failure_notification", notify)
        task = make_task()
        lease = MrLease(task.mr, "worker-1", 7)
        executor = ProcessTaskExecutor(broker, "worker-1", max_active_tasks=1)

        with pytest.raises(RuntimeError, match="exit=9"):
            await executor.execute(task, lease)

        broker.transition_task.assert_awaited_once_with(
            task.task_id,
            {TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.PUBLISHING},
            TaskStatus.FAILED,
            lease,
            {"error": "隔离任务进程异常退出（exit=9）"},
        )
        notify.assert_awaited_once()
        broker.resume_auto_after_triage.assert_awaited_once_with(
            task.mr,
            triage_task_id=task.task_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
        )

    asyncio.run(run_test())


def test_running_process_group_is_terminated_after_cancel(monkeypatch):
    async def run_test():
        process = BlockingProcess()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
        broker = AsyncMock()
        broker.is_cancel_requested.side_effect = [False, True]
        broker.get_active_effect.return_value = None
        broker.get_task.return_value = None
        broker.finalize_cancel_or_enqueue_rollback.return_value = None
        task = make_task()
        lease = MrLease(task.mr, "worker-1", 7)
        executor = ProcessTaskExecutor(
            broker,
            "worker-1",
            max_active_tasks=1,
            cancel_poll_seconds=0.001,
        )
        executor._terminate = AsyncMock()

        await executor.execute(task, lease)

        executor._terminate.assert_awaited_once_with(process)
        broker.finalize_cancel_or_enqueue_rollback.assert_awaited_once_with(task, lease)

    asyncio.run(run_test())


def test_interrupted_second_commit_reuses_original_manifest_base_tree(monkeypatch):
    async def run_test():
        base_sha = "a" * 40
        repair_sha = "b" * 40
        format_sha = "c" * 40
        base_tree_sha = "d" * 40
        repair_tree_sha = "e" * 40
        format_tree_sha = "f" * 40
        first_entry = RepairCommitEntry(
            sequence=1,
            commit_sha=repair_sha,
            parent_sha=base_sha,
            tree_sha=repair_tree_sha,
            effect_id="repair-effect",
            task_marker="[pr-agent-task:task-541:push-attempt:1:marker]",
            pushed_at="2026-08-18T08:28:18+00:00",
        )
        manifest = RepairCommitManifest(
            repair_task_id="task-541",
            project_id="eabot/cook",
            mr_iid=541,
            source_branch="feature/fix",
            base_commit_sha=base_sha,
            base_tree_sha=base_tree_sha,
            authorized_actor_id="ou_owner",
            entries=(first_entry,),
        )
        metadata = {
            "base_sha": repair_sha,
            "base_tree_sha": repair_tree_sha,
            "commit_sha": format_sha,
            "parent_sha": repair_sha,
            "tree_sha": format_tree_sha,
            "source_branch": "feature/fix",
            "task_marker": "[pr-agent-task:task-541:format:marker]",
            "pushed_at": "2026-08-18T08:39:07+00:00",
            "attempt_sequence": 2,
        }
        broker = AsyncMock()
        broker.get_active_effect.return_value = (
            "task-541:format-commit:effect",
            EffectRecord("started", metadata),
        )
        broker.get_task_triage_card.return_value = SimpleNamespace(receive_id="ou_owner")
        broker.get_task.return_value = SimpleNamespace(repair_commit_manifest=manifest)
        task = make_task()
        lease = MrLease(task.mr, "worker-1", 7)
        executor = ProcessTaskExecutor(broker, "worker-1", max_active_tasks=1)

        provider = MagicMock()
        provider.mr.source_project_id = "eabot/cook"
        provider.id_project = "eabot/cook"
        provider.gl.projects.get.return_value.branches.get.return_value.commit = {"id": format_sha}
        monkeypatch.setattr(
            "pr_agent.git_providers.gitlab_provider.GitLabProvider",
            MagicMock(return_value=provider),
        )

        assert await executor._reconcile_interrupted_commit(task, lease) is True
        assert broker.append_repair_commit.await_args.kwargs["base_tree_sha"] == base_tree_sha
        broker.complete_effect.assert_awaited_once()

    asyncio.run(run_test())
