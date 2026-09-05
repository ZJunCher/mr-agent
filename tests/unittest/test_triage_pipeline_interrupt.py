import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from pr_agent.distributed.broker import MrLease
from pr_agent.distributed.executor import TaskExecutor
from pr_agent.distributed.models import MrKey, PipelineEvent, TaskEnvelope, TaskKind, TaskStatus
from pr_agent.distributed.runtime import ExecutionRuntime, TaskSuspended, execution_context
from ut_agent.agent import UTAgent, _coverage_note
from ut_agent.tools import fetch_pipeline as pipeline_tools


def make_triage_task():
    return TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="gitlab",
        mr=MrKey("eabot/cook", 536),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        command="/triage",
        payload={},
        idempotency_key="note:triage",
    )


def test_queue_wait_uses_cached_terminal_event_without_polling(monkeypatch):
    event = PipelineEvent.new(
        project_id="eabot/cook",
        pipeline_id=29415,
        sha="abc",
        status="success",
        ref="feature/x",
    )
    sync_broker = Mock()
    sync_broker.register_pipeline_wait.return_value = event
    runtime = ExecutionRuntime("task-1", "worker-1", None, "queue", AsyncMock(), sync_broker)
    monkeypatch.setattr(
        pipeline_tools.fetch_pipeline_logs_tool,
        "func",
        lambda **kwargs: json.dumps({"status": "success", "coverage": 63.04, **kwargs}, default=str),
    )
    polling = Mock(side_effect=AssertionError("polling must not run in queue mode"))
    monkeypatch.setattr(pipeline_tools, "fetch_pipeline_feedback", polling)

    with execution_context(runtime):
        result = json.loads(
            pipeline_tools.wait_pipeline_tool.func(commit_sha="abc", state={"project_id": "eabot/cook"})
        )

    assert result["pipeline_id"] == 29415
    assert result["coverage"] == 63.04
    polling.assert_not_called()


def test_parent_event_re_registers_wait_for_running_validation_child(monkeypatch):
    parent = PipelineEvent.new(
        project_id="eabot/cook",
        pipeline_id=29920,
        sha="abc",
        status="success",
        ref="feature/x",
    )
    child = PipelineEvent.new(
        project_id="eabot/cook",
        pipeline_id=29921,
        sha="abc",
        status="failed",
        ref="feature/x",
        source="parent_pipeline",
    )
    sync_broker = Mock()
    sync_broker.register_pipeline_wait.side_effect = [parent, None]
    runtime = ExecutionRuntime("task-1", "worker-1", None, "queue", AsyncMock(), sync_broker)
    fetch_results = iter([
        {
            "status": "running",
            "root_pipeline_id": 29920,
            "validation_pipeline_id": 29921,
            "pipeline_status": "running",
        },
        {
            "status": "success",
            "root_pipeline_id": 29920,
            "validation_pipeline_id": 29921,
            "pipeline_id": 29921,
            "pipeline_status": "failed",
        },
    ])
    monkeypatch.setattr(
        pipeline_tools.fetch_pipeline_logs_tool,
        "func",
        lambda **_kwargs: json.dumps(next(fetch_results)),
    )
    monkeypatch.setattr(pipeline_tools, "interrupt", lambda _payload: child.to_dict())

    with execution_context(runtime):
        result = json.loads(
            pipeline_tools.wait_pipeline_tool.func(commit_sha="abc", state={"project_id": "eabot/cook"})
        )

    assert result["pipeline_id"] == 29921
    assert result["pipeline_status"] == "failed"
    assert sync_broker.register_pipeline_wait.call_args_list == [
        call("task-1", "eabot/cook", "abc", attempt_id="", pipeline_id=None),
        call("task-1", "eabot/cook", "abc", attempt_id="", pipeline_id=29921),
    ]


def test_old_pipeline_event_deserializes_without_source():
    value = PipelineEvent.new(
        project_id="eabot/cook",
        pipeline_id=29920,
        sha="abc",
        status="success",
        ref="feature/x",
    ).to_dict()
    value.pop("source")

    assert PipelineEvent.from_dict(value).source == ""


def test_ut_agent_interrupt_becomes_task_suspended():
    class Graph:
        async def ainvoke(self, graph_input, config=None):
            return {
                "__interrupt__": [
                    SimpleNamespace(value={"kind": "pipeline", "project_id": "eabot/cook", "commit_sha": "abc"})
                ]
            }

    async def run_test():
        agent = UTAgent.__new__(UTAgent)
        agent.graph = Graph()
        agent.checkpointer = None

        with pytest.raises(TaskSuspended) as suspended:
            await agent._invoke_graph({}, {"configurable": {"thread_id": "task-1"}})

        assert suspended.value.task_id == "task-1"
        assert suspended.value.wait_identity == "eabot/cook:abc"

    asyncio.run(run_test())


def test_queue_run_does_not_swallow_task_suspended():
    async def run_test():
        runtime = ExecutionRuntime("task-1", "worker-1", None, "queue", AsyncMock(), Mock())
        agent = UTAgent.__new__(UTAgent)
        agent._run = AsyncMock(side_effect=TaskSuspended("task-1", "pipeline", "eabot/cook:abc"))

        with execution_context(runtime), pytest.raises(TaskSuspended):
            await agent.run({})

        agent._run.assert_awaited_once_with({})

    asyncio.run(run_test())


def test_executor_persists_waiting_state_on_interrupt():
    async def run_test():
        task = make_triage_task()
        lease = MrLease(task.mr, "worker-1", 7)
        broker = AsyncMock()
        broker.transition_task.return_value = True
        agent = Mock()
        agent.handle_request = AsyncMock(side_effect=TaskSuspended(task.task_id, "pipeline", "eabot/cook:abc"))
        executor = TaskExecutor(
            broker,
            Mock(),
            "worker-1",
            max_active_tasks=1,
            agent_factory=Mock(return_value=agent),
        )

        with pytest.raises(TaskSuspended):
            await executor.execute(task, lease)

        broker.transition_task.assert_any_await(
            task.task_id,
            {TaskStatus.RUNNING},
            TaskStatus.WAITING_PIPELINE,
            lease,
            {"wait_kind": "pipeline", "wait_identity": "eabot/cook:abc"},
        )

    asyncio.run(run_test())


def test_coverage_is_included_in_finished_summary_note():
    messages = [
        AIMessage(content="", tool_calls=[{"name": "wait_pipeline_tool", "args": {}, "id": "call-1"}]),
        ToolMessage(
            content=json.dumps(
                {
                    "status": "success",
                    "pipeline_status": "success",
                    "coverage": 63.04,
                    "failed_jobs": [],
                }
            ),
            tool_call_id="call-1",
        ),
    ]

    assert _coverage_note(messages) == "变更行覆盖率 63.04%。"
