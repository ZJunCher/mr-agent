import json
from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing.
from ut_agent.native_repair_state import NativeCommitDecision
from ut_agent.repair_plan import (
    _required_checks,
    RepairPlan,
    RepairVerification,
    RepairWorkItem,
    active_work_item,
    build_initial_repair_plan,
    latest_repair_plan,
    plan_identity_for_revision,
    repair_plan_audit,
    repair_plan_commit_decision,
    repair_plan_required,
)

NOW = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
BASE_SHA = "a" * 40
DIFF_DIGEST = "sha256:" + "b" * 64


def _exchange(name: str, call_id: str, result: dict, args: dict | None = None) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}]),
        ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call_id),
    ]


def _pipeline(pipeline_id: int = 1001, *, second: bool = False) -> dict:
    groups = [{
        "root_cause_id": "root-parser",
        "normalized_diagnostic": "src/parser.py error missing default",
        "canonical_diagnostic": "src/parser.py:10: error: missing default",
        "canonical_job_name": "unit-test",
        "job_names": ["unit-test"],
        "job_ids": [7],
        "pipeline_ids": [pipeline_id],
    }]
    work_items = [{
        "job_id": 7,
        "pipeline_id": pipeline_id,
        "job_name": "unit-test",
        "kind": "other",
        "required_tool": "generate_code_tool",
        "root_cause_id": "root-parser",
        "canonical_job_name": "unit-test",
    }]
    failed_jobs = [{
        "name": "unit-test",
        "job_id": 7,
        "pipeline_id": pipeline_id,
        "causal_lines": ["src/parser.py:10: error: missing default"],
        "log_tail": "src/parser.py:10: error: missing default",
    }]
    if second:
        groups.append({
            "root_cause_id": "root-build",
            "normalized_diagnostic": "src/build.cpp undefined reference",
            "canonical_diagnostic": "src/build.cpp:20: undefined reference",
            "canonical_job_name": "build",
            "job_names": ["build"],
            "job_ids": [8],
            "pipeline_ids": [pipeline_id],
        })
        work_items.append({
            "job_id": 8,
            "pipeline_id": pipeline_id,
            "job_name": "build",
            "kind": "build",
            "required_tool": "generate_code_tool",
            "root_cause_id": "root-build",
            "canonical_job_name": "build",
        })
        failed_jobs.append({
            "name": "build",
            "job_id": 8,
            "pipeline_id": pipeline_id,
            "causal_lines": ["src/build.cpp:20: undefined reference"],
            "log_tail": "src/build.cpp:20: undefined reference",
        })
    return {
        "status": "success",
        "pipeline_status": "failed",
        "pipeline_id": pipeline_id,
        "requested_commit_sha": BASE_SHA,
        "matched_commit_sha": BASE_SHA,
        "failed_jobs": failed_jobs,
        "root_cause_groups": groups,
        "work_items": work_items,
    }


def _state(pipeline_id: int = 1001, *, second: bool = False) -> dict:
    return {
        "trigger_type": "pipeline_failed",
        "project_id": "group/repo",
        "mr_id": 42,
        "commit_sha": BASE_SHA,
        "messages": _exchange("fetch_pipeline_logs_tool", "fetch", _pipeline(pipeline_id, second=second)),
        "repair_plans": [],
        "repair_verifications": [],
    }


def _pass(plan: RepairPlan, work_item_id: str, covered: tuple[str, ...], *, digest: str = DIFF_DIGEST) -> dict:
    return RepairVerification(
        plan_id=plan.plan_id,
        lineage_id=plan.lineage_id,
        plan_version=plan.version,
        work_item_id=work_item_id,
        baseline_sha=plan.baseline_sha,
        diff_digest=digest,
        verdict="pass",
        causal_alignment=True,
        scope_compliant=True,
        evidence_sufficient=True,
        covered_work_item_ids=covered,
        reason="Diff directly fixes the recorded failure.",
        model="model-b",
        created_at=NOW.isoformat(),
    ).model_dump(mode="json")


