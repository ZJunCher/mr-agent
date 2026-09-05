"""Deterministic execution evidence for UT Agent completion."""

import json
import re
from typing import Any

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.triage.failure_explanations import sanitize_failure_text
from ut_agent.blocker_evidence import validate_blocker_record
from ut_agent.execution_ledger import ExecutionLedger, ToolAttempt, build_execution_ledger
from ut_agent.repair_coordinator import (
    build_repair_snapshot,
    load_max_repair_commits,
    terminal_guard,
)
from ut_agent.repair_progress import RootCauseProgress, build_root_cause_progress


def build_repair_action_records(messages: list) -> list[dict[str, Any]]:
    """Correlate CI evidence, real changed paths, pushes, and exact validation Pipelines."""
    from pr_agent.triage.failure_categories import categorize_failed_job
    from pr_agent.triage.repair_details import RepairAction, sanitize_repair_text
    from ut_agent.repair_progress import extract_causal_lines

    ledger = build_execution_ledger(messages)
    evidence_by_group: dict[str, dict[str, Any]] = {}
    group_by_job: dict[str, str] = {}
    for pipeline in ledger.pipelines:
        for raw_group in pipeline.get("root_cause_groups") or ():
            if not isinstance(raw_group, dict):
                continue
            group_id = str(raw_group.get("root_cause_id") or "")
            if not group_id:
                continue
            evidence_by_group[group_id] = raw_group
            for job_name in raw_group.get("job_names") or ():
                group_by_job[str(job_name)] = group_id

    actions: dict[str, dict[str, Any]] = {}
    positions = []
    # native backend 下，原生补丁和确定性格式补丁都纳入修复动作扫描
    _native_repair_tools = (
        {"apply_repo_patch_tool", "apply_format_report_tool"} if _is_native_backend() else set()
    )
    _native_group_order = list(evidence_by_group.keys()) if _native_repair_tools else []
    _native_group_index = 0
    for attempt in ledger.tool_attempts:
        # native 路径：apply_repo_patch_tool
        if attempt.name in _native_repair_tools:
            result = attempt.result or {}
            raw_status = str(result.get("status") or "")
            changed_files = [str(path) for path in result.get("changed_files") or () if str(path).strip()]
            # 关联到第一个 root_cause_group（native 路径没有 root_cause_id）
            group_id = _native_group_order[_native_group_index] if _native_group_index < len(_native_group_order) else "native_repair"
            if changed_files and _native_group_index < len(_native_group_order):
                _native_group_index += 1
            group = evidence_by_group.get(group_id) or {}
            job_names = [str(name) for name in group.get("job_names") or () if str(name)]
            categories = []
            for name in job_names:
                category = categorize_failed_job({"name": name}).value
                if category not in categories:
                    categories.append(category)
            root_cause = str(group.get("canonical_diagnostic") or "").strip()
            if raw_status == "changed":
                action_status = "editing"
            elif raw_status == "error":
                action_status = "failed"
            else:
                action_status = "diagnosing"
            record = actions.get(group_id)
            if record is None:
                record = {
                    "action_id": group_id,
                    "root_cause_group_id": group_id,
                    "categories": [],
                    "job_names": [],
                    "root_cause": "",
                    "evidence": "",
                    "confidence": "unknown",
                    "measures": [],
                    "changed_files": [],
                    "solution_summary": "",
                    "rationale": "",
                    "file_changes": [],
                    "commit_sha": "",
                    "validation_pipeline_id": 0,
                    "validation_status": "",
                    "status": "planned",
                    "failure_reason": "",
                    "_changed_sequence": -1,
                    "_last_sequence": -1,
                }
                actions[group_id] = record
                positions.append(group_id)
            record["categories"] = list(dict.fromkeys([*record["categories"], *categories]))
            record["job_names"] = list(dict.fromkeys([*record["job_names"], *job_names]))
            record["root_cause"] = record["root_cause"] or root_cause
            record["evidence"] = record["evidence"] or root_cause
            record["confidence"] = "confirmed" if record["root_cause"] else "unknown"
            record["changed_files"] = list(dict.fromkeys([*record["changed_files"], *changed_files]))
            record["status"] = action_status
            record["_last_sequence"] = attempt.sequence
            if changed_files:
                record["_changed_sequence"] = attempt.sequence
            if action_status in {"failed", "no_changes"}:
                record["failure_reason"] = sanitize_repair_text(result.get("message") or "", 300)
            continue
        if attempt.name != "generate_code_tool":
            continue
        result = attempt.result or {}
        operation = str(attempt.args.get("operation") or result.get("operation") or "")
        if operation not in {"investigate", "repair", "verify_blocker"}:
            continue
        job_name = str(attempt.args.get("job_name") or result.get("job_name") or "unknown")
        group_id = str(
            result.get("root_cause_id")
            or attempt.args.get("root_cause_id")
            or group_by_job.get(job_name)
            or f"job:{job_name}"
        )
        group = evidence_by_group.get(group_id) or {}
        job_names = [str(name) for name in group.get("job_names") or (job_name,) if str(name)]
        categories = []
        for name in job_names:
            category = categorize_failed_job({"name": name}).value
            if category not in categories:
                categories.append(category)
        root_cause = str(group.get("canonical_diagnostic") or "").strip()
        if not root_cause:
            causal_lines = extract_causal_lines(str(result.get("diagnostic") or ""), limit=1)
            root_cause = causal_lines[0] if causal_lines else ""
        changed_files = [str(path) for path in result.get("changed_files") or () if str(path).strip()]
        repair_report = result.get("repair_report") if isinstance(result.get("repair_report"), dict) else {}
        solution_summary = str(repair_report.get("solution_summary") or "")
        rationale = str(repair_report.get("rationale") or "")
        explanations = {
            str(item.get("path") or ""): str(item.get("summary") or "")
            for item in repair_report.get("file_explanations") or ()
            if isinstance(item, dict) and item.get("path") and item.get("summary")
        }
        file_changes = []
        for raw_change in result.get("file_changes") or ():
            if not isinstance(raw_change, dict):
                continue
            path = str(raw_change.get("path") or "")
            file_changes.append({**raw_change, "summary": explanations.get(path, "")})

        raw_status = str(result.get("status") or "")
        if raw_status in {"changed", "partial_changes"}:
            action_status = "editing"
        elif raw_status in {"repair_no_changes", "no_changes", "investigated"}:
            action_status = "no_changes" if operation == "repair" else "diagnosing"
        elif raw_status in {"blocked", "unsafe_changes", "coding_infra_error", "repair_timeout"}:
            action_status = "failed"
        else:
            action_status = "diagnosing"

        record = actions.get(group_id)
        if record is None:
            record = {
                "action_id": group_id,
                "root_cause_group_id": group_id,
                "categories": [],
                "job_names": [],
                "root_cause": "",
                "evidence": "",
                "confidence": "unknown",
                "measures": [],
                "changed_files": [],
                "solution_summary": "",
                "rationale": "",
                "file_changes": [],
                "commit_sha": "",
                "validation_pipeline_id": 0,
                "validation_status": "",
                "status": "planned",
                "failure_reason": "",
                "_changed_sequence": -1,
                "_last_sequence": -1,
            }
            actions[group_id] = record
            positions.append(group_id)
        record["categories"] = list(dict.fromkeys([*record["categories"], *categories]))
        record["job_names"] = list(dict.fromkeys([*record["job_names"], *job_names]))
        record["root_cause"] = record["root_cause"] or root_cause
        record["evidence"] = record["evidence"] or root_cause
        record["confidence"] = "confirmed" if record["root_cause"] else "unknown"
        record["changed_files"] = list(dict.fromkeys([*record["changed_files"], *changed_files]))
        if solution_summary:
            record["solution_summary"] = solution_summary
        if rationale:
            record["rationale"] = rationale
        if file_changes:
            record["file_changes"] = file_changes
        record["status"] = action_status
        record["_last_sequence"] = attempt.sequence
        if changed_files:
            record["_changed_sequence"] = attempt.sequence
        if action_status in {"failed", "no_changes"}:
            record["failure_reason"] = sanitize_repair_text(result.get("message") or "", 300)

    push_attempts = [
        attempt for attempt in ledger.tool_attempts
        if attempt.name == "commit_and_push_tool"
        and (attempt.result or {}).get("status") == "success"
        and (attempt.result or {}).get("changed") is True
        and (attempt.result or {}).get("commit_sha")
    ]
    for group_id in positions:
        record = actions[group_id]
        changed_sequence = int(record.pop("_changed_sequence", -1))
        last_sequence = int(record.pop("_last_sequence", -1))
        push = next((attempt for attempt in push_attempts if attempt.sequence > changed_sequence >= 0), None)
        if push is not None:
            sha = str((push.result or {}).get("commit_sha") or "")
            record["commit_sha"] = sha
            record["status"] = "committed"
            pipeline = next((
                value for value in ledger.pipelines
                if int(value.get("_sequence") or 0) > push.sequence
                and value.get("requested_commit_sha") == sha
                and value.get("matched_commit_sha") == sha
                and value.get("pipeline_status")
            ), None)
            if pipeline is not None:
                record["validation_pipeline_id"] = int(
                    pipeline.get("validation_pipeline_id") or pipeline.get("pipeline_id") or 0
                )
                record["validation_status"] = str(pipeline.get("pipeline_status") or "")
                remaining_categories = {
                    categorize_failed_job(job).value
                    for job in pipeline.get("failed_jobs") or ()
                    if isinstance(job, dict)
                }
                repaired_category_still_fails = bool(set(record["categories"]) & remaining_categories)
                exact_pipeline_succeeded = record["validation_status"] == "success" and not remaining_categories
                other_category_failed = bool(remaining_categories) and not repaired_category_still_fails
                record["status"] = "verified" if exact_pipeline_succeeded or other_category_failed else "failed"
                if record["status"] == "failed" and not record["failure_reason"]:
                    record["failure_reason"] = sanitize_repair_text(pipeline.get("message") or "流水线仍未通过")
        elif last_sequence >= 0 and record["status"] == "editing":
            record["status"] = "failed"
            record["failure_reason"] = "已产生代码修改，但尚未成功提交。"

    return [RepairAction.from_dict(actions[group_id]).to_dict() for group_id in positions]


