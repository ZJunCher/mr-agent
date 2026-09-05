"""Hybrid semantic and deterministic retrieval for repair memories.

The live path always reads the current project pool and the global pool. When
compatible BGE-M3 vectors are available it embeds the query once, computes
cosine similarity in Python, and applies the confirmed 100-point score. Missing
vectors are ranked with the existing deterministic score. Any query embedding
failure degrades the whole attempt to that deterministic path.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pr_agent.feedback.store import _connect, get_db_path
from pr_agent.log import get_logger
from pr_agent.storage.sqlite import run_write_transaction
from ut_agent.repair_memory.audit import record_retrieval_candidate_audits, record_retrieval_completion
from ut_agent.repair_memory.config import RepairMemorySettings, load_repair_memory_settings
from ut_agent.repair_memory.embedding import (
    EmbeddingBatch,
    EmbeddingClient,
    EmbeddingServiceError,
    HttpEmbeddingClient,
    blob_to_vector,
    build_memory_embedding_text,
    build_query_embedding_text,
    cosine_similarity,
    embedding_source_hash,
)
from ut_agent.repair_memory.models import (
    EmbeddingStatus,
    MemoryScope,
    RepairMemory,
    RepairMemoryCandidateAudit,
    RepairMemoryEmbedding,
    RepairMemoryHint,
    RepairQuery,
    RetrievalAuditStatus,
    RetrievalMode,
    RetrievalResult,
    _json_dumps,
)
from ut_agent.repair_memory.store import list_retrieval_candidates

_RETRIEVAL_SCHEMA_VERSION = 2

_FAILURE_FAMILY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"no member named|has no member|missing member", re.IGNORECASE), "missing_member"),
    (re.compile(r"fatal error|no such file|not found.*\.h", re.IGNORECASE), "missing_header"),
    (re.compile(r"undefined reference|undefined symbol", re.IGNORECASE), "undefined_symbol"),
    (re.compile(r"cannot convert|type mismatch|incompatible", re.IGNORECASE), "type_mismatch"),
    (re.compile(r"assertion failed|assert failed|expected true", re.IGNORECASE), "test_assertion"),
    (re.compile(r"cmake error|target not found|build failed", re.IGNORECASE), "build_config"),
)


def classify_failure_family(causal_lines: tuple[str, ...]) -> str:
    """Classify the current failure family from sanitized causal lines."""
    text = " ".join(causal_lines)
    for pattern, family in _FAILURE_FAMILY_PATTERNS:
        if pattern.search(text):
            return family
    return "other"


@dataclass(frozen=True)
class ScoreBreakdown:
    """Existing deterministic fallback score components."""

    total: int
    breakdown: dict[str, int]


@dataclass(frozen=True)
class HybridScoreBreakdown:
    """Confirmed 100-point hybrid score components."""

    total: int
    semantic_similarity: float
    semantic_points: int
    exact_fingerprint_points: int
    failure_family_points: int
    causal_token_points: int
    language_points: int
    build_system_points: int
    project_points: int
    quality_points: int
    scoring_mode: str = "hybrid"


@dataclass(frozen=True)
class _ScoredCandidate:
    memory: RepairMemory
    total: int
    exact_fingerprint_points: int
    semantic_points: int
    semantic_similarity: float | None
    current_project_points: int
    audit: dict[str, Any]
    match_reasons: tuple[str, ...]
    rejection_reason: str = ""


def _jaccard(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    if not a or not b:
        return 0.0
    set_a = set(a)
    set_b = set(b)
    union = len(set_a | set_b)
    return len(set_a & set_b) / union if union else 0.0


def _parse_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_stale(last_reinforced_at: str, now: str, stale_days: int) -> bool:
    now_dt = _parse_datetime(now)
    reinforced = _parse_datetime(last_reinforced_at)
    if now_dt is None or reinforced is None:
        return False
    return now_dt - reinforced > timedelta(days=stale_days)


def score_memory(
    query: RepairQuery,
    memory: RepairMemory,
    *,
    now: str = "",
) -> ScoreBreakdown:
    """Return the existing deterministic 0-100 fallback score."""
    settings = load_repair_memory_settings()
    exact_fingerprint = (
        40
        if query.diagnostic_fingerprint
        and query.diagnostic_fingerprint == memory.diagnostic_fingerprint
        else 0
    )
    failure_family = 20 if query.failure_family == memory.failure_family else 0
    language = 10 if query.language == memory.language else 0
    build_system = 10 if query.build_system == memory.build_system else 0
    causal = round(10 * _jaccard(query.causal_tokens, memory.causal_tokens))
    confidence_points = round(memory.confidence * 5)
    freshness_bonus = 5 if not _is_stale(memory.last_reinforced_at, now, settings.stale_days) else 0
    confidence_freshness = min(10, confidence_points + freshness_bonus)
    total = exact_fingerprint + failure_family + language + build_system + causal + confidence_freshness
    return ScoreBreakdown(
        total=total,
        breakdown={
            "exact_fingerprint": exact_fingerprint,
            "failure_family": failure_family,
            "language": language,
            "build_system": build_system,
            "causal_tokens": causal,
            "confidence_freshness": confidence_freshness,
        },
    )


def score_memory_hybrid(
    query: RepairQuery,
    memory: RepairMemory,
    *,
    semantic_similarity: float,
    now: datetime,
) -> HybridScoreBreakdown:
    """Return the confirmed explainable 100-point hybrid score."""
    settings = load_repair_memory_settings()
    similarity = max(0.0, min(1.0, float(semantic_similarity)))
    semantic_points = round(similarity * 40)
    exact_fingerprint_points = (
        25
        if query.diagnostic_fingerprint
        and query.diagnostic_fingerprint == memory.diagnostic_fingerprint
        else 0
    )
    failure_family_points = 10 if query.failure_family == memory.failure_family else 0
    causal_token_points = round(5 * _jaccard(query.causal_tokens, memory.causal_tokens))
    language_points = 5 if query.language == memory.language else 0
    build_system_points = 5 if query.build_system == memory.build_system else 0
    project_points = (
        5
        if memory.scope is MemoryScope.PROJECT and memory.scope_key == query.project
        else 0
    )
    confidence_points = round(max(0.0, min(1.0, memory.confidence)) * 3)
    freshness_points = (
        0
        if _is_stale(memory.last_reinforced_at, now.isoformat(), settings.stale_days)
        else 2
    )
    quality_points = min(5, confidence_points + freshness_points)
    components = (
        semantic_points,
        exact_fingerprint_points,
        failure_family_points,
        causal_token_points,
        language_points,
        build_system_points,
        project_points,
        quality_points,
    )
    return HybridScoreBreakdown(
        total=sum(components),
        semantic_similarity=similarity,
        semantic_points=semantic_points,
        exact_fingerprint_points=exact_fingerprint_points,
        failure_family_points=failure_family_points,
        causal_token_points=causal_token_points,
        language_points=language_points,
        build_system_points=build_system_points,
        project_points=project_points,
        quality_points=quality_points,
    )


def _attempt_id(query: RepairQuery, task_id: str) -> str:
    raw = (
        f"{task_id}:{query.root_cause_group_id}:"
        f"{query.source_pipeline_id}:{query.source_sha}:v{_RETRIEVAL_SCHEMA_VERSION}"
    )
    return f"attempt:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def load_candidate_rows(
    *,
    scope: str,
    scope_key: str,
    path: str | None = None,
    limit: int = 500,
) -> tuple[tuple[RepairMemory, RepairMemoryEmbedding | None], ...]:
    """Load one current active pool with optional vectors in one query."""
    return list_retrieval_candidates(
        scope=scope,
        scope_key=scope_key,
        limit=limit,
        path=path,
    )


def _to_hint(item: _ScoredCandidate) -> RepairMemoryHint:
    memory = item.memory
    return RepairMemoryHint(
        memory_id=memory.memory_id,
        scope=memory.scope,
        pattern_key=memory.pattern_key,
        score=item.total,
        match_reasons=item.match_reasons,
        problem_pattern=memory.problem_pattern,
        applicability=memory.applicability,
        anti_conditions=memory.anti_conditions,
        repair_guidance=memory.repair_guidance,
        validation_guidance=memory.validation_guidance,
        support_episode_count=memory.support_episode_count,
        support_project_count=memory.support_project_count,
        confidence=memory.confidence,
    )


def record_retrieval_hits(
    attempt_id: str,
    task_id: str,
    query: RepairQuery,
    hints: tuple[RepairMemoryHint, ...],
    mode: RetrievalMode,
    *,
    score_details: dict[str, dict[str, Any]] | None = None,
    path: str | None = None,
) -> bool:
    """Persist selected hints and bounded score details without text or vectors."""
    if not hints:
        return True
    db_path = path or get_db_path()
    now = _now_iso()
    details = score_details or {}

    def write(conn: sqlite3.Connection) -> bool:
        for rank, hint in enumerate(hints, start=1):
            score_payload = details.get(hint.memory_id, {"total": hint.score})
            conn.execute(
                "INSERT OR IGNORE INTO repair_memory_hits "
                "(attempt_id, task_id, root_cause_group_id, current_project, "
                "source_pipeline_id, source_sha, memory_id, memory_scope, rank, "
                "score_json, mode, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    task_id,
                    query.root_cause_group_id,
                    query.project,
                    query.source_pipeline_id,
                    query.source_sha,
                    hint.memory_id,
                    hint.scope.value,
                    rank,
                    _json_dumps(score_payload),
                    mode.value,
                    now,
                ),
            )
        return True

    try:
        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to record retrieval hits: {type(error).__name__}")
        return False


def _has_pending_injection(
    task_id: str,
    root_cause_group_id: str,
    *,
    path: str | None = None,
) -> bool:
    db_path = path or get_db_path()
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM repair_memory_hits "
                "WHERE task_id = ? AND root_cause_group_id = ? AND mode = ? LIMIT 1",
                (task_id, root_cause_group_id, RetrievalMode.INJECT.value),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False


def _compatible_embedding(
    memory: RepairMemory,
    embedding: RepairMemoryEmbedding | None,
    settings: RepairMemorySettings,
) -> bool:
    if embedding is None or embedding.status is not EmbeddingStatus.READY:
        return False
    text = build_memory_embedding_text(memory)
    expected_hash = embedding_source_hash(
        text,
        model_name=settings.embedding_model_name,
        model_revision=settings.embedding_model_revision,
    )
    return (
        embedding.model_name == settings.embedding_model_name
        and embedding.model_revision == settings.embedding_model_revision
        and embedding.dimensions == settings.embedding_dimensions
        and embedding.source_hash == expected_hash
        and bool(embedding.vector_blob)
    )


def _validate_query_batch(batch: EmbeddingBatch, settings: RepairMemorySettings) -> tuple[float, ...]:
    if batch.model != settings.embedding_model_name:
        raise EmbeddingServiceError("model_mismatch", "query embedding model mismatch")
    if batch.revision != settings.embedding_model_revision:
        raise EmbeddingServiceError("revision_mismatch", "query embedding revision mismatch")
    if batch.dimensions != settings.embedding_dimensions:
        raise EmbeddingServiceError("dimension_mismatch", "query embedding dimensions mismatch")
    if len(batch.vectors) != 1 or len(batch.vectors[0]) != settings.embedding_dimensions:
        raise EmbeddingServiceError("invalid_response", "query embedding shape mismatch")
    try:
        vector = tuple(float(value) for value in batch.vectors[0])
    except (TypeError, ValueError) as error:
        raise EmbeddingServiceError("invalid_vector", "query embedding contains invalid values") from error
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=0.01):
        raise EmbeddingServiceError("invalid_vector", "query embedding is not normalized")
    return vector


def _hybrid_candidate(
    query: RepairQuery,
    memory: RepairMemory,
    embedding: RepairMemoryEmbedding,
    query_vector: tuple[float, ...],
    *,
    settings: RepairMemorySettings,
    now: datetime,
) -> _ScoredCandidate:
    memory_vector = blob_to_vector(embedding.vector_blob, embedding.dimensions)
    similarity = cosine_similarity(query_vector, memory_vector)
    exact = bool(
        query.diagnostic_fingerprint
        and query.diagnostic_fingerprint == memory.diagnostic_fingerprint
    )
    score = score_memory_hybrid(query, memory, semantic_similarity=similarity, now=now)
    rejection_reason = ""
    if similarity < settings.semantic_min_similarity and not exact:
        rejection_reason = "semantic_below_threshold"
    elif score.total < settings.min_score:
        rejection_reason = "total_below_threshold"
    audit = {
        "total": score.total,
        "scoring_mode": score.scoring_mode,
        "semantic_similarity": round(score.semantic_similarity, 6),
        "semantic_points": score.semantic_points,
        "exact_fingerprint": score.exact_fingerprint_points,
        "failure_family": score.failure_family_points,
        "causal_tokens": score.causal_token_points,
        "language": score.language_points,
        "build_system": score.build_system_points,
        "project_scope": score.project_points,
        "confidence_freshness": score.quality_points,
        "embedding_model": settings.embedding_model_name,
        "embedding_revision": settings.embedding_model_revision,
        "effective_min_score": settings.min_score,
        "semantic_min_similarity": settings.semantic_min_similarity,
    }
    reasons = tuple(
        key
        for key, points in (
            ("semantic", score.semantic_points),
            ("exact_fingerprint", score.exact_fingerprint_points),
            ("failure_family", score.failure_family_points),
            ("causal_tokens", score.causal_token_points),
            ("language", score.language_points),
            ("build_system", score.build_system_points),
            ("project_scope", score.project_points),
            ("confidence_freshness", score.quality_points),
        )
        if points > 0
    )
    return _ScoredCandidate(
        memory=memory,
        total=score.total,
        exact_fingerprint_points=score.exact_fingerprint_points,
        semantic_points=score.semantic_points,
        semantic_similarity=score.semantic_similarity,
        current_project_points=score.project_points,
        audit=audit,
        match_reasons=reasons,
        rejection_reason=rejection_reason,
    )


def _rule_candidate(
    query: RepairQuery,
    memory: RepairMemory,
    *,
    settings: RepairMemorySettings,
    now: str,
    fallback_reason: str,
) -> _ScoredCandidate:
    score = score_memory(query, memory, now=now)
    threshold = settings.min_score if memory.scope is MemoryScope.PROJECT else max(0, settings.min_score - 40)
    current_project_points = (
        5
        if memory.scope is MemoryScope.PROJECT and memory.scope_key == query.project
        else 0
    )
    audit: dict[str, Any] = {
        "total": score.total,
        "scoring_mode": "rule_fallback",
        "fallback_reason": fallback_reason,
        "effective_min_score": threshold,
        **score.breakdown,
    }
    return _ScoredCandidate(
        memory=memory,
        total=score.total,
        exact_fingerprint_points=score.breakdown["exact_fingerprint"],
        semantic_points=0,
        semantic_similarity=None,
        current_project_points=current_project_points,
        audit=audit,
        match_reasons=tuple(key for key, value in score.breakdown.items() if value > 0),
        rejection_reason="total_below_threshold" if score.total < threshold else "",
    )


def _candidate_audits(
    evaluated: list[_ScoredCandidate],
    selected: tuple[_ScoredCandidate, ...],
    *,
    attempt_id: str,
    task_id: str,
) -> tuple[RepairMemoryCandidateAudit, ...]:
    selected_ids = {item.memory.memory_id for item in selected}
    rows = []
    for item in evaluated:
        if item.rejection_reason:
            decision = "rejected"
        elif item.memory.memory_id in selected_ids:
            decision = "selected"
        else:
            decision = "passed_not_selected"
        rows.append(RepairMemoryCandidateAudit(
            attempt_id=attempt_id,
            task_id=task_id,
            memory_id=item.memory.memory_id,
            memory_scope=item.memory.scope,
            scoring_mode=str(item.audit.get("scoring_mode", "")),
            semantic_similarity=item.semantic_similarity,
            total_score=item.total,
            score=item.audit,
            decision=decision,
            rejection_reason=item.rejection_reason,
        ))
    return tuple(rows)


def _sort_key(item: _ScoredCandidate) -> tuple[Any, ...]:
    reinforced = _parse_datetime(item.memory.last_reinforced_at)
    timestamp = reinforced.timestamp() if reinforced is not None else 0.0
    return (
        -item.total,
        -item.exact_fingerprint_points,
        -item.semantic_points,
        -item.current_project_points,
        -item.memory.confidence,
        -timestamp,
        item.memory.memory_id,
    )


def _deduplicate_and_select(
    scored: list[_ScoredCandidate],
    *,
    max_hints: int,
) -> tuple[_ScoredCandidate, ...]:
    ordered = sorted(scored, key=_sort_key)
    selected: list[_ScoredCandidate] = []
    seen_patterns: set[str] = set()
    for item in ordered:
        if item.memory.pattern_key in seen_patterns:
            continue
        selected.append(item)
        seen_patterns.add(item.memory.pattern_key)
        if len(selected) >= max_hints:
            break
    return tuple(selected)


def _fallback_reason(error: Exception) -> str:
    if isinstance(error, EmbeddingServiceError):
        return error.code[:80]
    return "embedding_error"


def retrieve_repair_hints(
    query: RepairQuery,
    task_id: str,
    mode: RetrievalMode,
    path: str | None = None,
    *,
    embedding_client: EmbeddingClient | None = None,
) -> RetrievalResult:
    """Retrieve project and global hints with hybrid scoring and safe fallback."""
    settings = load_repair_memory_settings()
    db_path = path or get_db_path()
    attempt_id = _attempt_id(query, task_id)
    if mode is RetrievalMode.OFF:
        record_retrieval_completion(
            task_id,
            query,
            attempt_id=attempt_id,
            mode=mode,
            status=RetrievalAuditStatus.NOT_ATTEMPTED,
            reason_code="memory_mode_off",
            candidate_count=0,
            passed_threshold_count=0,
            selected_count=0,
            increment_search=False,
            path=db_path,
        )
        return RetrievalResult(
            mode=mode,
            attempt_id="",
            hints=(),
            audit_persisted=False,
            max_prompt_chars=settings.max_prompt_chars,
        )

    now_text = _now_iso()
    now = _parse_datetime(now_text) or datetime.now(timezone.utc)
    try:
        if mode is RetrievalMode.INJECT and _has_pending_injection(
            task_id, query.root_cause_group_id, path=db_path
        ):
            record_retrieval_completion(
                task_id,
                query,
                attempt_id=attempt_id,
                mode=mode,
                status=RetrievalAuditStatus.RECALLED,
                reason_code="duplicate_suppressed",
                candidate_count=0,
                passed_threshold_count=0,
                selected_count=0,
                increment_search=False,
                path=db_path,
            )
            return RetrievalResult(
                mode=mode,
                attempt_id=attempt_id,
                hints=(),
                audit_persisted=True,
                max_prompt_chars=settings.max_prompt_chars,
            )

        limit = settings.semantic_candidate_limit_per_scope
        project_rows = load_candidate_rows(
            scope=MemoryScope.PROJECT.value,
            scope_key=query.project,
            path=db_path,
            limit=limit,
        )
        global_rows = load_candidate_rows(
            scope=MemoryScope.GLOBAL.value,
            scope_key="*",
            path=db_path,
            limit=limit,
        )
        candidates = tuple(project_rows) + tuple(global_rows)
        if not candidates:
            record_retrieval_completion(
                task_id,
                query,
                attempt_id=attempt_id,
                mode=mode,
                status=RetrievalAuditStatus.NO_MATCH,
                reason_code="no_candidates",
                candidate_count=0,
                passed_threshold_count=0,
                selected_count=0,
                path=db_path,
            )
            return RetrievalResult(mode, attempt_id, (), False, settings.max_prompt_chars)

        compatible = tuple(
            (memory, embedding)
            for memory, embedding in candidates
            if _compatible_embedding(memory, embedding, settings)
        )
        evaluated: list[_ScoredCandidate] = []
        fallback_reason = "missing_compatible_embedding"
        if compatible:
            try:
                client = embedding_client or HttpEmbeddingClient(settings.embedding_service_url)
                query_batch = client.encode(
                    (build_query_embedding_text(query),),
                    timeout_seconds=settings.semantic_timeout_ms / 1000.0,
                )
                query_vector = _validate_query_batch(query_batch, settings)
                compatible_ids = {memory.memory_id for memory, _embedding in compatible}
                for memory, embedding in compatible:
                    try:
                        item = _hybrid_candidate(
                            query,
                            memory,
                            embedding,
                            query_vector,
                            settings=settings,
                            now=now,
                        )
                    except ValueError:
                        item = _rule_candidate(
                            query,
                            memory,
                            settings=settings,
                            now=now_text,
                            fallback_reason="invalid_vector",
                        )
                    evaluated.append(item)
                for memory, _embedding in candidates:
                    if memory.memory_id in compatible_ids:
                        continue
                    item = _rule_candidate(
                        query,
                        memory,
                        settings=settings,
                        now=now_text,
                        fallback_reason="missing_compatible_embedding",
                    )
                    evaluated.append(item)
                fallback_reason = ""
            except Exception as error:
                fallback_reason = _fallback_reason(error)

        if fallback_reason:
            evaluated = []
            for memory, _embedding in candidates:
                item = _rule_candidate(
                    query,
                    memory,
                    settings=settings,
                    now=now_text,
                    fallback_reason=fallback_reason,
                )
                evaluated.append(item)

        scored = [item for item in evaluated if not item.rejection_reason]
        selected = _deduplicate_and_select(scored, max_hints=settings.max_hints)
        candidate_audit_persisted = record_retrieval_candidate_audits(
            _candidate_audits(evaluated, selected, attempt_id=attempt_id, task_id=task_id),
            db_path,
        )
        if not candidate_audit_persisted:
            get_logger().warning("Repair memory candidate audit was not persisted")
        if not selected:
            record_retrieval_completion(
                task_id,
                query,
                attempt_id=attempt_id,
                mode=mode,
                status=RetrievalAuditStatus.NO_MATCH,
                reason_code="below_threshold",
                candidate_count=len(candidates),
                passed_threshold_count=len(scored),
                selected_count=0,
                path=db_path,
            )
            return RetrievalResult(mode, attempt_id, (), False, settings.max_prompt_chars)
        hints = tuple(_to_hint(item) for item in selected)
        record_retrieval_completion(
            task_id,
            query,
            attempt_id=attempt_id,
            mode=mode,
            status=RetrievalAuditStatus.RECALLED,
            reason_code="selected",
            candidate_count=len(candidates),
            passed_threshold_count=len(scored),
            selected_count=len(selected),
            path=db_path,
        )
        score_details = {item.memory.memory_id: item.audit for item in selected}
        audit_persisted = record_retrieval_hits(
            attempt_id,
            task_id,
            query,
            hints,
            mode,
            score_details=score_details,
            path=db_path,
        )
        if mode is RetrievalMode.INJECT and not audit_persisted:
            return RetrievalResult(mode, attempt_id, (), False, settings.max_prompt_chars)

        get_logger().info(
            "Repair memory retrieval: "
            f"candidates={len(candidates)} semantic_candidates={len(compatible)} "
            f"hybrid={sum(item.audit.get('scoring_mode') == 'hybrid' for item in evaluated)} "
            f"fallback={sum(item.audit.get('scoring_mode') == 'rule_fallback' for item in evaluated)} "
            f"passed_threshold={len(scored)} selected={len(selected)} "
            f"injected={len(selected) if mode is RetrievalMode.INJECT else 0}"
        )
        return RetrievalResult(mode, attempt_id, hints, audit_persisted, settings.max_prompt_chars)
    except Exception as error:
        record_retrieval_completion(
            task_id,
            query,
            attempt_id=attempt_id,
            mode=mode,
            status=RetrievalAuditStatus.ERROR,
            reason_code="retrieval_error",
            candidate_count=0,
            passed_threshold_count=0,
            selected_count=0,
            error_code=type(error).__name__,
            path=db_path,
        )
        get_logger().error(f"Failed to retrieve repair hints: {type(error).__name__}")
        return RetrievalResult(mode, attempt_id, (), False, settings.max_prompt_chars)


def _now_iso() -> str:
    from pr_agent.feedback.timez import now_cn_iso

    return now_cn_iso()
