"""SQLite schema and transaction-safe CRUD for repair memory.

All five tables live in the existing configured ``review_feedback.db`` and use
the shared WAL, busy timeout, and bounded write retry helpers. Memory tables
are separate from ``triage_runs`` because a run is immutable telemetry while a
memory has a lifecycle, evidence set, confidence, usage history, and operator
state.

All public live-path functions catch exceptions, log bounded messages, and
return ``False``, ``None``, an empty tuple, or the updated value object. Schema
initialization may raise in tests/startup so malformed migrations are visible.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pr_agent.feedback.store import _connect, get_db_path
from pr_agent.log import get_logger
from pr_agent.storage.sqlite import run_write_transaction
from ut_agent.repair_memory.models import (
    EmbeddingStatus,
    MemoryEvent,
    MemoryScope,
    MemoryStatus,
    RepairEpisode,
    RepairMemory,
    RepairMemoryEmbedding,
    _json_dumps,
    _json_loads,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repair_memory_episodes (
    episode_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    action_identity TEXT NOT NULL,
    root_cause_group_id TEXT NOT NULL,
    project TEXT NOT NULL,
    mr_iid INTEGER NOT NULL,
    source_pipeline_id INTEGER NOT NULL,
    source_sha TEXT NOT NULL,
    final_pipeline_id INTEGER NOT NULL,
    final_sha TEXT NOT NULL,
    categories_json TEXT NOT NULL,
    job_names_json TEXT NOT NULL,
    language_hints_json TEXT NOT NULL,
    build_system_hints_json TEXT NOT NULL,
    diagnostic_fingerprint TEXT NOT NULL,
    causal_tokens_json TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    solution_summary TEXT NOT NULL,
    measures_json TEXT NOT NULL,
    changed_files_json TEXT NOT NULL,
    report_input_digest TEXT NOT NULL,
    report_source TEXT NOT NULL,
    eligibility_reason TEXT NOT NULL,
    consolidation_status TEXT NOT NULL,
    consolidation_owner TEXT,
    consolidation_lease_until REAL,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS repair_memories (
    memory_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    pattern_key TEXT NOT NULL,
    pattern_version INTEGER NOT NULL,
    language TEXT NOT NULL,
    build_system TEXT NOT NULL,
    failure_family TEXT NOT NULL,
    root_cause_class TEXT NOT NULL,
    repair_action_class TEXT NOT NULL,
    diagnostic_fingerprint TEXT NOT NULL,
    causal_tokens_json TEXT NOT NULL,
    problem_pattern TEXT NOT NULL,
    applicability_json TEXT NOT NULL,
    anti_conditions_json TEXT NOT NULL,
    repair_guidance TEXT NOT NULL,
    validation_guidance_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    support_episode_count INTEGER NOT NULL,
    support_project_count INTEGER NOT NULL,
    settled_attempts INTEGER NOT NULL,
    immediate_successes INTEGER NOT NULL,
    status TEXT NOT NULL,
    content_locale TEXT NOT NULL DEFAULT 'legacy',
    supersedes_id TEXT,
    manual_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_reinforced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repair_memory_evidence (
    memory_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repair_memory_hits (
    attempt_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    root_cause_group_id TEXT NOT NULL,
    current_project TEXT NOT NULL,
    source_pipeline_id INTEGER NOT NULL,
    source_sha TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    memory_scope TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score_json TEXT NOT NULL,
    mode TEXT NOT NULL,
    immediate_pipeline_id INTEGER,
    immediate_pipeline_sha TEXT,
    immediate_pipeline_status TEXT,
    outcome TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE TABLE IF NOT EXISTS repair_memory_retrieval_audits (
    task_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    mr_iid INTEGER NOT NULL,
    source_pipeline_id INTEGER NOT NULL,
    source_sha TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    search_count INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    passed_threshold_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    injected_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_id TEXT NOT NULL DEFAULT '',
    injected_attempt_ids_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    attempted_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS repair_memory_retrieval_candidates (
    attempt_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    memory_scope TEXT NOT NULL,
    scoring_mode TEXT NOT NULL,
    semantic_similarity REAL,
    total_score INTEGER NOT NULL,
    score_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    rejection_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(attempt_id, memory_id)
);

CREATE TABLE IF NOT EXISTS repair_memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repair_memory_embeddings (
    memory_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_blob BLOB,
    source_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    last_error_code TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(memory_id) REFERENCES repair_memories(memory_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_repair_memory_episode_identity
ON repair_memory_episodes(task_id, action_identity);

CREATE UNIQUE INDEX IF NOT EXISTS ux_repair_memory_scope_pattern
ON repair_memories(scope, scope_key, pattern_key, pattern_version)
WHERE status != 'superseded';

CREATE UNIQUE INDEX IF NOT EXISTS ux_repair_memory_evidence
ON repair_memory_evidence(memory_id, episode_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_repair_memory_hit
ON repair_memory_hits(attempt_id, memory_id);

CREATE INDEX IF NOT EXISTS ix_repair_memory_retrieval
ON repair_memories(scope, scope_key, status, pattern_version);

CREATE INDEX IF NOT EXISTS ix_repair_memory_pending
ON repair_memory_episodes(consolidation_status, consolidation_lease_until);

CREATE INDEX IF NOT EXISTS ix_repair_memory_pending_hits
ON repair_memory_hits(task_id, mode, settled_at);

CREATE INDEX IF NOT EXISTS ix_repair_memory_audit_recent
ON repair_memory_retrieval_audits(updated_at, project);

CREATE INDEX IF NOT EXISTS ix_repair_memory_candidate_task
ON repair_memory_retrieval_candidates(task_id, total_score DESC);

CREATE INDEX IF NOT EXISTS ix_repair_memory_embedding_pending
ON repair_memory_embeddings(status, next_retry_at);

CREATE INDEX IF NOT EXISTS ix_repair_memory_embedding_model
ON repair_memory_embeddings(model_name, model_revision);
"""


