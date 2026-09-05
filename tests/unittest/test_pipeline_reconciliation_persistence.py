import asyncio
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
from pr_agent.config_loader import get_settings


get_settings()


def _reconciliation():
    return {
        "previous_pipeline_id": 41,
        "previous_requested_commit_sha": "old-sha",
        "previous_matched_commit_sha": "old-sha",
        "current_pipeline_id": 42,
        "current_requested_commit_sha": "new-sha",
        "current_matched_commit_sha": "new-sha",
        "transitions": [
            {
                "root_cause_id": "root-a",
                "status": "resolved",
                "previous_job_names": ["build"],
                "current_job_names": [],
            },
            {
                "root_cause_id": "root-b",
                "status": "introduced",
                "previous_job_names": [],
                "current_job_names": ["unit_test"],
            },
        ],
        "transitions_truncated": False,
    }


def _pipeline_result():
    return {
        "status": "success",
        "pipeline_id": 42,
        "validation_pipeline_id": 42,
        "requested_commit_sha": "new-sha",
        "matched_commit_sha": "new-sha",
        "pipeline_status": "failed",
        "failed_jobs": [
            {
                "job_id": 2,
                "pipeline_id": 42,
                "name": "unit_test",
                "status": "failed",
                "log_tail": "x" * 7000,
                "causal_lines": ["AssertionError: expected 2, got 1"],
            }
        ],
        "observed_jobs": [
            {"pipeline_id": 42, "job_id": 1, "name": "build", "status": "success"},
            {"pipeline_id": 42, "job_id": 2, "name": "unit_test", "status": "failed"},
        ],
        "failure_reconciliation": _reconciliation(),
    }


def _exchange(result):
    return [
        AIMessage(
            content="",
            tool_calls=[{"name": "fetch_pipeline_logs_tool", "args": {}, "id": "pipeline"}],
        ),
        ToolMessage(content=json.dumps(result), tool_call_id="pipeline"),
    ]


def test_pipeline_tool_compaction_preserves_observations_and_reconciliation():
    from ut_agent.llm import _truncate_tool_results

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "pipeline", "function": {"name": "fetch_pipeline_logs_tool"}},
            ],
        },
        {"role": "tool", "tool_call_id": "pipeline", "content": json.dumps(_pipeline_result())},
    ]

    compacted = json.loads(_truncate_tool_results(messages)[1]["content"])

    assert compacted["observed_jobs"] == _pipeline_result()["observed_jobs"]
    assert compacted["failure_reconciliation"] == _reconciliation()


def test_pipeline_compaction_never_silently_drops_oversized_reconciliation():
    from ut_agent.llm import _compact_pipeline_result

    pipeline = _pipeline_result()
    pipeline["observed_jobs"] = [
        {
            "pipeline_id": 42,
            "job_id": index,
            "name": f"job-{index}-" + "x" * 280,
            "status": "failed",
        }
        for index in range(20)
    ]
    pipeline["failure_reconciliation"]["transitions"] = [
        {
            "root_cause_id": f"root-{index}-" + "r" * 60,
            "status": "persistent",
            "previous_job_names": ["previous-" + "p" * 280] * 20,
            "current_job_names": ["current-" + "c" * 280] * 20,
        }
        for index in range(20)
    ]

    compacted = _compact_pipeline_result(json.dumps(pipeline), 5_000)
    parsed = json.loads(compacted)

    assert len(compacted) <= 5_000
    assert parsed["observed_jobs"]
    assert parsed["failure_reconciliation"]["transitions"]
    assert parsed["failure_reconciliation"]["transitions_truncated"] is True


def test_rough_context_summary_keeps_pipeline_transition_facts():
    from ut_agent.llm import _summarize_messages

    summary = _summarize_messages([
        {"role": "tool", "tool_call_id": "pipeline", "content": json.dumps(_pipeline_result())},
    ])

    assert "build=success" in summary
    assert "unit_test=failed" in summary
    assert "root-a=resolved" in summary
    assert "root-b=introduced" in summary


def test_llm_context_summary_appends_pipeline_transition_facts(monkeypatch):
    import ut_agent.llm as llm_module

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="普通摘要"))],
    )

    async def completion(*_args, **_kwargs):
        return SimpleNamespace(terminal_error="", response=response)

    monkeypatch.setattr(llm_module, "_completion_with_failover", completion)

    summary = asyncio.run(llm_module._llm_summarize_messages([
        {"role": "tool", "tool_call_id": "pipeline", "content": json.dumps(_pipeline_result())},
    ]))

    assert "普通摘要" in summary
    assert "Pipeline Job 状态: build=success, unit_test=failed" in summary
    assert "Pipeline 根因变化: root-a=resolved, root-b=introduced" in summary


def test_final_result_keeps_pipeline_observations_and_reconciliation():
    from ut_agent.agent import _extract_result

    pipeline = _pipeline_result()
    messages = _exchange(pipeline)

    result = _extract_result(
        {"messages": messages, "iteration": 1, "max_iterations": 30},
        messages,
    )

    assert result["observed_jobs"] == pipeline["observed_jobs"]
    assert result["failure_reconciliation"] == pipeline["failure_reconciliation"]
    assert result["pipeline_groups"][0]["observed_jobs"] == pipeline["observed_jobs"]
    assert result["pipeline_groups"][0]["failure_reconciliation"] == pipeline["failure_reconciliation"]
