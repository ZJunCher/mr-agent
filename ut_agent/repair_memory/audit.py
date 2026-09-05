"""Task-level audit persistence for repair-memory retrieval decisions."""

from __future__ import annotations

import json
import math
import re
import sqlite3

from pr_agent.feedback.store import _connect, get_db_path
from pr_agent.feedback.timez import now_cn_iso, to_cn
from pr_agent.log import get_logger
from pr_agent.storage.sqlite import run_write_transaction
from ut_agent.repair_memory.models import (
    RepairMemoryCandidateAudit,
    RepairMemoryRetrievalAudit,
    RepairQuery,
    RetrievalAuditStatus,
    RetrievalMode,
)

_CODE_CLEANUP = re.compile(r"[^A-Za-z0-9_.:-]+")
_CANDIDATE_DECISIONS = frozenset({"selected", "passed_not_selected", "rejected"})
_CANDIDATE_INTEGER_SCORE_FIELDS = frozenset({
    "total",
    "semantic_points",
    "exact_fingerprint",
    "failure_family",
    "causal_tokens",
    "language",
    "build_system",
    "project_scope",
    "confidence_freshness",
    "effective_min_score",
})
_CANDIDATE_FLOAT_SCORE_FIELDS = frozenset({"semantic_similarity", "semantic_min_similarity"})
_CANDIDATE_CODE_SCORE_FIELDS = frozenset({
    "scoring_mode",
    "embedding_model",
    "embedding_revision",
    "fallback_reason",
})


def _path(path: str | None) -> str:
    return path or get_db_path()


def _code(value: str, limit: int = 80) -> str:
    return _CODE_CLEANUP.sub("_", str(value or "").strip())[:limit]


def _attempt_id(value: str) -> str:
    return _code(value, 96)


def _candidate_score_payload(score: dict[str, object]) -> dict[str, object]:
    """Keep only fixed, non-text score fields in candidate audit JSON."""
    payload: dict[str, object] = {}
    for key in _CANDIDATE_INTEGER_SCORE_FIELDS:
        if key not in score:
            continue
        try:
            payload[key] = int(score[key])
        except (TypeError, ValueError):
            continue
    for key in _CANDIDATE_FLOAT_SCORE_FIELDS:
        if key not in score:
            continue
        try:
            value = float(score[key])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            payload[key] = round(value, 6)
    for key in _CANDIDATE_CODE_SCORE_FIELDS:
        if key in score:
            payload[key] = _code(str(score[key]), 120)
    return payload


