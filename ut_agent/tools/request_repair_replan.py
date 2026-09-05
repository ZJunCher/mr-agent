"""Evidence-backed request for a new RepairPlan version."""

from __future__ import annotations

import json
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from ut_agent.execution_ledger import build_execution_ledger
from ut_agent.repair_plan import active_work_item, latest_repair_plan, normalize_repair_path


def _error(code: str, message: str) -> dict:
    return {"status": "blocked", "error_code": code, "message": message}


def validate_replan_request(
    state: dict,
    plan_id: str,
    expected_version: int,
    work_item_id: str,
    reason: str,
    hypothesis: str,
    proposed_paths: list[str],
    evidence_sequences: list[int],
) -> dict:
    """Validate optimistic plan identity and prove every new path from tool facts."""
    plan = latest_repair_plan(state)
    if plan is None or plan.plan_id != plan_id:
        return _error("repair_plan_stale", "RepairPlan 已变化，请读取当前计划后重试。")
    if plan.version != expected_version:
        return _error("repair_plan_version_stale", "RepairPlan 版本已变化，请基于最新版本重试。")
    current = active_work_item(state)
    if current is None or current.work_item_id != work_item_id:
        return _error("repair_work_item_stale", "只能为当前 Work Item 请求重规划。")
    compact_reason = " ".join(str(reason or "").split())[:500]
    compact_hypothesis = " ".join(str(hypothesis or "").split())[:1_000]
    if not compact_reason:
        return _error("repair_replan_reason_missing", "重规划必须说明新证据和原因。")
    try:
        requested_sequences = tuple(sorted({int(value) for value in evidence_sequences}))
    except (TypeError, ValueError):
        return _error("repair_replan_evidence_invalid", "证据序号必须是整数。")
    if not requested_sequences or any(value <= plan.evidence_cursor for value in requested_sequences):
        return _error("repair_replan_evidence_stale", "重规划必须引用计划创建后的新工具证据。")

    try:
        normalized_paths = tuple(dict.fromkeys(normalize_repair_path(path) for path in proposed_paths))
    except ValueError as error:
        return _error("repair_replan_path_unsafe", str(error))

    ledger = build_execution_ledger(state.get("messages", []))
    referenced = {attempt.sequence: attempt for attempt in ledger.tool_attempts if attempt.sequence in requested_sequences}
    if len(referenced) != len(requested_sequences):
        return _error("repair_replan_evidence_missing", "引用的工具证据不存在。")

    discovered_paths = set()
    for attempt in referenced.values():
        if str(attempt.args.get("work_item_id") or "") != current.work_item_id:
            return _error(
                "repair_replan_evidence_wrong_work_item",
                "引用的仓库证据不属于当前 Work Item。",
            )
        if attempt.name == "search_repo_tool" and attempt.result and attempt.result.get("status") == "ok":
            for match in attempt.result.get("matches") or ():
                if not isinstance(match, dict) or not match.get("path"):
                    continue
                try:
                    discovered_paths.add(normalize_repair_path(str(match["path"])))
                except ValueError:
                    continue
        elif attempt.name == "read_repo_file_tool" and attempt.result_text.startswith("[FACT]"):
            try:
                discovered_paths.add(normalize_repair_path(str(attempt.args.get("file_path") or "")))
            except ValueError:
                continue
        else:
            return _error(
                "repair_replan_evidence_unsupported",
                "重规划路径只能引用成功的仓库搜索或文件读取证据。",
            )
    if any(path not in discovered_paths for path in normalized_paths):
        return _error("repair_replan_path_unproven", "拟新增路径未出现在引用的仓库证据中。")
    return {
        "status": "success",
        "validated": True,
        "plan_id": plan.plan_id,
        "lineage_id": plan.lineage_id,
        "expected_version": plan.version,
        "work_item_id": current.work_item_id,
        "reason": compact_reason,
        "hypothesis": compact_hypothesis,
        "proposed_paths": list(normalized_paths),
        "evidence_sequences": list(requested_sequences),
    }


@tool
def request_repair_replan_tool(
    plan_id: str,
    expected_version: int,
    work_item_id: str,
    reason: str,
    hypothesis: str = "",
    proposed_paths: list[str] | None = None,
    evidence_sequences: list[int] | None = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """基于新的仓库工具证据，请求生成严格递增的 RepairPlan 版本。"""
    result = validate_replan_request(
        state or {},
        plan_id,
        expected_version,
        work_item_id,
        reason,
        hypothesis,
        proposed_paths or [],
        evidence_sequences or [],
    )
    return json.dumps(result, ensure_ascii=False)