def _clean_inferred_reason(text: str) -> str:
    """Strip machine-readable blocks so owner-facing analysis stays natural language."""
    from ut_agent.blocker_evidence import BLOCKER_JSON_BEGIN, BLOCKER_JSON_END
    from ut_agent.repair_report import REPAIR_REPORT_END, REPAIR_REPORT_START

    value = str(text or "")
    for begin, end in ((BLOCKER_JSON_BEGIN, BLOCKER_JSON_END), (REPAIR_REPORT_START, REPAIR_REPORT_END)):
        start = value.find(begin)
        while start >= 0:
            stop = value.find(end, start)
            value = value[:start] + (value[stop + len(end):] if stop >= 0 else "")
            start = value.find(begin)
    cleaned = " ".join(value.split()).strip()
    return "" if _looks_like_quoted_source(cleaned) else cleaned


_QUOTED_SOURCE_LINE_MARKER = re.compile(r"\bL\d+:")


def _looks_like_quoted_source(text: str) -> bool:
    """Detect Hermes stdout tails that are just echoed file content (e.g. 'L49: ...'), not prose."""
    return len(_QUOTED_SOURCE_LINE_MARKER.findall(text)) >= 3


def build_failure_explanation_records(messages: list, matched_pipeline: dict | None) -> list[dict]:
    """Build bounded Agent inferences correlated to Jobs in the current terminal Pipeline."""
    if not matched_pipeline:
        return []
    current_jobs = {
        str(job.get("name") or "")
        for job in matched_pipeline.get("failed_jobs") or []
        if isinstance(job, dict)
    }
    preflight_by_job = {
        str(job.get("name") or job.get("job_name") or ""): job["preflight_blocker"]
        for job in matched_pipeline.get("failed_jobs") or []
        if isinstance(job, dict) and isinstance(job.get("preflight_blocker"), dict)
    }
    ledger = build_execution_ledger(messages)
    records = []
    for attempt in reversed(ledger.tool_attempts):
        result = attempt.result or {}
        if attempt.name != "generate_code_tool":
            continue
        job_name = str(attempt.args.get("job_name") or result.get("job_name") or "")
        if job_name not in current_jobs or any(record["job_name"] == job_name for record in records):
            continue
        blocker = result.get("blocker") if isinstance(result.get("blocker"), dict) else {}
        operation = str(attempt.args.get("operation") or result.get("operation") or "")
        accepted_investigation = operation == "investigate" and result.get("status") == "investigated"
        possible_reason = str(blocker.get("root_cause") or "").strip() or _clean_inferred_reason(
            result.get("diagnostic") if accepted_investigation else ""
        )
        suggested_action = str(blocker.get("suggested_action") or "").strip()
        if possible_reason:
            records.append({
                "job_name": sanitize_failure_text(job_name, 120),
                "possible_reason": sanitize_failure_text(possible_reason),
                "suggested_action": sanitize_failure_text(suggested_action, 200),
                "confidence": "inferred",
            })
    records = list(reversed(records))
    covered = {record["job_name"] for record in records}
    for job_name, blocker in preflight_by_job.items():
        if job_name in covered or job_name not in current_jobs:
            continue
        root_cause = str(blocker.get("root_cause") or "").strip()
        if not root_cause:
            continue
        records.append({
            "job_name": sanitize_failure_text(job_name, 120),
            "possible_reason": sanitize_failure_text(root_cause),
            "suggested_action": sanitize_failure_text(str(blocker.get("suggested_action") or "").strip(), 200),
            "confidence": "inferred",
        })
    covered = {record["job_name"] for record in records}
    for attempt in reversed(ledger.tool_attempts):
        if attempt.name != "resolve_dependency_evidence_tool":
            continue
        result = attempt.result or {}
        job_name = str(attempt.args.get("job_name") or result.get("job_name") or "")
        analysis = str(result.get("owner_facing_analysis") or "").strip()
        if job_name in covered or job_name not in current_jobs or not analysis:
            continue
        records.append({
            "job_name": sanitize_failure_text(job_name, 120),
            "possible_reason": sanitize_failure_text(analysis),
            "suggested_action": "",
            "confidence": "inferred",
        })
        covered.add(job_name)
    finish_summary = ""
    for attempt in reversed(ledger.tool_attempts):
        if attempt.name != "finish_tool":
            continue
        if attempt.args.get("success") is not True:
            finish_summary = _clean_inferred_reason(str(attempt.args.get("summary") or ""))
        break
    if finish_summary:
        for job_name in current_jobs:
            if job_name in covered:
                continue
            records.append({
                "job_name": sanitize_failure_text(job_name, 120),
                "possible_reason": sanitize_failure_text(finish_summary),
                "suggested_action": "",
                "confidence": "inferred",
            })
            covered.add(job_name)
    return records


