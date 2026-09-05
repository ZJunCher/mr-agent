"""Task 4b: native 路径的 build_repair_action_records 兼容性测试。

验证 native backend 下，build_repair_action_records 能从 apply_repo_patch_tool 的
attempt 提取 changed_files，不依赖 generate_code_tool 的 result schema。
"""
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing.
from ut_agent.execution_policy import build_repair_action_records


def _ai_message(tool_calls: list[dict]) -> AIMessage:
    return AIMessage(content="", tool_calls=tool_calls)


def _tool_message(tool_call_id: str, content: dict) -> ToolMessage:
    return ToolMessage(content=json.dumps(content, ensure_ascii=False), tool_call_id=tool_call_id)


def _pipeline_result_with_root_cause(job_name: str, root_cause_id: str, commit_sha: str = "abc123") -> dict:
    """构造一个带 root_cause_groups 的失败流水线结果。"""
    return {
        "status": "success",
        "pipeline_status": "failed",
        "pipeline_id": 29921,
        "requested_commit_sha": commit_sha,
        "matched_commit_sha": commit_sha,
        "failed_jobs": [{"name": job_name, "job_id": 1, "log_tail": "error: no member named foo"}],
        "root_cause_groups": [{
            "root_cause_id": root_cause_id,
            "canonical_diagnostic": "error: no member named foo",
            "canonical_job_name": job_name,
            "job_names": [job_name],
            "job_ids": [1],
            "pipeline_ids": [29921],
        }],
        "work_items": [{
            "job_name": job_name,
            "job_id": 1,
            "pipeline_id": 29921,
            "root_cause_id": root_cause_id,
            "canonical_job_name": job_name,
            "required_tool": "generate_code_tool",
        }],
    }


class TestNativeBuildRepairActions:
    """native backend 下 build_repair_action_records 的行为。"""

    def test_extracts_changed_files_from_apply_repo_patch(self, monkeypatch):
        """native 路径能从 apply_repo_patch_tool 提取 changed_files。"""
        import ut_agent.config as config_module
        monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

        job_name = "build_release"
        root_cause_id = "rc_abc123"
        commit_sha = "abc123"
        pipeline = _pipeline_result_with_root_cause(job_name, root_cause_id, commit_sha)

        messages = [
            _ai_message([{"name": "fetch_pipeline_logs_tool", "args": {}, "id": "f1"}]),
            _tool_message("f1", pipeline),
            _ai_message([{"name": "apply_repo_patch_tool", "args": {"patch": "...", "reason": "fix"}, "id": "p1"}]),
            _tool_message("p1", {"status": "changed", "changed_files": ["src/example.py"], "diff_check": {"passed": True}}),
            _ai_message([{"name": "inspect_repo_diff_tool", "args": {}, "id": "i1"}]),
            _tool_message("i1", {"status": "ok", "changed_files": ["src/example.py"], "diff": "..."}),
            _ai_message([{"name": "commit_and_push_tool", "args": {}, "id": "c1"}]),
            _tool_message("c1", {"status": "success", "changed": True, "commit_sha": "def456"}),
            _ai_message([{"name": "wait_pipeline_tool", "args": {}, "id": "w1"}]),
            _tool_message("w1", {
                "status": "success", "pipeline_status": "success",
                "matched_commit_sha": "def456", "requested_commit_sha": "def456",
                "failed_jobs": [],
            }),
        ]
        actions = build_repair_action_records(messages)
        assert len(actions) >= 1
        action = actions[0]
        assert "src/example.py" in action.get("changed_files", [])

    def test_no_generate_code_tool_in_native_path(self, monkeypatch):
        """native 路径的 build_repair_action_records 不依赖 generate_code_tool。"""
        import ut_agent.config as config_module
        monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

        job_name = "build_release"
        root_cause_id = "rc_abc123"
        commit_sha = "abc123"
        pipeline = _pipeline_result_with_root_cause(job_name, root_cause_id, commit_sha)

        messages = [
            _ai_message([{"name": "fetch_pipeline_logs_tool", "args": {}, "id": "f1"}]),
            _tool_message("f1", pipeline),
            _ai_message([{"name": "apply_repo_patch_tool", "args": {"patch": "...", "reason": "fix"}, "id": "p1"}]),
            _tool_message("p1", {"status": "changed", "changed_files": ["src/example.py"]}),
        ]
        actions = build_repair_action_records(messages)
        # 即使没有 generate_code_tool，也应该能提取 changed_files
        assert len(actions) >= 1
        assert "src/example.py" in actions[0].get("changed_files", [])
