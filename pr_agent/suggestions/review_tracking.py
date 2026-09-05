"""Best-effort persistence for suggestion-review coverage and run progress."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from typing import Iterator, Optional

from pr_agent.feedback.timez import now_cn, now_cn_iso, to_cn
from pr_agent.storage.sqlite import connect_sqlite, run_write_transaction

_write_lock = threading.Lock()
_current_run_id: ContextVar[str | None] = ContextVar("suggestion_review_run_id", default=None)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mr_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    project_path TEXT NOT NULL,
    mr_iid TEXT NOT NULL,
    mr_url TEXT,
    title TEXT,
    author TEXT,
    source_branch TEXT,
    target_branch TEXT,
    state TEXT,
    draft INTEGER NOT NULL DEFAULT 0,
    commit_sha TEXT,
    created_at TEXT,
    updated_at TEXT,
    discovered_by TEXT,
    webhook_received_at TEXT,
    last_synced_at TEXT,
    creation_recovery_state TEXT,
    creation_reason_code TEXT,
    creation_reason_at TEXT,
    UNIQUE(project_id, mr_iid),
    UNIQUE(project_path, mr_iid)
);
CREATE INDEX IF NOT EXISTS idx_mr_inventory_updated ON mr_inventory(updated_at);
CREATE INDEX IF NOT EXISTS idx_mr_inventory_project ON mr_inventory(project_path, mr_iid);

CREATE TABLE IF NOT EXISTS suggestion_review_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    project_id TEXT,
    project_path TEXT,
    mr_iid TEXT,
    mr_url TEXT,
    commit_sha TEXT,
    trigger TEXT,
    task_id TEXT,
    review_id TEXT,
    webhook_id TEXT,
    review_scope TEXT NOT NULL DEFAULT 'legacy',
    stage TEXT NOT NULL DEFAULT 'queued',
    status TEXT NOT NULL DEFAULT 'running',
    generated_count INTEGER NOT NULL DEFAULT 0,
    kept_count INTEGER NOT NULL DEFAULT 0,
    filtered_count INTEGER NOT NULL DEFAULT 0,
    inline_selected_count INTEGER NOT NULL DEFAULT 0,
    inline_skipped_count INTEGER NOT NULL DEFAULT 0,
    inline_published_count INTEGER NOT NULL DEFAULT 0,
    inline_fallback_count INTEGER NOT NULL DEFAULT 0,
    inline_failed_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    improve_started_at TEXT,
    unpublished_reason TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_runs_mr
    ON suggestion_review_runs(project_path, mr_iid, started_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_runs_task
    ON suggestion_review_runs(task_id) WHERE task_id IS NOT NULL AND task_id != '';

CREATE TABLE IF NOT EXISTS suggestion_review_meta (
    meta_key TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestion_review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, event_key)
);
CREATE INDEX IF NOT EXISTS idx_review_events_run ON suggestion_review_events(run_id, id);

CREATE TABLE IF NOT EXISTS suggestion_sync_state (
    sync_name TEXT PRIMARY KEY,
    cursor_at TEXT,
    last_started_at TEXT,
    last_success_at TEXT,
    last_reconcile_at TEXT,
    last_error TEXT,
    lease_owner TEXT,
    lease_until TEXT
);
CREATE TABLE IF NOT EXISTS suggestion_sync_metrics (
    metric_key TEXT PRIMARY KEY,
    metric_value INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suggestion_review_alert_state (
    alert_key TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 0,
    last_count INTEGER NOT NULL DEFAULT 0,
    first_triggered_at TEXT,
    last_evaluated_at TEXT NOT NULL,
    last_emitted_at TEXT,
    resolved_at TEXT
);
"""


def _logger():
    from pr_agent.log import get_logger

    return get_logger()


def _db_path(path: Optional[str] = None) -> str:
    if path:
        return path
    from pr_agent.suggestions.store import get_db_path

    return get_db_path()