def validate_finish(state: dict, finish_args: dict) -> tuple[bool, str]:
    snapshot = build_repair_snapshot(state.get("messages", []))
    if state.get("trigger_type") == "pipeline_failed":
        terminal_allowed, terminal_reason = terminal_guard(snapshot)
        if not terminal_allowed:
            return False, f"系统拒绝结束任务：{terminal_reason}"
    if finish_args.get("success") is not True:
        if state.get("trigger_type") != "pipeline_failed":
            return True, ""
        return _validate_failed_finish(state, finish_args)

    pushed_sha = snapshot.latest_pushed_sha
    if not pushed_sha:
        return False, "系统拒绝 success=True：没有 Agent 成功推送的 commit SHA。"

    result = snapshot.latest_exact_pipeline
    if result is None:
        return False, f"系统拒绝 success=True：没有找到与最后推送 SHA {pushed_sha} 精确匹配的流水线结果。"

    pipeline_status = result.get("pipeline_status")
    missing_validation = "root_pipeline_id" in result and not result.get("validation_pipeline_id")
    if (
        result.get("status") != "success"
        or pipeline_status != "success"
        or result.get("failed_jobs")
        or missing_validation
    ):
        return False, (
            f"系统拒绝 success=True：最后推送 SHA {pushed_sha} 的流水线状态为 "
            f"{pipeline_status or result.get('status') or 'unknown'}。"
        )

    return True, ""


