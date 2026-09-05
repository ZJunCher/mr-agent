"""Pure mandatory action policy for evidence-driven pipeline repair."""

import json
from dataclasses import dataclass
from typing import Any

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from ut_agent.blocker_evidence import validate_blocker_record
from ut_agent.execution_policy import ExecutionLedger, ToolAttempt, build_execution_ledger, build_failed_summary
from ut_agent.repair_coordinator import build_repair_snapshot, load_max_repair_commits
from ut_agent.repair_progress import RootCauseProgress, build_root_cause_progress

_NONTERMINAL_PIPELINE_STATUSES = {"", "created", "pending", "preparing", "running", "waiting_for_resource"}
_DEPENDENCY_TERMINAL_STATUSES = {
    "resolved",
    "not_applicable",
    "not_found",
    "ambiguous",
    "limit_exceeded",
    "error",
    "blocked",
}
_INVESTIGATION_TERMINAL_STATUSES = {"investigated", "investigation_timeout"}
_CHANGED_REPAIR_STATUSES = {"changed", "partial_changes"}


@dataclass(frozen=True)
class MandatoryToolCall:
    name: str
    arguments: dict[str, Any]
    reason: str


def _pipeline_identity_matches(pipeline: dict, pipeline_id: Any, commit_sha: str) -> bool:
    has_identity = False
    if pipeline_id not in (None, ""):
        has_identity = True
        ids = {
            pipeline.get("pipeline_id"),
            pipeline.get("root_pipeline_id"),
            pipeline.get("validation_pipeline_id"),
        }
        if pipeline_id not in ids:
            return False
    if commit_sha:
        has_identity = True
        shas = {str(pipeline.get("requested_commit_sha") or ""), str(pipeline.get("matched_commit_sha") or "")}
        if commit_sha not in shas:
            return False
    return has_identity


def repeated_pipeline_fetch_reason(state: dict, args: dict) -> str:
    """Reject only an exact repeat of already saved terminal pipeline evidence."""
    ledger = build_execution_ledger(state.get("messages", []))
    pipeline_id = args.get("pipeline_id") or state.get("pipeline_id")
    commit_sha = str(args.get("commit_sha") or state.get("commit_sha") or "")
    for pipeline in reversed(ledger.pipelines):
        if not _pipeline_identity_matches(pipeline, pipeline_id, commit_sha):
            continue
        status = str(pipeline.get("pipeline_status") or pipeline.get("status") or "").lower()
        if status in _NONTERMINAL_PIPELINE_STATUSES:
            return ""
        return (
            f"流水线 #{pipeline.get('pipeline_id') or pipeline_id} 的终态证据已保存，"
            "禁止重复获取相同日志；请按已保存的 root_cause_groups 执行下一步。"
        )
    return ""


def _matching_attempt(
    attempts: list[ToolAttempt],
    name: str,
    root_cause_id: str,
    job_name: str,
    after_sequence: int,
) -> ToolAttempt | None:
    return next((
        attempt
        for attempt in reversed(attempts)
        if attempt.sequence > after_sequence
        and attempt.name == name
        and (
            str((attempt.result or {}).get("root_cause_id") or attempt.args.get("root_cause_id") or "")
            == root_cause_id
            or (
                not root_cause_id
                and str((attempt.result or {}).get("job_name") or attempt.args.get("job_name") or "") == job_name
            )
        )
    ), None)


def _validated_dependency_blocker(
    pipeline: dict,
    ledger: ExecutionLedger,
    root_cause_id: str,
    job_name: str,
) -> dict[str, Any] | None:
    attempt = _matching_attempt(
        ledger.tool_attempts,
        "resolve_dependency_evidence_tool",
        root_cause_id,
        job_name,
        int(pipeline.get("_sequence") or 0),
    )
    result = attempt.result if attempt is not None and isinstance(attempt.result, dict) else {}
    blocker = result.get("blocker")
    if result.get("status") != "blocked" or validate_blocker_record(blocker, job_name) is not None:
        return None
    if not isinstance(blocker, dict) or blocker.get("blocker_type") != "external_dependency":
        return None
    return blocker


