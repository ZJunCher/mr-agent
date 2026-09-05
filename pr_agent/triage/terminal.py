import asyncio
import hashlib
from dataclasses import replace
from typing import Any

from pr_agent.distributed.models import (
    PipelineEvent,
    PostRepairUTStatus,
    RepairItemStatus,
    TaskStatus,
    TriageCardState,
)
from pr_agent.log import get_logger
from pr_agent.triage.failure_categories import classify_failed_jobs
from pr_agent.triage.failure_explanations import source_job_records
from pr_agent.triage.pipeline_repair import PipelineRepairPhase, repair_source_failure_explanations
from pr_agent.triage.store import has_triage_run_task, save_triage_run, update_triage_run_repair_report

_REPAIR_COMMANDS = {"/repair-pipeline", "/triage", "/fix-format", "/fix_format"}


def _repair_command(command: str) -> str:
    parts = str(command or "").strip().split(maxsplit=1)
    return parts[0].lower() if parts and parts[0].lower() in _REPAIR_COMMANDS else ""


def _is_outer_repair(command: str) -> bool:
    return _repair_command(command) == "/repair-pipeline"


def _terminal_status(stored, binding) -> str:
    repair_state = stored.pipeline_repair_state
    if repair_state.final_pipeline_status:
        return repair_state.final_pipeline_status
    if stored.status is TaskStatus.CANCELED:
        return "canceled"
    if binding is not None and binding.state is TriageCardState.REPAIR_SUCCEEDED:
        return "success"
    if stored.status is TaskStatus.FAILED:
        return "error"
    return "failed"


def _coverage_continuation_details(repair_state) -> dict[str, Any]:
    return {
        "attempts": repair_state.coverage_attempts,
        "baseline_pipeline_id": repair_state.coverage_baseline_pipeline_id,
        "baseline_sha": repair_state.coverage_baseline_sha,
        "enhancement_sha": repair_state.coverage_enhancement_sha,
        "rollback_sha": repair_state.coverage_rollback_sha,
        "before": repair_state.coverage_before,
        "after": repair_state.coverage_after,
        "threshold": repair_state.coverage_threshold,
        "job_id": repair_state.coverage_job_id,
        "result": repair_state.coverage_result,
        "reason": repair_state.coverage_failure_reason or repair_state.coverage_skip_reason,
    }


def _blocker_details(repair_state) -> dict[str, Any] | None:
    if repair_state.blocker_type != "external_dependency" or not repair_state.blocked_job_names:
        return None
    return {
        "type": repair_state.blocker_type,
        "summary": repair_state.blocker_summary,
        "suggested_action": repair_state.blocker_suggested_action,
        "blocked_job_names": list(repair_state.blocked_job_names),
        "dependency_evidence": list(repair_state.dependency_evidence),
    }


async def _duration_details(broker, task_id: str, stored) -> tuple[int, dict[str, Any]]:
    fallback_ms = max(0, int(((stored.updated_at or stored.created_at) - stored.created_at) * 1000))
    try:
        from pr_agent.distributed.lifecycle import summarize_lifecycle

        events = await broker.get_lifecycle_events(task_id)
        summary = summarize_lifecycle(events)
        return summary.processing_total_ms or fallback_ms, summary.to_dict()
    except Exception:
        return fallback_ms, {"processing_total_ms": fallback_ms}


async def _record_health(broker, task_id: str, success: bool, error: str = "") -> None:
    try:
        await broker.record_triage_persistence(task_id, success, error)
    except Exception:
        get_logger().exception(f"Failed to record triage persistence health: task_id={task_id}")