def _validate_failed_finish(state: dict, finish_args: dict) -> tuple[bool, str]:
    summary = str(finish_args.get("summary", ""))
    if re.search(r"需要查看|待查看|尚未检查|仍需检查|需要检查具体", summary):
        return False, "系统拒绝 success=False：总结仍包含“需要查看”等未完成表述。"

    ledger = build_execution_ledger(state.get("messages", []))
    pipeline = next((
        result for result in reversed(ledger.pipelines)
        if result.get("pipeline_status") == "failed" and "work_items" in result
    ), None)
    if pipeline is None:
        return False, (
            "系统拒绝 success=False：尚未获取包含逐 job work_items 的失败流水线证据；"
            "请先调用 fetch_pipeline_logs_tool 并逐个处理所有失败 job。"
        )

    work_items = pipeline.get("work_items") or []
    if not work_items:
        # 流水线失败但无失败 job（如全部 canceled）：无可指派的动作，允许基于流水线证据结束
        return True, ""

    # 以该流水线首次被观察到的位置为取证基准。同一 commit SHA 的父/子流水线视为同一次
    # 失败观察：重复查询或父子切换都不会清零已完成的取证。
    terminal_identity = (
        pipeline.get("matched_commit_sha")
        or pipeline.get("requested_commit_sha")
        or pipeline.get("pipeline_id")
    )
    first_sequence = min(
        result["_sequence"]
        for result in ledger.pipelines
        if "work_items" in result
        and (
            result.get("matched_commit_sha")
            or result.get("requested_commit_sha")
            or result.get("pipeline_id")
        ) == terminal_identity
    )

    progress = _root_cause_progress(ledger)

    # 一次性收集全部缺口，避免模型逐项试错浪费轮次
    unmet = []
    for item in _canonical_work_items(work_items):
        root_cause_id = str(item.get("root_cause_id") or "")
        root_progress = progress.get(root_cause_id, RootCauseProgress(root_cause_id, "unattempted"))
        if root_cause_id and root_progress.repeat_exhausted:
            continue
        if _has_validated_dependency_blocker(item, ledger.tool_attempts, first_sequence):
            continue
        if _has_verified_failed_repair(state, item, ledger, int(pipeline.get("_sequence") or 0)):
            continue
        allowed, reason = _validate_work_item(item, ledger.tool_attempts, first_sequence)
        if not allowed:
            unmet.append(reason)
    if unmet:
        return False, "系统拒绝 success=False，以下动作缺失：" + " ".join(unmet)
    return True, ""


def _has_validated_dependency_blocker(
    item: dict,
    attempts: list[ToolAttempt],
    after_sequence: int,
) -> bool:
    root_cause_id = str(item.get("root_cause_id") or "")
    job_name = str(item.get("canonical_job_name") or item.get("job_name") or "")
    for attempt in reversed(attempts):
        if attempt.sequence <= after_sequence or attempt.name != "resolve_dependency_evidence_tool":
            continue
        result = attempt.result or {}
        attempt_root = str(result.get("root_cause_id") or attempt.args.get("root_cause_id") or "")
        attempt_job = str(result.get("job_name") or attempt.args.get("job_name") or "")
        if (root_cause_id and attempt_root != root_cause_id) or attempt_job != job_name:
            continue
        blocker = result.get("blocker")
        return (
            result.get("status") == "blocked"
            and isinstance(blocker, dict)
            and blocker.get("blocker_type") == "external_dependency"
            and validate_blocker_record(blocker, job_name) is None
        )
    return False