def _current_generate_item(
    pipeline: dict,
    ledger: ExecutionLedger,
    terminal_root_ids: set[str] | frozenset[str] = frozenset(),
) -> dict | None:
    work_items = [item for item in pipeline.get("work_items") or [] if isinstance(item, dict)]
    seen = set()
    for item in work_items:
        if item.get("required_tool") != "generate_code_tool":
            continue
        root_cause_id = str(item.get("root_cause_id") or "")
        identity = root_cause_id or f"job:{item.get('job_id')}"
        if identity in seen:
            continue
        seen.add(identity)
        if root_cause_id in terminal_root_ids:
            continue
        job_name = str(item.get("canonical_job_name") or item.get("job_name") or "")
        dependency_blocker = _validated_dependency_blocker(
            pipeline,
            ledger,
            root_cause_id,
            job_name,
        )
        if dependency_blocker is not None:
            continue
        verify = _matching_attempt(
            ledger.tool_attempts,
            "generate_code_tool",
            root_cause_id,
            job_name,
            int(pipeline.get("_sequence") or 0),
        )
        if verify is not None:
            operation = str(verify.args.get("operation") or (verify.result or {}).get("operation") or "")
            if operation == "verify_blocker" and (verify.result or {}).get("status") == "blocked":
                continue
        return {**item, "job_name": job_name, "root_cause_id": root_cause_id}
    return None


def _root_cause_progress(ledger: ExecutionLedger) -> dict[str, RootCauseProgress]:
    try:
        no_progress_limit = int(get_settings().get("TRIAGE.NO_PROGRESS_LIMIT", 2))
    except (TypeError, ValueError):
        no_progress_limit = 2
    return build_root_cause_progress(ledger.pipelines, ledger.tool_attempts, no_progress_limit)


def _log_root_progress(progress: dict[str, RootCauseProgress]) -> None:
    get_logger().info(
        "[pipeline-policy] root progress=%s",
        json.dumps([item.to_dict() for item in progress.values()], ensure_ascii=False, sort_keys=True),
    )


def _all_dependency_blockers(pipeline: dict, ledger: ExecutionLedger) -> list[dict[str, Any]]:
    blockers = []
    seen = set()
    for item in pipeline.get("work_items") or ():
        if not isinstance(item, dict) or item.get("required_tool") != "generate_code_tool":
            continue
        root_cause_id = str(item.get("root_cause_id") or "")
        identity = root_cause_id or f"job:{item.get('job_id')}"
        if identity in seen:
            continue
        seen.add(identity)
        job_name = str(item.get("canonical_job_name") or item.get("job_name") or "")
        blocker = _validated_dependency_blocker(pipeline, ledger, root_cause_id, job_name)
        if blocker is None:
            return []
        blockers.append(blocker)
    return blockers


def _generate_arguments(item: dict, operation: str) -> dict[str, Any]:
    descriptions = {
        "investigate": "根据系统注入的当前流水线根因和当前声明依赖接口，定向确认最小修复方案；禁止读取历史。",
        "repair": "根据系统注入的当前根因和接口快照执行最小安全修复；不得猜测或替换未声明字段。",
        "verify_blocker": "验证当前仓库内是否确实不存在安全修复路径，并输出规定的结构化阻塞证据。",
    }
    return {
        "job_name": item["job_name"],
        "root_cause_id": item["root_cause_id"],
        "operation": operation,
        "task_description": descriptions[operation],
    }


def _is_native_backend() -> bool:
    try:
        from ut_agent.config import REPAIR_BACKEND

        return REPAIR_BACKEND == "native"
    except Exception:
        return False


