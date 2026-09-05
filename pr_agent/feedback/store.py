"""SQLite storage backend for PR-Agent review feedback.

Stores user ratings/comments about review results into a single SQLite file so
they can later be analyzed/evaluated. SQLite ships with Python's standard
library, so no extra dependency or service is required.

The database file is a single file that should live on a mounted host volume in
production (e.g. ``-v /opt/pr-agent/data:/app/data``) so it survives container
restarts.
"""

import hashlib
import json
import sqlite3
import threading
from typing import Optional

from pr_agent.config_loader import get_settings
from pr_agent.feedback.timez import now_cn_iso
from pr_agent.log import get_logger
from pr_agent.storage.sqlite import connect_sqlite, run_write_transaction

# Default location inside the container. In production this directory should be
# backed by a mounted host volume so the data is not lost on container restart.
DEFAULT_DB_PATH = "/app/data/feedback/review_feedback.db"

# Serialize writes within the process. The gitlab_webhook server runs as a single
# uvicorn process, so a process-level lock plus SQLite transactions is enough to
# keep concurrent feedback writes safe.
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    pr_url TEXT,
    project TEXT,
    mr_iid TEXT,
    mr_author TEXT,
    reviewer_user TEXT,
    score INTEGER NOT NULL,
    comment TEXT,
    review_id TEXT,
    commit_sha TEXT,
    model TEXT,
    source TEXT,
    extra_json TEXT,
    task_id TEXT,
    effect_id TEXT
);
"""

_SKILL_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_skill_usages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    review_id TEXT NOT NULL,
    command TEXT NOT NULL,
    project TEXT,
    mr_iid TEXT,
    target_branch TEXT,
    target_sha TEXT,
    skill_hash TEXT,
    manifest_hash TEXT,
    load_status TEXT,
    selected_rule_ids_json TEXT,
    matched_files_json TEXT,
    reference_hashes_json TEXT,
    global_prompt_set_hash TEXT,
    prompt_bundle_hash TEXT,
    truncated INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    UNIQUE(review_id, command)
);
CREATE INDEX IF NOT EXISTS idx_project_skill_usages_project_mr
    ON project_skill_usages(project, mr_iid);
"""

_EVOLUTION_CASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL UNIQUE,
    case_hash TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    project TEXT NOT NULL,
    mr_iid TEXT NOT NULL,
    review_id TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    command TEXT NOT NULL,
    description TEXT NOT NULL,
    expected_action TEXT NOT NULL,
    source TEXT NOT NULL,
    file_path TEXT,
    line_start INTEGER NOT NULL DEFAULT 0,
    line_end INTEGER NOT NULL DEFAULT 0,
    suggestion_id TEXT,
    error_code TEXT,
    global_prompt_set_hash TEXT,
    prompt_bundle_hash TEXT,
    project_skill_hash TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evolution_cases_project_mr
    ON evolution_cases(project, mr_iid, created_at);
CREATE INDEX IF NOT EXISTS idx_evolution_cases_review
    ON evolution_cases(review_id);
