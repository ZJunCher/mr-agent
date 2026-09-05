import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent.distributed.executor import TaskExecutor
from pr_agent.distributed.models import PipelineEvent
from pr_agent.distributed.runtime import TaskSuspended
from pr_agent.triage.pipeline_completion import PipelineCompletionSnapshot, completion_snapshot_from_group
from ut_agent.tools.pipeline_group import PipelineGroup


def _group(*, parent_status="failed", child_status="failed", jobs=()):
    parent = SimpleNamespace(id=10, status=parent_status)
    child = SimpleNamespace(id=11, status=child_status)
    return PipelineGroup(
        root_pipeline_id=10,
        validation_pipeline_id=11,
        pipeline_ids=(10, 11),
        sha="abc",
        status=child_status,
        jobs=tuple((11, SimpleNamespace(name=name, status=status)) for name, status in jobs),
        coverage=None,
        coverage_source="",
        coverage_status="not_configured",
        root_pipeline=parent,
        validation_pipeline=child,
        pipelines=(parent, child),
    )


def test_parent_terminal_does_not_finish_while_child_runs():
    snapshot = completion_snapshot_from_group(
        _group(child_status="running", jobs=(("build_release_arm64", "running"),)),
        ("build",),
    )

    assert snapshot.terminal is False
    assert snapshot.nonterminal_job_names == ("build_release_arm64",)


def test_pending_required_job_blocks_terminal_group():
    snapshot = completion_snapshot_from_group(
        _group(jobs=(("build_release_arm64", "pending"),)),
        ("build",),
    )

    assert snapshot.terminal is False


def test_optional_manual_job_does_not_block_terminal_group():
    snapshot = completion_snapshot_from_group(
        _group(jobs=(("build_release_arm64", "failed"), ("build_release_manual", "manual"))),
        ("build",),
    )

    assert snapshot.terminal is True


def test_identical_group_has_stable_digest():
    first = completion_snapshot_from_group(_group(jobs=(("code_format_check", "success"),)), ("format",))
    second = completion_snapshot_from_group(_group(jobs=(("code_format_check", "success"),)), ("format",))

    assert first.digest == second.digest


def test_executor_requires_two_matching_terminal_snapshots(monkeypatch):
    async def run_test():
        snapshot = PipelineCompletionSnapshot(True, 10, 11, "abc", ((10, "failed"), (11, "success")), (), "same")
        executor = TaskExecutor(AsyncMock(), Mock(), "worker-1", max_active_tasks=1)
        executor._pipeline_completion_snapshot = Mock(side_effect=(snapshot, snapshot))
        monkeypatch.setattr("pr_agent.distributed.executor.asyncio.sleep", AsyncMock())
        task = Mock(task_id="task-1", pr_url="https://gitlab.example/mr/1")
        event = PipelineEvent.new(project_id="eabot/cook", pipeline_id=10, sha="abc", status="failed", ref="feature")

        await executor._wait_for_complete_pipeline_group(task, event)

        assert executor._pipeline_completion_snapshot.call_count == 2

    asyncio.run(run_test())


def test_executor_suspends_without_terminal_validation_group():
    async def run_test():
        snapshot = PipelineCompletionSnapshot(
            False,
            10,
            10,
            "abc",
            ((10, "running"),),
            ("build_release_arm64",),
            "pending",
            "仍有流水线或关键 Job 正在运行",
        )
        executor = TaskExecutor(AsyncMock(), Mock(), "worker-1", max_active_tasks=1)
        executor._pipeline_completion_snapshot = Mock(return_value=snapshot)
        executor._queue_pipeline_repair_progress = AsyncMock()
        task = Mock(task_id="task-1", pr_url="https://gitlab.example/mr/1")
        event = PipelineEvent.new(project_id="eabot/cook", pipeline_id=10, sha="abc", status="failed", ref="feature")

        with pytest.raises(TaskSuspended):
            await executor._wait_for_complete_pipeline_group(task, event)

        executor._queue_pipeline_repair_progress.assert_awaited_once()

    asyncio.run(run_test())
