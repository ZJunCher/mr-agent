import asyncio
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401
from ut_agent import repair_verifier
from ut_agent.native_repair_state import evaluate_native_commit
from ut_agent.repair_plan import (
    RepairVerification,
    RepairWorkItem,
    active_work_item,
    blocked_work_item_ids,
    build_initial_repair_plan,
    plan_scoped_attempts,
    repair_plan_commit_decision,
)
from ut_agent.repair_verifier import VerifierOutput, repair_verifier_node

BASE_SHA = "a" * 40
DIFF_DIGEST = "sha256:" + "b" * 64
DIFF = "\n".join((
    "diff --git a/src/parser.py b/src/parser.py",
    "--- a/src/parser.py",
    "+++ b/src/parser.py",
    "@@ -1 +1 @@",
))


def _exchange(name: str, call_id: str, result: dict, args: dict | None = None) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}]),
        ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call_id),
    ]


def _base_state() -> dict:
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
        "active_model": "executor-model",
        "messages": _exchange("fetch_pipeline_logs_tool", "fetch", pipeline),
        "repair_plans": [],
        "repair_verifications": [],
    }
    plan = build_initial_repair_plan(state)
    state["repair_plans"] = [plan.model_dump(mode="json")]
    return state


def _validated_state(*, include_test_check: bool = True) -> dict:
    state = _base_state()
    work_item_id = "root-parser"
    state["messages"] += _exchange("apply_repo_patch_tool", "patch", {
        "status": "changed",
        "patch_applied": True,
        "base_sha": BASE_SHA,
        "diff_digest": DIFF_DIGEST,
        "changed_files": ["src/parser.py"],
        "work_item_id": work_item_id,
    })
    state["messages"] += _exchange("inspect_repo_diff_tool", "inspect", {
        "status": "ok",
        "base_sha": BASE_SHA,
        "diff_digest": DIFF_DIGEST,
        "total_lines": 4,
        "page": {"start_line": 1, "end_line": 4, "has_more": False},
        "diff": DIFF,
        "work_item_id": work_item_id,
    })
    checks = ["diff_check", "test_check"] if include_test_check else ["diff_check"]
    state["messages"] += _exchange("run_repo_validation_tool", "validate", {
        "status": "ok",
        "all_passed": True,
        "base_sha": BASE_SHA,
        "validated_diff_digest": DIFF_DIGEST,
        "required_checks": checks,
        "executed_checks": [{"name": name, "passed": True} for name in checks],
        "work_item_id": work_item_id,
    })
    return state


def _two_item_validated_state() -> dict:
    state = _validated_state()
    plan = state["repair_plans"][0]
    plan["work_items"].append(RepairWorkItem(
        work_item_id="root-coverage",
        job_names=("coverage",),
        kind="coverage",
        required_tool="apply_repo_patch_tool",
        failure_signature="root-coverage",
        failure_evidence=("Coverage 78% is below the required 80%.",),
        hypothesis="The parser branch is missing a unit test.",
        allowed_paths=("tests/test_parser.py",),
        required_checks=("diff_check", "test_check"),
    ).model_dump(mode="json"))
    return state


