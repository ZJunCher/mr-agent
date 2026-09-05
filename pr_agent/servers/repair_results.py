"""Signed, read-only repair-result pages and live APIs for MR owners."""

# ruff: noqa: E501 -- The embedded dependency-free HTML/CSS/JS keeps browser assets in one removable module.

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from pr_agent.distributed.repair_report_tasks import final_file_changes
from pr_agent.log import get_logger
from pr_agent.servers.repair_results_page import render_repair_result_page
from pr_agent.triage.failure_explanations import source_job_records
from pr_agent.triage.final_repair_report import FinalRepairReportInput
from pr_agent.triage.pipeline_repair import repair_source_failure_explanations
from pr_agent.triage.repair_details import (
    repair_details_enabled,
    repair_details_heartbeat_seconds,
    sanitize_repair_text,
    verify_repair_details_signature,
)
from pr_agent.triage.store import get_triage_run_task

router = APIRouter()
_broker_provider: Callable[[], Any] | None = None


def configure_repair_results_broker(provider: Callable[[], Any] | None) -> None:
    """Inject the process-local Redis broker provider without creating a second client."""
    global _broker_provider
    _broker_provider = provider


def _private_headers() -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store",
        "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def _verify_or_404(task_id: str, signature: str) -> None:
    if not repair_details_enabled() or not verify_repair_details_signature(task_id, signature):
        raise HTTPException(status_code=404, detail="Not found")


def _deduplicate_progress(events) -> list[dict[str, Any]]:
    """Collapse adjacent low-value activity while preserving durable milestones."""
    milestones = {"committing", "waiting_pipeline", "validating", "terminal"}
    output: list[dict[str, Any]] = []
    for raw_event in events or ():
        event = raw_event.to_dict() if hasattr(raw_event, "to_dict") else dict(raw_event)
        event["count"] = max(1, int(event.get("count") or 1))
        if (
            output
            and event.get("phase") not in milestones
            and output[-1].get("phase") == event.get("phase")
            and output[-1].get("summary") == event.get("summary")
        ):
            output[-1]["count"] += event["count"]
            if event.get("event_id"):
                output[-1]["event_id"] = event["event_id"]
            if event.get("occurred_at"):
                output[-1]["last_occurred_at"] = event["occurred_at"]
            continue
        output.append(event)
    return output


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _records(values: object) -> list[Mapping[str, Any]]:
    if not isinstance(values, (list, tuple)):
        return []
    return [value for value in values if isinstance(value, Mapping)]


def _unique_text(values: Iterable[object], *, limit: int, count: int) -> list[str]:
    output = []
    for value in values:
        text = sanitize_repair_text(value, limit).strip()
        if text and text not in output:
            output.append(text)
        if len(output) >= count:
            break
    return output


def _dependency_candidate(value: object) -> dict[str, Any]:
    candidate = _mapping(value)
    file_paths = _mapping(candidate.get("file_paths"))
    return {
        "branch": sanitize_repair_text(candidate.get("branch"), 300).strip(),
        "resolved_sha": sanitize_repair_text(candidate.get("resolved_sha"), 100).strip(),
        "verification_complete": candidate.get("verification_complete") is True,
        "matched_queries": _unique_text(candidate.get("matched_queries") or (), limit=200, count=20),
        "missing_queries": _unique_text(candidate.get("missing_queries") or (), limit=200, count=20),
        "file_paths": {
            sanitize_repair_text(name, 200).strip(): sanitize_repair_text(path, 1000).strip()
            for name, path in list(file_paths.items())[:20]
            if sanitize_repair_text(name, 200).strip() and sanitize_repair_text(path, 1000).strip()
        },
    }


def _sanitize_dependency_blocker(value: object) -> dict[str, Any]:
    """Expose only bounded, verified dependency facts on the Native repair page."""
    blocker = _mapping(value)
    blocker_type = sanitize_repair_text(blocker.get("type"), 100).strip()
    if blocker_type != "external_dependency":
        return {}
    evidence_records = []
    for raw_evidence in _records(blocker.get("dependency_evidence"))[:20]:
        evidence_records.append({
            "project_path": sanitize_repair_text(raw_evidence.get("project_path"), 300).strip(),
            "declared_branch": sanitize_repair_text(raw_evidence.get("declared_branch"), 300).strip(),
            "declared_sha": sanitize_repair_text(raw_evidence.get("declared_sha"), 100).strip(),
            "queries": [
                {"filename": filename}
                for filename in _unique_text(
                    (_mapping(item).get("filename") for item in raw_evidence.get("queries") or ()),
                    limit=200,
                    count=20,
                )
            ],
            "current_branch": _dependency_candidate(raw_evidence.get("current_branch")),
            "candidate_kind": sanitize_repair_text(raw_evidence.get("candidate_kind"), 100).strip(),
            "verified_candidates": [
                _dependency_candidate(candidate)
                for candidate in _records(raw_evidence.get("verified_candidates"))[:5]
            ],
            "checked_branch_count": min(max(0, int(raw_evidence.get("checked_branch_count") or 0)), 10_000),
            "catalog_truncated": raw_evidence.get("catalog_truncated") is True,
        })
    return {
        "type": blocker_type,
        "summary": sanitize_repair_text(blocker.get("summary"), 2000).strip(),
        "suggested_action": sanitize_repair_text(blocker.get("suggested_action"), 2000).strip(),
        "blocked_job_names": _unique_text(blocker.get("blocked_job_names") or (), limit=120, count=40),
        "dependency_evidence": evidence_records,
    }


