import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.distributed.runtime as runtime_module
from ut_agent.tools import fetch_pipeline as pipeline_tools


def _pipeline_group(*, sha="new-sha"):
    jobs = (
        (102, SimpleNamespace(id=2, name="unit_test", status="success")),
        (102, SimpleNamespace(id=1, name="build", status="failed")),
    )
    pipeline = SimpleNamespace(id=102, sha=sha, status="failed")
    return SimpleNamespace(
        terminal=True,
        validation_pipeline=pipeline,
        validation_pipeline_id=102,
        root_pipeline_id=101,
        pipeline_ids=(101, 102),
        status="failed",
        coverage=None,
        coverage_source="",
        coverage_status="missing",
        resolution_source="causal_graph",
        jobs=jobs,
    )


def _messages_for_pipeline(result, *, call_id="pipeline"):
    return [
        AIMessage(
            content="",
            tool_calls=[{"name": "fetch_pipeline_logs_tool", "args": {}, "id": call_id}],
        ),
        ToolMessage(content=json.dumps(result), tool_call_id=call_id),
    ]


def test_fetch_pipeline_logs_persists_all_observed_jobs_and_reconciliation(monkeypatch):
    group = _pipeline_group()
    project = SimpleNamespace(
        pipelines=SimpleNamespace(get=lambda _pipeline_id: group.validation_pipeline),
    )
    provider = SimpleNamespace(
        id_project="eabot/demo",
        gl=SimpleNamespace(projects=SimpleNamespace(get=lambda _project_id: project)),
    )
    monkeypatch.setattr(pipeline_tools, "get_git_provider", lambda: provider)
    monkeypatch.setattr(pipeline_tools, "_resolve_group", lambda *_args: group)
    monkeypatch.setattr(
        pipeline_tools,
        "_get_failed_job_diagnostics",
        lambda *_args: "src/navigation.cpp:8: error: missing symbol",
    )

    result = json.loads(
        pipeline_tools.fetch_pipeline_logs_tool.func(
            pipeline_id=102,
            commit_sha="new-sha",
            state={"project_id": "eabot/demo", "messages": []},
        )
    )

    assert result["observed_jobs"] == [
        {"pipeline_id": 102, "job_id": 1, "name": "build", "status": "failed"},
        {"pipeline_id": 102, "job_id": 2, "name": "unit_test", "status": "success"},
    ]
    assert result["observed_jobs_truncated"] is False
    assert result["failure_reconciliation"]["transitions"] == [
        {
            "root_cause_id": result["root_cause_groups"][0]["root_cause_id"],
            "status": "introduced",
            "previous_job_names": [],
            "current_job_names": ["build"],
        }
    ]


def test_failure_reconciliation_never_compares_same_exact_sha_with_itself(monkeypatch):
    captured = []
    monkeypatch.setattr(
        pipeline_tools,
        "reconcile_pipeline_failures",
        lambda previous, current: captured.append(previous) or {"transitions": []},
    )
    current = {
        "status": "success",
        "pipeline_status": "failed",
        "pipeline_id": 102,
        "requested_commit_sha": "same-sha",
        "matched_commit_sha": "same-sha",
        "root_cause_groups": [],
        "observed_jobs": [],
    }

    result = pipeline_tools._with_failure_reconciliation(
        current,
        {"messages": _messages_for_pipeline(current)},
    )

    assert captured == [None]
    assert result["failure_reconciliation"] == {"transitions": []}


def test_failure_reconciliation_uses_latest_terminal_different_exact_sha(monkeypatch):
    captured = []
    monkeypatch.setattr(
        pipeline_tools,
        "reconcile_pipeline_failures",
        lambda previous, current: captured.append(previous) or {"transitions": []},
    )
    old = {
        "status": "success",
        "pipeline_status": "failed",
        "pipeline_id": 100,
        "requested_commit_sha": "old-sha",
        "matched_commit_sha": "old-sha",
        "root_cause_groups": [],
        "observed_jobs": [],
    }
    newer = {**old, "pipeline_id": 101, "requested_commit_sha": "newer-sha", "matched_commit_sha": "newer-sha"}
    running = {**old, "pipeline_id": 103, "pipeline_status": "running"}
    mismatched = {**old, "pipeline_id": 104, "requested_commit_sha": "requested", "matched_commit_sha": "other"}
    current = {**old, "pipeline_id": 105, "requested_commit_sha": "current-sha", "matched_commit_sha": "current-sha"}
    messages = []
    for index, pipeline in enumerate((old, newer, running, mismatched), start=1):
        messages.extend(_messages_for_pipeline(pipeline, call_id=f"pipeline-{index}"))

    pipeline_tools._with_failure_reconciliation(current, {"messages": messages})

    assert captured == [{**newer, "_sequence": 3}]


