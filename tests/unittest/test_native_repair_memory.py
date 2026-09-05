import asyncio
from types import SimpleNamespace

from ut_agent.repair_memory.models import MemoryScope, RepairMemoryHint, RetrievalMode, RetrievalResult
from ut_agent.repair_memory.native import (
    NativeRepairMemoryContext,
    build_native_repair_query,
    latest_native_memory_context,
    native_memory_required,
    repair_memory_node,
)
from ut_agent.repair_plan import build_initial_repair_plan
from ut_agent.prompt.agent_system import build_system_prompt

BASE_SHA = "a" * 40


def _state() -> dict:
    state = {
        "trigger_type": "pipeline_failed",
        "project_id": "group/repo",
        "mr_id": 42,
        "pipeline_id": 10,
        "commit_sha": BASE_SHA,
        "task_id": "task-42",
        "messages": [],
        "repair_plans": [],
        "repair_verifications": [],
        "repair_memory_contexts": [],
    }
    pipeline = {
        "pipeline_status": "failed",
        "pipeline_id": 10,
        "matched_commit_sha": BASE_SHA,
        "failed_jobs": [{"name": "cmake-build", "log_tail": "src/parser.cpp:7: undefined reference"}],
        "root_cause_groups": [{
            "root_cause_id": "root-parser",
            "canonical_diagnostic": "src/parser.cpp:7: undefined reference to parse_value",
            "job_names": ["cmake-build"],
        }],
        "work_items": [{
            "root_cause_id": "root-parser",
            "job_name": "cmake-build",
            "kind": "build",
            "required_tool": "generate_code_tool",
        }],
    }
    from tests.unittest.test_native_repair_hybrid_graph import _exchange

    state["messages"] = _exchange("fetch_pipeline_logs_tool", "fetch", pipeline)
    plan = build_initial_repair_plan(state)
    state["repair_plans"] = [plan.model_dump(mode="json")]
    return state


def _hint() -> RepairMemoryHint:
    return RepairMemoryHint(
        memory_id="memory-1",
        scope=MemoryScope.PROJECT,
        pattern_key="undefined-symbol",
        score=88,
        match_reasons=("failure_family",),
        problem_pattern="A declaration has no linked implementation.",
        applicability=("CMake build",),
        anti_conditions=(),
        repair_guidance="Inspect the target source list.",
        validation_guidance=("Build the affected target.",),
        support_episode_count=3,
        support_project_count=1,
        confidence=0.8,
    )


def test_build_query_is_bound_to_current_plan_and_work_item():
    query = build_native_repair_query(_state())

    assert query.project == "group/repo"
    assert query.root_cause_group_id == "root-parser"
    assert query.source_pipeline_id == 10
    assert query.source_sha == BASE_SHA
    assert query.failure_category == "build"
    assert query.failure_family == "undefined_symbol"
    assert query.language == "cpp"
    assert query.build_system == "cmake"
    assert query.diagnostic_fingerprint
    assert "parse_value" in query.causal_tokens


def test_matching_event_suppresses_duplicate_retrieval_but_new_plan_does_not():
    state = _state()
    plan = state["repair_plans"][-1]
    state["repair_memory_contexts"] = [NativeRepairMemoryContext(
        plan_id=plan["plan_id"],
        plan_version=plan["version"],
        work_item_id="root-parser",
        status="no_match",
        created_at="2026-08-27T00:00:00+00:00",
    ).model_dump(mode="json")]

    assert native_memory_required(state) is False
    assert latest_native_memory_context(state).status == "no_match"

    state["repair_plans"][-1] = {**plan, "version": 2, "plan_id": "b" * 64}
    assert native_memory_required(state) is True
    assert latest_native_memory_context(state) is None


def test_memory_node_records_and_renders_injected_hints(monkeypatch):
    state = _state()
    monkeypatch.setattr(
        "ut_agent.repair_memory.native.load_repair_memory_settings",
        lambda: SimpleNamespace(
            retrieval_mode=RetrievalMode.INJECT,
            project_allowlist=("*",),
            max_prompt_chars=2000,
        ),
    )
    monkeypatch.setattr("ut_agent.repair_memory.native.initialize_retrieval_audit", lambda **_kwargs: True)
    monkeypatch.setattr("ut_agent.repair_memory.native.record_retrieval_injection", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "ut_agent.repair_memory.native.retrieve_repair_hints",
        lambda **_kwargs: RetrievalResult(RetrievalMode.INJECT, "attempt-1", (_hint(),), True, 2000),
    )

    update = asyncio.run(repair_memory_node(state))
    event = NativeRepairMemoryContext.model_validate(update["repair_memory_contexts"][0])

    assert event.status == "injected"
    assert event.memory_ids == ("memory-1",)
    assert "UNTRUSTED HISTORICAL REPAIR HINTS" in event.prompt_block


def test_memory_node_fails_open_and_records_error(monkeypatch):
    state = _state()
    monkeypatch.setattr(
        "ut_agent.repair_memory.native.load_repair_memory_settings",
        lambda: SimpleNamespace(retrieval_mode=RetrievalMode.INJECT, project_allowlist=("*",)),
    )
    monkeypatch.setattr("ut_agent.repair_memory.native.initialize_retrieval_audit", lambda **_kwargs: True)

    def fail(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("ut_agent.repair_memory.native.retrieve_repair_hints", fail)
    monkeypatch.setattr("ut_agent.repair_memory.native.record_retrieval_error", lambda *_args, **_kwargs: True)

    update = asyncio.run(repair_memory_node(state))
    event = NativeRepairMemoryContext.model_validate(update["repair_memory_contexts"][0])

    assert event.status == "error"
    assert event.error_code == "RuntimeError"
    assert native_memory_required({**state, "repair_memory_contexts": update["repair_memory_contexts"]}) is False


def test_only_matching_injected_event_is_added_to_native_system_prompt(monkeypatch):
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")
    state = _state()
    plan = state["repair_plans"][-1]
    state["repair_memory_contexts"] = [NativeRepairMemoryContext(
        plan_id=plan["plan_id"],
        plan_version=plan["version"],
        work_item_id="root-parser",
        status="injected",
        attempt_id="attempt-1",
        memory_ids=("memory-1",),
        prompt_block="[UNTRUSTED HISTORICAL REPAIR HINTS]\ninspect target sources",
        created_at="2026-08-27T00:00:00+00:00",
    ).model_dump(mode="json")]

    prompt = build_system_prompt(state, "tools")

    assert "UNTRUSTED HISTORICAL REPAIR HINTS" in prompt
    assert "inspect target sources" in prompt