def _live_snapshot(stored, binding, progress, report_input=None) -> dict[str, Any]:
    from pr_agent.triage.repair_result_identity import resolve_repair_result_identity

    state = stored.pipeline_repair_state
    payload = stored.envelope.payload
    source_pipeline_id = int(payload.get("source_pipeline_id") or (binding.pipeline_id if binding else 0) or 0)
    source_pipeline_sha = str(payload.get("source_pipeline_sha") or (binding.pipeline_sha if binding else ""))
    project = stored.mr.project_id if stored.mr is not None else (binding.project_id if binding else "")
    iid = stored.mr.iid if stored.mr is not None else (binding.mr_iid if binding else 0)
    rollback_state = stored.repair_rollback_state
    report_state = stored.final_repair_report_state
    valid_report_input = (
        report_input
        if isinstance(report_input, FinalRepairReportInput)
        and report_state is not None
        and report_input.digest() == report_state.input_digest
        else None
    )
    failed_job_names = (
        list(state.failed_job_names)
        if state.final_pipeline_status
        else list(binding.failed_job_names if binding else ())
    )
    evidence_pipeline = {
        "id": state.latest_pipeline_id,
        "sha": state.latest_pipeline_sha,
        "status": state.final_pipeline_status,
        "coverage": state.final_coverage,
        "coverage_source": state.final_coverage_source,
        "coverage_status": state.final_coverage_status,
    }
    result_identity = resolve_repair_result_identity(
        stored.repair_commit_manifest,
        state.repair_actions,
        current_pipeline_id=state.latest_pipeline_id,
        current_pipeline_sha=state.latest_pipeline_sha,
        current_pipeline_status=state.final_pipeline_status,
    )
    return {
        "schema_version": 2,
        "task_id": stored.task_id,
        "source": "live",
        "status": stored.status.value,
        "phase": state.phase.value,
        "terminal": stored.status.value in {"completed", "failed", "canceled"},
        "created_at": stored.created_at,
        "updated_at": stored.updated_at,
        "mr": {
            "project": project,
            "iid": iid,
            "title": binding.mr_title if binding is not None else "",
            "url": stored.envelope.pr_url,
            "source_branch": binding.source_branch if binding is not None else "",
        },
        "source_pipeline": {"id": source_pipeline_id, "sha": source_pipeline_sha},
        "final_pipeline": ({
            "id": result_identity.pipeline_id,
            "sha": result_identity.commit_sha,
            "status": result_identity.pipeline_status or state.final_pipeline_status,
            "coverage": state.final_coverage,
            "coverage_source": state.final_coverage_source,
            "coverage_status": state.final_coverage_status,
        } if result_identity.exists else None),
        "evidence_pipeline": None if result_identity.exists else evidence_pipeline,
        "selected_categories": list(state.selected_categories),
        "repair_outcome": state.repair_outcome,
        "blocker": _sanitize_dependency_blocker({
            "type": state.blocker_type,
            "summary": state.blocker_summary,
            "suggested_action": state.blocker_suggested_action,
            "blocked_job_names": list(state.blocked_job_names),
            "dependency_evidence": list(state.dependency_evidence),
        }),
        "category_results": [item.to_dict() for item in state.category_results],
        "introduced_failure_categories": list(state.introduced_failure_categories),
        "introduced_failed_job_names": list(state.introduced_failed_job_names),
        "failed_job_names": failed_job_names,
        "completed_steps": list(state.completed_steps),
        "error": stored.error or state.terminal_error or state.terminal_validation_summary,
        "terminal_validation_error_code": state.terminal_validation_error_code,
        "terminal_validation_summary": state.terminal_validation_summary,
        "normalized_diagnostic_alias_count": state.normalized_diagnostic_alias_count,
        "actions": [action.to_dict() for action in state.repair_actions],
        "progress": _deduplicate_progress(progress),
        "rollback": rollback_state.to_dict() if rollback_state is not None else None,
        "report": report_state.to_public_dict() if report_state is not None else None,
        "final_file_changes": (
            [item.to_dict() for item in final_file_changes(valid_report_input)]
            if valid_report_input is not None
            else []
        ),
        "source_jobs": list(source_job_records(repair_source_failure_explanations(state))),
    }


def _stored_source_failure_explanations(extra: dict[str, Any]):
    return extra.get("source_failure_explanations") or extra.get("failure_explanations") or ()


