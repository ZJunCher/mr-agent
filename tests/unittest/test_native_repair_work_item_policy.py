import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401
from ut_agent.execution_ledger import build_execution_ledger
from ut_agent.execution_policy import validate_tool_call
from ut_agent.native_repair_state import evaluate_native_commit
from ut_agent.repair_plan import build_initial_repair_plan

BASE_SHA = "a" * 40
DIFF_DIGEST = "sha256:" + "b" * 64
PATCH = """diff --git a/src/parser.py b/src/parser.py
--- a/src/parser.py
+++ b/src/parser.py
@@ -1 +1 @@
-old
+new
"""


@pytest.fixture(autouse=True)
def native_backend(monkeypatch):
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")


def _exchange(name: str, call_id: str, result: dict, args: dict | None = None) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}]),
        ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call_id),
    ]


def _state(*, planned: bool = True) -> dict:
    pipeline = {
        "status": "success",
        "pipeline_status": "failed",
        "pipeline_id": 10,
        "matched_commit_sha": BASE_SHA,
        "failed_jobs": [{"name": "test", "log_tail": "src/parser.py:1: error"}],
        "root_cause_groups": [{
            "root_cause_id": "root-parser",
            "canonical_diagnostic": "src/parser.py:1: error",
            "job_names": ["test"],
        }],
        "work_items": [{
            "root_cause_id": "root-parser",
            "job_name": "test",
            "kind": "other",
            "required_tool": "generate_code_tool",
        }],
    }
    state = {
        "trigger_type": "pipeline_failed",
        "project_id": "group/repo",
        "mr_id": 42,
        "commit_sha": BASE_SHA,
        "messages": _exchange("fetch_pipeline_logs_tool", "fetch", pipeline),
        "repair_plans": [],
        "repair_verifications": [],
    }
    if planned:
        state["repair_plans"] = [build_initial_repair_plan(state).model_dump(mode="json")]
    return state


def test_native_repository_tool_requires_current_plan_and_work_item():
    allowed, reason = validate_tool_call(
        _state(planned=False),
        "search_repo_tool",
        {"query": "parser", "work_item_id": "root-parser"},
    )

    assert allowed is False
    assert "RepairPlan" in reason


def test_native_patch_must_match_active_work_item():
    allowed, reason = validate_tool_call(
        _state(),
        "apply_repo_patch_tool",
        {"work_item_id": "other", "patch": PATCH, "reason": "fix"},
    )

    assert allowed is False
    assert "当前 Work Item" in reason


def test_native_format_adapter_must_match_active_work_item():
    allowed, reason = validate_tool_call(
        _state(),
        "apply_format_report_tool",
        {"pipeline_id": 10, "job_id": 7, "work_item_id": "other"},
    )

    assert allowed is False
    assert "当前 Work Item" in reason


def test_native_patch_rejects_unplanned_path():
    patch = PATCH.replace("src/parser.py", "src/unplanned.py")
    allowed, reason = validate_tool_call(
        _state(),
        "apply_repo_patch_tool",
        {"work_item_id": "root-parser", "patch": patch, "reason": "fix"},
    )

    assert allowed is False
    assert "受控路径" in reason
    assert "request_repair_replan_tool" in reason


def test_native_patch_accepts_current_item_and_planned_path():
    assert validate_tool_call(
        _state(),
        "apply_repo_patch_tool",
        {"work_item_id": "root-parser", "patch": PATCH, "reason": "fix"},
    ) == (True, "")


def test_historical_inspection_can_be_reused_for_the_same_cumulative_diff():
    messages = [
        *_exchange("apply_repo_patch_tool", "patch", {
            "status": "changed",
            "patch_applied": True,
            "base_sha": BASE_SHA,
            "diff_digest": DIFF_DIGEST,
            "changed_files": ["src/parser.py"],
            "work_item_id": "root-parser",
        }),
        *_exchange("inspect_repo_diff_tool", "inspect", {
            "status": "ok",
            "base_sha": BASE_SHA,
            "diff_digest": DIFF_DIGEST,
            "total_lines": 2,
            "page": {"start_line": 1, "end_line": 2},
            "work_item_id": "other",
        }),
    ]
    attempts = build_execution_ledger(messages).tool_attempts

    assert evaluate_native_commit(attempts).error_code == "native_validation_missing"


def test_native_format_adapter_produces_patch_evidence():
    messages = [
        *_exchange("apply_format_report_tool", "format", {
            "status": "changed",
            "patch_applied": True,
            "base_sha": BASE_SHA,
            "diff_digest": DIFF_DIGEST,
            "changed_files": ["src/parser.py"],
            "work_item_id": "root-parser",
        }),
        *_exchange("inspect_repo_diff_tool", "inspect", {
            "status": "ok",
            "base_sha": BASE_SHA,
            "diff_digest": DIFF_DIGEST,
            "total_lines": 1,
            "page": {"start_line": 1, "end_line": 1},
            "work_item_id": "root-parser",
        }),
        *_exchange("run_repo_validation_tool", "validation", {
            "status": "ok",
            "all_passed": True,
            "base_sha": BASE_SHA,
            "validated_diff_digest": DIFF_DIGEST,
            "required_checks": ["diff_check"],
            "executed_checks": [{"name": "diff_check", "passed": True}],
            "work_item_id": "root-parser",
        }),
    ]

    decision = evaluate_native_commit(build_execution_ledger(messages).tool_attempts)

    assert decision.allowed is True
    assert decision.validated_base_sha == BASE_SHA


def test_non_pipeline_native_tool_keeps_optional_work_item_compatibility():
    assert validate_tool_call(
        {"trigger_type": "mr_created", "messages": []},
        "search_repo_tool",
        {"query": "parser"},
    ) == (True, "")
