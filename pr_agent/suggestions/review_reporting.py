"""Selection and presentation rules for automatic MR-creation suggestion reviews."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from pr_agent.config_loader import get_settings
from pr_agent.feedback.timez import now_cn, to_cn, to_cn_display
from pr_agent.log import get_logger
from pr_agent.storage.sqlite import connect_sqlite
from pr_agent.suggestions.review_tracking import (
    get_creation_tracking_boundary,
    init_review_tracking,
    list_review_events,
)
from pr_agent.suggestions.store import (
    get_db_path,
    get_filtered_suggestions,
    get_filtered_suggestions_for_run,
    get_published_suggestions,
    get_published_suggestions_for_run,
    get_suggestion_threads,
    get_suggestion_threads_for_run,
    migrate_schema,
)

CREATION_TRIGGER = "auto_mr_create"
HISTORICAL_CREATION_TRIGGER = "historical_auto_mr_create"
CREATION_SCOPE = "mr_creation"
ABNORMAL_REASON_LABELS = {
    "creation_webhook_missing": "未收到创建事件",
    "recovery_window_expired": "发现时已超过补审期限",
    "queue_admission_failed": "自动任务入队失败",
    "queue_timeout": "自动任务排队超时",
    "review_startup_failed": "自动审查启动失败",
    "worker_lost": "Worker 丢失",
    "model_call_failed": "模型调用失败",
    "suggestion_parse_failed": "建议解析失败",
    "review_execution_failed": "建议审查执行失败",
    "gitlab_publish_failed": "GitLab 发布失败",
}
SKIP_REASON_LABELS = {
    "missing_object_attributes": "事件缺少 MR 信息",
    "unsupported_action": "MR 动作不触发审查",
    "ignored_repository": "仓库规则跳过",
    "ignored_author": "作者规则跳过",
    "ignored_source_branch": "源分支规则跳过",
    "ignored_target_branch": "目标分支规则跳过",
    "ignored_label": "标签规则跳过",
    "ignored_title": "标题规则跳过",
    "auto_feedback_disabled": "自动审查已关闭",
    "invalid_event": "MR 事件无效",
}
UNPUBLISHED_REASONS = {
    "secondary_review_filtered",
    "publishing_skipped",
    "not_selected_for_inline",
    "unknown_unpublished",
}


def _mr_time(mr: dict):
    return to_cn(mr.get("created_at") or mr.get("updated_at") or mr.get("last_synced_at"))


def _is_reliable_mr(mr: dict, reliable_started_at: str | None) -> bool:
    mr_time = _mr_time(mr)
    boundary = to_cn(reliable_started_at)
    return bool(mr_time and boundary and mr_time >= boundary)


def include_in_operational_summary(mr: dict, status: str, reliable_started_at: str | None) -> bool:
    """Return whether one MR belongs in the operational review dashboard."""
    if status != "not_triggered" or not reliable_started_at:
        return True
    mr_time = _mr_time(mr)
    boundary = to_cn(reliable_started_at)
    if not mr_time or not boundary:
        return True
    return mr_time >= boundary


def select_creation_review_run(
    mr: dict,
    runs: list[dict],
    reliable_started_at: str | None,
) -> dict | None:
    """Select the one automatic creation run used by summary and detail views."""
    candidates = [
        run for run in runs
        if str(run.get("trigger") or "") in {CREATION_TRIGGER, HISTORICAL_CREATION_TRIGGER}
    ]
    if _is_reliable_mr(mr, reliable_started_at):
        candidates = [run for run in candidates if str(run.get("review_scope") or "") == CREATION_SCOPE]
    if not candidates:
        return None
    return max(candidates, key=lambda run: str(run.get("started_at") or ""))


def historical_evidence_run(
    mr: dict,
    evidence: dict,
    reliable_started_at: str | None,
) -> dict | None:
    """Build a reporting-only historical run from existing suggestion evidence."""
    if _is_reliable_mr(mr, reliable_started_at):
        return None
    published = int(evidence.get("published") or 0)
    filtered = int(evidence.get("filtered") or 0)
    failed = int(evidence.get("failed") or 0)
    generated = published + filtered + failed
    if generated <= 0:
        return None
    stage = "published" if published else "publish_failed" if failed else "validated"
    return {
        "run_id": "", "trigger": "historical_suggestion_evidence", "review_scope": "legacy",
        "status": "completed", "stage": stage, "generated_count": generated,
        "kept_count": published + failed, "filtered_count": filtered,
        "inline_selected_count": published + failed, "inline_skipped_count": 0,
        "inline_published_count": published, "inline_fallback_count": 0, "inline_failed_count": failed,
        "commit_sha": str(mr.get("commit_sha") or ""),
    }


def derive_abnormal_reason(
    mr: dict,
    run: dict | None,
    status: str,
    *,
    reliable_started_at: str | None = None,
) -> dict | None:
    """Return a stable short reason for one abnormal primary result."""
    if status == "skipped":
        code = str((run or {}).get("error_code") or "invalid_event")
        return {
            "code": code,
            "label": SKIP_REASON_LABELS.get(code, "规则跳过"),
            "detail": "",
        }
    if status not in {"not_triggered", "startup_failed", "execution_failed", "publish_failed"}:
        return None
    if reliable_started_at and not _is_reliable_mr(mr, reliable_started_at):
        return None
    stored = str((mr or {}).get("creation_reason_code") or (run or {}).get("unpublished_reason") or "")
    error_code = str((run or {}).get("error_code") or "").lower()
    error_message = str((run or {}).get("error_message") or "").lower()
    combined = f"{error_code} {error_message}"
    if stored not in ABNORMAL_REASON_LABELS:
        if status == "not_triggered":
            if str((mr or {}).get("creation_recovery_state") or "") == "outside_window":
                stored = "recovery_window_expired"
            elif (
                str((mr or {}).get("discovered_by") or "") in {"incremental_sync", "reconcile"}
                and not str((mr or {}).get("webhook_received_at") or "")
            ):
                stored = "creation_webhook_missing"
            else:
                return None
        elif status == "startup_failed":
            if "worker lost" in combined:
                stored = "worker_lost"
            elif "timeout" in combined:
                stored = "queue_timeout"
            else:
                stored = "review_startup_failed"
        elif status == "publish_failed":
            stored = "gitlab_publish_failed"
        elif any(value in combined for value in ("openai", "model", "llm", "api")):
            stored = "model_call_failed"
        elif any(value in combined for value in ("parse", "json", "schema")):
            stored = "suggestion_parse_failed"
        else:
            stored = "review_execution_failed"
    return {"code": stored, "label": ABNORMAL_REASON_LABELS[stored], "detail": ""}


def derive_primary_status(
    mr: dict,
    run: dict | None,
    *,
    grace_seconds: int,
    run_timeout_seconds: int,
) -> str:
    """Return the mutually exclusive creation-review result used by the UI."""
    if not run:
        discovered = _mr_time(mr)
        if discovered and now_cn() - discovered < timedelta(seconds=max(0, grace_seconds)):
            return "waiting"
        return "not_triggered"

    status = str(run.get("status") or "")
    stage = str(run.get("stage") or "")
    improve_started = bool(run.get("improve_started_at"))
    if status == "skipped":
        return "skipped"
    if status == "running":
        updated_at = to_cn(run.get("updated_at") or run.get("started_at"))
        if not updated_at or now_cn() - updated_at < timedelta(seconds=max(1, run_timeout_seconds)):
            return "waiting"
        return "execution_failed" if improve_started else "startup_failed"

    published = int(run.get("inline_published_count") or 0)
    fallback = int(run.get("inline_fallback_count") or 0)
    failed = int(run.get("inline_failed_count") or 0)
    if published > 0:
        return "published"
    if fallback > 0 and failed == 0:
        return "fallback_published"
    if failed > 0 or stage == "publish_failed":
        return "publish_failed"
    if status == "failed":
        return "execution_failed" if improve_started else "startup_failed"
    if int(run.get("generated_count") or 0) == 0:
        return "no_suggestions"
    return "unpublished"


def derive_unpublished_reason(run: dict) -> str | None:
    """Return the stable machine reason for an unpublished positive result."""
    stored = str(run.get("unpublished_reason") or "")
    if stored in UNPUBLISHED_REASONS:
        return stored
    generated = int(run.get("generated_count") or 0)
    filtered = int(run.get("filtered_count") or 0)
    skipped = int(run.get("inline_skipped_count") or 0)
    selected = int(run.get("inline_selected_count") or 0)
    published = int(run.get("inline_published_count") or 0)
    fallback = int(run.get("inline_fallback_count") or 0)
    failed = int(run.get("inline_failed_count") or 0)
    if generated <= 0 or published > 0 or fallback > 0 or failed > 0:
        return None
    if filtered >= generated:
        return "secondary_review_filtered"
    if skipped > 0:
        return "publishing_skipped"
    if selected == 0:
        return "not_selected_for_inline"
    return "unknown_unpublished"


def _normalise_suggestion(row: dict, disposition: str, mr_url: str) -> dict:
    note_id = str(row.get("gitlab_note_id") or "")
    discussion_url = f"{mr_url}#note_{note_id}" if mr_url and note_id else ""
    return {
        "file_path": str(row.get("file_path") or ""),
        "line_start": row.get("line_start"),
        "line_end": row.get("line_end"),
        "label": str(row.get("label") or ""),
        "severity": str(row.get("severity") or ""),
        "score": row.get("score"),
        "summary": str(row.get("one_sentence_summary") or ""),
        "suggestion": str(row.get("suggestion_content") or ""),
        "existing_code": str(row.get("existing_code") or ""),
        "improved_code": str(row.get("improved_code") or ""),
        "disposition": disposition,
        "reason": str(row.get("skip_reason") or ""),
        "created_at": to_cn_display(row.get("created_at")),
        "discussion_url": discussion_url,
    }


def _unavailable_detail(project: str, mr_iid: str) -> dict:
    return {
        "detail_state": "unavailable",
        "mr": {"project": project, "mr_iid": mr_iid},
        "counts": {
            "generated": 0, "filtered": 0, "skipped": 0,
            "published": 0, "fallback_published": 0, "failed": 0,
        },
        "timeline": [], "filtered_suggestions": [], "published_suggestions": [], "errors": [],
    }


def collect_creation_review_detail(
    project: str,
    mr_iid: str,
    path: str | None = None,
    gitlab_url: str | None = None,
) -> dict:
    """Return detail for the same automatic creation run selected by the summary."""
    path = path or get_db_path()
    project = str(project or "")
    mr_iid = str(mr_iid or "")
    if not project or not mr_iid:
        return _unavailable_detail(project, mr_iid)
    try:
        migrate_schema(path)
        init_review_tracking(path)
        conn = connect_sqlite(path)
        conn.row_factory = sqlite3.Row
        try:
            mr_row = conn.execute(
                "SELECT * FROM mr_inventory WHERE project_path = ? AND mr_iid = ?",
                (project, mr_iid),
            ).fetchone()
            if not mr_row:
                return _unavailable_detail(project, mr_iid)
            mr = dict(mr_row)
            runs = [dict(row) for row in conn.execute(
                "SELECT * FROM suggestion_review_runs WHERE project_path = ? AND mr_iid = ? ORDER BY started_at DESC",
                (project, mr_iid),
            ).fetchall()]
        finally:
            conn.close()

        reliable_started_at = get_creation_tracking_boundary(path)
        run = select_creation_review_run(mr, runs, reliable_started_at)
        legacy_filtered = legacy_published = legacy_threads = []
        if not run and not _is_reliable_mr(mr, reliable_started_at):
            legacy_filtered = get_filtered_suggestions(project, mr_iid, path)
            legacy_published = get_published_suggestions(project, mr_iid, path)
            legacy_threads = get_suggestion_threads(project, mr_iid, path)
            run = historical_evidence_run(mr, {
                "filtered": len(legacy_filtered),
                "published": len(legacy_published),
                "failed": sum(
                    str(row.get("publish_status") or row.get("state") or "") == "failed"
                    for row in legacy_threads
                ),
            }, reliable_started_at)
        has_creation_classification = bool(
            mr.get("creation_reason_code") or mr.get("creation_recovery_state")
        )
        if not run and not has_creation_classification:
            return _unavailable_detail(project, mr_iid)
        run_id = str((run or {}).get("run_id") or "")
        mr_url = str(mr.get("mr_url") or (run or {}).get("mr_url") or "")
        if not mr_url and gitlab_url:
            mr_url = f"{str(gitlab_url).rstrip('/')}/{project}/-/merge_requests/{mr_iid}"
        filtered_rows = get_filtered_suggestions_for_run(run_id, path) if run_id else legacy_filtered
        published_rows = get_published_suggestions_for_run(run_id, path) if run_id else legacy_published
        filtered = [
            _normalise_suggestion(row, "filtered", mr_url)
            for row in filtered_rows
        ]
        published = [
            _normalise_suggestion(row, "published", mr_url)
            for row in published_rows
        ]
        threads = get_suggestion_threads_for_run(run_id, path) if run_id else legacy_threads
        published.extend(
            _normalise_suggestion(row, "fallback_published", mr_url)
            for row in threads
            if str(row.get("publish_status") or row.get("state") or "") == "fallback_published"
        )
        timeline = list_review_events(run_id, path) if run_id else []
        errors = []
        for event in timeline:
            if event.get("status") == "failed" or event.get("error_code") or event.get("error_message"):
                errors.append({
                    "stage": str(event.get("stage") or ""),
                    "error_code": str(event.get("error_code") or ""),
                    "message": str(event.get("error_message") or "")[:1000],
                    "created_at": to_cn_display(event.get("created_at")),
                })
        for row in threads:
            status = str(row.get("publish_status") or row.get("state") or "")
            if status not in {"failed", "skipped"}:
                continue
            errors.append({
                "stage": "publishing", "error_code": status,
                "message": str(row.get("skip_reason") or "Publication did not complete")[:1000],
                "created_at": to_cn_display(row.get("created_at")),
                "file_path": str(row.get("file_path") or ""),
            })
        deduplicated_errors = []
        seen = set()
        for error in errors:
            identity = (error["stage"], error["error_code"], error["message"], error["created_at"])
            if identity not in seen:
                seen.add(identity)
                deduplicated_errors.append(error)
        grace_seconds = int(get_settings().get("suggestion_review_dashboard.not_triggered_after_seconds", 900))
        timeout_seconds = int(get_settings().get("suggestion_review_dashboard.run_timeout_seconds", 7200))
        status = derive_primary_status(
            mr, run, grace_seconds=grace_seconds, run_timeout_seconds=timeout_seconds,
        )
        abnormal_reason = derive_abnormal_reason(
            mr, run, status, reliable_started_at=reliable_started_at,
        )
        counts = {
            "generated": int((run or {}).get("generated_count") or 0),
            "filtered": int((run or {}).get("filtered_count") or 0),
            "skipped": int((run or {}).get("inline_skipped_count") or 0),
            "published": int((run or {}).get("inline_published_count") or 0),
            "fallback_published": int((run or {}).get("inline_fallback_count") or 0),
            "failed": int((run or {}).get("inline_failed_count") or 0),
        }
        is_empty = not filtered and not published and not deduplicated_errors and counts["generated"] == 0
        return {
            "detail_state": "available_empty" if is_empty else "available",
            "mr": {
                "project": project, "mr_iid": mr_iid, "title": str(mr.get("title") or ""),
                "author": str(mr.get("author") or ""), "created_at": to_cn_display(mr.get("created_at")),
                "link": mr_url, "run_id": run_id, "task_id": str((run or {}).get("task_id") or ""),
                "initial_commit_sha": str((run or {}).get("commit_sha") or mr.get("commit_sha") or ""),
                "status": status,
                "has_secondary_filter": counts["filtered"] > 0,
                "unpublished_reason": derive_unpublished_reason(run or {}) if status == "unpublished" else None,
                "reason_code": str((abnormal_reason or {}).get("code") or ""),
                "reason_label": str((abnormal_reason or {}).get("label") or ""),
                "recovery_source": (
                    "sync" if str(mr.get("creation_recovery_state") or "") == "recovered" else ""
                ),
            },
            "counts": counts, "timeline": timeline, "filtered_suggestions": filtered,
            "published_suggestions": published, "errors": deduplicated_errors,
        }
    except Exception as exc:
        get_logger().error(f"Failed to collect creation review detail: {exc}")
        return _unavailable_detail(project, mr_iid)