def _run_write(path: str, operation):
    with _write_lock:
        return run_write_transaction(path, operation, connect=connect_sqlite)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    for name in ("creation_recovery_state", "creation_reason_code", "creation_reason_at"):
        try:
            conn.execute(f"ALTER TABLE mr_inventory ADD COLUMN {name} TEXT")
        except sqlite3.OperationalError:
            pass
    for name, definition in (
        ("review_scope", "TEXT NOT NULL DEFAULT 'legacy'"),
        ("improve_started_at", "TEXT"),
        ("unpublished_reason", "TEXT"),
        ("inline_fallback_count", "INTEGER NOT NULL DEFAULT 0"),
        ("review_id", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE suggestion_review_runs ADD COLUMN {name} {definition}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_review_runs_creation_webhook "
        "ON suggestion_review_runs(webhook_id) "
        "WHERE review_scope = 'mr_creation' AND webhook_id IS NOT NULL AND webhook_id != ''"
    )


def init_review_tracking(path: Optional[str] = None) -> None:
    """Create tracking tables. Never raises."""
    try:
        _run_write(_db_path(path), _ensure_schema)
    except Exception as exc:
        _logger().error(f"Failed to initialize suggestion review tracking: {exc}")


def _clean(value, limit: int = 1000) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def mark_creation_tracking_started(path: Optional[str] = None) -> str | None:
    """Persist and return the reliable creation-tracking boundary. Never raises."""
    database_path = _db_path(path)
    now = now_cn_iso()
    try:
        def write(conn):
            _ensure_schema(conn)
            conn.execute(
                "INSERT OR IGNORE INTO suggestion_review_meta(meta_key, meta_value) VALUES (?, ?)",
                ("creation_tracking_started_at", now),
            )
            row = conn.execute(
                "SELECT meta_value FROM suggestion_review_meta WHERE meta_key = ?",
                ("creation_tracking_started_at",),
            ).fetchone()
            return str(row[0]) if row else None

        return _run_write(database_path, write)
    except Exception as exc:
        _logger().error(f"Failed to mark creation review tracking start: {exc}")
        return None


def get_creation_tracking_boundary(path: Optional[str] = None) -> str | None:
    """Return the reliable creation-tracking boundary. Never raises."""
    init_review_tracking(path)
    try:
        conn = connect_sqlite(_db_path(path))
        try:
            row = conn.execute(
                "SELECT meta_value FROM suggestion_review_meta WHERE meta_key = ?",
                ("creation_tracking_started_at",),
            ).fetchone()
            return str(row[0]) if row else None
        finally:
            conn.close()
    except Exception as exc:
        _logger().error(f"Failed to read creation review tracking boundary: {exc}")
        return None


def upsert_mr(record: dict, path: Optional[str] = None) -> bool:
    """Insert or refresh one GitLab MR metadata record. Never raises."""
    project_path = _clean(record.get("project_path") or record.get("project"), 500) or ""
    project_id = _clean(record.get("project_id") or project_path, 200) or ""
    mr_iid = _clean(record.get("mr_iid") or record.get("iid"), 100) or ""
    if not project_id or not mr_iid:
        return False
    now = now_cn_iso()
    discovered_by = _clean(record.get("discovered_by"), 50) or "webhook"
    values = (
        project_id, project_path, mr_iid, _clean(record.get("mr_url") or record.get("web_url"), 1000),
        _clean(record.get("title"), 1000), _clean(record.get("author"), 300),
        _clean(record.get("source_branch"), 500), _clean(record.get("target_branch"), 500),
        _clean(record.get("state"), 50), 1 if record.get("draft") else 0,
        _clean(record.get("commit_sha") or record.get("sha"), 100),
        _clean(record.get("created_at"), 100), _clean(record.get("updated_at"), 100) or now,
        discovered_by, now if discovered_by == "webhook" else None, now,
    )
    try:
        def write(conn):
            _ensure_schema(conn)
            conn.execute(
                """INSERT INTO mr_inventory (
                       project_id, project_path, mr_iid, mr_url, title, author, source_branch,
                       target_branch, state, draft, commit_sha, created_at, updated_at,
                       discovered_by, webhook_received_at, last_synced_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_path, mr_iid) DO UPDATE SET
                       project_id=CASE
                           WHEN excluded.project_id = excluded.project_path THEN mr_inventory.project_id
                           ELSE excluded.project_id
                       END,
                       mr_url=COALESCE(excluded.mr_url, mr_inventory.mr_url),
                       title=COALESCE(excluded.title, mr_inventory.title),
                       author=COALESCE(excluded.author, mr_inventory.author),
                       source_branch=COALESCE(excluded.source_branch, mr_inventory.source_branch),
                       target_branch=COALESCE(excluded.target_branch, mr_inventory.target_branch),
                       state=COALESCE(excluded.state, mr_inventory.state),
                       draft=excluded.draft,
                       commit_sha=COALESCE(excluded.commit_sha, mr_inventory.commit_sha),
                       created_at=COALESCE(excluded.created_at, mr_inventory.created_at),
                       updated_at=COALESCE(excluded.updated_at, mr_inventory.updated_at),
                       webhook_received_at=COALESCE(excluded.webhook_received_at, mr_inventory.webhook_received_at),
                       last_synced_at=excluded.last_synced_at""",
                values,
            )

        _run_write(_db_path(path), write)
        return True
    except Exception as exc:
        _logger().error(f"Failed to upsert MR inventory: {exc}")
        return False


def get_current_run_id() -> str | None:
    return _current_run_id.get()


def _provider_record(instance, trigger: str) -> dict:
    provider = getattr(instance, "git_provider", None)
    project = str(getattr(provider, "id_project", "") or "")
    mr_iid = str(getattr(provider, "id_mr", "") or "")
    mr_url = str(getattr(instance, "pr_url", "") or getattr(provider, "pr_url", "") or "")
    commit_sha = ""
    try:
        refs = provider.get_diff_refs()
        commit_sha = str((refs.get("head_sha") if isinstance(refs, dict) else refs.head_sha) or "")
    except Exception:
        pass
    task_id = None
    try:
        from pr_agent.distributed.runtime import get_execution_runtime

        runtime = get_execution_runtime()
        task_id = runtime.task_id if runtime else None
    except Exception:
        pass
    return {
        "project_id": project, "project_path": project, "project": project, "mr_iid": mr_iid,
        "mr_url": mr_url, "commit_sha": commit_sha, "trigger": trigger, "task_id": task_id,
    }


def track_review_run(trigger: str):
    """Decorate an async tool entry point with a best-effort review-run scope."""
    def decorate(func):
        @wraps(func)
        async def wrapped(instance, *args, **kwargs):
            if get_current_run_id():
                return await func(instance, *args, **kwargs)
            record = _provider_record(instance, trigger)
            upsert_mr({**record, "discovered_by": "review_run", "state": "opened"})
            run_id = start_review_run(record)
            if not run_id:
                return await func(instance, *args, **kwargs)
            with activate_review_run(run_id):
                update_review_run(run_id, stage="generating")
                try:
                    result = await func(instance, *args, **kwargs)
                except Exception as exc:
                    finish_review_run("failed", run_id, error_code=type(exc).__name__, error_message=str(exc))
                    raise
                else:
                    current = get_review_run(run_id)
                    status = str(current.get("status") or "running")
                    finish_review_run(status if status in {"failed", "skipped"} else "completed", run_id)
                    return result

        return wrapped
    return decorate


@contextmanager
def activate_review_run(run_id: str | None) -> Iterator[str | None]:
    token = _current_run_id.set(run_id)
    try:
        yield run_id
    finally:
        _current_run_id.reset(token)


def start_review_run(record: dict, path: Optional[str] = None) -> str | None:
    """Create a run, or return the existing run for the same distributed task."""
    run_id = _clean(record.get("run_id"), 100) or uuid.uuid4().hex
    task_id = _clean(record.get("task_id"), 200)
    now = now_cn_iso()
    values = (
        run_id, _clean(record.get("project_id"), 200),
        _clean(record.get("project_path") or record.get("project"), 500),
        _clean(record.get("mr_iid"), 100), _clean(record.get("mr_url"), 1000),
        _clean(record.get("commit_sha"), 100), _clean(record.get("trigger"), 100) or "manual_improve",
        task_id, _clean(record.get("webhook_id"), 200),
        _clean(record.get("review_scope"), 100) or "legacy",
        _clean(record.get("stage"), 100) or "queued",
        _clean(record.get("improve_started_at"), 100), _clean(record.get("unpublished_reason"), 100),
        now, now,
    )
    try:
        def write(conn):
            _ensure_schema(conn)
            if task_id:
                found = conn.execute(
                    "SELECT run_id FROM suggestion_review_runs WHERE task_id = ?", (task_id,)
                ).fetchone()
                if found:
                    return found[0]
            webhook_id = values[8]
            if values[9] == "mr_creation" and webhook_id:
                found = conn.execute(
                    "SELECT run_id FROM suggestion_review_runs WHERE review_scope = ? AND webhook_id = ?",
                    ("mr_creation", webhook_id),
                ).fetchone()
                if found:
                    return found[0]
            conn.execute(
                """INSERT INTO suggestion_review_runs (
                       run_id, project_id, project_path, mr_iid, mr_url, commit_sha, trigger,
                       task_id, webhook_id, review_scope, stage, improve_started_at,
                       unpublished_reason, started_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            return run_id

        return _run_write(_db_path(path), write)
    except Exception as exc:
        _logger().error(f"Failed to start suggestion review run: {exc}")
        return None


def ensure_creation_review(record: dict, path: Optional[str] = None) -> str | None:
    """Create or reuse the automatic MR-creation review run."""
    run_id = start_review_run({
        **record,
        "review_scope": "mr_creation",
        "trigger": "auto_mr_create",
        "stage": "event_received",
    }, path=path)
    if run_id:
        record_review_event(run_id, "creation_received", "event_received", path=path)
    return run_id


_COUNT_FIELDS = {
    "generated_count", "kept_count", "filtered_count", "inline_selected_count",
    "inline_skipped_count", "inline_published_count", "inline_fallback_count", "inline_failed_count",
}


def update_review_run(run_id: str | None = None, path: Optional[str] = None, **changes) -> bool:
    """Update allowed run fields. Values are absolute, not increments."""
    run_id = run_id or get_current_run_id()
    if not run_id:
        return False
    allowed = _COUNT_FIELDS | {
        "stage", "status", "error_code", "error_message", "completed_at", "commit_sha",
        "review_scope", "improve_started_at", "unpublished_reason", "review_id",
    }
    payload = {key: value for key, value in changes.items() if key in allowed}
    if not payload:
        return True
    for key in _COUNT_FIELDS:
        if key in payload:
            try:
                payload[key] = max(0, int(payload[key] or 0))
            except (TypeError, ValueError):
                payload[key] = 0
    if "error_message" in payload:
        payload["error_message"] = _clean(payload["error_message"], 1000)
    payload["updated_at"] = now_cn_iso()
    assignments = ", ".join(
        "commit_sha = COALESCE(NULLIF(commit_sha, ''), ?)" if key == "commit_sha" else f"{key} = ?"
        for key in payload
    )
    try:
        def write(conn):
            _ensure_schema(conn)
            conn.execute(
                f"UPDATE suggestion_review_runs SET {assignments} WHERE run_id = ?",
                (*payload.values(), run_id),
            )

        _run_write(_db_path(path), write)
        if payload.get("error_code"):
            capture_attributable_evolution_case(run_id, path=path)
        return True
    except Exception as exc:
        _logger().error(f"Failed to update suggestion review run: {exc}")
        return False


def capture_attributable_evolution_case(run_id: str, path: Optional[str] = None) -> bool:
    """Persist only reproducible Prompt-attributable run failures."""
    from pr_agent.feedback.store import save_evolution_case
    from pr_agent.suggestions.prompt_evolution.cases import attributable_case_kind

    run = get_review_run(run_id, path=path)
    raw_error_code = str(run.get("error_code") or "").strip()
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", raw_error_code).replace("-", "_").casefold()
    kind = attributable_case_kind(normalized)
    if kind is None and normalized.endswith("output_schema_error"):
        kind = attributable_case_kind("output_schema_error")
    if kind is None or not all((run.get("review_id"), run.get("project_path"), run.get("commit_sha"))):
        return False
    error_code = kind.value
    return save_evolution_case({
        "kind": kind.value,
        "project": str(run.get("project_path") or ""),
        "mr_iid": str(run.get("mr_iid") or ""),
        "review_id": str(run.get("review_id") or ""),
        "head_sha": str(run.get("commit_sha") or ""),
        "command": "improve",
        "description": str(run.get("error_message") or raw_error_code or error_code)[:2_000],
        "source": "automatic",
        "error_code": error_code,
        "created_at": str(run.get("updated_at") or now_cn_iso()),
    }, path=path)


def finish_review_run(status: str = "completed", run_id: str | None = None,
                      path: Optional[str] = None, **changes) -> bool:
    changes.update(status=status, completed_at=now_cn_iso())
    return update_review_run(run_id, path=path, **changes)


def get_review_run(run_id: str, path: Optional[str] = None) -> dict:
    init_review_tracking(path)
    try:
        conn = connect_sqlite(_db_path(path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM suggestion_review_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()
    except Exception as exc:
        _logger().error(f"Failed to read suggestion review run: {exc}")
        return {}


def get_review_run_for_task(task_id: str, path: Optional[str] = None) -> dict:
    """Return the review run associated with a distributed task. Never raises."""
    if not task_id:
        return {}
    init_review_tracking(path)
    try:
        conn = connect_sqlite(_db_path(path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM suggestion_review_runs WHERE task_id = ?", (str(task_id),)
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()
    except Exception as exc:
        _logger().error(f"Failed to read suggestion review task run: {exc}")
        return {}


def get_creation_review_for_mr(
    project_path: str,
    mr_iid: str,
    path: Optional[str] = None,
) -> dict:
    """Return the durable automatic creation review for one MR. Never raises."""
    init_review_tracking(path)
    try:
        conn = connect_sqlite(_db_path(path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT * FROM suggestion_review_runs
                   WHERE project_path = ? AND mr_iid = ? AND (
                       review_scope = 'mr_creation'
                       OR trigger IN ('auto_mr_create', 'historical_auto_mr_create')
                   )
                   ORDER BY started_at DESC LIMIT 1""",
                (str(project_path), str(mr_iid)),
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()
    except Exception as exc:
        _logger().error(f"Failed to read creation review for MR: {exc}")
        return {}


def get_inventory_mr(project_path: str, mr_iid: str, path: Optional[str] = None) -> dict:
    """Return one MR inventory row. Never raises."""
    init_review_tracking(path)
    try:
        conn = connect_sqlite(_db_path(path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM mr_inventory WHERE project_path = ? AND mr_iid = ?",
                (str(project_path), str(mr_iid)),
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()
    except Exception as exc:
        _logger().error(f"Failed to read MR inventory row: {exc}")
        return {}


def mark_creation_recovery(
    project_path: str,
    mr_iid: str,
    state: str,
    reason_code: str | None = None,
    path: Optional[str] = None,
) -> bool:
    """Persist the latest recovery decision for an inventory MR. Never raises."""
    try:
        def write(conn):
            _ensure_schema(conn)
            conn.execute(
                """UPDATE mr_inventory
                   SET creation_recovery_state = ?, creation_reason_code = ?, creation_reason_at = ?
                   WHERE project_path = ? AND mr_iid = ?""",
                (
                    _clean(state, 100), _clean(reason_code, 100), now_cn_iso(),
                    _clean(project_path, 500), _clean(mr_iid, 100),
                ),
            )

        _run_write(_db_path(path), write)
        return True
    except Exception as exc:
        _logger().error(f"Failed to mark creation review recovery: {exc}")
        return False


def increment_sync_metric(
    metric_key: str,
    amount: int = 1,
    path: Optional[str] = None,
) -> bool:
    """Increment one durable inventory-recovery counter. Never raises."""
    if not metric_key:
        return False
    try:
        value = int(amount)

        def write(conn):
            _ensure_schema(conn)
            conn.execute(
                """INSERT INTO suggestion_sync_metrics(metric_key, metric_value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(metric_key) DO UPDATE SET
                       metric_value = metric_value + excluded.metric_value,
                       updated_at = excluded.updated_at""",
                (_clean(metric_key, 100), value, now_cn_iso()),
            )

        _run_write(_db_path(path), write)
        return True
    except Exception as exc:
        _logger().error(f"Failed to increment suggestion sync metric: {exc}")
        return False


def get_sync_metrics(path: Optional[str] = None) -> dict[str, int]:
    """Return durable inventory-recovery counters. Never raises."""
    init_review_tracking(path)
    try:
        conn = connect_sqlite(_db_path(path))
        try:
            return {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT metric_key, metric_value FROM suggestion_sync_metrics"
                ).fetchall()
            }
        finally:
            conn.close()
    except Exception as exc:
        _logger().error(f"Failed to read suggestion sync metrics: {exc}")
        return {}


def project_webhook_suspected(
    project_path: str,
    *,
    now=None,
    threshold: int = 3,
    window_days: int = 7,
    path: Optional[str] = None,
) -> bool:
    """Return true after repeated recent sync-only creations with no webhook evidence."""
    init_review_tracking(path)
    current = now or now_cn()
    cutoff = current - timedelta(days=max(1, int(window_days)))
    try:
        conn = connect_sqlite(_db_path(path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT created_at, discovered_by, webhook_received_at
                   FROM mr_inventory WHERE project_path = ?""",
                (str(project_path),),
            ).fetchall()
        finally:
            conn.close()
        recent = []
        for row in rows:
            created = to_cn(row["created_at"])
            if not created or created < cutoff or created > current:
                continue
            recent.append(row)
        if any(row["webhook_received_at"] for row in recent):
            return False
        sync_only = sum(
            str(row["discovered_by"] or "") in {"incremental_sync", "reconcile"}
            for row in recent
        )
        return sync_only >= max(1, int(threshold))
    except Exception as exc:
        _logger().error(f"Failed to evaluate project webhook coverage: {exc}")
        return False


def record_review_event(
    run_id: str | None,
    event_key: str,
    stage: str,
    *,
    status: str = "running",
    error_code: str | None = None,
    error_message: str | None = None,
    details: dict | None = None,
    path: Optional[str] = None,
) -> bool:
    """Append an idempotent lifecycle event. Never raises."""
    run_id = run_id or get_current_run_id()
    if not run_id or not event_key or not stage:
        return False
    try:
        details_json = json.dumps(details, ensure_ascii=False, sort_keys=True) if details is not None else None

        def write(conn):
            _ensure_schema(conn)
            conn.execute(
                """INSERT OR IGNORE INTO suggestion_review_events (
                       run_id, event_key, stage, status, error_code, error_message, details_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _clean(run_id, 100), _clean(event_key, 100), _clean(stage, 100),
                    _clean(status, 50) or "running", _clean(error_code, 200),
                    _clean(error_message, 1000), details_json, now_cn_iso(),
                ),
            )

        _run_write(_db_path(path), write)
        return True
    except Exception as exc:
        _logger().error(f"Failed to record suggestion review event: {exc}")
        return False


def list_review_events(run_id: str, path: Optional[str] = None) -> list[dict]:
    """Return lifecycle events in first-recorded order. Never raises."""
    if not run_id:
        return []
    init_review_tracking(path)
    try:
        conn = connect_sqlite(_db_path(path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM suggestion_review_events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
            events = []
            for row in rows:
                event = dict(row)
                raw_details = event.pop("details_json", None)
                try:
                    event["details"] = json.loads(raw_details) if raw_details else {}
                except (TypeError, ValueError):
                    event["details"] = {}
                events.append(event)
            return events
        finally:
            conn.close()
    except Exception as exc:
        _logger().error(f"Failed to list suggestion review events: {exc}")
        return []


@dataclass(frozen=True)
class ReviewAlertTransition:
    alert_key: str
    active: bool
    count: int
    should_emit: bool
    resolved: bool


def count_review_alert_signals(since: str, path: Optional[str] = None) -> dict[str, int]:
    """Count distinct affected review runs since the supplied timestamp. Never raises."""
    signals = {"model_failures": 0, "startup_retry_exhausted": 0, "publish_fallbacks": 0}
    try:
        conn = connect_sqlite(_db_path(path))
        try:
            _ensure_schema(conn)
            signals["model_failures"] = int(conn.execute(
                "SELECT COUNT(DISTINCT run_id) FROM suggestion_review_events "
                "WHERE created_at >= ? AND event_key LIKE 'model_attempt_failed:%'",
                (str(since),),
            ).fetchone()[0] or 0)
            signals["startup_retry_exhausted"] = int(conn.execute(
                "SELECT COUNT(DISTINCT run_id) FROM suggestion_review_runs "
                "WHERE updated_at >= ? AND stage = 'startup_failed' AND error_code = 'QueueStartupTimeout'",
                (str(since),),
            ).fetchone()[0] or 0)
            signals["publish_fallbacks"] = int(conn.execute(
                "SELECT COUNT(DISTINCT run_id) FROM suggestion_review_runs "
                "WHERE updated_at >= ? AND inline_fallback_count > 0",
                (str(since),),
            ).fetchone()[0] or 0)
        finally:
            conn.close()
        return signals
    except Exception as exc:
        _logger().error(f"Failed to count suggestion review alert signals: {exc}")
        return signals


def update_review_alert_state(
    alert_key: str,
    *,
    active: bool,
    count: int,
    cooldown_seconds: int,
    path: Optional[str] = None,
) -> ReviewAlertTransition:
    """Atomically update alert state and decide whether this process should emit."""
    key = _clean(alert_key, 100) or ""
    count = max(0, int(count or 0))
    if not key:
        return ReviewAlertTransition("", False, count, False, False)
    current = now_cn()
    current_iso = current.isoformat()
    cooldown_seconds = max(0, int(cooldown_seconds or 0))
    try:
        def write(conn):
            _ensure_schema(conn)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM suggestion_review_alert_state WHERE alert_key = ?", (key,)
            ).fetchone()
            previous = dict(row) if row is not None else {}
            was_active = bool(previous.get("active"))
            last_emitted = to_cn(previous.get("last_emitted_at"))
            should_emit = bool(active and (
                not was_active
                or not last_emitted
                or (current - last_emitted).total_seconds() >= cooldown_seconds
            ))
            resolved = bool(was_active and not active)
            first_triggered_at = previous.get("first_triggered_at")
            if active and not was_active:
                first_triggered_at = current_iso
            last_emitted_at = current_iso if should_emit else previous.get("last_emitted_at")
            resolved_at = current_iso if resolved else (None if active else previous.get("resolved_at"))
            conn.execute(
                """INSERT INTO suggestion_review_alert_state (
                       alert_key, active, last_count, first_triggered_at, last_evaluated_at,
                       last_emitted_at, resolved_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(alert_key) DO UPDATE SET
                       active=excluded.active, last_count=excluded.last_count,
                       first_triggered_at=excluded.first_triggered_at,
                       last_evaluated_at=excluded.last_evaluated_at,
                       last_emitted_at=excluded.last_emitted_at, resolved_at=excluded.resolved_at""",
                (
                    key, int(bool(active)), count, first_triggered_at, current_iso,
                    last_emitted_at, resolved_at,
                ),
            )
            return ReviewAlertTransition(key, bool(active), count, should_emit, resolved)

        return _run_write(_db_path(path), write)
    except Exception as exc:
        _logger().error(f"Failed to update suggestion review alert state: {exc}")
        return ReviewAlertTransition(key, bool(active), count, False, False)


def list_active_review_alerts(path: Optional[str] = None) -> list[dict]:
    """Return persisted active aggregate alerts. Never raises."""
    init_review_tracking(path)
    try:
        conn = connect_sqlite(_db_path(path))
        conn.row_factory = sqlite3.Row
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT alert_key, last_count, first_triggered_at, last_evaluated_at, last_emitted_at "
                    "FROM suggestion_review_alert_state WHERE active = 1 ORDER BY alert_key"
                ).fetchall()
            ]
        finally:
            conn.close()
    except Exception as exc:
        _logger().error(f"Failed to list active suggestion review alerts: {exc}")
        return []


def claim_sync_lease(sync_name: str, owner: str, lease_seconds: int = 120,
                     path: Optional[str] = None) -> bool:
    now = now_cn()
    now_value = now.isoformat()
    lease_until = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
    try:
        def write(conn):
            _ensure_schema(conn)
            conn.execute("INSERT OR IGNORE INTO suggestion_sync_state(sync_name) VALUES (?)", (sync_name,))
            cursor = conn.execute(
                """UPDATE suggestion_sync_state
                   SET lease_owner = ?, lease_until = ?, last_started_at = ?
                   WHERE sync_name = ? AND (
                       lease_until IS NULL OR lease_until < ? OR lease_owner = ?
                   )""",
                (owner, lease_until, now_value, sync_name, now_value, owner),
            )
            return cursor.rowcount == 1

        return bool(_run_write(_db_path(path), write))
    except Exception as exc:
        _logger().error(f"Failed to claim suggestion sync lease: {exc}")
        return False


def complete_sync(sync_name: str, owner: str, *, cursor_at: str | None = None,
                  reconciled: bool = False, error: str | None = None,
                  path: Optional[str] = None) -> bool:
    now = now_cn_iso()
    try:
        def write(conn):
            _ensure_schema(conn)
            sets = ["lease_owner = NULL", "lease_until = NULL", "last_error = ?"]
            values: list[object] = [_clean(error, 1000)]
            if error is None:
                sets.append("last_success_at = ?")
                values.append(now)
                if cursor_at:
                    sets.append("cursor_at = ?")
                    values.append(cursor_at)
                if reconciled:
                    sets.append("last_reconcile_at = ?")
                    values.append(now)
            values.extend([sync_name, owner])
            conn.execute(
                f"UPDATE suggestion_sync_state SET {', '.join(sets)} WHERE sync_name = ? AND lease_owner = ?",
                values,
            )

        _run_write(_db_path(path), write)
        return True
    except Exception as exc:
        _logger().error(f"Failed to complete suggestion sync: {exc}")
        return False


def get_sync_state(sync_name: str = "gitlab_mr_inventory", path: Optional[str] = None) -> dict:
    init_review_tracking(path)
    try:
        conn = connect_sqlite(_db_path(path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM suggestion_sync_state WHERE sync_name = ?", (sync_name,)
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()
    except Exception as exc:
        _logger().error(f"Failed to read suggestion sync state: {exc}")
        return {}
