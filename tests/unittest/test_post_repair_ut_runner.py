import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pr_agent.distributed.models import MrKey, PipelineEvent, TaskEnvelope, TaskKind
from pr_agent.distributed.post_repair_ut_runner import PostRepairUTRunner


def _task() -> TaskEnvelope:
    return TaskEnvelope.new(
        kind=TaskKind.POST_REPAIR_UT,
        source="feishu",
        mr=MrKey("eabot/cook", 549),
        pr_url="https://gitlab/eabot/cook/-/merge_requests/549",
        command="/ut",
        payload={
            "baseline_pipeline_id": 33141,
            "baseline_sha": "a" * 40,
            "coverage_before": 63.04,
            "coverage_status_before": "reported",
        },
        idempotency_key="ut-549",
    )


def _provider():
    return SimpleNamespace(
        pr=SimpleNamespace(
            title="tests",
            iid=549,
            author={"username": "jun.zhao"},
            target_branch="dev",
        ),
        id_project="eabot/cook",
        get_pr_branch=Mock(return_value="feature/tests"),
    )


def test_runner_starts_ut_agent_with_feishu_specific_trigger(monkeypatch):
    async def run_test():
        provider = _provider()
        agent = Mock()
        agent.run = AsyncMock(return_value={"response": "done", "result": {"success": True}})
        monkeypatch.setattr(
            "pr_agent.distributed.post_repair_ut_runner.get_git_provider_with_context", lambda _url: provider
        )
        monkeypatch.setattr("pr_agent.distributed.post_repair_ut_runner.serialize_diff_files", lambda _provider: [])
        monkeypatch.setattr("pr_agent.distributed.post_repair_ut_runner.UTAgent", lambda checkpointer=None: agent)
        monkeypatch.setattr("pr_agent.distributed.post_repair_ut_runner.init_context", lambda **_kwargs: "token")
        reset = Mock()
        monkeypatch.setattr("pr_agent.distributed.post_repair_ut_runner.reset_context", reset)

        result = await PostRepairUTRunner(checkpointer="cp").run(_task())

        state = agent.run.await_args.args[0]
        assert state["trigger_type"] == "feishu_post_repair_ut"
        assert state["commit_sha"] == "a" * 40
        assert state["coverage_before"] == 63.04
        assert result["result"]["success"] is True
        reset.assert_called_once_with("token")

    asyncio.run(run_test())


def test_runner_resumes_existing_graph_instead_of_starting_again(monkeypatch):
    async def run_test():
        provider = _provider()
        agent = Mock()
        agent.resume = AsyncMock(return_value={"response": "done", "result": {"success": True}})
        monkeypatch.setattr(
            "pr_agent.distributed.post_repair_ut_runner.get_git_provider_with_context", lambda _url: provider
        )
        monkeypatch.setattr("pr_agent.distributed.post_repair_ut_runner.serialize_diff_files", lambda _provider: [])
        monkeypatch.setattr("pr_agent.distributed.post_repair_ut_runner.UTAgent", lambda checkpointer=None: agent)
        monkeypatch.setattr("pr_agent.distributed.post_repair_ut_runner.init_context", lambda **_kwargs: "token")
        monkeypatch.setattr("pr_agent.distributed.post_repair_ut_runner.reset_context", Mock())
        event = PipelineEvent.new(
            project_id="eabot/cook",
            pipeline_id=33142,
            sha="b" * 40,
            status="success",
            ref="feature/tests",
        )
        task = _task()

        await PostRepairUTRunner(checkpointer="cp").resume(task, event)

        agent.resume.assert_awaited_once_with(task.task_id, event)

    asyncio.run(run_test())