def init_repair_memory_tables(path: str | None = None) -> None:
    """Create all repair-memory tables and indexes if they do not yet exist.

    Idempotent: safe to call repeatedly. May raise on schema errors so
    malformed migrations are visible in tests and startup.
    """
    db_path = path or get_db_path()

    def initialize(conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)
        memory_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(repair_memories)").fetchall()
        }
        if "content_locale" not in memory_columns:
            conn.execute(
                "ALTER TABLE repair_memories "
                "ADD COLUMN content_locale TEXT NOT NULL DEFAULT 'legacy'"
            )

    run_write_transaction(db_path, initialize, connect=_connect)


def _resolve_path(path: str | None) -> str:
    return path or get_db_path()


def _episode_to_row(episode: RepairEpisode) -> tuple[Any, ...]:
    """Serialize a ``RepairEpisode`` to a SQLite row tuple."""
    return (
        episode.episode_id,
        episode.task_id,
        episode.action_identity,
        episode.root_cause_group_id,
        episode.project,
        episode.mr_iid,
        episode.source_pipeline_id,
        episode.source_sha,
        episode.final_pipeline_id,
        episode.final_sha,
        _json_dumps(list(episode.categories)),
        _json_dumps(list(episode.job_names)),
        _json_dumps(list(episode.language_hints)),
        _json_dumps(list(episode.build_system_hints)),
        episode.diagnostic_fingerprint,
        _json_dumps(list(episode.causal_tokens)),
        episode.root_cause,
        episode.solution_summary,
        _json_dumps(list(episode.measures)),
        _json_dumps(list(episode.changed_files)),
        episode.report_input_digest,
        episode.report_source,
        episode.eligibility_reason,
        episode.consolidation_status,
        None,  # consolidation_owner
        None,  # consolidation_lease_until
        None,  # last_error_code
        episode.created_at,
        None,  # processed_at
    )


_EPISODE_COLUMNS = (
    "episode_id, task_id, action_identity, root_cause_group_id, project, "
    "mr_iid, source_pipeline_id, source_sha, final_pipeline_id, final_sha, "
    "categories_json, job_names_json, language_hints_json, build_system_hints_json, "
    "diagnostic_fingerprint, causal_tokens_json, root_cause, solution_summary, "
    "measures_json, changed_files_json, report_input_digest, report_source, "
    "eligibility_reason, consolidation_status, consolidation_owner, "
    "consolidation_lease_until, last_error_code, created_at, processed_at"
)


def _row_to_episode(row: sqlite3.Row) -> RepairEpisode:
    return RepairEpisode(
        episode_id=row["episode_id"],
        task_id=row["task_id"],
        action_identity=row["action_identity"],
        root_cause_group_id=row["root_cause_group_id"],
        project=row["project"],
        mr_iid=row["mr_iid"],
        source_pipeline_id=row["source_pipeline_id"],
        source_sha=row["source_sha"],
        final_pipeline_id=row["final_pipeline_id"],
        final_sha=row["final_sha"],
        categories=tuple(_json_loads(row["categories_json"]) or ()),
        job_names=tuple(_json_loads(row["job_names_json"]) or ()),
        language_hints=tuple(_json_loads(row["language_hints_json"]) or ()),
        build_system_hints=tuple(_json_loads(row["build_system_hints_json"]) or ()),
        diagnostic_fingerprint=row["diagnostic_fingerprint"],
        causal_tokens=tuple(_json_loads(row["causal_tokens_json"]) or ()),
        root_cause=row["root_cause"],
        solution_summary=row["solution_summary"],
        measures=tuple(_json_loads(row["measures_json"]) or ()),
        changed_files=tuple(_json_loads(row["changed_files_json"]) or ()),
        report_input_digest=row["report_input_digest"],
        report_source=row["report_source"],
        eligibility_reason=row["eligibility_reason"],
        consolidation_status=row["consolidation_status"],
        created_at=row["created_at"],
    )


