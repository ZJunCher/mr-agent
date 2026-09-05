import asyncio
import json
from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401
from ut_agent import repair_planner
from ut_agent.agent import (
    build_graph,
    route_after_planner,
    route_after_tools,
    route_after_verifier,
    route_from_start,
)
from ut_agent.repair_memory.native import NativeRepairMemoryContext
from ut_agent.execution_policy import validate_tool_call
from ut_agent.pipeline_actions import next_mandatory_pipeline_action
from ut_agent.repair_plan import (
    RepairVerification,
    RepairWorkItem,
    active_work_item,
    build_initial_repair_plan,
)

BASE_SHA = "a" * 40
DIFF_DIGEST = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def native_backend(monkeypatch):
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")


def _exchange(name: str, call_id: str, result: dict, args: dict | None = None) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}]),
        ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call_id),
    ]


def _pipeline() -> dict:
    return {
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


def _state(*, planned: bool = True, validated: bool = False) -> dict:
    state = {
        "trigger_type": "pipeline_failed",
        "project_id": "group/repo",
        "mr_id": 42,
        "commit_sha": BASE_SHA,
        "messages": _exchange("fetch_pipeline_logs_tool", "fetch", _pipeline()),
        "repair_plans": [],
        "repair_verifications": [],
        "repair_memory_contexts": [],
        "iteration": 1,
        "max_iterations": 30,
        "workspace_snapshot": {"status": "ready"},
    }
    if planned:
        plan = build_initial_repair_plan(state)
        state["repair_plans"] = [plan.model_dump(mode="json")]
    if validated:
        item_id = "root-parser"
        state["messages"] += _exchange("apply_repo_patch_tool", "patch", {
            "status": "changed",
            "patch_applied": True,
            "base_sha": BASE_SHA,
            "diff_digest": DIFF_DIGEST,
            "changed_files": ["src/parser.py"],
            "work_item_id": item_id,
        })
        state["messages"] += _exchange("inspect_repo_diff_tool", "inspect", {
            "status": "ok",
            "base_sha": BASE_SHA,
            "diff_digest": DIFF_DIGEST,
            "total_lines": 1,
            "page": {"start_line": 1, "end_line": 1},
            "diff": "diff",
            "work_item_id": item_id,
        })
        state["messages"] += _exchange("run_repo_validation_tool", "validate", {
            "status": "ok",
            "all_passed": True,
            "base_sha": BASE_SHA,
            "validated_diff_digest": DIFF_DIGEST,
            "required_checks": ["diff_check", "test_check"],
            "executed_checks": [
                {"name": "diff_check", "passed": True},
                {"name": "test_check", "passed": True},
            ],
            "work_item_id": item_id,
        })
    return state


def _pass_verification(state: dict, *, verdict: str = "pass") -> dict:
    plan = build_initial_repair_plan({**state, "repair_plans": []})
    current_plan = state["repair_plans"][-1]
    plan = plan.model_validate(current_plan)
    return RepairVerification(
        plan_id=plan.plan_id,
        lineage_id=plan.lineage_id,
        plan_version=plan.version,
        work_item_id="root-parser",
        baseline_sha=BASE_SHA,
        diff_digest=DIFF_DIGEST,
        verdict=verdict,
        causal_alignment=verdict == "pass",
        scope_compliant=verdict == "pass",
        evidence_sufficient=verdict == "pass",
        covered_work_item_ids=("root-parser",) if verdict == "pass" else (),
        reason="Independent semantic verification.",
        model="verifier-model",
        created_at=datetime.now(timezone.utc).isoformat(),
    ).model_dump(mode="json")


def _two_item_state() -> dict:
    state = _state(validated=True)
    state["repair_plans"][0]["work_items"].append(RepairWorkItem(
        work_item_id="root-coverage",
        job_names=("coverage",),
        kind="coverage",
        required_tool="apply_repo_patch_tool",
        failure_signature="root-coverage",
        failure_evidence=("Coverage remains below threshold.",),
        hypothesis="The parser branch is missing a unit test.",
        allowed_paths=("tests/test_parser.py",),
        required_checks=("diff_check", "test_check"),
    ).model_dump(mode="json"))
    return state


def test_failed_pipeline_tool_result_routes_to_planner():
    state = _state(planned=False)

    assert route_after_tools(state) == "planner"
    assert route_from_start(state) == "planner"


def test_planned_work_item_routes_through_memory_once():
    state = _state()

    assert route_from_start(state) == "repair_memory"
    assert route_after_planner(state) == "repair_memory"

    plan = state["repair_plans"][-1]
    state["repair_memory_contexts"] = [NativeRepairMemoryContext(
        plan_id=plan["plan_id"],
        plan_version=plan["version"],
        work_item_id="root-parser",
        status="no_match",
        created_at="2026-08-27T00:00:00+00:00",
    ).model_dump(mode="json")]

    assert route_from_start(state) == "agent"
    assert route_after_planner(state) == "agent"


def test_old_checkpoint_runs_through_graph_and_persists_plan_v1(monkeypatch):
    async def unavailable(*_args, **_kwargs):
        return type("Outcome", (), {"text": "", "model": "", "terminal_error": "offline"})()

    monkeypatch.setattr(repair_planner, "call_llm_outcome", unavailable)
    state = _state(planned=False)
    state.pop("repair_plans")
    state.pop("repair_verifications")
    state["max_iterations"] = 0

    result = asyncio.run(build_graph().ainvoke(state))

    assert result["repair_plans"][0]["version"] == 1
    assert result.get("repair_verifications", []) == []


def test_successful_native_validation_routes_to_verifier():
    assert route_after_tools(_state(validated=True)) == "verifier"


def test_same_diff_is_reverified_when_active_sibling_was_not_covered():
    state = _two_item_state()
    state["repair_verifications"] = [_pass_verification(state)]
    assert active_work_item(state).work_item_id == "root-coverage"
    state["messages"] += _exchange("run_repo_validation_tool", "validate-sibling", {
        "status": "ok",
        "all_passed": True,
        "base_sha": BASE_SHA,
        "validated_diff_digest": DIFF_DIGEST,
        "required_checks": ["diff_check", "test_check"],
        "executed_checks": [
            {"name": "diff_check", "passed": True},
            {"name": "test_check", "passed": True},
        ],
        "work_item_id": "root-coverage",
    })

    assert route_after_tools(state) == "verifier"
    patch_calls = [
        message
        for message in state["messages"]
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == "apply_repo_patch_tool"
    ]
    assert len(patch_calls) == 1


def test_replan_verdict_routes_back_to_planner():
    state = _state(validated=True)
    state["repair_verifications"] = [_pass_verification(state, verdict="replan")]

    assert route_after_verifier(state) == "planner"


def test_passing_verifier_unlocks_plan_aware_commit():
    state = _state(validated=True)
    state["repair_verifications"] = [_pass_verification(state)]

    action = next_mandatory_pipeline_action(state)
    assert action.name == "commit_and_push_tool"
    assert validate_tool_call(state, "commit_and_push_tool", {}) == (True, "")


def test_missing_verifier_keeps_commit_locked():
    state = _state(validated=True)

    allowed, reason = validate_tool_call(state, "commit_and_push_tool", {})

    assert allowed is False
    assert "repair_plan_work_items_pending" in reason


def test_hermes_and_non_pipeline_routes_remain_agent(monkeypatch):
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "hermes")
    assert route_after_tools(_state(planned=False)) == "agent"
    assert route_from_start({"trigger_type": "mr_created", "messages": []}) == "agent"
