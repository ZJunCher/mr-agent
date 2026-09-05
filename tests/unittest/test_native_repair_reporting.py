"""Task 6: native repair 报告兼容测试。

验证 native 路径的用户可见报告不包含 Hermes 协议字段或文案。
方案验收标准：用户可见文案不得出现 Hermes。
"""
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing.
from ut_agent.execution_policy import build_repair_action_records, build_failure_explanation_records


def _ai_message(tool_calls: list[dict]) -> AIMessage:
    return AIMessage(content="", tool_calls=tool_calls)


def _tool_message(tool_call_id: str, content: dict) -> ToolMessage:
    return ToolMessage(content=json.dumps(content, ensure_ascii=False), tool_call_id=tool_call_id)


def _native_success_sequence() -> tuple[list, dict]:
    """返回 (messages, pipeline) 用于 native 成功场景。"""
    pipeline = {
        "status": "success",
        "pipeline_status": "failed",
        "pipeline_id": 29921,
        "requested_commit_sha": "abc123",
        "matched_commit_sha": "abc123",
        "failed_jobs": [{"name": "build_release", "job_id": 1, "log_tail": "error: no member named foo"}],
        "root_cause_groups": [{
            "root_cause_id": "rc_abc",
            "canonical_diagnostic": "error: no member named foo",
            "canonical_job_name": "build_release",
            "job_names": ["build_release"],
            "job_ids": [1],
            "pipeline_ids": [29921],
        }],
        "work_items": [{
            "job_name": "build_release",
            "job_id": 1,
            "pipeline_id": 29921,
            "root_cause_id": "rc_abc",
            "canonical_job_name": "build_release",
            "required_tool": "generate_code_tool",
        }],
    }
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
    return messages, pipeline


class TestNativeRepairReporting:
    """native 路径报告不包含 Hermes 文案。"""

    def test_repair_actions_no_hermes_in_user_visible_fields(self, monkeypatch):
        """RepairAction 的用户可见字段不包含 Hermes。"""
        import ut_agent.config as config_module
        monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

        messages, _ = _native_success_sequence()
        actions = build_repair_action_records(messages)
        assert len(actions) >= 1

        for action in actions:
            # 检查所有用户可见字段
            for field in ["solution_summary", "rationale", "failure_reason", "evidence", "root_cause"]:
                value = str(action.get(field, ""))
                assert "Hermes" not in value, f"字段 {field} 包含 Hermes: {value}"
                assert "hermes" not in value.lower(), f"字段 {field} 包含 hermes: {value}"

    def test_failure_explanations_empty_for_native_path(self, monkeypatch):
        """native 路径不调用 generate_code_tool，failure_explanations 应为空。"""
        import ut_agent.config as config_module
        monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

        messages, pipeline = _native_success_sequence()
        explanations = build_failure_explanation_records(messages, pipeline)
        # build_failure_explanation_records 只扫描 generate_code_tool，
        # native 路径不调用它，所以返回空列表
        assert explanations == []

    def test_repair_action_changed_files_from_real_diff(self, monkeypatch):
        """native 路径的 changed_files 来自 apply_repo_patch_tool 的真实 diff。"""
        import ut_agent.config as config_module
        monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

        messages, _ = _native_success_sequence()
        actions = build_repair_action_records(messages)
        assert len(actions) >= 1
        action = actions[0]
        assert action["changed_files"] == ["src/example.py"]
        # 不应包含工具过程文本
        assert "patch" not in str(action["changed_files"]).lower()

    def test_repair_action_no_changes_not_reported_as_success(self, monkeypatch):
        """native 路径无改动时不报告为成功。"""
        import ut_agent.config as config_module
        monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

        pipeline = {
            "status": "success",
            "pipeline_status": "failed",
            "pipeline_id": 29921,
            "requested_commit_sha": "abc123",
            "matched_commit_sha": "abc123",
            "failed_jobs": [{"name": "build_release", "job_id": 1, "log_tail": "error"}],
            "root_cause_groups": [{
                "root_cause_id": "rc_abc",
                "canonical_diagnostic": "error",
                "canonical_job_name": "build_release",
                "job_names": ["build_release"],
                "job_ids": [1],
                "pipeline_ids": [29921],
            }],
            "work_items": [],
        }
        messages = [
            _ai_message([{"name": "fetch_pipeline_logs_tool", "args": {}, "id": "f1"}]),
            _tool_message("f1", pipeline),
            # 只有诊断，没有 patch
            _ai_message([{"name": "search_repo_tool", "args": {"query": "foo"}, "id": "s1"}]),
            _tool_message("s1", {"status": "ok", "matches": []}),
        ]
        actions = build_repair_action_records(messages)
        # 无改动时不应有 verified 状态
        for action in actions:
            assert action["status"] != "verified"
            assert action.get("commit_sha", "") == ""