def _memory_to_row(memory: RepairMemory) -> tuple[Any, ...]:
    """Serialize a ``RepairMemory`` to a SQLite row tuple."""
    return (
        memory.memory_id,
        memory.scope.value,
        memory.scope_key,
        memory.pattern_key,
        memory.pattern_version,
        memory.language,
        memory.build_system,
        memory.failure_family,
        memory.root_cause_class,
        memory.repair_action_class,
        memory.diagnostic_fingerprint,
        _json_dumps(list(memory.causal_tokens)),
        memory.problem_pattern,
        _json_dumps(list(memory.applicability)),
        _json_dumps(list(memory.anti_conditions)),
        memory.repair_guidance,
        _json_dumps(list(memory.validation_guidance)),
        memory.confidence,
        memory.support_episode_count,
        memory.support_project_count,
        memory.settled_attempts,
        memory.immediate_successes,
        memory.status.value,
        memory.content_locale,
        memory.supersedes_id or None,
        memory.manual_reason or None,
        memory.created_at,
        memory.updated_at,
        memory.last_reinforced_at,
    )


_MEMORY_COLUMNS = (
    "memory_id, scope, scope_key, pattern_key, pattern_version, language, "
    "build_system, failure_family, root_cause_class, repair_action_class, "
    "diagnostic_fingerprint, causal_tokens_json, problem_pattern, "
    "applicability_json, anti_conditions_json, repair_guidance, "
    "validation_guidance_json, confidence, support_episode_count, "
    "support_project_count, settled_attempts, immediate_successes, status, "
    "content_locale, supersedes_id, manual_reason, created_at, updated_at, last_reinforced_at"
)


