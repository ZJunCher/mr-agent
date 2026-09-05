"""Durable SQLite inventory for failed MR Pipelines and bounded Job evidence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import timedelta
from typing import Iterable, Mapping
from urllib.parse import urlparse

from pr_agent.feedback.store import _connect, _write_lock, get_db_path
from pr_agent.feedback.timez import now_cn, now_cn_iso
from pr_agent.log import get_logger
from pr_agent.storage.sqlite import run_write_transaction
from pr_agent.triage.ci_failure_analysis import CapabilityClass, FailureAggregate
from pr_agent.triage.failure_explanations import sanitize_failure_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ci_failure_pipelines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    project_id TEXT NOT NULL,
    project_path TEXT NOT NULL,
    mr_iid TEXT NOT NULL,
    mr_url TEXT,
    mr_title TEXT,
    mr_author TEXT,
    source_branch TEXT,
    target_branch TEXT,
    pipeline_id INTEGER NOT NULL,
    pipeline_url TEXT,
    pipeline_sha TEXT,
    pipeline_status TEXT NOT NULL DEFAULT 'failed',
    failed_job_count INTEGER NOT NULL DEFAULT 0,
    unknown_reason_count INTEGER NOT NULL DEFAULT 0,
    categories_json TEXT NOT NULL DEFAULT '[]',
    primary_reason TEXT,
    primary_fingerprint TEXT,
    notification_state TEXT NOT NULL DEFAULT 'not_attempted',
    notification_reason_code TEXT,
    card_id TEXT,
    followup_state TEXT NOT NULL DEFAULT 'pending',
    followup_pipeline_id INTEGER,
    followup_sha TEXT,
    source TEXT NOT NULL DEFAULT 'webhook',
    UNIQUE(project_id, mr_iid, pipeline_id)
);
CREATE INDEX IF NOT EXISTS idx_ci_failure_detected ON ci_failure_pipelines(detected_at);
CREATE INDEX IF NOT EXISTS idx_ci_failure_project ON ci_failure_pipelines(project_path, mr_iid);
CREATE INDEX IF NOT EXISTS idx_ci_failure_fingerprint ON ci_failure_pipelines(primary_fingerprint);
CREATE INDEX IF NOT EXISTS idx_ci_failure_card ON ci_failure_pipelines(card_id);

CREATE TABLE IF NOT EXISTS ci_failure_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    failure_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    job_name TEXT NOT NULL,
    stage TEXT,
    job_url TEXT,
    pipeline_id INTEGER,
    family TEXT NOT NULL,
    confirmed_reason TEXT,
    trace_line INTEGER NOT NULL DEFAULT 0,
    reason_confidence TEXT NOT NULL DEFAULT 'unknown',
    fingerprint TEXT,
    system_capability TEXT NOT NULL DEFAULT 'unknown',
    capability_basis TEXT,
    capability_confidence TEXT NOT NULL DEFAULT 'low',
    UNIQUE(failure_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_ci_failure_jobs_failure ON ci_failure_jobs(failure_id);
CREATE INDEX IF NOT EXISTS idx_ci_failure_jobs_fingerprint ON ci_failure_jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_ci_failure_jobs_family ON ci_failure_jobs(family);

CREATE TABLE IF NOT EXISTS ci_failure_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    failure_id INTEGER NOT NULL,
    target_key TEXT NOT NULL,
    job_row_id INTEGER,
    manual_reason TEXT,
    manual_capability TEXT,
    note TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(failure_id, target_key)
);
CREATE INDEX IF NOT EXISTS idx_ci_failure_annotations_failure ON ci_failure_annotations(failure_id);
"""

_NOTIFICATION_STATES = frozenset({"not_attempted", "queued", "delivered", "recipient_missing", "failed"})
_CAPABILITIES = frozenset(item.value for item in CapabilityClass)


def _initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def init_ci_failure_tables(path: str | None = None) -> None:
    run_write_transaction(path or get_db_path(), _initialize, connect=_connect)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value) or "")


