"""Live, server-rendered dashboards for review + inline-suggestion feedback.

Unlike ``pr_agent.feedback.report`` (a CLI that snapshots the SQLite feedback
DB into a static HTML file you have to `sync` + regenerate by hand), these
routes are mounted on the already-running GitLab webhook FastAPI app
(see ``pr_agent/servers/gitlab_webhook.py``) and query the local SQLite file
on every request -- so refreshing the page always shows the latest data,
with no separate process, port, or manual sync step.

Routes:
    GET /dashboard/feedback   -- review-score dashboard (HTML shell)
    GET /dashboard/inline     -- inline-suggestion adoption dashboard (HTML shell)
    GET /api/feedback/summary -- JSON data backing the feedback dashboard
    GET /api/inline/summary   -- JSON data backing the inline dashboard

Never raises: any query failure results in a 200 response with an empty/zero
payload rather than a 500, so a dashboard hiccup can't affect the webhook
routes that share this FastAPI app.
"""

# ruff: noqa: E501 -- Dashboard HTML/CSS/JS is intentionally embedded in this dependency-free route module.

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from pr_agent.config_loader import get_settings
from pr_agent.feedback.report import DEFAULT_GITLAB_URL
from pr_agent.feedback.store import get_db_path as get_feedback_db_path
from pr_agent.feedback.store import init_db as init_feedback_db
from pr_agent.feedback.timez import now_cn, to_cn, to_cn_display
from pr_agent.log import get_logger
from pr_agent.servers.ci_failure_dashboard import (
    CiFailureAnnotationRequest,
    annotate_ci_failure,
    collect_ci_failure_detail,
    collect_ci_failure_summary,
    render_ci_failure_dashboard,
)
from pr_agent.servers.suggestion_review_dashboard import render_suggestion_review_dashboard
from pr_agent.suggestions.review_alerts import active_review_alerts_payload
from pr_agent.suggestions.review_reporting import (
    collect_creation_review_detail,
    derive_abnormal_reason,
    derive_primary_status,
    derive_unpublished_reason,
    historical_evidence_run,
    include_in_operational_summary,
    select_creation_review_run,
)
from pr_agent.suggestions.review_tracking import (
    get_creation_tracking_boundary,
    get_sync_metrics,
    get_sync_state,
    init_review_tracking,
)
from pr_agent.suggestions.store import get_db_path as get_inline_db_path
from pr_agent.suggestions.store import migrate_schema as migrate_inline_schema
from pr_agent.triage.repair_details import repair_details_enabled, sign_repair_details_task
from pr_agent.triage.store import init_triage_table

router = APIRouter()
SUGGESTION_REVIEW_PAGE_SIZE = 15
_TRIAGE_RECENT_PAGE_SIZE = 15
SUGGESTION_REVIEW_ALERT_LABELS = {
    "model_failures": "模型调用连续失败",
    "startup_retry_exhausted": "启动重试已耗尽",
    "publish_fallbacks": "行内发布频繁降级",
}
SUGGESTION_REVIEW_TABLES = {
    "review_mrs", "filter_projects", "filter_mrs", "filtered_suggestions",
}


class MemoryStatusChangeRequest(BaseModel):
    """Validated operator reason for one repair-memory state change."""

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> str:
        reason = str(value or "").strip()
        if not 1 <= len(reason) <= 500:
            raise ValueError("reason must contain 1 to 500 characters after trimming")
        return reason


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# Data collection (queried fresh on every request -- no caching, no snapshot)
# --------------------------------------------------------------------------- #

def collect_feedback_summary(days: Optional[int] = 30, project: Optional[str] = None) -> dict:
    """Return the review-feedback dashboard payload as plain JSON-able data."""
    try:
        db_path = get_feedback_db_path()
        init_feedback_db(db_path)
        conn = _connect(db_path)
    except Exception as e:
        get_logger().error(f"dashboard: failed to open feedback db: {e}")
        return {"total": 0, "avg": 0, "median": None, "dist_labels": [], "dist_values": [],
                "week_labels": [], "week_values": [], "project_rows": [], "all_rows": []}

    try:
        clauses, params = [], []
        if days:
            clauses.append("created_at >= ?")
            params.append((now_cn() - timedelta(days=days)).isoformat())
        if project:
            clauses.append("project = ?")
            params.append(project)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        overview = conn.execute(
            f"SELECT COUNT(*) AS c, AVG(score) AS a FROM review_feedback {where}", params
        ).fetchone()
        total = overview["c"] or 0
        if not total:
            return {"total": 0, "avg": 0, "median": None, "dist_labels": ["1", "2", "3", "4", "5"],
                    "dist_values": [0, 0, 0, 0, 0], "week_labels": [], "week_values": [],
                    "project_rows": [], "all_rows": []}

        median_row = conn.execute(
            f"SELECT score FROM review_feedback {where} ORDER BY score LIMIT 1 OFFSET ?",
            (*params, total // 2),
        ).fetchone()
        median = median_row["score"] if median_row else None
        avg = overview["a"] or 0

        dist_rows = conn.execute(
            f"SELECT score, COUNT(*) AS c FROM review_feedback {where} GROUP BY score ORDER BY score",
            params,
        ).fetchall()
        dist_map = {row["score"]: row["c"] for row in dist_rows}
        dist_labels = ["1", "2", "3", "4", "5"]
        dist_values = [dist_map.get(score, 0) for score in range(1, 6)]

        week_rows = conn.execute(
            f"""SELECT strftime('%Y-W%W', created_at) AS week, AVG(score) AS a, COUNT(*) AS c
                FROM review_feedback {where} GROUP BY week ORDER BY week DESC LIMIT 8""",
            params,
        ).fetchall()
        week_rows = list(reversed(week_rows))

        project_rows = conn.execute(
            f"""SELECT project, COUNT(*) AS c, ROUND(AVG(score), 2) AS a, MIN(score) AS mn, MAX(score) AS mx
                FROM review_feedback {where} GROUP BY project ORDER BY c DESC, a DESC""",
            params,
        ).fetchall()

        all_rows = conn.execute(
            f"""SELECT id, created_at, reviewer_user, score, comment, pr_url, project,
                       mr_iid, mr_author, review_id, commit_sha, model, source
                FROM review_feedback {where} ORDER BY created_at DESC""",
            params,
        ).fetchall()

        return {
            "total": total,
            "avg": round(avg, 2),
            "median": median,
            "dist_labels": dist_labels,
            "dist_values": dist_values,
            "week_labels": [r["week"] for r in week_rows],
            "week_values": [round(r["a"], 2) for r in week_rows],
            "project_rows": [
                {"project": r["project"] or "(unknown)", "count": r["c"], "avg": r["a"],
                 "min": r["mn"], "max": r["mx"]}
                for r in project_rows
            ],
            "all_rows": [
                {
                    "id": r["id"],
                    "created_at": to_cn_display(r["created_at"]) if r["created_at"] else "",
                    "reviewer_user": r["reviewer_user"] or "",
                    "score": r["score"],
                    "comment": (r["comment"] or "").replace("\n", " ").strip(),
                    "pr_url": r["pr_url"] or "",
                    "project": r["project"] or "",
                    "mr_iid": r["mr_iid"] or "",
                }
                for r in all_rows
            ],
        }
    except Exception as e:
        get_logger().error(f"dashboard: feedback summary query failed: {e}")
        return {"total": 0, "avg": 0, "median": None, "dist_labels": [], "dist_values": [],
                "week_labels": [], "week_values": [], "project_rows": [], "all_rows": []}
    finally:
        conn.close()


def collect_inline_summary(gitlab_url: Optional[str] = None) -> dict:
    """Return the inline-suggestion dashboard payload as plain JSON-able data."""
    gitlab_url = (gitlab_url or DEFAULT_GITLAB_URL).rstrip("/")
    db_path = get_inline_db_path()
    try:
        migrate_inline_schema(path=db_path)
        conn = _connect(db_path)
    except Exception as e:
        get_logger().error(f"dashboard: failed to open inline suggestions db: {e}")
        return {"pub_total": 0, "app_total": 0, "overall_pct": 0, "dist_labels": [],
                "dist_values": [], "week_labels": [], "week_values": [], "project_rows": [],
                "mr_rows": [], "fb_rows": []}

    try:
        pub_total = conn.execute("SELECT COUNT(*) FROM published_suggestions").fetchone()[0]
        app_total = conn.execute(
            "SELECT COUNT(*) FROM published_suggestions WHERE applied_at IS NOT NULL"
        ).fetchone()[0]

        proj_rows = conn.execute(
            "SELECT project, COUNT(*) AS pub,"
            " SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS app"
            " FROM published_suggestions GROUP BY project ORDER BY pub DESC"
        ).fetchall()

        week_rows = conn.execute(
            "SELECT strftime('%Y-W%W', created_at) AS week, COUNT(*) AS pub,"
            " SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS app"
            " FROM published_suggestions GROUP BY week ORDER BY week DESC LIMIT 8"
        ).fetchall()
        week_rows = list(reversed(week_rows))

        try:
            mr_rows = conn.execute(
                "SELECT ps.project AS project, ps.mr_iid AS mr_iid,"
                " COUNT(*) AS pub,"
                " SUM(CASE WHEN ps.applied_at IS NOT NULL THEN 1 ELSE 0 END) AS app,"
                " MAX(ps.created_at) AS last_at,"
                " MAX(ps.mr_url) AS mr_url,"
                " COALESCE("
                "   (SELECT ps2.mr_author FROM published_suggestions ps2"
                "    WHERE ps2.project = ps.project AND ps2.mr_iid = ps.mr_iid"
                "    AND ps2.mr_author IS NOT NULL AND ps2.mr_author != '' LIMIT 1),"
                "   (SELECT rf.mr_author FROM review_feedback rf"
                "    WHERE rf.project = ps.project AND rf.mr_iid = ps.mr_iid"
                "    AND rf.mr_author IS NOT NULL AND rf.mr_author != ''"
                "    ORDER BY rf.id DESC LIMIT 1)"
                " ) AS owner"
                " FROM published_suggestions ps GROUP BY ps.project, ps.mr_iid"
                " ORDER BY last_at DESC LIMIT 1000"
            ).fetchall()
        except Exception:
            mr_rows = conn.execute(
                "SELECT project, mr_iid, COUNT(*) AS pub,"
                " SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS app,"
                " MAX(created_at) AS last_at, MAX(mr_url) AS mr_url, NULL AS owner"
                " FROM published_suggestions GROUP BY project, mr_iid"
                " ORDER BY last_at DESC LIMIT 1000"
            ).fetchall()

        try:
            fb_rows = conn.execute(
                "SELECT feedback_user, project, mr_iid, comment, created_at"
                " FROM inline_suggestion_feedback ORDER BY id DESC LIMIT 50"
            ).fetchall()
        except Exception:
            fb_rows = []

        top_projects = sorted(proj_rows, key=lambda r: r["pub"], reverse=True)[:8]

        def _mr_link(project: str, mr_iid: str, stored_url: str) -> str:
            stored_url = (stored_url or "").strip()
            if stored_url:
                return stored_url
            if project and mr_iid:
                return f"{gitlab_url}/{project}/-/merge_requests/{mr_iid}"
            return ""

        return {
            "pub_total": pub_total,
            "app_total": app_total,
            "overall_pct": round((app_total / pub_total * 100) if pub_total else 0, 1),
            "dist_labels": [str(r["project"] or "(unknown)") for r in top_projects],
            "dist_values": [r["pub"] for r in top_projects],
            "week_labels": [r["week"] for r in week_rows],
            "week_values": [
                round((r["app"] / r["pub"] * 100) if r["pub"] else 0, 1) for r in week_rows
            ],
            "project_rows": [
                {
                    "project": str(r["project"] or "(unknown)"),
                    "pub": r["pub"],
                    "app": r["app"],
                    "pct": round((r["app"] / r["pub"] * 100) if r["pub"] else 0, 1),
                }
                for r in proj_rows
            ],
            "mr_rows": [
                {
                    "mr": f"{r['project']}!{r['mr_iid']}",
                    "project": str(r["project"]),
                    "pub": r["pub"],
                    "app": r["app"],
                    "pct": round((r["app"] / r["pub"] * 100) if r["pub"] else 0, 1),
                    "ts": to_cn_display(r["last_at"]) if r["last_at"] else "",
                    "owner": (r["owner"] if "owner" in r.keys() else None) or "",
                    "link": _mr_link(str(r["project"]), str(r["mr_iid"]), r["mr_url"]),
                }
                for r in mr_rows
            ],
            "fb_rows": [
                {
                    "ts": to_cn_display(r["created_at"]) if r["created_at"] else "",
                    "user": str(r["feedback_user"] or ""),
                    "mr": f"{r['project'] or ''}!{r['mr_iid'] or ''}",
                    "link": _mr_link(str(r["project"] or ""), str(r["mr_iid"] or ""), ""),
                    "comment": str(r["comment"] or "")[:200],
                }
                for r in fb_rows
            ],
        }
    except Exception as e:
        get_logger().error(f"dashboard: inline summary query failed: {e}")
        return {"pub_total": 0, "app_total": 0, "overall_pct": 0, "dist_labels": [],
                "dist_values": [], "week_labels": [], "week_values": [], "project_rows": [],
                "mr_rows": [], "fb_rows": []}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# JSON API (data source for the HTML shells below; also fine to call directly)
# --------------------------------------------------------------------------- #

def _safe_memory_summary(db_path: str, days: Optional[int], project: Optional[str]) -> dict:
    """Return the repair-memory effectiveness summary, never raising."""
    try:
        from ut_agent.repair_memory.outcomes import memory_effectiveness_summary

        return memory_effectiveness_summary(days=days, project=project, path=db_path)
    except Exception:
        return {
            "eligible_episodes": 0,
            "active_project_memories": 0,
            "active_global_memories": 0,
            "shadow_attempts": 0,
            "injected_attempts": 0,
            "settled_pipeline_attempts": 0,
            "immediate_successes": 0,
            "immediate_success_rate": 0,
            "no_validation_attempts": 0,
            "needs_review": 0,
        }


_TRIAGE_DETAIL_LEGACY_REASON = "该记录生成时未保存修复详情。"
_TRIAGE_DETAIL_DISABLED_REASON = "修复详情功能当前不可用。"


def _triage_repair_detail_metadata(task_id: object) -> dict[str, object]:
    normalized = str(task_id or "").strip()
    if not normalized:
        return {
            "task_id": "",
            "detail_available": False,
            "detail_url": "",
            "detail_unavailable_reason": _TRIAGE_DETAIL_LEGACY_REASON,
        }
    signature = sign_repair_details_task(normalized) if repair_details_enabled() else ""
    if not signature:
        return {
            "task_id": normalized,
            "detail_available": False,
            "detail_url": "",
            "detail_unavailable_reason": _TRIAGE_DETAIL_DISABLED_REASON,
        }
    task_path = quote(normalized, safe="")
    signed = quote(signature, safe="")
    return {
        "task_id": normalized,
        "detail_available": True,
        "detail_url": f"/repair-results/{task_path}?sig={signed}&embed=1",
        "detail_unavailable_reason": "",
    }


def collect_triage_summary(
    days: Optional[int] = 30,
    project: Optional[str] = None,
    page: int = 1,
) -> dict:
    """Return the CI-triage dashboard payload as plain JSON-able data."""
    try:
        db_path = get_feedback_db_path()
        init_triage_table(db_path)
        conn = _connect(db_path)
    except Exception as e:
        get_logger().error(f"dashboard: failed to open triage db: {e}")
        return {"total": 0, "success_rate": 0, "partial_count": 0, "blocked_count": 0,
                "avg_iterations": 0, "avg_duration_ms": 0,
                "cat_labels": [], "cat_values": [], "cat_sr": [],
                "week_labels": [], "week_values": [], "recent_rows": [],
                "recent_page": 1, "recent_page_size": _TRIAGE_RECENT_PAGE_SIZE,
                "recent_total": 0, "recent_total_pages": 0,
                "memory": _safe_memory_summary("", days, project)}

    try:
        clauses, params = [], []
        if days:
            clauses.append("created_at >= ?")
            params.append((now_cn() - timedelta(days=days)).isoformat())
        if project:
            clauses.append("project = ?")
            params.append(project)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            requested_page = max(1, int(page))
        except (TypeError, ValueError):
            requested_page = 1
        recent_total = conn.execute(
            f"SELECT COUNT(*) FROM triage_runs {where}",
            params,
        ).fetchone()[0]
        recent_total_pages = (
            recent_total + _TRIAGE_RECENT_PAGE_SIZE - 1
        ) // _TRIAGE_RECENT_PAGE_SIZE
        recent_page = min(requested_page, recent_total_pages) if recent_total_pages else 1
        recent_offset = (recent_page - 1) * _TRIAGE_RECENT_PAGE_SIZE
        recent_params = [*params, _TRIAGE_RECENT_PAGE_SIZE, recent_offset]
        aggregate_clauses = [*clauses, "COALESCE(trigger_type, '') != 'post_repair_ut'"]
        aggregate_where = "WHERE " + " AND ".join(aggregate_clauses)
        outcome = (
            "CASE WHEN repair_outcome IN ('success', 'partial_success', 'failed', 'blocked') "
            "THEN repair_outcome WHEN success = 1 THEN 'success' ELSE 'failed' END"
        )
        qualified_outcome = (
            "CASE WHEN triage_runs.repair_outcome IN ('success', 'partial_success', 'failed', 'blocked') "
            "THEN triage_runs.repair_outcome WHEN triage_runs.success = 1 THEN 'success' ELSE 'failed' END"
        )

        overview = conn.execute(
            f"""SELECT COUNT(*) AS c,
                       CAST(SUM(CASE WHEN {outcome} = 'success' THEN 1 ELSE 0 END) AS REAL)
                           / NULLIF(SUM(CASE WHEN {outcome} != 'blocked' THEN 1 ELSE 0 END), 0) AS sr,
                       SUM(CASE WHEN {outcome} = 'blocked' THEN 1 ELSE 0 END) AS blocked_count,
                       AVG(iterations) AS ai, AVG(fix_duration_ms) AS ad
                FROM triage_runs {aggregate_where}""",
            params,
        ).fetchone()
        total = overview["c"] or 0

        cat_rows = conn.execute(
            f"""SELECT json_each.value AS cat, COUNT(*) AS c,
                       ROUND(100.0 * SUM(CASE WHEN {qualified_outcome} = 'success' THEN 1 ELSE 0 END)
                           / NULLIF(SUM(CASE WHEN {qualified_outcome} != 'blocked' THEN 1 ELSE 0 END), 0), 1) AS sr
                FROM triage_runs, json_each(triage_runs.failure_categories)
                {aggregate_where}
                GROUP BY cat ORDER BY c DESC""",
            params,
        ).fetchall()

        week_rows = conn.execute(
            f"""SELECT strftime('%Y-W%W', created_at) AS week, COUNT(*) AS c,
                       ROUND(100.0 * SUM(CASE WHEN {outcome} = 'success' THEN 1 ELSE 0 END)
                           / NULLIF(SUM(CASE WHEN {outcome} != 'blocked' THEN 1 ELSE 0 END), 0), 1) AS sr
                FROM triage_runs {aggregate_where} GROUP BY week ORDER BY week DESC LIMIT 8""",
            params,
        ).fetchall()
        week_rows = list(reversed(week_rows))

        has_inventory = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mr_inventory'"
        ).fetchone() is not None
        if has_inventory:
            recent_sql = f"""
                SELECT t.created_at, t.pr_url, t.project, t.mr_iid,
                       COALESCE(
                           NULLIF(NULLIF(TRIM(t.mr_author), ''), 'unknown'),
                           NULLIF(NULLIF(TRIM(i.author), ''), 'unknown'),
                           '未解析'
                       ) AS actor,
                       t.failure_categories, t.success, t.repair_outcome, t.category_results, t.task_id,
                       t.trigger_type,
                       t.final_pipeline_status, t.iterations, t.fix_duration_ms, t.error, t.final_coverage,
                       COALESCE(json_extract(
                           CASE WHEN json_valid(t.extra_json) THEN t.extra_json ELSE '{{}}' END,
                           '$.coverage_source'
                       ), '') AS coverage_source,
                       COALESCE(json_extract(
                           CASE WHEN json_valid(t.extra_json) THEN t.extra_json ELSE '{{}}' END,
                           '$.coverage_status'
                       ), '') AS coverage_status,
                       COALESCE(json_extract(
                           CASE WHEN json_valid(t.extra_json) THEN t.extra_json ELSE '{{}}' END,
                           '$.blocker_type'
                       ), '') AS blocker_type,
                       COALESCE(json_extract(
                           CASE WHEN json_valid(t.extra_json) THEN t.extra_json ELSE '{{}}' END,
                           '$.blocker_summary'
                       ), '') AS blocker_summary,
                       COALESCE(json_extract(
                           CASE WHEN json_valid(t.extra_json) THEN t.extra_json ELSE '{{}}' END,
                           '$.blocker_suggested_action'
                       ), '') AS blocker_suggested_action
                FROM (
                    SELECT * FROM triage_runs {where}
                    ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
                ) AS t
                LEFT JOIN mr_inventory AS i
                  ON i.project_path = t.project
                 AND CAST(i.mr_iid AS TEXT) = CAST(t.mr_iid AS TEXT)
                ORDER BY t.created_at DESC, t.id DESC
            """
        else:
            recent_sql = f"""
                SELECT created_at, pr_url, project, mr_iid,
                       COALESCE(
                           NULLIF(NULLIF(TRIM(mr_author), ''), 'unknown'),
                           '未解析'
                       ) AS actor,
                       failure_categories, success, repair_outcome, category_results, task_id, trigger_type,
                       final_pipeline_status, iterations, fix_duration_ms, error, final_coverage,
                       COALESCE(json_extract(
                           CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{{}}' END,
                           '$.coverage_source'
                       ), '') AS coverage_source,
                       COALESCE(json_extract(
                           CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{{}}' END,
                           '$.coverage_status'
                       ), '') AS coverage_status,
                       COALESCE(json_extract(
                           CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{{}}' END,
                           '$.blocker_type'
                       ), '') AS blocker_type,
                       COALESCE(json_extract(
                           CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{{}}' END,
                           '$.blocker_summary'
                       ), '') AS blocker_summary,
                       COALESCE(json_extract(
                           CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{{}}' END,
                           '$.blocker_suggested_action'
                       ), '') AS blocker_suggested_action
                FROM triage_runs {where}
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
            """
        recent = conn.execute(recent_sql, recent_params).fetchall()

        import json as _json
        memory_summary = _safe_memory_summary(db_path, days, project)
        return {
            "total": total,
            "success_rate": round((overview["sr"] or 0) * 100, 1),
            "partial_count": conn.execute(
                f"SELECT COUNT(*) FROM triage_runs {aggregate_where} AND repair_outcome = 'partial_success'",
                params,
            ).fetchone()[0],
            "blocked_count": overview["blocked_count"] or 0,
            "avg_iterations": round(overview["ai"] or 0, 1),
            "avg_duration_ms": int(overview["ad"] or 0),
            "cat_labels": [r["cat"] for r in cat_rows],
            "cat_values": [r["c"] for r in cat_rows],
            "cat_sr": [r["sr"] or 0 for r in cat_rows],
            "week_labels": [r["week"] for r in week_rows],
            "week_values": [r["sr"] or 0 for r in week_rows],
            "recent_page": recent_page,
            "recent_page_size": _TRIAGE_RECENT_PAGE_SIZE,
            "recent_total": recent_total,
            "recent_total_pages": recent_total_pages,
            "recent_rows": [
                {
                    "ts": to_cn_display(r["created_at"]) if r["created_at"] else "",
                    "url": r["pr_url"] or "",
                    "project": r["project"] or "",
                    "mr_iid": r["mr_iid"] or "",
                    "actor": r["actor"] or "未解析",
                    "cats": _json.loads(r["failure_categories"] or "[]"),
                    "trigger_type": r["trigger_type"] or "",
                    "success": r["success"],
                    "repair_outcome": (
                        r["repair_outcome"]
                        if r["repair_outcome"] in {
                            "success", "partial_success", "failed", "blocked", "succeeded", "partial",
                            "unverified", "canceled", "rollback_failed",
                        }
                        else "success" if r["success"] else "failed"
                    ),
                    "category_results": _json.loads(r["category_results"] or "[]"),
                    "pipeline_status": r["final_pipeline_status"] or "unknown",
                    "iters": r["iterations"],
                    "dur_ms": r["fix_duration_ms"],
                    "coverage": r["final_coverage"],
                    "coverage_source": str(r["coverage_source"] or "")[:32],
                    "coverage_status": str(r["coverage_status"] or "")[:64],
                    "blocker_type": str(r["blocker_type"] or "")[:100],
                    "blocker_summary": str(r["blocker_summary"] or "")[:500],
                    "blocker_suggested_action": str(r["blocker_suggested_action"] or "")[:500],
                    "error": (r["error"] or "")[:200],
                    **_triage_repair_detail_metadata(r["task_id"]),
                }
                for r in recent
            ],
            "memory": memory_summary,
        }
    except Exception as e:
        get_logger().error(f"dashboard: triage summary query failed: {e}")
        return {"total": 0, "success_rate": 0, "partial_count": 0, "blocked_count": 0,
                "avg_iterations": 0, "avg_duration_ms": 0,
                "cat_labels": [], "cat_values": [], "cat_sr": [],
                "week_labels": [], "week_values": [], "recent_rows": [],
                "recent_page": 1, "recent_page_size": _TRIAGE_RECENT_PAGE_SIZE,
                "recent_total": 0, "recent_total_pages": 0,
                "memory": _safe_memory_summary(db_path, days, project)}
    finally:
        conn.close()