def _row_to_memory(row: sqlite3.Row) -> RepairMemory:
    return RepairMemory(
        memory_id=row["memory_id"],
        scope=MemoryScope(row["scope"]),
        scope_key=row["scope_key"],
        pattern_key=row["pattern_key"],
        pattern_version=row["pattern_version"],
        language=row["language"],
        build_system=row["build_system"],
        failure_family=row["failure_family"],
        root_cause_class=row["root_cause_class"],
        repair_action_class=row["repair_action_class"],
        diagnostic_fingerprint=row["diagnostic_fingerprint"],
        causal_tokens=tuple(_json_loads(row["causal_tokens_json"]) or ()),
        problem_pattern=row["problem_pattern"],
        applicability=tuple(_json_loads(row["applicability_json"]) or ()),
        anti_conditions=tuple(_json_loads(row["anti_conditions_json"]) or ()),
        repair_guidance=row["repair_guidance"],
        validation_guidance=tuple(_json_loads(row["validation_guidance_json"]) or ()),
        confidence=row["confidence"],
        support_episode_count=row["support_episode_count"],
        support_project_count=row["support_project_count"],
        settled_attempts=row["settled_attempts"],
        immediate_successes=row["immediate_successes"],
        status=MemoryStatus(row["status"]),
        content_locale=row["content_locale"],
        supersedes_id=row["supersedes_id"] or "",
        manual_reason=row["manual_reason"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_reinforced_at=row["last_reinforced_at"],
    )


_EMBEDDING_COLUMNS = (
    "memory_id, model_name, model_revision, dimensions, vector_blob, source_hash, "
    "status, last_error_code, attempt_count, next_retry_at, created_at, updated_at"
)


def _row_to_embedding(row: sqlite3.Row, *, prefix: str = "") -> RepairMemoryEmbedding:
    def value(name: str) -> Any:
        return row[f"{prefix}{name}"]

    return RepairMemoryEmbedding(
        memory_id=value("memory_id"),
        model_name=value("model_name"),
        model_revision=value("model_revision"),
        dimensions=value("dimensions"),
        vector_blob=value("vector_blob") or b"",
        source_hash=value("source_hash"),
        status=EmbeddingStatus(value("status")),
        last_error_code=value("last_error_code") or "",
        attempt_count=value("attempt_count"),
        next_retry_at=value("next_retry_at") or "",
        created_at=value("created_at"),
        updated_at=value("updated_at"),
    )


def save_episode(episode: RepairEpisode, path: str | None = None) -> bool:
    """Persist one episode idempotently. Returns ``True`` on success.

    Repeated calls with the same ``(task_id, action_identity)`` are no-ops due
    to the unique index. Never raises; storage failures return ``False``.
    """
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> bool:
        placeholders = ",".join("?" for _ in range(29))
        conn.execute(
            f"INSERT OR IGNORE INTO repair_memory_episodes ({_EPISODE_COLUMNS}) "
            f"VALUES ({placeholders})",
            _episode_to_row(episode),
        )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to save repair memory episode: {type(error).__name__}")
        return False


def load_episode(episode_id: str, path: str | None = None) -> RepairEpisode | None:
    """Return one episode by ID, or ``None`` if missing or unreadable."""
    db_path = _resolve_path(path)
    try:
        conn = _connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT {_EPISODE_COLUMNS} FROM repair_memory_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            return _row_to_episode(row) if row is not None else None
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to load repair memory episode: {type(error).__name__}")
        return None


def save_memory(memory: RepairMemory, path: str | None = None) -> bool:
    """Upsert one memory row. Returns ``True`` on success, never raises."""
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> bool:
        placeholders = ",".join("?" for _ in range(29))
        conn.execute(
            f"INSERT OR REPLACE INTO repair_memories ({_MEMORY_COLUMNS}) "
            f"VALUES ({placeholders})",
            _memory_to_row(memory),
        )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to save repair memory: {type(error).__name__}")
        return False


def save_memory_with_evidence(
    memory: RepairMemory, episode_id: str, path: str | None = None
) -> bool:
    """Upsert a memory and link it to one supporting episode idempotently.

    The evidence link is idempotent due to the unique ``(memory_id, episode_id)``
    index. Never raises.
    """
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> bool:
        placeholders = ",".join("?" for _ in range(29))
        conn.execute(
            f"INSERT OR REPLACE INTO repair_memories ({_MEMORY_COLUMNS}) "
            f"VALUES ({placeholders})",
            _memory_to_row(memory),
        )
        conn.execute(
            "INSERT OR IGNORE INTO repair_memory_evidence "
            "(memory_id, episode_id, relation, created_at) VALUES (?, ?, ?, ?)",
            (memory.memory_id, episode_id, "support", memory.created_at),
        )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to save repair memory with evidence: {type(error).__name__}")
        return False


def load_memory(memory_id: str, path: str | None = None) -> RepairMemory | None:
    """Return one memory by ID, or ``None`` if missing or unreadable."""
    db_path = _resolve_path(path)
    try:
        conn = _connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM repair_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            return _row_to_memory(row) if row is not None else None
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to load repair memory: {type(error).__name__}")
        return None


def list_memories(
    *,
    scope: str = "",
    scope_key: str = "",
    pattern_key: str = "",
    status: str = "",
    path: str | None = None,
) -> tuple[RepairMemory, ...]:
    """Return memories matching the supplied filters, ordered by recency.

    Empty filter values match any. Never raises; returns an empty tuple on error.
    """
    db_path = _resolve_path(path)
    clauses: list[str] = []
    params: list[Any] = []
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    if scope_key:
        clauses.append("scope_key = ?")
        params.append(scope_key)
    if pattern_key:
        clauses.append("pattern_key = ?")
        params.append(pattern_key)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = (
        f"SELECT {_MEMORY_COLUMNS} FROM repair_memories{where} "
        "ORDER BY confidence DESC, last_reinforced_at DESC"
    )
    try:
        conn = _connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
            return tuple(_row_to_memory(row) for row in rows)
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to list repair memories: {type(error).__name__}")
        return ()


def list_retrieval_candidates(
    *,
    scope: str,
    scope_key: str,
    limit: int,
    path: str | None = None,
) -> tuple[tuple[RepairMemory, RepairMemoryEmbedding | None], ...]:
    """Load current active memories and optional vectors for one retrieval pool."""
    if limit <= 0:
        return ()
    from ut_agent.repair_memory.config import load_repair_memory_settings

    db_path = _resolve_path(path)
    timeout = max(1, load_repair_memory_settings().retrieval_timeout_ms) / 1000.0
    memory_columns = ", ".join(f"m.{column.strip()}" for column in _MEMORY_COLUMNS.split(","))
    embedding_columns = (
        "e.memory_id AS embedding_memory_id, e.model_name AS embedding_model_name, "
        "e.model_revision AS embedding_model_revision, e.dimensions AS embedding_dimensions, "
        "e.vector_blob AS embedding_vector_blob, e.source_hash AS embedding_source_hash, "
        "e.status AS embedding_status, e.last_error_code AS embedding_last_error_code, "
        "e.attempt_count AS embedding_attempt_count, e.next_retry_at AS embedding_next_retry_at, "
        "e.created_at AS embedding_created_at, e.updated_at AS embedding_updated_at"
    )
    try:
        conn = sqlite3.connect(db_path, timeout=timeout)
        try:
            conn.execute("PRAGMA query_only=ON;")
            conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)};")
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT {memory_columns}, {embedding_columns} FROM repair_memories m "
                "LEFT JOIN repair_memory_embeddings e ON e.memory_id = m.memory_id "
                "WHERE m.scope = ? AND m.scope_key = ? AND m.status = 'active' "
                "AND m.content_locale = 'zh-CN' "
                "AND NOT EXISTS ("
                "SELECT 1 FROM repair_memories newer WHERE newer.scope = m.scope "
                "AND newer.scope_key = m.scope_key AND newer.pattern_key = m.pattern_key "
                "AND newer.status = 'active' AND newer.pattern_version > m.pattern_version"
                ") ORDER BY m.confidence DESC, m.last_reinforced_at DESC, m.memory_id ASC LIMIT ?",
                (scope, scope_key, limit),
            ).fetchall()
            return tuple(
                (
                    _row_to_memory(row),
                    _row_to_embedding(row, prefix="embedding_")
                    if row["embedding_memory_id"] is not None
                    else None,
                )
                for row in rows
            )
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to list retrieval candidates: {type(error).__name__}")
        return ()


def load_memory_embedding(
    memory_id: str, path: str | None = None
) -> RepairMemoryEmbedding | None:
    """Return one persisted embedding, or ``None`` when absent or unreadable."""
    db_path = _resolve_path(path)
    try:
        conn = _connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT {_EMBEDDING_COLUMNS} FROM repair_memory_embeddings WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            return _row_to_embedding(row) if row is not None else None
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to load repair memory embedding: {type(error).__name__}")
        return None