def _safe_url(value: object) -> str:
    url = sanitize_failure_text(value, 500)
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _job_dict(value: object) -> dict:
    if is_dataclass(value):
        return asdict(value)
    return dict(value) if isinstance(value, Mapping) else {}


def save_ci_failure(
    record: Mapping[str, object],
    jobs: Iterable[object],
    *,
    aggregate: FailureAggregate,
    path: str | None = None,
) -> int | None:
    """Upsert one failed Pipeline and its bounded Job evidence. Never raises."""
    db_path = path or get_db_path()
    timestamp = sanitize_failure_text(record.get("detected_at") or now_cn_iso(), 64)
    serialized_categories = json.dumps(list(aggregate.categories), ensure_ascii=False, separators=(",", ":"))

    def write(conn: sqlite3.Connection) -> int:
        _initialize(conn)
        conn.execute(
            """
            INSERT INTO ci_failure_pipelines (
                detected_at, updated_at, project_id, project_path, mr_iid, mr_url, mr_title, mr_author,
                source_branch, target_branch, pipeline_id, pipeline_url, pipeline_sha, pipeline_status,
                failed_job_count, unknown_reason_count, categories_json, primary_reason, primary_fingerprint,
                notification_state, notification_reason_code, card_id, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, mr_iid, pipeline_id) DO UPDATE SET
                updated_at=excluded.updated_at, project_path=excluded.project_path, mr_url=excluded.mr_url,
                mr_title=excluded.mr_title, mr_author=excluded.mr_author, source_branch=excluded.source_branch,
                target_branch=excluded.target_branch, pipeline_url=excluded.pipeline_url,
                pipeline_sha=excluded.pipeline_sha, pipeline_status=excluded.pipeline_status,
                failed_job_count=excluded.failed_job_count, unknown_reason_count=excluded.unknown_reason_count,
                categories_json=excluded.categories_json, primary_reason=excluded.primary_reason,
                primary_fingerprint=excluded.primary_fingerprint, card_id=COALESCE(excluded.card_id, card_id)
            """,
            (
                timestamp, now_cn_iso(), str(record.get("project_id") or ""),
                sanitize_failure_text(record.get("project_path"), 240), str(record.get("mr_iid") or ""),
                _safe_url(record.get("mr_url")), sanitize_failure_text(record.get("mr_title"), 300),
                sanitize_failure_text(record.get("mr_author"), 120),
                sanitize_failure_text(record.get("source_branch"), 240),
                sanitize_failure_text(record.get("target_branch"), 240), int(record.get("pipeline_id") or 0),
                _safe_url(record.get("pipeline_url")),
                sanitize_failure_text(record.get("pipeline_sha"), 64),
                sanitize_failure_text(record.get("pipeline_status") or "failed", 32),
                aggregate.failed_job_count, aggregate.unknown_reason_count, serialized_categories,
                sanitize_failure_text(aggregate.primary_reason), aggregate.primary_fingerprint,
                str(record.get("notification_state") or "not_attempted"),
                sanitize_failure_text(record.get("notification_reason_code"), 80),
                sanitize_failure_text(record.get("card_id"), 300),
                sanitize_failure_text(record.get("source") or "webhook", 32),
            ),
        )
        failure_id = int(conn.execute(
            "SELECT id FROM ci_failure_pipelines WHERE project_id = ? AND mr_iid = ? AND pipeline_id = ?",
            (str(record.get("project_id") or ""), str(record.get("mr_iid") or ""), int(record.get("pipeline_id") or 0)),
        ).fetchone()[0])
        seen_job_ids = []
        for raw_job in jobs:
            job = _job_dict(raw_job)
            job_id = int(job.get("job_id") or 0)
            seen_job_ids.append(job_id)
            conn.execute(
                """
                INSERT INTO ci_failure_jobs (
                    failure_id, job_id, job_name, stage, job_url, pipeline_id, family, confirmed_reason,
                    trace_line, reason_confidence, fingerprint, system_capability, capability_basis,
                    capability_confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(failure_id, job_id) DO UPDATE SET
                    job_name=excluded.job_name, stage=excluded.stage, job_url=excluded.job_url,
                    pipeline_id=excluded.pipeline_id, family=excluded.family,
                    confirmed_reason=excluded.confirmed_reason, trace_line=excluded.trace_line,
                    reason_confidence=excluded.reason_confidence, fingerprint=excluded.fingerprint,
                    system_capability=excluded.system_capability, capability_basis=excluded.capability_basis,
                    capability_confidence=excluded.capability_confidence
                """,
                (
                    failure_id, job_id, sanitize_failure_text(job.get("job_name") or "unknown", 120),
                    sanitize_failure_text(job.get("stage"), 80), sanitize_failure_text(job.get("job_url"), 500),
                    int(job.get("pipeline_id") or record.get("pipeline_id") or 0), _enum_value(job.get("family")),
                    sanitize_failure_text(job.get("confirmed_reason")), int(job.get("trace_line") or 0),
                    sanitize_failure_text(job.get("reason_confidence") or "unknown", 20),
                    sanitize_failure_text(job.get("fingerprint"), 64), _enum_value(job.get("capability")),
                    sanitize_failure_text(job.get("capability_basis"), 100),
                    sanitize_failure_text(job.get("capability_confidence") or "low", 20),
                ),
            )
        if seen_job_ids:
            placeholders = ",".join("?" for _ in seen_job_ids)
            conn.execute(
                f"DELETE FROM ci_failure_jobs WHERE failure_id = ? AND job_id NOT IN ({placeholders})",
                (failure_id, *seen_job_ids),
            )
        return failure_id

    try:
        with _write_lock:
            return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to save CI failure: {type(error).__name__}")
        return None