def test_inline_wait_attaches_failure_reconciliation(monkeypatch):
    pipeline = {
        "status": "success",
        "pipeline_status": "failed",
        "pipeline_id": 105,
        "requested_commit_sha": "current-sha",
        "matched_commit_sha": "current-sha",
        "failed_jobs": [],
        "work_items": [],
        "root_cause_groups": [],
        "observed_jobs": [
            {"pipeline_id": 105, "job_id": 1, "name": "build", "status": "success"},
        ],
    }
    monkeypatch.setattr(runtime_module, "get_execution_runtime", lambda: None)
    monkeypatch.setattr(pipeline_tools, "fetch_pipeline_feedback", lambda _sha: dict(pipeline))
    monkeypatch.setattr(
        pipeline_tools,
        "reconcile_pipeline_failures",
        lambda previous, current: {"transitions": [], "current_pipeline_id": current["pipeline_id"]},
    )

    result = json.loads(
        pipeline_tools.wait_pipeline_tool.func(
            commit_sha="current-sha",
            state={"messages": []},
        )
    )

    assert result["failure_reconciliation"] == {"transitions": [], "current_pipeline_id": 105}


def test_inline_feedback_keeps_nonfailed_terminal_jobs_out_of_repair_plan(monkeypatch):
    jobs = (
        SimpleNamespace(id=1, name="build_release_arm64", status="failed"),
        SimpleNamespace(id=2, name="build_release_arm64_cancel", status="canceled"),
        SimpleNamespace(id=3, name="build_release_arm64_skip", status="skipped"),
        SimpleNamespace(id=4, name="build_release_arm64_manual", status="manual"),
    )

    class Jobs:
        def list(self, **_kwargs):
            return list(jobs)

    pipeline = SimpleNamespace(id=102, sha="new-sha", status="failed", jobs=Jobs())
    group = SimpleNamespace(
        terminal=True,
        validation_pipeline=pipeline,
        validation_pipeline_id=102,
        root_pipeline_id=102,
        pipeline_ids=(102,),
        status="failed",
        coverage=None,
        coverage_source="",
        coverage_status="missing",
        resolution_source="anchor_only",
        jobs=tuple((102, job) for job in jobs),
    )
    project = SimpleNamespace(
        pipelines=SimpleNamespace(
            list=lambda **_kwargs: [pipeline],
            get=lambda _pipeline_id: pipeline,
        ),
    )
    provider = SimpleNamespace(
        id_project="eabot/demo",
        gl=SimpleNamespace(projects=SimpleNamespace(get=lambda _project_id: project)),
    )
    monkeypatch.setattr(pipeline_tools, "get_git_provider", lambda: provider)
    monkeypatch.setattr(pipeline_tools, "_resolve_group", lambda *_args: group)
    monkeypatch.setattr(pipeline_tools.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        pipeline_tools,
        "_get_failed_job_diagnostics",
        lambda *_args: "src/navigation.cpp:8: error: missing symbol",
    )

    result = pipeline_tools.fetch_pipeline_feedback("new-sha")

    assert [job["status"] for job in result["failed_jobs"]] == ["failed"]
    assert len(result["work_items"]) == 1
    assert {job["status"] for job in result["observed_jobs"]} == {
        "failed",
        "canceled",
        "skipped",
        "manual",
    }
    assert result["observed_jobs_truncated"] is False


@pytest.mark.parametrize(
    ("job_name", "expected_kind"),
    (
        ("compile_cpp", "build"),
        ("cmake_configure", "build"),
        ("ninja_release", "build"),
        ("colcon_packages", "build"),
        ("clang-tidy", "lint"),
        ("static_analysis", "lint"),
    ),
)
def test_cpp_and_ros_validation_jobs_select_the_required_local_check(job_name, expected_kind):
    kind, required_tool = pipeline_tools._classify_failed_job(job_name)

    assert kind == expected_kind
    assert required_tool == "generate_code_tool"


def test_generic_job_with_cpp_linker_diagnostic_requires_build_validation():
    kind, required_tool = pipeline_tools._classify_failed_job(
        "verify",
        {"causal_lines": ["navigation.cpp:42: undefined reference to `Navigation::start()`"]},
    )

    assert kind == "build"
    assert required_tool == "generate_code_tool"


def test_generic_job_with_assertion_diagnostic_requires_test_validation():
    kind, required_tool = pipeline_tools._classify_failed_job(
        "verify",
        {"primary_diagnostic": {"signal": "AssertionError", "text": "expected 2, got 1"}},
    )

    assert kind == "test"
    assert required_tool == "generate_code_tool"