def _next_native_pipeline_action(
    state: dict,
    ledger: ExecutionLedger,
    pipeline: dict,
) -> MandatoryToolCall | None:
    """Force Native post-edit stages while leaving diagnosis and repair to the outer Agent."""
    snapshot = state.get("workspace_snapshot") or {}
    if not isinstance(snapshot, dict) or snapshot.get("status") != "ready":
        return MandatoryToolCall("clone_source_branch_tool", {}, "必须先确认当前 MR 工作区已准备完成。")

    from ut_agent.native_repair_state import build_native_repair_evidence, evaluate_native_commit
    from ut_agent.repair_plan import (
        active_work_item,
        blocked_work_item_ids,
        latest_repair_plan,
        plan_scoped_attempts,
        repair_plan_commit_decision,
    )
    from ut_agent.tools.run_repo_validation import required_checks_for_paths

    pipeline_sequence = int(pipeline.get("_sequence") or 0)
    plan = latest_repair_plan(state)
    if plan is None:
        return None
    attempts = [
        attempt for attempt in plan_scoped_attempts(state, ledger)
        if attempt.sequence > pipeline_sequence
    ]
    evidence = build_native_repair_evidence(attempts)
    blocked = blocked_work_item_ids(state, plan)
    if blocked:
        if evidence.diff_digest:
            return MandatoryToolCall(
                "discard_workspace_tool",
                {},
                "独立 Verifier 已阻止当前 RepairPlan，必须丢弃尚未提交的工作区修改。",
            )
        reason = f"RepairPlan 包含不可自动修复的 Work Item：{', '.join(sorted(blocked))}。"
        return MandatoryToolCall(
            "finish_tool",
            {"success": False, "summary": reason},
            reason,
        )
    current = active_work_item(state)
    exhausted = sorted(item.work_item_id for item in plan.work_items if item.status == "exhausted")
    if current is None and evidence.last_patch_sequence < 0 and exhausted:
        roots = "、".join(exhausted)
        reason = f"根因组 {roots} 已达到有证据的重复修复上限，当前没有其他可执行 Work Item。"
        return MandatoryToolCall(
            "finish_tool",
            {"success": False, "summary": reason},
            reason,
        )
    if evidence.last_patch_sequence < 0:
        return None
    if evidence.failed_patch_after_success or evidence.last_patch_status != "changed":
        return None

    decision = evaluate_native_commit(attempts)
    if decision.error_code in {"native_diff_review_incomplete", "native_diff_review_stale"}:
        start_line = decision.next_start_line or 1
        return MandatoryToolCall(
            "inspect_repo_diff_tool",
            {"start_line": start_line, "work_item_id": current.work_item_id if current else ""},
            "Native 补丁已经应用，必须完整检查当前 Diff 的所有页面。",
        )
    if decision.error_code in {
        "native_validation_missing",
        "native_validation_checks_missing",
        "native_validation_stale",
    }:
        checks = required_checks_for_paths(state, list(evidence.changed_files))
        return MandatoryToolCall(
            "run_repo_validation_tool",
            {"checks": list(checks), "work_item_id": current.work_item_id if current else ""},
            "当前 Diff 已完整检查，必须运行该变更所需的全部本地验证。",
        )
    if decision.allowed:
        plan_decision = repair_plan_commit_decision(state, decision)
        if plan_decision.allowed:
            return MandatoryToolCall(
                "commit_and_push_tool",
                {},
                "RepairPlan 已全部完成，当前 Diff 通过独立验收和全部硬门禁，可以提交并推送。",
            )
    return None


