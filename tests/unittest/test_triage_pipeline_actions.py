import asyncio
import json

import pytest

import pr_agent.config_loader  # noqa: F401 - Initialize Dynaconf before importing ut_agent.
import ut_agent.agent as agent_module
import ut_agent.config as config_module
import ut_agent.execution_policy as execution_policy
from ut_agent.pipeline_actions import next_mandatory_pipeline_action, repeated_pipeline_fetch_reason
from ut_agent.repair_plan import RepairVerification, build_initial_repair_plan

BASE_SHA = "source-sha"
DIFF_DIGEST = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def hermes_backend(monkeypatch):
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "hermes")


@pytest.fixture
def native_backend(monkeypatch):
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")


def _exchange(name, call_id, result, arguments=None):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments or {})},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False),
        },
    ]


def _pipeline(status="failed", pipeline_id=30960, sha="source-sha"):
    diagnostic = "component.cpp:142:23: error: RemoteControl_Request has no member named 'node_name'"
    return {
        "status": "success",
        "requested_commit_sha": sha,
        "matched_commit_sha": sha,
        "pipeline_id": pipeline_id,
        "pipeline_status": status,
        "failed_jobs": [] if status == "success" else [{
            "job_id": 99429,
            "pipeline_id": pipeline_id,
            "name": "build_release_arm64",
            "status": "failed",
            "causal_lines": [diagnostic],
        }],
        "work_items": [] if status == "success" else [{
            "job_id": 99429,
            "pipeline_id": pipeline_id,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
            "root_cause_id": "root-549",
            "canonical_job_name": "build_release_arm64",
        }],
        "root_cause_groups": [] if status == "success" else [{
            "root_cause_id": "root-549",
            "canonical_job_name": "build_release_arm64",
            "job_names": ["build_release_arm64"],
            "canonical_diagnostic": diagnostic,
        }],
    }


def _state(messages=None, *, workspace=False):
    state = {
        "trigger_type": "pipeline_failed",
        "pipeline_id": 30960,
        "commit_sha": "source-sha",
        "messages": messages or [],
    }
    if workspace:
        state["workspace_snapshot"] = {"status": "ready", "local_sha": "source-sha"}
    return state


def _native_state(messages, *, workspace=True):
    state = {
        **_state(messages, workspace=workspace),
        "project_id": "group/repo",
        "mr_id": 42,
        "repair_plans": [],
        "repair_verifications": [],
    }
    planning_state = {**state, "messages": messages[:2]}
    state["repair_plans"] = [build_initial_repair_plan(planning_state).model_dump(mode="json")]
    return state


def _native_patch(call_id="patch"):
    return _exchange("apply_repo_patch_tool", call_id, {
        "status": "changed",
        "patch_applied": True,
        "base_sha": BASE_SHA,
        "diff_digest": DIFF_DIGEST,
        "changed_files": ["src/example.py"],
        "work_item_id": "root-549",
    }, {"patch": "diff", "reason": "fix", "work_item_id": "root-549"})


def _native_page(start_line=1, end_line=4, total_lines=4, call_id="inspect"):
    return _exchange("inspect_repo_diff_tool", call_id, {
        "status": "ok",
        "base_sha": BASE_SHA,
        "diff_digest": DIFF_DIGEST,
        "total_lines": total_lines,
        "page": {
            "start_line": start_line,
            "end_line": end_line,
            "has_more": end_line < total_lines,
            "next_start_line": end_line + 1 if end_line < total_lines else None,
        },
        "work_item_id": "root-549",
    }, {"start_line": start_line, "work_item_id": "root-549"})


def _native_validation(all_passed=True):
    return _exchange("run_repo_validation_tool", "validation", {
        "status": "ok",
        "all_passed": all_passed,
        "base_sha": BASE_SHA,
        "validated_diff_digest": DIFF_DIGEST,
        "required_checks": ["diff_check", "python_compile_check", "build_check"],
        "executed_checks": [
            {"name": "diff_check", "passed": all_passed},
            {"name": "python_compile_check", "passed": True},
            {"name": "build_check", "passed": True},
        ],
        "work_item_id": "root-549",
    }, {"checks": [], "work_item_id": "root-549"})


