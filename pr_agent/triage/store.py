"""triage_runs 表存储层（整合进 review_feedback.db）。

复用 pr_agent.feedback.store 的 _connect/_write_lock/get_db_path，
在同一个 SQLite 文件里新增 triage_runs 表，不动 review_feedback 表。
save_triage_run never raises，存储故障不阻断 triage 主流程。
"""
import json
import sqlite3
from typing import Optional

from pr_agent.feedback.store import _connect, _write_lock, get_db_path
from pr_agent.feedback.timez import now_cn_iso
from pr_agent.log import get_logger
from pr_agent.storage.sqlite import run_write_transaction

_SCHEMA = """
CREATE TABLE IF NOT EXISTS triage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    pr_url TEXT,
    project TEXT,
    mr_iid TEXT,
    mr_author TEXT,
    feishu_user_name TEXT,
    source_branch TEXT,
    target_branch TEXT,
    commit_sha TEXT,
    pipeline_id TEXT,
    trigger_type TEXT,
    failed_job_names TEXT,
    failure_categories TEXT,
    success INTEGER,
    finish_reason TEXT,
    iterations INTEGER,
    max_iterations INTEGER,
    pushed_sha TEXT,
    final_pipeline_status TEXT,
    failure_signatures TEXT,
    fix_duration_ms INTEGER,
    model TEXT,
    error TEXT,
    extra_json TEXT,
    final_coverage REAL,
    task_id TEXT,
    repair_outcome TEXT,
    category_results TEXT
);
"""


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for name, column_type in (
        ("final_coverage", "REAL"),
        ("task_id", "TEXT"),
        ("feishu_user_name", "TEXT"),
        ("repair_outcome", "TEXT"),
        ("category_results", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE triage_runs ADD COLUMN {name} {column_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_triage_runs_task_id "
        "ON triage_runs(task_id) WHERE task_id IS NOT NULL"
    )


def _runtime_task_id(record: dict) -> str | None:
    if record.get("task_id"):
        return str(record["task_id"])
    try:
        from pr_agent.distributed.runtime import get_execution_runtime

        runtime = get_execution_runtime()
        return runtime.task_id if runtime is not None else None
    except Exception:
        return None


def init_triage_table(path: Optional[str] = None) -> None:
    """创建 triage_runs 表（幂等，不动 review_feedback 表）。

    对已存在的旧库，幂等补 final_coverage 列（ALTER TABLE ADD COLUMN，已存在则跳过）。
    """
    path = path or get_db_path()
    def initialize(conn):
        conn.execute(_SCHEMA)
        _ensure_columns(conn)

    run_write_transaction(path, initialize, connect=_connect)


def save_triage_run(record: dict, path: Optional[str] = None) -> bool:
    """持久化一条 triage 运行记录。never raises，返回是否成功。"""
    path = path or get_db_path()
    created_at = record.get("created_at") or now_cn_iso()
    task_id = _runtime_task_id(record)

    def _to_json(value):
        if value is None:
            return None
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return None

    try:
        with _write_lock:
            def write(conn):
                conn.execute(_SCHEMA)
                _ensure_columns(conn)
                conn.execute(
                    """
                    INSERT INTO triage_runs (
                        created_at, pr_url, project, mr_iid, mr_author, feishu_user_name,
                        source_branch, target_branch, commit_sha, pipeline_id,
                        trigger_type, failed_job_names, failure_categories,
                        success, finish_reason, iterations, max_iterations,
                        pushed_sha, final_pipeline_status, failure_signatures,
                        fix_duration_ms, model, error, extra_json, final_coverage, task_id,
                        repair_outcome, category_results
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) WHERE task_id IS NOT NULL DO UPDATE SET
                        created_at=excluded.created_at,
                        pr_url=excluded.pr_url,
                        project=excluded.project,
                        mr_iid=excluded.mr_iid,
                        mr_author=excluded.mr_author,
                        feishu_user_name=excluded.feishu_user_name,
                        source_branch=excluded.source_branch,
                        target_branch=excluded.target_branch,
                        commit_sha=excluded.commit_sha,
                        pipeline_id=excluded.pipeline_id,
                        trigger_type=excluded.trigger_type,
                        failed_job_names=excluded.failed_job_names,
                        failure_categories=excluded.failure_categories,
                        success=excluded.success,
                        finish_reason=excluded.finish_reason,
                        iterations=excluded.iterations,
                        max_iterations=excluded.max_iterations,
                        pushed_sha=excluded.pushed_sha,
                        final_pipeline_status=excluded.final_pipeline_status,
                        failure_signatures=excluded.failure_signatures,
                        fix_duration_ms=excluded.fix_duration_ms,
                        model=excluded.model,
                        error=excluded.error,
                        extra_json=excluded.extra_json,
                        final_coverage=excluded.final_coverage,
                        repair_outcome=excluded.repair_outcome,
                        category_results=excluded.category_results
                    """,
                    (
                        created_at,
                        record.get("pr_url"),
                        str(record.get("project")) if record.get("project") is not None else None,
                        str(record.get("mr_iid")) if record.get("mr_iid") is not None else None,
                        record.get("mr_author"),
                        record.get("feishu_user_name"),
                        record.get("source_branch"),
                        record.get("target_branch"),
                        record.get("commit_sha"),
                        str(record.get("pipeline_id")) if record.get("pipeline_id") is not None else None,
                        record.get("trigger_type"),
                        _to_json(record.get("failed_job_names")),
                        _to_json(record.get("failure_categories")),
                        int(record.get("success")) if record.get("success") is not None else None,
                        record.get("finish_reason"),
                        int(record.get("iterations")) if record.get("iterations") is not None else None,
                        int(record.get("max_iterations")) if record.get("max_iterations") is not None else None,
                        record.get("pushed_sha"),
                        record.get("final_pipeline_status"),
                        _to_json(record.get("failure_signatures")),
                        int(record.get("fix_duration_ms")) if record.get("fix_duration_ms") is not None else None,
                        record.get("model"),
                        record.get("error"),
                        _to_json(record.get("extra")),
                        float(record.get("final_coverage")) if record.get("final_coverage") is not None else None,
                        task_id,
                        record.get("repair_outcome"),
                        _to_json(record.get("category_results")),
                    ),
                )
            run_write_transaction(path, write, connect=_connect)
        return True
    except Exception as e:
        get_logger().error(f"Failed to save triage run: {e}")
        return False


def update_triage_run_identity(
    task_id: str,
    feishu_user_name: str,
    path: Optional[str] = None,
) -> bool:
    """Enrich an existing task row with its Feishu display name. Never raises."""
    if not task_id or not feishu_user_name:
        return False
    path = path or get_db_path()
    try:
        with _write_lock:
            def write(conn):
                conn.execute(_SCHEMA)
                _ensure_columns(conn)
                cursor = conn.execute(
                    "UPDATE triage_runs SET feishu_user_name = ? WHERE task_id = ?",
                    (feishu_user_name, task_id),
                )
                return cursor.rowcount > 0

            return bool(run_write_transaction(path, write, connect=_connect))
    except Exception as error:
        get_logger().error(f"Failed to update triage actor identity: {error}")
        return False


def has_triage_run(project: str, mr_iid: str, commit_sha: str, path: Optional[str] = None) -> bool:
    """该 commit 是否已有 triage 记录。never raises。"""
    path = path or get_db_path()
    try:
        conn = _connect(path)
        try:
            conn.execute(_SCHEMA)
            cur = conn.execute(
                "SELECT 1 FROM triage_runs WHERE project = ? AND mr_iid = ? AND commit_sha = ? LIMIT 1",
                (str(project) if project is not None else None,
                 str(mr_iid) if mr_iid is not None else None,
                 commit_sha),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        get_logger().error(f"Failed to query triage run: {e}")
        return False


def has_triage_run_task(task_id: str, path: Optional[str] = None) -> bool:
    """Return whether a detailed Triage row exists for this distributed task."""
    if not task_id:
        return False
    path = path or get_db_path()
    try:
        conn = _connect(path)
        try:
            conn.execute(_SCHEMA)
            _ensure_columns(conn)
            return conn.execute(
                "SELECT 1 FROM triage_runs WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone() is not None
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to query Triage task result: {error}")
        return False


def get_triage_run_task(task_id: str, path: Optional[str] = None) -> dict | None:
    """Load one durable Triage task with decoded JSON fields. Never raises."""
    if not task_id:
        return None
    path = path or get_db_path()
    try:
        conn = _connect(path)
        try:
            conn.execute(_SCHEMA)
            _ensure_columns(conn)
            cursor = conn.execute(
                "SELECT * FROM triage_runs WHERE task_id = ? LIMIT 1",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            result = dict(zip((column[0] for column in cursor.description), row, strict=True))
            for column, target in (
                ("extra_json", "extra"),
                ("failed_job_names", "failed_job_names_list"),
                ("failure_categories", "failure_categories_list"),
                ("failure_signatures", "failure_signatures_list"),
            ):
                try:
                    default_json = "{}" if column == "extra_json" else "[]"
                    result[target] = json.loads(result.get(column) or default_json)
                except (TypeError, ValueError, json.JSONDecodeError):
                    result[target] = {} if column == "extra_json" else []
            return result
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to load Triage task result: {error}")
        return None


def update_triage_run_repair_report(
    task_id: str,
    repair_report: dict,
    path: Optional[str] = None,
) -> bool:
    """Merge a terminal owner report into an existing detailed Triage row. Never raises."""
    if not task_id or not isinstance(repair_report, dict):
        return False
    path = path or get_db_path()
    try:
        with _write_lock:
            def write(conn):
                conn.execute(_SCHEMA)
                _ensure_columns(conn)
                row = conn.execute(
                    "SELECT extra_json FROM triage_runs WHERE task_id = ? LIMIT 1",
                    (task_id,),
                ).fetchone()
                if row is None:
                    return False
                try:
                    extra = json.loads(row[0] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    extra = {}
                if not isinstance(extra, dict):
                    extra = {}
                extra["repair_report"] = repair_report
                cursor = conn.execute(
                    "UPDATE triage_runs SET extra_json = ? WHERE task_id = ?",
                    (json.dumps(extra, ensure_ascii=False), task_id),
                )
                return cursor.rowcount > 0

            return bool(run_write_transaction(path, write, connect=_connect))
    except Exception as error:
        get_logger().error(f"Failed to update Triage repair report: {error}")
        return False