def test_repair_work_item_rejects_unknown_fields_and_unsafe_paths():
    raw = {
        "work_item_id": "root-parser",
        "job_names": ["unit-test"],
        "kind": "other",
        "required_tool": "apply_repo_patch_tool",
        "failure_signature": "root-parser",
        "failure_evidence": ["src/parser.py:10: error"],
        "allowed_paths": ["../secret"],
        "extra": True,
    }

    with pytest.raises(ValidationError):
        RepairWorkItem.model_validate(raw)


def test_initial_plan_is_bounded_to_pipeline_identity_and_diagnostic_paths():
    first = build_initial_repair_plan(_state(), now=NOW)
    second = build_initial_repair_plan(_state(1002), now=NOW)

    assert first.plan_id != second.plan_id
    assert first.lineage_id != second.lineage_id
    assert first.version == 1
    assert first.baseline_sha == BASE_SHA
    assert first.source_pipeline_id == 1001
    assert first.work_items[0].allowed_paths == ("src/parser.py",)
    assert first.work_items[0].failure_signature == "root-parser"
    assert first.evidence_cursor == 1


@pytest.mark.parametrize("kinds", (("lint", "build"), ("build", "lint")))
def test_grouped_jobs_union_every_required_validation_check(kinds):
    pipeline = _pipeline()
    pipeline["root_cause_groups"] = [{
        "root_cause_id": "root-shared",
        "canonical_diagnostic": "src/navigation.cpp:42: undefined reference",
        "canonical_job_name": "clang-tidy",
        "job_names": ["clang-tidy", "compile_cpp"],
    }]
    names = {"lint": "clang-tidy", "build": "compile_cpp"}
    pipeline["work_items"] = [
        {
            "job_id": index,
            "pipeline_id": pipeline["pipeline_id"],
            "job_name": names[kind],
            "kind": kind,
            "required_tool": "generate_code_tool",
            "root_cause_id": "root-shared",
            "canonical_job_name": "clang-tidy",
        }
        for index, kind in enumerate(kinds, start=1)
    ]
    pipeline["failed_jobs"] = [
        {"name": names[kind], "job_id": index, "status": "failed"}
        for index, kind in enumerate(kinds, start=1)
    ]
    state = _state()
    state["messages"] = _exchange("fetch_pipeline_logs_tool", "fetch-shared", pipeline)

    plan = build_initial_repair_plan(state, now=NOW)

    assert plan.work_items[0].kind == "build"
    assert set(plan.work_items[0].required_checks) == {"diff_check", "build_check", "lint_check"}


def test_latest_plan_ignores_invalid_events_and_plan_required_tracks_pipeline():
    state = _state()
    plan = build_initial_repair_plan(state, now=NOW)
    state["repair_plans"] = [plan.model_dump(mode="json"), {"invalid": True}]

    assert latest_repair_plan(state) == plan
    assert repair_plan_required(state) is False

    state["messages"] = _exchange("fetch_pipeline_logs_tool", "fetch-new", _pipeline(1002))
    assert repair_plan_required(state) is True


def test_active_work_item_advances_from_passing_verification():
    state = _state(second=True)
    plan = build_initial_repair_plan(state, now=NOW)
    state["repair_plans"] = [plan.model_dump(mode="json")]

    assert active_work_item(state).work_item_id == "root-build"

    first_id = plan.work_items[0].work_item_id
    state["repair_verifications"] = [_pass(plan, first_id, (first_id,))]

    assert active_work_item(state).work_item_id == plan.work_items[1].work_item_id


def test_commit_requires_current_diff_and_complete_verifier_coverage():
    state = _state(second=True)
    plan = build_initial_repair_plan(state, now=NOW)
    state["repair_plans"] = [plan.model_dump(mode="json")]
    ids = tuple(item.work_item_id for item in plan.work_items)
    state["repair_verifications"] = [
        _pass(plan, ids[0], (ids[0],)),
        _pass(plan, ids[1], ids),
    ]
    native = NativeCommitDecision(True, validated_diff_digest=DIFF_DIGEST, validated_base_sha=BASE_SHA)

    decision = repair_plan_commit_decision(state, native)

    assert decision.allowed is True
    assert decision.plan_id == plan.plan_id
    assert decision.diff_digest == DIFF_DIGEST

    stale = repair_plan_commit_decision(
        {**state, "repair_verifications": [*_state()["repair_verifications"]]},
        native,
    )
    assert stale.allowed is False