def _dependency_blocker_result(root_cause_id="root-549", job_name="build_release_arm64"):
    blocker = {
        "schema_version": 1,
        "outcome": "blocked",
        "job_name": job_name,
        "blocker_type": "external_dependency",
        "root_cause": "当前声明分支缺少已观察接口。",
        "ci_evidence": [{"job_name": job_name, "observation": "fatal error: interface missing"}],
        "repository_evidence": [
            {
                "kind": "declared_dependency",
                "locator": "eabot/lhotse@dev-sha",
                "observation": "dev 分支缺少 DrivableMiniOutput.msg",
            }
        ],
        "attempted_repairs": ["只读核验当前声明分支和候选分支。"],
        "why_no_safe_repo_change": "当前仓库不能安全生成上游接口。",
        "suggested_action": "请维护者确认 TwoEndAreas/phase1/0820 后人工调整依赖。",
    }
    return {
        "status": "blocked",
        "root_cause_id": root_cause_id,
        "job_name": job_name,
        "blocker": blocker,
        "dependency_evidence": {
            "project_path": "eabot/lhotse",
            "declared_branch": "dev",
            "declared_sha": "dev-sha",
            "candidate_kind": "unique_verified_candidate",
        },
    }


def _prism_dependency_blocker_result():
    result = _dependency_blocker_result("root-prism")
    result["blocker"]["root_cause"] = "eabot/lhotse 的 dev 分支缺少本次构建观察到的四个消息接口。"
    result["dependency_evidence"].update({
        "queries": [
            {"filename": "DrivableMiniOutput.msg"},
            {"filename": "GateleverDetectionOutput.msg"},
            {"filename": "OrderInfo.msg"},
            {"filename": "PlanningStatus.msg"},
        ],
        "current_branch": {
            "verification_complete": True,
            "missing_queries": [
                "DrivableMiniOutput.msg",
                "GateleverDetectionOutput.msg",
                "OrderInfo.msg",
                "PlanningStatus.msg",
            ],
        },
        "verified_candidates": [{
            "branch": "TwoEndAreas/phase1/0820",
            "resolved_sha": "two-end-areas-sha",
            "verification_complete": True,
            "matched_queries": [
                "DrivableMiniOutput.msg",
                "GateleverDetectionOutput.msg",
                "OrderInfo.msg",
                "PlanningStatus.msg",
            ],
            "missing_queries": [],
            "file_paths": {
                "DrivableMiniOutput.msg": "eabot_msgs/msg/DrivableMiniOutput.msg",
                "GateleverDetectionOutput.msg": "eabot_msgs/msg/GateleverDetectionOutput.msg",
                "OrderInfo.msg": "eabot_msgs/msg/OrderInfo.msg",
                "PlanningStatus.msg": "eabot_msgs/msg/PlanningStatus.msg",
            },
        }],
    })
    return result


def _two_root_pipeline():
    pipeline = _pipeline()
    pipeline["failed_jobs"].append({
        "job_id": 99430,
        "pipeline_id": pipeline["pipeline_id"],
        "name": "clang_tidy_check",
        "status": "failed",
        "causal_lines": ["clang error"],
    })
    pipeline["work_items"].append({
        "job_id": 99430,
        "pipeline_id": pipeline["pipeline_id"],
        "job_name": "clang_tidy_check",
        "kind": "clang",
        "required_tool": "generate_code_tool",
        "root_cause_id": "root-clang",
        "canonical_job_name": "clang_tidy_check",
    })
    pipeline["root_cause_groups"].append({
        "root_cause_id": "root-clang",
        "canonical_job_name": "clang_tidy_check",
        "job_names": ["clang_tidy_check"],
        "canonical_diagnostic": "clang error",
    })
    return pipeline


def _root_pipeline(pipeline_id, sha, roots):
    failed_jobs = []
    work_items = []
    root_cause_groups = []
    for index, root_cause_id in enumerate(roots, start=1):
        job_name = f"build_{root_cause_id}"
        diagnostic = f"{root_cause_id}.cpp:10: error: {root_cause_id}"
        job_id = pipeline_id * 10 + index
        failed_jobs.append({
            "job_id": job_id,
            "pipeline_id": pipeline_id,
            "name": job_name,
            "status": "failed",
            "causal_lines": [diagnostic],
        })
        work_items.append({
            "job_id": job_id,
            "pipeline_id": pipeline_id,
            "job_name": job_name,
            "kind": "build",
            "required_tool": "generate_code_tool",
            "root_cause_id": root_cause_id,
            "canonical_job_name": job_name,
        })
        root_cause_groups.append({
            "root_cause_id": root_cause_id,
            "canonical_job_name": job_name,
            "job_names": [job_name],
            "canonical_diagnostic": diagnostic,
        })
    return {
        "status": "success",
        "requested_commit_sha": sha,
        "matched_commit_sha": sha,
        "pipeline_id": pipeline_id,
        "pipeline_status": "failed",
        "failed_jobs": failed_jobs,
        "work_items": work_items,
        "root_cause_groups": root_cause_groups,
    }