async def _admit_final_report_after_persist(broker, stored, persisted: bool) -> None:
    if not persisted:
        return
    from datetime import datetime, timezone

    from pr_agent.triage.final_repair_report import (
        FinalRepairReportState,
        RepairReportStatus,
        final_repair_report_enabled,
    )

    if not final_repair_report_enabled():
        return
    manifest = stored.repair_commit_manifest
    if manifest is None or not manifest.entries:
        now = datetime.now(timezone.utc).isoformat()
        state = FinalRepairReportState(
            RepairReportStatus.NOT_APPLICABLE,
            failure_reason="本次修复未产生代码提交。",
            created_at=now,
            updated_at=now,
        )
        await broker.set_final_repair_report_state(stored.task_id, stored.task_id, state)
        return
    await broker.admit_final_repair_report(stored.task_id)


async def _repair_report(broker, task_id: str, stored, binding, terminal_status: str) -> dict[str, Any]:
    from pr_agent.triage.repair_result_identity import resolve_repair_result_identity

    repair_state = stored.pipeline_repair_state
    result_identity = resolve_repair_result_identity(
        stored.repair_commit_manifest,
        repair_state.repair_actions,
        current_pipeline_id=repair_state.latest_pipeline_id,
        current_pipeline_sha=repair_state.latest_pipeline_sha,
        current_pipeline_status=terminal_status,
    )
    evidence_pipeline = {
        "id": repair_state.latest_pipeline_id,
        "sha": repair_state.latest_pipeline_sha,
        "status": terminal_status,
        "coverage": repair_state.final_coverage,
        "coverage_source": repair_state.final_coverage_source,
        "coverage_status": repair_state.final_coverage_status,
    }
    source_explanations = repair_source_failure_explanations(repair_state)
    try:
        progress = [event.to_dict() for event in await broker.get_repair_progress(task_id)]
    except Exception:
        progress = []
    report_state = stored.final_repair_report_state
    report_input = None
    if report_state is not None and report_state.input_digest:
        try:
            candidate = await broker.get_final_repair_report_input(task_id)
            if candidate is not None and candidate.digest() == report_state.input_digest:
                report_input = candidate
        except Exception:
            get_logger().exception(f"Failed to load final repair report input: task_id={task_id}")
    final_changes = []
    if report_input is not None:
        from pr_agent.distributed.repair_report_tasks import final_file_changes

        final_changes = [item.to_dict() for item in final_file_changes(report_input)]
    return {
        "schema_version": 2,
        "task_id": task_id,
        "source": "durable",
        "status": stored.status.value,
        "phase": repair_state.phase.value,
        "terminal": True,
        "created_at": stored.created_at,
        "updated_at": stored.updated_at,
        "mr": {
            "project": stored.mr.project_id if stored.mr is not None else "",
            "iid": stored.mr.iid if stored.mr is not None else 0,
            "title": binding.mr_title if binding is not None else "",
            "url": stored.envelope.pr_url,
            "source_branch": binding.source_branch if binding is not None else "",
        },
        "source_pipeline": {
            "id": int(stored.envelope.payload.get("source_pipeline_id") or 0),
            "sha": str(stored.envelope.payload.get("source_pipeline_sha") or ""),
        },
        "final_pipeline": ({
            "id": result_identity.pipeline_id,
            "sha": result_identity.commit_sha,
            "status": result_identity.pipeline_status or terminal_status,
            "coverage": repair_state.final_coverage,
            "coverage_source": repair_state.final_coverage_source,
            "coverage_status": repair_state.final_coverage_status,
        } if result_identity.exists else None),
        "evidence_pipeline": None if result_identity.exists else evidence_pipeline,
        "coverage_continuation": _coverage_continuation_details(repair_state),
        "repair_outcome": repair_state.repair_outcome,
        "blocker": _blocker_details(repair_state),
        "selected_categories": list(repair_state.selected_categories),
        "failed_job_names": list(repair_state.failed_job_names),
        "completed_steps": list(repair_state.completed_steps),
        "error": stored.error or repair_state.terminal_error,
        "actions": [action.to_dict() for action in repair_state.repair_actions],
        "progress": progress,
        "report": report_state.to_public_dict() if report_state is not None else None,
        "final_file_changes": final_changes,
        "source_job_names": list(
            report_input.failed_jobs
            if report_input
            else dict.fromkeys(record.job_name for record in source_explanations if record.job_name)
        ),
        "source_jobs": list(source_job_records(source_explanations)),
    }


