"""SQLite storage for published inline code-suggestion threads.

Mirrors :mod:`pr_agent.feedback.store`: a single SQLite file, process-level
write lock, and functions that never raise so a storage problem can never break
the MR flow. Rows are written for both published and skipped suggestions so the
data is available for phase-2 status tracking / gating.
"""

import hashlib
import json
import sqlite3
import threading
from typing import Optional

from pr_agent.config_loader import get_settings
from pr_agent.feedback.store import DEFAULT_DB_PATH
from pr_agent.feedback.timez import now_cn_iso
from pr_agent.log import get_logger
from pr_agent.storage.sqlite import connect_sqlite, run_write_transaction

_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestion_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    suggestion_id TEXT,
    review_id TEXT,
    project TEXT,
    mr_iid TEXT,
    mr_url TEXT,
    mr_author TEXT,

    commit_sha TEXT,
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    label TEXT,
    severity TEXT,
    score INTEGER,
    one_sentence_summary TEXT,
    suggestion_content TEXT,
    existing_code TEXT,
    improved_code TEXT,
    gitlab_discussion_id TEXT,
    gitlab_note_id TEXT,
    publish_status TEXT,
    skip_reason TEXT,
    state TEXT,
    extra_json TEXT,
    task_id TEXT,
    effect_id TEXT,
    run_id TEXT
);
"""

_COLUMNS = [
    "created_at", "updated_at", "suggestion_id", "review_id", "project", "mr_iid",
    "mr_url", "mr_author", "commit_sha", "file_path", "line_start", "line_end", "label", "severity",
    "score", "one_sentence_summary", "suggestion_content", "existing_code",
    "improved_code", "gitlab_discussion_id", "gitlab_note_id", "publish_status",
    "skip_reason", "state", "extra_json", "task_id", "effect_id", "run_id",
]


_PUBLISHED_SCHEMA = """
CREATE TABLE IF NOT EXISTS published_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    suggestion_id TEXT,
    review_id TEXT,
    project TEXT,
    mr_iid TEXT,
    mr_url TEXT,
    mr_author TEXT,
    commit_sha TEXT,
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    label TEXT,
    severity TEXT,
    score INTEGER,
    one_sentence_summary TEXT,
    suggestion_content TEXT,
    existing_code TEXT,
    improved_code TEXT,
    gitlab_discussion_id TEXT,
    gitlab_note_id TEXT,
    state TEXT,
    extra_json TEXT,
    applied_at TEXT,
    apply_user TEXT,
    resolved_by_stage TEXT,
    tier2_duration_ms INTEGER,
    task_id TEXT,
    effect_id TEXT,
    run_id TEXT,
    global_prompt_set_hash TEXT,
    project_rules_hash TEXT,
    prompt_bundle_hash TEXT,
    prompt_version TEXT,
    project_skill_hash TEXT,
    project_skill_manifest_hash TEXT,
    project_skill_target_sha TEXT,
    project_skill_status TEXT,
    project_skill_rule_ids_json TEXT,
    project_skill_matched_files_json TEXT,
    project_skill_reference_hashes_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_published_discussion
    ON published_suggestions (gitlab_discussion_id);