def collect_suggestion_filter_summary(
    days: Optional[int] = 30,
    project: Optional[str] = None,
    gitlab_url: Optional[str] = None,
    cutoff: Optional[str] = None,
    include_rows: bool = True,
) -> dict:
    """Return the suggestion-filter dashboard payload as plain JSON-able data.

    Queries both published_suggestions and filtered_suggestions tables to show
    a published-vs-filtered comparison. Never raises; returns an empty payload
    on any error so a dashboard hiccup can't affect the webhook routes.
    """
    gitlab_url = (gitlab_url or DEFAULT_GITLAB_URL).rstrip("/")
    empty = {
        "pub_total": 0, "filtered_total": 0, "filter_rate": 0, "mr_count": 0,
        "reason_labels": [], "reason_values": [],
        "week_labels": [], "week_values": [],
        "project_rows": [], "mr_rows": [], "filtered_rows": [],
    }
    try:
        db_path = get_inline_db_path()
        migrate_inline_schema(path=db_path)
        conn = _connect(db_path)
    except Exception as e:
        get_logger().error(f"dashboard: failed to open suggestion-filter db: {e}")
        return empty

    try:
        clauses, params = [], []
        if cutoff:
            clauses.append("created_at >= ?")
            params.append(cutoff)
        elif days:
            clauses.append("created_at >= ?")
            params.append((now_cn() - timedelta(days=days)).isoformat())
        if project:
            clauses.append("project = ?")
            params.append(project)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        pub_total = conn.execute(
            f"SELECT COUNT(*) FROM published_suggestions {where}", params
        ).fetchone()[0]
        filtered_total = conn.execute(
            f"SELECT COUNT(*) FROM filtered_suggestions {where}", params
        ).fetchone()[0]
        total = pub_total + filtered_total
        filter_rate = round((filtered_total / total * 100) if total else 0, 1)

        mr_count = conn.execute(
            f"SELECT COUNT(DISTINCT project || '!' || mr_iid) FROM filtered_suggestions {where}",
            params,
        ).fetchone()[0]

        reason_rows = conn.execute(
            f"SELECT skip_reason, COUNT(*) AS c FROM filtered_suggestions {where} "
            f"GROUP BY skip_reason ORDER BY c DESC",
            params,
        ).fetchall()

        week_rows = conn.execute(
            f"""SELECT strftime('%Y-W%W', created_at) AS week,
                       COUNT(*) AS flt
                FROM filtered_suggestions {where}
                GROUP BY week ORDER BY week DESC LIMIT 8""",
            params,
        ).fetchall()
        # compute weekly filter rate: filtered / (filtered + published) per week
        pub_week_rows = conn.execute(
            f"""SELECT strftime('%Y-W%W', created_at) AS week, COUNT(*) AS pub
                FROM published_suggestions {where}
                GROUP BY week ORDER BY week DESC LIMIT 8""",
            params,
        ).fetchall()
        pub_week_map = {r["week"]: r["pub"] for r in pub_week_rows}
        week_rows = list(reversed(week_rows))
        week_labels = [r["week"] for r in week_rows]
        week_values = []
        for r in week_rows:
            w = r["week"]
            flt = r["flt"]
            pub = pub_week_map.get(w, 0)
            t = flt + pub
            week_values.append(round((flt / t * 100) if t else 0, 1))

        project_rows = conn.execute(
            f"""SELECT
                    COALESCE(NULLIF(project, ''), '(unknown)') AS project,
                    COUNT(*) AS flt
                FROM filtered_suggestions {where}
                GROUP BY project ORDER BY flt DESC""",
            params,
        ).fetchall()
        # published per project
        pub_proj_rows = conn.execute(
            f"""SELECT COALESCE(NULLIF(project, ''), '(unknown)') AS project,
                       COUNT(*) AS pub
                FROM published_suggestions {where}
                GROUP BY project""",
            params,
        ).fetchall()
        pub_proj_map = {r["project"]: r["pub"] for r in pub_proj_rows}
        filtered_proj_map = {r["project"]: r["flt"] for r in project_rows}
        proj_mrs: dict[str, set[str]] = {}
        for table in ("filtered_suggestions", "published_suggestions"):
            for row in conn.execute(
                f"""SELECT COALESCE(NULLIF(project, ''), '(unknown)') AS project, mr_iid
                    FROM {table} {where} GROUP BY project, mr_iid""",
                params,
            ).fetchall():
                proj_mrs.setdefault(row["project"], set()).add(str(row["mr_iid"] or ""))

        project_out = []
        projects = sorted(
            set(filtered_proj_map) | set(pub_proj_map),
            key=lambda value: (-filtered_proj_map.get(value, 0), value),
        )
        for p in projects:
            flt = filtered_proj_map.get(p, 0)
            pub = pub_proj_map.get(p, 0)
            t = flt + pub
            project_out.append({
                "project": p,
                "pub": pub,
                "filtered": flt,
                "rate": round((flt / t * 100) if t else 0, 1),
                "mr_count": len(proj_mrs.get(p, set())),
            })

        # MR-level aggregation: union of published + filtered per MR
        mr_union = {}
        for r in conn.execute(
            f"""SELECT project, mr_iid, MAX(mr_url) AS mr_url, MAX(mr_author) AS owner,
                       MAX(created_at) AS last_at,
                       COUNT(*) AS flt
                FROM filtered_suggestions {where}
                GROUP BY project, mr_iid ORDER BY last_at DESC LIMIT 1000""",
            params,
        ).fetchall():
            key = (str(r["project"] or ""), str(r["mr_iid"] or ""))
            mr_union[key] = {
                "project": str(r["project"] or ""),
                "mr_iid": str(r["mr_iid"] or ""),
                "mr_url": str(r["mr_url"] or ""),
                "owner": str(r["owner"] or ""),
                "ts": str(r["last_at"] or ""),
                "filtered": r["flt"],
                "pub": 0,
            }
        for r in conn.execute(
            f"""SELECT project, mr_iid, MAX(mr_url) AS mr_url, MAX(mr_author) AS owner,
                       MAX(created_at) AS last_at,
                       COUNT(*) AS pub
                FROM published_suggestions {where}
                GROUP BY project, mr_iid""",
            params,
        ).fetchall():
            key = (str(r["project"] or ""), str(r["mr_iid"] or ""))
            if key not in mr_union:
                mr_union[key] = {
                    "project": str(r["project"] or ""),
                    "mr_iid": str(r["mr_iid"] or ""),
                    "mr_url": str(r["mr_url"] or ""),
                    "owner": str(r["owner"] or ""),
                    "ts": str(r["last_at"] or ""),
                    "filtered": 0,
                    "pub": 0,
                }
            mr_union[key]["pub"] = r["pub"]

        def _mr_link(project, mr_iid, stored_url):
            stored_url = (stored_url or "").strip()
            if stored_url:
                return stored_url
            if project and mr_iid:
                return f"{gitlab_url}/{project}/-/merge_requests/{mr_iid}"
            return ""

        mr_out = []
        for _key, v in sorted(mr_union.items(), key=lambda kv: kv[1]["ts"], reverse=True):
            t = v["pub"] + v["filtered"]
            mr_out.append({
                "mr": f"{v['project']}!{v['mr_iid']}",
                "project": v["project"],
                "pub": v["pub"],
                "filtered": v["filtered"],
                "rate": round((v["filtered"] / t * 100) if t else 0, 1),
                "ts": to_cn_display(v["ts"]) if v["ts"] else "",
                "owner": v["owner"] or "",
                "link": _mr_link(v["project"], v["mr_iid"], v["mr_url"]),
            })

        filtered_rows = conn.execute(
            f"""SELECT created_at, project, mr_iid, mr_url, file_path, label, score,
                       skip_reason, suggestion_content
                FROM filtered_suggestions {where}
                ORDER BY created_at DESC LIMIT 200""",
            params,
        ).fetchall()
        filtered_out = [
            {
                "ts": to_cn_display(r["created_at"]) if r["created_at"] else "",
                "project": str(r["project"] or ""),
                "mr": f"{r['project'] or ''}!{r['mr_iid'] or ''}",
                "file": str(r["file_path"] or ""),
                "label": str(r["label"] or ""),
                "score": r["score"],
                "reason": str(r["skip_reason"] or ""),
                "content": str(r["suggestion_content"] or "")[:500],
                "link": _mr_link(str(r["project"] or ""), str(r["mr_iid"] or ""), r["mr_url"]),
            }
            for r in filtered_rows
        ]

        return {
            "pub_total": pub_total,
            "filtered_total": filtered_total,
            "filter_rate": filter_rate,
            "mr_count": mr_count,
            "reason_labels": [r["skip_reason"] or "(unknown)" for r in reason_rows],
            "reason_values": [r["c"] for r in reason_rows],
            "week_labels": week_labels,
            "week_values": week_values,
            "project_rows": project_out if include_rows else [],
            "mr_rows": mr_out if include_rows else [],
            "filtered_rows": filtered_out if include_rows else [],
        }
    except Exception as e:
        get_logger().error(f"dashboard: suggestion-filter summary query failed: {e}")
        return empty
    finally:
        conn.close()


def _suggestion_review_cutoff(days: Optional[int]) -> str | None:
    candidates = []
    if days:
        candidates.append(now_cn() - timedelta(days=days))
    configured = get_settings().get("suggestion_review_dashboard.history_started_at", "")
    if configured:
        boundary = to_cn(configured)
        if boundary:
            candidates.append(boundary)
        else:
            get_logger().warning(
                f"dashboard: invalid suggestion review tracking boundary: {configured!r}"
            )
    return max(candidates).isoformat() if candidates else None


def collect_suggestion_review_summary(
    days: Optional[int] = 30,
    project: Optional[str] = None,
    gitlab_url: Optional[str] = None,
    include_rows: bool = True,
) -> dict:
    """Return MR coverage plus the existing suggestion-filter analytics."""
    cutoff = _suggestion_review_cutoff(days)
    legacy = collect_suggestion_filter_summary(
        days=None, project=project, gitlab_url=gitlab_url, cutoff=cutoff, include_rows=include_rows,
    )
    empty_coverage = {
        "inventory_total": 0, "triggered_total": 0, "completed_total": 0,
        "published_mr_total": 0, "filtered_mr_total": 0,
        "attention_total": 0, "status_counts": {},
        "alerts": [],
        "coverage_project_rows": [], "review_mr_rows": [],
        "project_options": [],
        "visibility_note": "仅统计当前 GitLab token 有权限访问的项目。",
        "history_started_at": str(get_settings().get("suggestion_review_dashboard.history_started_at", "")),
        "reliable_tracking_started_at": "", "reliability_note": "创建审查可靠窗口尚未初始化。",
        "sync": {},
    }
    try:
        db_path = get_inline_db_path()
        migrate_inline_schema(path=db_path)
        init_review_tracking(path=db_path)
        conn = _connect(db_path)
    except Exception as exc:
        get_logger().error(f"dashboard: failed to open suggestion-review db: {exc}")
        return {**legacy, **empty_coverage}

    try:
        inventory_clauses, inventory_params = [], []
        run_clauses, run_params = [], []
        if cutoff:
            inventory_clauses.append("COALESCE(created_at, updated_at, last_synced_at) >= ?")
            inventory_params.append(cutoff)
        if project:
            inventory_clauses.append("project_path = ?")
            inventory_params.append(project)
            run_clauses.append("project_path = ?")
            run_params.append(project)
        inventory_where = " WHERE " + " AND ".join(inventory_clauses) if inventory_clauses else ""
        run_where = " WHERE " + " AND ".join(run_clauses) if run_clauses else ""

        inventory = [dict(row) for row in conn.execute(
            f"SELECT * FROM mr_inventory{inventory_where} ORDER BY COALESCE(created_at, updated_at) DESC",
            inventory_params,
        ).fetchall()]
        runs = [dict(row) for row in conn.execute(
            f"SELECT * FROM suggestion_review_runs{run_where} ORDER BY started_at DESC", run_params
        ).fetchall()]

        runs_by_mr: dict[tuple[str, str], list[dict]] = {}
        for run in runs:
            runs_by_mr.setdefault((str(run.get("project_path") or ""), str(run.get("mr_iid") or "")), []).append(run)

        evidence_by_mr: dict[tuple[str, str], dict[str, int]] = {}
        for table, field, clause in (
            ("published_suggestions", "published", ""),
            ("filtered_suggestions", "filtered", ""),
            ("suggestion_threads", "failed", "WHERE COALESCE(publish_status, state) = 'failed'"),
        ):
            for evidence_row in conn.execute(
                f"SELECT project, mr_iid, COUNT(*) AS count FROM {table} {clause} GROUP BY project, mr_iid"
            ).fetchall():
                evidence_key = (str(evidence_row["project"] or ""), str(evidence_row["mr_iid"] or ""))
                evidence_by_mr.setdefault(evidence_key, {})[field] = int(evidence_row["count"] or 0)

        grace_seconds = int(get_settings().get("suggestion_review_dashboard.not_triggered_after_seconds", 900))
        run_timeout_seconds = int(get_settings().get("suggestion_review_dashboard.run_timeout_seconds", 7200))
        reliable_started_at = get_creation_tracking_boundary(path=db_path)
        status_counts: dict[str, int] = {}
        mr_rows = []
        included_inventory_total = 0
        triggered = completed = published = filtered_mrs = attention = 0
        project_stats: dict[str, dict] = {}
        for mr in inventory:
            key = (str(mr.get("project_path") or ""), str(mr.get("mr_iid") or ""))
            mr_runs = runs_by_mr.get(key, [])
            run = select_creation_review_run(mr, mr_runs, reliable_started_at)
            if not run:
                run = historical_evidence_run(mr, evidence_by_mr.get(key, {}), reliable_started_at)
            status_name = derive_primary_status(
                mr, run, grace_seconds=grace_seconds, run_timeout_seconds=run_timeout_seconds,
            )
            if not include_in_operational_summary(mr, status_name, reliable_started_at):
                continue
            included_inventory_total += 1
            abnormal_reason = derive_abnormal_reason(
                mr, run, status_name, reliable_started_at=reliable_started_at,
            )
            status_counts[status_name] = status_counts.get(status_name, 0) + 1
            was_triggered = bool(run)
            if was_triggered:
                triggered += 1
            if status_name not in {"waiting", "not_triggered"}:
                completed += 1
            if status_name in {"published", "fallback_published"}:
                published += 1
            if status_name in {"not_triggered", "startup_failed", "execution_failed", "unpublished", "publish_failed"}:
                attention += 1
            counts = {name: int((run or {}).get(name) or 0) for name in (
                "generated_count", "kept_count", "filtered_count", "inline_selected_count",
                "inline_skipped_count", "inline_published_count", "inline_fallback_count", "inline_failed_count",
            )}
            has_secondary_filter = counts["filtered_count"] > 0
            filtered_mrs += int(has_secondary_filter)
            row = {
                "mr": f"{key[0]}!{key[1]}", "project": key[0], "mr_iid": key[1],
                "title": str(mr.get("title") or ""), "owner": str(mr.get("author") or ""),
                "link": str(mr.get("mr_url") or ""), "state": str(mr.get("state") or ""),
                "commit_sha": str((run or {}).get("commit_sha") or mr.get("commit_sha") or "")[:12],
                "status": status_name,
                "stage": str((run or {}).get("stage") or ""),
                "trigger": str((run or {}).get("trigger") or ""),
                "ts": to_cn_display(mr.get("created_at") or mr.get("updated_at") or mr.get("last_synced_at")),
                "has_secondary_filter": has_secondary_filter,
                "unpublished_reason": derive_unpublished_reason(run or {}) if status_name == "unpublished" else None,
                "reason_code": str((abnormal_reason or {}).get("code") or ""),
                "reason_label": str((abnormal_reason or {}).get("label") or ""),
                "recovery_source": (
                    "sync" if str(mr.get("creation_recovery_state") or "") == "recovered" else ""
                ),
                **counts,
            }
            mr_rows.append(row)
            stats = project_stats.setdefault(
                key[0],
                {"project": key[0], "total": 0, "triggered": 0, "published": 0, "filtered": 0, "attention": 0},
            )
            stats["total"] += 1
            stats["triggered"] += 1 if was_triggered else 0
            stats["published"] += 1 if status_name in {"published", "fallback_published"} else 0
            stats["filtered"] += int(has_secondary_filter)
            needs_attention = status_name in {
                "not_triggered", "startup_failed", "execution_failed", "unpublished", "publish_failed",
            }
            stats["attention"] += 1 if needs_attention else 0

        project_rows = sorted(project_stats.values(), key=lambda item: (-item["total"], item["project"]))
        for row in project_rows:
            row["coverage_rate"] = round(row["triggered"] / row["total"] * 100, 1) if row["total"] else 0
        sync = get_sync_state(path=db_path)
        sync_metrics = get_sync_metrics(path=db_path)
        alerts = [
            {**alert, "label": SUGGESTION_REVIEW_ALERT_LABELS.get(alert["key"], alert["key"])}
            for alert in active_review_alerts_payload(path=db_path)
        ]
        return {
            **legacy,
            "inventory_total": included_inventory_total, "triggered_total": triggered,
            "completed_total": completed, "published_mr_total": published,
            "filtered_mr_total": filtered_mrs,
            "attention_total": attention, "status_counts": status_counts,
            "alerts": alerts,
            "coverage_project_rows": project_rows if include_rows else [],
            "review_mr_rows": mr_rows if include_rows else [],
            "project_options": sorted(project_stats),
            "visibility_note": "仅统计当前 GitLab token 有权限访问的项目。",
            "history_started_at": str(get_settings().get("suggestion_review_dashboard.history_started_at", "")),
            "reliable_tracking_started_at": reliable_started_at or "",
            "reliability_note": (
                f"{to_cn_display(reliable_started_at)} 起使用创建审查状态机；此前记录按已有证据展示。"
                if reliable_started_at else "创建审查可靠窗口尚未初始化；历史记录仍按已有证据展示。"
            ),
            "sync": {
                "last_success_at": to_cn_display(sync.get("last_success_at")),
                "last_reconcile_at": to_cn_display(sync.get("last_reconcile_at")),
                "last_error": str(sync.get("last_error") or "")[:300],
                "metrics": sync_metrics,
            },
        }
    except Exception as exc:
        get_logger().error(f"dashboard: suggestion-review summary query failed: {exc}")
        return {**legacy, **empty_coverage}
    finally:
        conn.close()