async def persist_repair_terminal(broker, task_id: str, *, error: str = "") -> bool:
    """Persist or enrich one terminal dashboard row for every repair command."""
    try:
        stored = await broker.get_task(task_id)
        if stored is None:
            return False
        command = _repair_command(stored.envelope.command)
        if not command:
            return False
        if stored.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED}:
            return False
        binding = await broker.get_task_triage_card(task_id)
        if command == "/triage" and await asyncio.to_thread(has_triage_run_task, task_id):
            report = await _repair_report(broker, task_id, stored, binding, _terminal_status(stored, binding))
            updated = bool(await asyncio.to_thread(update_triage_run_repair_report, task_id, report))
            await _record_health(broker, task_id, updated, "" if updated else "repair report merge returned false")
            await _admit_final_report_after_persist(broker, stored, updated)
            return updated
        repair_state = stored.pipeline_repair_state
        original_pipeline_id = int(stored.envelope.payload.get("source_pipeline_id") or 0)
        original_sha = str(stored.envelope.payload.get("source_pipeline_sha") or "")
        if binding is not None:
            original_pipeline_id = original_pipeline_id or binding.pipeline_id
            original_sha = original_sha or binding.pipeline_sha
        failed_job_names = list(binding.failed_job_names if binding is not None else ())
        detected_categories = [category.value for category in classify_failed_jobs(
            {"name": name} for name in failed_job_names
        )]
        if command in {"/fix-format", "/fix_format"}:
            detected_categories = ["format"]
        detected_categories = detected_categories or ["unknown"]
        categories = list(repair_state.selected_categories) or detected_categories
        repair_outcome = repair_state.repair_outcome or (
            "success" if binding is not None and binding.state is TriageCardState.REPAIR_SUCCEEDED else "failed"
        )
        success = repair_outcome == "success"
        terminal_error = error or stored.error or repair_state.terminal_error
        from pr_agent.triage.model_availability import is_model_service_unavailable

        model_unavailable = is_model_service_unavailable(
            repair_state.terminal_failure_kind,
            terminal_error,
        )
        final_pipeline_id = repair_state.latest_pipeline_id
        final_pipeline_sha = repair_state.latest_pipeline_sha
        if binding is not None:
            final_pipeline_id = final_pipeline_id or binding.current_pipeline_id
            final_pipeline_sha = final_pipeline_sha or binding.current_pipeline_sha
        duration_ms, duration_breakdown = await _duration_details(broker, task_id, stored)
        finish_reason = (
            "external_dependency_blocked"
            if repair_outcome == "blocked"
            else "model_service_unavailable"
            if model_unavailable
            else "completed"
            if success
            else "pipeline_failed"
        )
        if repair_outcome != "blocked" and not success and stored.status is TaskStatus.CANCELED:
            finish_reason = "canceled"
        elif (
            repair_outcome != "blocked"
            and not success
            and not model_unavailable
            and stored.status is TaskStatus.FAILED
        ):
            finish_reason = "infrastructure_error"
        terminal_status = _terminal_status(stored, binding)
        from pr_agent.triage.repair_result_identity import resolve_repair_result_identity

        result_identity = resolve_repair_result_identity(
            stored.repair_commit_manifest,
            repair_state.repair_actions,
            current_pipeline_id=final_pipeline_id,
            current_pipeline_sha=final_pipeline_sha,
            current_pipeline_status=terminal_status,
        )
        repair_report = await _repair_report(broker, task_id, stored, binding, terminal_status)
        record = {
            "task_id": task_id,
            "created_at": stored.envelope.created_at,
            "pr_url": stored.envelope.pr_url,
            "project": stored.mr.project_id if stored.mr is not None else "",
            "mr_iid": stored.mr.iid if stored.mr is not None else None,
            "mr_author": binding.mr_author_username if binding is not None else None,
            "feishu_user_name": None,
            "source_branch": binding.source_branch if binding is not None else "",
            "commit_sha": original_sha,
            "pipeline_id": original_pipeline_id or None,
            "trigger_type": "pipeline_failed",
            "failed_job_names": failed_job_names,
            "failure_categories": categories,
            "success": int(success),
            "repair_outcome": repair_outcome,
            "category_results": [result.to_dict() for result in repair_state.category_results],
            "finish_reason": finish_reason,
            "iterations": repair_state.iterations,
            "max_iterations": repair_state.max_iterations,
            "pushed_sha": result_identity.commit_sha,
            "final_pipeline_status": (
                result_identity.pipeline_status or terminal_status if result_identity.exists else "unknown"
            ),
            "final_coverage": repair_state.final_coverage,
            "failure_signatures": [],
            "fix_duration_ms": duration_ms,
            "error": terminal_error or None,
            "extra": {
                "duration_breakdown": duration_breakdown,
                "source_pipeline_id": original_pipeline_id or None,
                "source_pipeline_sha": original_sha,
                "final_pipeline_id": result_identity.pipeline_id or None,
                "final_pipeline_sha": result_identity.commit_sha,
                "evidence_pipeline_id": final_pipeline_id or None,
                "evidence_pipeline_sha": final_pipeline_sha,
                "evidence_pipeline_status": terminal_status,
                "coverage_source": repair_state.final_coverage_source,
                "coverage_status": repair_state.final_coverage_status,
                "coverage_continuation": _coverage_continuation_details(repair_state),
                "completed_steps": list(repair_state.completed_steps),
                "final_failed_job_names": list(repair_state.failed_job_names),
                "selected_categories": list(repair_state.selected_categories),
                "detected_failure_categories": detected_categories,
                "repair_outcome": repair_outcome,
                "terminal_failure_kind": repair_state.terminal_failure_kind,
                "terminal_validation_error_code": repair_state.terminal_validation_error_code,
                "terminal_validation_summary": repair_state.terminal_validation_summary,
                "normalized_diagnostic_alias_count": repair_state.normalized_diagnostic_alias_count,
                "blocker_type": repair_state.blocker_type,
                "blocker_summary": repair_state.blocker_summary,
                "blocker_suggested_action": repair_state.blocker_suggested_action,
                "blocked_job_names": list(repair_state.blocked_job_names),
                "dependency_evidence": list(repair_state.dependency_evidence),
                "blocker": _blocker_details(repair_state),
                "category_results": [result.to_dict() for result in repair_state.category_results],
                "introduced_failure_categories": list(repair_state.introduced_failure_categories),
                "introduced_failed_job_names": list(repair_state.introduced_failed_job_names),
                "effective_categories": list(repair_state.effective_categories),
                "auto_format_cleanup": repair_state.auto_format_cleanup,
                "source_failure_explanations": [
                    record.to_dict() for record in repair_source_failure_explanations(repair_state)
                ],
                "failure_explanations": [record.to_dict() for record in repair_state.failure_explanations],
                "repair_report": repair_report,
            },
        }
        saved = bool(await asyncio.to_thread(save_triage_run, record))
        await _record_health(broker, task_id, saved, "" if saved else "save_triage_run returned false")
        await _admit_final_report_after_persist(broker, stored, saved)
        try:
            from ut_agent.repair_memory.outcomes import settle_without_validation

            await asyncio.to_thread(
                settle_without_validation,
                task_id,
                "external_dependency_blocked" if repair_outcome == "blocked" else "terminal_before_validation",
            )
        except Exception:
            get_logger().debug(f"Repair memory terminal settlement skipped: task_id={task_id}")
        return saved
    except Exception as exc:
        get_logger().exception(f"Failed to persist repair terminal result: task_id={task_id}")
        await _record_health(broker, task_id, False, str(exc))
        return False