def test_verifier_does_not_call_model_when_native_gate_fails(monkeypatch):
    called = False

    async def should_not_call(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(repair_verifier, "call_structured_output", should_not_call)
    state = _base_state()
    state["messages"] += _exchange("apply_repo_patch_tool", "patch", {
        "status": "changed",
        "patch_applied": True,
        "base_sha": BASE_SHA,
        "diff_digest": DIFF_DIGEST,
        "changed_files": ["src/parser.py"],
        "work_item_id": "root-parser",
    })

    update = asyncio.run(repair_verifier_node(state))
    event = RepairVerification.model_validate(update["repair_verifications"][0])

    assert event.error_code == "native_diff_review_incomplete"
    assert event.verdict == "block"
    assert called is False


def test_verifier_rejects_same_model_route(monkeypatch):
    state = _validated_state()
    state["active_model"] = "model-a"
    monkeypatch.setattr(repair_verifier, "MODEL_CANDIDATES", ("model-a",))

    update = asyncio.run(repair_verifier_node(state))
    event = RepairVerification.model_validate(update["repair_verifications"][0])

    assert event.error_code == "independent_model_unavailable"
    assert event.verdict == "block"


def test_verifier_persists_strict_pass_for_current_diff(monkeypatch):
    state = _validated_state()
    monkeypatch.setattr(repair_verifier, "MODEL_CANDIDATES", ("executor-model", "verifier-model"))
    payload = {}

    async def passed(_system, user, **_kwargs):
        payload.update(json.loads(user))
        return SimpleNamespace(
            value=VerifierOutput(
                verdict="pass",
                causal_alignment=True,
                scope_compliant=True,
                evidence_sufficient=True,
                covered_work_item_ids=("root-parser",),
                reason="The complete Diff fixes the exact parser failure.",
                risks=(),
            ),
            model="verifier-model",
            terminal_error="",
            validation_error="",
        )

    monkeypatch.setattr(repair_verifier, "call_structured_output", passed)
    update = asyncio.run(repair_verifier_node(state))
    event = RepairVerification.model_validate(update["repair_verifications"][0])

    assert event.verdict == "pass"
    assert event.plan_version == 1
    assert event.diff_digest == DIFF_DIGEST
    assert event.model == "verifier-model"
    assert payload["validation"] == {
        "required_checks": ["diff_check", "test_check"],
        "executed_checks": [{
            "name": "diff_check",
            "passed": True,
            "exit_code": None,
            "timed_out": False,
        }, {
            "name": "test_check",
            "passed": True,
            "exit_code": None,
            "timed_out": False,
        }],
        "all_passed": True,
    }


def test_verifier_payload_includes_the_bounded_full_repair_plan(monkeypatch):
    state = _two_item_validated_state()
    monkeypatch.setattr(repair_verifier, "MODEL_CANDIDATES", ("executor-model", "verifier-model"))
    payload = {}

    async def passed(_system, user, **_kwargs):
        payload.update(json.loads(user))
        return SimpleNamespace(
            value=VerifierOutput(
                verdict="pass",
                causal_alignment=True,
                scope_compliant=True,
                evidence_sufficient=True,
                covered_work_item_ids=("root-parser",),
                reason="The complete Diff fixes the active parser failure.",
                risks=(),
            ),
            model="verifier-model",
            terminal_error="",
            validation_error="",
        )

    monkeypatch.setattr(repair_verifier, "call_structured_output", passed)
    asyncio.run(repair_verifier_node(state))

    assert len(payload["work_items"]) == 2
    assert payload["work_items"][0]["work_item_id"] == "root-parser"
    assert payload["work_items"][0]["failure_evidence"] == ["src/parser.py:1: error"]
    assert payload["work_items"][0]["allowed_paths"] == ["src/parser.py"]
    assert payload["work_items"][0]["required_checks"] == ["diff_check", "test_check"]
    assert payload["work_items"][1]["work_item_id"] == "root-coverage"
    assert payload["work_items"][1]["failure_evidence"] == ["Coverage 78% is below the required 80%."]
    assert payload["work_items"][1]["allowed_paths"] == ["tests/test_parser.py"]
    assert payload["work_items"][1]["required_checks"] == ["diff_check", "test_check"]


def test_verifier_downgrades_incomplete_pass_to_replan(monkeypatch):
    state = _validated_state()
    monkeypatch.setattr(repair_verifier, "MODEL_CANDIDATES", ("verifier-model",))

    async def incomplete(*_args, **_kwargs):
        return SimpleNamespace(
            value=VerifierOutput(
                verdict="pass",
                causal_alignment=False,
                scope_compliant=True,
                evidence_sufficient=True,
                covered_work_item_ids=("root-parser",),
                reason="Causal link is not established.",
            ),
            model="verifier-model",
            terminal_error="",
            validation_error="",
        )

    monkeypatch.setattr(repair_verifier, "call_structured_output", incomplete)
    event = RepairVerification.model_validate(
        asyncio.run(repair_verifier_node(state))["repair_verifications"][0]
    )

    assert event.verdict == "replan"
    assert event.error_code == "repair_verification_rejected"


def test_verifier_rejects_and_drops_unknown_work_item_coverage(monkeypatch):
    state = _validated_state()
    monkeypatch.setattr(repair_verifier, "MODEL_CANDIDATES", ("verifier-model",))

    async def invented(*_args, **_kwargs):
        return SimpleNamespace(
            value=VerifierOutput(
                verdict="pass",
                causal_alignment=True,
                scope_compliant=True,
                evidence_sufficient=True,
                covered_work_item_ids=("root-parser", "invented-root"),
                reason="The active item passes, but the second identity is not in the plan.",
            ),
            model="verifier-model",
            terminal_error="",
            validation_error="",
        )

    monkeypatch.setattr(repair_verifier, "call_structured_output", invented)
    event = RepairVerification.model_validate(
        asyncio.run(repair_verifier_node(state))["repair_verifications"][0]
    )

    assert event.verdict == "replan"
    assert event.error_code == "repair_verification_unknown_work_items"
    assert event.covered_work_item_ids == ("root-parser",)


def test_verifier_rejects_coverage_without_the_work_item_required_checks(monkeypatch):
    state = _validated_state(include_test_check=False)
    monkeypatch.setattr(repair_verifier, "MODEL_CANDIDATES", ("verifier-model",))

    async def passed_without_tests(*_args, **_kwargs):
        return SimpleNamespace(
            value=VerifierOutput(
                verdict="pass",
                causal_alignment=True,
                scope_compliant=True,
                evidence_sufficient=True,
                covered_work_item_ids=("root-parser",),
                reason="The Diff looks correct, but the declared test check was not executed.",
            ),
            model="verifier-model",
            terminal_error="",
            validation_error="",
        )

    monkeypatch.setattr(repair_verifier, "call_structured_output", passed_without_tests)
    event = RepairVerification.model_validate(
        asyncio.run(repair_verifier_node(state))["repair_verifications"][0]
    )

    assert event.verdict == "replan"
    assert event.error_code == "repair_verification_checks_missing"


def test_block_verdict_is_always_bound_to_the_active_work_item(monkeypatch):
    state = _two_item_validated_state()
    monkeypatch.setattr(repair_verifier, "MODEL_CANDIDATES", ("verifier-model",))

    async def blocked(*_args, **_kwargs):
        return SimpleNamespace(
            value=VerifierOutput(
                verdict="block",
                causal_alignment=False,
                scope_compliant=True,
                evidence_sufficient=True,
                covered_work_item_ids=("root-coverage",),
                reason="The active parser repair cannot be verified safely.",
            ),
            model="verifier-model",
            terminal_error="",
            validation_error="",
        )

    monkeypatch.setattr(repair_verifier, "call_structured_output", blocked)
    event = RepairVerification.model_validate(
        asyncio.run(repair_verifier_node(state))["repair_verifications"][0]
    )
    state["repair_verifications"] = [event.model_dump(mode="json")]

    assert event.work_item_id == "root-parser"
    assert event.covered_work_item_ids == ("root-parser",)
    assert blocked_work_item_ids(state) == frozenset({"root-parser"})


def test_second_verification_reuses_cumulative_diff_and_unlocks_commit(monkeypatch):
    state = _two_item_validated_state()
    monkeypatch.setattr(repair_verifier, "MODEL_CANDIDATES", ("verifier-model",))

    async def verify(_system, user, **_kwargs):
        payload = json.loads(user)
        active_id = payload["active_work_item"]["work_item_id"]
        covered = ("root-parser",) if active_id == "root-parser" else ("root-parser", "root-coverage")
        return SimpleNamespace(
            value=VerifierOutput(
                verdict="pass",
                causal_alignment=True,
                scope_compliant=True,
                evidence_sufficient=True,
                covered_work_item_ids=covered,
                reason="The cumulative Diff and checks cover the requested Work Items.",
            ),
            model="verifier-model",
            terminal_error="",
            validation_error="",
        )

    monkeypatch.setattr(repair_verifier, "call_structured_output", verify)
    first = RepairVerification.model_validate(
        asyncio.run(repair_verifier_node(state))["repair_verifications"][0]
    )
    state["repair_verifications"] = [first.model_dump(mode="json")]
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
    second = RepairVerification.model_validate(
        asyncio.run(repair_verifier_node(state))["repair_verifications"][0]
    )
    state["repair_verifications"].append(second.model_dump(mode="json"))

    ledger = repair_verifier.build_execution_ledger(state["messages"])
    native = evaluate_native_commit(plan_scoped_attempts(state, ledger))
    assert second.work_item_id == "root-coverage"
    assert second.covered_work_item_ids == ("root-parser", "root-coverage")
    assert active_work_item(state) is None
    assert repair_plan_commit_decision(state, native).allowed is True
    assert sum(attempt.name == "apply_repo_patch_tool" for attempt in ledger.tool_attempts) == 1