def update_notification_state(
    card_id: str,
    state: str,
    reason_code: str = "",
    *,
    path: str | None = None,
) -> bool:
    if state not in _NOTIFICATION_STATES or not card_id:
        return False

    def write(conn: sqlite3.Connection) -> bool:
        _initialize(conn)
        cursor = conn.execute(
            "UPDATE ci_failure_pipelines SET notification_state = ?, notification_reason_code = ?, updated_at = ? "
            "WHERE card_id = ?",
            (state, sanitize_failure_text(reason_code, 80), now_cn_iso(), card_id),
        )
        return cursor.rowcount > 0

    try:
        with _write_lock:
            return bool(run_write_transaction(path or get_db_path(), write, connect=_connect))
    except Exception as error:
        get_logger().error(f"Failed to update CI failure notification: {type(error).__name__}")
        return False


def record_followup_pipeline(
    project_id: str,
    mr_iid: str,
    pipeline_id: int,
    pipeline_sha: str,
    status: str,
    *,
    path: str | None = None,
) -> int:
    """Attach later terminal Pipeline evidence to unresolved earlier failures."""
    if str(status).lower() not in {"success", "failed"}:
        return 0

    def write(conn: sqlite3.Connection) -> int:
        _initialize(conn)
        rows = conn.execute(
            "SELECT id, pipeline_sha FROM ci_failure_pipelines "
            "WHERE project_id = ? AND mr_iid = ? AND pipeline_id < ? "
            "AND followup_state NOT IN ('same_sha_success', 'new_sha_success')",
            (str(project_id), str(mr_iid), int(pipeline_id)),
        ).fetchall()
        count = 0
        for failure_id, source_sha in rows:
            state = "later_failed"
            if str(status).lower() == "success":
                state = "same_sha_success" if str(source_sha or "") == str(pipeline_sha or "") else "new_sha_success"
            conn.execute(
                "UPDATE ci_failure_pipelines SET followup_state = ?, followup_pipeline_id = ?, followup_sha = ?, "
                "updated_at = ? WHERE id = ?",
                (state, int(pipeline_id), sanitize_failure_text(pipeline_sha, 64), now_cn_iso(), failure_id),
            )
            count += 1
        return count

    try:
        with _write_lock:
            return int(run_write_transaction(path or get_db_path(), write, connect=_connect))
    except Exception as error:
        get_logger().error(f"Failed to record CI followup: {type(error).__name__}")
        return 0