def upsert_memory_embeddings(
    embeddings: tuple[RepairMemoryEmbedding, ...], path: str | None = None
) -> bool:
    """Atomically upsert a bounded embedding batch. Never logs vector content."""
    if not embeddings:
        return True
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> bool:
        conn.executemany(
            f"INSERT INTO repair_memory_embeddings ({_EMBEDDING_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(memory_id) DO UPDATE SET "
            "model_name = excluded.model_name, model_revision = excluded.model_revision, "
            "dimensions = excluded.dimensions, vector_blob = excluded.vector_blob, "
            "source_hash = excluded.source_hash, status = excluded.status, "
            "last_error_code = excluded.last_error_code, attempt_count = excluded.attempt_count, "
            "next_retry_at = excluded.next_retry_at, updated_at = excluded.updated_at",
            tuple(
                (
                    item.memory_id,
                    item.model_name,
                    item.model_revision,
                    item.dimensions,
                    item.vector_blob or None,
                    item.source_hash,
                    item.status.value,
                    item.last_error_code or None,
                    item.attempt_count,
                    item.next_retry_at or None,
                    item.created_at,
                    item.updated_at,
                )
                for item in embeddings
            ),
        )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to upsert repair memory embeddings: {type(error).__name__}")
        return False


def list_legacy_memories(
    *, limit: int, path: str | None = None
) -> tuple[RepairMemory, ...]:
    """Return active non-Chinese memories eligible for one migration attempt."""
    if limit <= 0:
        return ()
    db_path = _resolve_path(path)
    try:
        conn = _connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM repair_memories "
                "WHERE status = 'active' AND content_locale != 'zh-CN' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(_row_to_memory(row) for row in rows)
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to list legacy repair memories: {type(error).__name__}")
        return ()


def list_memory_supporting_episodes(
    memory_id: str, path: str | None = None
) -> tuple[RepairEpisode, ...]:
    """Return complete immutable episodes linked to one memory, oldest first."""
    db_path = _resolve_path(path)
    episode_columns = ", ".join(f"ep.{column.strip()}" for column in _EPISODE_COLUMNS.split(","))
    try:
        conn = _connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT {episode_columns} FROM repair_memory_episodes ep "
                "JOIN repair_memory_evidence ev ON ev.episode_id = ep.episode_id "
                "WHERE ev.memory_id = ? ORDER BY ep.created_at ASC",
                (memory_id,),
            ).fetchall()
            return tuple(_row_to_episode(row) for row in rows)
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to list memory supporting episodes: {type(error).__name__}")
        return ()


def mark_legacy_memory_needs_review(memory_id: str, path: str | None = None) -> bool:
    """Move an evidence-free legacy memory to review with an audit event."""
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM repair_memories "
            "WHERE memory_id = ? AND status = 'active' AND content_locale != 'zh-CN'",
            (memory_id,),
        ).fetchone()
        if row is None:
            return False
        now = _now_iso()
        conn.execute(
            "UPDATE repair_memories SET status = 'needs_review', updated_at = ?, "
            "manual_reason = ? WHERE memory_id = ?",
            (now, "missing_supporting_episode", memory_id),
        )
        conn.execute(
            "INSERT INTO repair_memory_events "
            "(memory_id, event_type, reason, metadata_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                memory_id,
                "legacy_memory_needs_review",
                "missing_supporting_episode",
                _json_dumps({}),
                now,
            ),
        )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to mark legacy memory for review: {type(error).__name__}")
        return False


def record_legacy_migration_failure(
    memory_id: str,
    error_code: str,
    *,
    owner: str,
    path: str | None = None,
) -> bool:
    """Append one bounded migration failure event while preserving old state."""
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT COUNT(*) FROM repair_memory_events "
            "WHERE memory_id = ? AND event_type = 'legacy_memory_migration_failed'",
            (memory_id,),
        ).fetchone()
        attempt = int(row[0] if row is not None else 0) + 1
        conn.execute(
            "INSERT INTO repair_memory_events "
            "(memory_id, event_type, reason, metadata_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                memory_id,
                "legacy_memory_migration_failed",
                str(error_code or "unknown")[:120],
                _json_dumps({"attempt": attempt, "owner": owner[:120]}),
                _now_iso(),
            ),
        )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to record legacy migration failure: {type(error).__name__}")
        return False


def commit_legacy_memory_migration(
    old_memory_id: str,
    replacement: RepairMemory,
    pending_embedding: RepairMemoryEmbedding,
    *,
    owner: str,
    path: str | None = None,
) -> bool:
    """Atomically supersede legacy text, copy evidence, and queue embedding."""
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM repair_memories "
            "WHERE memory_id = ? AND status = 'active' AND content_locale != 'zh-CN'",
            (old_memory_id,),
        ).fetchone()
        if row is None:
            return False
        now = replacement.updated_at or _now_iso()
        conn.execute(
            "UPDATE repair_memories SET status = 'superseded', updated_at = ? WHERE memory_id = ?",
            (now, old_memory_id),
        )
        placeholders = ",".join("?" for _ in range(29))
        conn.execute(
            f"INSERT INTO repair_memories ({_MEMORY_COLUMNS}) VALUES ({placeholders})",
            _memory_to_row(replacement),
        )
        conn.execute(
            "INSERT OR IGNORE INTO repair_memory_evidence (memory_id, episode_id, relation, created_at) "
            "SELECT ?, episode_id, relation, ? FROM repair_memory_evidence WHERE memory_id = ?",
            (replacement.memory_id, now, old_memory_id),
        )
        conn.execute(
            f"INSERT INTO repair_memory_embeddings ({_EMBEDDING_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pending_embedding.memory_id,
                pending_embedding.model_name,
                pending_embedding.model_revision,
                pending_embedding.dimensions,
                None,
                pending_embedding.source_hash,
                pending_embedding.status.value,
                None,
                pending_embedding.attempt_count,
                None,
                pending_embedding.created_at,
                pending_embedding.updated_at,
            ),
        )
        conn.execute(
            "INSERT INTO repair_memory_events "
            "(memory_id, event_type, reason, metadata_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                old_memory_id,
                "legacy_memory_migrated",
                "regenerated_from_supporting_episode",
                _json_dumps(
                    {
                        "replacement_memory_id": replacement.memory_id,
                        "owner": owner[:120],
                    }
                ),
                now,
            ),
        )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to commit legacy memory migration: {type(error).__name__}")
        return False