@pytest.mark.parametrize(
    ("work_item_selector", "coverage_selector"),
    (
        ("invented", "first"),
        ("first", "second"),
        ("first", "first_and_invented"),
    ),
)
def test_forged_verifier_coverage_cannot_complete_work_items_or_allow_commit(
    work_item_selector,
    coverage_selector,
):
    state = _state(second=True)
    plan = build_initial_repair_plan(state, now=NOW)
    state["repair_plans"] = [plan.model_dump(mode="json")]
    ids = tuple(item.work_item_id for item in plan.work_items)
    work_item_id = "invented" if work_item_selector == "invented" else ids[0]
    coverage = {
        "first": (ids[0],),
        "second": (ids[1],),
        "first_and_invented": (ids[0], "invented"),
    }[coverage_selector]
    state["repair_verifications"] = [_pass(plan, work_item_id, coverage)]
    native = NativeCommitDecision(True, validated_diff_digest=DIFF_DIGEST, validated_base_sha=BASE_SHA)

    assert active_work_item(state).work_item_id == ids[0]
    assert repair_plan_commit_decision(state, native).allowed is False


def test_semantically_invalid_pass_does_not_complete_a_work_item():
    state = _state(second=True)
    plan = build_initial_repair_plan(state, now=NOW)
    state["repair_plans"] = [plan.model_dump(mode="json")]
    first_id = plan.work_items[0].work_item_id
    invalid = _pass(plan, first_id, (first_id,))
    invalid["causal_alignment"] = False
    state["repair_verifications"] = [invalid]

    assert active_work_item(state).work_item_id == first_id


def test_replan_preserves_strictly_verified_work_items_from_the_same_lineage():
    state = _state(second=True)
    first = build_initial_repair_plan(state, now=NOW)
    ids = tuple(item.work_item_id for item in first.work_items)
    state["repair_plans"] = [first.model_dump(mode="json")]
    state["repair_verifications"] = [_pass(first, ids[0], (ids[0],))]
    revised_items = tuple(
        item.model_copy(update={"hypothesis": "revised parser hypothesis"})
        if item.work_item_id == ids[1]
        else item
        for item in first.work_items
    )
    revised = first.model_copy(update={
        "plan_id": plan_identity_for_revision(first, revised_items, 2),
        "version": 2,
        "revision_reason": "new evidence for the remaining item",
        "work_items": revised_items,
    })
    state["repair_plans"].append(revised.model_dump(mode="json"))

    assert active_work_item(state).work_item_id == ids[1]


def test_audit_summary_is_bounded_and_contains_no_full_evidence():
    state = _state()
    plan = build_initial_repair_plan(state, now=NOW)
    state["repair_plans"] = [plan.model_dump(mode="json")]

    summary = repair_plan_audit(state)

    assert summary == {
        "plan_id": plan.plan_id,
        "lineage_id": plan.lineage_id,
        "version": 1,
        "source_pipeline_id": 1001,
        "work_item_count": 1,
        "completed_work_item_count": 0,
        "blocked_work_item_count": 0,
        "exhausted_work_item_count": 0,
        "active_work_item_id": "root-parser",
        "replan_count": 0,
        "planning_mode": "deterministic_fallback",
        "planner_error_code": "",
    }