def record_retrieval_candidate_audits(
    candidates: tuple[RepairMemoryCandidateAudit, ...],
    path: str | None = None,
) -> bool:
    """Persist replay-safe candidate decisions without diagnostics or memory text."""
    if not candidates:
        return True
    now = now_cn_iso()

    def write(conn: sqlite3.Connection) -> bool:
        for candidate in candidates:
            attempt_id = _attempt_id(candidate.attempt_id)
            memory_id = str(candidate.memory_id)[:255]
            if not attempt_id or not memory_id:
                continue
            decision = candidate.decision if candidate.decision in _CANDIDATE_DECISIONS else "rejected"
            similarity = candidate.semantic_similarity
            if similarity is not None and not math.isfinite(float(similarity)):
                similarity = None
            conn.execute(
                "INSERT INTO repair_memory_retrieval_candidates "
                "(attempt_id, task_id, memory_id, memory_scope, scoring_mode, semantic_similarity, "
                "total_score, score_json, decision, rejection_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(attempt_id, memory_id) DO UPDATE SET "
                "task_id = excluded.task_id, memory_scope = excluded.memory_scope, "
                "scoring_mode = excluded.scoring_mode, semantic_similarity = excluded.semantic_similarity, "
                "total_score = excluded.total_score, score_json = excluded.score_json, "
                "decision = excluded.decision, rejection_reason = excluded.rejection_reason",
                (
                    attempt_id,
                    str(candidate.task_id)[:128],
                    memory_id,
                    candidate.memory_scope.value,
                    _code(candidate.scoring_mode),
                    None if similarity is None else round(float(similarity), 6),
                    int(candidate.total_score),
                    json.dumps(_candidate_score_payload(candidate.score), ensure_ascii=False, separators=(",", ":")),
                    decision,
                    _code(candidate.rejection_reason),
                    str(candidate.created_at or now)[:40],
                ),
            )
        return True

    try:
        return run_write_transaction(_path(path), write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to record retrieval candidate audits: {type(error).__name__}")
        return False


def initialize_retrieval_audit(
    *,
    task_id: str,
    project: str,
    mr_iid: int,
    source_pipeline_id: int,
    source_sha: str,
    mode: RetrievalMode,
    reason_code: str,
    path: str | None = None,
) -> bool:
    """Create a task audit without changing an existing retrieval decision."""
    now = now_cn_iso()

    def write(conn: sqlite3.Connection) -> bool:
        conn.execute(
            "INSERT INTO repair_memory_retrieval_audits "
            "(task_id, project, mr_iid, source_pipeline_id, source_sha, mode, status, reason_code, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO NOTHING",
            (
                str(task_id)[:128],
                str(project)[:255],
                int(mr_iid or 0),
                int(source_pipeline_id or 0),
                str(source_sha)[:64],
                mode.value,
                RetrievalAuditStatus.NOT_ATTEMPTED.value,
                _code(reason_code),
                now,
                now,
            ),
        )
        return True

    try:
        return run_write_transaction(_path(path), write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to initialize retrieval audit: {type(error).__name__}")
        return False


def mark_retrieval_not_attempted(
    task_id: str,
    *,
    mode: RetrievalMode,
    reason_code: str,
    path: str | None = None,
) -> bool:
    """Update the reason only while the task has not performed a search."""
    now = now_cn_iso()

    def write(conn: sqlite3.Connection) -> bool:
        conn.execute(
            "UPDATE repair_memory_retrieval_audits SET mode = ?, reason_code = ?, updated_at = ? "
            "WHERE task_id = ? AND status = ? AND search_count = 0",
            (
                mode.value,
                _code(reason_code),
                now,
                str(task_id)[:128],
                RetrievalAuditStatus.NOT_ATTEMPTED.value,
            ),
        )
        return True

    try:
        return run_write_transaction(_path(path), write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to mark retrieval not attempted: {type(error).__name__}")
        return False


def _ensure_from_query(
    conn: sqlite3.Connection,
    task_id: str,
    query: RepairQuery,
    mode: RetrievalMode,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO repair_memory_retrieval_audits "
        "(task_id, project, mr_iid, source_pipeline_id, source_sha, mode, status, reason_code, "
        "created_at, updated_at) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id) DO NOTHING",
        (
            str(task_id)[:128],
            str(query.project)[:255],
            int(query.source_pipeline_id or 0),
            str(query.source_sha)[:64],
            mode.value,
            RetrievalAuditStatus.NOT_ATTEMPTED.value,
            "repair_session_not_reached",
            now,
            now,
        ),
    )


def record_retrieval_completion(
    task_id: str,
    query: RepairQuery,
    *,
    attempt_id: str,
    mode: RetrievalMode,
    status: RetrievalAuditStatus,
    reason_code: str,
    candidate_count: int,
    passed_threshold_count: int,
    selected_count: int,
    error_code: str = "",
    increment_search: bool = True,
    path: str | None = None,
) -> bool:
    """Record one terminal retrieval result with replay-safe cumulative counts."""
    now = now_cn_iso()
    bounded_attempt_id = _attempt_id(attempt_id)

    def write(conn: sqlite3.Connection) -> bool:
        _ensure_from_query(conn, task_id, query, mode, now)
        row = conn.execute(
            "SELECT status, reason_code, search_count, candidate_count, passed_threshold_count, "
            "selected_count, last_attempt_id, error_code, attempted_at "
            "FROM repair_memory_retrieval_audits WHERE task_id = ?",
            (str(task_id)[:128],),
        ).fetchone()
        if row is None:
            return False
        current_status = RetrievalAuditStatus(str(row[0]))
        replay = bool(bounded_attempt_id) and str(row[6]) == bounded_attempt_id
        add_counts = bool(increment_search and not replay)
        keep_recall = current_status is RetrievalAuditStatus.RECALLED and (
            status is not RetrievalAuditStatus.RECALLED or replay
        )
        next_status = RetrievalAuditStatus.RECALLED if keep_recall else status
        next_reason = str(row[1]) if keep_recall else _code(reason_code)
        next_error = str(row[7]) if keep_recall else _code(error_code)
        attempted_at = str(row[8]) or (now if increment_search else "")
        conn.execute(
            "UPDATE repair_memory_retrieval_audits SET mode = ?, status = ?, reason_code = ?, "
            "search_count = ?, candidate_count = ?, passed_threshold_count = ?, selected_count = ?, "
            "last_attempt_id = ?, error_code = ?, updated_at = ?, attempted_at = ? WHERE task_id = ?",
            (
                mode.value,
                next_status.value,
                next_reason,
                int(row[2]) + (1 if add_counts else 0),
                int(row[3]) + (max(0, int(candidate_count)) if add_counts else 0),
                int(row[4]) + (max(0, int(passed_threshold_count)) if add_counts else 0),
                int(row[5]) + (max(0, int(selected_count)) if add_counts else 0),
                bounded_attempt_id or str(row[6]),
                next_error,
                now,
                attempted_at,
                str(task_id)[:128],
            ),
        )
        return True

    try:
        return run_write_transaction(_path(path), write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to record retrieval completion: {type(error).__name__}")
        return False


def record_retrieval_error(
    task_id: str,
    *,
    error_code: str,
    attempt_id: str = "",
    increment_search: bool = False,
    path: str | None = None,
) -> bool:
    """Record a bounded caller-side error against an initialized task audit."""
    now = now_cn_iso()
    bounded_attempt_id = _attempt_id(attempt_id)

    def write(conn: sqlite3.Connection) -> bool:
        conn.execute(
            "INSERT INTO repair_memory_retrieval_audits "
            "(task_id, project, mr_iid, source_pipeline_id, source_sha, mode, status, reason_code, "
            "error_code, created_at, updated_at) VALUES (?, '', 0, 0, '', ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO NOTHING",
            (
                str(task_id)[:128],
                RetrievalMode.OFF.value,
                RetrievalAuditStatus.ERROR.value,
                "retrieval_error",
                _code(error_code),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT status, search_count, last_attempt_id, attempted_at "
            "FROM repair_memory_retrieval_audits WHERE task_id = ?",
            (str(task_id)[:128],),
        ).fetchone()
        if row is None:
            return False
        current_status = RetrievalAuditStatus(str(row[0]))
        if current_status is RetrievalAuditStatus.RECALLED:
            return True
        replay = bool(bounded_attempt_id) and str(row[2]) == bounded_attempt_id
        add_search = bool(increment_search and not replay)
        conn.execute(
            "UPDATE repair_memory_retrieval_audits SET status = ?, reason_code = ?, search_count = ?, "
            "last_attempt_id = ?, error_code = ?, updated_at = ?, attempted_at = ? WHERE task_id = ?",
            (
                RetrievalAuditStatus.ERROR.value,
                "retrieval_error",
                int(row[1]) + (1 if add_search else 0),
                bounded_attempt_id or str(row[2]),
                _code(error_code),
                now,
                str(row[3]) or (now if increment_search else ""),
                str(task_id)[:128],
            ),
        )
        return True

    try:
        return run_write_transaction(_path(path), write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to record retrieval error: {type(error).__name__}")
        return False


def record_retrieval_injection(
    task_id: str,
    attempt_id: str,
    injected_count: int,
    path: str | None = None,
) -> bool:
    """Record actual Hermes prompt injection once per retrieval attempt."""
    now = now_cn_iso()
    bounded_attempt_id = _attempt_id(attempt_id)
    if not bounded_attempt_id or injected_count <= 0:
        return False

    def write(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT status, injected_count, injected_attempt_ids_json "
            "FROM repair_memory_retrieval_audits WHERE task_id = ?",
            (str(task_id)[:128],),
        ).fetchone()
        if row is None or str(row[0]) != RetrievalAuditStatus.RECALLED.value:
            return False
        try:
            injected_ids = [str(value) for value in json.loads(str(row[2]) or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            injected_ids = []
        if bounded_attempt_id in injected_ids:
            return True
        injected_ids = [*injected_ids[-63:], bounded_attempt_id]
        conn.execute(
            "UPDATE repair_memory_retrieval_audits SET injected_count = ?, "
            "injected_attempt_ids_json = ?, updated_at = ? WHERE task_id = ?",
            (
                int(row[1]) + int(injected_count),
                json.dumps(injected_ids, separators=(",", ":")),
                now,
                str(task_id)[:128],
            ),
        )
        return True

    try:
        return run_write_transaction(_path(path), write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to record retrieval injection: {type(error).__name__}")
        return False


def load_retrieval_audit(
    task_id: str,
    path: str | None = None,
) -> RepairMemoryRetrievalAudit | None:
    """Load one task-level audit, returning ``None`` on missing data or errors."""
    try:
        conn = _connect(_path(path))
        try:
            row = conn.execute(
                "SELECT task_id, project, mr_iid, source_pipeline_id, source_sha, mode, status, "
                "reason_code, search_count, candidate_count, passed_threshold_count, selected_count, "
                "injected_count, last_attempt_id, error_code, created_at, updated_at, attempted_at "
                "FROM repair_memory_retrieval_audits WHERE task_id = ?",
                (str(task_id)[:128],),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return RepairMemoryRetrievalAudit(
            task_id=str(row[0]),
            project=str(row[1]),
            mr_iid=int(row[2]),
            source_pipeline_id=int(row[3]),
            source_sha=str(row[4]),
            mode=RetrievalMode(str(row[5])),
            status=RetrievalAuditStatus(str(row[6])),
            reason_code=str(row[7]),
            search_count=int(row[8]),
            candidate_count=int(row[9]),
            passed_threshold_count=int(row[10]),
            selected_count=int(row[11]),
            injected_count=int(row[12]),
            last_attempt_id=str(row[13]),
            error_code=str(row[14]),
            created_at=str(row[15]),
            updated_at=str(row[16]),
            attempted_at=str(row[17]),
        )
    except Exception as error:
        get_logger().error(f"Failed to load retrieval audit: {type(error).__name__}")
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _final_repair_outcome(
    repair_outcome: object,
    success: object,
    final_pipeline_status: object,
) -> str:
    if repair_outcome:
        return str(repair_outcome)
    if success is not None:
        return "success" if int(success) == 1 else "failed"
    return str(final_pipeline_status or "")


def _timestamp_value(value: object) -> float:
    parsed = to_cn(str(value or ""))
    return parsed.timestamp() if parsed is not None else 0.0


def _retrieval_union_sql(has_triage: bool) -> str:
    audit_columns = (
        "a.task_id AS task_id, COALESCE(NULLIF(a.project, ''), t.project, '') AS project, "
        "COALESCE(a.mr_iid, t.mr_iid, 0) AS mr_iid, "
        "COALESCE(a.source_pipeline_id, t.pipeline_id, 0) AS source_pipeline_id, "
        "a.mode AS mode, a.status AS status, a.reason_code AS reason_code, "
        "a.search_count AS search_count, a.candidate_count AS candidate_count, "
        "a.passed_threshold_count AS passed_threshold_count, a.selected_count AS selected_count, "
        "a.injected_count AS injected_count, a.last_attempt_id AS last_attempt_id, "
        "a.error_code AS error_code, COALESCE(NULLIF(a.created_at, ''), t.created_at, '') AS created_at, "
        "COALESCE(NULLIF(a.updated_at, ''), t.created_at, '') AS updated_at, "
        "a.attempted_at AS attempted_at, t.success AS triage_success, "
        "t.final_pipeline_status AS final_pipeline_status, t.repair_outcome AS repair_outcome"
    )
    if not has_triage:
        return (
            "SELECT a.task_id AS task_id, a.project AS project, a.mr_iid AS mr_iid, "
            "a.source_pipeline_id AS source_pipeline_id, a.mode AS mode, a.status AS status, "
            "a.reason_code AS reason_code, a.search_count AS search_count, "
            "a.candidate_count AS candidate_count, a.passed_threshold_count AS passed_threshold_count, "
            "a.selected_count AS selected_count, a.injected_count AS injected_count, "
            "a.last_attempt_id AS last_attempt_id, a.error_code AS error_code, "
            "a.created_at AS created_at, a.updated_at AS updated_at, a.attempted_at AS attempted_at, "
            "NULL AS triage_success, NULL AS final_pipeline_status, NULL AS repair_outcome "
            "FROM repair_memory_retrieval_audits a"
        )
    latest_triage = (
        "LEFT JOIN (SELECT task_id, MAX(id) AS id FROM triage_runs "
        "WHERE task_id IS NOT NULL AND task_id != '' GROUP BY task_id) latest ON latest.task_id = a.task_id "
        "LEFT JOIN triage_runs t ON t.id = latest.id"
    )
    audited = f"SELECT {audit_columns} FROM repair_memory_retrieval_audits a {latest_triage}"
    legacy = (
        "SELECT t.task_id AS task_id, COALESCE(t.project, '') AS project, COALESCE(t.mr_iid, 0) AS mr_iid, "
        "COALESCE(t.pipeline_id, 0) AS source_pipeline_id, '' AS mode, 'legacy_unknown' AS status, "
        "'legacy_no_audit' AS reason_code, 0 AS search_count, 0 AS candidate_count, "
        "0 AS passed_threshold_count, 0 AS selected_count, 0 AS injected_count, '' AS last_attempt_id, "
        "'' AS error_code, COALESCE(t.created_at, '') AS created_at, COALESCE(t.created_at, '') AS updated_at, "
        "'' AS attempted_at, t.success AS triage_success, t.final_pipeline_status AS final_pipeline_status, "
        "t.repair_outcome AS repair_outcome FROM triage_runs t "
        "LEFT JOIN repair_memory_retrieval_audits a ON a.task_id = t.task_id "
        "WHERE t.id = (SELECT MAX(t2.id) FROM triage_runs t2 WHERE t2.task_id = t.task_id) "
        "AND t.task_id IS NOT NULL AND t.task_id != '' AND a.task_id IS NULL"
    )
    return f"{audited} UNION ALL {legacy}"


def query_retrieval_audits(
    page: int = 1,
    page_size: int = 15,
    project: str | None = None,
    path: str | None = None,
) -> dict[str, object]:
    """Return a stable page across audited and legacy retrieval tasks."""
    safe_page = max(int(page), 1)
    safe_size = min(max(int(page_size), 1), 100)
    project_filter = str(project or "").strip()
    empty = {"audits": [], "page": safe_page, "page_size": safe_size, "total": 0, "total_pages": 0}
    try:
        conn = _connect(_path(path))
        try:
            union_sql = _retrieval_union_sql(_table_exists(conn, "triage_runs"))
            where = " WHERE merged.project = ?" if project_filter else ""
            params: tuple[object, ...] = (project_filter,) if project_filter else ()
            total = int(conn.execute(f"SELECT COUNT(*) FROM ({union_sql}) merged{where}", params).fetchone()[0])
            rows = conn.execute(
                f"SELECT * FROM ({union_sql}) merged{where} "
                "ORDER BY merged.updated_at DESC, merged.task_id DESC LIMIT ? OFFSET ?",
                (*params, safe_size, (safe_page - 1) * safe_size),
            ).fetchall()
            results = [{
                "task_id": str(row[0]),
                "project": str(row[1] or ""),
                "mr_iid": int(row[2] or 0),
                "source_pipeline_id": int(row[3] or 0),
                "mode": str(row[4] or ""),
                "status": str(row[5] or ""),
                "reason_code": str(row[6] or ""),
                "search_count": int(row[7] or 0),
                "candidate_count": int(row[8] or 0),
                "passed_threshold_count": int(row[9] or 0),
                "selected_count": int(row[10] or 0),
                "injected_count": int(row[11] or 0),
                "last_attempt_id": str(row[12] or ""),
                "error_code": str(row[13] or ""),
                "created_at": str(row[14] or ""),
                "updated_at": str(row[15] or ""),
                "attempted_at": str(row[16] or ""),
                "final_repair_outcome": _final_repair_outcome(row[19], row[17], row[18]),
                "recalled_memories": [],
                "candidate_scores": [],
            } for row in rows]
            task_ids = [str(item["task_id"]) for item in results]
            memories_by_task: dict[str, list[dict[str, str]]] = {task_id: [] for task_id in task_ids}
            candidates_by_task: dict[str, list[dict[str, object]]] = {task_id: [] for task_id in task_ids}
            seen: dict[str, set[str]] = {task_id: set() for task_id in task_ids}
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                memory_rows = conn.execute(
                    "SELECT h.task_id, h.memory_id, h.memory_scope, m.problem_pattern "
                    "FROM repair_memory_hits h LEFT JOIN repair_memories m ON m.memory_id = h.memory_id "
                    f"WHERE h.task_id IN ({placeholders}) ORDER BY h.created_at DESC, h.rank ASC",
                    tuple(task_ids),
                ).fetchall()
                for hit in memory_rows:
                    task_id, memory_id = str(hit[0]), str(hit[1])
                    if memory_id in seen[task_id]:
                        continue
                    seen[task_id].add(memory_id)
                    memories_by_task[task_id].append({
                        "memory_id": memory_id,
                        "scope": str(hit[2]),
                        "problem_pattern": str(hit[3] or "")[:500],
                    })
                task_attempts = [
                    (str(item["task_id"]), str(item["last_attempt_id"]))
                    for item in results
                    if item["last_attempt_id"]
                ]
                if task_attempts and _table_exists(conn, "repair_memory_retrieval_candidates"):
                    attempt_where = " OR ".join("(task_id = ? AND attempt_id = ?)" for _ in task_attempts)
                    attempt_params = tuple(value for pair in task_attempts for value in pair)
                    candidate_rows = conn.execute(
                        "SELECT task_id, attempt_id, memory_id, memory_scope, scoring_mode, "
                        "semantic_similarity, total_score, score_json, decision, rejection_reason, created_at, "
                        "problem_pattern FROM (SELECT c.*, COALESCE(m.problem_pattern, '') AS problem_pattern, "
                        "ROW_NUMBER() OVER (PARTITION BY c.task_id "
                        "ORDER BY c.total_score DESC, c.memory_id ASC) AS candidate_rank "
                        "FROM repair_memory_retrieval_candidates c "
                        "LEFT JOIN repair_memories m ON m.memory_id = c.memory_id WHERE "
                        f"{attempt_where}) WHERE candidate_rank <= 20 "
                        "ORDER BY task_id ASC, total_score DESC, memory_id ASC",
                        attempt_params,
                    ).fetchall()
                    for candidate in candidate_rows:
                        try:
                            raw_score = json.loads(str(candidate[7] or "{}"))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            raw_score = {}
                        score = _candidate_score_payload(raw_score if isinstance(raw_score, dict) else {})
                        candidates_by_task[str(candidate[0])].append({
                            "attempt_id": str(candidate[1]),
                            "memory_id": str(candidate[2]),
                            "problem_pattern": str(candidate[11] or "")[:500],
                            "memory_scope": str(candidate[3]),
                            "scoring_mode": str(candidate[4]),
                            "semantic_similarity": (
                                None if candidate[5] is None else round(float(candidate[5]), 6)
                            ),
                            "total_score": int(candidate[6]),
                            "score": score,
                            "decision": str(candidate[8]),
                            "rejection_reason": str(candidate[9]),
                            "created_at": str(candidate[10]),
                        })
            for item in results:
                item["recalled_memories"] = memories_by_task[str(item["task_id"])]
                item["candidate_scores"] = candidates_by_task[str(item["task_id"])]
            return {
                "audits": results,
                "page": safe_page,
                "page_size": safe_size,
                "total": total,
                "total_pages": (total + safe_size - 1) // safe_size,
            }
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to query retrieval audits: {type(error).__name__}")
        return empty


def list_recent_retrieval_audits(
    limit: int = 20,
    project: str | None = None,
    path: str | None = None,
) -> list[dict[str, object]]:
    """Return recent task audits while preserving the original list interface."""
    result = query_retrieval_audits(page=1, page_size=limit, project=project, path=path)
    return list(result["audits"])