def _changed_repair(root_cause_id):
    return {
        "status": "changed",
        "operation": "repair_session",
        "root_cause_id": root_cause_id,
        "job_name": f"build_{root_cause_id}",
        "changed_files": [f"src/{root_cause_id}.cpp"],
    }


def _successful_push(commit_sha):
    return {
        "status": "success",
        "changed": True,
        "commit_sha": commit_sha,
        "attempt_id": f"attempt-{commit_sha}",
    }


def _three_published_attempts(last_status=None):
    messages = []
    for index in (1, 2):
        sha = f"fix-{index}"
        messages += _exchange("commit_and_push_tool", f"push-{index}", _successful_push(sha))
        messages += _exchange(
            "wait_pipeline_tool",
            f"wait-{index}",
            _root_pipeline(34700 + index, sha, [f"root-{index}"]),
            {"commit_sha": sha},
        )
    messages += _exchange("commit_and_push_tool", "push-3", _successful_push("fix-3"))
    if last_status is not None:
        pipeline = _root_pipeline(34713, "fix-3", ["root-3"])
        if last_status == "success":
            pipeline.update(pipeline_status="success", failed_jobs=[], work_items=[], root_cause_groups=[])
        messages += _exchange("wait_pipeline_tool", "wait-3", pipeline, {"commit_sha": "fix-3"})
    return messages


def test_third_push_waits_for_its_own_pipeline_before_attempt_limit():
    action = next_mandatory_pipeline_action(_state(_three_published_attempts(), workspace=True))

    assert action.name == "wait_pipeline_tool"
    assert action.arguments == {"commit_sha": "fix-3"}


def test_third_success_finishes_success_before_attempt_limit():
    action = next_mandatory_pipeline_action(
        _state(_three_published_attempts(last_status="success"), workspace=True)
    )

    assert action.name == "finish_tool"
    assert action.arguments["success"] is True


def test_third_failure_exhausts_only_after_exact_validation():
    action = next_mandatory_pipeline_action(
        _state(_three_published_attempts(last_status="failed"), workspace=True)
    )

    assert action.name == "finish_tool"
    assert action.arguments["success"] is False
    assert "3" in action.reason


@pytest.mark.parametrize("status", ["canceled", "skipped"])
def test_non_success_terminal_pipeline_finishes_with_explicit_failure(status):
    messages = _exchange("commit_and_push_tool", "push-1", _successful_push("fix-1"))
    pipeline = _root_pipeline(34701, "fix-1", ["root-1"])
    pipeline.update(pipeline_status=status, failed_jobs=[], work_items=[], root_cause_groups=[])
    messages += _exchange("wait_pipeline_tool", "wait-1", pipeline, {"commit_sha": "fix-1"})

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "finish_tool"
    assert action.arguments["success"] is False
    assert status in action.reason


def test_mandatory_flow_fetches_pipeline_first():
    action = next_mandatory_pipeline_action(_state())

    assert action.name == "fetch_pipeline_logs_tool"
    assert action.arguments == {"pipeline_id": 30960, "commit_sha": "source-sha"}


