"""Native Repair evidence and commit-gate tests."""

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing.
from ut_agent.execution_policy import build_execution_ledger, is_recoverable_tool_rejection, validate_tool_call
from ut_agent.native_repair_state import build_native_repair_evidence, evaluate_native_commit

BASE_SHA = "a" * 40
DIFF_DIGEST = "sha256:" + "b" * 64
OTHER_DIGEST = "sha256:" + "c" * 64


@pytest.fixture(autouse=True)
def native_backend(monkeypatch):
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")


def _exchange(name: str, call_id: str, result: dict, args: dict | None = None) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}]),
        ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call_id),
    ]


def _patch(
    call_id: str = "patch",
    digest: str = DIFF_DIGEST,
    work_item_id: str = "root-a",
) -> list:
    return _exchange(
        "apply_repo_patch_tool",
        call_id,
        {
            "status": "changed",
            "patch_applied": True,
            "base_sha": BASE_SHA,
            "diff_digest": digest,
            "changed_files": ["src/example.py"],
            "work_item_id": work_item_id,
        },
        {"patch": "diff", "reason": "fix"},
    )


def _inspect(
    start_line: int = 1,
    end_line: int = 4,
    total_lines: int = 4,
    digest: str = DIFF_DIGEST,
    call_id: str = "inspect",
    work_item_id: str = "root-a",
) -> list:
    return _exchange(
        "inspect_repo_diff_tool",
        call_id,
        {
            "status": "ok",
            "base_sha": BASE_SHA,
            "diff_digest": digest,
            "total_lines": total_lines,
            "page": {
                "start_line": start_line,
                "end_line": end_line,
                "has_more": end_line < total_lines,
                "next_start_line": end_line + 1 if end_line < total_lines else None,
            },
            "work_item_id": work_item_id,
        },
        {"start_line": start_line},
    )


def _validation(
    *,
    digest: str = DIFF_DIGEST,
    status: str = "ok",
    all_passed: bool = True,
    required: tuple[str, ...] = ("diff_check", "python_compile_check"),
    executed: tuple[tuple[str, bool], ...] | None = None,
    error_code: str = "",
    call_id: str = "validation",
    work_item_id: str = "root-a",
) -> list:
    checks = executed if executed is not None else tuple((name, True) for name in required)
    result = {
        "status": status,
        "all_passed": all_passed,
        "base_sha": BASE_SHA,
        "validated_diff_digest": digest,
        "required_checks": list(required),
        "executed_checks": [
            {"name": name, "check": name, "passed": passed}
            for name, passed in checks
        ],
        "work_item_id": work_item_id,
    }
    if error_code:
        result["error_code"] = error_code
    return _exchange("run_repo_validation_tool", call_id, result, {"checks": []})


def _decision(messages: list):
    ledger = build_execution_ledger(messages)
    return evaluate_native_commit(ledger.tool_attempts)


def _full_sequence() -> list:
    return [*_patch(), *_inspect(), *_validation()]


def test_native_pipeline_rejects_generate_code_tool():
    allowed, reason = validate_tool_call(
        {"trigger_type": "pipeline_failed", "messages": []},
        "generate_code_tool",
        {"job_name": "coverage", "operation": "repair"},
    )

    assert allowed is False
    assert "native" in reason.lower()
    assert "apply_repo_patch_tool" in reason


def test_hermes_pipeline_does_not_hit_native_generate_rejection(monkeypatch):
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "hermes")
    allowed, reason = validate_tool_call(
        {"trigger_type": "pipeline_failed", "messages": []},
        "generate_code_tool",
        {"job_name": "coverage", "operation": "investigate"},
    )

    assert allowed is True
    assert reason == ""


def test_commit_without_patch_is_rejected():
    decision = _decision([])

    assert decision.error_code == "native_patch_missing"


def test_commit_after_patch_and_inspection_requires_validation():
    decision = _decision([*_patch(), *_inspect()])

    assert decision.error_code == "native_validation_missing"


def test_native_hard_checks_alone_do_not_bypass_repair_plan_gate():
    messages = _full_sequence()
    state = {"trigger_type": "pipeline_failed", "messages": messages}

    decision = _decision(messages)
    allowed, reason = validate_tool_call(state, "commit_and_push_tool", {})

    assert decision.allowed is True
    assert decision.validated_diff_digest == DIFF_DIGEST
    assert allowed is False
    assert "repair_plan_missing_or_stale" in reason


def test_failed_patch_after_success_is_rejected():
    messages = [
        *_full_sequence(),
        *_exchange("apply_repo_patch_tool", "failed-patch", {"status": "error", "message": "does not apply"}),
    ]

    assert _decision(messages).error_code == "native_patch_failed_after_success"