def save_annotation(
    failure_id: int,
    *,
    job_id: int | None = None,
    reason: str = "",
    capability: str = "",
    note: str = "",
    path: str | None = None,
) -> bool:
    if capability and capability not in _CAPABILITIES:
        raise ValueError("invalid capability")
    reason = sanitize_failure_text(reason, 300)
    note = sanitize_failure_text(note, 1000)
    target_key = f"job:{job_id}" if job_id is not None else "pipeline"

    def write(conn: sqlite3.Connection) -> bool:
        _initialize(conn)
        if conn.execute("SELECT 1 FROM ci_failure_pipelines WHERE id = ?", (failure_id,)).fetchone() is None:
            return False
        if job_id is not None and conn.execute(
            "SELECT 1 FROM ci_failure_jobs WHERE id = ? AND failure_id = ?", (job_id, failure_id)
        ).fetchone() is None:
            return False
        conn.execute(
            """
            INSERT INTO ci_failure_annotations (
                failure_id, target_key, job_row_id, manual_reason, manual_capability, note, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(failure_id, target_key) DO UPDATE SET
                manual_reason=excluded.manual_reason, manual_capability=excluded.manual_capability,
                note=excluded.note, updated_at=excluded.updated_at
            """,
            (failure_id, target_key, job_id, reason, capability, note, now_cn_iso()),
        )
        return True

    with _write_lock:
        return bool(run_write_transaction(path or get_db_path(), write, connect=_connect))


def _dict_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone() is not None