def _backfill_source_jobs(snapshot: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    output = dict(snapshot)
    if "source_jobs" not in output:
        output["source_jobs"] = list(source_job_records(_stored_source_failure_explanations(extra)))
    return output


def _durable_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    report = extra.get("repair_report") if isinstance(extra.get("repair_report"), dict) else None
    if report is not None:
        snapshot = _backfill_source_jobs(
            {**report, "source": "durable", "progress": _deduplicate_progress(report.get("progress") or ())},
            extra,
        )
        if not snapshot.get("blocker"):
            snapshot["blocker"] = extra.get("blocker")
        snapshot["blocker"] = _sanitize_dependency_blocker(snapshot.get("blocker"))
        final_pipeline = snapshot.get("final_pipeline")
        if isinstance(final_pipeline, dict):
            final_pipeline.setdefault("coverage_source", str(extra.get("coverage_source") or ""))
            final_pipeline.setdefault("coverage_status", str(extra.get("coverage_status") or ""))
        return snapshot
    has_result_pipeline = bool(str(record.get("pushed_sha") or "") and int(extra.get("final_pipeline_id") or 0))
    final_pipeline = {
        "id": int(extra.get("final_pipeline_id") or 0),
        "sha": str(record.get("pushed_sha") or ""),
        "status": str(record.get("final_pipeline_status") or ""),
        "coverage": record.get("final_coverage"),
        "coverage_source": str(extra.get("coverage_source") or ""),
        "coverage_status": str(extra.get("coverage_status") or ""),
    }
    evidence_pipeline = {
        "id": int(extra.get("evidence_pipeline_id") or extra.get("final_pipeline_id") or 0),
        "sha": str(extra.get("evidence_pipeline_sha") or ""),
        "status": str(extra.get("evidence_pipeline_status") or record.get("final_pipeline_status") or ""),
        "coverage": record.get("final_coverage"),
        "coverage_source": str(extra.get("coverage_source") or ""),
        "coverage_status": str(extra.get("coverage_status") or ""),
    }
    return {
        "schema_version": 2,
        "task_id": str(record.get("task_id") or ""),
        "source": "durable",
        "status": "completed" if record.get("success") else "failed",
        "phase": "terminal",
        "terminal": True,
        "created_at": record.get("created_at"),
        "updated_at": record.get("created_at"),
        "mr": {
            "project": str(record.get("project") or ""),
            "iid": int(record.get("mr_iid") or 0),
            "title": "",
            "url": str(record.get("pr_url") or ""),
            "source_branch": str(record.get("source_branch") or ""),
        },
        "source_pipeline": {
            "id": int(record.get("pipeline_id") or 0),
            "sha": str(record.get("commit_sha") or ""),
        },
        "final_pipeline": final_pipeline if has_result_pipeline else None,
        "evidence_pipeline": None if has_result_pipeline else evidence_pipeline,
        "selected_categories": list(record.get("failure_categories_list") or ()),
        "repair_outcome": str(record.get("repair_outcome") or ""),
        "blocker": _sanitize_dependency_blocker(extra.get("blocker") or {
            "type": extra.get("blocker_type"),
            "summary": extra.get("blocker_summary"),
            "suggested_action": extra.get("blocker_suggested_action"),
            "blocked_job_names": extra.get("blocked_job_names") or (),
            "dependency_evidence": extra.get("dependency_evidence") or (),
        }),
        "category_results": list(record.get("category_results") or ()),
        "introduced_failure_categories": list(extra.get("introduced_failure_categories") or ()),
        "introduced_failed_job_names": list(extra.get("introduced_failed_job_names") or ()),
        "failed_job_names": list(extra.get("final_failed_job_names") or ()),
        "completed_steps": list(extra.get("completed_steps") or ()),
        "error": str(
            record.get("error")
            or extra.get("terminal_validation_summary")
            or ""
        ),
        "terminal_validation_error_code": str(extra.get("terminal_validation_error_code") or ""),
        "terminal_validation_summary": str(extra.get("terminal_validation_summary") or ""),
        "normalized_diagnostic_alias_count": int(extra.get("normalized_diagnostic_alias_count") or 0),
        "actions": [],
        "progress": [],
        "rollback": None,
        "report": None,
        "final_file_changes": [],
        "source_jobs": list(source_job_records(_stored_source_failure_explanations(extra))),
    }


def _snapshot_is_settled(snapshot: dict[str, Any]) -> bool:
    if not snapshot.get("terminal"):
        return False
    report = snapshot.get("report")
    if not isinstance(report, dict):
        return True
    return str(report.get("status") or "") in {"not_applicable", "model_generated", "fallback"}


def _get_broker():
    return _broker_provider() if _broker_provider is not None else None


async def load_repair_result_snapshot(task_id: str, broker=None) -> dict[str, Any] | None:
    broker = broker if broker is not None else _get_broker()
    if broker is not None:
        try:
            stored = await broker.get_task(task_id)
            if stored is not None:
                binding, progress, report_input = await asyncio.gather(
                    broker.get_task_triage_card(task_id),
                    broker.get_repair_progress(task_id),
                    broker.get_final_repair_report_input(task_id),
                )
                return _live_snapshot(stored, binding, progress, report_input)
        except Exception as error:
            get_logger().warning(f"Unable to load live repair result task_id={task_id}: {error}")
    record = await asyncio.to_thread(get_triage_run_task, task_id)
    return _durable_snapshot(record) if record is not None else None


def _repair_result_html(task_id: str, signature: str, *, embedded: bool = False) -> str:
    return render_repair_result_page(task_id, signature, embedded=embedded)


def _legacy_repair_result_html(task_id: str, signature: str) -> str:
    """Render the dependency-free, owner-facing live repair experience."""
    template = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="robots" content="noindex,nofollow">
  <meta name="color-scheme" content="dark">
  <title>CI 修复详情 · PR-Agent</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #080f1e;
      --surface: #101a2c;
      --surface-raised: #172338;
      --surface-soft: #0d1728;
      --text: #f8fafc;
      --muted: #aebdd0;
      --subtle: #7e90a9;
      --border: #31415a;
      --border-strong: #4b607e;
      --blue: #6ea8fe;
      --blue-soft: rgba(110, 168, 254, .12);
      --success: #4ade80;
      --success-soft: rgba(74, 222, 128, .11);
      --warning: #fbbf24;
      --warning-soft: rgba(251, 191, 36, .11);
      --danger: #fb7185;
      --danger-soft: rgba(251, 113, 133, .11);
      --focus: #93c5fd;
      --radius: 18px;
      --shadow: 0 24px 70px rgba(0, 0, 0, .28);
    }
    * { box-sizing: border-box; }
    html { min-width: 320px; background: var(--bg); scroll-behavior: smooth; }
    body {
      min-height: 100vh;
      margin: 0;
      overflow-x: hidden;
      color: var(--text);
      background:
        radial-gradient(circle at 85% -10%, rgba(59, 130, 246, .17), transparent 34rem),
        radial-gradient(circle at -10% 42%, rgba(74, 222, 128, .07), transparent 30rem),
        var(--bg);
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 15px;
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
    }
    a { color: inherit; }
    button, summary, a { -webkit-tap-highlight-color: transparent; }
    :is(a, button, summary):focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; }
    .shell { width: min(100% - 32px, 1120px); margin: 0 auto; padding: 28px 0 72px; }
    .topbar {
      display: flex;
      min-height: 48px;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 22px;
    }
    .brand { display: flex; align-items: center; gap: 11px; color: #dbeafe; font-weight: 760; letter-spacing: -.02em; }
    .brand-mark {
      display: grid;
      width: 34px;
      height: 34px;
      place-items: center;
      border: 1px solid rgba(110, 168, 254, .42);
      border-radius: 11px;
      background: linear-gradient(145deg, rgba(110, 168, 254, .2), rgba(74, 222, 128, .07));
      box-shadow: inset 0 1px rgba(255, 255, 255, .08);
    }
    .brand-mark svg { width: 19px; height: 19px; fill: none; stroke: #9fc3ff; stroke-width: 1.8; }
    .connection { display: flex; align-items: center; gap: 9px; color: var(--muted); font-size: 12px; }
    .connection-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warning); box-shadow: 0 0 0 5px var(--warning-soft); }
    .connection.live .connection-dot { background: var(--success); box-shadow: 0 0 0 5px var(--success-soft); }
    .connection.settled .connection-dot { background: var(--success); box-shadow: 0 0 0 5px var(--success-soft); }
    .connection.offline .connection-dot { background: var(--danger); box-shadow: 0 0 0 5px var(--danger-soft); }
    .hero {
      position: relative;
      overflow: hidden;
      padding: clamp(24px, 4vw, 42px);
      border: 1px solid var(--border);
      border-radius: 24px;
      background: linear-gradient(145deg, rgba(23, 35, 56, .96), rgba(13, 23, 40, .96));
      box-shadow: var(--shadow);
    }
    .hero::after {
      position: absolute;
      top: -90px;
      right: -70px;
      width: 260px;
      height: 260px;
      border: 1px solid rgba(110, 168, 254, .12);
      border-radius: 50%;
      background: rgba(110, 168, 254, .045);
      content: "";
    }
    .eyebrow { color: #8fb7f5; font-size: 11px; font-weight: 800; letter-spacing: .15em; text-transform: uppercase; }
    .hero-title { max-width: 790px; margin: 11px 0 0; font-size: clamp(25px, 4.2vw, 43px); line-height: 1.16; letter-spacing: -.035em; }
    .hero-subtitle { max-width: 780px; margin: 12px 0 0; color: var(--muted); font-size: 14px; }
    .hero-meta { display: flex; flex-wrap: wrap; gap: 9px 18px; margin-top: 22px; color: var(--muted); font-size: 12px; }
    .hero-link { display: inline-flex; min-height: 44px; align-items: center; gap: 7px; color: #a9c9ff; text-decoration: none; }
    .hero-link:hover { color: #d4e5ff; text-decoration: underline; text-underline-offset: 4px; }
    .status-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 24px; }
    .status-pill, .chip {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--border-strong);
      border-radius: 999px;
      font-size: 12px;
      font-weight: 720;
      line-height: 1;
      white-space: nowrap;
    }
    .status-pill { min-height: 34px; padding: 0 13px; }
    .chip { min-height: 28px; padding: 0 10px; color: #c6d4e7; background: rgba(148, 163, 184, .07); }
    .tone-live { color: #9dc2ff; border-color: rgba(110, 168, 254, .34); background: var(--blue-soft); }
    .tone-success { color: #86efac; border-color: rgba(74, 222, 128, .3); background: var(--success-soft); }
    .tone-warning { color: #fcd76b; border-color: rgba(251, 191, 36, .3); background: var(--warning-soft); }
    .tone-danger { color: #fda4af; border-color: rgba(251, 113, 133, .3); background: var(--danger-soft); }
    .summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
    .metric {
      min-width: 0;
      padding: 17px;
      border: 1px solid var(--border);
      border-radius: 15px;
      background: rgba(16, 26, 44, .86);
    }
    .metric-label { color: var(--subtle); font-size: 11px; font-weight: 720; letter-spacing: .035em; }
    .metric-value { margin-top: 6px; overflow-wrap: anywhere; font-size: 16px; font-weight: 740; letter-spacing: -.015em; }
    .content-grid { display: grid; grid-template-columns: minmax(0, 1fr) 300px; align-items: start; gap: 14px; margin-top: 14px; }
    .stack { display: grid; min-width: 0; gap: 14px; }
    .panel {
      min-width: 0;
      padding: 20px;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: rgba(16, 26, 44, .9);
      box-shadow: 0 12px 38px rgba(0, 0, 0, .13);
    }
    .sticky { position: sticky; top: 18px; }
    .panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 16px; }
    .panel-title { margin: 0; font-size: 16px; letter-spacing: -.01em; }
    .panel-note { margin: 3px 0 0; color: var(--subtle); font-size: 12px; }
    .action-list { display: grid; gap: 12px; }
    .action-card { padding: 17px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface-soft); }
    .action-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
    .action-title { margin: 0; font-size: 14px; font-weight: 760; }
    .chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
    .root-cause { margin: 14px 0 0; color: #dce7f6; line-height: 1.65; overflow-wrap: anywhere; }
    .measure-list { margin: 12px 0 0; padding-left: 19px; color: var(--muted); }
    .measure-list li + li { margin-top: 5px; }
    details {
      margin-top: 13px;
      border-top: 1px solid rgba(75, 96, 126, .55);
    }
    summary { display: flex; min-height: 44px; cursor: pointer; align-items: center; color: #b9cdf0; font-size: 12px; font-weight: 720; }
    summary:hover { color: #e0eaff; }
    .detail-body { padding: 0 0 8px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .file-list { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }
    .file-item, code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .file-item { padding: 8px 10px; border: 1px solid rgba(75, 96, 126, .48); border-radius: 9px; background: rgba(4, 10, 20, .35); overflow-wrap: anywhere; }
    .timeline { position: relative; display: grid; gap: 0; margin: 0; padding: 0; list-style: none; }
    .timeline::before { position: absolute; top: 14px; bottom: 14px; left: 5px; width: 1px; background: var(--border); content: ""; }
    .timeline-item { position: relative; padding: 0 0 18px 24px; }
    .timeline-item:last-child { padding-bottom: 0; }
    .timeline-dot { position: absolute; top: 8px; left: 1px; width: 9px; height: 9px; border: 2px solid var(--surface); border-radius: 50%; background: var(--blue); box-shadow: 0 0 0 2px rgba(110, 168, 254, .26); }
    .timeline-summary { color: #dce7f6; font-size: 13px; font-weight: 650; overflow-wrap: anywhere; }
    .timeline-meta { margin-top: 2px; color: var(--subtle); font-size: 11px; }
    .fact-list { display: grid; gap: 12px; margin: 0; }
    .fact { display: grid; gap: 3px; padding-bottom: 11px; border-bottom: 1px solid rgba(75, 96, 126, .42); }
    .fact:last-child { padding-bottom: 0; border-bottom: 0; }
    .fact dt { color: var(--subtle); font-size: 11px; }
    .fact dd { margin: 0; color: #dce7f6; font-size: 13px; font-weight: 650; overflow-wrap: anywhere; }
    .empty { padding: 24px 16px; color: var(--subtle); text-align: center; }
    .error-banner { display: none; margin-top: 14px; padding: 14px 16px; border: 1px solid rgba(251, 113, 133, .32); border-radius: 13px; color: #fecdd3; background: var(--danger-soft); }
    .error-banner.visible { display: block; }
    .skeleton { position: relative; overflow: hidden; min-height: 18px; border-radius: 7px; background: rgba(148, 163, 184, .1); }
    .skeleton::after { position: absolute; inset: 0; transform: translateX(-100%); background: linear-gradient(90deg, transparent, rgba(255,255,255,.08), transparent); animation: shimmer 1.4s infinite; content: ""; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
    @keyframes shimmer { to { transform: translateX(100%); } }
    @media (max-width: 900px) {
      .content-grid { grid-template-columns: 1fr; }
      .sticky { position: static; }
      .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      .shell { width: min(100% - 20px, 1120px); padding-top: 16px; }
      .topbar { align-items: flex-start; flex-direction: column; gap: 8px; }
      .hero { padding: 22px 18px; border-radius: 19px; }
      .summary-grid { grid-template-columns: 1fr; gap: 9px; }
      .metric { padding: 14px 15px; }
      .panel { padding: 16px; border-radius: 15px; }
      .panel-head, .action-head { align-items: flex-start; flex-direction: column; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M7 3v4M17 3v4M5 7h14v10a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3V7Zm4 5h6M9 16h3"/></svg>
        </span>
        <span>PR-Agent · CI Repair</span>
      </div>
      <div id="connection" class="connection" aria-label="实时连接状态">
        <span class="connection-dot" aria-hidden="true"></span><span id="connectionText">正在连接</span>
      </div>
    </header>

    <main>
      <section class="hero" aria-labelledby="pageTitle">
        <div class="eyebrow">查看修复详情</div>
        <h1 id="pageTitle" class="hero-title">正在载入 CI 修复报告</h1>
        <p id="heroSubtitle" class="hero-subtitle">正在安全地读取任务状态、修复动作和流水线验证结果。</p>
        <div id="heroMeta" class="hero-meta"></div>
        <div id="statusRow" class="status-row"><span class="status-pill tone-live">载入中</span></div>
      </section>

      <div id="errorBanner" class="error-banner" role="alert"></div>
      <div class="summary-grid" aria-label="修复摘要">
        <section class="metric"><div class="metric-label">当前状态</div><div id="metricStatus" class="metric-value skeleton"></div></section>
        <section class="metric"><div class="metric-label">验证流水线</div><div id="metricPipeline" class="metric-value skeleton"></div></section>
        <section class="metric"><div class="metric-label">代码覆盖率</div><div id="metricCoverage" class="metric-value skeleton"></div></section>
        <section class="metric"><div class="metric-label">修复动作</div><div id="metricActions" class="metric-value skeleton"></div></section>
      </div>

      <div class="content-grid">
        <div class="stack">
          <section class="panel" aria-labelledby="actionsTitle">
            <div class="panel-head"><div><h2 id="actionsTitle" class="panel-title">诊断与修复措施</h2><p class="panel-note">只展示经过结构化和脱敏的修复证据。</p></div></div>
            <div id="actionList" class="action-list"><div class="empty">等待诊断结果…</div></div>
          </section>
          <section class="panel" aria-labelledby="timelineTitle">
            <div class="panel-head"><div><h2 id="timelineTitle" class="panel-title">实时进度</h2><p class="panel-note">页面会自动接收最新阶段，无需手动刷新。</p></div></div>
            <ol id="timeline" class="timeline"><li class="empty">正在建立实时连接…</li></ol>
          </section>
        </div>
        <aside class="stack sticky" aria-label="任务信息">
          <section class="panel"><div class="panel-head"><div><h2 class="panel-title">验证结果</h2><p class="panel-note">以最新匹配 Commit 的流水线为准。</p></div></div><dl id="facts" class="fact-list"></dl></section>
        </aside>
      </div>
      <div id="liveAnnouncement" class="sr-only" aria-live="polite" aria-atomic="true"></div>
    </main>
  </div>

  <script>
  (() => {
    'use strict';
    const taskId = __TASK_ID__;
    const signature = __SIGNATURE__;
    const apiBase = `/api/repair-results/${encodeURIComponent(taskId)}`;
    const query = `sig=${encodeURIComponent(signature)}`;
    const categoryNames = { format: 'Format', clang: 'Clang', build: 'Build', unknown: 'Unknown' };
    const phaseNames = {
      pending: '等待开始', queued: '已进入队列', preparing: '准备工作区', diagnosing: '正在诊断',
      editing: '正在修改', committing: '正在提交', waiting_pipeline: '等待流水线', validating: '正在验证',
      triage_running: '正在诊断与修复', triage_waiting: '等待流水线', format_running: '正在修复格式',
      format_waiting: '等待格式流水线', terminal: '已结束'
    };
    const statusNames = { assigned: '等待执行', running: '修复中', waiting_pipeline: '等待流水线', completed: '已完成', failed: '修复失败', canceled: '已取消' };
    let snapshot = null;
    let eventSource = null;
    let pollingTimer = null;
    let lastEventId = '';
    let announcedPhase = '';

    const byId = id => document.getElementById(id);
    const create = (tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = String(text);
      return node;
    };
    const nonEmpty = value => value !== undefined && value !== null && value !== '';
    const shortSha = value => value ? String(value).slice(0, 12) : '未提供';
    const safeUrl = value => {
      try { const url = new URL(String(value)); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
      catch (_) { return ''; }
    };
    const formatTime = value => {
      if (!value) return '—';
      const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
      return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(date);
    };
    const projectBase = mrUrl => String(mrUrl || '').split('/-/merge_requests/')[0];
    const toneFor = data => {
      if (data.status === 'canceled') return 'warning';
      if (data.terminal && data.final_pipeline && data.final_pipeline.status === 'success') return 'success';
      if (data.terminal || data.status === 'failed') return 'danger';
      return 'live';
    };
    const outcomeLabel = data => {
      if (!data.terminal) return phaseNames[data.phase] || statusNames[data.status] || '修复中';
      if (data.status === 'canceled') return '修复已取消';
      return data.final_pipeline && data.final_pipeline.status === 'success' ? '修复成功' : '修复未完成';
    };
    const setText = (id, value) => { const node = byId(id); node.classList.remove('skeleton'); node.textContent = String(value); };
    const addChip = (parent, text, tone = '') => parent.appendChild(create('span', `chip${tone ? ` tone-${tone}` : ''}`, text));

    function renderHero(data) {
      const mr = data.mr || {};
      byId('pageTitle').textContent = mr.title || `${mr.project || '项目'} !${mr.iid || ''} CI 修复报告`;
      byId('heroSubtitle').textContent = `${mr.project || '未知项目'} · MR !${mr.iid || '—'} · ${mr.source_branch || '未提供分支'}`;
      const meta = byId('heroMeta'); meta.replaceChildren();
      const href = safeUrl(mr.url);
      if (href) {
        const link = create('a', 'hero-link', '在 GitLab 中查看 MR');
        link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; meta.appendChild(link);
      }
      meta.appendChild(create('span', 'mono', `任务 ${String(data.task_id || taskId).slice(0, 12)}`));
      const row = byId('statusRow'); row.replaceChildren();
      row.appendChild(create('span', `status-pill tone-${toneFor(data)}`, outcomeLabel(data)));
      (data.selected_categories || []).forEach(category => addChip(row, categoryNames[category] || category));
    }

    function renderMetrics(data) {
      const finalPipeline = data.final_pipeline || {};
      const evidencePipeline = data.evidence_pipeline || {};
      setText('metricStatus', outcomeLabel(data));
      setText('metricPipeline', finalPipeline.id ? `#${finalPipeline.id}` : evidencePipeline.id ? `证据 #${evidencePipeline.id}` : '等待生成');
      const coveragePipeline = finalPipeline.id ? finalPipeline : evidencePipeline;
      setText('metricCoverage', nonEmpty(coveragePipeline.coverage) ? `${coveragePipeline.coverage}%` : '未提供');
      setText('metricActions', `${(data.actions || []).length} 项`);
    }

    function actionTone(status) {
      if (status === 'verified') return 'success';
      if (status === 'failed' || status === 'no_changes') return 'danger';
      if (status === 'committed') return 'warning';
      return 'live';
    }

    function renderActions(actions) {
      const root = byId('actionList'); root.replaceChildren();
      if (!actions || !actions.length) { root.appendChild(create('div', 'empty', '正在收集诊断证据和修复动作…')); return; }
      actions.forEach((action, index) => {
        const card = create('article', 'action-card');
        const head = create('div', 'action-head');
        const categoryTitle = (action.categories || []).map(category => categoryNames[category] || category).join(' / ');
        const heading = create('h3', 'action-title', categoryTitle ? `${categoryTitle} 修复` : `修复动作 ${index + 1}`);
        head.append(heading, create('span', `status-pill tone-${actionTone(action.status)}`, phaseNames[action.status] || ({ verified: '已验证', failed: '未通过', no_changes: '无改动', committed: '已提交' }[action.status] || '处理中')));
        card.appendChild(head);
        const chips = create('div', 'chip-row');
        (action.categories || []).forEach(category => addChip(chips, categoryNames[category] || category));
        (action.job_names || []).forEach(job => addChip(chips, job));
        card.appendChild(chips);
        if (action.root_cause) card.appendChild(create('p', 'root-cause', action.root_cause));
        if (action.measures && action.measures.length) {
          const list = create('ul', 'measure-list');
          action.measures.forEach(measure => list.appendChild(create('li', '', measure))); card.appendChild(list);
        }
        const details = document.createElement('details');
        const detailTitle = document.createElement('summary');
        detailTitle.textContent = `查看证据与修改文件 (${(action.changed_files || []).length})`;
        const body = create('div', 'detail-body');
        if (action.evidence) body.appendChild(create('p', '', action.evidence));
        if (action.failure_reason) body.appendChild(create('p', 'tone-danger', action.failure_reason));
        if (action.changed_files && action.changed_files.length) {
          const files = create('ul', 'file-list');
          action.changed_files.forEach(file => files.appendChild(create('li', 'file-item', file))); body.appendChild(files);
        } else body.appendChild(create('p', '', '本次动作没有记录到文件改动。'));
        details.append(detailTitle, body); card.appendChild(details); root.appendChild(card);
      });
    }

    function renderTimeline(events) {
      const root = byId('timeline'); root.replaceChildren();
      if (!events || !events.length) { root.appendChild(create('li', 'empty', '等待第一条实时进度…')); return; }
      events.slice(-80).forEach(event => {
        const item = create('li', 'timeline-item');
        item.appendChild(create('span', 'timeline-dot'));
        item.appendChild(create('div', 'timeline-summary', event.summary || phaseNames[event.phase] || '状态更新'));
        item.appendChild(create('div', 'timeline-meta', `${phaseNames[event.phase] || event.phase || '进度'} · ${formatTime(event.occurred_at)}`));
        root.appendChild(item);
        if (event.event_id) lastEventId = event.event_id;
      });
    }

    function addFact(root, label, value, href = '') {
      const box = create('div', 'fact'); box.appendChild(create('dt', '', label));
      const dd = create('dd');
      if (href) { const link = create('a', 'hero-link mono', value); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; dd.appendChild(link); }
      else dd.textContent = String(value || '—');
      box.appendChild(dd); root.appendChild(box);
    }

    function renderFacts(data) {
      const root = byId('facts'); root.replaceChildren();
      const mr = data.mr || {}; const source = data.source_pipeline || {}; const finalResult = data.final_pipeline || {};
      const evidence = data.evidence_pipeline || {};
      const base = projectBase(mr.url);
      addFact(root, '原始流水线', source.id ? `#${source.id}` : '—', source.id && base ? `${base}/-/pipelines/${source.id}` : '');
      addFact(root, '原始 Commit', shortSha(source.sha), source.sha && base ? `${base}/-/commit/${encodeURIComponent(source.sha)}` : '');
      if (finalResult.id) {
        addFact(root, '结果流水线', `#${finalResult.id}`, base ? `${base}/-/pipelines/${finalResult.id}` : '');
        addFact(root, '修复 Commit', shortSha(finalResult.sha), finalResult.sha && base ? `${base}/-/commit/${encodeURIComponent(finalResult.sha)}` : '');
      } else {
        addFact(root, '当前失败流水线（证据）', evidence.id ? `#${evidence.id}` : '—', evidence.id && base ? `${base}/-/pipelines/${evidence.id}` : '');
        addFact(root, '证据 Commit', shortSha(evidence.sha), evidence.sha && base ? `${base}/-/commit/${encodeURIComponent(evidence.sha)}` : '');
      }
      addFact(root, '开始时间', formatTime(data.created_at)); addFact(root, '最后更新', formatTime(data.updated_at));
    }

    function render(data, announce = true) {
      snapshot = data;
      renderHero(data); renderMetrics(data); renderActions(data.actions || []); renderTimeline(data.progress || []); renderFacts(data);
      const banner = byId('errorBanner');
      if (data.error) { banner.textContent = data.error; banner.classList.add('visible'); }
      else { banner.textContent = ''; banner.classList.remove('visible'); }
      if (announce && data.phase !== announcedPhase) {
        announcedPhase = data.phase; byId('liveAnnouncement').textContent = `修复状态更新：${outcomeLabel(data)}`;
      }
      if (data.terminal) stopLiveUpdates();
    }

    function setConnection(state, text) {
      const node = byId('connection'); node.className = `connection ${state}`; byId('connectionText').textContent = text;
    }

    async function fetchSnapshot() {
      const response = await fetch(`${apiBase}?${query}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error(response.status === 404 ? '修复详情不存在或链接已失效。' : '暂时无法读取修复详情。');
      render(await response.json());
    }

    function startPollingFallback() {
      if (pollingTimer || (snapshot && snapshot.terminal)) return;
      setConnection('offline', '实时连接中断，自动刷新中');
      pollingTimer = setInterval(() => fetchSnapshot().catch(() => {}), 3000);
    }

    function stopLiveUpdates() {
      if (eventSource) { eventSource.close(); eventSource = null; }
      if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
      const succeeded = snapshot && snapshot.terminal && snapshot.final_pipeline && snapshot.final_pipeline.status === 'success';
      setConnection(snapshot && snapshot.terminal ? (succeeded ? 'settled' : 'offline') : 'offline', snapshot && snapshot.terminal ? '任务已结束' : '连接已停止');
    }

    function mergeProgress(event) {
      if (!snapshot) return;
      const events = snapshot.progress || [];
      if (event.event_id && events.some(item => item.event_id === event.event_id)) return;
      snapshot.progress = [...events, event]; snapshot.updated_at = event.occurred_at || snapshot.updated_at;
      render(snapshot);
    }

    function startLiveUpdates() {
      if (!window.EventSource || (snapshot && snapshot.terminal)) { startPollingFallback(); return; }
      const after = lastEventId ? `&after=${encodeURIComponent(lastEventId)}` : '';
      eventSource = new EventSource(`${apiBase}/events?${query}${after}`);
      eventSource.onopen = () => {
        setConnection('live', '实时更新中');
        if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
      };
      eventSource.addEventListener('progress', message => {
        try { mergeProgress(JSON.parse(message.data)); } catch (_) {}
      });
      eventSource.addEventListener('snapshot', message => {
        try { render(JSON.parse(message.data)); } catch (_) {}
      });
      eventSource.addEventListener('stream_error', () => startPollingFallback());
      eventSource.onerror = () => { if (eventSource) { eventSource.close(); eventSource = null; } startPollingFallback(); };
    }

    fetchSnapshot().then(() => { if (!snapshot.terminal) startLiveUpdates(); }).catch(error => {
      setConnection('offline', '加载失败');
      const banner = byId('errorBanner'); banner.textContent = error.message; banner.classList.add('visible');
      byId('pageTitle').textContent = '无法加载 CI 修复详情';
    });
  })();
  </script>
</body>
</html>'''
    return template.replace("__TASK_ID__", json.dumps(task_id)).replace("__SIGNATURE__", json.dumps(signature))


def _sse_message(event: str, payload: dict[str, Any], event_id: str = "") -> str:
    fields = [f"id: {event_id}"] if event_id else []
    fields.extend((f"event: {event}", f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"))
    return "\n".join(fields) + "\n\n"


async def _event_stream(request: Request, task_id: str, broker, after_id: str, initial: dict[str, Any]):
    cursor = after_id or "0-0"
    try:
        existing = await broker.get_repair_progress(task_id, after_id=cursor)
        for event in existing:
            cursor = event.event_id or cursor
            yield _sse_message("progress", event.to_dict(), cursor)
        if _snapshot_is_settled(initial):
            yield _sse_message("snapshot", initial)
            return
        while not await request.is_disconnected():
            events = await broker.read_repair_progress(
                task_id,
                after_id=cursor,
                block_ms=repair_details_heartbeat_seconds() * 1000,
            )
            if not events:
                yield ": heartbeat\n\n"
            for event in events:
                cursor = event.event_id or cursor
                yield _sse_message("progress", event.to_dict(), cursor)
            snapshot = await load_repair_result_snapshot(task_id, broker)
            if snapshot is not None and _snapshot_is_settled(snapshot):
                yield _sse_message("snapshot", snapshot)
                return
    except asyncio.CancelledError:
        return
    except Exception as error:
        get_logger().warning(f"Repair result stream ended task_id={task_id}: {error}")
        yield _sse_message("stream_error", {"message": "实时连接暂时不可用，请使用页面自动刷新。"})


@router.get("/repair-results/{task_id}", response_class=HTMLResponse)
async def repair_result_page(task_id: str, sig: str = Query(""), embed: bool = Query(False)):
    _verify_or_404(task_id, sig)
    snapshot = await load_repair_result_snapshot(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(_repair_result_html(task_id, sig, embedded=embed), headers=_private_headers())


@router.get("/api/repair-results/{task_id}")
async def repair_result_snapshot(task_id: str, sig: str = Query("")):
    _verify_or_404(task_id, sig)
    snapshot = await load_repair_result_snapshot(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(snapshot, headers=_private_headers())


@router.get("/api/repair-results/{task_id}/events")
async def repair_result_events(request: Request, task_id: str, sig: str = Query(""), after: str = Query("")):
    _verify_or_404(task_id, sig)
    broker = _get_broker()
    snapshot = await load_repair_result_snapshot(task_id, broker)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Not found")
    if broker is None or snapshot.get("source") == "durable":
        async def terminal_event():
            yield _sse_message("snapshot", snapshot)

        stream = terminal_event()
    else:
        last_event_id = after or request.headers.get("last-event-id", "")
        stream = _event_stream(request, task_id, broker, last_event_id, snapshot)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={**_private_headers(), "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