def test_repeated_unattempted_root_does_not_stop_new_root_repair():
    messages = _exchange(
        "fetch_pipeline_logs_tool",
        "source-pipeline",
        _root_pipeline(31001, "source-sha", ["root-a", "root-b"]),
    )
    messages += _exchange(
        "generate_code_tool",
        "repair-a",
        _changed_repair("root-a"),
        {"operation": "repair_session", "root_cause_id": "root-a", "job_name": "build_root-a"},
    )
    messages += _exchange("commit_and_push_tool", "push-a", _successful_push("fix-a"))
    messages += _exchange(
        "wait_pipeline_tool",
        "validation-a",
        _root_pipeline(31002, "fix-a", ["root-b", "root-c"]),
        {"commit_sha": "fix-a"},
    )
    state = _state(messages, workspace=True)

    action = next_mandatory_pipeline_action(state)
    allowed, reason = execution_policy.validate_tool_call(
        state,
        "generate_code_tool",
        {"operation": "repair_session", "root_cause_id": "root-b", "job_name": "build_root-b"},
    )

    assert action.name == "resolve_dependency_evidence_tool"
    assert action.arguments["root_cause_id"] == "root-b"
    assert allowed is True
    assert reason == ""

    messages += _exchange(
        "resolve_dependency_evidence_tool",
        "dependency-b",
        {"status": "resolved", "root_cause_id": "root-b", "job_name": "build_root-b"},
        action.arguments,
    )
    repair_action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert repair_action.name == "generate_code_tool"
    assert repair_action.arguments["operation"] == "investigate"
    assert repair_action.arguments["root_cause_id"] == "root-b"


def test_twice_failed_root_is_skipped_while_other_root_continues():
    messages = _exchange(
        "fetch_pipeline_logs_tool",
        "source-pipeline",
        _root_pipeline(31101, "source-sha", ["root-a", "root-b"]),
    )
    for index in (1, 2):
        messages += _exchange(
            "generate_code_tool",
            f"repair-a-{index}",
            _changed_repair("root-a"),
            {"operation": "repair_session", "root_cause_id": "root-a", "job_name": "build_root-a"},
        )
        messages += _exchange(
            "commit_and_push_tool",
            f"push-a-{index}",
            _successful_push(f"fix-a-{index}"),
        )
        messages += _exchange(
            "wait_pipeline_tool",
            f"validation-a-{index}",
            _root_pipeline(31101 + index, f"fix-a-{index}", ["root-a", "root-b"]),
            {"commit_sha": f"fix-a-{index}"},
        )
    state = _state(messages, workspace=True)

    action = next_mandatory_pipeline_action(state)
    retry_a, reason_a = execution_policy.validate_tool_call(
        state,
        "generate_code_tool",
        {"operation": "repair_session", "root_cause_id": "root-a", "job_name": "build_root-a"},
    )
    repair_b, reason_b = execution_policy.validate_tool_call(
        state,
        "generate_code_tool",
        {"operation": "repair_session", "root_cause_id": "root-b", "job_name": "build_root-b"},
    )

    assert action.name == "resolve_dependency_evidence_tool"
    assert action.arguments["root_cause_id"] == "root-b"
    assert retry_a is False
    assert "root-a" in reason_a and "2" in reason_a
    assert repair_b is True
    assert reason_b == ""


def test_only_twice_failed_root_finishes_with_root_scoped_summary():
    messages = _exchange(
        "fetch_pipeline_logs_tool",
        "source-pipeline",
        _root_pipeline(31201, "source-sha", ["root-a"]),
    )
    for index in (1, 2):
        messages += _exchange(
            "generate_code_tool",
            f"repair-a-{index}",
            _changed_repair("root-a"),
            {"operation": "repair_session", "root_cause_id": "root-a", "job_name": "build_root-a"},
        )
        messages += _exchange(
            "commit_and_push_tool",
            f"push-a-{index}",
            _successful_push(f"fix-a-{index}"),
        )
        messages += _exchange(
            "wait_pipeline_tool",
            f"validation-a-{index}",
            _root_pipeline(31201 + index, f"fix-a-{index}", ["root-a"]),
            {"commit_sha": f"fix-a-{index}"},
        )
    state = _state(messages, workspace=True)

    action = next_mandatory_pipeline_action(state)

    assert action.name == "finish_tool"
    assert action.arguments["success"] is False
    assert "root-a" in action.arguments["summary"]
    assert "该根因组连续修复后仍原样失败" in action.arguments["summary"]
    assert execution_policy.validate_finish(state, action.arguments) == (True, "")