"""

_PUBLISHED_COLUMNS = [
    "created_at", "updated_at", "suggestion_id", "review_id", "project", "mr_iid",
    "mr_url", "mr_author", "commit_sha", "file_path", "line_start", "line_end", "label", "severity",
    "score", "one_sentence_summary", "suggestion_content", "existing_code",
    "improved_code", "gitlab_discussion_id", "gitlab_note_id", "state", "extra_json",
    "resolved_by_stage", "tier2_duration_ms", "task_id", "effect_id", "run_id",
    "global_prompt_set_hash", "project_rules_hash", "prompt_bundle_hash", "prompt_version",
    "project_skill_hash", "project_skill_manifest_hash", "project_skill_target_sha", "project_skill_status",
    "project_skill_rule_ids_json", "project_skill_matched_files_json", "project_skill_reference_hashes_json",
]


def get_db_path() -> str:
    """Return the configured SQLite path.

    Prefers ``pr_code_suggestions.inline_suggestions_storage_path``; when empty
    it reuses ``pr_feedback.storage_path`` so all local data lives in one file.
    """
    try:
        path = get_settings().get("pr_code_suggestions.inline_suggestions_storage_path", "")
        if not path:
            path = get_settings().get("pr_feedback.storage_path", DEFAULT_DB_PATH)
    except Exception:
        path = DEFAULT_DB_PATH
    return path or DEFAULT_DB_PATH


def _connect(path: str) -> sqlite3.Connection:
    return connect_sqlite(path)


def _runtime_task_id(record: dict) -> str | None:
    if record.get("task_id"):
        return str(record["task_id"])
    try:
        from pr_agent.distributed.runtime import get_execution_runtime

        runtime = get_execution_runtime()
        return runtime.task_id if runtime is not None else None
    except Exception:
        return None


def _effect_identity(record: dict, table: str) -> tuple[str | None, str | None]:
    task_id = _runtime_task_id(record)
    if not task_id:
        return None, None
    payload = json.dumps(
        {
            "suggestion_id": record.get("suggestion_id"),
            "review_id": record.get("review_id"),
            "discussion_id": record.get("gitlab_discussion_id"),
            "file_path": record.get("file_path"),
            "line_start": record.get("line_start"),
            "publish_status": record.get("publish_status"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return task_id, f"{task_id}:{table}:{digest}"


def _review_run_id(record: dict) -> str | None:
    if record.get("run_id"):
        return str(record["run_id"])
    try:
        from pr_agent.suggestions.review_tracking import get_current_run_id

        return get_current_run_id()
    except Exception:
        return None


def _ensure_identity_columns(conn: sqlite3.Connection, table: str) -> None:
    for name in ("task_id", "effect_id"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{table}_effect_id "
        f"ON {table}(effect_id) WHERE effect_id IS NOT NULL"
    )


def _ensure_run_id_column(conn: sqlite3.Connection, table: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN run_id TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_run_id ON {table}(run_id)")


_PROMPT_PROVENANCE_COLUMN_TYPES = (
    ("global_prompt_set_hash", "TEXT"),
    ("project_rules_hash", "TEXT"),
    ("prompt_bundle_hash", "TEXT"),
    ("prompt_version", "TEXT"),
    ("project_skill_hash", "TEXT"),
    ("project_skill_manifest_hash", "TEXT"),
    ("project_skill_target_sha", "TEXT"),
    ("project_skill_status", "TEXT"),
    ("project_skill_rule_ids_json", "TEXT"),
    ("project_skill_matched_files_json", "TEXT"),
    ("project_skill_reference_hashes_json", "TEXT"),
)


def _ensure_prompt_provenance_columns(conn: sqlite3.Connection) -> None:
    for name, column_type in _PROMPT_PROVENANCE_COLUMN_TYPES:
        try:
            conn.execute(f"ALTER TABLE published_suggestions ADD COLUMN {name} {column_type}")
        except sqlite3.OperationalError:
            pass


def _run_write(path: str, operation):
    with _write_lock:
        return run_write_transaction(path, operation, connect=_connect)


def init_db(path: Optional[str] = None) -> None:
    """Create the suggestion_threads table if it does not yet exist."""
    path = path or get_db_path()
    def initialize(conn):
        conn.execute(_SCHEMA)
        _ensure_identity_columns(conn, "suggestion_threads")
        _ensure_run_id_column(conn, "suggestion_threads")

    _run_write(path, initialize)


def _str_or_none(value):
    return str(value) if value is not None else None


def _ensure_mr_url_column(conn: sqlite3.Connection, table: str) -> None:
    """Add the mr_url/mr_author columns to a table created before they existed.

    No-op (via try/except) once the columns are present. Never raises.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN mr_url TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN mr_author TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists


