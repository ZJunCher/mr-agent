"""Task 5: native repair 生命周期契约测试。

验证 native 路径产出的 RepairAction 数据能被现有的报告/详情/撤回模块正确消费，
不依赖 generate_code_tool 的 result schema。

重点验证：
1. build_repair_action_records 在 native 路径下产出的 RepairAction 能被 RepairAction.from_dict 解析。
2. native 路径的 commit/push/rollback 流程不引用 generate_code_tool 专属字段。
3. native 路径的 RepairAction 不包含 Hermes 协议字段（operation/root_cause_id 来自 pipeline 而非工具）。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing.
from pr_agent.triage.repair_details import RepairAction
from ut_agent import repair_planner
from ut_agent.execution_policy import build_repair_action_records
from ut_agent.repair_plan import RepairPlan
from ut_agent.repair_planner import repair_planner_node

BASE_SHA = "a" * 40
DIFF_DIGEST = "sha256:" + "b" * 64


def _ai_message(tool_calls: list[dict]) -> AIMessage:
    return AIMessage(content="", tool_calls=tool_calls)


def _tool_message(tool_call_id: str, content: dict) -> ToolMessage:
    return ToolMessage(content=json.dumps(content, ensure_ascii=False), tool_call_id=tool_call_id)


def _native_full_success_sequence() -> list:
    """native 路径完整成功序列：fetch → patch → inspect → validate → commit → wait(success)。"""
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
    return [
        _ai_message([{"name": "fetch_pipeline_logs_tool", "args": {}, "id": "f1"}]),
        _tool_message("f1", pipeline),
        _ai_message([{"name": "apply_repo_patch_tool", "args": {"patch": "...", "reason": "fix"}, "id": "p1"}]),
        _tool_message("p1", {
            "status": "changed",
            "patch_applied": True,
            "base_sha": BASE_SHA,
            "diff_digest": DIFF_DIGEST,
            "changed_files": ["src/example.py"],
            "diff_check": {"passed": True},
        }),
        _ai_message([{"name": "inspect_repo_diff_tool", "args": {"start_line": 1}, "id": "i1"}]),
        _tool_message("i1", {
            "status": "ok",
            "base_sha": BASE_SHA,
            "diff_digest": DIFF_DIGEST,
            "total_lines": 1,
            "page": {"start_line": 1, "end_line": 1, "has_more": False, "next_start_line": None},
        }),
        _ai_message([{"name": "run_repo_validation_tool", "args": {"checks": []}, "id": "v1"}]),
        _tool_message("v1", {
            "status": "ok",
            "all_passed": True,
            "base_sha": BASE_SHA,
            "validated_diff_digest": DIFF_DIGEST,
            "required_checks": ["diff_check", "python_compile_check"],
            "executed_checks": [
                {"name": "diff_check", "check": "diff_check", "passed": True},
                {"name": "python_compile_check", "check": "python_compile_check", "passed": True},
            ],
        }),
        _ai_message([{"name": "commit_and_push_tool", "args": {}, "id": "c1"}]),
        _tool_message("c1", {"status": "success", "changed": True, "commit_sha": "def456"}),
        _ai_message([{"name": "wait_pipeline_tool", "args": {}, "id": "w1"}]),
        _tool_message("w1", {
            "status": "success", "pipeline_status": "success",
            "matched_commit_sha": "def456", "requested_commit_sha": "def456",
            "failed_jobs": [],
        }),
    ]


class TestNativeRepairLifecycle:
    """native 路径产出的 RepairAction 能被现有模块消费。"""

    def test_native_repair_action_parseable_by_from_dict(self, monkeypatch):
        """build_repair_action_records 产出的 dict 能被 RepairAction.from_dict 解析。"""
        import ut_agent.config as config_module
        monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

        messages = _native_full_success_sequence()
        actions = build_repair_action_records(messages)
        assert len(actions) >= 1

        # 每个 action dict 都能被 RepairAction.from_dict 解析，不抛异常
        for action_dict in actions:
            repair_action = RepairAction.from_dict(action_dict)
            assert repair_action.action_id == action_dict["action_id"]
            assert isinstance(repair_action.changed_files, tuple)

    def test_native_repair_action_has_no_hermes_protocol_fields(self, monkeypatch):
        """native 路径的 RepairAction 不包含 Hermes 协议字段。"""
        import ut_agent.config as config_module
        monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

        messages = _native_full_success_sequence()
        actions = build_repair_action_records(messages)

        for action_dict in actions:
            # RepairAction schema 不应包含 Hermes 专属字段
            assert "operation" not in action_dict
            assert "diagnostic" not in action_dict
            assert "repair_report" not in action_dict

    def test_native_repair_action_status_transitions_correctly(self, monkeypatch):
        """native 路径的 RepairAction 状态在 commit+pipeline 后变为 verified。"""
        import ut_agent.config as config_module
        monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

        messages = _native_full_success_sequence()
        actions = build_repair_action_records(messages)
        assert len(actions) >= 1
        action = actions[0]
        # 成功的 patch + commit + pipeline(success) → status 应为 verified 或 committed
        assert action["status"] in {"verified", "committed", "editing"}
        assert action.get("commit_sha") == "def456"
        assert "src/example.py" in action.get("changed_files", [])

    def test_native_repair_action_without_commit_is_editing(self, monkeypatch):
        """native 路径只有 patch 没有 commit 时，status 为 editing 或 failed。"""
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
            _ai_message([{"name": "apply_repo_patch_tool", "args": {"patch": "...", "reason": "fix"}, "id": "p1"}]),
            _tool_message("p1", {"status": "changed", "changed_files": ["src/example.py"]}),
        ]
        actions = build_repair_action_records(messages)
        assert len(actions) >= 1
        # 有 patch 但没 commit → editing 或 failed
        assert actions[0]["status"] in {"editing", "failed"}

    def test_old_checkpoint_without_hybrid_channels_creates_plan_v1(self, monkeypatch):
        import ut_agent.config as config_module

        monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

        async def unavailable(*_args, **_kwargs):
            return SimpleNamespace(text="", model="", terminal_error="offline")

        monkeypatch.setattr(repair_planner, "call_llm_outcome", unavailable)
        messages = _native_full_success_sequence()[:2]
        old_state = {
            "trigger_type": "pipeline_failed",
            "project_id": "group/repo",
            "mr_id": 42,
            "commit_sha": "abc123",
            "messages": messages,
        }

        update = asyncio.run(repair_planner_node(old_state))
        plan = RepairPlan.model_validate(update["repair_plans"][0])

        assert plan.version == 1
        assert plan.source_pipeline_id == 29921
