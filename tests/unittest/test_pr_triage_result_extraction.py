"""UTAgent._run 返回 dict + PRTriage 异常落盘单测。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


def _fake_agent_result(content: str, messages=None):
    """模拟 graph.ainvoke 返回。"""
    msgs = messages if messages is not None else [{"role": "assistant", "content": content}]
    return {"messages": msgs, "iteration": 5, "max_iterations": 30}


def _ready_workspace():
    return SimpleNamespace(status="ready", error_code="", message="ready", to_dict=lambda: {"status": "ready"})


def test_run_returns_dict_with_response_and_result():
    from ut_agent.agent import UTAgent

    agent = UTAgent()
    with patch.object(agent, "graph") as mock_graph:
        mock_graph.ainvoke = AsyncMock(return_value=_fake_agent_result("FINISHED: 修复完成"))
        result = asyncio.run(agent.run({"mr_id": 1, "trigger_type": "pipeline_failed"}))

    assert isinstance(result, dict)
    assert "response" in result
    assert "result" in result
    assert "FINISHED" in result["response"]
    assert "success" in result["result"]
    assert "iterations" in result["result"]
    assert result["result"]["iterations"] == 5


def test_run_result_contains_ledger_fields():
    """result 含 pushed_sha/final_pipeline_status/failure_signatures 字段（即使为空）。"""
    from ut_agent.agent import UTAgent

    agent = UTAgent()
    with patch.object(agent, "graph") as mock_graph:
        mock_graph.ainvoke = AsyncMock(return_value=_fake_agent_result("FINISHED: done"))
        result = asyncio.run(agent.run({"mr_id": 1, "trigger_type": "pipeline_failed"}))

    r = result["result"]
    for key in ("pushed_sha", "final_pipeline_status", "failure_signatures", "finish_reason"):
        assert key in r, f"missing key {key}"


def test_extract_result_preserves_validated_dependency_blocker():
    from ut_agent import agent as agent_module

    pipeline = {
        "status": "success",
        "pipeline_id": 33871,
        "pipeline_status": "failed",
        "requested_commit_sha": "source-sha",
        "matched_commit_sha": "source-sha",
        "failed_jobs": [{"name": "build_release_arm64", "status": "failed"}],
        "work_items": [{
            "job_name": "build_release_arm64",
            "canonical_job_name": "build_release_arm64",
            "root_cause_id": "root-prism",
            "required_tool": "generate_code_tool",
        }],
    }
    blocker = {
        "schema_version": 1,
        "outcome": "blocked",
        "job_name": "build_release_arm64",
        "blocker_type": "external_dependency",
        "root_cause": "当前声明分支缺少已观察接口。",
        "ci_evidence": [{"job_name": "build_release_arm64", "observation": "fatal error: interface missing"}],
        "repository_evidence": [{
            "kind": "declared_dependency",
            "locator": "eabot/lhotse@dev-sha",
            "observation": "dev 分支缺少接口",
        }],
        "attempted_repairs": ["只读核验依赖分支。"],
        "why_no_safe_repo_change": "当前仓库不能安全生成上游接口。",
        "suggested_action": "请维护者确认候选分支后人工调整依赖。",
    }
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "pipeline",
                "type": "function",
                "function": {"name": "fetch_pipeline_logs_tool", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "pipeline", "content": json.dumps(pipeline)},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "dependency",
                "type": "function",
                "function": {
                    "name": "resolve_dependency_evidence_tool",
                    "arguments": json.dumps({
                        "job_name": "build_release_arm64",
                        "root_cause_id": "root-prism",
                    }),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "dependency",
            "content": json.dumps({
                "status": "blocked",
                "job_name": "build_release_arm64",
                "root_cause_id": "root-prism",
                "blocker": blocker,
                "dependency_evidence": {
                    "project_path": "eabot/lhotse",
                    "declared_branch": "dev",
                    "declared_sha": "dev-sha",
                },
            }),
        },
    ]

    result = agent_module._extract_result({"iteration": 2, "max_iterations": 30}, messages)

    assert result["success"] == 0
    assert result["result_pipeline_id"] == 0
    assert result["final_pipeline_status"] == "unknown"
    assert result["pipeline_groups"][-1]["validation_pipeline_id"] == 33871
    assert result["blocked_job_names"] == ["build_release_arm64"]
    assert result["dependency_blockers"][0]["root_cause_id"] == "root-prism"
    assert result["dependency_blockers"][0]["dependency_evidence"]["project_path"] == "eabot/lhotse"


def test_run_fallback_when_result_extraction_fails():
    """graph 返回异常结构时降级为 success=0。"""
    from ut_agent.agent import UTAgent

    agent = UTAgent()
    with patch.object(agent, "graph") as mock_graph:
        mock_graph.ainvoke = AsyncMock(return_value={})  # 无 messages
        result = asyncio.run(agent.run({"mr_id": 1, "trigger_type": "pipeline_failed"}))

    assert result["result"]["success"] == 0
    assert "error" in result["result"]


def test_pr_triage_saves_on_success(tmp_path):
    """PRTriage.run 成功路径调 save_triage_run。"""
    from pr_agent.tools.pr_triage import PRTriage

    saved = []

    class FakeAgent:
        async def run(self, mr_info):
            return {"response": "FINISHED: ok", "result": {"success": 1, "iterations": 3,
                    "max_iterations": 30, "pushed_sha": "sha1", "final_pipeline_status": "success",
                    "failure_signatures": [], "finish_reason": "", "error": None}}

    triage = PRTriage.__new__(PRTriage)
    triage.pr_url = "https://gitlab.example.com/g/r/-/merge_requests/1"
    triage.args = None

    class FakeProvider:
        pr = type("P", (), {"title": "t", "iid": 1, "target_branch": "main"})()
        id_project = "g/r"
        def get_pr_branch(self): return "feature/x"
        def publish_comment(self, *a, **k): pass
        def remove_initial_comment(self): pass

    triage.git_provider = FakeProvider()
    with patch("pr_agent.tools.pr_triage.get_settings") as gs, \
         patch("pr_agent.tools.pr_triage.UTAgent", FakeAgent), \
         patch("pr_agent.tools.pr_triage.save_triage_run", side_effect=lambda r, path=None: saved.append(r) or True), \
         patch("pr_agent.tools.pr_triage.serialize_diff_files", return_value=[]), \
         patch("ut_agent.workspace.prepare_workspace", return_value=_ready_workspace()), \
         patch("pr_agent.tools.pr_triage.PRTriage._fetch_failed_pipeline_info", return_value=([], None, None)):
        gs.return_value.config.publish_output = False
        asyncio.run(triage.run())

    assert len(saved) == 1
    assert saved[0]["success"] == 1
    assert saved[0]["fix_duration_ms"] >= 0


def test_pr_triage_saves_on_exception(tmp_path):
    """PRTriage.run 异常路径也落盘 success=0。"""
    from pr_agent.tools.pr_triage import PRTriage

    saved = []

    class BoomAgent:
        async def run(self, mr_info):
            raise RuntimeError("agent crashed")

    triage = PRTriage.__new__(PRTriage)
    triage.pr_url = "https://gitlab.example.com/g/r/-/merge_requests/1"
    triage.args = None

    class FakeProvider:
        pr = type("P", (), {"title": "t", "iid": 1, "target_branch": "main"})()
        id_project = "g/r"
        def get_pr_branch(self): return "feature/x"
        def publish_comment(self, *a, **k): pass
        def remove_initial_comment(self): pass

    triage.git_provider = FakeProvider()
    with patch("pr_agent.tools.pr_triage.get_settings") as gs, \
         patch("pr_agent.tools.pr_triage.UTAgent", BoomAgent), \
         patch("pr_agent.tools.pr_triage.save_triage_run", side_effect=lambda r, path=None: saved.append(r) or True), \
         patch("pr_agent.tools.pr_triage.serialize_diff_files", return_value=[]), \
         patch("ut_agent.workspace.prepare_workspace", return_value=_ready_workspace()), \
         patch("pr_agent.tools.pr_triage.PRTriage._fetch_failed_pipeline_info", return_value=([], None, None)):
        gs.return_value.config.publish_output = False
        asyncio.run(triage.run())

    assert len(saved) == 1
    assert saved[0]["success"] == 0
    assert "agent crashed" in saved[0]["error"]


def test_pr_triage_publishes_structured_result_with_coverage():
    from pr_agent.tools.pr_triage import PRTriage

    published = []
    saved = []

    class FakeAgent:
        async def run(self, _mr_info):
            return {
                "response": "FINISHED: success=True, summary=修复完成",
                "result": {
                    "success": 1,
                    "pushed_sha": "abc123",
                    "final_pipeline_status": "success",
                    "final_coverage": 63.04,
                    "coverage_source": "changed_lines",
                    "coverage_status": "reported",
                },
            }

    class FakeProvider:
        pr = type("P", (), {"title": "t", "iid": 1, "target_branch": "main"})()
        id_project = "g/r"

        def get_pr_branch(self):
            return "feature/x"

        def publish_comment(self, *_args, **_kwargs):
            return None

        def publish_triage_result(self, content, *, success, details):
            published.append((content, success, details))

        def remove_initial_comment(self):
            return None

    triage = PRTriage.__new__(PRTriage)
    triage.pr_url = "https://gitlab.example.com/g/r/-/merge_requests/1"
    triage.args = None
    triage.git_provider = FakeProvider()
    with patch("pr_agent.tools.pr_triage.get_settings") as settings, \
         patch("pr_agent.tools.pr_triage.UTAgent", FakeAgent), \
         patch("pr_agent.tools.pr_triage.save_triage_run", side_effect=lambda record: saved.append(record) or True), \
         patch("pr_agent.tools.pr_triage.serialize_diff_files", return_value=[]), \
         patch("ut_agent.workspace.prepare_workspace", return_value=_ready_workspace()), \
         patch("pr_agent.tools.pr_triage.PRTriage._fetch_failed_pipeline_info", return_value=([], None, None)):
        settings.return_value.config.publish_output = True
        asyncio.run(triage.run())

    assert len(published) == 1
    assert published[0][1] is True
    assert published[0][2]["final_coverage"] == 63.04
    assert published[0][2]["coverage_source"] == "changed_lines"
    assert published[0][2]["coverage_status"] == "reported"
    assert saved[0]["extra"]["coverage_source"] == "changed_lines"
    assert saved[0]["extra"]["coverage_status"] == "reported"
    assert published[0][2]["duration_ms"] >= 0


def test_pr_triage_can_return_result_without_publishing():
    from pr_agent.tools.pr_triage import PRTriage

    class FakeAgent:
        async def run(self, _mr_info):
            return {"response": "FINISHED", "result": {"success": 1, "final_pipeline_status": "success"}}

    provider = Mock()
    provider.pr = type("P", (), {"title": "t", "iid": 1, "target_branch": "main"})()
    provider.id_project = "g/r"
    provider.get_pr_branch.return_value = "feature/x"
    triage = PRTriage.__new__(PRTriage)
    triage.pr_url = "https://gitlab.example.com/g/r/-/merge_requests/1"
    triage.args = None
    triage.git_provider = provider

    with patch("pr_agent.tools.pr_triage.get_settings") as settings, \
         patch("pr_agent.tools.pr_triage.UTAgent", FakeAgent), \
         patch("pr_agent.tools.pr_triage.save_triage_run", return_value=True), \
         patch("pr_agent.tools.pr_triage.serialize_diff_files", return_value=[]), \
         patch("ut_agent.workspace.prepare_workspace", return_value=_ready_workspace()), \
         patch("pr_agent.tools.pr_triage.PRTriage._fetch_failed_pipeline_info", return_value=([], None, None)):
        settings.return_value.config.publish_output = True
        result = asyncio.run(triage.run(publish_result=False))

    assert result["result"]["success"] == 1
    provider.publish_comment.assert_not_called()
    provider.publish_triage_result.assert_not_called()
    provider.remove_initial_comment.assert_not_called()


def test_pr_triage_can_skip_intermediate_persistence():
    from pr_agent.tools.pr_triage import PRTriage

    class FakeAgent:
        async def run(self, _mr_info):
            return {"response": "FINISHED", "result": {"success": 1, "final_pipeline_status": "success"}}

    provider = Mock()
    provider.pr = type("P", (), {"title": "t", "iid": 1, "target_branch": "main"})()
    provider.id_project = "g/r"
    provider.get_pr_branch.return_value = "feature/x"
    triage = PRTriage.__new__(PRTriage)
    triage.pr_url = "https://gitlab.example.com/g/r/-/merge_requests/1"
    triage.args = None
    triage.git_provider = provider

    with patch("pr_agent.tools.pr_triage.get_settings") as settings, \
         patch("pr_agent.tools.pr_triage.UTAgent", FakeAgent), \
         patch("pr_agent.tools.pr_triage.save_triage_run", return_value=True) as save, \
         patch("pr_agent.tools.pr_triage.serialize_diff_files", return_value=[]), \
         patch("ut_agent.workspace.prepare_workspace", return_value=_ready_workspace()), \
         patch("pr_agent.tools.pr_triage.PRTriage._fetch_failed_pipeline_info", return_value=([], None, None)):
        settings.return_value.config.publish_output = True
        result = asyncio.run(triage.run(publish_result=False, persist_result=False))

    assert result["result"]["success"] == 1
    save.assert_not_called()


def test_pr_triage_uses_persisted_lifecycle_across_resume_boundaries():
    from pr_agent.tools.pr_triage import PRTriage

    runtime = SimpleNamespace(
        record_lifecycle_sync=Mock(),
        lifecycle_summary_sync=lambda: SimpleNamespace(
            processing_total_ms=90_000,
            to_dict=lambda: {
                "processing_total_ms": 90_000,
                "pipeline_wait_duration_ms": 60_000,
            },
        ),
    )
    triage = PRTriage.__new__(PRTriage)
    result = {"result": {"success": 1}}

    with patch.object(PRTriage, "_runtime", return_value=runtime):
        triage._finalize_timing(result, 0.0)

    assert result["result"]["processing_total_ms"] == 90_000
    assert result["result"]["duration_ms"] == 90_000
    assert result["result"]["duration_breakdown"]["pipeline_wait_duration_ms"] == 60_000