def next_mandatory_pipeline_action(state: dict) -> MandatoryToolCall | None:
    """Return the next required tool call without executing it."""
    if state.get("trigger_type") != "pipeline_failed":
        return None
    snapshot = build_repair_snapshot(state.get("messages", []))
    ledger = snapshot.ledger
    if snapshot.requires_exact_pipeline:
        return MandatoryToolCall(
            "wait_pipeline_tool",
            {"commit_sha": snapshot.latest_pushed_sha},
            "修复提交已经推送，必须等待与该 SHA 精确匹配的验证流水线。",
        )

    pushed_pipeline = snapshot.latest_exact_pipeline
    if (
        snapshot.terminal_proof is not None
        and snapshot.terminal_proof.status == "success"
        and (pushed_pipeline or {}).get("status") == "success"
        and not (pushed_pipeline or {}).get("failed_jobs")
    ):
        return MandatoryToolCall(
            "finish_tool",
            {"success": True, "summary": "修复提交已推送，且精确匹配的新流水线全部通过。"},
            "最新修复 SHA 已由成功流水线验证。",
        )
    if snapshot.terminal_proof is not None and snapshot.terminal_proof.status in {"canceled", "skipped"}:
        status = snapshot.terminal_proof.status
        reason = f"最新修复 SHA 的验证流水线终态为 {status}，无法证明修复成功。"
        return MandatoryToolCall(
            "finish_tool",
            {"success": False, "summary": reason},
            reason,
        )

    max_repair_commits = load_max_repair_commits()
    if snapshot.published_attempt_count >= max_repair_commits:
        reason = f"系统拒绝继续提交：本次运行已达到 {max_repair_commits} 个修复 commit 上限。"
        return MandatoryToolCall(
            "finish_tool",
            {"success": False, "summary": build_failed_summary(state, reason)},
            reason,
        )

    if snapshot.latest_push_attempt is not None:
        pipeline = pushed_pipeline
    else:
        pipeline = next((value for value in reversed(ledger.pipelines) if value.get("pipeline_status")), None)

    if pipeline is None:
        pipeline_id = state.get("pipeline_id")
        commit_sha = str(state.get("commit_sha") or "")
        if pipeline_id in (None, "") and not commit_sha:
            return None
        arguments = {}
        if pipeline_id not in (None, ""):
            arguments["pipeline_id"] = pipeline_id
        if commit_sha:
            arguments["commit_sha"] = commit_sha
        return MandatoryToolCall("fetch_pipeline_logs_tool", arguments, "必须先保存当前流水线的精确失败证据。")

    pipeline_status = str(pipeline.get("pipeline_status") or "").lower()
    if pipeline_status in _NONTERMINAL_PIPELINE_STATUSES:
        return None
    if pipeline_status != "failed":
        return None

    if _is_native_backend():
        return _next_native_pipeline_action(state, ledger, pipeline)

    progress = _root_cause_progress(ledger)
    exhausted_root_ids = {
        root_cause_id for root_cause_id, item in progress.items() if item.repeat_exhausted
    }
    terminal_root_ids = {
        root_cause_id
        for root_cause_id, item in progress.items()
        if item.state in {"blocked", "repeat_exhausted"}
    }
    if exhausted_root_ids:
        _log_root_progress(progress)

    item = _current_generate_item(pipeline, ledger, terminal_root_ids)
    if item is None:
        if not pipeline.get("failed_jobs"):
            return MandatoryToolCall(
                "finish_tool",
                {"success": True, "summary": "所选修复范围内的失败任务已经清除。"},
                "验证流水线整体仍失败，但所选修复类别已不再包含失败 Job。",
            )
        dependency_blockers = _all_dependency_blockers(pipeline, ledger)
        if dependency_blockers:
            parts = []
            for blocker in dependency_blockers:
                root_cause = str(blocker.get("root_cause") or "").strip()
                suggested_action = str(blocker.get("suggested_action") or "").strip()
                part = root_cause if not suggested_action else f"{root_cause} 建议：{suggested_action}"
                if part and part not in parts:
                    parts.append(part)
            summary = "外部依赖阻塞：" + "；".join(parts)
            return MandatoryToolCall(
                "finish_tool",
                {"success": False, "summary": summary[:1_000]},
                "所有可修复根因都已取得确定性的外部依赖阻塞证据。",
            )
        current_root_ids = {
            str(value.get("root_cause_id") or "")
            for value in pipeline.get("work_items") or ()
            if isinstance(value, dict) and value.get("root_cause_id")
        }
        current_exhausted = sorted(current_root_ids & exhausted_root_ids)
        if current_exhausted and current_root_ids <= terminal_root_ids:
            roots = "、".join(current_exhausted)
            summary = f"根因组 {roots}：该根因组连续修复后仍原样失败，已停止继续修改。"
            return MandatoryToolCall(
                "finish_tool",
                {"success": False, "summary": summary[:1_000]},
                "所有尚未解决的根因组均已取得阻塞证据或达到重复修复上限。",
            )
        return MandatoryToolCall(
            "finish_tool",
            {"success": False, "summary": "所选修复范围仍有失败 Job，但没有可执行的安全修复动作。"},
            "流水线失败证据没有对应到允许的修复工具，必须明确结束而不是开放工具选择。",
        )
    blocker = item.get("preflight_blocker")
    if isinstance(blocker, dict) and str(blocker.get("outcome") or "") == "blocked":
        root_cause = str(blocker.get("root_cause") or "流水线失败发生在仓库代码修复之前。").strip()
        suggested_action = str(blocker.get("suggested_action") or "").strip()
        summary = root_cause if not suggested_action else f"{root_cause} 建议：{suggested_action}"
        return MandatoryToolCall(
            "finish_tool",
            {"success": False, "summary": summary[:1000]},
            "高置信度流水线前置检查已确认当前错误没有安全的代码修复动作。",
        )
    pipeline_sequence = int(pipeline.get("_sequence") or 0)
    snapshot = state.get("workspace_snapshot") or {}
    if not isinstance(snapshot, dict) or snapshot.get("status") != "ready":
        return MandatoryToolCall("clone_source_branch_tool", {}, "必须先确认当前 MR 工作区已准备完成。")

    root_cause_id = item["root_cause_id"]
    job_name = item["job_name"]
    dependency_attempt = _matching_attempt(
        ledger.tool_attempts,
        "resolve_dependency_evidence_tool",
        root_cause_id,
        job_name,
        pipeline_sequence,
    )
    dependency_status = str((dependency_attempt.result or {}).get("status") or "") if dependency_attempt else ""
    dependency_complete = dependency_status in _DEPENDENCY_TERMINAL_STATUSES
    if dependency_status == "blocked":
        dependency_complete = _validated_dependency_blocker(pipeline, ledger, root_cause_id, job_name) is not None
    if not dependency_complete:
        return MandatoryToolCall(
            "resolve_dependency_evidence_tool",
            {"job_name": job_name, "root_cause_id": root_cause_id},
            "Hermes 运行前必须先固定当前声明依赖的接口快照。",
        )

    generate_attempts = [
        attempt
        for attempt in ledger.tool_attempts
        if attempt.sequence > pipeline_sequence
        and attempt.name == "generate_code_tool"
        and (
            str((attempt.result or {}).get("root_cause_id") or attempt.args.get("root_cause_id") or "")
            == root_cause_id
        )
    ]
    investigation = next((
        attempt
        for attempt in reversed(generate_attempts)
        if (attempt.args.get("operation") or (attempt.result or {}).get("operation")) == "investigate"
    ), None)
    if investigation is None:
        return MandatoryToolCall(
            "generate_code_tool",
            _generate_arguments(item, "investigate"),
            "先进行一次有当前 CI 和接口证据约束的定向调查。",
        )

    investigation_status = str((investigation.result or {}).get("status") or "")
    investigation_kind = str((investigation.result or {}).get("failure_kind") or "")
    usable_investigation = investigation_status == "investigated" or (
        investigation_status == "investigation_timeout"
        and investigation_kind in {"search_loop", "execution_budget_exhausted"}
    )
    if not usable_investigation:
        return None

    repair = next((
        attempt
        for attempt in reversed(generate_attempts)
        if attempt.sequence > investigation.sequence
        and (attempt.args.get("operation") or (attempt.result or {}).get("operation")) == "repair"
    ), None)
    if repair is None:
        return MandatoryToolCall(
            "generate_code_tool",
            _generate_arguments(item, "repair"),
            "调查阶段不是终态，必须立即执行一次最小安全修复。",
        )

    repair_status = str((repair.result or {}).get("status") or "")
    if repair_status in _CHANGED_REPAIR_STATUSES:
        later_push = next((
            attempt
            for attempt in ledger.tool_attempts
            if attempt.sequence > repair.sequence and attempt.name == "commit_and_push_tool"
        ), None)
        if later_push is None:
            return MandatoryToolCall("commit_and_push_tool", {}, "工作区已有安全修复，必须提交并推送。")
        return None
    if repair_status == "unsafe_changes":
        return MandatoryToolCall("discard_workspace_tool", {}, "字段替换缺少当前接口证据，必须丢弃不安全修改。")
    if repair_status == "repair_no_changes":
        verification = next((
            attempt
            for attempt in reversed(generate_attempts)
            if attempt.sequence > repair.sequence
            and (attempt.args.get("operation") or (attempt.result or {}).get("operation")) == "verify_blocker"
        ), None)
        if verification is None:
            return MandatoryToolCall(
                "generate_code_tool",
                _generate_arguments(item, "verify_blocker"),
                "真实修复未产生改动，必须验证是否存在仓库外阻塞。",
            )
        if (verification.result or {}).get("status") == "blocked":
            return MandatoryToolCall(
                "finish_tool",
                {"success": False, "summary": "已完成真实修复尝试，并确认当前仓库内不存在安全修复路径。"},
                "当前根因已取得完整阻塞证据。",
            )
    return None