"""


def _ensure_skill_usage_columns(conn: sqlite3.Connection) -> None:
    for name in ("global_prompt_set_hash", "prompt_bundle_hash"):
        try:
            conn.execute(f"ALTER TABLE project_skill_usages ADD COLUMN {name} TEXT")
        except sqlite3.OperationalError:
            pass


def get_db_path() -> str:
    """Return the configured SQLite path, falling back to the default."""
    try:
        path = get_settings().get("pr_feedback.storage_path", DEFAULT_DB_PATH)
    except Exception:
        path = DEFAULT_DB_PATH
    return path or DEFAULT_DB_PATH


def _connect(path: str) -> sqlite3.Connection:
    return connect_sqlite(path)


def _ensure_identity_columns(conn: sqlite3.Connection) -> None:
    for name in ("task_id", "effect_id"):
        try:
            conn.execute(f"ALTER TABLE review_feedback ADD COLUMN {name} TEXT")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_review_feedback_effect_id "
        "ON review_feedback(effect_id) WHERE effect_id IS NOT NULL"
    )


def _task_identity(record: dict) -> tuple[str | None, str | None]:
    task_id = record.get("task_id")
    if not task_id:
        try:
            from pr_agent.distributed.runtime import get_execution_runtime

            runtime = get_execution_runtime()
            task_id = runtime.task_id if runtime is not None else None
        except Exception:
            task_id = None
    if not task_id:
        return None, None
    value = json.dumps(
        {
            "review_id": record.get("review_id"),
            "reviewer_user": record.get("reviewer_user"),
            "score": record.get("score"),
            "comment": record.get("comment"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return str(task_id), f"{task_id}:feedback:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def init_db(path: Optional[str] = None) -> None:
    """Create the feedback table if it does not yet exist."""
    path = path or get_db_path()
    def initialize(conn):
        conn.execute(_SCHEMA)
        conn.executescript(_SKILL_USAGE_SCHEMA)
        conn.executescript(_EVOLUTION_CASE_SCHEMA)
        _ensure_skill_usage_columns(conn)
        _ensure_identity_columns(conn)

    run_write_transaction(path, initialize, connect=_connect)


def save_feedback(record: dict, path: Optional[str] = None) -> bool:
    """Persist a single feedback record.

    Returns True on success, False on failure (never raises, so a storage
    problem cannot break the webhook flow).
    """
    path = path or get_db_path()
    created_at = record.get("created_at") or now_cn_iso()
    task_id, effect_id = _task_identity(record)
    extra = record.get("extra")
    if extra is not None and not isinstance(extra, str):
        try:
            extra = json.dumps(extra, ensure_ascii=False)
        except Exception:
            extra = None

    try:
        with _write_lock:
            def write(conn):
                conn.execute(_SCHEMA)
                _ensure_identity_columns(conn)
                conn.execute(
                    """
                    INSERT INTO review_feedback (
                        created_at, pr_url, project, mr_iid, mr_author,
                        reviewer_user, score, comment, review_id, commit_sha,
                        model, source, extra_json, task_id, effect_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(effect_id) WHERE effect_id IS NOT NULL DO NOTHING
                    """,
                    (
                        created_at,
                        record.get("pr_url"),
                        str(record.get("project")) if record.get("project") is not None else None,
                        str(record.get("mr_iid")) if record.get("mr_iid") is not None else None,
                        record.get("mr_author"),
                        record.get("reviewer_user"),
                        int(record.get("score")),
                        record.get("comment"),
                        record.get("review_id"),
                        record.get("commit_sha"),
                        record.get("model"),
                        record.get("source"),
                        extra,
                        task_id,
                        effect_id,
                    ),
                )
            run_write_transaction(path, write, connect=_connect)
        return True
    except Exception as e:
        get_logger().error(f"Failed to save review feedback: {e}")
        return False


def save_evolution_case(record: dict, path: Optional[str] = None) -> bool:
    """Validate and idempotently persist one reproducible evolution case."""
    from pr_agent.suggestions.prompt_evolution.cases import build_evolution_case

    path = path or get_db_path()
    values = dict(record)
    values["created_at"] = values.get("created_at") or now_cn_iso()
    try:
        case = build_evolution_case(values)
    except (TypeError, ValueError) as exc:
        get_logger().warning(f"Rejected invalid evolution case: {exc}")
        return False
    try:
        def write(conn):
            conn.executescript(_EVOLUTION_CASE_SCHEMA)
            conn.execute(
                """
                INSERT INTO evolution_cases (
                    case_id, case_hash, schema_version, kind, project, mr_iid, review_id,
                    head_sha, command, description, expected_action, source, file_path,
                    line_start, line_end, suggestion_id, error_code, global_prompt_set_hash,
                    prompt_bundle_hash, project_skill_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_hash) DO NOTHING
                """,
                (
                    case.case_id,
                    case.case_hash,
                    case.schema_version,
                    case.kind.value,
                    case.project,
                    case.mr_iid,
                    case.review_id,
                    case.head_sha,
                    case.command,
                    case.description,
                    case.expected_action,
                    case.source,
                    case.file_path,
                    case.line_start,
                    case.line_end,
                    case.suggestion_id,
                    case.error_code,
                    case.global_prompt_set_hash,
                    case.prompt_bundle_hash,
                    case.project_skill_hash,
                    case.created_at,
                ),
            )

        with _write_lock:
            run_write_transaction(path, write, connect=_connect)
        return True
    except Exception as exc:
        get_logger().error(f"Failed to save evolution case: {exc}")
        return False


def list_evolution_cases(path: Optional[str] = None, *, project: str = "") -> list[dict]:
    """Return validated case rows in deterministic creation order; never raises."""
    path = path or get_db_path()
    try:
        conn = _connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_EVOLUTION_CASE_SCHEMA)
            if project:
                rows = conn.execute(
                    "SELECT * FROM evolution_cases WHERE project = ? ORDER BY created_at, id",
                    (str(project),),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM evolution_cases ORDER BY created_at, id").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        get_logger().error(f"Failed to list evolution cases: {exc}")
        return []


def save_project_skill_usage(record: dict, path: Optional[str] = None) -> bool:
    """Persist immutable review/improve Skill provenance; never blocks publishing."""
    path = path or get_db_path()
    try:
        def write(conn):
            conn.executescript(_SKILL_USAGE_SCHEMA)
            _ensure_skill_usage_columns(conn)
            conn.execute(
                """
                INSERT INTO project_skill_usages (
                    created_at, review_id, command, project, mr_iid, target_branch, target_sha,
                    skill_hash, manifest_hash, load_status, selected_rule_ids_json,
                    matched_files_json, reference_hashes_json, global_prompt_set_hash,
                    prompt_bundle_hash, truncated, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id, command) DO NOTHING
                """,
                (
                    record.get("created_at") or now_cn_iso(),
                    str(record.get("review_id") or ""),
                    str(record.get("command") or ""),
                    str(record.get("project") or ""),
                    str(record.get("mr_iid") or ""),
                    str(record.get("target_branch") or ""),
                    str(record.get("target_sha") or ""),
                    str(record.get("skill_hash") or ""),
                    str(record.get("manifest_hash") or ""),
                    str(record.get("load_status") or ""),
                    json.dumps(record.get("selected_rule_ids") or [], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(record.get("matched_files") or {}, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")),
                    json.dumps(record.get("reference_hashes") or {}, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")),
                    str(record.get("global_prompt_set_hash") or ""),
                    str(record.get("prompt_bundle_hash") or ""),
                    1 if record.get("truncated") else 0,
                    str(record.get("error") or "")[:1_000],
                ),
            )

        with _write_lock:
            run_write_transaction(path, write, connect=_connect)
        return True
    except Exception as exc:
        get_logger().error(f"Failed to save project Skill usage: {exc}")
        return False


def get_project_skill_usages(project, mr_iid, path: Optional[str] = None) -> list[dict]:
    """Return Skill usage rows for one MR; never raises."""
    path = path or get_db_path()
    try:
        conn = _connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SKILL_USAGE_SCHEMA)
            rows = conn.execute(
                "SELECT * FROM project_skill_usages WHERE project = ? AND mr_iid = ? ORDER BY id",
                (str(project or ""), str(mr_iid or "")),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        get_logger().error(f"Failed to query project Skill usage: {exc}")
        return []


def has_feedback(project, mr_iid, path: Optional[str] = None) -> bool:
    """Return True if at least one feedback record exists for the given MR.

    project/mr_iid are compared as strings to match how save_feedback stores
    them. Never raises; returns False on any error (conservative: prefer
    keeping the merge gated rather than falsely unlocking it).
    """
    path = path or get_db_path()
    try:
        conn = _connect(path)
        try:
            conn.execute(_SCHEMA)
            cur = conn.execute(
                "SELECT 1 FROM review_feedback WHERE project = ? AND mr_iid = ? LIMIT 1",
                (
                    str(project) if project is not None else None,
                    str(mr_iid) if mr_iid is not None else None,
                ),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        get_logger().error(f"Failed to query review feedback: {e}")
        return False