def _paginate_suggestion_review_rows(rows: list[dict], page: int) -> dict:
    total_rows = len(rows)
    total_pages = max(1, (total_rows + SUGGESTION_REVIEW_PAGE_SIZE - 1) // SUGGESTION_REVIEW_PAGE_SIZE)
    current_page = min(max(1, int(page or 1)), total_pages)
    offset = (current_page - 1) * SUGGESTION_REVIEW_PAGE_SIZE
    return {
        "rows": rows[offset:offset + SUGGESTION_REVIEW_PAGE_SIZE],
        "page": current_page,
        "page_size": SUGGESTION_REVIEW_PAGE_SIZE,
        "total_rows": total_rows,
        "total_pages": total_pages,
    }


def collect_suggestion_review_table(
    table: str,
    page: int = 1,
    days: Optional[int] = 30,
    project: Optional[str] = None,
    status: Optional[str] = None,
    query: Optional[str] = None,
    attention_only: bool = False,
    gitlab_url: Optional[str] = None,
) -> dict:
    """Return one fixed-size page for a Suggestion Filter dashboard table."""
    if table not in SUGGESTION_REVIEW_TABLES:
        raise ValueError(f"Unsupported suggestion-review table: {table}")
    if table == "review_mrs":
        rows = collect_suggestion_review_summary(
            days=days, project=project, gitlab_url=gitlab_url, include_rows=True,
        ).get("review_mr_rows", [])
        query_value = str(query or "").strip().lower()
        attention_statuses = {
            "not_triggered", "startup_failed", "execution_failed", "unpublished", "publish_failed",
        }

        def matches(row: dict) -> bool:
            status_matches = not status or (
                row.get("has_secondary_filter") if status == "secondary_filtered" else row.get("status") == status
            )
            haystack = " ".join(str(row.get(key) or "") for key in ("mr", "project", "title", "owner")).lower()
            return (
                status_matches
                and (not query_value or query_value in haystack)
                and (not attention_only or row.get("status") in attention_statuses)
            )

        rows = [row for row in rows if matches(row)]
    else:
        cutoff = _suggestion_review_cutoff(days)
        legacy = collect_suggestion_filter_summary(
            days=None, project=project, gitlab_url=gitlab_url, cutoff=cutoff, include_rows=True,
        )
        key = {
            "filter_projects": "project_rows",
            "filter_mrs": "mr_rows",
            "filtered_suggestions": "filtered_rows",
        }[table]
        rows = legacy.get(key, [])
    return _paginate_suggestion_review_rows(rows, page)


@router.get("/api/feedback/summary")
async def api_feedback_summary(days: int = Query(30), project: Optional[str] = Query(None)):
    return JSONResponse(collect_feedback_summary(days=days or None, project=project))


@router.get("/api/inline/summary")
async def api_inline_summary(gitlab_url: Optional[str] = Query(None)):
    return JSONResponse(collect_inline_summary(gitlab_url=gitlab_url))


@router.get("/api/triage/summary")
async def api_triage_summary(
    days: int = Query(30),
    project: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
):
    return JSONResponse(collect_triage_summary(days=days or None, project=project, page=page))


@router.get("/api/ci-failures/summary")
async def api_ci_failure_summary(
    days: int = Query(30, ge=0, le=3650),
    project: str = Query("", max_length=240),
    family: str = Query("", max_length=40),
    capability: str = Query("", max_length=40),
    fingerprint: str = Query("", max_length=64),
    q: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    recurring_page: int = Query(1, ge=1),
    recurring_page_size: int = Query(5, ge=1),
    project_distribution_page: int = Query(1, ge=1),
    project_distribution_page_size: int = Query(5, ge=1),
    job_distribution_page: int = Query(1, ge=1),
    job_distribution_page_size: int = Query(5, ge=1),
):
    try:
        return JSONResponse(collect_ci_failure_summary(
            days=days or None,
            project=project,
            family=family,
            capability=capability,
            fingerprint=fingerprint,
            query=q,
            page=page,
            page_size=page_size,
            recurring_page=recurring_page,
            recurring_page_size=recurring_page_size,
            project_distribution_page=project_distribution_page,
            project_distribution_page_size=project_distribution_page_size,
            job_distribution_page=job_distribution_page,
            job_distribution_page_size=job_distribution_page_size,
        ))
    except Exception as error:
        get_logger().error(f"dashboard: CI failure summary failed: {type(error).__name__}")
        raise HTTPException(status_code=500, detail="读取 CI 失败数据失败，请稍后重试") from error


@router.get("/api/ci-failures/{failure_id}")
async def api_ci_failure_detail(failure_id: int):
    value = collect_ci_failure_detail(failure_id)
    if value is None:
        raise HTTPException(status_code=404, detail="失败记录不存在")
    return JSONResponse(value)


@router.post("/api/ci-failures/{failure_id}/annotations")
async def api_ci_failure_annotation(failure_id: int, request: CiFailureAnnotationRequest):
    try:
        value = annotate_ci_failure(failure_id, request)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="人工标注内容无效") from error
    if value is None:
        raise HTTPException(status_code=404, detail="失败记录或 Job 不存在")
    return JSONResponse({"changed": True, "failure": value})


@router.get("/api/repair-memory/memories")
async def api_repair_memory_memories(
    scope: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
):
    """Return repair-memory rows for the dashboard. Never raises."""
    try:
        from ut_agent.repair_memory.store import init_repair_memory_tables, list_memories

        db_path = get_feedback_db_path()
        init_repair_memory_tables(db_path)
        scope_key = "*" if scope == "global" else (project or "")
        requested_status = str(status or "").strip()
        effective_status = "" if requested_status == "all" else (requested_status or "active")
        memories = list_memories(
            scope=scope or "",
            scope_key=scope_key,
            status=effective_status,
            path=db_path,
        )
        return JSONResponse({
            "memories": [
                {
                    "memory_id": m.memory_id,
                    "scope": m.scope.value,
                    "scope_key": m.scope_key,
                    "pattern_key": m.pattern_key,
                    "pattern_version": m.pattern_version,
                    "language": m.language,
                    "build_system": m.build_system,
                    "failure_family": m.failure_family,
                    "root_cause_class": m.root_cause_class,
                    "repair_action_class": m.repair_action_class,
                    "problem_pattern": m.problem_pattern,
                    "applicability": list(m.applicability),
                    "anti_conditions": list(m.anti_conditions),
                    "repair_guidance": m.repair_guidance,
                    "validation_guidance": list(m.validation_guidance),
                    "confidence": m.confidence,
                    "support_episode_count": m.support_episode_count,
                    "support_project_count": m.support_project_count,
                    "settled_attempts": m.settled_attempts,
                    "immediate_successes": m.immediate_successes,
                    "status": m.status.value,
                    "created_at": m.created_at,
                    "updated_at": m.updated_at,
                    "last_reinforced_at": m.last_reinforced_at,
                }
                for m in memories
            ]
        })
    except Exception as e:
        get_logger().error(f"dashboard: repair-memory memories query failed: {e}")
        return JSONResponse({"memories": []})


async def _change_repair_memory_status(
    memory_id: str,
    request: MemoryStatusChangeRequest,
    *,
    enable: bool,
) -> JSONResponse:
    """Apply one bounded, idempotent dashboard memory-state transition."""
    from ut_agent.repair_memory.models import MemoryStatus
    from ut_agent.repair_memory.store import (
        init_repair_memory_tables,
        load_memory,
        revalidate_global_support,
        update_memory_status,
    )

    db_path = get_feedback_db_path()
    init_repair_memory_tables(db_path)
    current = load_memory(memory_id, path=db_path)
    if current is None:
        raise HTTPException(status_code=404, detail="修复经验不存在")

    target = MemoryStatus.ACTIVE if enable else MemoryStatus.DISABLED
    expected = (
        frozenset({MemoryStatus.DISABLED})
        if enable
        else frozenset({MemoryStatus.ACTIVE, MemoryStatus.NEEDS_REVIEW})
    )
    if current.status is target:
        return JSONResponse({"memory_id": memory_id, "status": target.value, "changed": False})
    if current.status not in expected:
        raise HTTPException(status_code=409, detail=f"当前状态 {current.status.value} 不允许该操作")

    updated = update_memory_status(
        memory_id,
        target,
        request.reason,
        source="dashboard",
        expected_statuses=expected,
        path=db_path,
    )
    if updated is None:
        latest = load_memory(memory_id, path=db_path)
        if latest is None:
            raise HTTPException(status_code=404, detail="修复经验不存在")
        raise HTTPException(
            status_code=409,
            detail=f"经验状态已变更为 {latest.status.value}，请刷新后重试",
        )
    if not enable and updated.scope.value == "project":
        revalidate_global_support(updated.pattern_key, path=db_path)
    return JSONResponse({"memory_id": memory_id, "status": updated.status.value, "changed": True})


# These operator routes intentionally reuse the dashboard's internal network
# boundary in v1 and must not be exposed directly to the public internet. A
# future authentication layer belongs in shared dashboard middleware, not in
# individual button handlers.
@router.post("/api/repair-memory/memories/{memory_id}/disable")
async def api_disable_repair_memory(memory_id: str, request: MemoryStatusChangeRequest):
    try:
        return await _change_repair_memory_status(memory_id, request, enable=False)
    except HTTPException:
        raise
    except Exception as error:
        get_logger().error(f"dashboard: repair-memory disable failed: {type(error).__name__}")
        raise HTTPException(status_code=500, detail="删除经验失败，请稍后重试") from error


@router.post("/api/repair-memory/memories/{memory_id}/enable")
async def api_enable_repair_memory(memory_id: str, request: MemoryStatusChangeRequest):
    try:
        return await _change_repair_memory_status(memory_id, request, enable=True)
    except HTTPException:
        raise
    except Exception as error:
        get_logger().error(f"dashboard: repair-memory enable failed: {type(error).__name__}")
        raise HTTPException(status_code=500, detail="恢复经验失败，请稍后重试") from error


@router.get("/api/repair-memory/effectiveness")
async def api_repair_memory_effectiveness(
    days: Optional[int] = Query(None),
    project: Optional[str] = Query(None),
):
    """Return the repair-memory effectiveness summary. Never raises."""
    try:
        from ut_agent.repair_memory.outcomes import memory_effectiveness_summary

        return JSONResponse(memory_effectiveness_summary(days=days, project=project))
    except Exception as e:
        get_logger().error(f"dashboard: repair-memory effectiveness query failed: {e}")
        return JSONResponse({
            "eligible_episodes": 0,
            "active_project_memories": 0,
            "active_global_memories": 0,
            "shadow_attempts": 0,
            "injected_attempts": 0,
            "settled_pipeline_attempts": 0,
            "immediate_successes": 0,
            "immediate_success_rate": 0,
            "no_validation_attempts": 0,
            "needs_review": 0,
        })


@router.get("/api/repair-memory/retrieval-audits")
async def api_repair_memory_retrieval_audits(
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    project: Optional[str] = Query(None),
):
    """Return recent task-level repair-memory retrieval decisions."""
    try:
        from ut_agent.repair_memory.audit import query_retrieval_audits

        return JSONResponse(query_retrieval_audits(
            page=page,
            page_size=page_size,
            project=project,
            path=get_feedback_db_path(),
        ))
    except Exception as error:
        get_logger().error(f"dashboard: retrieval audit query failed: {type(error).__name__}")
        return JSONResponse({"audits": [], "page": page, "page_size": page_size, "total": 0, "total_pages": 0})


@router.get("/api/suggestion-filter/summary")
async def api_suggestion_filter_summary(
    days: int = Query(30), project: Optional[str] = Query(None),
    gitlab_url: Optional[str] = Query(None),
):
    return JSONResponse(
        collect_suggestion_filter_summary(days=days or None, project=project, gitlab_url=gitlab_url)
    )


@router.get("/api/suggestion-review/summary")
async def api_suggestion_review_summary(
    days: int = Query(30), project: Optional[str] = Query(None),
    gitlab_url: Optional[str] = Query(None),
):
    return JSONResponse(
        collect_suggestion_review_summary(
            days=days or None, project=project, gitlab_url=gitlab_url, include_rows=False,
        )
    )


@router.get("/api/suggestion-review/table/{table}")
async def api_suggestion_review_table(
    table: str,
    page: int = Query(1, ge=1),
    days: int = Query(30, ge=0),
    project: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    query: Optional[str] = Query(None, max_length=300),
    attention_only: bool = Query(False),
    gitlab_url: Optional[str] = Query(None),
):
    if table not in SUGGESTION_REVIEW_TABLES:
        raise HTTPException(status_code=404, detail="Unknown suggestion-review table")
    return JSONResponse(collect_suggestion_review_table(
        table, page=page, days=days or None, project=project, status=status,
        query=query, attention_only=attention_only, gitlab_url=gitlab_url,
    ))


@router.get("/api/suggestion-review/detail")
async def api_suggestion_review_detail(
    project: str = Query(..., min_length=1, max_length=500),
    mr_iid: str = Query(..., min_length=1, max_length=100),
    gitlab_url: Optional[str] = Query(None),
):
    return JSONResponse(collect_creation_review_detail(project, mr_iid, gitlab_url=gitlab_url))


# --------------------------------------------------------------------------- #
# HTML shells -- static page + JS that fetches the JSON above on every load,
# so refreshing the browser always shows the latest data (no build/sync step).
# --------------------------------------------------------------------------- #

_BASE_CSS = """
:root {
  --bg: #0b1020; --panel: rgba(17, 24, 39, 0.82); --text: #e5eefb; --muted: #94a3b8;
  --line: rgba(148, 163, 184, 0.18); --shadow: 0 20px 40px rgba(0, 0, 0, 0.35); --radius: 22px;
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--text);
  background: radial-gradient(circle at top left, rgba(96,165,250,.24), transparent 28%),
    radial-gradient(circle at top right, rgba(167,139,250,.18), transparent 24%),
    linear-gradient(180deg, #0b1020 0%, #111827 100%);
}
a { color: inherit; text-decoration: none; }
.container { max-width: 1280px; margin: 0 auto; padding: 32px 24px 48px; }
.hero {
  display: flex; justify-content: space-between; gap: 24px; align-items: flex-start;
  padding: 28px 30px; border: 1px solid var(--line); border-radius: 28px;
  background: linear-gradient(135deg, rgba(15,23,42,.88), rgba(30,41,59,.72));
  box-shadow: var(--shadow); backdrop-filter: blur(16px);
}
.hero h1 { margin: 0 0 8px; font-size: 30px; }
.hero p { margin: 0; color: var(--muted); }
.stamp { text-align: right; color: var(--muted); font-size: 14px; }
.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 18px; margin-top: 24px; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 22px; backdrop-filter: blur(14px);
}
.metric-value { font-size: 34px; font-weight: 700; margin-top: 10px; }
.metric-label, .section-subtitle, .muted { color: var(--muted); }
.grid-2 { display: grid; grid-template-columns: 1.15fr 1fr; gap: 18px; margin-top: 18px; }
.section-title { margin: 0 0 6px; font-size: 18px; font-weight: 700; }
.chart-wrap { height: 320px; margin-top: 18px; }
table { width: 100%; border-collapse: collapse; margin-top: 16px; overflow: hidden; border-radius: 14px; }
th, td { text-align: left; padding: 12px 14px; border-bottom: 1px solid var(--line); font-size: 14px; }
th { color: var(--muted); font-weight: 600; }
tr:last-child td { border-bottom: none; }
.mini-badge {
  display: inline-flex; align-items: center; justify-content: center; min-width: 56px;
  padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; color: white;
}
.pct-low { background: linear-gradient(135deg, #ef4444, #f87171); }
.pct-mid { background: linear-gradient(135deg, #f59e0b, #fbbf24); color: #1f2937; }
.pct-high { background: linear-gradient(135deg, #10b981, #34d399); }
.score-1 { background: linear-gradient(135deg, #ef4444, #f87171); }
.score-2 { background: linear-gradient(135deg, #f97316, #fb923c); }
.score-3 { background: linear-gradient(135deg, #f59e0b, #fbbf24); color: #1f2937; }
.score-4 { background: linear-gradient(135deg, #10b981, #34d399); }
.score-5 { background: linear-gradient(135deg, #3b82f6, #60a5fa); }
.comment-cell { max-width: 420px; white-space: normal; word-break: break-word; line-height: 1.55; }
.link-cell a { color: #93c5fd; }
.table-wrap {
  margin-top: 14px; border: 1px solid var(--line); border-radius: 16px; overflow: hidden;
  background: rgba(2, 6, 23, 0.28);
}
.table-wrap table { margin-top: 0; }
.pager { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 16px; flex-wrap: wrap; }
.pager-actions { display: flex; gap: 10px; align-items: center; }
.btn {
  border: 1px solid var(--line); background: rgba(15, 23, 42, 0.9); color: var(--text);
  padding: 10px 14px; border-radius: 12px; cursor: pointer;
}
.btn:disabled { opacity: .45; cursor: not-allowed; }
.filter-select {
  border: 1px solid var(--line); background: rgba(15, 23, 42, 0.9); color: var(--text);
  padding: 9px 12px; border-radius: 10px; margin: 8px 8px 0 0;
}
.refresh-note { color: var(--muted); font-size: 13px; margin-top: 6px; }
@media (max-width: 980px) {
  .metrics, .grid-2 { grid-template-columns: 1fr; }
  .hero { flex-direction: column; }
  .stamp { text-align: left; }
}
"""