def _canonical_work_items(work_items: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    order = []
    for index, item in enumerate(work_items):
        root_cause_id = str(item.get("root_cause_id") or "")
        key = f"root:{root_cause_id}" if root_cause_id else f"job:{item.get('job_id')}:{index}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(item)

    canonical = []
    for key in order:
        items = grouped[key]
        canonical_name = str(items[0].get("canonical_job_name") or "")
        canonical.append(next((item for item in items if item.get("job_name") == canonical_name), items[0]))
    return canonical


def _has_verified_failed_repair(
    state: dict,
    item: dict,
    ledger: ExecutionLedger,
    terminal_sequence: int,
) -> bool:
    if _is_native_backend():
        return _has_verified_failed_native_repair(state, item)

    root_cause_id = str(item.get("root_cause_id") or "")
    job_name = str(item.get("canonical_job_name") or item.get("job_name") or "")
    changed_statuses = {"changed", "partial_changes", "unexpected_changes"}

    repair_attempts = [
        attempt for attempt in ledger.tool_attempts
        if attempt.sequence < terminal_sequence
        and attempt.name == "generate_code_tool"
        and (attempt.args.get("operation") or (attempt.result or {}).get("operation")) == "repair"
        and (attempt.result or {}).get("status") in changed_statuses
        and (
            (root_cause_id and str(
                (attempt.result or {}).get("root_cause_id") or attempt.args.get("root_cause_id") or ""
            ) == root_cause_id)
            or (not root_cause_id and attempt.args.get("job_name") == job_name)
        )
    ]
    for repair in reversed(repair_attempts):
        pushes = [
            attempt for attempt in ledger.tool_attempts
            if repair.sequence < attempt.sequence < terminal_sequence
            and attempt.name == "commit_and_push_tool"
            and (attempt.result or {}).get("status") == "success"
            and (attempt.result or {}).get("changed")
            and (attempt.result or {}).get("commit_sha")
        ]
        for push in reversed(pushes):
            sha = str((push.result or {}).get("commit_sha") or "")
            if any(
                push.sequence < result.get("_sequence", 0) <= terminal_sequence
                and result.get("pipeline_status") == "failed"
                and result.get("requested_commit_sha") == sha
                and result.get("matched_commit_sha") == sha
                for result in ledger.pipelines
            ):
                return True
    return False


def _has_verified_failed_native_repair(state: dict, item: dict) -> bool:
    """Require strict root-scoped attribution; another Work Item cannot satisfy this one."""
    from ut_agent.pipeline_reconciliation import native_failed_validation_counts

    root_cause_id = str(item.get("root_cause_id") or "")
    return bool(root_cause_id and native_failed_validation_counts(state).get(root_cause_id, 0) > 0)


def _validate_work_item(item: dict, attempts: list[ToolAttempt], after_sequence: int) -> tuple[bool, str]:
    job_name = str(item.get("job_name", ""))
    job_id = item.get("job_id")
    required_tool = str(item.get("required_tool", ""))

    if required_tool == "apply_format_report_tool":
        attempt = _last_attempt(
            attempts,
            required_tool,
            lambda value: value.sequence > after_sequence and value.args.get("job_id") == job_id,
        )
        if attempt is None:
            return False, (
                f"{job_name} 尚未调用 apply_format_report_tool(job_id={job_id}, "
                f"pipeline_id={item.get('pipeline_id')})；缺少本机 clang-format 不能作为阻塞原因。"
            )
        status = (attempt.result or {}).get("status")
        if status == "changed":
            return False, f"{job_name} 已产生格式修改，必须提交并验证新流水线。"
        if status not in {"no_changes", "blocked"}:
            return False, f"{job_name} 的 {required_tool} 尚未得到确定性结果。"
        return True, ""

    if required_tool == "fetch_coverage_report_tool":
        coverage_attempt = _last_attempt(
            attempts,
            required_tool,
            lambda value: value.sequence > after_sequence and value.args.get("job_id") == job_id,
        )
        if coverage_attempt is None:
            return False, f"{job_name} 尚未调用 fetch_coverage_report_tool(job_id={job_id}) 获取覆盖率证据。"
        coverage_result = coverage_attempt.result or {}
        if coverage_result.get("status") == "unknown" and coverage_result.get("available") is False:
            return True, ""
        if coverage_result.get("status") != "success" or coverage_result.get("available") is not True:
            return False, f"{job_name} 的覆盖率证据不完整。"
        if _is_native_backend():
            return _validate_native_attempt(job_name, attempts, after_sequence)
        return _validate_generate_attempt(job_name, attempts, after_sequence)

    if _is_native_backend():
        return _validate_native_attempt(job_name, attempts, after_sequence)
    return _validate_generate_attempt(job_name, attempts, after_sequence)


def _validate_native_attempt(
    job_name: str,
    attempts: list[ToolAttempt],
    after_sequence: int,
) -> tuple[bool, str]:
    from ut_agent.native_repair_state import build_native_repair_evidence

    current = [attempt for attempt in attempts if attempt.sequence > after_sequence]
    evidence = build_native_repair_evidence(current)
    if evidence.last_patch_sequence < 0:
        return False, f"{job_name} 尚未调用 apply_repo_patch_tool 尝试真实修复。"
    if evidence.failed_patch_after_success or evidence.last_patch_status != "changed":
        return False, f"{job_name} 的 Native patch 尚未成功，请根据工具诊断重新修复。"
    return False, f"{job_name} 已产生代码修改，必须提交并验证新流水线。"


def _validate_generate_attempt(
    job_name: str,
    attempts: list[ToolAttempt],
    after_sequence: int,
) -> tuple[bool, str]:
    phase = "no_repair"
    phase_error = ""
    phase_before_pending = "no_repair"

    for attempt in attempts:
        if attempt.sequence <= after_sequence:
            continue
        result = attempt.result or {}

        if attempt.name == "discard_workspace_tool" and result.get("status") == "success":
            if phase == "pending_changes":
                phase = phase_before_pending
                phase_error = ""
            continue

        if attempt.name != "generate_code_tool" or attempt.args.get("job_name") != job_name:
            continue

        operation = attempt.args.get("operation") or result.get("operation")
        status = result.get("status")

        if status in {"changed", "partial_changes", "unexpected_changes"}:
            phase_before_pending = "no_repair" if operation == "repair" else phase
            phase = "pending_changes"
            continue

        if operation == "repair":
            if status == "repair_no_changes":
                phase = "repair_no_changes"
                phase_error = ""
            else:
                phase = "repair_incomplete"
                phase_error = str(result.get("validation_error") or result.get("message") or status or "unknown")
            continue

        if operation != "verify_blocker" or phase != "repair_no_changes":
            continue

        if status != "blocked":
            phase = "verify_incomplete"
            phase_error = str(result.get("validation_error") or result.get("message") or status or "unknown")
            continue

        blocker_error = validate_blocker_record(result.get("blocker"), job_name)
        if blocker_error:
            phase = "verify_incomplete"
            phase_error = blocker_error
            continue
        phase = "blocked"
        phase_error = ""

    if phase == "blocked":
        return True, ""
    if phase == "pending_changes":
        return False, (
            f"{job_name} 已产生代码修改，必须提交并验证新流水线；"
            "若修改与修复任务无关，先调用 discard_workspace_tool 显式丢弃。"
        )
    if phase == "repair_no_changes":
        return False, (
            f'{job_name} 已完成真实修复尝试但未产生改动；请调用 generate_code_tool(job_name="{job_name}", '
            'operation="verify_blocker", task_description="验证仓库内是否确实不存在安全修复路径")。'
        )
    if phase == "verify_incomplete":
        return False, (
            f"{job_name} 的 blocker 验证未完成：{phase_error}；请重新调用 "
            f'generate_code_tool(job_name="{job_name}", operation="verify_blocker", '
            'task_description="补齐当前 job 的结构化阻塞证据")。'
        )
    if phase == "repair_incomplete":
        return False, (
            f"{job_name} 的 repair 操作未完成：{phase_error}；请重新调用 "
            f'generate_code_tool(job_name="{job_name}", operation="repair", '
            'task_description="根据流水线证据尝试最小安全修复")。'
        )
    return False, (
        f'{job_name} 尚未完成真实修复尝试；请调用 generate_code_tool(job_name="{job_name}", '
        'operation="repair", task_description="根据流水线证据尝试最小安全修复")。'
    )


def _last_attempt(
    attempts: list[ToolAttempt],
    name: str,
    predicate,
) -> ToolAttempt | None:
    return next((
        attempt for attempt in reversed(attempts)
        if attempt.name == name and predicate(attempt)
    ), None)


def build_failed_summary(state: dict, stop_reason: str) -> str:
    ledger = build_execution_ledger(state.get("messages", []))
    pipeline = next((
        result for result in reversed(ledger.pipelines)
        if result.get("pipeline_status") == "failed" and result.get("work_items")
    ), None)
    lines = ["自动修复已达到安全上限，已停止继续提交。"]
    if "相同失败" in stop_reason:
        lines.append("原因：同一失败在两个不同的修复流水线中重复出现。")
    elif "3 个修复 commit" in stop_reason:
        lines.append("原因：本次运行已推送 3 个修复提交。")

    if pipeline is None:
        return "\n".join(lines)
    terminal_identity = (
        pipeline.get("matched_commit_sha")
        or pipeline.get("requested_commit_sha")
        or pipeline.get("pipeline_id")
    )
    first_sequence = min(
        result["_sequence"]
        for result in ledger.pipelines
        if result.get("work_items")
        and (
            result.get("matched_commit_sha")
            or result.get("requested_commit_sha")
            or result.get("pipeline_id")
        ) == terminal_identity
    )
    lines.append(f"最后核验流水线：#{pipeline.get('pipeline_id')}")
    for item in pipeline["work_items"]:
        job_name = str(item.get("job_name", "unknown"))
        job_id = item.get("job_id")
        failed_job = next((
            value for value in pipeline.get("failed_jobs") or []
            if (
                job_id is not None and value.get("job_id") == job_id
            ) or str(value.get("name") or "") == job_name
        ), {})
        root_cause_id = str(item.get("root_cause_id") or "")
        attempt = next((
            value for value in reversed(ledger.tool_attempts)
            if value.sequence > first_sequence
            and value.name not in {"fetch_pipeline_logs_tool", "wait_pipeline_tool"}
            and (
                value.args.get("job_name") == job_name
                or value.args.get("job_id") == job_id
                or (
                    root_cause_id
                    and str(
                        (value.result or {}).get("root_cause_id")
                        or value.args.get("root_cause_id")
                        or ""
                    ) == root_cause_id
                )
            )
        ), None)
        result = attempt.result if attempt and attempt.result else {}
        causal_lines = [str(line).strip() for line in failed_job.get("causal_lines") or [] if str(line).strip()]
        ci_evidence = str(
            (causal_lines[0] if causal_lines else failed_job.get("log_tail"))
            or "未提供 CI 因果错误"
        )
        ci_evidence = next(
            (line.strip() for line in ci_evidence.splitlines() if line.strip()),
            "未提供 CI 因果错误",
        )
        action_evidence = str(
            result.get("diagnostic")
            or result.get("reason")
            or result.get("message")
            or ""
        )
        action_evidence = next((line.strip() for line in action_evidence.splitlines() if line.strip()), "")
        job_status = str(failed_job.get("status") or "failed")
        action_status = str(result.get("status") or "")
        action_note = f"；自动处理结果: {action_status}" if action_status else ""
        evidence = ci_evidence if not action_evidence else f"{ci_evidence}；处理说明: {action_evidence}"
        lines.append(f"- {job_name}: {job_status}{action_note} — {evidence[:300]}")
    return "\n".join(lines)


def validate_tool_call(
    state: dict,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if state.get("trigger_type") == "pipeline_failed":
        allowed_tools = {
            "fetch_pipeline_logs_tool",
            "clone_source_branch_tool",
            "resolve_dependency_evidence_tool",
            "generate_code_tool",
            "discard_workspace_tool",
            "commit_and_push_tool",
            "wait_pipeline_tool",
            "finish_tool",
            "search_repo_tool",
            "read_repo_file_tool",
            "fetch_coverage_report_tool",
            "apply_format_report_tool",
            "request_repair_replan_tool",
            "apply_repo_patch_tool",
            "inspect_repo_diff_tool",
            "run_repo_validation_tool",
        }
        if tool_name not in allowed_tools:
            return False, f"当前流水线修复阶段不允许调用 {tool_name}。"
    # Native backend tools with additional stateful validation below.
    native_tools = {
        "search_repo_tool",
        "read_repo_file_tool",
        "request_repair_replan_tool",
        "apply_format_report_tool",
        "apply_repo_patch_tool",
        "inspect_repo_diff_tool",
        "run_repo_validation_tool",
    }
    if tool_name not in {
        "fetch_pipeline_logs_tool",
        "generate_code_tool",
        "discard_workspace_tool",
        "commit_and_push_tool",
    } | native_tools:
        return True, ""

    tool_args = tool_args or {}
    ledger = build_execution_ledger(state.get("messages", []))
    if state.get("trigger_type") == "pipeline_failed" and _is_native_backend() and tool_name in native_tools:
        from ut_agent.repair_plan import active_work_item, latest_repair_plan, normalize_repair_path

        plan = latest_repair_plan(state)
        current = active_work_item(state)
        if plan is None or current is None:
            return False, "当前失败快照尚无可执行 RepairPlan/Work Item。"
        if str(tool_args.get("work_item_id") or "") != current.work_item_id:
            return False, f"工具调用必须绑定当前 Work Item：{current.work_item_id}。"
        if tool_name == "apply_repo_patch_tool":
            from ut_agent.tools.apply_repo_patch import extract_patch_paths

            paths = extract_patch_paths(str(tool_args.get("patch") or ""))
            if not paths:
                return False, "补丁中未找到可校验的 unified diff 路径。"
            try:
                normalized = tuple(normalize_repair_path(path) for path in paths)
            except ValueError as error:
                return False, f"补丁路径不安全：{error}"
            allowed_paths = set(current.allowed_paths)
            outside = [path for path in normalized if path not in allowed_paths]
            if outside:
                return False, (
                    f"补丁超出当前 Work Item 受控路径：{outside}；"
                    "请先用仓库证据调用 request_repair_replan_tool 扩展计划。"
                )
    if tool_name == "fetch_pipeline_logs_tool":
        from ut_agent.pipeline_actions import repeated_pipeline_fetch_reason

        reason = repeated_pipeline_fetch_reason(state, tool_args)
        return (False, reason) if reason else (True, "")
    if tool_name == "generate_code_tool":
        if state.get("trigger_type") == "pipeline_failed" and _is_native_backend():
            return False, (
                "系统拒绝调用 Hermes：native pipeline repair 必须使用 search_repo_tool、"
                "apply_repo_patch_tool、inspect_repo_diff_tool 和 run_repo_validation_tool。"
            )
        root_cause_id = str(tool_args.get("root_cause_id") or "")
        operation = str(tool_args.get("operation") or "")
        if state.get("trigger_type") == "pipeline_failed" and root_cause_id and operation in {
            "repair",
            "repair_session",
        }:
            progress = _root_cause_progress(ledger)
            root_progress = progress.get(root_cause_id)
            if root_progress is not None and root_progress.state in {"blocked", "repeat_exhausted"}:
                _log_root_progress(progress)
                if root_progress.repeat_exhausted:
                    return False, (
                        f"系统拒绝继续修改根因组 {root_cause_id}：该根因组已在 "
                        f"{root_progress.failed_validations} 个精确 SHA 验证流水线中原样失败。"
                    )
                return False, f"系统拒绝继续修改根因组 {root_cause_id}：该根因组已有有效阻塞证据。"
        if (
            state.get("trigger_type") == "pipeline_failed"
            and tool_args.get("operation") == "repair_session"
            and _has_current_repair_session_attempt(ledger, tool_args)
        ):
            root = tool_args.get("root_cause_id") or tool_args.get("job_name") or "unknown"
            return False, f"根因组 {root} 已对当前流水线证据执行过完整修复会话，禁止无新证据重复运行。"
        if (
            state.get("trigger_type") == "pipeline_failed"
            and tool_args.get("operation") == "investigate"
            and _has_precise_pipeline_evidence(ledger, tool_args)
            and _has_usable_investigation_attempt(ledger, tool_args)
        ):
            root = tool_args.get("root_cause_id") or tool_args.get("job_name") or "unknown"
            return False, (
                f"根因组 {root} 已有精确 CI 因果错误，且已完成一次定向调查尝试；"
                "禁止再次调查搜索，请直接调用 generate_code_tool(operation=\"repair\")，"
                "让 Hermes 根据系统注入的原始错误执行最小安全修复。"
            )
        if (
            state.get("trigger_type") == "pipeline_failed"
            and tool_args.get("operation") == "repair"
            and not _has_investigation_evidence(ledger, tool_args)
        ):
            root = tool_args.get("root_cause_id") or tool_args.get("job_name") or "unknown"
            return False, (
                f"系统拒绝直接修复根因组 {root}：请先对同一 root_cause_id 调用 "
                'generate_code_tool(operation="investigate")，并取得明确诊断证据。'
            )
        from ut_agent.repair_progress import evaluate_hermes_budget

        decision = evaluate_hermes_budget(state, tool_name, tool_args)
        if not decision.allowed:
            return False, decision.reason

    # native backend: commit_and_push_tool 前必须有成功的 patch + diff 检查
    if (
        tool_name == "commit_and_push_tool"
        and state.get("trigger_type") == "pipeline_failed"
        and _is_native_backend()
    ):
        reason = _validate_native_commit_preconditions(state, ledger)
        if reason:
            return False, reason

    pending_change = _latest_unresolved_workspace_change(ledger)
    if tool_name == "discard_workspace_tool":
        result = pending_change.result if pending_change and pending_change.result else {}
        operation = pending_change.args.get("operation") if pending_change else ""
        operation = operation or result.get("operation")
        if (
            pending_change is not None
            and pending_change.name == "generate_code_tool"
            and operation == "repair_session"
            and result.get("status") == "changed"
            and result.get("terminal_protocol_status") == "valid_candidate"
            and isinstance(result.get("repair_report"), dict)
        ):
            return False, "系统拒绝丢弃：当前工作区已有通过有效修复报告校验的修改，请提交并验证流水线。"
        return True, ""

    if tool_name == "commit_and_push_tool" and _has_unresolved_unsafe_changes(ledger):
        return False, (
            "系统拒绝提交：工作区包含无当前接口证据的字段替换；"
            "请先调用 discard_workspace_tool 丢弃该修改，再根据调查证据重新修复。"
        )
    if tool_name == "commit_and_push_tool":
        pending_result = pending_change.result if pending_change and pending_change.result else {}
        pending_status = str(pending_result.get("status") or "")
        if pending_status in {"partial_changes", "unexpected_changes", "unsafe_changes"}:
            validation_code = str(
                pending_result.get("terminal_validation_error_code")
                or pending_result.get("validation_error")
                or pending_status
            )
            return False, (
                f"系统拒绝提交：当前工作区修改未通过完整修复校验（{validation_code}）；"
                "请调用 discard_workspace_tool 丢弃该修改。"
            )
        snapshot = build_repair_snapshot(state.get("messages", []))
        if snapshot.requires_exact_pipeline:
            return False, f"系统拒绝继续提交：修复提交 {snapshot.latest_pushed_sha} 尚未完成精确流水线验证。"
        max_repair_commits = load_max_repair_commits()
        if snapshot.published_attempt_count >= max_repair_commits:
            return False, f"系统拒绝继续提交：本次运行已达到 {max_repair_commits} 个修复 commit 上限。"

    return True, ""


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


def _has_current_repair_session_attempt(ledger: ExecutionLedger, tool_args: dict[str, Any]) -> bool:
    root_cause_id = str(tool_args.get("root_cause_id") or "")
    job_name = str(tool_args.get("job_name") or "")
    pipeline_sequence = -1
    for pipeline in reversed(ledger.pipelines):
        if pipeline.get("pipeline_status") != "failed":
            continue
        groups = [group for group in pipeline.get("root_cause_groups") or () if isinstance(group, dict)]
        matches = any(
            (
                root_cause_id
                and str(group.get("root_cause_id") or "") == root_cause_id
            )
            or (
                not root_cause_id
                and job_name in {str(name) for name in group.get("job_names") or ()}
            )
            for group in groups
        )
        if matches:
            pipeline_sequence = int(pipeline.get("_sequence") or 0)
            break
    if pipeline_sequence < 0:
        return False
    return any(
        attempt.sequence > pipeline_sequence
        and attempt.name == "generate_code_tool"
        and (attempt.args.get("operation") or (attempt.result or {}).get("operation")) == "repair_session"
        and (
            str((attempt.result or {}).get("root_cause_id") or attempt.args.get("root_cause_id") or "")
            == root_cause_id
            if root_cause_id
            else str(attempt.args.get("job_name") or (attempt.result or {}).get("job_name") or "") == job_name
        )
        for attempt in ledger.tool_attempts
    )


def _has_investigation_evidence(ledger: ExecutionLedger, tool_args: dict[str, Any]) -> bool:
    root_cause_id = str(tool_args.get("root_cause_id") or "")
    job_name = str(tool_args.get("job_name") or "")
    for attempt in reversed(ledger.tool_attempts):
        if attempt.name != "generate_code_tool" or not attempt.result:
            continue
        operation = attempt.args.get("operation") or attempt.result.get("operation")
        if operation != "investigate" or attempt.result.get("status") != "investigated":
            continue
        attempt_root = str(attempt.result.get("root_cause_id") or attempt.args.get("root_cause_id") or "")
        same_target = attempt_root == root_cause_id if root_cause_id else attempt.args.get("job_name") == job_name
        diagnostic = "".join(str(attempt.result.get("diagnostic") or "").split())
        if same_target and len(diagnostic) >= 20:
            return True
    return _has_precise_pipeline_evidence(ledger, tool_args) and _has_usable_investigation_attempt(ledger, tool_args)


def _has_precise_pipeline_evidence(ledger: ExecutionLedger, tool_args: dict[str, Any]) -> bool:
    root_cause_id = str(tool_args.get("root_cause_id") or "")
    job_name = str(tool_args.get("job_name") or "")
    for pipeline in reversed(ledger.pipelines):
        if pipeline.get("pipeline_status") != "failed":
            continue
        for group in pipeline.get("root_cause_groups") or []:
            if not isinstance(group, dict):
                continue
            group_root = str(group.get("root_cause_id") or "")
            names = {str(name) for name in group.get("job_names") or []}
            same_target = group_root == root_cause_id if root_cause_id else job_name in names
            diagnostic = "".join(str(group.get("canonical_diagnostic") or "").split())
            if same_target and len(diagnostic) >= 20:
                return True
    return False


def _has_usable_investigation_attempt(ledger: ExecutionLedger, tool_args: dict[str, Any]) -> bool:
    root_cause_id = str(tool_args.get("root_cause_id") or "")
    job_name = str(tool_args.get("job_name") or "")
    for attempt in reversed(ledger.tool_attempts):
        if attempt.name != "generate_code_tool" or not attempt.result:
            continue
        operation = attempt.args.get("operation") or attempt.result.get("operation")
        if operation != "investigate":
            continue
        attempt_root = str(attempt.result.get("root_cause_id") or attempt.args.get("root_cause_id") or "")
        same_target = attempt_root == root_cause_id if root_cause_id else attempt.args.get("job_name") == job_name
        status = str(attempt.result.get("status") or "")
        failure_kind = str(attempt.result.get("failure_kind") or "")
        if same_target and (
            status == "investigated"
            or (
                status == "investigation_timeout"
                and failure_kind in {"search_loop", "execution_budget_exhausted"}
            )
        ):
            return True
    return False


def _has_unresolved_unsafe_changes(ledger: ExecutionLedger) -> bool:
    unsafe = False
    for attempt in ledger.tool_attempts:
        result = attempt.result or {}
        if attempt.name == "generate_code_tool" and result.get("status") == "unsafe_changes":
            unsafe = True
        elif attempt.name == "discard_workspace_tool" and result.get("status") == "success":
            unsafe = False
    return unsafe


def _latest_unresolved_workspace_change(ledger: ExecutionLedger) -> ToolAttempt | None:
    """Return the latest workspace mutation not cleared by discard or a successful push."""
    pending = None
    for attempt in ledger.tool_attempts:
        result = attempt.result or {}
        if attempt.name == "discard_workspace_tool" and result.get("status") == "success":
            pending = None
            continue
        if attempt.name == "commit_and_push_tool" and result.get("status") in {"success", "no_changes"}:
            pending = None
            continue
        if attempt.name == "generate_code_tool" and result.get("status") in {
            "changed",
            "partial_changes",
            "unexpected_changes",
            "unsafe_changes",
        } and result.get("changed_files"):
            pending = attempt
            continue
        if attempt.name == "apply_format_report_tool" and result.get("status") == "changed":
            pending = attempt
    return pending


def is_recoverable_tool_rejection(reason: str) -> bool:
    """Return whether the Agent should correct its next action instead of terminating."""
    return (
        'operation="investigate"' in reason
        or 'operation="repair"' in reason
        or "discard_workspace_tool" in reason
        or "终态证据已保存" in reason
        or "工具调用必须绑定当前 Work Item" in reason
        or "补丁中未找到可校验的 unified diff 路径" in reason
        or "补丁路径不安全" in reason
        or "补丁超出当前 Work Item 受控路径" in reason
        or "request_repair_replan_tool" in reason
        or "native pipeline repair 必须使用" in reason
        or reason.startswith((
            "native_patch_missing:",
            "native_patch_failed_after_success:",
            "native_diff_review_incomplete:",
            "native_diff_review_stale:",
            "native_validation_missing:",
            "native_validation_failed:",
            "native_validation_checks_missing:",
            "native_validation_profile_missing:",
            "native_validation_stale:",
        ))
    )


def _is_native_backend() -> bool:
    """True when ut_agent is configured to use the native repair backend."""
    try:
        from ut_agent.config import REPAIR_BACKEND
        return REPAIR_BACKEND == "native"
    except Exception:
        return False


def _validate_native_commit_preconditions(state: dict, ledger: ExecutionLedger) -> str:
    """Return an actionable Native Repair rejection, or an empty string when ready."""
    from ut_agent.native_repair_state import evaluate_native_commit
    from ut_agent.repair_plan import plan_scoped_attempts, repair_plan_commit_decision

    native = evaluate_native_commit(plan_scoped_attempts(state, ledger))
    decision = repair_plan_commit_decision(state, native)
    return "" if decision.allowed else f"{decision.error_code}: {decision.message}"