async def persist_repair_rollback(broker, repair_task_id: str) -> bool:
    """Add rollback audit data without changing the original repair result columns."""
    try:
        from pr_agent.triage.store import get_triage_run_task

        stored = await broker.get_task(repair_task_id)
        state = stored.repair_rollback_state if stored is not None else None
        if state is None:
            return False
        record = await asyncio.to_thread(get_triage_run_task, repair_task_id)
        if record is None:
            return False
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        report = extra.get("repair_report") if isinstance(extra.get("repair_report"), dict) else {}
        report = dict(report)
        report["rollback"] = {
            "task_id": state.rollback_task_id,
            "trigger": state.trigger,
            "status": state.status.value,
            "manifest_digest": state.manifest_digest,
            "rollback_commit_sha": state.rollback_commit_sha,
            "requested_by": state.requested_by,
            "failure_code": state.failure_code.value if state.failure_code else "",
            "failure_message": state.failure_message,
        }
        return bool(await asyncio.to_thread(update_triage_run_repair_report, repair_task_id, report))
    except Exception:
        get_logger().exception(f"Failed to persist repair rollback result: task_id={repair_task_id}")
        return False


async def persist_post_repair_ut_terminal(broker, task_id: str) -> bool:
    """Persist UT supplementation as a separate row from its parent CI repair."""
    try:
        import json

        from pr_agent.triage.store import save_triage_run

        stored = await broker.get_task(task_id)
        binding = await broker.get_task_triage_card(task_id)
        if stored is None or binding is None or stored.mr is None:
            return False
        state = binding.post_repair_ut
        try:
            payload = json.loads(stored.result) if stored.result else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        iterations = int(result.get("iterations") or 0) if isinstance(result, dict) else 0
        max_iterations = int(result.get("max_iterations") or 0) if isinstance(result, dict) else 0
        success = state.status in {
            PostRepairUTStatus.SUCCEEDED,
            PostRepairUTStatus.PARTIAL,
            PostRepairUTStatus.UNVERIFIED,
        }
        duration_ms = max(0, int((stored.updated_at - stored.created_at) * 1000))
        record = {
            "task_id": task_id,
            "created_at": stored.envelope.created_at,
            "pr_url": stored.envelope.pr_url,
            "project": stored.mr.project_id,
            "mr_iid": stored.mr.iid,
            "mr_author": binding.mr_author_username,
            "source_branch": binding.source_branch,
            "commit_sha": state.baseline_sha,
            "pipeline_id": state.baseline_pipeline_id,
            "trigger_type": "post_repair_ut",
            "failed_job_names": [],
            "failure_categories": ["unit_test"],
            "success": int(success),
            "repair_outcome": state.status.value,
            "finish_reason": state.outcome_reason,
            "iterations": iterations,
            "max_iterations": max_iterations,
            "pushed_sha": state.current_sha,
            "final_pipeline_status": "success" if success else "failed",
            "final_coverage": state.coverage_after,
            "fix_duration_ms": duration_ms,
            "error": None if success else state.outcome_reason,
            "extra": {
                "post_repair_ut": {
                    "origin_repair_task_id": state.origin_repair_task_id,
                    "coverage_before": state.coverage_before,
                    "coverage_after": state.coverage_after,
                    "coverage_delta": (
                        state.coverage_after - state.coverage_before
                        if state.coverage_after is not None and state.coverage_before is not None
                        else None
                    ),
                    "status": state.status.value,
                    "rollback_task_id": state.rollback_task_id,
                    "rollback_status": state.rollback_status,
                    "rollback_commit_sha": state.rollback_commit_sha,
                }
            },
        }
        return bool(await asyncio.to_thread(save_triage_run, record))
    except Exception:
        get_logger().exception(f"Failed to persist post-repair UT result: task_id={task_id}")
        return False