_OPERATIONS_DASHBOARD_CSS = """
.ops-dashboard {
  --ops-bg: #070d19; --ops-surface: #0e1728; --ops-surface-raised: #111d31;
  --ops-border: rgba(148, 163, 184, 0.16); --ops-border-strong: rgba(148, 163, 184, 0.27);
  --ops-text: #edf4ff; --ops-muted: #8fa1ba; --ops-blue: #5b8cff; --ops-violet: #9b7cff;
  --ops-green: #35c793; --ops-amber: #f4b44d; --ops-red: #f87171;
  min-width: 0; min-height: 100vh; overflow-x: hidden; color: var(--ops-text);
  background:
    radial-gradient(circle at 12% -10%, rgba(59, 130, 246, .13), transparent 31rem),
    radial-gradient(circle at 90% 0%, rgba(139, 92, 246, .08), transparent 26rem),
    var(--ops-bg);
}
.ops-dashboard .ops-shell { width: min(100%, 1440px); margin: 0 auto; padding: 18px clamp(16px, 2.4vw, 34px) 42px; }
.ops-dashboard .nav-bar {
  display: flex; align-items: center; gap: 6px; min-width: 0; padding: 0 0 14px;
  overflow-x: auto; scrollbar-width: none;
}
.ops-dashboard .nav-bar::-webkit-scrollbar { display: none; }
.ops-dashboard .nav-tab {
  display: inline-flex; flex: 0 0 auto; align-items: center; justify-content: center; min-height: 44px;
  padding: 0 15px; border: 1px solid transparent; border-radius: 10px; color: var(--ops-muted);
  font-size: 13px; font-weight: 650; letter-spacing: .01em; transition: color .18s ease, background .18s ease, border-color .18s ease;
}
.ops-dashboard .nav-tab:hover { color: var(--ops-text); background: rgba(148, 163, 184, .07); }
.ops-dashboard .nav-tab.active {
  color: #fff; border-color: rgba(91, 140, 255, .38); background: rgba(91, 140, 255, .14);
  box-shadow: inset 0 0 0 1px rgba(91, 140, 255, .06);
}
.ops-dashboard .ops-hero {
  display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; padding: 24px 26px;
  border: 1px solid var(--ops-border); border-radius: 18px;
  background: linear-gradient(135deg, rgba(17, 29, 49, .96), rgba(10, 19, 34, .92));
  box-shadow: 0 18px 46px rgba(0, 0, 0, .2);
}
.ops-dashboard .ops-eyebrow { margin-bottom: 8px; color: #79a3ff; font-size: 11px; font-weight: 750; letter-spacing: .14em; text-transform: uppercase; }
.ops-dashboard .ops-hero h1 { margin: 0; font-size: clamp(24px, 3vw, 34px); line-height: 1.16; letter-spacing: -.025em; }
.ops-dashboard .ops-hero p { max-width: 720px; margin: 9px 0 0; color: var(--ops-muted); font-size: 14px; line-height: 1.6; }
.ops-dashboard .ops-live { display: flex; flex: 0 0 auto; align-items: center; gap: 10px; color: var(--ops-muted); font-size: 12px; white-space: nowrap; }
.ops-dashboard .ops-live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ops-green); box-shadow: 0 0 0 5px rgba(53, 199, 147, .11); }
.ops-dashboard .ops-live strong { display: block; margin-top: 3px; color: #c7d4e7; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; font-weight: 550; }
.ops-dashboard .ops-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
.ops-dashboard .ops-metrics.triage-metrics { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.ops-dashboard .metric-card {
  position: relative; min-width: 0; padding: 17px 18px 16px; overflow: hidden; border: 1px solid var(--ops-border);
  border-radius: 14px; background: rgba(14, 23, 40, .92); box-shadow: 0 12px 30px rgba(0, 0, 0, .13);
}
.ops-dashboard .metric-card::before { position: absolute; inset: 0 auto 0 0; width: 3px; content: ""; background: var(--metric-accent, var(--ops-blue)); }
.ops-dashboard .metric-label { color: var(--ops-muted); font-size: 12px; font-weight: 650; letter-spacing: .02em; }
.ops-dashboard .metric-value { margin-top: 8px; color: var(--ops-text); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: clamp(27px, 2.5vw, 34px); font-weight: 760; line-height: 1.05; letter-spacing: -.04em; }
.ops-dashboard .ops-grid { display: grid; grid-template-columns: minmax(0, 1.08fr) minmax(0, .92fr); gap: 14px; margin-top: 14px; }
.ops-dashboard .ops-card { min-width: 0; margin-top: 14px; padding: 18px; border: 1px solid var(--ops-border); border-radius: 16px; background: rgba(14, 23, 40, .9); box-shadow: 0 12px 32px rgba(0, 0, 0, .14); }
.ops-dashboard .ops-grid .ops-card { margin-top: 0; }
.ops-dashboard .section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 13px; }
.ops-dashboard .section-title { margin: 0; color: var(--ops-text); font-size: 15px; font-weight: 700; letter-spacing: -.01em; }
.ops-dashboard .section-subtitle { margin-top: 4px; color: var(--ops-muted); font-size: 12px; line-height: 1.5; }
.ops-dashboard .section-kicker { color: var(--ops-muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; white-space: nowrap; }
.ops-dashboard .chart-wrap { height: 280px; margin-top: 0; }
.ops-dashboard .table-wrap { max-width: 100%; margin-top: 0; overflow-x: auto; overflow-y: auto; border: 1px solid var(--ops-border); border-radius: 12px; background: rgba(5, 11, 22, .42); }
.ops-dashboard .ops-table { min-width: 720px; margin: 0; border-collapse: separate; border-spacing: 0; }
.ops-dashboard .ops-table.wide { min-width: 1020px; }
.ops-dashboard .ops-table th, .ops-dashboard .ops-table td { padding: 11px 13px; border-bottom: 1px solid var(--ops-border); font-size: 12.5px; line-height: 1.45; vertical-align: middle; }
.ops-dashboard .ops-table th { position: sticky; top: 0; z-index: 1; color: #9fb0c8; background: #101b2d; font-size: 11px; font-weight: 700; letter-spacing: .035em; white-space: nowrap; }
.ops-dashboard .ops-table tbody tr:nth-child(even) { background: rgba(148, 163, 184, .025); }
.ops-dashboard .ops-table tbody tr:hover { background: rgba(91, 140, 255, .065); }
.ops-dashboard .ops-table tbody tr:last-child td { border-bottom: 0; }
.ops-dashboard .num { text-align: right; font-variant-numeric: tabular-nums; }
.ops-dashboard .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }
.ops-dashboard .link-cell a { display: inline-flex; min-height: 32px; align-items: center; color: #8ab0ff; font-weight: 650; }
.ops-dashboard .link-cell a:hover { color: #b7ccff; text-decoration: underline; text-underline-offset: 3px; }
.ops-dashboard .mini-badge { min-width: 60px; padding: 4px 9px; box-shadow: none; }
.ops-dashboard .status-badge, .ops-dashboard .category-chip {
  display: inline-flex; align-items: center; border: 1px solid transparent; border-radius: 999px; font-size: 11px; font-weight: 700; white-space: nowrap;
}
.ops-dashboard .status-badge { min-width: 52px; justify-content: center; padding: 4px 9px; }
.ops-dashboard .status-success { color: #82e3be; border-color: rgba(53, 199, 147, .28); background: rgba(53, 199, 147, .1); }
.ops-dashboard .status-warning {
  color: #f4c66a; border-color: rgba(244, 180, 77, .3); background: rgba(244, 180, 77, .1);
}
.ops-dashboard .status-failed { color: #fca5a5; border-color: rgba(248, 113, 113, .28); background: rgba(248, 113, 113, .1); }
.ops-dashboard .category-list { display: flex; flex-wrap: wrap; gap: 5px; }
.ops-dashboard .category-chip { padding: 3px 8px; color: #bdcbdf; border-color: var(--ops-border); background: rgba(148, 163, 184, .07); }
.ops-dashboard .coverage-good { color: #6ee7b7; font-weight: 700; }
.ops-dashboard .coverage-low { color: #fca5a5; font-weight: 700; }
.ops-dashboard .coverage-none { color: var(--ops-muted); }
.ops-dashboard .pager { margin-top: 12px; }
.ops-dashboard .pager .muted { color: var(--ops-muted); font-size: 12px; }
.ops-dashboard .btn { min-width: 74px; min-height: 44px; padding: 0 14px; border-color: var(--ops-border-strong); border-radius: 10px; background: #101b2d; font-size: 12px; font-weight: 650; }
.ops-dashboard .btn:hover:not(:disabled) { border-color: rgba(91, 140, 255, .5); background: rgba(91, 140, 255, .1); }
.ops-dashboard .refresh-note { margin-top: 10px; color: var(--ops-muted); font-size: 11px; }
.ops-dashboard :is(a, button):focus-visible { outline: 3px solid rgba(91, 140, 255, .48); outline-offset: 2px; }
@media (max-width: 1100px) {
  .ops-dashboard .ops-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ops-dashboard .ops-metrics.triage-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .ops-dashboard .ops-grid { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .ops-dashboard .ops-shell { padding: 10px 12px 30px; }
  .ops-dashboard .ops-hero { align-items: flex-start; flex-direction: column; padding: 20px 18px; }
  .ops-dashboard .ops-live { width: 100%; }
  .ops-dashboard .ops-metrics { grid-template-columns: 1fr; gap: 9px; margin-top: 10px; }
  .ops-dashboard .ops-metrics.triage-metrics { grid-template-columns: 1fr; }
  .ops-dashboard .metric-card { padding: 15px 16px; }
  .ops-dashboard .ops-card { margin-top: 10px; padding: 14px; border-radius: 14px; }
  .ops-dashboard .ops-grid { gap: 10px; margin-top: 10px; }
  .ops-dashboard .section-head { flex-direction: column; gap: 5px; }
  .ops-dashboard .chart-wrap { height: 250px; }
  .ops-dashboard .pager { align-items: stretch; flex-direction: column; }
  .ops-dashboard .pager-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ops-dashboard .btn { width: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .ops-dashboard *, .ops-dashboard *::before, .ops-dashboard *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; }
}
.ops-dashboard.ops-dashboard-light {
  --ops-bg: #f8fafc; --ops-surface: #ffffff; --ops-surface-raised: #ffffff;
  --ops-border: #dbe4f0; --ops-border-strong: #bfcee0; --ops-text: #0f172a; --ops-muted: #64748b;
  --ops-blue: #1e40af; --ops-violet: #6d28d9; --ops-green: #15803d; --ops-amber: #b45309; --ops-red: #dc2626;
  color: var(--ops-text); background: var(--ops-bg);
}
.ops-dashboard.ops-dashboard-light .nav-tab:hover { color: var(--ops-text); background: #eef4ff; }
.ops-dashboard.ops-dashboard-light .nav-tab.active {
  color: #fff; border-color: #1e40af; background: #1e40af; box-shadow: 0 1px 2px rgba(15, 23, 42, .1);
}
.ops-dashboard.ops-dashboard-light .ops-hero,
.ops-dashboard.ops-dashboard-light .metric-card,
.ops-dashboard.ops-dashboard-light .ops-card {
  border-color: var(--ops-border); background: #fff; box-shadow: 0 8px 24px rgba(15, 23, 42, .06);
}
.ops-dashboard.ops-dashboard-light .ops-eyebrow { color: #1e40af; }
.ops-dashboard.ops-dashboard-light .ops-live strong { color: var(--ops-text); }
.ops-dashboard.ops-dashboard-light .ops-live-dot { box-shadow: 0 0 0 5px rgba(21, 128, 61, .1); }
.ops-dashboard.ops-dashboard-light .table-wrap { border-color: var(--ops-border); background: #fff; }
.ops-dashboard.ops-dashboard-light .ops-table th { color: #334155; background: #eff6ff; }
.ops-dashboard.ops-dashboard-light .ops-table tbody tr:nth-child(even) { background: #f8fafc; }
.ops-dashboard.ops-dashboard-light .ops-table tbody tr:hover { background: #eff6ff; }
.ops-dashboard.ops-dashboard-light a { color: #1e40af; }
.ops-dashboard.ops-dashboard-light .status-success { color: #166534; border-color: #bbf7d0; background: #f0fdf4; }
.ops-dashboard.ops-dashboard-light .status-warning { color: #92400e; border-color: #fde68a; background: #fffbeb; }
.ops-dashboard.ops-dashboard-light .status-failed { color: #991b1b; border-color: #fecaca; background: #fef2f2; }
.ops-dashboard.ops-dashboard-light .category-chip,
.ops-dashboard.ops-dashboard-light .mem-chip { color: #334155; border-color: #cbd5e1; background: #f8fafc; }
.ops-dashboard.ops-dashboard-light :is(input, select, textarea, .filter-select) {
  min-height: 44px; color: var(--ops-text); border-color: var(--ops-border-strong); background: #fff;
}
.ops-dashboard.ops-dashboard-light :is(button, .btn) { min-height: 44px; }
.ops-dashboard.ops-dashboard-light .btn { color: var(--ops-text); border-color: var(--ops-border-strong); background: #fff; }
.ops-dashboard.ops-dashboard-light .btn:hover:not(:disabled) { border-color: #1e40af; background: #eff6ff; }
.ops-dashboard.ops-dashboard-light :is(a, button, input, select, textarea):focus-visible {
  outline: 3px solid rgba(30, 64, 175, .35); outline-offset: 2px;
}
.ops-dashboard.ops-dashboard-light .retrieval-row,
.ops-dashboard.ops-dashboard-light .mem-card,
.ops-dashboard.ops-dashboard-light .ci-job { border-color: var(--ops-border); background: #fff; }
.ops-dashboard.ops-dashboard-light .retrieval-recalled { color: #166534; border-color: #bbf7d0; background: #f0fdf4; }
.ops-dashboard.ops-dashboard-light .retrieval-no_match { color: #1e40af; border-color: #bfdbfe; background: #eff6ff; }
.ops-dashboard.ops-dashboard-light .retrieval-error { color: #991b1b; border-color: #fecaca; background: #fef2f2; }
.ops-dashboard.ops-dashboard-light .retrieval-not_attempted,
.ops-dashboard.ops-dashboard-light .retrieval-legacy_unknown { color: #475569; border-color: #cbd5e1; background: #f8fafc; }
.ops-dashboard.ops-dashboard-light .retrieval-reason { color: #334155; }
"""

_JS_HELPERS = """
function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}
function pctBadgeClass(pct) {
  if (pct < 20) return 'pct-low';
  if (pct < 50) return 'pct-mid';
  return 'pct-high';
}
function pctBadge(pct) {
  return `<span class="mini-badge ${pctBadgeClass(pct)}">${pct}%</span>`;
}
function createPager({
  rows, tbodyId, pageInfoId, prevBtnId, nextBtnId, pageSize, emptyColspan, renderRow, emptyText, formatPageInfo,
}) {
  let page = 1;
  let currentRows = rows;
  const tbody = document.getElementById(tbodyId);
  const pageInfoEl = document.getElementById(pageInfoId);
  const prevBtn = document.getElementById(prevBtnId);
  const nextBtn = document.getElementById(nextBtnId);
  function render() {
    const totalPages = Math.max(1, Math.ceil(currentRows.length / pageSize));
    page = Math.min(page, totalPages);
    const start = (page - 1) * pageSize;
    const pageRows = currentRows.slice(start, start + pageSize);
    tbody.innerHTML = pageRows.length
      ? pageRows.map(renderRow).join('')
      : `<tr><td colspan="${emptyColspan}" class="muted">${emptyText}</td></tr>`;
    pageInfoEl.textContent = formatPageInfo
      ? formatPageInfo(page, totalPages, currentRows.length)
      : `Page ${page} / ${totalPages} (${currentRows.length} rows)`;
    prevBtn.disabled = page <= 1;
    nextBtn.disabled = page >= totalPages;
  }
  prevBtn.onclick = () => { if (page > 1) { page -= 1; render(); } };
  nextBtn.onclick = () => { page += 1; render(); };
  render();
  return { render, setRows(nextRows) { currentRows = nextRows; page = 1; render(); } };
}
"""


def _feedback_dashboard_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>PR-Agent Feedback Dashboard (Live)</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>{_BASE_CSS}</style>
</head>
<body>
<div class="container">
  <section class="hero">
    <div>
      <h1>PR-Agent Feedback Dashboard</h1>
      <p>Live view -- queries the SQLite feedback DB on every page load.</p>
    </div>
    <div class="stamp"><div>Loaded at</div><strong id="loadedAt">--</strong></div>
  </section>

  <section class="metrics">
    <div class="card"><div class="metric-label">Total feedback</div><div class="metric-value" id="mTotal">--</div></div>
    <div class="card"><div class="metric-label">Average score</div><div class="metric-value" id="mAvg">--</div></div>
    <div class="card"><div class="metric-label">Median score</div><div class="metric-value" id="mMedian">--</div></div>
    <div class="card"><div class="metric-label">Positive rate (4-5)</div><div class="metric-value" id="mPositive">--</div></div>
  </section>

  <section class="grid-2">
    <div class="card">
      <h2 class="section-title">Score distribution</h2>
      <div class="chart-wrap"><canvas id="scoreChart"></canvas></div>
    </div>
    <div class="card">
      <h2 class="section-title">Weekly trend</h2>
      <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
    </div>
  </section>

  <section class="card" style="margin-top: 18px;">
    <h2 class="section-title">Project summary table</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Project</th><th>Count</th><th>Avg</th><th>Min</th><th>Max</th></tr></thead>
        <tbody id="projectTableBody"><tr><td colspan="5" class="muted">Loading...</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <div id="projectPageInfo" class="muted">Page 1</div>
      <div class="pager-actions">
        <button id="projectPrevPage" class="btn" type="button">Previous</button>
        <button id="projectNextPage" class="btn" type="button">Next</button>
      </div>
    </div>
  </section>

  <section class="card" style="margin-top: 18px;">
    <h2 class="section-title">All feedback</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Score</th><th>User</th><th>Project</th><th>Comment</th><th>Link</th></tr></thead>
        <tbody id="feedbackTableBody"><tr><td colspan="6" class="muted">Loading...</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <div id="feedbackPageInfo" class="muted">Page 1</div>
      <div class="pager-actions">
        <button id="feedbackPrevPage" class="btn" type="button">Previous</button>
        <button id="feedbackNextPage" class="btn" type="button">Next</button>
      </div>
    </div>
    <div class="refresh-note">Refresh the page to reload the latest data.</div>
  </section>
</div>

<script>
{_JS_HELPERS}

function scoreBadge(score) {{
  return `<span class="mini-badge score-${{score}}">${{score}}/5</span>`;
}}

async function init() {{
  const res = await fetch('/api/feedback/summary?days=0');
  const data = await res.json();

  document.getElementById('loadedAt').textContent = new Date().toLocaleString();
  document.getElementById('mTotal').textContent = data.total;
  document.getElementById('mAvg').textContent = (data.avg || 0).toFixed(2);
  document.getElementById('mMedian').textContent = data.median ?? 'N/A';
  const positive = data.total ? Math.round(((data.dist_values[3] + data.dist_values[4]) / data.total) * 100) : 0;
  document.getElementById('mPositive').textContent = positive + '%';

  new Chart(document.getElementById('scoreChart'), {{
    type: 'doughnut',
    data: {{ labels: data.dist_labels, datasets: [{{ data: data.dist_values,
      backgroundColor: ['#f87171', '#fb923c', '#fbbf24', '#34d399', '#60a5fa'],
      borderColor: '#0f172a', borderWidth: 4, hoverOffset: 8 }}] }},
    options: {{ responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ labels: {{ color: '#cbd5e1' }} }} }} }},
  }});

  new Chart(document.getElementById('trendChart'), {{
    type: 'line',
    data: {{ labels: data.week_labels, datasets: [{{ label: 'Average score', data: data.week_values,
      borderColor: '#a78bfa', backgroundColor: 'rgba(167, 139, 250, 0.18)', fill: true,
      tension: 0.35, pointRadius: 4 }}] }},
    options: {{ responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ labels: {{ color: '#cbd5e1' }} }} }},
      scales: {{ x: {{ ticks: {{ color: '#94a3b8' }} }}, y: {{ min: 0, max: 5, ticks: {{ color: '#94a3b8' }} }} }} }},
  }});

  createPager({{
    rows: data.project_rows, tbodyId: 'projectTableBody', pageInfoId: 'projectPageInfo',
    prevBtnId: 'projectPrevPage', nextBtnId: 'projectNextPage', pageSize: 10, emptyColspan: 5,
    emptyText: 'No project data',
    renderRow: (row) => `
      <tr><td>${{escapeHtml(row.project)}}</td><td>${{row.count}}</td><td>${{row.avg}}</td>
      <td>${{row.min}}</td><td>${{row.max}}</td></tr>`,
  }});

  createPager({{
    rows: data.all_rows, tbodyId: 'feedbackTableBody', pageInfoId: 'feedbackPageInfo',
    prevBtnId: 'feedbackPrevPage', nextBtnId: 'feedbackNextPage', pageSize: 10, emptyColspan: 6,
    emptyText: 'No feedback yet',
    renderRow: (row) => `
      <tr><td>${{escapeHtml(row.created_at)}}</td><td>${{scoreBadge(row.score)}}</td>
      <td>${{escapeHtml(row.reviewer_user)}}</td><td>${{escapeHtml(row.project)}}</td>
      <td class="comment-cell">${{escapeHtml(row.comment) || '<span class="muted">\u2014</span>'}}</td>
      <td class="link-cell">${{row.pr_url ? `<a href="${{escapeHtml(row.pr_url)}}" target="_blank" rel="noreferrer">\u67e5\u770b</a>` : ''}}</td></tr>`,
  }});
}}