def update_memory_status(
    memory_id: str,
    status: MemoryStatus,
    reason: str,
    *,
    source: str = "system",
    expected_statuses: frozenset[MemoryStatus] | None = None,
    path: str | None = None,
) -> RepairMemory | None:
    """Atomically transition a memory and return its current value.

    A repeated transition to the current status is idempotent and does not add
    another event. ``expected_statuses`` provides optimistic state validation;
    a missing memory, invalid reason, conflicting state, or storage failure
    returns ``None``. Never raises.
    """
    normalized_reason = str(reason or "").strip()
    if not 1 <= len(normalized_reason) <= 500:
        return None
    normalized_source = str(source or "system").strip()[:80] or "system"
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> RepairMemory | None:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM repair_memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        current = _row_to_memory(row)
        if current.status is status:
            return current
        if expected_statuses is not None and current.status not in expected_statuses:
            return None
        changed_at = _now_iso()
        conn.execute(
            "UPDATE repair_memories SET status = ?, updated_at = ? "
            "WHERE memory_id = ?",
            (status.value, changed_at, memory_id),
        )
        conn.execute(
            "INSERT INTO repair_memory_events "
            "(memory_id, event_type, reason, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                memory_id,
                status.value,
                normalized_reason,
                _json_dumps({
                    "source": normalized_source,
                    "previous_status": current.status.value,
                    "new_status": status.value,
                    "changed_at": changed_at,
                }),
                changed_at,
            ),
        )
        updated = conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM repair_memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        return _row_to_memory(updated) if updated is not None else None

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to update memory status: {type(error).__name__}")
        return None


def list_memory_events(memory_id: str, path: str | None = None) -> tuple[MemoryEvent, ...]:
    """Return all audit events for a memory, oldest first. Never raises."""
    db_path = _resolve_path(path)
    try:
        conn = _connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, memory_id, event_type, reason, metadata_json, created_at "
                "FROM repair_memory_events WHERE memory_id = ? ORDER BY id ASC",
                (memory_id,),
            ).fetchall()
            return tuple(
                MemoryEvent(
                    id=row["id"],
                    memory_id=row["memory_id"],
                    event_type=row["event_type"],
                    reason=row["reason"],
                    metadata=_json_loads(row["metadata_json"]) or {},
                    created_at=row["created_at"],
                )
                for row in rows
            )
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to list memory events: {type(error).__name__}")
        return ()


