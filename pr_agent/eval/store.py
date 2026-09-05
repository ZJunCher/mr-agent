"""SQLite storage for the offline evaluation benchmark.

Two logical stores, both plain SQLite files (no extra dependency/service):

- ``review_runs`` : the *baseline* captured at scoring time — the frozen shas,
  model, config snapshot, the original review output, the frozen non-code
  inputs and the human ``score``/``comment`` (denormalised so the eval set is
  self-contained for later validation). It lives in the SAME database file as
  ``review_feedback`` and is joined to it by ``review_id`` (which keeps the
  full rating history). Default path is the feedback DB; configurable via
  ``eval.storage_path``.

- ``replay_results`` : the output of replaying a ``review_run`` under some
  experiment ``tag``. Kept in a SEPARATE file (``eval.benchmark_db_path``) so
  heavy replay writes never contend with the live feedback DB write lock.

Every write is best-effort and idempotent; a storage error returns False and
never raises, so it cannot break the webhook flow.
"""

import json
import os
import sqlite3
import threading
from typing import Optional

from pr_agent.config_loader import get_settings
from pr_agent.feedback.timez import now_cn_iso
from pr_agent.log import get_logger

# Reuse the feedback DB by default so review_runs can be joined to review_feedback
# by review_id. Overridable via [eval] storage_path.
DEFAULT_REVIEW_RUNS_DB = "/app/data/feedback/review_feedback.db"
# Replay results live in a separate file to isolate write locks.
DEFAULT_BENCHMARK_DB = "/app/data/eval/benchmark_eval.db"

_write_lock = threading.Lock()

_REVIEW_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_runs (
    review_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    pr_url TEXT,
    provider TEXT,
    project TEXT,
    mr_iid TEXT,
    base_sha TEXT,
    head_sha TEXT,
    start_sha TEXT,
    model TEXT,
    cfg_json TEXT,
    review_output TEXT,
    score INTEGER,
    comment TEXT,
    note_id TEXT,
    discussion_id TEXT,
    marker_ts TEXT,
    input_json TEXT,
    extra_json TEXT
);
"""

_REPLAY_RESULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    tag TEXT NOT NULL,
    review_id TEXT NOT NULL,
    pr_url TEXT,
    project TEXT,
    mr_iid TEXT,
    base_sha TEXT,
    head_sha TEXT,
    model TEXT,
    cfg_json TEXT,
    review_output TEXT,
    status TEXT,
    error TEXT,
    duration_ms INTEGER,
    extra_json TEXT,
    UNIQUE(tag, review_id)
);
"""


def get_review_runs_db_path() -> str:
    try:
        path = get_settings().get("eval.storage_path", None)
    except Exception:
        path = None
    if not path:
        # fall back to the feedback DB so the two tables sit together
        try:
            path = get_settings().get("pr_feedback.storage_path", DEFAULT_REVIEW_RUNS_DB)
        except Exception:
            path = DEFAULT_REVIEW_RUNS_DB
    return path or DEFAULT_REVIEW_RUNS_DB


def get_benchmark_db_path() -> str:
    try:
        path = get_settings().get("eval.benchmark_db_path", DEFAULT_BENCHMARK_DB)
    except Exception:
        path = DEFAULT_BENCHMARK_DB
    return path or DEFAULT_BENCHMARK_DB


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def _connect(path: str) -> sqlite3.Connection:
    _ensure_parent_dir(path)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=DELETE;")
    except Exception:
        pass
    return conn