def save_suggestion_thread(record: dict, path: Optional[str] = None) -> bool:
    """Persist a single suggestion record.

    Published suggestions are routed to the dedicated ``published_suggestions``
    table; skipped / failed ones stay in ``suggestion_threads``.

    Returns True on success, False on failure (never raises).
    """
    path = path or get_db_path()
    if (record.get("publish_status") or "") == "published":
        return save_published_suggestion(record, path=path)
    now = now_cn_iso()
    extra = record.get("extra")
    if extra is not None and not isinstance(extra, str):
        try:
            extra = json.dumps(extra, ensure_ascii=False)
        except Exception:
            extra = None

    score = record.get("score")
    try:
        score = int(score) if score is not None and score != "" else None
    except Exception:
        score = None
    task_id, effect_id = _effect_identity(record, "suggestion_threads")

    values = (
        record.get("created_at") or now,
        record.get("updated_at") or now,
        record.get("suggestion_id"),
        record.get("review_id"),
        _str_or_none(record.get("project")),
        _str_or_none(record.get("mr_iid")),
        record.get("mr_url"),
        record.get("mr_author"),
        record.get("commit_sha"),
        record.get("file_path"),
        record.get("line_start"),
        record.get("line_end"),
        record.get("label"),
        record.get("severity"),
        score,
        record.get("one_sentence_summary"),
        record.get("suggestion_content"),
        record.get("existing_code"),
        record.get("improved_code"),
        _str_or_none(record.get("gitlab_discussion_id")),
        _str_or_none(record.get("gitlab_note_id")),
        record.get("publish_status"),
        record.get("skip_reason"),
        record.get("state"),
        extra,
        task_id,
        effect_id,
        _review_run_id(record),
    )
    placeholders = ", ".join(["?"] * len(_COLUMNS))
    try:
        def write(conn):
            conn.execute(_SCHEMA)
            _ensure_mr_url_column(conn, "suggestion_threads")
            _ensure_identity_columns(conn, "suggestion_threads")
            _ensure_run_id_column(conn, "suggestion_threads")
            conn.execute(
                f"INSERT INTO suggestion_threads ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
                "ON CONFLICT(effect_id) WHERE effect_id IS NOT NULL DO UPDATE SET "
                "updated_at=excluded.updated_at, publish_status=excluded.publish_status, "
                "skip_reason=excluded.skip_reason, state=excluded.state, extra_json=excluded.extra_json, "
                "run_id=COALESCE(excluded.run_id, suggestion_threads.run_id)",
                values,
            )

        _run_write(path, write)
        return True
    except Exception as e:
        get_logger().error(f"Failed to save suggestion thread: {e}")
        return False


def _published_values(record: dict):
    now = now_cn_iso()
    extra = record.get("extra")
    if extra is not None and not isinstance(extra, str):
        try:
            extra = json.dumps(extra, ensure_ascii=False)
        except Exception:
            extra = None
    score = record.get("score")
    try:
        score = int(score) if score is not None and score != "" else None
    except Exception:
        score = None
    tier2_duration_ms = record.get("tier2_duration_ms")
    try:
        tier2_duration_ms = (
            int(tier2_duration_ms) if tier2_duration_ms is not None and tier2_duration_ms != "" else None
        )
    except Exception:
        tier2_duration_ms = None
    task_id, effect_id = _effect_identity(record, "published_suggestions")
    return (
        record.get("created_at") or now,
        record.get("updated_at") or now,
        record.get("suggestion_id"),
        record.get("review_id"),
        _str_or_none(record.get("project")),
        _str_or_none(record.get("mr_iid")),
        record.get("mr_url"),
        record.get("mr_author"),
        record.get("commit_sha"),
        record.get("file_path"),
        record.get("line_start"),
        record.get("line_end"),
        record.get("label"),
        record.get("severity"),
        score,
        record.get("one_sentence_summary"),
        record.get("suggestion_content"),
        record.get("existing_code"),
        record.get("improved_code"),
        _str_or_none(record.get("gitlab_discussion_id")),
        _str_or_none(record.get("gitlab_note_id")),
        record.get("state"),
        extra,
        record.get("resolved_by_stage"),
        tier2_duration_ms,
        task_id,
        effect_id,
        _review_run_id(record),
        record.get("global_prompt_set_hash"),
        record.get("project_rules_hash"),
        record.get("prompt_bundle_hash"),
        record.get("prompt_version"),
        record.get("project_skill_hash"),
        record.get("project_skill_manifest_hash"),
        record.get("project_skill_target_sha"),
        record.get("project_skill_status"),
        record.get("project_skill_rule_ids_json"),
        record.get("project_skill_matched_files_json"),
        record.get("project_skill_reference_hashes_json"),
    )