def list_attempt_hits(attempt_id: str, path: str | None = None) -> tuple[dict[str, Any], ...]:
    """Return all hit rows for one attempt as plain dicts. Never raises.

    Used for audit and tests. Each dict mirrors the persisted hit row.
    """
    db_path = _resolve_path(path)
    try:
        conn = _connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT attempt_id, task_id, root_cause_group_id, current_project, "
                "source_pipeline_id, source_sha, memory_id, memory_scope, rank, "
                "score_json, mode, immediate_pipeline_id, immediate_pipeline_sha, "
                "immediate_pipeline_status, outcome, created_at, settled_at "
                "FROM repair_memory_hits WHERE attempt_id = ? ORDER BY rank ASC",
                (attempt_id,),
            ).fetchall()
            return tuple(
                {
                    "attempt_id": row["attempt_id"],
                    "task_id": row["task_id"],
                    "root_cause_group_id": row["root_cause_group_id"],
                    "current_project": row["current_project"],
                    "source_pipeline_id": row["source_pipeline_id"],
                    "source_sha": row["source_sha"],
                    "memory_id": row["memory_id"],
                    "memory_scope": row["memory_scope"],
                    "rank": row["rank"],
                    "score": _json_loads(row["score_json"]) or {},
                    "mode": row["mode"],
                    "immediate_pipeline_id": row["immediate_pipeline_id"],
                    "immediate_pipeline_sha": row["immediate_pipeline_sha"],
                    "immediate_pipeline_status": row["immediate_pipeline_status"],
                    "outcome": row["outcome"],
                    "created_at": row["created_at"],
                    "settled_at": row["settled_at"],
                }
                for row in rows
            )
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to list attempt hits: {type(error).__name__}")
        return ()


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Imported lazily to avoid a hard dependency on the feedback timez helper at
    module load time.
    """
    from pr_agent.feedback.timez import now_cn_iso

    return now_cn_iso()


def _now_epoch() -> float:
    """Return the current Unix timestamp for lease comparisons."""
    import time

    return time.time()


@dataclass(frozen=True)
class ClaimedEpisode:
    """A pending episode claimed for consolidation."""

    episode_id: str
    task_id: str
    action_identity: str
    root_cause_group_id: str
    project: str
    mr_iid: int
    source_pipeline_id: int
    source_sha: str
    final_pipeline_id: int
    final_sha: str
    categories: tuple[str, ...]
    job_names: tuple[str, ...]
    language_hints: tuple[str, ...]
    build_system_hints: tuple[str, ...]
    diagnostic_fingerprint: str
    causal_tokens: tuple[str, ...]
    root_cause: str
    solution_summary: str
    measures: tuple[str, ...]
    changed_files: tuple[str, ...]
    report_input_digest: str
    report_source: str


def _row_to_claimed(row: sqlite3.Row) -> ClaimedEpisode:
    return ClaimedEpisode(
        episode_id=row["episode_id"],
        task_id=row["task_id"],
        action_identity=row["action_identity"],
        root_cause_group_id=row["root_cause_group_id"],
        project=row["project"],
        mr_iid=row["mr_iid"],
        source_pipeline_id=row["source_pipeline_id"],
        source_sha=row["source_sha"],
        final_pipeline_id=row["final_pipeline_id"],
        final_sha=row["final_sha"],
        categories=tuple(_json_loads(row["categories_json"]) or ()),
        job_names=tuple(_json_loads(row["job_names_json"]) or ()),
        language_hints=tuple(_json_loads(row["language_hints_json"]) or ()),
        build_system_hints=tuple(_json_loads(row["build_system_hints_json"]) or ()),
        diagnostic_fingerprint=row["diagnostic_fingerprint"],
        causal_tokens=tuple(_json_loads(row["causal_tokens_json"]) or ()),
        root_cause=row["root_cause"],
        solution_summary=row["solution_summary"],
        measures=tuple(_json_loads(row["measures_json"]) or ()),
        changed_files=tuple(_json_loads(row["changed_files_json"]) or ()),
        report_input_digest=row["report_input_digest"],
        report_source=row["report_source"],
    )


def claim_pending_episodes(
    owner: str,
    *,
    limit: int = 50,
    lease_seconds: int = 300,
    now: float | None = None,
    path: str | None = None,
) -> tuple[ClaimedEpisode, ...]:
    """Atomically claim pending or expired-lease episodes for consolidation.

    Sets ``consolidation_status='claimed'``, ``consolidation_owner=owner``, and
    ``consolidation_lease_until=now+lease_seconds``. Returns the claimed rows.
    Never raises; returns an empty tuple on error.
    """
    db_path = _resolve_path(path)
    now_ts = now if now is not None else _now_epoch()
    lease_until = now_ts + max(1, lease_seconds)

    def write(conn: sqlite3.Connection) -> tuple[ClaimedEpisode, ...]:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT episode_id FROM repair_memory_episodes "
            "WHERE consolidation_status = 'pending' "
            "OR (consolidation_status = 'claimed' AND consolidation_lease_until < ?) "
            "ORDER BY created_at ASC LIMIT ?",
            (now_ts, max(1, limit)),
        ).fetchall()
        claimed: list[ClaimedEpisode] = []
        for row in rows:
            episode_id = row["episode_id"]
            conn.execute(
                "UPDATE repair_memory_episodes "
                "SET consolidation_status = 'claimed', consolidation_owner = ?, "
                "consolidation_lease_until = ? WHERE episode_id = ?",
                (owner, lease_until, episode_id),
            )
            full = conn.execute(
                f"SELECT {_EPISODE_COLUMNS} FROM repair_memory_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if full is not None:
                claimed.append(_row_to_claimed(full))
        return tuple(claimed)

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to claim pending episodes: {type(error).__name__}")
        return ()


def mark_episode_consolidated(
    episode_id: str, *, path: str | None = None
) -> bool:
    """Mark an episode as successfully consolidated. Never raises."""
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> bool:
        conn.execute(
            "UPDATE repair_memory_episodes "
            "SET consolidation_status = 'complete', processed_at = ? "
            "WHERE episode_id = ?",
            (_now_iso(), episode_id),
        )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to mark episode consolidated: {type(error).__name__}")
        return False


def mark_episode_failed(
    episode_id: str, error_code: str, *, path: str | None = None
) -> bool:
    """Mark an episode as failed consolidation (retryable). Never raises."""
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> bool:
        conn.execute(
            "UPDATE repair_memory_episodes "
            "SET consolidation_status = 'pending', last_error_code = ? "
            "WHERE episode_id = ?",
            (error_code[:120], episode_id),
        )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to mark episode failed: {type(error).__name__}")
        return False


def mark_episode_invalid(
    episode_id: str, error_code: str, *, path: str | None = None
) -> bool:
    """Mark an episode as permanently invalid (requires operator inspection)."""
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> bool:
        conn.execute(
            "UPDATE repair_memory_episodes "
            "SET consolidation_status = 'invalid', last_error_code = ? "
            "WHERE episode_id = ?",
            (error_code[:120], episode_id),
        )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to mark episode invalid: {type(error).__name__}")
        return False


def count_distinct_supporting_projects(
    pattern_key: str, *, path: str | None = None
) -> int:
    """Count distinct projects with active project memories for a pattern.

    Used by global promotion and support revalidation. Never raises.
    """
    db_path = _resolve_path(path)
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(DISTINCT m.scope_key) "
                "FROM repair_memories m "
                "JOIN repair_memory_evidence e ON e.memory_id = m.memory_id "
                "JOIN repair_memory_episodes ep ON ep.episode_id = e.episode_id "
                "WHERE m.scope = 'project' AND m.status = 'active' "
                "AND m.pattern_key = ?",
                (pattern_key,),
            ).fetchone()
            return int(row[0]) if row is not None else 0
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to count supporting projects: {type(error).__name__}")
        return 0


def revalidate_global_support(pattern_key: str, path: str | None = None) -> bool:
    """Recheck active global memories for a pattern against project support.

    If active project support drops below the configured minimum, move the
    global memory to ``needs_review`` and append an audit event. Returns True
    if any global memory was moved. Never raises.
    """
    from ut_agent.repair_memory.config import load_repair_memory_settings

    db_path = _resolve_path(path)
    try:
        settings = load_repair_memory_settings()
        min_projects = settings.global_min_projects
    except Exception:
        min_projects = 2
    support = count_distinct_supporting_projects(pattern_key, path=db_path)
    if support >= min_projects:
        return False

    def write(conn: sqlite3.Connection) -> bool:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT memory_id FROM repair_memories "
            "WHERE scope = 'global' AND pattern_key = ? AND status = 'active'",
            (pattern_key,),
        ).fetchall()
        if not rows:
            return False
        now = _now_iso()
        for row in rows:
            memory_id = row["memory_id"]
            conn.execute(
                "UPDATE repair_memories SET status = 'needs_review', updated_at = ? "
                "WHERE memory_id = ?",
                (now, memory_id),
            )
            conn.execute(
                "INSERT INTO repair_memory_events "
                "(memory_id, event_type, reason, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    memory_id,
                    "needs_review",
                    "insufficient active project support",
                    _json_dumps({"support": support, "min": min_projects}),
                    now,
                ),
            )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to revalidate global support: {type(error).__name__}")
        return False


@dataclass(frozen=True)
class PruneSummary:
    """Counts of rows deleted by retention cleanup."""

    deleted_episodes: int
    deleted_hits: int


def _parse_retention_timestamp(value: Any) -> datetime | None:
    """Return one comparable UTC timestamp, preserving malformed values."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def prune_expired_memory_data(
    now: str,
    *,
    episode_retention_days: int = 365,
    hit_retention_days: int = 365,
    path: str | None = None,
) -> PruneSummary:
    """Delete expired episodes and settled hits past their retention window.

    Episodes are deleted only when no ``repair_memory_evidence`` row references
    them, so supporting evidence, memory rows, and audit events remain auditable.
    Never raises; returns zero counts on error.
    """
    db_path = _resolve_path(path)

    def write(conn: sqlite3.Connection) -> PruneSummary:
        now_dt = _parse_retention_timestamp(now) or datetime.now(timezone.utc)
        episode_cutoff = now_dt - timedelta(days=episode_retention_days)
        hit_cutoff = now_dt - timedelta(days=hit_retention_days)

        conn.row_factory = sqlite3.Row
        unreferenced_rows = conn.execute(
            "SELECT ep.episode_id, ep.created_at FROM repair_memory_episodes ep "
            "LEFT JOIN repair_memory_evidence ev ON ev.episode_id = ep.episode_id "
            "WHERE ev.episode_id IS NULL"
        ).fetchall()
        expired_episode_ids = tuple(
            str(row["episode_id"])
            for row in unreferenced_rows
            if (created_at := _parse_retention_timestamp(row["created_at"])) is not None
            and created_at < episode_cutoff
        )
        if expired_episode_ids:
            conn.executemany(
                "DELETE FROM repair_memory_episodes WHERE episode_id = ?",
                ((episode_id,) for episode_id in expired_episode_ids),
            )
        deleted_episodes = len(expired_episode_ids)

        settled_hit_rows = conn.execute(
            "SELECT attempt_id, memory_id, settled_at FROM repair_memory_hits "
            "WHERE settled_at IS NOT NULL"
        ).fetchall()
        expired_hit_keys = tuple(
            (str(row["attempt_id"]), str(row["memory_id"]))
            for row in settled_hit_rows
            if (settled_at := _parse_retention_timestamp(row["settled_at"])) is not None
            and settled_at < hit_cutoff
        )
        if expired_hit_keys:
            conn.executemany(
                "DELETE FROM repair_memory_hits WHERE attempt_id = ? AND memory_id = ?",
                expired_hit_keys,
            )
        deleted_hits = len(expired_hit_keys)
        return PruneSummary(deleted_episodes=deleted_episodes, deleted_hits=deleted_hits)

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to prune expired memory data: {type(error).__name__}")
        return PruneSummary(deleted_episodes=0, deleted_hits=0)