def _ensure_review_runs_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the table was first created (idempotent).

    ``CREATE TABLE IF NOT EXISTS`` never adds columns to a pre-existing table, so
    older databases need an explicit ``ALTER TABLE`` for new fields like
    ``input_json``. Best-effort; a failure here must not break a write.
    """
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(review_runs)").fetchall()}
    except Exception:
        return
    if not cols:
        return
    for name, decl in (("input_json", "TEXT"), ("score", "INTEGER"), ("comment", "TEXT")):
        if name not in cols:
            try:
                conn.execute(f"ALTER TABLE review_runs ADD COLUMN {name} {decl}")
            except Exception:
                pass


def _dumps(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return None


def init_review_runs_db(path: Optional[str] = None) -> None:
    path = path or get_review_runs_db_path()
    conn = _connect(path)
    try:
        conn.execute(_REVIEW_RUNS_SCHEMA)
        _ensure_review_runs_columns(conn)
        conn.commit()
    finally:
        conn.close()


def init_benchmark_db(path: Optional[str] = None) -> None:
    path = path or get_benchmark_db_path()
    conn = _connect(path)
    try:
        conn.execute(_REPLAY_RESULTS_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def save_review_run(record: dict, path: Optional[str] = None) -> bool:
    """Persist a captured baseline review run (idempotent by review_id).

    Returns True on success, False otherwise. Never raises.
    """
    review_id = record.get("review_id")
    if not review_id:
        get_logger().warning("save_review_run skipped: missing review_id")
        return False
    path = path or get_review_runs_db_path()
    created_at = record.get("created_at") or now_cn_iso()
    try:
        with _write_lock:
            conn = _connect(path)
            try:
                conn.execute(_REVIEW_RUNS_SCHEMA)
                _ensure_review_runs_columns(conn)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO review_runs (
                        review_id, created_at, pr_url, provider, project, mr_iid,
                        base_sha, head_sha, start_sha, model, cfg_json,
                        review_output, score, comment, note_id, discussion_id,
                        marker_ts, input_json, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(review_id),
                        created_at,
                        record.get("pr_url"),
                        record.get("provider"),
                        _dumps(record.get("project")) if not isinstance(record.get("project"), (str, type(None))) else record.get("project"),
                        str(record.get("mr_iid")) if record.get("mr_iid") is not None else None,
                        record.get("base_sha"),
                        record.get("head_sha"),
                        record.get("start_sha"),
                        record.get("model"),
                        _dumps(record.get("cfg")),
                        record.get("review_output"),
                        int(record.get("score")) if record.get("score") is not None else None,
                        record.get("comment"),
                        str(record.get("note_id")) if record.get("note_id") is not None else None,
                        str(record.get("discussion_id")) if record.get("discussion_id") is not None else None,
                        record.get("marker_ts"),
                        _dumps(record.get("input")),
                        _dumps(record.get("extra")),
                    ),
                )
                if record.get("score") is not None:
                    conn.execute(
                        "UPDATE review_runs SET score = ?, comment = ? WHERE review_id = ?",
                        (int(record.get("score")), record.get("comment"), str(review_id)),
                    )
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception as e:
        get_logger().error(f"Failed to save review run: {e}")
        return False


def save_replay_result(record: dict, path: Optional[str] = None) -> bool:
    """Persist a replay result for an experiment ``tag`` (idempotent by tag+review_id)."""
    review_id = record.get("review_id")
    tag = record.get("tag")
    if not review_id or not tag:
        get_logger().warning("save_replay_result skipped: missing tag/review_id")
        return False
    path = path or get_benchmark_db_path()
    created_at = record.get("created_at") or now_cn_iso()
    try:
        with _write_lock:
            conn = _connect(path)
            try:
                conn.execute(_REPLAY_RESULTS_SCHEMA)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO replay_results (
                        id, created_at, tag, review_id, pr_url, project, mr_iid,
                        base_sha, head_sha, model, cfg_json, review_output,
                        status, error, duration_ms, extra_json
                    ) VALUES (
                        (SELECT id FROM replay_results WHERE tag = ? AND review_id = ?),
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        str(tag), str(review_id),
                        created_at,
                        str(tag),
                        str(review_id),
                        record.get("pr_url"),
                        record.get("project"),
                        str(record.get("mr_iid")) if record.get("mr_iid") is not None else None,
                        record.get("base_sha"),
                        record.get("head_sha"),
                        record.get("model"),
                        _dumps(record.get("cfg")),
                        record.get("review_output"),
                        record.get("status"),
                        record.get("error"),
                        int(record.get("duration_ms")) if record.get("duration_ms") is not None else None,
                        _dumps(record.get("extra")),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception as e:
        get_logger().error(f"Failed to save replay result: {e}")
        return False


def list_review_runs(path: Optional[str] = None, limit: Optional[int] = None,
                     only_replayable: bool = False) -> list:
    """Return review_runs as a list of dicts (newest first)."""
    path = path or get_review_runs_db_path()
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("SELECT 1 FROM review_runs LIMIT 1")
        except Exception:
            return []
        query = "SELECT * FROM review_runs"
        if only_replayable:
            query += " WHERE base_sha IS NOT NULL AND head_sha IS NOT NULL"
        query += " ORDER BY created_at DESC"
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def find_replayable_runs(project: str, mr_iids, path: Optional[str] = None) -> list:
    """Return the newest complete frozen review record for each requested MR."""
    requested = tuple(sorted({str(value) for value in mr_iids if str(value)}))
    path = path or get_review_runs_db_path()
    if not requested or not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            placeholders = ",".join("?" for _ in requested)
            rows = conn.execute(
                f"""
                SELECT * FROM review_runs
                WHERE project = ? AND mr_iid IN ({placeholders})
                  AND base_sha IS NOT NULL AND base_sha != ''
                  AND head_sha IS NOT NULL AND head_sha != ''
                  AND input_json IS NOT NULL AND input_json != ''
                ORDER BY created_at DESC, review_id DESC
                """,
                (str(project), *requested),
            ).fetchall()
        except Exception:
            return []
        selected = {}
        for row in rows:
            record = dict(row)
            mr_iid = str(record.get("mr_iid") or "")
            if mr_iid in selected:
                continue
            try:
                frozen_input = json.loads(record.get("input_json") or "")
            except Exception:
                continue
            if not isinstance(frozen_input, dict) or not frozen_input:
                continue
            record["input"] = frozen_input
            for source, target in (("cfg_json", "cfg"), ("extra_json", "extra")):
                try:
                    record[target] = json.loads(record.get(source) or "null")
                except Exception:
                    record[target] = None
            selected[mr_iid] = record
        return [selected[mr_iid] for mr_iid in requested if mr_iid in selected]
    finally:
        conn.close()


def find_replayable_runs_by_review_ids(review_ids, path: Optional[str] = None) -> list:
    """Return complete frozen runs for exact immutable review identities."""
    requested = tuple(dict.fromkeys(str(value) for value in review_ids if str(value)))
    path = path or get_review_runs_db_path()
    if not requested or not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in requested)
        try:
            rows = conn.execute(
                f"SELECT * FROM review_runs WHERE review_id IN ({placeholders}) "
                "AND base_sha IS NOT NULL AND base_sha != '' "
                "AND head_sha IS NOT NULL AND head_sha != '' "
                "AND input_json IS NOT NULL AND input_json != ''",
                requested,
            ).fetchall()
        except Exception:
            return []
        by_id = {}
        for row in rows:
            record = dict(row)
            try:
                frozen_input = json.loads(record.get("input_json") or "")
            except Exception:
                continue
            if not isinstance(frozen_input, dict) or not frozen_input:
                continue
            record["input"] = frozen_input
            for source, target in (("cfg_json", "cfg"), ("extra_json", "extra")):
                try:
                    record[target] = json.loads(record.get(source) or "null")
                except Exception:
                    record[target] = None
            by_id[str(record.get("review_id") or "")] = record
        return [by_id[review_id] for review_id in requested if review_id in by_id]
    finally:
        conn.close()


def list_replay_results(tag: str, path: Optional[str] = None) -> list:
    """Return replay results for a tag as a list of dicts."""
    path = path or get_benchmark_db_path()
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("SELECT 1 FROM replay_results LIMIT 1")
        except Exception:
            return []
        rows = conn.execute(
            "SELECT * FROM replay_results WHERE tag = ? ORDER BY review_id", (tag,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
