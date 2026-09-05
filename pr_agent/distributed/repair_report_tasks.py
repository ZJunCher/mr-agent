"""Read-only construction and generation of final-diff repair reports."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable

from pr_agent.distributed.broker import StoredTask
from pr_agent.triage.final_repair_report import (
    FinalRepairDiff,
    FinalRepairReportInput,
    FinalRepairReportOutput,
    FinalRepairReportState,
    RepairReportStatus,
    RepairReportValidationError,
    build_diff_fallback,
    build_report_prompt,
    parse_cached_legacy_report,
    repair_report_setting,
    validate_report_output,
)
from pr_agent.triage.pipeline_repair import repair_source_failure_explanations
from pr_agent.triage.repair_details import RepairFileChange, repair_file_change_from_patch
from ut_agent.structured_output import StructuredOutputOutcome, call_structured_output

_HUNK_RE = re.compile(r"^@@ ")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _change_type(value: dict[str, Any]) -> str:
    if value.get("new_file"):
        return "added"
    if value.get("deleted_file"):
        return "deleted"
    if value.get("renamed_file"):
        return "renamed"
    return "modified"


def _bounded_patch(
    raw_patch: str,
    *,
    max_hunks: int,
    max_lines: int,
    max_chars: int,
    max_patch_chars: int,
) -> tuple[str, int, int, bool, int]:
    output: list[str] = []
    additions = deletions = stored_lines = omitted = hunks = 0
    keep_hunk = False
    truncated = False
    stored_chars = 0
    for raw_line in str(raw_patch or "").splitlines():
        if _HUNK_RE.match(raw_line):
            hunks += 1
            keep_hunk = hunks <= max_hunks
            if keep_hunk and stored_chars < max_patch_chars:
                bounded = raw_line[:min(max_chars, max_patch_chars - stored_chars)]
                output.append(bounded)
                stored_chars += len(bounded) + 1
            else:
                truncated = True
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            additions += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            deletions += 1
        if not keep_hunk or not raw_line or raw_line.startswith("\\ No newline"):
            if raw_line[:1] in {" ", "+", "-"}:
                omitted += 1
            continue
        if raw_line[:1] not in {" ", "+", "-"}:
            continue
        if stored_lines >= max_lines:
            omitted += 1
            truncated = True
            continue
        remaining_chars = max(0, max_patch_chars - stored_chars)
        if remaining_chars <= 1:
            omitted += 1
            truncated = True
            continue
        bounded = raw_line[:min(max_chars, remaining_chars)]
        truncated = truncated or len(bounded) < len(raw_line)
        output.append(bounded)
        stored_lines += 1
        stored_chars += len(bounded) + 1
    return "\n".join(output), additions, deletions, truncated, omitted


def parse_gitlab_compare_diffs(
    raw_diffs: Iterable[dict[str, Any]],
    *,
    max_files: int | None = None,
    max_hunks_per_file: int | None = None,
    max_lines_per_file: int | None = None,
    max_line_chars: int | None = None,
    max_total_chars: int | None = None,
) -> tuple[FinalRepairDiff, ...]:
    max_files = max_files or repair_report_setting("max_files", 40)
    max_hunks_per_file = max_hunks_per_file or repair_report_setting("max_hunks_per_file", 20)
    max_lines_per_file = max_lines_per_file or repair_report_setting("max_lines_per_file", 400)
    max_line_chars = max_line_chars or repair_report_setting("max_line_chars", 500)
    values = list(raw_diffs)
    if len(values) > max_files:
        raise ValueError(f"final diff contains {len(values)} files; limit is {max_files}")
    max_total_chars = max_total_chars or repair_report_setting("max_input_tokens", 24000) * 2
    per_file_chars = max(512, max_total_chars // max(1, len(values)))
    output = []
    for value in values:
        path = str(value.get("new_path") or value.get("old_path") or "")
        patch, additions, deletions, truncated, omitted = _bounded_patch(
            str(value.get("diff") or ""),
            max_hunks=max_hunks_per_file,
            max_lines=max_lines_per_file,
            max_chars=max_line_chars,
            max_patch_chars=per_file_chars,
        )
        output.append(FinalRepairDiff.from_dict({
            "path": path,
            "change_type": _change_type(value),
            "additions": additions,
            "deletions": deletions,
            "patch": patch,
            "truncated": truncated,
            "omitted_lines": omitted,
        }))
    return tuple(sorted(output, key=lambda item: item.path))


def _boundary_fallback(report_task_id: str, reason: str) -> FinalRepairReportState:
    now = _now()
    return FinalRepairReportState(
        RepairReportStatus.FALLBACK,
        report_task_id=report_task_id,
        failure_reason=reason,
        created_at=now,
        updated_at=now,
    )


def build_final_report_input(
    original: StoredTask,
    binding: Any,
    project: Any,
    *,
    report_task_id: str = "",
) -> FinalRepairReportInput | FinalRepairReportState:
    manifest = original.repair_commit_manifest
    if manifest is None or not manifest.entries:
        now = _now()
        return FinalRepairReportState(
            RepairReportStatus.NOT_APPLICABLE,
            report_task_id=report_task_id,
            failure_reason="本次修复未产生代码提交。",
            created_at=now,
            updated_at=now,
        )
    validation = manifest.validate_static()
    repair_state = original.pipeline_repair_state
    if not validation.ok:
        return _boundary_fallback(report_task_id, f"无法确认修复提交边界：{validation.message}")
    original_project = original.mr.project_id if original.mr else ""
    if manifest.repair_task_id != original.task_id or manifest.project_id != original_project:
        return _boundary_fallback(report_task_id, "无法确认修复提交边界：任务身份不一致")
    if manifest.final_repair_sha != repair_state.latest_pipeline_sha:
        return _boundary_fallback(report_task_id, "无法确认修复提交边界：最终流水线 SHA 与修复提交不一致")

    comparison = project.repository_compare(manifest.base_commit_sha, manifest.final_repair_sha)
    raw_diffs = comparison.get("diffs") or []
    commits = comparison.get("commits") or []
    if commits:
        last_sha = str((commits[-1] or {}).get("id") or (commits[-1] or {}).get("sha") or "")
        if last_sha and last_sha != manifest.final_repair_sha:
            return _boundary_fallback(report_task_id, "无法确认修复提交边界：GitLab Compare 终点不一致")
    try:
        diffs = parse_gitlab_compare_diffs(raw_diffs)
    except (TypeError, ValueError) as error:
        return _boundary_fallback(report_task_id, f"最终代码差异无法安全展示：{error}")
    if not diffs:
        return _boundary_fallback(report_task_id, "修复提交存在，但 GitLab Compare 未返回可展示差异")

    source_pipeline_id = int(original.envelope.payload.get("source_pipeline_id") or 0)
    source_sha = str(original.envelope.payload.get("source_pipeline_sha") or "")
    if binding is not None:
        source_pipeline_id = source_pipeline_id or int(getattr(binding, "pipeline_id", 0) or 0)
        source_sha = source_sha or str(getattr(binding, "pipeline_sha", "") or "")
    source_explanations = repair_source_failure_explanations(repair_state)
    source_job_names = tuple(dict.fromkeys(
        record.job_name for record in source_explanations if record.job_name
    ))
    causal_lines = []
    for explanation in source_explanations:
        causal_lines.extend((explanation.confirmed_reason, explanation.possible_reason))
    for action in repair_state.repair_actions:
        causal_lines.extend((action.root_cause, action.evidence))
    causal_lines = tuple(dict.fromkeys(line for line in causal_lines if line))[:60]
    return FinalRepairReportInput(
        repair_task_id=original.task_id,
        project_id=manifest.project_id,
        mr_iid=manifest.mr_iid,
        pr_url=original.envelope.pr_url,
        source_pipeline_id=source_pipeline_id,
        source_sha=source_sha,
        base_sha=manifest.base_commit_sha,
        final_sha=manifest.final_repair_sha,
        final_pipeline_id=repair_state.latest_pipeline_id,
        final_pipeline_status=repair_state.final_pipeline_status,
        final_coverage=repair_state.final_coverage,
        final_coverage_source=repair_state.final_coverage_source,
        final_coverage_status=repair_state.final_coverage_status,
        selected_categories=repair_state.selected_categories,
        failed_jobs=source_job_names or repair_state.failed_job_names,
        causal_lines=causal_lines,
        diffs=diffs,
    )


def final_file_changes(value: FinalRepairReportInput) -> tuple[RepairFileChange, ...]:
    output = []
    for item in value.diffs:
        change = repair_file_change_from_patch(
            item.path,
            item.patch,
            change_type=item.change_type,
            truncated=item.truncated,
            omitted_lines=item.omitted_lines,
        )
        if change is not None:
            output.append(replace(change, additions=item.additions, deletions=item.deletions))
    return tuple(output)


async def generate_final_repair_report(
    value: FinalRepairReportInput,
    *,
    outcome: Any = None,
    llm_call=None,
) -> FinalRepairReportState:
    """Generate one strict report with one correction, then use trusted Diff facts."""
    created_at = _now()
    failure_reason = ""
    attempted_models: list[str] = []
    selected_model = ""

    if outcome is not None:
        attempted_models.extend(
            str(getattr(attempt, "model", "") or "")
            for attempt in getattr(outcome, "attempts", ())
            if getattr(attempt, "model", "")
        )
        selected_model = str(getattr(outcome, "model", "") or "")
        if getattr(outcome, "terminal_error", ""):
            failure_reason = str(getattr(outcome, "terminal_error", ""))[:500]
        elif isinstance(outcome, StructuredOutputOutcome):
            if outcome.value is None:
                failure_reason = outcome.validation_error or "模型总结未返回有效结果"
            else:
                try:
                    report = validate_report_output(outcome.value, value)
                    return FinalRepairReportState(
                        RepairReportStatus.MODEL_GENERATED,
                        input_digest=value.digest(),
                        report=report,
                        model=selected_model,
                        attempted_models=tuple(dict.fromkeys(attempted_models)),
                        created_at=created_at,
                        updated_at=_now(),
                    )
                except RepairReportValidationError as error:
                    failure_reason = str(error)
        else:
            # Compatibility for a completed pre-upgrade idempotency effect.
            try:
                report = parse_cached_legacy_report(str(getattr(outcome, "text", "") or ""), value)
                return FinalRepairReportState(
                    RepairReportStatus.MODEL_GENERATED,
                    input_digest=value.digest(),
                    report=report,
                    model=selected_model,
                    attempted_models=tuple(dict.fromkeys(attempted_models)),
                    created_at=created_at,
                    updated_at=_now(),
                )
            except RepairReportValidationError as error:
                failure_reason = str(error)
    else:
        system, base_user = build_report_prompt(value)
        for attempt_index in range(2):
            user = base_user
            if attempt_index:
                user = (
                    "上一次 submit_final_repair_report 调用未通过校验："
                    f"{failure_reason[:240]}。请严格按工具 Schema 重新提交。\n{base_user}"
                )
            try:
                async with asyncio.timeout(repair_report_setting("model_timeout_seconds", 120)):
                    structured = await call_structured_output(
                        system,
                        user,
                        output_model=FinalRepairReportOutput,
                        tool_name="submit_final_repair_report",
                        tool_description="提交基于真实 CI 证据和最终 Diff 的中文修复报告。",
                        llm_call=llm_call,
                        temperature=0.0,
                        max_tokens=repair_report_setting("max_output_tokens", 1800),
                    )
            except TimeoutError:
                failure_reason = "模型总结超时"
                break
            selected_model = structured.model or selected_model
            attempted_models.extend(
                item.model for item in structured.attempts if item.model
            )
            if structured.terminal_error:
                failure_reason = structured.terminal_error
                break
            if structured.value is None:
                failure_reason = structured.validation_error or "模型总结未返回有效结果"
                continue
            try:
                report = validate_report_output(structured.value, value)
                return FinalRepairReportState(
                    RepairReportStatus.MODEL_GENERATED,
                    input_digest=value.digest(),
                    report=report,
                    model=selected_model,
                    attempted_models=tuple(dict.fromkeys(attempted_models)),
                    created_at=created_at,
                    updated_at=_now(),
                )
            except RepairReportValidationError as error:
                failure_reason = str(error)

    report = build_diff_fallback(value, failure_reason or "模型总结未返回有效结果")
    return FinalRepairReportState(
        RepairReportStatus.FALLBACK,
        input_digest=value.digest(),
        report=report,
        model=selected_model,
        attempted_models=tuple(dict.fromkeys(attempted_models)),
        failure_reason=failure_reason or "模型总结未返回有效结果",
        created_at=created_at,
        updated_at=_now(),
    )