def test_page_gap_reports_next_missing_line():
    messages = [
        *_patch(),
        *_inspect(1, 2, 5, call_id="page-1"),
        *_inspect(4, 5, 5, call_id="page-2"),
    ]

    decision = _decision(messages)

    assert decision.error_code == "native_diff_review_incomplete"
    assert decision.next_start_line == 3


def test_overlapping_pages_can_complete_review():
    messages = [
        *_patch(),
        *_inspect(1, 3, 5, call_id="page-1"),
        *_inspect(3, 5, 5, call_id="page-2"),
        *_validation(),
    ]

    evidence = build_native_repair_evidence(build_execution_ledger(messages).tool_attempts)

    assert evidence.covered_intervals == ((1, 5),)
    assert evidence.diff_review_complete is True
    assert _decision(messages).allowed is True


def test_unchanged_cumulative_diff_can_be_revalidated_for_a_sibling_work_item():
    messages = [
        *_patch(work_item_id="root-a"),
        *_inspect(work_item_id="root-a"),
        *_validation(work_item_id="root-b"),
    ]

    evidence = build_native_repair_evidence(build_execution_ledger(messages).tool_attempts)

    assert evidence.last_patch_work_item_id == "root-a"
    assert evidence.validated_diff_digest == DIFF_DIGEST
    assert _decision(messages).allowed is True


def test_inspection_from_another_diff_is_stale():
    decision = _decision([*_patch(), *_inspect(digest=OTHER_DIGEST)])

    assert decision.error_code == "native_diff_review_stale"


def test_failed_validation_is_rejected():
    decision = _decision([
        *_patch(),
        *_inspect(),
        *_validation(all_passed=False, executed=(("diff_check", False), ("python_compile_check", True))),
    ])

    assert decision.error_code == "native_validation_failed"


def test_missing_required_validation_check_is_rejected():
    decision = _decision([
        *_patch(),
        *_inspect(),
        *_validation(executed=(("diff_check", True),)),
    ])

    assert decision.error_code == "native_validation_checks_missing"


def test_missing_validation_profile_is_rejected():
    decision = _decision([
        *_patch(),
        *_inspect(),
        *_validation(status="blocked", all_passed=False, error_code="validation_profile_missing"),
    ])

    assert decision.error_code == "native_validation_profile_missing"


def test_validation_before_final_diff_page_is_stale():
    messages = [
        *_patch(),
        *_inspect(1, 2, 4, call_id="page-1"),
        *_validation(),
        *_inspect(3, 4, 4, call_id="page-2"),
    ]

    assert _decision(messages).error_code == "native_validation_stale"


def test_validation_from_another_diff_is_stale():
    decision = _decision([*_patch(), *_inspect(), *_validation(digest=OTHER_DIGEST)])

    assert decision.error_code == "native_validation_stale"


def test_new_patch_invalidates_older_review_and_validation():
    messages = [*_full_sequence(), *_patch(call_id="new-patch", digest=OTHER_DIGEST)]

    decision = _decision(messages)

    assert decision.error_code == "native_diff_review_incomplete"
    assert decision.next_start_line == 1


def test_successful_discard_clears_native_evidence():
    messages = [*_full_sequence(), *_exchange("discard_workspace_tool", "discard", {"status": "success"})]

    assert _decision(messages).error_code == "native_patch_missing"


@pytest.mark.parametrize(
    "code",
    [
        "native_patch_missing",
        "native_patch_failed_after_success",
        "native_diff_review_incomplete",
        "native_diff_review_stale",
        "native_validation_missing",
        "native_validation_failed",
        "native_validation_checks_missing",
        "native_validation_profile_missing",
        "native_validation_stale",
    ],
)
def test_native_evidence_rejections_are_recoverable(code):
    assert is_recoverable_tool_rejection(f"{code}: retry the required Native step") is True


@pytest.mark.parametrize("reason", [
    "工具调用必须绑定当前 Work Item：root-a。",
    "补丁中未找到可校验的 unified diff 路径。",
    "补丁路径不安全：absolute path",
    "补丁超出当前 Work Item 受控路径：['src/b.py']；请调用 request_repair_replan_tool。",
    "系统拒绝调用 Hermes：native pipeline repair 必须使用原生仓库工具。",
])
def test_native_correctable_policy_rejections_do_not_force_terminal_finish(reason):
    assert is_recoverable_tool_rejection(reason) is True


def test_native_sequence_never_calls_generate_code_tool():
    names = {attempt.name for attempt in build_execution_ledger(_full_sequence()).tool_attempts}

    assert "generate_code_tool" not in names