def test_native_initial_plan_exhausts_only_attributed_root(monkeypatch):
    import ut_agent.config as config_module
    import ut_agent.pipeline_reconciliation as reconciliation_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")
    monkeypatch.setattr(
        reconciliation_module,
        "native_exhausted_root_ids",
        lambda _state, _limit: frozenset({"root-build"}),
    )
    state = _state(second=True)

    plan = build_initial_repair_plan(state, now=NOW)
    state["repair_plans"] = [plan.model_dump(mode="json")]
    statuses = {item.work_item_id: item.status for item in plan.work_items}

    assert statuses == {"root-build": "exhausted", "root-parser": "pending"}
    assert active_work_item(state).work_item_id == "root-parser"
    assert repair_plan_audit(state)["exhausted_work_item_count"] == 1


def test_native_plan_uses_exact_verified_history_to_exhaust_only_repeated_root(monkeypatch):
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")
    first_sha = "c" * 40
    second_sha = "d" * 40
    first_diff = "sha256:" + "1" * 64
    second_diff = "sha256:" + "2" * 64
    first_pipeline = {
        **_pipeline(1002, second=True),
        "requested_commit_sha": first_sha,
        "matched_commit_sha": first_sha,
    }
    second_pipeline = {
        **_pipeline(1003, second=True),
        "requested_commit_sha": second_sha,
        "matched_commit_sha": second_sha,
    }
    first_plan = build_initial_repair_plan(_state(second=True), now=NOW)
    second_plan_state = {
        **_state(1002, second=True),
        "commit_sha": first_sha,
        "messages": _exchange("fetch_pipeline_logs_tool", "first", first_pipeline),
    }
    second_plan = build_initial_repair_plan(second_plan_state, now=NOW)
    messages = _exchange("fetch_pipeline_logs_tool", "source", _pipeline(second=True))
    messages += _exchange("commit_and_push_tool", "push-1", {
        "status": "success",
        "changed": True,
        "attempt_id": "attempt-1",
        "attempt_sequence": 1,
        "base_sha": BASE_SHA,
        "diff_digest": first_diff,
        "commit_sha": first_sha,
    })
    messages += _exchange("wait_pipeline_tool", "pipeline-1", first_pipeline)
    messages += _exchange("commit_and_push_tool", "push-2", {
        "status": "success",
        "changed": True,
        "attempt_id": "attempt-2",
        "attempt_sequence": 2,
        "base_sha": first_sha,
        "diff_digest": second_diff,
        "commit_sha": second_sha,
    })
    messages += _exchange("wait_pipeline_tool", "pipeline-2", second_pipeline)
    state = {
        **_state(second=True),
        "commit_sha": second_sha,
        "messages": messages,
        "repair_plans": [
            first_plan.model_dump(mode="json"),
            second_plan.model_dump(mode="json"),
        ],
        "repair_verifications": [
            _pass(first_plan, "root-build", ("root-build",), digest=first_diff),
            _pass(second_plan, "root-build", ("root-build",), digest=second_diff),
        ],
    }

    plan = build_initial_repair_plan(state, now=NOW)
    state["repair_plans"].append(plan.model_dump(mode="json"))

    assert {item.work_item_id: item.status for item in plan.work_items} == {
        "root-build": "exhausted",
        "root-parser": "pending",
    }
    assert active_work_item(state).work_item_id == "root-parser"


def test_validation_requirements_include_the_active_work_item_checks(monkeypatch):
    import ut_agent.config as config_module
    from ut_agent.tools.run_repo_validation import required_checks_for_paths

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")
    state = _state()
    plan = build_initial_repair_plan(state, now=NOW)
    state["repair_plans"] = [plan.model_dump(mode="json")]

    assert required_checks_for_paths(state, ["src/parser.py"]) == (
        "diff_check",
        "python_compile_check",
        "test_check",
    )


@pytest.mark.parametrize(("kind", "job_names", "expected"), (
    ("build", ("x86_64_build",), ("diff_check", "build_check")),
    ("format", ("format_check",), ("diff_check", "lint_check")),
    ("lint", ("clang-tidy",), ("diff_check", "lint_check")),
    ("coverage", ("coverage",), ("diff_check", "test_check")),
    ("test", ("verify",), ("diff_check", "test_check")),
))
def test_work_item_failure_kind_selects_a_matching_local_check(kind, job_names, expected):
    assert _required_checks(kind, job_names) == expected