@pytest.mark.usefixtures("native_backend")
def test_native_repeated_unattributed_failure_does_not_block_current_work_item():
    messages = _exchange(
        "fetch_pipeline_logs_tool",
        "source-pipeline",
        _root_pipeline(31301, "source-sha", ["root-a"]),
    )
    messages += _exchange(
        "wait_pipeline_tool",
        "later-pipeline",
        _root_pipeline(31302, "later-sha", ["root-a"]),
        {"commit_sha": "later-sha"},
    )
    state = {
        **_state(messages, workspace=True),
        "project_id": "group/repo",
        "mr_id": 42,
        "commit_sha": "later-sha",
        "repair_plans": [],
        "repair_verifications": [],
    }
    plan = build_initial_repair_plan(state)
    state["repair_plans"] = [plan.model_dump(mode="json")]

    allowed, reason = execution_policy.validate_tool_call(
        state,
        "search_repo_tool",
        {"query": "root-a", "work_item_id": "root-a"},
    )

    assert allowed is True
    assert reason == ""


@pytest.mark.usefixtures("native_backend")
def test_native_failed_finish_cannot_use_root_a_repair_as_evidence_for_new_root_b():
    source = _root_pipeline(31310, "source-sha", ["root-a"])
    planning_state = {
        **_state(_exchange("fetch_pipeline_logs_tool", "source-plan", source), workspace=True),
        "project_id": "group/repo",
        "mr_id": 42,
        "repair_plans": [],
        "repair_verifications": [],
    }
    plan = build_initial_repair_plan(planning_state)
    diff_digest = "sha256:" + "d" * 64
    verification = RepairVerification(
        plan_id=plan.plan_id,
        lineage_id=plan.lineage_id,
        plan_version=plan.version,
        work_item_id="root-a",
        baseline_sha="source-sha",
        diff_digest=diff_digest,
        verdict="pass",
        causal_alignment=True,
        scope_compliant=True,
        evidence_sufficient=True,
        covered_work_item_ids=("root-a",),
        reason="The verified Diff covered root-a only.",
        created_at="2026-09-02T00:00:00+00:00",
    )
    messages = _exchange("fetch_pipeline_logs_tool", "source", source)
    messages += _exchange("commit_and_push_tool", "push-a", {
        "status": "success",
        "changed": True,
        "attempt_id": "attempt-a",
        "attempt_sequence": 1,
        "base_sha": "source-sha",
        "diff_digest": diff_digest,
        "commit_sha": "fix-a",
    })
    messages += _exchange(
        "wait_pipeline_tool",
        "failed-a-and-b",
        _root_pipeline(31311, "fix-a", ["root-a", "root-b"]),
        {"commit_sha": "fix-a"},
    )
    state = {
        **planning_state,
        "commit_sha": "fix-a",
        "messages": messages,
        "repair_plans": [plan.model_dump(mode="json")],
        "repair_verifications": [verification.model_dump(mode="json")],
    }

    allowed, reason = execution_policy.validate_finish(
        state,
        {"success": False, "summary": "root-a 修复后流水线仍然失败。"},
    )

    assert allowed is False
    assert "build_root-b" in reason


@pytest.mark.usefixtures("native_backend")
def test_native_all_exhausted_plan_finishes_without_requesting_another_patch():
    messages = _exchange(
        "fetch_pipeline_logs_tool",
        "source-pipeline",
        _root_pipeline(31401, "source-sha", ["root-a"]),
    )
    state = {
        **_state(messages, workspace=True),
        "project_id": "group/repo",
        "mr_id": 42,
        "repair_plans": [],
        "repair_verifications": [],
    }
    plan = build_initial_repair_plan(state)
    exhausted = plan.model_copy(update={
        "work_items": tuple(item.model_copy(update={"status": "exhausted"}) for item in plan.work_items),
    })
    state["repair_plans"] = [exhausted.model_dump(mode="json")]

    action = next_mandatory_pipeline_action(state)

    assert action.name == "finish_tool"
    assert action.arguments["success"] is False
    assert "root-a" in action.reason
    assert "重复修复上限" in action.reason


def test_mandatory_flow_requires_workspace_after_pipeline():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())

    assert next_mandatory_pipeline_action(_state(messages)).name == "clone_source_branch_tool"


def test_mandatory_flow_finishes_exact_external_preflight_without_starting_hermes():
    pipeline = _pipeline()
    pipeline["work_items"][0]["preflight_blocker"] = {
        "outcome": "blocked",
        "blocker_type": "external_dependency",
        "root_cause": "CI 配置引用的依赖分支不存在：joint/e2e/da_mini/830。",
        "suggested_action": "恢复该依赖分支，或指定确认过的替代分支。",
    }
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", pipeline)

    action = next_mandatory_pipeline_action(_state(messages))

    assert action.name == "finish_tool"
    assert action.arguments["success"] is False
    assert "joint/e2e/da_mini/830" in action.arguments["summary"]