async def reconcile_late_repair_success(broker, event: PipelineEvent) -> list[str]:
    """Correct a terminal failure only for the exact Pipeline and SHA that later succeeded."""
    if event.status != "success":
        return []
    corrected = []
    for stored in await broker.list_terminal_repair_candidates(event.project_id, event.sha):
        repair_state = stored.pipeline_repair_state
        if (
            stored.status not in {TaskStatus.FAILED, TaskStatus.COMPLETED}
            or not _is_outer_repair(stored.envelope.command)
            or repair_state.latest_pipeline_id != event.pipeline_id
            or repair_state.latest_pipeline_sha != event.sha
            or repair_state.final_pipeline_status == "success"
            or (
                stored.status is TaskStatus.COMPLETED
                and repair_state.final_pipeline_status != "failed"
            )
        ):
            continue
        binding = await broker.get_task_triage_card(stored.task_id)
        if (
            binding is None
            or binding.state not in {TriageCardState.PIPELINE_FAILED, TriageCardState.REPAIR_FAILED}
            or stored.mr is None
            or binding.project_id != stored.mr.project_id
            or binding.mr_iid != stored.mr.iid
        ):
            continue
        terminal = replace(
            repair_state,
            phase=PipelineRepairPhase.TERMINAL,
            latest_pipeline_id=event.pipeline_id,
            latest_pipeline_sha=event.sha,
            final_pipeline_status="success",
            failed_job_names=(),
            terminal_error="",
        )
        status_markdown = (
            "已收到此前异常终止任务对应的最终流水线结果，修复结果已更正为成功。"
            f"\n\n- Pipeline: `#{event.pipeline_id}`\n- Commit: `{event.sha[:12]}`"
        )
        items = tuple(
            replace(
                item,
                status=(
                    RepairItemStatus.SUCCEEDED
                    if item.task_id == stored.task_id or item.category.value == "pipeline"
                    else RepairItemStatus.RESOLVED
                ),
                result_pipeline_id=event.pipeline_id,
                result_pipeline_sha=event.sha,
                status_markdown=(
                    "流水线已通过"
                    if item.task_id == stored.task_id or item.category.value == "pipeline"
                    else "已随最终流水线通过"
                ),
                failure_explanations=(),
            )
            for item in binding.repair_items
        )
        predicted = replace(
            binding,
            repair_items=items,
            state=TriageCardState.REPAIR_SUCCEEDED,
            status_markdown=status_markdown,
            current_pipeline_id=event.pipeline_id,
            current_pipeline_sha=event.sha,
            active_task_id="",
            active_category="",
            revision=binding.revision + 1,
        )
        from pr_agent.distributed.notifications import build_card_update_notification

        notification = build_card_update_notification(
            predicted,
            stored.task_id,
            TriageCardState.REPAIR_SUCCEEDED,
            status_markdown,
        )
        card_changed = await broker.correct_late_repair_terminal(
            task_id=stored.task_id,
            expected_task_status=stored.status,
            terminal_state=terminal,
            expected_card_states={TriageCardState.PIPELINE_FAILED, TriageCardState.REPAIR_FAILED},
            expected_revision=binding.revision,
            repair_items=items,
            status_markdown=status_markdown,
            current_pipeline_id=event.pipeline_id,
            current_pipeline_sha=event.sha,
            notification=notification,
        )
        if not card_changed:
            continue
        await persist_repair_terminal(broker, stored.task_id)
        try:
            from pr_agent.distributed.models import NotificationEnvelope

            reminder = NotificationEnvelope.new(
                task_id=stored.task_id,
                receive_id=binding.receive_id,
                recipient_email="",
                recipient_username="",
                kind="text",
                content=(
                    f"✅【{binding.project_id} !{binding.mr_iid}】修复结果已更正为成功。\n"
                    f"Pipeline: #{event.pipeline_id}\nMR: {binding.mr_url}"
                ),
                title="PR-Agent",
                header_template="green",
                mr_url=binding.mr_url,
                notification_id=hashlib.sha256(
                    f"{stored.task_id}\x1flate-success\x1f{event.sha}".encode("utf-8")
                ).hexdigest()[:32],
            )
            await broker.enqueue_notification(reminder)
        except Exception:
            get_logger().exception(f"Failed to queue late repair correction reminder: task_id={stored.task_id}")
        corrected.append(stored.task_id)
    return corrected
