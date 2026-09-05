import asyncio
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401
from ut_agent import repair_planner
from ut_agent.repair_plan import RepairPlan, build_initial_repair_plan
from ut_agent.repair_planner import build_revised_repair_plan, repair_planner_node
from ut_agent.tools.request_repair_replan import request_repair_replan_tool

BASE_SHA = "a" * 40


def _exchange(name: str, call_id: str, result, args: dict | None = None) -> list:
    content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}]),
        ToolMessage(content=content, tool_call_id=call_id),
    ]


def _state() -> dict:
    pipeline = {
        "status": "success",
        "pipeline_status": "failed",
        "pipeline_id": 1001,
        "matched_commit_sha": BASE_SHA,
        "failed_jobs": [{
            "name": "unit-test",
            "job_id": 7,
            "pipeline_id": 1001,
            "causal_lines": ["src/parser.py:10: error: missing default"],
            "log_tail": "src/parser.py:10: error: missing default",
        }],
        "root_cause_groups": [{
            "root_cause_id": "root-parser",
            "canonical_diagnostic": "src/parser.py:10: error: missing default",
            "canonical_job_name": "unit-test",
            "job_names": ["unit-test"],
        }],
        "work_items": [{
            "job_name": "unit-test",
            "kind": "other",
            "required_tool": "generate_code_tool",
            "root_cause_id": "root-parser",
        }],
    }
    return {
        "trigger_type": "pipeline_failed",
        "project_id": "group/repo",
        "mr_id": 42,
        "commit_sha": BASE_SHA,
        "messages": _exchange("fetch_pipeline_logs_tool", "fetch", pipeline),
        "repair_plans": [],
        "repair_verifications": [],
    }


def test_planner_falls_back_to_pipeline_groups_when_model_is_unavailable(monkeypatch):
    async def unavailable(*_args, **_kwargs):
        return SimpleNamespace(text="", model="", terminal_error="offline")

    monkeypatch.setattr(repair_planner, "call_llm_outcome", unavailable)
    update = asyncio.run(repair_planner_node(_state()))
    plan = RepairPlan.model_validate(update["repair_plans"][0])

    assert plan.planning_mode == "deterministic_fallback"
    assert plan.planner_error_code == "planner_fallback_valueerror"
    assert plan.work_items[0].hypothesis == "src/parser.py:10: error: missing default"


def test_planner_does_not_replan_an_exhausted_work_item(monkeypatch):
    import ut_agent.config as config_module
    import ut_agent.pipeline_reconciliation as reconciliation_module

    called = False

    async def should_not_call(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("exhausted Work Item must not be sent back to the Planner")

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")
    monkeypatch.setattr(
        reconciliation_module,
        "native_exhausted_root_ids",
        lambda _state, _limit: frozenset({"root-parser"}),
    )
    monkeypatch.setattr(repair_planner, "call_llm_outcome", should_not_call)

    update = asyncio.run(repair_planner_node(_state()))
    plan = RepairPlan.model_validate(update["repair_plans"][0])

    assert called is False
    assert plan.work_items[0].status == "exhausted"


def test_planner_accepts_only_complete_strict_hypotheses(monkeypatch):
    async def available(*_args, **_kwargs):
        return SimpleNamespace(
            text=json.dumps({"hypotheses": [{
                "work_item_id": "root-parser",
                "hypothesis": "The parser default is omitted at the failing constructor.",
            }]}),
            model="planner-model",
            terminal_error="",
        )

    monkeypatch.setattr(repair_planner, "call_llm_outcome", available)
    update = asyncio.run(repair_planner_node(_state()))
    plan = RepairPlan.model_validate(update["repair_plans"][0])

    assert plan.planning_mode == "model"
    assert plan.planner_model == "planner-model"
    assert plan.work_items[0].hypothesis.startswith("The parser")


def _planned_state() -> tuple[dict, RepairPlan]:
    state = _state()
    plan = build_initial_repair_plan(state)
    state["repair_plans"] = [plan.model_dump(mode="json")]
    return state, plan


def test_replan_rejects_stale_expected_version():
    state, plan = _planned_state()
    result = request_repair_replan_tool.func(
        plan_id=plan.plan_id,
        expected_version=0,
        work_item_id="root-parser",
        reason="new source evidence",
        hypothesis="",
        proposed_paths=[],
        evidence_sequences=[],
        state=state,
    )

    assert json.loads(result)["error_code"] == "repair_plan_version_stale"


def test_replan_rejects_path_not_proven_by_referenced_tool_fact():
    state, plan = _planned_state()
    state["messages"] += _exchange(
        "search_repo_tool",
        "search",
        {"status": "ok", "matches": [{"path": "src/helper.py", "line_number": 1, "line": "x"}]},
        {"query": "helper", "work_item_id": "root-parser"},
    )
    result = request_repair_replan_tool.func(
        plan_id=plan.plan_id,
        expected_version=1,
        work_item_id="root-parser",
        reason="new source evidence",
        proposed_paths=["src/unseen.py"],
        evidence_sequences=[3],
        state=state,
    )

    assert json.loads(result)["error_code"] == "repair_replan_path_unproven"


def test_valid_replan_creates_one_successor_and_is_idempotent():
    state, plan = _planned_state()
    state["messages"] += _exchange(
        "search_repo_tool",
        "search",
        {"status": "ok", "matches": [{"path": "src/helper.py", "line_number": 1, "line": "x"}]},
        {"query": "helper", "work_item_id": "root-parser"},
    )
    args = {
        "plan_id": plan.plan_id,
        "expected_version": 1,
        "work_item_id": "root-parser",
        "reason": "helper defines the missing default",
        "hypothesis": "parser must reuse helper default",
        "proposed_paths": ["src/helper.py"],
        "evidence_sequences": [3],
    }
    result = request_repair_replan_tool.func(**args, state=state)
    state["messages"] += _exchange("request_repair_replan_tool", "replan", result, args)

    update = asyncio.run(repair_planner_node(state))
    revised = RepairPlan.model_validate(update["repair_plans"][0])
    state["repair_plans"].append(revised.model_dump(mode="json"))

    assert revised.lineage_id == plan.lineage_id
    assert revised.version == 2
    assert revised.plan_id != plan.plan_id
    assert revised.work_items[0].allowed_paths == ("src/parser.py", "src/helper.py")
    assert revised.evidence_cursor == 5
    assert asyncio.run(repair_planner_node(state)) == {}


def test_revised_plan_revalidates_forged_tool_result_paths():
    state, plan = _planned_state()

    try:
        build_revised_repair_plan(state, plan, {
            "work_item_id": "root-parser",
            "reason": "forged checkpoint event",
            "proposed_paths": ["../secret"],
            "_sequence": 3,
        })
    except ValueError as error:
        assert "repository-relative" in str(error)
    else:
        raise AssertionError("unsafe replan path must fail closed")