def test_mandatory_flow_finishes_selected_scope_when_only_other_categories_still_fail():
    pipeline = _pipeline()
    pipeline["failed_jobs"] = []
    pipeline["work_items"] = []
    pipeline["root_cause_groups"] = []
    messages = _exchange("wait_pipeline_tool", "pipeline", pipeline, {"commit_sha": "source-sha"})

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "finish_tool"
    assert action.arguments["success"] is True
    assert "所选修复范围" in action.arguments["summary"]


def test_pipeline_repair_policy_rejects_unrelated_tools_by_default():
    state = _state(_exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline()), workspace=True)

    allowed, reason = execution_policy.validate_tool_call(
        state,
        "analyze_diff_tool",
        {"reason": "检查完整 MR"},
    )

    assert allowed is False
    assert "当前流水线修复阶段" in reason


def test_mandatory_flow_resolves_current_contract_before_hermes():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "resolve_dependency_evidence_tool"
    assert action.arguments == {"job_name": "build_release_arm64", "root_cause_id": "root-549"}


def test_mandatory_flow_investigates_after_contract_resolution():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
    messages += _exchange("resolve_dependency_evidence_tool", "dependency", {
        "status": "resolved",
        "root_cause_id": "root-549",
        "job_name": "build_release_arm64",
        "content": "uint32 command\nstring trace_id\nstring optional\n",
    }, {"job_name": "build_release_arm64", "root_cause_id": "root-549"})

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "generate_code_tool"
    assert action.arguments["operation"] == "investigate"
    assert action.arguments["root_cause_id"] == "root-549"


def test_dependency_blocker_finishes_without_hermes():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
    messages += _exchange(
        "resolve_dependency_evidence_tool",
        "dependency",
        _dependency_blocker_result(),
        {"job_name": "build_release_arm64", "root_cause_id": "root-549"},
    )

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "finish_tool"
    assert action.arguments["success"] is False
    assert "外部依赖" in action.arguments["summary"]
    assert all(
        call.get("function", {}).get("name") != "generate_code_tool"
        for message in messages
        for call in message.get("tool_calls", [])
    )
    assert execution_policy.validate_finish(_state(messages, workspace=True), action.arguments) == (True, "")


def test_prism_dependency_blocker_never_enters_write_or_pipeline_wait_actions():
    pipeline = _pipeline()
    pipeline["root_cause_groups"][0]["root_cause_id"] = "root-prism"
    pipeline["work_items"][0]["root_cause_id"] = "root-prism"
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", pipeline)
    messages += _exchange(
        "resolve_dependency_evidence_tool",
        "dependency",
        _prism_dependency_blocker_result(),
        {"job_name": "build_release_arm64", "root_cause_id": "root-prism"},
    )

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))
    called_tools = {
        call.get("function", {}).get("name")
        for message in messages
        for call in message.get("tool_calls", [])
    }

    assert action.name == "finish_tool"
    assert action.arguments["success"] is False
    assert called_tools == {"fetch_pipeline_logs_tool", "resolve_dependency_evidence_tool"}
    assert called_tools.isdisjoint({"generate_code_tool", "commit_and_push_tool", "wait_pipeline_tool"})
    assert execution_policy.validate_finish(_state(messages, workspace=True), action.arguments) == (True, "")


def test_one_blocked_root_does_not_hide_second_root():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _two_root_pipeline())
    messages += _exchange(
        "resolve_dependency_evidence_tool",
        "dependency-build",
        _dependency_blocker_result(),
        {"job_name": "build_release_arm64", "root_cause_id": "root-549"},
    )

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "resolve_dependency_evidence_tool"
    assert action.arguments == {"job_name": "clang_tidy_check", "root_cause_id": "root-clang"}


def test_all_blocked_roots_finish_once_with_bounded_summary():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _two_root_pipeline())
    messages += _exchange(
        "resolve_dependency_evidence_tool",
        "dependency-build",
        _dependency_blocker_result(),
        {"job_name": "build_release_arm64", "root_cause_id": "root-549"},
    )
    messages += _exchange(
        "resolve_dependency_evidence_tool",
        "dependency-clang",
        _dependency_blocker_result("root-clang", "clang_tidy_check"),
        {"job_name": "clang_tidy_check", "root_cause_id": "root-clang"},
    )

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "finish_tool"
    assert action.arguments["success"] is False
    assert "外部依赖阻塞" in action.arguments["summary"]
    assert len(action.arguments["summary"]) <= 1_000
    assert execution_policy.validate_finish(_state(messages, workspace=True), action.arguments) == (True, "")