def get_ci_failure(failure_id: int, *, path: str | None = None) -> dict | None:
    try:
        init_ci_failure_tables(path)
        conn = _connect(path or get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM ci_failure_pipelines WHERE id = ?", (failure_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["categories"] = json.loads(result.pop("categories_json") or "[]")
            jobs = _dict_rows(conn, """
                SELECT j.*, a.manual_reason, a.manual_capability, a.note, a.updated_at AS annotation_updated_at
                FROM ci_failure_jobs AS j
                LEFT JOIN ci_failure_annotations AS a
                  ON a.failure_id = j.failure_id AND a.target_key = 'job:' || j.id
                WHERE j.failure_id = ? ORDER BY j.id
            """, (failure_id,))
            for job in jobs:
                job["system_reason"] = job.pop("confirmed_reason") or ""
                job["effective_reason"] = job.get("manual_reason") or job["system_reason"]
                job["effective_capability"] = job.get("manual_capability") or job["system_capability"]
            result["jobs"] = jobs
            result["triage_runs"] = []
            if _table_exists(conn, "triage_runs"):
                result["triage_runs"] = _dict_rows(conn, """
                    SELECT task_id, success, repair_outcome, final_pipeline_status, created_at
                    FROM triage_runs WHERE project = ? AND CAST(mr_iid AS TEXT) = ?
                      AND (CAST(pipeline_id AS INTEGER) = ? OR commit_sha = ?)
                    ORDER BY created_at DESC
                """, (result["project_path"], result["mr_iid"], result["pipeline_id"], result["pipeline_sha"]))
            return result
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to read CI failure: {type(error).__name__}")
        return None


def _query_filters(filters: Mapping[str, object]) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    days = filters.get("days", 30)
    if days:
        clauses.append("p.detected_at >= ?")
        params.append((now_cn() - timedelta(days=max(1, int(days)))).isoformat())
    if filters.get("project"):
        clauses.append("p.project_path = ?")
        params.append(str(filters["project"]))
    if filters.get("family"):
        clauses.append("EXISTS (SELECT 1 FROM ci_failure_jobs fj WHERE fj.failure_id = p.id AND fj.family = ?)")
        params.append(str(filters["family"]))
    if filters.get("capability"):
        clauses.append(
            "EXISTS (SELECT 1 FROM ci_failure_jobs cj LEFT JOIN ci_failure_annotations ca "
            "ON ca.failure_id = cj.failure_id AND ca.target_key = 'job:' || cj.id "
            "WHERE cj.failure_id = p.id AND COALESCE(NULLIF(ca.manual_capability, ''), cj.system_capability) = ?)"
        )
        params.append(str(filters["capability"]))
    if filters.get("fingerprint"):
        clauses.append("EXISTS (SELECT 1 FROM ci_failure_jobs ff WHERE ff.failure_id = p.id AND ff.fingerprint = ?)")
        params.append(str(filters["fingerprint"]))
    if filters.get("q"):
        clauses.append("(p.project_path LIKE ? OR p.mr_title LIKE ? OR p.primary_reason LIKE ?)")
        search = f"%{sanitize_failure_text(filters['q'], 100)}%"
        params.extend((search, search, search))
    return clauses, params


def query_ci_failures(filters: Mapping[str, object], *, path: str | None = None) -> dict:
    """Return bounded aggregate and row data for the operator dashboard."""
    init_ci_failure_tables(path)
    conn = _connect(path or get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        clauses, params = _query_filters(filters)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        page = max(1, int(filters.get("page") or 1))
        page_size = min(100, max(1, int(filters.get("page_size") or 20)))
        recurring_page = max(1, int(filters.get("recurring_page") or 1))
        recurring_page_size = min(100, max(1, int(filters.get("recurring_page_size") or 5)))
        project_distribution_page = max(1, int(filters.get("project_distribution_page") or 1))
        project_distribution_page_size = min(
            100, max(1, int(filters.get("project_distribution_page_size") or 5))
        )
        job_distribution_page = max(1, int(filters.get("job_distribution_page") or 1))
        job_distribution_page_size = min(100, max(1, int(filters.get("job_distribution_page_size") or 5)))
        total = int(conn.execute(f"SELECT COUNT(*) FROM ci_failure_pipelines p {where}", params).fetchone()[0])
        failed_jobs = int(conn.execute(
            f"SELECT COUNT(*) FROM ci_failure_jobs j JOIN ci_failure_pipelines p ON p.id = j.failure_id {where}", params
        ).fetchone()[0])
        unknown_jobs = int(conn.execute(
            f"SELECT COUNT(*) FROM ci_failure_jobs j JOIN ci_failure_pipelines p ON p.id = j.failure_id {where} "
            f"{'AND' if where else 'WHERE'} COALESCE(j.confirmed_reason, '') = ''", params
        ).fetchone()[0])
        recurring_count = int(conn.execute(
            f"SELECT COUNT(*) FROM (SELECT j.fingerprint FROM ci_failure_jobs j "
            f"JOIN ci_failure_pipelines p ON p.id = j.failure_id {where} "
            f"{'AND' if where else 'WHERE'} COALESCE(j.fingerprint, '') != '' "
            "GROUP BY j.fingerprint HAVING COUNT(*) >= 2)", params
        ).fetchone()[0])
        project_distribution_total = int(conn.execute(
            f"SELECT COUNT(DISTINCT p.project_path) FROM ci_failure_pipelines p {where}", params
        ).fetchone()[0])
        job_distribution_total = int(conn.execute(
            f"SELECT COUNT(DISTINCT j.job_name) FROM ci_failure_jobs j "
            f"JOIN ci_failure_pipelines p ON p.id = j.failure_id {where}",
            params,
        ).fetchone()[0])
        recurring = _dict_rows(conn, f"""
            SELECT j.fingerprint, MAX(j.confirmed_reason) AS reason, COUNT(*) AS occurrences,
                   COUNT(DISTINCT p.project_path) AS project_count, MAX(p.detected_at) AS last_seen
            FROM ci_failure_jobs j JOIN ci_failure_pipelines p ON p.id = j.failure_id {where}
            {'AND' if where else 'WHERE'} COALESCE(j.fingerprint, '') != ''
            GROUP BY j.fingerprint HAVING COUNT(*) >= 2
            ORDER BY occurrences DESC, last_seen DESC LIMIT ? OFFSET ?
        """, (*params, recurring_page_size, (recurring_page - 1) * recurring_page_size))
        rows = _dict_rows(conn, f"""
            SELECT p.*,
                   (SELECT COALESCE(NULLIF(a.manual_capability, ''), j.system_capability)
                    FROM ci_failure_jobs j LEFT JOIN ci_failure_annotations a
                      ON a.failure_id = j.failure_id AND a.target_key = 'job:' || j.id
                    WHERE j.failure_id = p.id ORDER BY j.id LIMIT 1) AS capability,
                   (SELECT COUNT(*) FROM triage_runs t WHERE t.project = p.project_path
                    AND CAST(t.mr_iid AS TEXT) = p.mr_iid
                    AND (CAST(t.pipeline_id AS INTEGER) = p.pipeline_id OR t.commit_sha = p.pipeline_sha)
                   ) AS triage_count
            FROM ci_failure_pipelines p {where}
            ORDER BY p.detected_at DESC LIMIT ? OFFSET ?
        """ if _table_exists(conn, "triage_runs") else f"""
            SELECT p.*,
                   (SELECT COALESCE(NULLIF(a.manual_capability, ''), j.system_capability)
                    FROM ci_failure_jobs j LEFT JOIN ci_failure_annotations a
                      ON a.failure_id = j.failure_id AND a.target_key = 'job:' || j.id
                    WHERE j.failure_id = p.id ORDER BY j.id LIMIT 1) AS capability,
                   0 AS triage_count
            FROM ci_failure_pipelines p {where}
            ORDER BY p.detected_at DESC LIMIT ? OFFSET ?
        """, (*params, page_size, (page - 1) * page_size))
        for row in rows:
            row["categories"] = json.loads(row.pop("categories_json") or "[]")
        trend = _dict_rows(conn, f"""
            SELECT substr(p.detected_at, 1, 10) AS day, COUNT(*) AS count
            FROM ci_failure_pipelines p {where} GROUP BY day ORDER BY day
        """, tuple(params))
        categories = _dict_rows(conn, f"""
            SELECT j.family, COUNT(*) AS count FROM ci_failure_jobs j
            JOIN ci_failure_pipelines p ON p.id = j.failure_id {where}
            GROUP BY j.family ORDER BY count DESC
        """, tuple(params))
        top_projects = _dict_rows(conn, f"""
            SELECT p.project_path, COUNT(*) AS count FROM ci_failure_pipelines p {where}
            GROUP BY p.project_path ORDER BY count DESC, p.project_path ASC LIMIT ? OFFSET ?
        """, (*params, project_distribution_page_size,
                (project_distribution_page - 1) * project_distribution_page_size))
        top_jobs = _dict_rows(conn, f"""
            SELECT j.job_name, COUNT(*) AS count FROM ci_failure_jobs j
            JOIN ci_failure_pipelines p ON p.id = j.failure_id {where}
            GROUP BY j.job_name ORDER BY count DESC, j.job_name ASC LIMIT ? OFFSET ?
        """, (*params, job_distribution_page_size,
                (job_distribution_page - 1) * job_distribution_page_size))
        return {
            "metrics": {
                "failed_pipelines": total,
                "failed_jobs": failed_jobs,
                "unknown_reason_jobs": unknown_jobs,
                "recurring_patterns": recurring_count,
            },
            "trend": trend,
            "categories": categories,
            "recurring": recurring,
            "top_projects": top_projects,
            "top_jobs": top_jobs,
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
            "recurring_page": recurring_page,
            "recurring_page_size": recurring_page_size,
            "recurring_total": recurring_count,
            "recurring_total_pages": (recurring_count + recurring_page_size - 1) // recurring_page_size,
            "project_distribution_page": project_distribution_page,
            "project_distribution_page_size": project_distribution_page_size,
            "project_distribution_total": project_distribution_total,
            "project_distribution_total_pages": (
                project_distribution_total + project_distribution_page_size - 1
            ) // project_distribution_page_size,
            "job_distribution_page": job_distribution_page,
            "job_distribution_page_size": job_distribution_page_size,
            "job_distribution_total": job_distribution_total,
            "job_distribution_total_pages": (
                job_distribution_total + job_distribution_page_size - 1
            ) // job_distribution_page_size,
        }
    finally:
        conn.close()