init();
</script>
</body>
</html>
"""


def _nav_bar(active: str, compact: bool = False) -> str:
    """顶部导航栏，当前页高亮。"""
    labels = ("行内建议", "CI 失败分析", "CI 诊断", "建议审查", "修复经验") if compact else (
        "Inline Suggestion", "CI Failures", "CI Triage", "Suggestion Filter", "Repair Memory",
    )
    tabs = [("inline", labels[0], "/dashboard/inline"),
            ("ci-failures", labels[1], "/dashboard/ci-failures"),
            ("triage", labels[2], "/dashboard/triage"),
            ("filter", labels[3], "/dashboard/suggestion-filter"),
            ("memory", labels[4], "/dashboard/repair-memory")]
    links = []
    for key, label, href in tabs:
        cls = "nav-tab active" if key == active else "nav-tab"
        current = ' aria-current="page"' if key == active else ""
        links.append(f'<a class="{cls}" href="{href}"{current}>{label}</a>')
    return '<nav class="nav-bar" aria-label="看板导航">' + "".join(links) + "</nav>"


def _inline_dashboard_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>行内建议看板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>{_BASE_CSS}{_OPERATIONS_DASHBOARD_CSS}</style>
</head>
<body class="ops-dashboard">
<main class="ops-shell">
{_nav_bar("inline", compact=True)}
  <section class="ops-hero">
    <div>
      <div class="ops-eyebrow">Suggestion Operations</div>
      <h1>行内建议看板</h1>
      <p>查看建议的发布、采纳与用户反馈情况。页面刷新时读取最新数据。</p>
    </div>
    <div class="ops-live"><span class="ops-live-dot" aria-hidden="true"></span><div>数据已加载<strong id="loadedAt">--</strong></div></div>
  </section>

  <section class="ops-metrics" aria-label="核心指标">
    <div class="metric-card" style="--metric-accent: var(--ops-blue)"><div class="metric-label">已发布建议</div><div class="metric-value" id="mPub">--</div></div>
    <div class="metric-card" style="--metric-accent: var(--ops-green)"><div class="metric-label">已采纳</div><div class="metric-value" id="mApp">--</div></div>
    <div class="metric-card" style="--metric-accent: var(--ops-violet)"><div class="metric-label">整体采纳率</div><div class="metric-value" id="mPct">--</div></div>
    <div class="metric-card" style="--metric-accent: var(--ops-amber)"><div class="metric-label">用户反馈</div><div class="metric-value" id="mFb">--</div></div>
  </section>

  <section class="ops-grid">
    <div class="ops-card">
      <div class="section-head"><div><h2 class="section-title">建议数量最多的项目</h2><div class="section-subtitle">按已发布建议数量排序</div></div><span class="section-kicker">PROJECTS</span></div>
      <div class="chart-wrap"><canvas id="projectChart"></canvas></div>
    </div>
    <div class="ops-card">
      <div class="section-head"><div><h2 class="section-title">每周采纳趋势</h2><div class="section-subtitle">采纳率变化，范围 0–100%</div></div><span class="section-kicker">WEEKLY</span></div>
      <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
    </div>
  </section>

  <section class="ops-card">
    <div class="section-head"><div><h2 class="section-title">项目汇总</h2><div class="section-subtitle">各项目建议发布与采纳情况</div></div><span class="section-kicker">SUMMARY</span></div>
    <div class="table-wrap">
      <table class="ops-table">
        <thead><tr><th>项目</th><th class="num">已发布</th><th class="num">已采纳</th><th class="num">采纳率</th></tr></thead>
        <tbody id="projectTableBody"><tr><td colspan="4" class="muted">加载中...</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <div id="projectPageInfo" class="muted">第 1 页</div>
      <div class="pager-actions">
        <button id="projectPrevPage" class="btn" type="button" aria-label="项目汇总上一页">上一页</button>
        <button id="projectNextPage" class="btn" type="button" aria-label="项目汇总下一页">下一页</button>
      </div>
    </div>
  </section>

  <section class="ops-card">
    <div class="section-head"><div><h2 class="section-title">MR 明细</h2><div class="section-subtitle">逐条查看发布、采纳、负责人和时间</div></div><span class="section-kicker">MERGE REQUESTS</span></div>
    <div class="table-wrap">
      <table class="ops-table wide">
        <thead><tr><th>MR</th><th class="num">已发布</th><th class="num">已采纳</th><th class="num">采纳率</th><th>时间</th><th>负责人</th><th>链接</th></tr></thead>
        <tbody id="mrTableBody"><tr><td colspan="7" class="muted">加载中...</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <div id="mrPageInfo" class="muted">第 1 页</div>
      <div class="pager-actions">
        <button id="mrPrevPage" class="btn" type="button" aria-label="MR 明细上一页">上一页</button>
        <button id="mrNextPage" class="btn" type="button" aria-label="MR 明细下一页">下一页</button>
      </div>
    </div>
  </section>

  <section class="ops-card">
    <div class="section-head"><div><h2 class="section-title">用户反馈</h2><div class="section-subtitle">建议使用者提交的反馈内容</div></div><span class="section-kicker">FEEDBACK</span></div>
    <div class="table-wrap">
      <table class="ops-table wide">
        <thead><tr><th>时间</th><th>用户</th><th>MR</th><th>反馈内容</th><th>链接</th></tr></thead>
        <tbody id="feedbackTableBody"><tr><td colspan="5" class="muted">加载中...</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <div id="feedbackPageInfo" class="muted">第 1 页</div>
      <div class="pager-actions">
        <button id="feedbackPrevPage" class="btn" type="button" aria-label="用户反馈上一页">上一页</button>
        <button id="feedbackNextPage" class="btn" type="button" aria-label="用户反馈下一页">下一页</button>
      </div>
    </div>
    <div class="refresh-note">刷新页面可重新读取最新数据。</div>
  </section>
</main>

<script>
{_JS_HELPERS}

async function init() {{
  const res = await fetch('/api/inline/summary');
  const data = await res.json();

  document.getElementById('loadedAt').textContent = new Date().toLocaleString();
  document.getElementById('mPub').textContent = data.pub_total;
  document.getElementById('mApp').textContent = data.app_total;
  document.getElementById('mPct').textContent = data.overall_pct + '%';
  document.getElementById('mFb').textContent = data.fb_rows.length;

  new Chart(document.getElementById('projectChart'), {{
    type: 'bar',
    data: {{ labels: data.dist_labels, datasets: [{{ label: '已发布建议', data: data.dist_values,
      backgroundColor: 'rgba(91, 140, 255, 0.78)', borderColor: '#7ca2ff', borderWidth: 1,
      borderRadius: 5, barThickness: 18 }}] }},
    options: {{ responsive: true, maintainAspectRatio: false, indexAxis: 'y',
      layout: {{ padding: {{ right: 8 }} }},
      plugins: {{ legend: {{ display: false }}, tooltip: {{ backgroundColor: '#162238', titleColor: '#edf4ff',
        bodyColor: '#c8d5e8', borderColor: 'rgba(148,163,184,.22)', borderWidth: 1 }} }},
      scales: {{ x: {{ beginAtZero: true, ticks: {{ color: '#8fa1ba', precision: 0 }},
        grid: {{ color: 'rgba(148,163,184,.10)' }}, border: {{ display: false }} }},
        y: {{ ticks: {{ color: '#aab9cd', autoSkip: false }}, grid: {{ display: false }},
        border: {{ display: false }} }} }} }},
  }});

  new Chart(document.getElementById('trendChart'), {{
    type: 'line',
    data: {{ labels: data.week_labels, datasets: [{{ label: '采纳率 %', data: data.week_values,
      borderColor: '#9b7cff', backgroundColor: 'rgba(155, 124, 255, 0.12)', fill: true,
      tension: 0.35, pointRadius: 3, pointHoverRadius: 5, pointBackgroundColor: '#b29cff', borderWidth: 2 }}] }},
    options: {{ responsive: true, maintainAspectRatio: false,
      interaction: {{ intersect: false, mode: 'index' }},
      plugins: {{ legend: {{ labels: {{ color: '#aab9cd', usePointStyle: true, boxWidth: 8 }} }},
        tooltip: {{ backgroundColor: '#162238', titleColor: '#edf4ff', bodyColor: '#c8d5e8',
        borderColor: 'rgba(148,163,184,.22)', borderWidth: 1 }} }},
      scales: {{ x: {{ ticks: {{ color: '#8fa1ba', maxRotation: 0 }}, grid: {{ display: false }},
        border: {{ display: false }} }}, y: {{ min: 0, max: 100,
        ticks: {{ color: '#8fa1ba', callback: (value) => value + '%' }},
        grid: {{ color: 'rgba(148,163,184,.10)' }}, border: {{ display: false }} }} }} }},
  }});

  createPager({{
    rows: data.project_rows, tbodyId: 'projectTableBody', pageInfoId: 'projectPageInfo',
    prevBtnId: 'projectPrevPage', nextBtnId: 'projectNextPage', pageSize: 10, emptyColspan: 4,
    emptyText: '暂无项目数据',
    formatPageInfo: (page, totalPages, total) => `第 ${{page}} / ${{totalPages}} 页（共 ${{total}} 条）`,
    renderRow: (row) => `
      <tr><td>${{escapeHtml(row.project)}}</td><td class="num mono">${{row.pub}}</td><td class="num mono">${{row.app}}</td>
      <td class="num">${{pctBadge(row.pct)}}</td></tr>`,
  }});

  createPager({{
    rows: data.mr_rows, tbodyId: 'mrTableBody', pageInfoId: 'mrPageInfo',
    prevBtnId: 'mrPrevPage', nextBtnId: 'mrNextPage', pageSize: 10, emptyColspan: 7,
    emptyText: '暂无匹配的 MR',
    formatPageInfo: (page, totalPages, total) => `第 ${{page}} / ${{totalPages}} 页（共 ${{total}} 条）`,
    renderRow: (row) => `
      <tr><td>${{escapeHtml(row.mr)}}</td><td class="num mono">${{row.pub}}</td><td class="num mono">${{row.app}}</td>
      <td class="num">${{pctBadge(row.pct)}}</td><td class="mono">${{escapeHtml(row.ts)}}</td>
      <td>${{row.owner ? escapeHtml(row.owner) : '<span class="muted">\u2014</span>'}}</td>
      <td class="link-cell">${{row.link ? `<a href="${{escapeHtml(row.link)}}" target="_blank" rel="noreferrer">\u67e5\u770b</a>` : '<span class="muted">\u2014</span>'}}</td></tr>`,
  }});

  createPager({{
    rows: data.fb_rows, tbodyId: 'feedbackTableBody', pageInfoId: 'feedbackPageInfo',
    prevBtnId: 'feedbackPrevPage', nextBtnId: 'feedbackNextPage', pageSize: 10, emptyColspan: 5,
    emptyText: '暂无用户反馈',
    formatPageInfo: (page, totalPages, total) => `第 ${{page}} / ${{totalPages}} 页（共 ${{total}} 条）`,
    renderRow: (row) => `
      <tr><td class="mono">${{escapeHtml(row.ts)}}</td><td>@${{escapeHtml(row.user)}}</td>
      <td>${{escapeHtml(row.mr)}}</td>
      <td class="comment-cell">${{escapeHtml(row.comment) || '<span class="muted">\u2014</span>'}}</td>
      <td class="link-cell">${{row.link ? `<a href="${{escapeHtml(row.link)}}" target="_blank" rel="noreferrer">\u67e5\u770b</a>` : '<span class="muted">\u2014</span>'}}</td></tr>`,
  }});
}}

init();
</script>
</body>
</html>
"""