def test_invalid_dependency_blocker_does_not_complete_root():
    invalid = _dependency_blocker_result()
    invalid["blocker"]["repository_evidence"] = []
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
    messages += _exchange(
        "resolve_dependency_evidence_tool",
        "dependency",
        invalid,
        {"job_name": "build_release_arm64", "root_cause_id": "root-549"},
    )

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "resolve_dependency_evidence_tool"


def test_mandatory_flow_repairs_after_investigation_timeout():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
    messages += _exchange("resolve_dependency_evidence_tool", "dependency", {
        "status": "resolved",
        "root_cause_id": "root-549",
        "job_name": "build_release_arm64",
        "content": "uint32 command\nstring trace_id\nstring optional\n",
    }, {"job_name": "build_release_arm64", "root_cause_id": "root-549"})
    messages += _exchange("generate_code_tool", "investigate", {
        "status": "investigation_timeout",
        "operation": "investigate",
        "root_cause_id": "root-549",
        "job_name": "build_release_arm64",
        "failure_kind": "search_loop",
    }, {"operation": "investigate", "root_cause_id": "root-549", "job_name": "build_release_arm64"})

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "generate_code_tool"
    assert action.arguments["operation"] == "repair"


def test_mandatory_flow_commits_changed_repair():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
    messages += _exchange("resolve_dependency_evidence_tool", "dependency", {
        "status": "resolved", "root_cause_id": "root-549", "job_name": "build_release_arm64"
    }, {"job_name": "build_release_arm64", "root_cause_id": "root-549"})
    messages += _exchange("generate_code_tool", "investigate", {
        "status": "investigated", "operation": "investigate", "root_cause_id": "root-549",
        "job_name": "build_release_arm64", "diagnostic": "current contract excludes node_name",
    }, {"operation": "investigate", "root_cause_id": "root-549", "job_name": "build_release_arm64"})
    messages += _exchange("generate_code_tool", "repair", {
        "status": "changed", "operation": "repair", "root_cause_id": "root-549",
        "job_name": "build_release_arm64", "changed_files": ["src/component.cpp"],
    }, {"operation": "repair", "root_cause_id": "root-549", "job_name": "build_release_arm64"})

    assert next_mandatory_pipeline_action(_state(messages, workspace=True)).name == "commit_and_push_tool"


def test_mandatory_flow_waits_for_exact_pushed_sha():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
    messages += _exchange("commit_and_push_tool", "push", {
        "status": "success",
        "changed": True,
        "commit_sha": "fixed-sha",
        "attempt_id": "push-attempt-1",
    })

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "wait_pipeline_tool"
    assert action.arguments == {"commit_sha": "fixed-sha"}


def test_mandatory_flow_finishes_after_exact_success():
    messages = _exchange("commit_and_push_tool", "push", {
        "status": "success",
        "changed": True,
        "commit_sha": "fixed-sha",
        "attempt_id": "push-attempt-1",
    })
    success = _pipeline("success", 31000, "fixed-sha")
    success["attempt_id"] = "push-attempt-1"
    messages += _exchange("wait_pipeline_tool", "wait", success, {"commit_sha": "fixed-sha"})

    action = next_mandatory_pipeline_action(_state(messages, workspace=True))

    assert action.name == "finish_tool"
    assert action.arguments["success"] is True


def test_repeated_terminal_pipeline_fetch_is_rejected_but_new_identity_is_allowed():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
    state = _state(messages, workspace=True)

    assert repeated_pipeline_fetch_reason(state, {"pipeline_id": 30960, "commit_sha": "source-sha"})
    assert repeated_pipeline_fetch_reason(state, {"pipeline_id": 30961, "commit_sha": "new-sha"}) == ""


def test_running_pipeline_can_be_fetched_again():
    running = _pipeline()
    running["status"] = "running"
    running["pipeline_status"] = "running"
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", running)

    assert repeated_pipeline_fetch_reason(_state(messages), {"pipeline_id": 30960}) == ""