def save_published_suggestion(record: dict, path: Optional[str] = None) -> bool:
    """Persist a published suggestion into ``published_suggestions``.

    Returns True on success, False on failure (never raises).
    """
    path = path or get_db_path()
    values = _published_values(record)
    placeholders = ", ".join(["?"] * len(_PUBLISHED_COLUMNS))
    try:
        def write(conn):
            conn.executescript(_PUBLISHED_SCHEMA)
            _ensure_mr_url_column(conn, "published_suggestions")
            _ensure_identity_columns(conn, "published_suggestions")
            _ensure_run_id_column(conn, "published_suggestions")
            _ensure_prompt_provenance_columns(conn)
            conn.execute(
                f"INSERT INTO published_suggestions "
                f"({', '.join(_PUBLISHED_COLUMNS)}) VALUES ({placeholders}) "
                "ON CONFLICT(effect_id) WHERE effect_id IS NOT NULL DO UPDATE SET "
                "updated_at=excluded.updated_at, gitlab_discussion_id=excluded.gitlab_discussion_id, "
                "gitlab_note_id=excluded.gitlab_note_id, state=excluded.state, extra_json=excluded.extra_json, "
                "run_id=COALESCE(excluded.run_id, published_suggestions.run_id)",
                values,
            )

        _run_write(path, write)
        return True
    except Exception as e:
        get_logger().error(f"Failed to save published suggestion: {e}")
        return False


# --------------------------------------------------------------------------- #
# filtered_suggestions table (scenario_validation cross-review filtered)
# --------------------------------------------------------------------------- #

_FILTERED_SCHEMA = """
CREATE TABLE IF NOT EXISTS filtered_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    review_id TEXT,
    project TEXT,
    mr_iid TEXT,
    mr_url TEXT,
    mr_author TEXT,
    commit_sha TEXT,
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    label TEXT,
    severity TEXT,
    score INTEGER,
    one_sentence_summary TEXT,
    suggestion_content TEXT,
    existing_code TEXT,
    improved_code TEXT,
    filter_stage TEXT,
    skip_reason TEXT,
    judge_model TEXT,
    extra_json TEXT,
    task_id TEXT,
    effect_id TEXT,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_filtered_project_mr
    ON filtered_suggestions (project, mr_iid);
CREATE INDEX IF NOT EXISTS idx_filtered_created
    ON filtered_suggestions (created_at);
"""

_FILTERED_COLUMNS = [
    "created_at", "review_id", "project", "mr_iid", "mr_url", "mr_author",
    "commit_sha", "file_path", "line_start", "line_end", "label", "severity",
    "score", "one_sentence_summary", "suggestion_content", "existing_code",
    "improved_code", "filter_stage", "skip_reason", "judge_model", "extra_json", "task_id", "effect_id", "run_id",
]


def init_filtered_table(path: Optional[str] = None) -> None:
    """Create the filtered_suggestions table if it does not yet exist. Idempotent."""
    path = path or get_db_path()
    def initialize(conn):
        conn.executescript(_FILTERED_SCHEMA)
        _ensure_identity_columns(conn, "filtered_suggestions")
        _ensure_run_id_column(conn, "filtered_suggestions")

    _run_write(path, initialize)


def save_filtered_suggestion(record: dict, path: Optional[str] = None) -> bool:
    """Persist a single cross-review-filtered suggestion into filtered_suggestions.

    Returns True on success, False on failure (never raises, so a storage
    problem cannot break the /improve flow).
    """
    path = path or get_db_path()
    now = now_cn_iso()
    extra = record.get("extra")
    if extra is not None and not isinstance(extra, str):
        try:
            extra = json.dumps(extra, ensure_ascii=False)
        except Exception:
            extra = None

    score = record.get("score")
    try:
        score = int(score) if score is not None and score != "" else None
    except Exception:
        score = None
    task_id, effect_id = _effect_identity(record, "filtered_suggestions")

    values = (
        record.get("created_at") or now,
        record.get("review_id"),
        _str_or_none(record.get("project")),
        _str_or_none(record.get("mr_iid")),
        record.get("mr_url"),
        record.get("mr_author"),
        record.get("commit_sha"),
        record.get("file_path"),
        record.get("line_start"),
        record.get("line_end"),
        record.get("label"),
        record.get("severity"),
        score,
        record.get("one_sentence_summary"),
        record.get("suggestion_content"),
        record.get("existing_code"),
        record.get("improved_code"),
        record.get("filter_stage") or "scenario_validation",
        record.get("skip_reason"),
        record.get("judge_model"),
        extra,
        task_id,
        effect_id,
        _review_run_id(record),
    )
    placeholders = ", ".join(["?"] * len(_FILTERED_COLUMNS))
    try:
        def write(conn):
            conn.executescript(_FILTERED_SCHEMA)
            _ensure_identity_columns(conn, "filtered_suggestions")
            _ensure_run_id_column(conn, "filtered_suggestions")
            conn.execute(
                f"INSERT INTO filtered_suggestions "
                f"({', '.join(_FILTERED_COLUMNS)}) VALUES ({placeholders}) "
                "ON CONFLICT(effect_id) WHERE effect_id IS NOT NULL DO UPDATE SET "
                "skip_reason=excluded.skip_reason, judge_model=excluded.judge_model, extra_json=excluded.extra_json, "
                "run_id=COALESCE(excluded.run_id, filtered_suggestions.run_id)",
                values,
            )

        _run_write(path, write)
        return True
    except Exception as e:
        get_logger().error(f"Failed to save filtered suggestion: {e}")
        return False