def _triage_dashboard_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>CI 诊断看板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>{_BASE_CSS}{_OPERATIONS_DASHBOARD_CSS}
.triage-run-row {{ cursor: pointer; transition: background .16s ease, box-shadow .16s ease; }}
.triage-run-row:hover, .triage-run-row:focus-visible,
.triage-run-row[aria-expanded="true"] {{ background: rgba(91,140,255,.07); }}
.triage-run-row:focus-visible {{ outline: 2px solid var(--ops-blue); outline-offset: -2px; }}
.triage-disclosure {{ display: inline-block; margin-left: 8px; color: var(--ops-blue); transition: transform .16s ease; }}
.triage-run-row[aria-expanded="true"] .triage-disclosure {{ transform: rotate(180deg); }}
.triage-detail-row > td {{ padding: 0; background: rgba(8,15,30,.35); }}
.triage-detail-shell {{ padding: 14px; border-top: 1px solid var(--ops-border); }}
.triage-detail-frame {{ display: block; width: 100%; min-height: 280px; border: 0; border-radius: 10px; background: #f6f8fb; }}
.triage-detail-frame[hidden] {{ display: none; }}
.triage-detail-state {{ padding: 28px; color: var(--ops-muted); text-align: center; }}
.triage-detail-retry {{ display: block; margin: 12px auto 0; }}
.triage-pager {{ gap: 12px; flex-wrap: wrap; }}
.triage-page-numbers {{ display: flex; gap: 6px; flex-wrap: wrap; justify-content: center; }}
.triage-page-number {{ min-width: 38px; padding-inline: 10px; }}
.triage-page-number[aria-current="page"] {{ color: #fff; border-color: rgba(91,140,255,.72); background: rgba(91,140,255,.32); }}
.triage-pager .btn:disabled {{ cursor: not-allowed; opacity: .45; }}
@media (max-width: 720px) {{
  .triage-pager {{ align-items: stretch; }}
  .triage-page-numbers {{ order: 3; width: 100%; }}
}}
</style>
</head>
<body class="ops-dashboard">
<main class="ops-shell">
{_nav_bar("triage", compact=True)}
  <section class="ops-hero">
    <div>
      <div class="ops-eyebrow">Continuous Integration</div>
      <h1>CI 诊断看板</h1>
      <p>汇总最近 30 天的自动修复结果、失败类别与处理效率。页面刷新时读取最新数据。</p>
    </div>
    <div class="ops-live"><span class="ops-live-dot" aria-hidden="true"></span><div>数据已加载<strong id="loadedAt">--</strong></div></div>
  </section>

  <section class="ops-metrics triage-metrics" aria-label="核心指标">
    <div class="metric-card" style="--metric-accent: var(--ops-blue)"><div class="metric-label">执行总数</div><div class="metric-value" id="mTotal">--</div></div>
    <div class="metric-card" style="--metric-accent: var(--ops-green)"><div class="metric-label">修复成功率</div><div class="metric-value" id="mSR">--</div></div>
    <div class="metric-card" style="--metric-accent: var(--ops-amber)"><div class="metric-label">外部依赖阻塞</div><div class="metric-value" id="mBlocked">--</div></div>
    <div class="metric-card" style="--metric-accent: var(--ops-violet)"><div class="metric-label">平均迭代次数</div><div class="metric-value" id="mIters">--</div></div>
    <div class="metric-card" style="--metric-accent: var(--ops-amber)"><div class="metric-label">平均耗时</div><div class="metric-value" id="mDur">--</div></div>
  </section>

  <section class="ops-grid">
    <div class="ops-card">
      <div class="section-head"><div><h2 class="section-title">各失败类别成功率</h2><div class="section-subtitle">按 CI 失败类别对比自动修复效果</div></div><span class="section-kicker">CATEGORY</span></div>
      <div class="chart-wrap"><canvas id="catChart"></canvas></div>
    </div>
    <div class="ops-card">
      <div class="section-head"><div><h2 class="section-title">每周成功率趋势</h2><div class="section-subtitle">最近 30 天的周度变化</div></div><span class="section-kicker">WEEKLY</span></div>
      <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
    </div>
  </section>

  <section class="ops-card">
    <div class="section-head"><div><h2 class="section-title">最近执行记录</h2><div class="section-subtitle">按接口返回顺序展示，不做二次排序</div></div><span class="section-kicker">RECENT RUNS</span></div>
    <div class="table-wrap">
      <table class="ops-table wide">
        <thead><tr><th>时间</th><th>项目</th><th>MR</th><th>作者（GitLab）</th><th>修复类别</th><th>修复结果</th><th>流水线</th><th class="num">覆盖率</th><th class="num">迭代</th><th class="num">耗时</th><th>链接</th></tr></thead>
        <tbody id="runsBody"></tbody>
      </table>
    </div>
    <div class="pager triage-pager" aria-label="最近执行记录分页">
      <div id="triagePageInfo" class="muted" aria-live="polite">第 1 页</div>
      <div id="triagePageNumbers" class="triage-page-numbers" aria-label="页码"></div>
      <div class="pager-actions">
        <button id="triagePrevPage" class="btn" type="button">上一页</button>
        <button id="triageNextPage" class="btn" type="button">下一页</button>
      </div>
    </div>
  </section>
</main>
<script>
{_JS_HELPERS}

let expandedRepair = null;

function closeRepairDetail() {{
  if (!expandedRepair) return;
  expandedRepair.summaryRow.setAttribute('aria-expanded', 'false');
  expandedRepair.detailRow.remove();
  expandedRepair = null;
}}

function detailState(text, retry) {{
  const root = document.createElement('div');
  root.className = 'triage-detail-state';
  root.textContent = text;
  if (retry) {{
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn triage-detail-retry';
    button.textContent = '重新加载';
    button.addEventListener('click', event => {{ event.stopPropagation(); retry(); }});
    root.appendChild(button);
  }}
  return root;
}}

function loadRepairDetail(shell, iframe, row) {{
  shell.replaceChildren();
  const loading = detailState('正在加载修复详情…');
  iframe.hidden = true;
  const retry = () => loadRepairDetail(shell, iframe, row);
  iframe.onload = () => {{
    loading.remove();
    iframe.hidden = false;
  }};
  iframe.onerror = () => shell.replaceChildren(detailState('暂时无法读取修复详情。', retry));
  shell.append(loading, iframe);
  iframe.src = row.detail_url;
}}

function toggleRepairDetail(summaryRow, row) {{
  if (expandedRepair?.summaryRow === summaryRow) {{
    closeRepairDetail();
    return;
  }}
  closeRepairDetail();
  const detailRow = document.createElement('tr');
  detailRow.className = 'triage-detail-row';
  detailRow.id = summaryRow.getAttribute('aria-controls');
  const cell = document.createElement('td');
  cell.colSpan = 11;
  const shell = document.createElement('div');
  shell.className = 'triage-detail-shell';
  cell.appendChild(shell);
  detailRow.appendChild(cell);
  summaryRow.after(detailRow);
  summaryRow.setAttribute('aria-expanded', 'true');
  if (!row.detail_available) {{
    shell.appendChild(detailState(row.detail_unavailable_reason || '该记录生成时未保存修复详情。'));
    expandedRepair = {{ summaryRow, detailRow, frame: null, taskId: row.task_id || '' }};
    return;
  }}
  const iframe = document.createElement('iframe');
  iframe.className = 'triage-detail-frame';
  iframe.title = `CI 修复详情：${{row.project}} !${{row.mr_iid}}`;
  iframe.loading = "lazy";
  iframe.dataset.taskId = row.task_id;
  expandedRepair = {{ summaryRow, detailRow, frame: iframe, taskId: row.task_id }};
  loadRepairDetail(shell, iframe, row);
}}

window.addEventListener('message', event => {{
  if (event.origin !== window.location.origin || !expandedRepair || !expandedRepair.frame) return;
  const data = event.data || {{}};
  if (data.type !== 'repair-detail-height' || data.taskId !== expandedRepair.taskId) return;
  const height = Math.max(280, Math.min(6000, Number(data.height) || 0));
  expandedRepair.frame.style.height = `${{height}}px`;
}});

const coverageReasons = {{
  not_configured: '未配置覆盖率任务',
  job_failed: '覆盖率任务失败，未生成报告',
  report_missing: '覆盖率报告缺失',
  fetch_failed: '覆盖率读取失败',
  validation_pipeline_missing: '未找到验证流水线',
}};
const categoryLabels = {{
  unit_test: '单元测试', format: 'Format', clang: 'Clang', build: 'Build', unknown: 'Unknown',
}};
let recentRequestId = 0;
let currentRecentPage = 1;
let currentRecentData = null;

function renderRecentRows(data) {{
  closeRepairDetail();
  const tb = document.getElementById('runsBody');
  tb.replaceChildren();
  if (!data.recent_rows.length) {{
    const empty = document.createElement('tr');
    empty.innerHTML = '<td colspan="11" class="muted">暂无执行记录</td>';
    tb.appendChild(empty);
    return;
  }}
  data.recent_rows.forEach((r, index) => {{
    const tr = document.createElement('tr');
    const hasCoverage = r.coverage != null;
    const cov = hasCoverage ? r.coverage.toFixed(2) + '%' : (coverageReasons[r.coverage_status] || '未提供');
    const covClass = hasCoverage ? (r.coverage >= 80 ? 'coverage-good' : 'coverage-low') : 'coverage-none';
    const categories = r.cats.map(cat => `<span class="category-chip">${{escapeHtml(categoryLabels[cat] || cat)}}</span>`).join('');
    const blockerTitle = escapeHtml(r.blocker_summary);
    const status = ['success', 'succeeded'].includes(r.repair_outcome)
      ? '<span class="status-badge status-success">成功</span>'
      : ['partial_success', 'partial', 'unverified'].includes(r.repair_outcome)
        ? '<span class="status-badge status-warning">部分成功</span>'
        : r.repair_outcome === 'blocked'
          ? `<span class="status-badge status-warning" title="${{blockerTitle}}">外部依赖阻塞</span>`
          : '<span class="status-badge status-failed">失败</span>';
    const pipelineStatus = r.pipeline_status === 'success'
      ? '<span class="status-badge status-success">通过</span>'
      : r.pipeline_status === 'unknown'
        ? '<span class="status-badge">未知</span>'
        : '<span class="status-badge status-failed">仍失败</span>';
    tr.innerHTML = `<td class="mono">${{escapeHtml(r.ts)}}</td><td>${{escapeHtml(r.project)}}</td>
      <td class="mono">!${{escapeHtml(r.mr_iid)}}</td><td>${{escapeHtml(r.actor)}}</td>
      <td><div class="category-list">${{categories || '<span class="muted">—</span>'}}</div></td>
      <td>${{status}}</td><td>${{pipelineStatus}}</td><td class="num mono ${{covClass}}">${{cov}}</td><td class="num mono">${{r.iters}}</td>
      <td class="num mono">${{(r.dur_ms / 1000).toFixed(1)}}s</td><td class="link-cell">${{r.url ? `<a href="${{escapeHtml(r.url)}}" target="_blank" rel="noreferrer">查看</a>` : '<span class="muted">—</span>'}}<span class="triage-disclosure" aria-hidden="true">⌄</span></td>`;
    tr.classList.add('triage-run-row');
    tr.tabIndex = 0;
    tr.setAttribute('role', 'button');
    tr.setAttribute('aria-expanded', 'false');
    tr.setAttribute('aria-controls', `triage-detail-${{data.recent_page}}-${{index}}`);
    tr.addEventListener('click', () => toggleRepairDetail(tr, r));
    tr.addEventListener('keydown', event => {{
      if (event.key === 'Enter' || event.key === ' ') {{
        event.preventDefault();
        toggleRepairDetail(tr, r);
      }}
    }});
    tr.querySelectorAll('a').forEach(link => {{
      link.addEventListener('click', event => event.stopPropagation());
      link.addEventListener('keydown', event => event.stopPropagation());
    }});
    tb.appendChild(tr);
  }});
}}

function renderRecentPager(data) {{
  const page = data.recent_page;
  const totalPages = data.recent_total_pages;
  currentRecentPage = page;
  currentRecentData = data;
  document.getElementById('triagePageInfo').textContent = totalPages
    ? `第 ${{page}}/${{totalPages}} 页，共 ${{data.recent_total}} 条`
    : '共 0 条';
  document.getElementById('triagePrevPage').disabled = page <= 1;
  document.getElementById('triageNextPage').disabled = !totalPages || page >= totalPages;

  const numbers = document.getElementById('triagePageNumbers');
  numbers.replaceChildren();
  const start = Math.max(1, page - 2);
  const end = Math.min(totalPages, page + 2);
  for (let value = start; value <= end; value += 1) {{
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn triage-page-number';
    button.textContent = String(value);
    button.disabled = value === page;
    if (value === page) button.setAttribute('aria-current', 'page');
    button.addEventListener('click', () => loadRecentPage(value));
    numbers.appendChild(button);
  }}
}}

async function loadRecentPage(page) {{
  const requestId = ++recentRequestId;
  closeRepairDetail();
  const info = document.getElementById('triagePageInfo');
  const previous = document.getElementById('triagePrevPage');
  const next = document.getElementById('triageNextPage');
  info.textContent = '正在加载…';
  previous.disabled = true;
  next.disabled = true;
  try {{
    const response = await fetch(`/api/triage/summary?days=30&page=${{encodeURIComponent(page)}}`);
    if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
    const data = await response.json();
    if (requestId !== recentRequestId) return;
    renderRecentRows(data);
    renderRecentPager(data);
  }} catch (error) {{
    if (requestId !== recentRequestId) return;
    if (currentRecentData) renderRecentPager(currentRecentData);
    info.textContent = currentRecentData
      ? `第 ${{currentRecentPage}} 页 · 加载失败，请重试`
      : '加载失败，请重试';
  }}
}}

async function init() {{
  document.getElementById('loadedAt').textContent = new Date().toLocaleString('zh-CN');
  document.getElementById('triagePrevPage').addEventListener('click', () => loadRecentPage(currentRecentPage - 1));
  document.getElementById('triageNextPage').addEventListener('click', () => loadRecentPage(currentRecentPage + 1));
  try {{
    const r = await fetch('/api/triage/summary?days=30&page=1');
    if (!r.ok) throw new Error(`HTTP ${{r.status}}`);
    const d = await r.json();
    document.getElementById('mTotal').textContent = d.total;
    document.getElementById('mSR').textContent = d.success_rate + '%';
    document.getElementById('mBlocked').textContent = d.blocked_count || 0;
    document.getElementById('mIters').textContent = d.avg_iterations;
    document.getElementById('mDur').textContent = (d.avg_duration_ms / 1000).toFixed(1) + 's';

    new Chart(document.getElementById('catChart'), {{
      type: 'bar',
      data: {{ labels: d.cat_labels, datasets: [{{ label: '成功率 %', data: d.cat_sr,
        backgroundColor: 'rgba(91, 140, 255, 0.78)', borderColor: '#7ca2ff', borderWidth: 1,
        borderRadius: 5, maxBarThickness: 34 }}] }},
      options: {{ responsive: true, maintainAspectRatio: false,
        plugins: {{ legend: {{ display: false }}, tooltip: {{ backgroundColor: '#162238', titleColor: '#edf4ff',
          bodyColor: '#c8d5e8', borderColor: 'rgba(148,163,184,.22)', borderWidth: 1 }} }},
        scales: {{ x: {{ ticks: {{ color: '#8fa1ba', maxRotation: 0 }}, grid: {{ display: false }},
          border: {{ display: false }} }}, y: {{ beginAtZero: true, max: 100,
          ticks: {{ color: '#8fa1ba', callback: (value) => value + '%' }},
          grid: {{ color: 'rgba(148,163,184,.10)' }}, border: {{ display: false }} }} }} }}
    }});

    new Chart(document.getElementById('trendChart'), {{
      type: 'line',
      data: {{ labels: d.week_labels, datasets: [{{ label: '成功率 %', data: d.week_values,
        borderColor: '#35c793', backgroundColor: 'rgba(53, 199, 147, 0.10)', fill: true,
        tension: 0.35, pointRadius: 3, pointHoverRadius: 5, pointBackgroundColor: '#6ee7b7', borderWidth: 2 }}] }},
      options: {{ responsive: true, maintainAspectRatio: false, interaction: {{ intersect: false, mode: 'index' }},
        plugins: {{ legend: {{ labels: {{ color: '#aab9cd', usePointStyle: true, boxWidth: 8 }} }},
          tooltip: {{ backgroundColor: '#162238', titleColor: '#edf4ff', bodyColor: '#c8d5e8',
          borderColor: 'rgba(148,163,184,.22)', borderWidth: 1 }} }},
        scales: {{ x: {{ ticks: {{ color: '#8fa1ba', maxRotation: 0 }}, grid: {{ display: false }},
          border: {{ display: false }} }}, y: {{ beginAtZero: true, max: 100,
          ticks: {{ color: '#8fa1ba', callback: (value) => value + '%' }},
          grid: {{ color: 'rgba(148,163,184,.10)' }}, border: {{ display: false }} }} }} }}
    }});

    renderRecentRows(d);
    renderRecentPager(d);
  }} catch (e) {{
    document.getElementById('mTotal').textContent = '异常';
    document.getElementById('triagePageInfo').textContent = '加载失败，请刷新页面重试';
  }}
}}
init();
</script>
</body>
</html>
"""


def _ci_failure_dashboard_html() -> str:
    return render_ci_failure_dashboard(
        _BASE_CSS,
        _OPERATIONS_DASHBOARD_CSS,
        _nav_bar("ci-failures", compact=True),
        _JS_HELPERS,
    )


def _repair_memory_dashboard_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>修复经验看板</title>
<style>{_BASE_CSS}{_OPERATIONS_DASHBOARD_CSS}
.mem-card {{ margin-top: 12px; padding: 0; overflow: hidden; border: 1px solid var(--ops-border); border-radius: 14px; background: rgba(14, 23, 40, .9); box-shadow: 0 8px 24px rgba(0,0,0,.1); transition: border-color .2s ease, transform .2s ease, box-shadow .2s ease; }}
.mem-card:hover {{ transform: translateY(-1px); border-color: rgba(91,140,255,.38); box-shadow: 0 12px 30px rgba(0,0,0,.14); }}
.mem-card-toggle {{ display: block; width: 100%; min-height: 44px; padding: 16px 18px; border: 0; color: inherit; background: transparent; text-align: left; cursor: pointer; appearance: none; }}
.mem-card-toggle:active {{ background: rgba(91,140,255,.06); }}
.mem-card-toggle:focus-visible, .mem-page-button:focus-visible {{ outline: 3px solid rgba(91,140,255,.35); outline-offset: -3px; }}
.mem-card-head {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
.mem-scope-badge {{ display: inline-flex; align-items: center; padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; }}
.mem-scope-project {{ color: #8ab0ff; border: 1px solid rgba(91,140,255,.3); background: rgba(91,140,255,.1); }}
.mem-scope-global {{ color: #c4a3ff; border: 1px solid rgba(155,124,255,.3); background: rgba(155,124,255,.1); }}
.mem-status-active {{ color: #82e3be; border: 1px solid rgba(53,199,147,.28); background: rgba(53,199,147,.1); }}
.mem-status-needs_review {{ color: #f4b44d; border: 1px solid rgba(244,180,77,.28); background: rgba(244,180,77,.1); }}
.mem-status-disabled {{ color: #fca5a5; border: 1px solid rgba(248,113,113,.28); background: rgba(248,113,113,.1); }}
.mem-status-superseded {{ color: var(--ops-muted); border: 1px solid var(--ops-border); background: rgba(148,163,184,.07); }}
.mem-conf-bar {{ height: 5px; border-radius: 3px; background: rgba(148,163,184,.15); overflow: hidden; margin-top: 10px; }}
.mem-conf-fill {{ height: 100%; border-radius: 3px; }}
.mem-conf-high {{ background: linear-gradient(90deg, #10b981, #34d399); }}
.mem-conf-mid {{ background: linear-gradient(90deg, #f4b44d, #fbbf24); }}
.mem-conf-low {{ background: linear-gradient(90deg, #f87171, #fca5a5); }}
.mem-problem {{ margin-top: 11px; color: var(--ops-text); font-size: 14px; font-weight: 650; line-height: 1.55; overflow-wrap: anywhere; }}
.mem-problem-clamp {{ display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }}
.mem-chips {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }}
.mem-chip {{ display: inline-flex; padding: 3px 8px; border: 1px solid rgba(148,163,184,.16); border-radius: 7px; color: #aebbd0; background: rgba(148,163,184,.07); font-size: 11px; font-weight: 650; }}
.mem-summary-foot {{ display: flex; align-items: center; gap: 8px 16px; flex-wrap: wrap; margin-top: 11px; color: var(--ops-muted); font-size: 11px; }}
.mem-summary-foot strong {{ color: #c7d4e7; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
.mem-expand-label {{ margin-left: auto; color: #8ab0ff; font-weight: 700; }}
.mem-expand-icon {{ display: inline-block; margin-left: 5px; transition: transform .2s ease; }}
.mem-card-toggle[aria-expanded="true"] .mem-expand-icon {{ transform: rotate(180deg); }}
.mem-card-details {{ padding: 2px 18px 18px; border-top: 1px solid var(--ops-border); background: rgba(8,15,27,.36); }}
.mem-card-details[hidden] {{ display: none; }}
.mem-field {{ margin-top: 12px; font-size: 13px; line-height: 1.65; overflow-wrap: anywhere; }}
.mem-field-label {{ color: var(--ops-muted); font-weight: 650; margin-right: 6px; }}
.mem-field-value {{ color: var(--ops-text); }}
.mem-meta {{ display: flex; gap: 18px; flex-wrap: wrap; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--ops-border); font-size: 12px; color: var(--ops-muted); }}
.mem-meta strong {{ color: #c7d4e7; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
.filter-bar {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 14px; }}
.filter-bar select {{ min-width: 130px; }}
.mem-empty {{ margin-top: 18px; padding: 40px; text-align: center; color: var(--ops-muted); font-size: 14px; border: 1px dashed var(--ops-border); border-radius: 14px; }}
.mem-card-actions {{ display: flex; justify-content: flex-end; margin-top: 14px; }}
.mem-action-danger {{ color: #fecaca; border-color: rgba(248,113,113,.38); background: rgba(248,113,113,.1); }}
.mem-action-restore {{ color: #a7f3d0; border-color: rgba(53,199,147,.38); background: rgba(53,199,147,.1); }}
.mem-notice {{ min-height: 20px; margin-top: 10px; color: var(--ops-muted); font-size: 12px; }}
.mem-notice-error {{ color: #fca5a5; }}
.mem-pagination {{ display: flex; justify-content: center; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 18px; }}
.mem-pagination[hidden] {{ display: none; }}
.mem-page-summary {{ width: 100%; color: var(--ops-muted); font-size: 12px; text-align: center; }}
.mem-page-buttons {{ display: inline-flex; align-items: center; justify-content: center; gap: 8px; flex-wrap: wrap; }}
.mem-page-button {{ min-width: 44px; min-height: 44px; padding: 7px 10px; border: 1px solid var(--ops-border); border-radius: 9px; color: #b8c5d8; background: rgba(14,23,40,.76); font: inherit; font-size: 12px; cursor: pointer; }}
.mem-page-button:hover:not(:disabled) {{ color: #fff; border-color: rgba(91,140,255,.45); background: rgba(91,140,255,.14); }}
.mem-page-button:active:not(:disabled) {{ background: rgba(91,140,255,.24); }}
.mem-page-button[aria-current="page"] {{ color: #fff; border-color: rgba(91,140,255,.68); background: rgba(91,140,255,.26); }}
.mem-page-button:disabled {{ cursor: not-allowed; opacity: .42; }}
.mem-page-gap {{ padding: 0 2px; color: var(--ops-muted); }}
.retrieval-list {{ display: grid; gap: 10px; }}
.retrieval-row {{ padding: 14px 16px; border: 1px solid var(--ops-border); border-radius: 12px; background: rgba(14,23,40,.76); }}
.retrieval-head {{ display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }}
.retrieval-title {{ color: var(--ops-text); font-size: 14px; font-weight: 700; }}
.retrieval-status {{ display: inline-flex; padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 700; border: 1px solid var(--ops-border); }}
.retrieval-recalled {{ color: #82e3be; border-color: rgba(53,199,147,.3); background: rgba(53,199,147,.1); }}
.retrieval-no_match {{ color: #8ab0ff; border-color: rgba(91,140,255,.3); background: rgba(91,140,255,.1); }}
.retrieval-error {{ color: #fca5a5; border-color: rgba(248,113,113,.3); background: rgba(248,113,113,.1); }}
.retrieval-not_attempted, .retrieval-legacy_unknown {{ color: var(--ops-muted); background: rgba(148,163,184,.07); }}
.retrieval-meta {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 8px; color: var(--ops-muted); font-size: 12px; }}
.retrieval-reason {{ margin-top: 8px; color: #b8c5d8; font-size: 12px; }}
.retrieval-memories {{ margin-top: 8px; color: var(--ops-text); font-size: 12px; line-height: 1.55; }}
.retrieval-candidates {{ margin-top: 10px; border-top: 1px solid var(--ops-border); }}
.retrieval-candidates summary {{ padding: 10px 0 2px; color: #8ab0ff; font-size: 12px; font-weight: 700; cursor: pointer; }}
.retrieval-candidate-list {{ display: grid; gap: 8px; margin-top: 9px; }}
.retrieval-candidate {{ padding: 10px 12px; border: 1px solid var(--ops-border); border-radius: 9px; background: rgba(2,6,23,.22); }}
.retrieval-candidate-head {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
.retrieval-candidate-name {{ min-width: 0; color: var(--ops-text); font-size: 12px; font-weight: 700; overflow-wrap: anywhere; }}
.retrieval-candidate-state {{ padding: 2px 7px; border-radius: 999px; font-size: 10px; font-weight: 700; }}
.retrieval-candidate-selected {{ color: #82e3be; background: rgba(53,199,147,.12); }}
.retrieval-candidate-passed_not_selected {{ color: #fcd34d; background: rgba(245,158,11,.12); }}
.retrieval-candidate-rejected {{ color: #fca5a5; background: rgba(248,113,113,.12); }}
.retrieval-candidate-score {{ margin-left: auto; color: var(--ops-text); font-size: 12px; font-weight: 700; }}
.retrieval-candidate-meta {{ margin-top: 6px; color: var(--ops-muted); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }}
.ops-dashboard-light .mem-card:hover {{ border-color: #93b4e8; box-shadow: 0 10px 26px rgba(15,23,42,.08); }}
.ops-dashboard-light .mem-card-details {{ background: #f8fafc; }}
.ops-dashboard-light .mem-summary-foot strong,.ops-dashboard-light .mem-meta strong {{ color: #0f172a; }}
.ops-dashboard-light .mem-expand-label {{ color: #1e40af; }}
.ops-dashboard-light .mem-page-button {{ color: #334155; border-color: #bfcee0; background: #fff; }}
.ops-dashboard-light .mem-page-button:hover:not(:disabled) {{ color: #1e40af; border-color: #1e40af; background: #eff6ff; }}
.ops-dashboard-light .mem-page-button[aria-current="page"] {{ color: #fff; border-color: #1e40af; background: #1e40af; }}
.ops-dashboard-light .mem-scope-project {{ color: #1e40af; border-color: #bfdbfe; background: #eff6ff; }}
.ops-dashboard-light .mem-scope-global {{ color: #6d28d9; border-color: #ddd6fe; background: #f5f3ff; }}
.ops-dashboard-light .mem-status-active {{ color: #166534; border-color: #bbf7d0; background: #f0fdf4; }}
.ops-dashboard-light .mem-status-needs_review {{ color: #92400e; border-color: #fde68a; background: #fffbeb; }}
.ops-dashboard-light .mem-status-disabled {{ color: #991b1b; border-color: #fecaca; background: #fef2f2; }}
.ops-dashboard-light .mem-action-danger {{ color: #991b1b; border-color: #fecaca; background: #fef2f2; }}
.ops-dashboard-light .mem-action-restore {{ color: #166534; border-color: #bbf7d0; background: #f0fdf4; }}
.ops-dashboard-light .retrieval-candidates summary {{ color: #1e40af; }}
.ops-dashboard-light .retrieval-candidate {{ border-color: #dbe4f0; background: #f8fafc; }}
.ops-dashboard-light .retrieval-candidate-selected {{ color: #166534; background: #dcfce7; }}
.ops-dashboard-light .retrieval-candidate-passed_not_selected {{ color: #92400e; background: #fef3c7; }}
.ops-dashboard-light .retrieval-candidate-rejected {{ color: #991b1b; background: #fee2e2; }}
@media (max-width: 640px) {{
  .mem-card-toggle, .mem-card-details {{ padding-left: 14px; padding-right: 14px; }}
  .mem-expand-label {{ width: 100%; margin-left: 0; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .mem-card, .mem-card-toggle, .mem-expand-icon {{ transition: none; }}
}}
</style>
</head>
<body class="ops-dashboard ops-dashboard-light">
<main class="ops-shell">
{_nav_bar("memory", compact=True)}
  <section class="ops-hero">
    <div>
      <div class="ops-eyebrow">Repair Memory</div>
      <h1>修复经验看板</h1>
      <p>展示系统从被 Pipeline 验证成功的修复中学到的经验卡片。项目记忆优先，全局记忆需两个独立项目验证后去标识化提升。</p>
    </div>
    <div class="ops-live" aria-live="polite"><span class="ops-live-dot" aria-hidden="true"></span><div>数据状态<strong id="loadedAt">正在加载...</strong></div></div>
  </section>

  <section class="ops-metrics" aria-label="核心指标">
    <div class="metric-card" style="--metric-accent: var(--ops-blue)"><div class="metric-label">当前有效经验</div><div class="metric-value" id="mMem">--</div></div>
    <div class="metric-card" style="--metric-accent: var(--ops-green)"><div class="metric-label">命中一次通过率</div><div class="metric-value" id="mRate">--</div></div>
    <div class="metric-card" style="--metric-accent: var(--ops-violet)"><div class="metric-label">记忆辅助尝试</div><div class="metric-value" id="mAttempt">--</div></div>
    <div class="metric-card" style="--metric-accent: var(--ops-amber)"><div class="metric-label">待复核记忆</div><div class="metric-value" id="mReview">--</div></div>
  </section>

  <section class="ops-card" id="memoryCardsSection">
    <div class="section-head"><div><h2 class="section-title">经验卡片</h2><div class="section-subtitle">默认仅展示当前有效的中文版本，按置信度降序排列</div></div><span class="section-kicker">MEMORIES</span></div>
    <div class="filter-bar">
      <select class="filter-select" id="fScope"><option value="">全部范围</option><option value="project">项目</option><option value="global">全局</option></select>
      <select class="filter-select" id="fStatus"><option value="active">当前有效</option><option value="all">全部状态</option><option value="needs_review">needs_review</option><option value="disabled">disabled</option><option value="superseded">superseded</option></select>
      <input class="filter-select" id="fProject" placeholder="项目名（如 group/a）" style="min-width:200px" />
      <button class="btn" id="fApply">筛选</button>
    </div>
    <div class="mem-notice">
      删除为可恢复的软删除，只影响之后启动的检索；已经进入运行中 Agent 上下文的经验不会被中途撤回。
    </div>
    <div class="mem-notice" id="memActionNotice" role="status" aria-live="polite"></div>
    <div id="memList"></div>
    <nav class="mem-pagination" id="memPagination" aria-label="经验卡片分页" hidden>
      <div class="mem-page-summary" id="memPageSummary"></div>
      <div class="mem-page-buttons" id="memPageButtons"></div>
    </nav>
  </section>

  <section class="ops-card" id="retrievalAuditSection">
    <div class="section-head"><div><h2 class="section-title">最近检索记录</h2><div class="section-subtitle">明确区分未检索、检索无匹配、成功召回和检索异常</div></div><span class="section-kicker">RETRIEVALS</span></div>
    <div class="retrieval-list" id="retrievalAuditList" role="status" aria-live="polite"><div class="mem-empty">正在加载检索记录...</div></div>
    <nav class="mem-pagination" id="retrievalPagination" aria-label="最近检索记录分页" hidden>
      <div class="mem-page-summary" id="retrievalPageInfo"></div>
      <div class="mem-page-buttons"><button class="mem-page-button" id="retrievalPrev" type="button">上一页</button><button class="mem-page-button" id="retrievalNext" type="button">下一页</button></div>
    </nav>
  </section>
</main>
<script>
{_JS_HELPERS}

const MEMORY_PAGE_SIZE = 10;
const RETRIEVAL_PAGE_SIZE = 15;
let memoryRows = [];
let currentMemoryPage = 1;
let expandedMemoryId = '';
let currentRetrievalPage = 1;
let retrievalPageData = null;

const retrievalStatusLabels = {{
  not_attempted: '未执行检索',
  no_match: '已检索，无匹配经验',
  error: '检索异常',
  legacy_unknown: '历史数据未知',
}};
const retrievalReasonLabels = {{
  repair_session_not_reached: '修复流程尚未进入记忆检索阶段',
  memory_mode_off: '记忆检索已关闭',
  project_not_allowed: '当前项目不在检索范围内',
  format_only_repair: '本次仅执行格式修复',
  no_candidates: '当前项目和通用记忆中没有候选经验',
  below_threshold: '存在候选经验，但没有达到召回阈值',
  selected: '已有经验通过召回条件',
  retrieval_error: '检索过程发生异常',
  duplicate_suppressed: '同一根因已召回，跳过重复检索',
  legacy_no_audit: '该任务早于检索审计功能',
}};
const candidateDecisionLabels = {{
  selected: '已召回',
  passed_not_selected: '已过阈值，未选入',
  rejected: '未通过',
}};
const candidateReasonLabels = {{
  semantic_below_threshold: '语义相似度不足',
  total_below_threshold: '总分未达到阈值',
}};
const candidateComponentLabels = [
  ['semantic_points', '语义'],
  ['exact_fingerprint', '诊断指纹'],
  ['failure_family', '失败类型'],
  ['causal_tokens', '根因词'],
  ['language', '语言'],
  ['build_system', '构建系统'],
  ['project_scope', '项目范围'],
  ['confidence_freshness', '置信度与时效'],
];

function retrievalStatusLabel(audit) {{
  if (audit.status === 'recalled') {{
    if (audit.injected_count > 0) return '已召回，已注入 Hermes';
    return audit.mode === 'shadow' ? '已召回，仅影子评估' : '已召回，未注入 Hermes';
  }}
  return retrievalStatusLabels[audit.status] || audit.status;
}}

function renderRetrievalCandidate(candidate) {{
  const score = candidate.score || {{}};
  const threshold = Number(score.effective_min_score || 0);
  const total = Number(candidate.total_score || 0);
  const decision = candidateDecisionLabels[candidate.decision] || candidate.decision || '—';
  const rejection = candidateReasonLabels[candidate.rejection_reason] || candidate.rejection_reason || '';
  const title = candidate.problem_pattern || candidate.memory_id || '未知经验';
  const components = candidateComponentLabels.map(([key, label]) =>
    `${{label}} ${{Number(score[key] || 0)}}`
  ).join(' · ');
  let semantic = '';
  if (candidate.semantic_similarity !== null && candidate.semantic_similarity !== undefined) {{
    const similarity = Number(candidate.semantic_similarity).toFixed(3);
    const semanticThreshold = Number(score.semantic_min_similarity || 0).toFixed(3);
    semantic = ` · 语义相似度 ${{similarity}} / 门槛 ${{semanticThreshold}}`;
  }}
  return `<div class="retrieval-candidate">
    <div class="retrieval-candidate-head">
      <span class="retrieval-candidate-name">${{escapeHtml(title)}}</span>
      <span class="retrieval-candidate-state retrieval-candidate-${{escapeHtml(candidate.decision)}}">${{escapeHtml(decision)}}</span>
      <span class="retrieval-candidate-score">总分 ${{total}} / 阈值 ${{threshold}}</span>
    </div>
    <div class="retrieval-candidate-meta">${{rejection ? `${{escapeHtml(rejection)}} · ` : ''}}${{escapeHtml(candidate.scoring_mode || '—')}}${{escapeHtml(semantic)}}</div>
    <div class="retrieval-candidate-meta">得分构成：${{escapeHtml(components)}}</div>
    <div class="retrieval-candidate-meta mono">${{escapeHtml(candidate.memory_id || '')}}</div>
  </div>`;
}}

function renderRetrievalAudit(audit) {{
  const status = retrievalStatusLabel(audit);
  const reason = retrievalReasonLabels[audit.reason_code] || audit.reason_code || '—';
  const timestamp = audit.updated_at ? new Date(audit.updated_at).toLocaleString('zh-CN') : '—';
  const pipeline = audit.source_pipeline_id ? `Pipeline #${{audit.source_pipeline_id}}` : 'Pipeline —';
  const outcome = audit.final_repair_outcome || '处理中';
  const errorCode = audit.error_code ? ` · ${{escapeHtml(audit.error_code)}}` : '';
  const memories = (audit.recalled_memories || []).map(memory =>
    `<div>• ${{escapeHtml(memory.problem_pattern || memory.memory_id)}}</div>`
  ).join('');
  const candidateScores = (audit.candidate_scores || []).map(renderRetrievalCandidate).join('');
  const candidates = candidateScores
    ? `<details class="retrieval-candidates"><summary>查看候选评分（${{audit.candidate_scores.length}}）</summary><div class="retrieval-candidate-list">${{candidateScores}}</div></details>`
    : '';
  return `<article class="retrieval-row">
    <div class="retrieval-head">
      <span class="retrieval-title">${{escapeHtml(audit.project || '—')}} !${{escapeHtml(audit.mr_iid || '—')}}</span>
      <span class="retrieval-status retrieval-${{escapeHtml(audit.status)}}">${{escapeHtml(status)}}</span>
      <span class="mono" style="margin-left:auto;color:var(--ops-muted);font-size:12px">${{escapeHtml(timestamp)}}</span>
    </div>
    <div class="retrieval-meta">
      <span>${{escapeHtml(pipeline)}}</span><span>检索 ${{audit.search_count || 0}} 次</span>
      <span>候选 ${{audit.candidate_count || 0}} / 通过阈值 ${{audit.passed_threshold_count || 0}} / 召回 ${{audit.selected_count || 0}} / 注入 ${{audit.injected_count || 0}}</span>
      <span>修复结果：${{escapeHtml(outcome)}}</span>
    </div>
    <div class="retrieval-reason">${{escapeHtml(reason)}}${{errorCode}}</div>
    ${{memories ? `<div class="retrieval-memories">${{memories}}</div>` : ''}}
    ${{candidates}}
  </article>`;
}}

function renderRetrievalPagination(data) {{
  const pagination = document.getElementById('retrievalPagination');
  const total = data.total || 0;
  pagination.hidden = total === 0;
  document.getElementById('retrievalPageInfo').textContent = total
    ? `第 ${{data.page}} / ${{data.total_pages}} 页，共 ${{total}} 条`
    : '';
  document.getElementById('retrievalPrev').disabled = total === 0 || data.page <= 1;
  document.getElementById('retrievalNext').disabled = total === 0 || data.page >= data.total_pages;
}}

async function loadRetrievalAudits(scrollToSection = false) {{
  const list = document.getElementById('retrievalAuditList');
  document.getElementById('retrievalPrev').disabled = true;
  document.getElementById('retrievalNext').disabled = true;
  try {{
    const params = new URLSearchParams();
    params.set('page', String(currentRetrievalPage));
    params.set('page_size', String(RETRIEVAL_PAGE_SIZE));
    const response = await fetch('/api/repair-memory/retrieval-audits?' + params.toString());
    if (!response.ok) throw new Error('retrieval audit request failed');
    const data = await response.json();
    if (data.total_pages > 0 && currentRetrievalPage > data.total_pages) {{
      currentRetrievalPage = data.total_pages;
      return loadRetrievalAudits(scrollToSection);
    }}
    if (data.total_pages === 0) currentRetrievalPage = 1;
    retrievalPageData = data;
    const audits = data.audits || [];
    list.innerHTML = audits.length
      ? audits.map(renderRetrievalAudit).join('')
      : '<div class="mem-empty">暂无检索记录。</div>';
    renderRetrievalPagination(data);
    if (scrollToSection) {{
      const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      document.getElementById('retrievalAuditSection').scrollIntoView({{behavior: reduceMotion ? 'auto' : 'smooth', block: 'start'}});
    }}
  }} catch (error) {{
    list.innerHTML = '<div class="mem-empty">加载失败，请刷新重试。</div>';
    if (retrievalPageData) renderRetrievalPagination(retrievalPageData);
  }}
}}

function changeRetrievalPage(delta) {{
  if (!retrievalPageData) return;
  const target = currentRetrievalPage + delta;
  if (target < 1 || target > retrievalPageData.total_pages) return;
  currentRetrievalPage = target;
  loadRetrievalAudits(true);
}}

function confClass(c) {{
  if (c >= 0.7) return 'mem-conf-high';
  if (c >= 0.45) return 'mem-conf-mid';
  return 'mem-conf-low';
}}
function statusClass(s) {{
  return 'mem-status-' + s;
}}
function renderCard(m) {{
  const scopeLabel = m.scope === 'global' ? '全局' : '项目';
  const scopeCls = m.scope === 'global' ? 'mem-scope-global' : 'mem-scope-project';
  const confPct = Math.round(m.confidence * 100);
  const scopeKeyDisplay = m.scope === 'global' ? '' : escapeHtml(m.scope_key);
  const expanded = expandedMemoryId === m.memory_id;
  const applicability = m.applicability.map(a => `<div class="mem-field-value">• ${{escapeHtml(a)}}</div>`).join('');
  const anti = m.anti_conditions.map(a => `<div class="mem-field-value">• ${{escapeHtml(a)}}</div>`).join('');
  const validation = m.validation_guidance.map(v => `<div class="mem-field-value">• ${{escapeHtml(v)}}</div>`).join('');
  const action = m.status === 'disabled' ? 'enable' : (['active', 'needs_review'].includes(m.status) ? 'disable' : '');
  const actionLabel = action === 'enable' ? '恢复' : '删除';
  const actionClass = action === 'enable' ? 'mem-action-restore' : 'mem-action-danger';
  const actionButton = action
    ? `<div class="mem-card-actions">
        <button class="btn ${{actionClass}}" type="button" data-memory-action="${{action}}"
          data-memory-id="${{escapeHtml(m.memory_id)}}">${{actionLabel}}</button>
      </div>`
    : '';
  const memoryId = escapeHtml(m.memory_id);
  return `<article class="mem-card" data-memory-card="${{memoryId}}">
    <button class="mem-card-toggle" type="button" data-memory-toggle="${{memoryId}}"
      aria-expanded="${{expanded}}" aria-controls="memory-details-${{memoryId}}">
      <div class="mem-card-head">
        <span class="mem-scope-badge ${{scopeCls}}">${{scopeLabel}}</span>
        ${{scopeKeyDisplay ? `<span class="mono" style="color:var(--ops-muted);font-size:12px;overflow-wrap:anywhere">${{scopeKeyDisplay}}</span>` : ''}}
        <span class="mem-scope-badge ${{statusClass(m.status)}}">${{escapeHtml(m.status)}}</span>
        <span style="margin-left:auto;color:var(--ops-muted);font-size:12px">置信度 <strong style="color:var(--ops-text)">${{confPct}}%</strong></span>
      </div>
      <div class="mem-conf-bar"><div class="mem-conf-fill ${{confClass(m.confidence)}}" style="width:${{confPct}}%"></div></div>
      <div class="mem-problem mem-problem-clamp">${{escapeHtml(m.problem_pattern)}}</div>
      <div class="mem-chips">
        ${{m.failure_family ? `<span class="mem-chip">${{escapeHtml(m.failure_family)}}</span>` : ''}}
        ${{m.language ? `<span class="mem-chip">${{escapeHtml(m.language)}}</span>` : ''}}
        ${{m.build_system ? `<span class="mem-chip">${{escapeHtml(m.build_system)}}</span>` : ''}}
      </div>
      <div class="mem-summary-foot">
        <span>支持 <strong>${{m.support_episode_count}}</strong> 个案例 / <strong>${{m.support_project_count}}</strong> 个项目</span>
        <span>最后强化 <strong>${{escapeHtml(m.last_reinforced_at || '—')}}</strong></span>
        <span class="mem-expand-label">${{expanded ? '收起详情' : '展开详情'}}<span class="mem-expand-icon" aria-hidden="true">⌄</span></span>
      </div>
    </button>
    <div class="mem-card-details" id="memory-details-${{memoryId}}" ${{expanded ? '' : 'hidden'}}>
      <div class="mem-field"><span class="mem-field-label">问题模式</span><span class="mem-field-value">${{escapeHtml(m.problem_pattern)}}</span></div>
      ${{applicability ? `<div class="mem-field"><span class="mem-field-label">适用条件</span>${{applicability}}</div>` : ''}}
      ${{anti ? `<div class="mem-field"><span class="mem-field-label">反条件</span>${{anti}}</div>` : ''}}
      ${{m.repair_guidance ? `<div class="mem-field"><span class="mem-field-label">修复原则</span><span class="mem-field-value">${{escapeHtml(m.repair_guidance)}}</span></div>` : ''}}
      ${{validation ? `<div class="mem-field"><span class="mem-field-label">验证建议</span>${{validation}}</div>` : ''}}
      <div class="mem-meta">
        <span>支持 <strong>${{m.support_episode_count}}</strong> episodes / <strong>${{m.support_project_count}}</strong> projects</span>
        <span>结算 <strong>${{m.settled_attempts}}</strong> 次 / 成功 <strong>${{m.immediate_successes}}</strong> 次</span>
        <span>创建 <strong>${{escapeHtml(m.created_at || '—')}}</strong></span>
        <span>最后强化 <strong>${{escapeHtml(m.last_reinforced_at || '—')}}</strong></span>
      </div>
      ${{actionButton}}
    </div>
  </article>`;
}}

function toggleMemoryDetails(memoryId) {{
  expandedMemoryId = expandedMemoryId === memoryId ? '' : memoryId;
  renderMemoryList();
}}

function memoryPageItems(page, pageCount) {{
  const pages = new Set([1, pageCount]);
  for (let candidate = page - 2; candidate <= page + 2; candidate += 1) {{
    if (candidate >= 1 && candidate <= pageCount) pages.add(candidate);
  }}
  const ordered = [...pages].sort((a, b) => a - b);
  const items = [];
  ordered.forEach((value, index) => {{
    if (index > 0 && value - ordered[index - 1] > 1) items.push('gap-' + value);
    items.push(value);
  }});
  return items;
}}

function renderMemoryPagination(pageCount) {{
  const pagination = document.getElementById('memPagination');
  const buttons = document.getElementById('memPageButtons');
  if (!memoryRows.length || pageCount <= 0) {{
    pagination.hidden = true;
    buttons.innerHTML = '';
    return;
  }}
  pagination.hidden = false;
  document.getElementById('memPageSummary').textContent = `共 ${{memoryRows.length}} 条 · 第 ${{currentMemoryPage}} / ${{pageCount}} 页`;
  const pageButtons = memoryPageItems(currentMemoryPage, pageCount).map(item => {{
    if (typeof item === 'string') return '<span class="mem-page-gap" aria-hidden="true">…</span>';
    const current = item === currentMemoryPage ? ' aria-current="page"' : '';
    return `<button class="mem-page-button" type="button" data-memory-page="${{item}}"${{current}}>${{item}}</button>`;
  }}).join('');
  buttons.innerHTML = `<button class="mem-page-button" type="button" data-memory-page="${{currentMemoryPage - 1}}" ${{currentMemoryPage === 1 ? 'disabled' : ''}}>上一页</button>${{pageButtons}}<button class="mem-page-button" type="button" data-memory-page="${{currentMemoryPage + 1}}" ${{currentMemoryPage === pageCount ? 'disabled' : ''}}>下一页</button>`;
  buttons.querySelectorAll('[data-memory-page]').forEach(button => {{
    button.onclick = () => changeMemoryPage(Number(button.dataset.memoryPage));
  }});
}}

function bindMemoryInteractions() {{
  const list = document.getElementById('memList');
  list.querySelectorAll('[data-memory-toggle]').forEach(button => {{
    button.onclick = () => toggleMemoryDetails(button.dataset.memoryToggle);
  }});
  list.querySelectorAll('[data-memory-action]').forEach(button => {{
    button.onclick = () => changeMemoryStatus(button.dataset.memoryId, button.dataset.memoryAction);
  }});
}}

function renderMemoryList() {{
  const list = document.getElementById('memList');
  if (!memoryRows.length) {{
    list.innerHTML = '<div class="mem-empty">暂无经验卡片。启用 capture 并等待修复成功后会出现。</div>';
    renderMemoryPagination(0);
    return;
  }}
  const pageCount = Math.ceil(memoryRows.length / MEMORY_PAGE_SIZE);
  currentMemoryPage = Math.min(Math.max(currentMemoryPage, 1), pageCount);
  const offset = (currentMemoryPage - 1) * MEMORY_PAGE_SIZE;
  list.innerHTML = memoryRows.slice(offset, offset + MEMORY_PAGE_SIZE).map(renderCard).join('');
  bindMemoryInteractions();
  renderMemoryPagination(pageCount);
}}

function changeMemoryPage(page) {{
  const pageCount = Math.max(1, Math.ceil(memoryRows.length / MEMORY_PAGE_SIZE));
  currentMemoryPage = Math.min(Math.max(page, 1), pageCount);
  expandedMemoryId = '';
  renderMemoryList();
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.getElementById('memoryCardsSection').scrollIntoView({{behavior: reduceMotion ? 'auto' : 'smooth', block: 'start'}});
}}

function setMemoryNotice(message, isError = false) {{
  const notice = document.getElementById('memActionNotice');
  notice.textContent = message;
  notice.classList.toggle('mem-notice-error', isError);
}}

async function changeMemoryStatus(memoryId, action) {{
  const actionLabel = action === 'enable' ? '恢复' : '删除';
  if (!window.confirm(`确认${{actionLabel}}这条经验吗？`)) return;
  const input = window.prompt(`请填写${{actionLabel}}原因（必填，1-500 字）：`);
  if (input === null) return;
  const reason = input.trim();
  if (!reason || reason.length > 500) {{
    setMemoryNotice('请输入 1-500 字的操作原因。', true);
    return;
  }}
  setMemoryNotice(`${{actionLabel}}处理中...`);
  try {{
    const response = await fetch(`/api/repair-memory/memories/${{encodeURIComponent(memoryId)}}/${{action}}`, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{reason}}),
    }});
    if (!response.ok) throw new Error('memory status request failed');
    setMemoryNotice(`${{actionLabel}}成功。`);
    await loadMemories(false);
  }} catch (error) {{
    setMemoryNotice(`${{actionLabel}}失败，请稍后重试。`, true);
  }}
}}

async function loadMemories(resetView = true) {{
  const scope = document.getElementById('fScope').value;
  const status = document.getElementById('fStatus').value;
  const project = document.getElementById('fProject').value.trim();
  const params = new URLSearchParams();
  if (scope) params.set('scope', scope);
  if (status) params.set('status', status);
  if (project) params.set('project', project);
  try {{
    const r = await fetch('/api/repair-memory/memories?' + params.toString());
    const d = await r.json();
    memoryRows = d.memories || [];
    if (resetView) currentMemoryPage = 1;
    expandedMemoryId = '';
    renderMemoryList();
  }} catch (e) {{
    memoryRows = [];
    expandedMemoryId = '';
    document.getElementById('memPagination').hidden = true;
    document.getElementById('memList').innerHTML = '<div class="mem-empty">加载失败，请刷新重试。</div>';
  }}
}}

async function init() {{
  document.getElementById('loadedAt').textContent = new Date().toLocaleString('zh-CN');
  try {{
    const r = await fetch('/api/repair-memory/effectiveness');
    const d = await r.json();
    document.getElementById('mMem').textContent = (d.active_project_memories || 0) + (d.active_global_memories || 0);
    document.getElementById('mRate').textContent = (d.immediate_success_rate || 0) + '%';
    document.getElementById('mAttempt').textContent = d.settled_pipeline_attempts || 0;
    document.getElementById('mReview').textContent = d.needs_review || 0;
  }} catch (e) {{
    document.getElementById('mMem').textContent = '异常';
  }}
  await loadMemories();
  await loadRetrievalAudits();
  document.getElementById('fApply').onclick = () => loadMemories();
  document.getElementById('retrievalPrev').onclick = () => changeRetrievalPage(-1);
  document.getElementById('retrievalNext').onclick = () => changeRetrievalPage(1);
}}
init();
</script>
</body>
</html>
"""


def _legacy_suggestion_filter_dashboard_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Suggestion Filter Dashboard (Live)</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>{_BASE_CSS}
.nav-bar {{ display: flex; gap: 8px; padding: 10px 0 16px; }}
.nav-tab {{ padding: 8px 16px; border-radius: 999px; font-size: 14px; font-weight: 600; color: var(--muted); border: 1px solid var(--line); background: var(--panel); }}
.nav-tab.active {{ color: var(--text); background: linear-gradient(135deg, rgba(96,165,250,.3), rgba(167,139,250,.2)); border-color: rgba(96,165,250,.5); }}
.nav-tab:hover {{ color: var(--text); }}
details {{ margin-top: 8px; }}
details summary {{ cursor: pointer; color: var(--muted); font-size: 13px; }}
details[open] summary {{ color: var(--text); margin-bottom: 6px; }}
.content-pre {{ white-space: pre-wrap; word-break: break-word; line-height: 1.55; font-size: 13px; color: var(--text); background: rgba(2,6,23,0.4); padding: 10px; border-radius: 8px; }}
.reason-badge {{ display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; background: rgba(239,68,68,0.18); color: #fca5a5; border: 1px solid rgba(239,68,68,0.3); }}</style>
</head>
<body>
<div class="container">
{_nav_bar("filter")}
  <section class="hero">
    <div>
      <h1>Suggestion Review Coverage</h1>
      <p>全部 MR 的建议审查覆盖率，以及二次审查过滤明细。原 Suggestion Filter 看板已原地升级。</p>
    </div>
    <div class="stamp"><div>Loaded at</div><strong id="loadedAt">--</strong></div>
  </section>

  <section class="metrics">
    <div class="card"><div class="metric-label">GitLab MRs</div><div class="metric-value" id="mInventory">--</div></div>
    <div class="card"><div class="metric-label">Review triggered</div><div class="metric-value" id="mTriggered">--</div></div>
    <div class="card"><div class="metric-label">Review completed</div><div class="metric-value" id="mCompleted">--</div></div>
    <div class="card"><div class="metric-label">MRs with published suggestions</div><div class="metric-value" id="mPublishedMR">--</div></div>
    <div class="card"><div class="metric-label">Needs attention</div><div class="metric-value" id="mAttention">--</div></div>
  </section>

  <section class="card" style="margin-top: 18px;">
    <h2 class="section-title">MR review coverage</h2>
    <p class="section-subtitle" id="visibilityNote">仅统计当前 GitLab token 有权限访问的项目。</p>
    <p class="refresh-note" id="syncNote">Waiting for inventory synchronization...</p>
    <p class="refresh-note" id="statusSummary"></p>
    <div>
      <select id="projectFilter" class="filter-select"><option value="">全部项目</option></select>
      <select id="statusFilter" class="filter-select"><option value="">全部状态</option></select>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>MR</th><th>Title / Owner</th><th>Status</th><th>Stage</th><th>Generated</th><th>Filtered</th><th>Published</th><th>Created</th><th>Link</th></tr></thead>
        <tbody id="reviewMrTableBody"><tr><td colspan="9" class="muted">Loading...</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <div id="reviewMrPageInfo" class="muted">Page 1</div>
      <div class="pager-actions">
        <button id="reviewMrPrevPage" class="btn" type="button">Previous</button>
        <button id="reviewMrNextPage" class="btn" type="button">Next</button>
      </div>
    </div>
  </section>

  <section class="metrics">
    <div class="card"><div class="metric-label">Published suggestions</div><div class="metric-value" id="mPub">--</div></div>
    <div class="card"><div class="metric-label">Filtered by cross-review</div><div class="metric-value" id="mFiltered">--</div></div>
    <div class="card"><div class="metric-label">Filter rate</div><div class="metric-value" id="mRate">--</div></div>
    <div class="card"><div class="metric-label">MRs with filtered</div><div class="metric-value" id="mMR">--</div></div>
  </section>

  <section class="grid-2">
    <div class="card">
      <h2 class="section-title">Filter reason distribution</h2>
      <div class="chart-wrap"><canvas id="reasonChart"></canvas></div>
    </div>
    <div class="card">
      <h2 class="section-title">Weekly filter rate trend</h2>
      <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
    </div>
  </section>

  <section class="card" style="margin-top: 18px;">
    <h2 class="section-title">Project summary</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Project</th><th>Published</th><th>Filtered</th><th>Filter rate</th><th>MRs</th></tr></thead>
        <tbody id="projectTableBody"><tr><td colspan="5" class="muted">Loading...</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <div id="projectPageInfo" class="muted">Page 1</div>
      <div class="pager-actions">
        <button id="projectPrevPage" class="btn" type="button">Previous</button>
        <button id="projectNextPage" class="btn" type="button">Next</button>
      </div>
    </div>
  </section>

  <section class="card" style="margin-top: 18px;">
    <h2 class="section-title">MR filter-result explorer</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>MR</th><th>Published</th><th>Filtered</th><th>Filter rate</th><th>Time</th><th>Owner</th><th>Link</th></tr></thead>
        <tbody id="mrTableBody"><tr><td colspan="7" class="muted">Loading...</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <div id="mrPageInfo" class="muted">Page 1</div>
      <div class="pager-actions">
        <button id="mrPrevPage" class="btn" type="button">Previous</button>
        <button id="mrNextPage" class="btn" type="button">Next</button>
      </div>
    </div>
  </section>

  <section class="card" style="margin-top: 18px;">
    <h2 class="section-title">Filtered suggestions detail</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Project</th><th>MR</th><th>File</th><th>Label</th><th>Score</th><th>Reason</th><th>Link</th></tr></thead>
        <tbody id="filteredTableBody"><tr><td colspan="8" class="muted">Loading...</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <div id="filteredPageInfo" class="muted">Page 1</div>
      <div class="pager-actions">
        <button id="filteredPrevPage" class="btn" type="button">Previous</button>
        <button id="filteredNextPage" class="btn" type="button">Next</button>
      </div>
    </div>
    <div class="refresh-note">Click a row to expand suggestion content. Refresh the page to reload the latest data.</div>
  </section>
</div>

<script>
{_JS_HELPERS}

async function init() {{
  try {{
    const res = await fetch('/api/suggestion-review/summary?days=30');
    const data = await res.json();

    document.getElementById('loadedAt').textContent = new Date().toLocaleString('zh-CN');
    document.getElementById('mInventory').textContent = data.inventory_total;
    document.getElementById('mTriggered').textContent = data.triggered_total;
    document.getElementById('mCompleted').textContent = data.completed_total;
    document.getElementById('mPublishedMR').textContent = data.published_mr_total;
    document.getElementById('mAttention').textContent = data.attention_total;
    document.getElementById('visibilityNote').textContent = data.visibility_note;
    const sync = data.sync || {{}};
    document.getElementById('syncNote').textContent = sync.last_error
      ? `Last sync: ${{sync.last_success_at || 'none'}} · Error: ${{sync.last_error}}`
      : `Last sync: ${{sync.last_success_at || 'pending'}} · Daily reconcile: ${{sync.last_reconcile_at || 'pending'}}`;
    document.getElementById('mPub').textContent = data.pub_total;
    document.getElementById('mFiltered').textContent = data.filtered_total;
    document.getElementById('mRate').textContent = data.filter_rate + '%';
    document.getElementById('mMR').textContent = data.mr_count;

    const statusLabels = {{
      waiting_trigger: '等待触发', not_triggered: '未触发', queued: '排队中', running: '审查中',
      no_suggestions: '无建议', all_filtered: '全部过滤', partially_filtered: '部分过滤',
      published: '已发布', fallback_published: '降级发布成功', publish_failed: '发布失败', execution_failed: '执行失败',
      skipped: '规则跳过', stale: '审查已过期', completed: '已完成', historical: '历史数据'
    }};
    document.getElementById('statusSummary').textContent = Object.entries(data.status_counts || {{}})
      .map(([key, value]) => `${{statusLabels[key] || key}}: ${{value}}`).join(' · ');
    const reviewRows = data.review_mr_rows || [];
    const reviewPager = createPager({{
      rows: reviewRows, tbodyId: 'reviewMrTableBody', pageInfoId: 'reviewMrPageInfo',
      prevBtnId: 'reviewMrPrevPage', nextBtnId: 'reviewMrNextPage', pageSize: 20, emptyColspan: 9,
      emptyText: '尚未同步到 MR；同步成功前，下方仍会显示已有建议记录。',
      renderRow: (row) => `
        <tr><td>${{escapeHtml(row.mr)}}</td>
        <td><div>${{escapeHtml(row.title || '—')}}</div><div class="muted">${{escapeHtml(row.owner || '—')}}</div></td>
        <td><span class="reason-badge">${{escapeHtml(statusLabels[row.status] || row.status)}}</span></td>
        <td>${{escapeHtml(row.stage || '—')}}</td><td>${{row.generated_count}}</td>
        <td>${{row.filtered_count}}</td><td>${{row.inline_published_count + row.inline_fallback_count}}</td>
        <td>${{escapeHtml(row.ts)}}</td>
        <td class="link-cell">${{row.link ? `<a href="${{escapeHtml(row.link)}}" target="_blank" rel="noreferrer">查看</a>` : '<span class="muted">—</span>'}}</td></tr>`,
    }});
    const projectFilter = document.getElementById('projectFilter');
    const statusFilter = document.getElementById('statusFilter');
    [...new Set(reviewRows.map(row => row.project).filter(Boolean))].sort().forEach(project => {{
      projectFilter.insertAdjacentHTML('beforeend', `<option value="${{escapeHtml(project)}}">${{escapeHtml(project)}}</option>`);
    }});
    Object.keys(data.status_counts || {{}}).sort().forEach(status => {{
      statusFilter.insertAdjacentHTML(
        'beforeend', `<option value="${{escapeHtml(status)}}">${{escapeHtml(statusLabels[status] || status)}}</option>`);
    }});
    const applyReviewFilters = () => reviewPager.setRows(reviewRows.filter(row =>
      (!projectFilter.value || row.project === projectFilter.value)
      && (!statusFilter.value || row.status === statusFilter.value)));
    projectFilter.addEventListener('change', applyReviewFilters);
    statusFilter.addEventListener('change', applyReviewFilters);

    new Chart(document.getElementById('reasonChart'), {{
      type: 'bar',
      data: {{ labels: data.reason_labels, datasets: [{{ label: 'Filtered count', data: data.reason_values,
        backgroundColor: '#f87171', borderRadius: 6 }}] }},
      options: {{ responsive: true, maintainAspectRatio: false, indexAxis: 'y',
        plugins: {{ legend: {{ display: false }} }},
        scales: {{ x: {{ ticks: {{ color: '#94a3b8' }} }}, y: {{ ticks: {{ color: '#94a3b8' }} }} }} }},
    }});

    new Chart(document.getElementById('trendChart'), {{
      type: 'line',
      data: {{ labels: data.week_labels, datasets: [{{ label: 'Filter rate %', data: data.week_values,
        borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.18)', fill: true,
        tension: 0.35, pointRadius: 4 }}] }},
      options: {{ responsive: true, maintainAspectRatio: false,
        plugins: {{ legend: {{ labels: {{ color: '#cbd5e1' }} }} }},
        scales: {{ x: {{ ticks: {{ color: '#94a3b8' }} }}, y: {{ min: 0, max: 100, ticks: {{ color: '#94a3b8' }} }} }} }},
    }});

    createPager({{
      rows: data.project_rows, tbodyId: 'projectTableBody', pageInfoId: 'projectPageInfo',
      prevBtnId: 'projectPrevPage', nextBtnId: 'projectNextPage', pageSize: 10, emptyColspan: 5,
      emptyText: 'No project data',
      renderRow: (row) => `
        <tr><td>${{escapeHtml(row.project)}}</td><td>${{row.pub}}</td><td>${{row.filtered}}</td>
        <td>${{pctBadge(row.rate)}}</td><td>${{row.mr_count}}</td></tr>`,
    }});

    createPager({{
      rows: data.mr_rows, tbodyId: 'mrTableBody', pageInfoId: 'mrPageInfo',
      prevBtnId: 'mrPrevPage', nextBtnId: 'mrNextPage', pageSize: 10, emptyColspan: 7,
      emptyText: 'No matching MR',
      renderRow: (row) => `
        <tr><td>${{escapeHtml(row.mr)}}</td><td>${{row.pub}}</td><td>${{row.filtered}}</td>
        <td>${{pctBadge(row.rate)}}</td><td>${{escapeHtml(row.ts)}}</td>
        <td>${{row.owner ? escapeHtml(row.owner) : '<span class="muted">\\u2014</span>'}}</td>
        <td class="link-cell">${{row.link ? `<a href="${{escapeHtml(row.link)}}" target="_blank" rel="noreferrer">\\u67e5\\u770b</a>` : '<span class="muted">\\u2014</span>'}}</td></tr>`,
    }});

    createPager({{
      rows: data.filtered_rows, tbodyId: 'filteredTableBody', pageInfoId: 'filteredPageInfo',
      prevBtnId: 'filteredPrevPage', nextBtnId: 'filteredNextPage', pageSize: 15, emptyColspan: 8,
      emptyText: 'No filtered suggestions yet',
      renderRow: (row) => `
        <tr><td>${{escapeHtml(row.ts)}}</td><td>${{escapeHtml(row.project)}}</td>
        <td>${{escapeHtml(row.mr)}}</td><td>${{escapeHtml(row.file)}}</td>
        <td>${{escapeHtml(row.label) || '<span class="muted">\\u2014</span>'}}</td>
        <td>${{row.score ?? '\\u2014'}}</td>
        <td><span class="reason-badge">${{escapeHtml(row.reason)}}</span></td>
        <td class="link-cell">${{row.link ? `<a href="${{escapeHtml(row.link)}}" target="_blank" rel="noreferrer">\\u67e5\\u770b</a>` : '<span class="muted">\\u2014</span>'}}</td></tr>
        <tr><td colspan="8" style="padding:0;border:none"><details><summary>\\u5c55\\u5f00\\u5efa\\u8bae\\u5185\\u5bb9</summary><pre class="content-pre">${{escapeHtml(row.content)}}</pre></details></td></tr>`,
    }});
  }} catch (e) {{
    document.getElementById('mPub').textContent = 'Error';
  }}
}}
init();
</script>
</body>
</html>
"""


def _suggestion_filter_dashboard_html() -> str:
    return render_suggestion_review_dashboard(_BASE_CSS, _JS_HELPERS, _nav_bar("filter"))


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_index():
    return (
        "<html><body style='font-family:sans-serif;padding:2rem'>"
        "<h1>PR-Agent Dashboards</h1>"
        "<ul>"
        "<li><a href='/dashboard/inline'>Inline suggestion adoption</a></li>"
        "<li><a href='/dashboard/triage'>CI triage results</a></li>"
        "<li><a href='/dashboard/repair-memory'>Repair memory</a></li>"
        "<li><a href='/dashboard/suggestion-filter'>Suggestion filter analysis</a></li>"
        "</ul></body></html>"
    )


@router.get("/dashboard/feedback", response_class=HTMLResponse)
async def dashboard_feedback():
    return _feedback_dashboard_html()


@router.get("/dashboard/inline", response_class=HTMLResponse)
async def dashboard_inline():
    return _inline_dashboard_html()


@router.get("/dashboard/triage", response_class=HTMLResponse)
async def dashboard_triage():
    return _triage_dashboard_html()


@router.get("/dashboard/ci-failures", response_class=HTMLResponse)
async def dashboard_ci_failures():
    return _ci_failure_dashboard_html()


@router.get("/dashboard/repair-memory", response_class=HTMLResponse)
async def dashboard_repair_memory():
    return _repair_memory_dashboard_html()


@router.get("/dashboard/suggestion-filter", response_class=HTMLResponse)
async def dashboard_suggestion_filter():
    return _suggestion_filter_dashboard_html()