def test_agent_emits_mandatory_action_without_calling_outer_model(monkeypatch):
    async def unexpected_model_call(**_kwargs):
        raise AssertionError("outer model must not choose mandatory pipeline transitions")

    monkeypatch.setattr(agent_module, "call_agent_llm", unexpected_model_call)

    result = asyncio.run(agent_module.agent_node({**_state(), "iteration": 0, "max_iterations": 30}))

    assert result["messages"][0].tool_calls[0]["name"] == "fetch_pipeline_logs_tool"
    assert result["iteration"] == 1


def test_execution_policy_rejects_duplicate_terminal_fetch_recoverably():
    messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
    state = _state(messages, workspace=True)

    allowed, reason = execution_policy.validate_tool_call(
        state,
        "fetch_pipeline_logs_tool",
        {"pipeline_id": 30960, "commit_sha": "source-sha"},
    )

    assert allowed is False
    assert "终态证据已保存" in reason
    assert execution_policy.is_recoverable_tool_rejection(reason) is True


class TestNativeMandatoryFlow:
    def test_failed_pipeline_without_workspace_requires_clone(self, native_backend):
        messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())

        action = next_mandatory_pipeline_action(_state(messages))

        assert action.name == "clone_source_branch_tool"

    def test_ready_workspace_without_patch_leaves_diagnosis_to_agent(self, native_backend):
        messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())

        assert next_mandatory_pipeline_action(_state(messages, workspace=True)) is None

    def test_successful_patch_requires_first_diff_page(self, native_backend):
        messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
        messages += _native_patch()

        action = next_mandatory_pipeline_action(_native_state(messages))

        assert action.name == "inspect_repo_diff_tool"
        assert action.arguments == {"start_line": 1, "work_item_id": "root-549"}

    def test_partial_diff_requires_first_gap(self, native_backend):
        messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
        messages += _native_patch()
        messages += _native_page(1, 2, 4)

        action = next_mandatory_pipeline_action(_native_state(messages))

        assert action.name == "inspect_repo_diff_tool"
        assert action.arguments == {"start_line": 3, "work_item_id": "root-549"}

    def test_complete_diff_requires_all_path_checks(self, native_backend):
        messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
        messages += _native_patch()
        messages += _native_page()

        action = next_mandatory_pipeline_action(_native_state(messages))

        assert action.name == "run_repo_validation_tool"
        assert action.arguments == {
            "checks": ["diff_check", "python_compile_check", "build_check"],
            "work_item_id": "root-549",
        }

    def test_failed_current_validation_returns_control_to_agent(self, native_backend):
        messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
        messages += _native_patch()
        messages += _native_page()
        messages += _native_validation(False)

        assert next_mandatory_pipeline_action(_native_state(messages)) is None

    def test_passing_current_validation_requires_independent_verifier(self, native_backend):
        messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
        messages += _native_patch()
        messages += _native_page()
        messages += _native_validation()

        assert next_mandatory_pipeline_action(_native_state(messages)) is None

    def test_successful_push_waits_for_exact_sha_without_hermes(self, native_backend):
        messages = _exchange("fetch_pipeline_logs_tool", "pipeline", _pipeline())
        messages += _exchange("commit_and_push_tool", "push", {
            "status": "success",
            "changed": True,
            "commit_sha": "fixed-sha",
            "attempt_id": "push-attempt-1",
        })

        action = next_mandatory_pipeline_action(_state(messages, workspace=True))

        assert action.name == "wait_pipeline_tool"
        assert action.arguments == {"commit_sha": "fixed-sha"}
        assert action.name != "generate_code_tool"

    def test_matching_successful_pipeline_finishes_without_hermes(self, native_backend):
        messages = _exchange("commit_and_push_tool", "push", {
            "status": "success",
            "changed": True,
            "commit_sha": "fixed-sha",
            "attempt_id": "push-attempt-1",
        })
        success = _pipeline("success", 31000, "fixed-sha")
        success["attempt_id"] = "push-attempt-1"
        messages += _exchange("wait_pipeline_tool", "wait", success, {"commit_sha": "fixed-sha"})

        action = next_mandatory_pipeline_action(_state(messages, workspace=True))

        assert action.name == "finish_tool"
        assert action.arguments["success"] is True
        assert action.name != "generate_code_tool"