def get_published_suggestions(project, mr_iid, path: Optional[str] = None) -> list:
    """Return published-suggestion rows for the given MR as dicts. Never raises."""
    path = path or get_db_path()
    try:
        conn = _connect(path)
        try:
            conn.executescript(_PUBLISHED_SCHEMA)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM published_suggestions "
                "WHERE project = ? AND mr_iid = ? ORDER BY id",
                (_str_or_none(project), _str_or_none(mr_iid)),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        get_logger().error(f"Failed to query published suggestions: {e}")
        return []


def get_filtered_suggestions(project, mr_iid, path: Optional[str] = None) -> list:
    """Return filtered-suggestion rows for the given MR as dicts. Never raises."""
    path = path or get_db_path()
    try:
        conn = _connect(path)
        try:
            conn.executescript(_FILTERED_SCHEMA)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM filtered_suggestions WHERE project = ? AND mr_iid = ? ORDER BY id",
                (_str_or_none(project), _str_or_none(mr_iid)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        get_logger().error(f"Failed to query filtered suggestions: {exc}")
        return []


def get_suggestion_threads(project, mr_iid, path: Optional[str] = None) -> list:
    """Return all suggestion-thread rows for the given MR as dicts.

    Never raises; returns an empty list on any error.
    """
    path = path or get_db_path()
    try:
        conn = _connect(path)
        try:
            conn.execute(_SCHEMA)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM suggestion_threads WHERE project = ? AND mr_iid = ? ORDER BY id",
                (_str_or_none(project), _str_or_none(mr_iid)),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        get_logger().error(f"Failed to query suggestion threads: {e}")
        return []


_RUN_SCOPED_TABLES = {
    "suggestion_threads": _SCHEMA,
    "published_suggestions": _PUBLISHED_SCHEMA,
    "filtered_suggestions": _FILTERED_SCHEMA,
}


def _get_rows_for_run(table: str, run_id: str, path: Optional[str] = None) -> list[dict]:
    if not run_id or table not in _RUN_SCOPED_TABLES:
        return []
    path = path or get_db_path()
    migrate_schema(path)
    try:
        conn = _connect(path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(f"SELECT * FROM {table} WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        get_logger().error(f"Failed to query {table} by review run: {exc}")
        return []


def get_filtered_suggestions_for_run(run_id: str, path: Optional[str] = None) -> list[dict]:
    """Return only secondary-review rejections attached to one review run."""
    return _get_rows_for_run("filtered_suggestions", run_id, path)


def get_published_suggestions_for_run(run_id: str, path: Optional[str] = None) -> list[dict]:
    """Return only published suggestions attached to one review run."""
    return _get_rows_for_run("published_suggestions", run_id, path)


def get_suggestion_threads_for_run(run_id: str, path: Optional[str] = None) -> list[dict]:
    """Return skipped or failed publication rows attached to one review run."""
    return _get_rows_for_run("suggestion_threads", run_id, path)


_FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS inline_suggestion_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    project TEXT,
    mr_iid TEXT,
    mr_url TEXT,
    suggestion_id TEXT,
    discussion_id TEXT,
    feedback_user TEXT,
    comment TEXT,
    gitlab_note_id TEXT UNIQUE
);
"""


def migrate_schema(path: Optional[str] = None) -> None:
    """Add applied_at/apply_user columns and inline_suggestion_feedback table if missing.

    Safe to call multiple times (idempotent). Never raises.
    """
    path = path or get_db_path()
    try:
        def migrate(conn):
            conn.execute(_SCHEMA)
            # ALTER TABLE is a no-op if column already exists — wrap each in try/except
            for col, coltype in [
                ("applied_at", "TEXT"),
                ("apply_user", "TEXT"),
                ("mr_url", "TEXT"),
                ("mr_author", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE suggestion_threads ADD COLUMN {col} {coltype}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.execute(_FEEDBACK_SCHEMA)
            _ensure_mr_url_column(conn, "inline_suggestion_feedback")
            conn.executescript(_PUBLISHED_SCHEMA)
            conn.executescript(_FILTERED_SCHEMA)
            _ensure_identity_columns(conn, "suggestion_threads")
            _ensure_identity_columns(conn, "published_suggestions")
            _ensure_identity_columns(conn, "filtered_suggestions")
            _ensure_run_id_column(conn, "suggestion_threads")
            _ensure_run_id_column(conn, "published_suggestions")
            _ensure_run_id_column(conn, "filtered_suggestions")
            for col, coltype in [
                ("mr_url", "TEXT"),
                ("mr_author", "TEXT"),
                ("resolved_at", "TEXT"),
                ("resolve_user", "TEXT"),
                ("resolved_by_stage", "TEXT"),
                ("tier2_duration_ms", "INTEGER"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE published_suggestions ADD COLUMN {col} {coltype}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            _ensure_prompt_provenance_columns(conn)

        _run_write(path, migrate)
    except Exception as e:
        get_logger().error(f"migrate_schema failed: {e}")


def mark_applied(discussion_id: str, apply_user: str,
                 applied_at: Optional[str] = None,
                 path: Optional[str] = None) -> bool:
    """Set applied_at/apply_user on the matching suggestion thread.

    Only writes when applied_at IS NULL so the first apply timestamp is preserved.
    Returns True if a matching row exists, False otherwise. Never raises.
    """
    path = path or get_db_path()
    ts = applied_at or now_cn_iso()
    try:
        def write(conn):
            conn.executescript(_PUBLISHED_SCHEMA)
            cur = conn.execute(
                "SELECT id FROM published_suggestions WHERE gitlab_discussion_id = ? LIMIT 1",
                (discussion_id,),
            )
            if cur.fetchone() is None:
                return False
            conn.execute(
                """UPDATE published_suggestions
                   SET applied_at = ?, apply_user = ?, updated_at = ?
                   WHERE gitlab_discussion_id = ? AND applied_at IS NULL""",
                (ts, apply_user, ts, discussion_id),
            )
            return True

        return _run_write(path, write)
    except Exception as e:
        get_logger().error(f"mark_applied failed: {e}")
        return False


def mark_resolved(discussion_id: str, resolve_user: str,
                  resolved_at: Optional[str] = None,
                  path: Optional[str] = None) -> bool:
    """Set resolved_at/resolve_user on the matching suggestion thread.

    Only writes when resolved_at IS NULL so the first resolve timestamp is
    preserved (mirrors mark_applied's idempotent-write semantics).
    Returns True if a matching row exists, False otherwise. Never raises.
    """
    path = path or get_db_path()
    ts = resolved_at or now_cn_iso()
    try:
        def write(conn):
            conn.executescript(_PUBLISHED_SCHEMA)
            cur = conn.execute(
                "SELECT id FROM published_suggestions WHERE gitlab_discussion_id = ? LIMIT 1",
                (discussion_id,),
            )
            if cur.fetchone() is None:
                return False
            conn.execute(
                """UPDATE published_suggestions
                   SET resolved_at = ?, resolve_user = ?, updated_at = ?
                   WHERE gitlab_discussion_id = ? AND resolved_at IS NULL""",
                (ts, resolve_user, ts, discussion_id),
            )
            return True

        return _run_write(path, write)
    except Exception as e:
        get_logger().error(f"mark_resolved failed: {e}")
        return False


def sync_thread_state(discussion_id: str, applied: bool = False, apply_user: str = "",
                      resolved: bool = False, resolve_user: str = "",
                      path: Optional[str] = None) -> None:
    """Reconcile a single discussion's real applied/resolved state into
    published_suggestions. Thin wrapper over mark_applied/mark_resolved so
    callers (the Discussions-API sync) have one entry point. Never raises."""
    try:
        if applied:
            mark_applied(discussion_id, apply_user=apply_user, path=path)
        if resolved:
            mark_resolved(discussion_id, resolve_user=resolve_user, path=path)
    except Exception as e:
        get_logger().error(f"sync_thread_state failed for {discussion_id}: {e}")


def all_threads_satisfied(project, mr_iid, path: Optional[str] = None) -> bool:
    """True if every published_suggestions row for this MR has applied_at or
    resolved_at set. True when there are no rows at all (nothing to gate on).
    Never raises; returns False on error (conservative: keep gate locked)."""
    try:
        rows = get_published_suggestions(project, mr_iid, path=path)
        if not rows:
            return True
        return all(bool(r.get("applied_at")) or bool(r.get("resolved_at")) for r in rows)
    except Exception as e:
        get_logger().error(f"all_threads_satisfied failed: {e}")
        return False


def has_published_suggestions(project, mr_iid, path: Optional[str] = None) -> bool:
    """True if at least one inline suggestion has been published for this MR.
    Never raises; returns False on error."""
    try:
        return bool(get_published_suggestions(project, mr_iid, path=path))
    except Exception as e:
        get_logger().error(f"has_published_suggestions failed: {e}")
        return False


def get_apply_stats(project, mr_iid, path: Optional[str] = None) -> dict:
    """Return {"published": int, "applied": int} for the given MR. Never raises."""
    path = path or get_db_path()
    try:
        conn = _connect(path)
        try:
            conn.executescript(_PUBLISHED_SCHEMA)
            p, m = _str_or_none(project), _str_or_none(mr_iid)
            published = conn.execute(
                "SELECT COUNT(*) FROM published_suggestions "
                "WHERE project=? AND mr_iid=?",
                (p, m),
            ).fetchone()[0]
            applied = conn.execute(
                "SELECT COUNT(*) FROM published_suggestions "
                "WHERE project=? AND mr_iid=? AND applied_at IS NOT NULL",
                (p, m),
            ).fetchone()[0]
            return {"published": published, "applied": applied}
        finally:
            conn.close()
    except Exception as e:
        get_logger().error(f"get_apply_stats failed: {e}")
        return {"published": 0, "applied": 0}


def save_inline_feedback(record: dict, path: Optional[str] = None) -> bool:
    """Persist a user feedback note on a suggestion discussion. Never raises."""
    path = path or get_db_path()
    now = now_cn_iso()
    try:
        def write(conn):
            conn.execute(_FEEDBACK_SCHEMA)
            _ensure_mr_url_column(conn, "inline_suggestion_feedback")
            conn.execute(
                """INSERT OR IGNORE INTO inline_suggestion_feedback
                   (created_at, project, mr_iid, mr_url, suggestion_id, discussion_id,
                    feedback_user, comment, gitlab_note_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.get("created_at") or now,
                    _str_or_none(record.get("project")),
                    _str_or_none(record.get("mr_iid")),
                    record.get("mr_url"),
                    record.get("suggestion_id"),
                    record.get("discussion_id"),
                    record.get("feedback_user"),
                    record.get("comment"),
                    record.get("gitlab_note_id"),
                ),
            )
            return True

        return _run_write(path, write)
    except Exception as e:
        get_logger().error(f"save_inline_feedback failed: {e}")
        return False


def get_inline_feedbacks(project=None, mr_iid=None,
                         days: Optional[int] = None,
                         path: Optional[str] = None) -> list:
    """Return inline feedback rows for the given project/MR. Never raises."""
    path = path or get_db_path()
    try:
        conn = _connect(path)
        try:
            conn.execute(_FEEDBACK_SCHEMA)
            conn.row_factory = sqlite3.Row
            clauses = []
            params = []
            if project is not None:
                clauses.append("project = ?")
                params.append(_str_or_none(project))
            if mr_iid is not None:
                clauses.append("mr_iid = ?")
                params.append(_str_or_none(mr_iid))
            if days:
                from datetime import timedelta

                from pr_agent.feedback.timez import now_cn
                cutoff = (now_cn() - timedelta(days=days)).isoformat()
                clauses.append("created_at >= ?")
                params.append(cutoff)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            cur = conn.execute(
                f"SELECT * FROM inline_suggestion_feedback {where} ORDER BY id",
                params,
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        get_logger().error(f"get_inline_feedbacks failed: {e}")
        return []
