"""Incremental BGE-M3 indexing for active repair memories."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pr_agent.log import get_logger
from ut_agent.repair_memory.config import RepairMemorySettings, load_repair_memory_settings
from ut_agent.repair_memory.embedding import (
    MAX_EMBEDDING_BATCH,
    EmbeddingBatch,
    EmbeddingClient,
    EmbeddingServiceError,
    build_memory_embedding_text,
    embedding_source_hash,
    vector_to_blob,
)
from ut_agent.repair_memory.models import EmbeddingStatus, RepairMemory, RepairMemoryEmbedding
from ut_agent.repair_memory.store import (
    list_memories,
    load_memory_embedding,
    upsert_memory_embeddings,
)

_RETRY_DELAYS_SECONDS = (60, 300, 1800, 21600)
_STABLE_ERROR_CODES = frozenset(
    {
        "timeout",
        "unavailable",
        "http_error",
        "invalid_response",
        "model_mismatch",
        "revision_mismatch",
        "dimension_mismatch",
        "invalid_vector",
    }
)


@dataclass(frozen=True)
class EmbeddingIndexSummary:
    """Bounded counters for one incremental indexing batch."""

    selected: int = 0
    indexed: int = 0
    skipped_unchanged: int = 0
    failed: int = 0


@dataclass(frozen=True)
class EmbeddingStatusSummary:
    """Current active-memory embedding states."""

    ready: int = 0
    pending: int = 0
    failed: int = 0
    stale: int = 0
    missing: int = 0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _source(memory: RepairMemory, settings: RepairMemorySettings) -> tuple[str, str]:
    text = build_memory_embedding_text(memory)
    return text, embedding_source_hash(
        text,
        model_name=settings.embedding_model_name,
        model_revision=settings.embedding_model_revision,
    )


def _matches_source(
    embedding: RepairMemoryEmbedding,
    source_hash: str,
    settings: RepairMemorySettings,
) -> bool:
    return (
        embedding.source_hash == source_hash
        and embedding.model_name == settings.embedding_model_name
        and embedding.model_revision == settings.embedding_model_revision
        and embedding.dimensions == settings.embedding_dimensions
    )


def _retry_due(embedding: RepairMemoryEmbedding, now: datetime) -> bool:
    retry_at = _parse_iso(embedding.next_retry_at)
    return retry_at is None or retry_at <= _utc(now)


def list_memories_requiring_embedding(
    *,
    limit: int,
    now: datetime,
    path: str | None = None,
    settings: RepairMemorySettings | None = None,
) -> tuple[RepairMemory, ...]:
    """Select active memories whose vector is missing, stale, pending, or retryable."""
    if limit <= 0:
        return ()
    effective_settings = settings or load_repair_memory_settings()
    selected: list[RepairMemory] = []
    for memory in list_memories(status="active", path=path):
        _text, source_hash = _source(memory, effective_settings)
        embedding = load_memory_embedding(memory.memory_id, path)
        if embedding is None or not _matches_source(embedding, source_hash, effective_settings):
            selected.append(memory)
        elif embedding.status is EmbeddingStatus.READY:
            continue
        elif embedding.status is EmbeddingStatus.FAILED and not _retry_due(embedding, now):
            continue
        else:
            selected.append(memory)
        if len(selected) >= min(limit, MAX_EMBEDDING_BATCH):
            break
    return tuple(selected)


def _validate_batch(
    batch: EmbeddingBatch,
    *,
    expected_count: int,
    settings: RepairMemorySettings,
) -> None:
    if batch.model != settings.embedding_model_name:
        raise EmbeddingServiceError("model_mismatch", "embedding model mismatch")
    if batch.revision != settings.embedding_model_revision:
        raise EmbeddingServiceError("revision_mismatch", "embedding revision mismatch")
    if batch.dimensions != settings.embedding_dimensions:
        raise EmbeddingServiceError("dimension_mismatch", "embedding dimensions mismatch")
    if len(batch.vectors) != expected_count:
        raise EmbeddingServiceError("invalid_response", "embedding vector count mismatch")
    for vector in batch.vectors:
        if len(vector) != settings.embedding_dimensions:
            raise EmbeddingServiceError("dimension_mismatch", "embedding dimensions mismatch")
        norm = math.sqrt(sum(float(value) * float(value) for value in vector))
        if not math.isfinite(norm) or not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=0.01):
            raise EmbeddingServiceError("invalid_vector", "embedding vector is not normalized")


def _error_code(error: Exception) -> str:
    if isinstance(error, EmbeddingServiceError) and error.code in _STABLE_ERROR_CODES:
        return error.code
    return "invalid_response"


def _retry_delay(attempt_count: int) -> int:
    index = min(max(1, attempt_count), len(_RETRY_DELAYS_SECONDS)) - 1
    return _RETRY_DELAYS_SECONDS[index]


def _failed_rows(
    memories: tuple[RepairMemory, ...],
    source_hashes: tuple[str, ...],
    *,
    error_code: str,
    settings: RepairMemorySettings,
    now: datetime,
    path: str | None,
) -> tuple[RepairMemoryEmbedding, ...]:
    now_text = _iso(now)
    rows: list[RepairMemoryEmbedding] = []
    for memory, source_hash in zip(memories, source_hashes, strict=True):
        existing = load_memory_embedding(memory.memory_id, path)
        same_source = existing is not None and _matches_source(existing, source_hash, settings)
        attempt_count = (existing.attempt_count if same_source else 0) + 1
        created_at = existing.created_at if existing is not None else now_text
        rows.append(
            RepairMemoryEmbedding(
                memory_id=memory.memory_id,
                model_name=settings.embedding_model_name,
                model_revision=settings.embedding_model_revision,
                dimensions=settings.embedding_dimensions,
                vector_blob=b"",
                source_hash=source_hash,
                status=EmbeddingStatus.FAILED,
                last_error_code=error_code,
                attempt_count=attempt_count,
                next_retry_at=_iso(now + timedelta(seconds=_retry_delay(attempt_count))),
                created_at=created_at,
                updated_at=now_text,
            )
        )
    return tuple(rows)


def _ready_row(
    memory: RepairMemory,
    source_hash: str,
    vector: tuple[float, ...],
    *,
    settings: RepairMemorySettings,
    now_text: str,
    path: str | None,
) -> RepairMemoryEmbedding:
    existing = load_memory_embedding(memory.memory_id, path)
    return RepairMemoryEmbedding(
        memory_id=memory.memory_id,
        model_name=settings.embedding_model_name,
        model_revision=settings.embedding_model_revision,
        dimensions=settings.embedding_dimensions,
        vector_blob=vector_to_blob(vector),
        source_hash=source_hash,
        status=EmbeddingStatus.READY,
        created_at=existing.created_at if existing is not None else now_text,
        updated_at=now_text,
    )


def run_embedding_batch(
    *,
    client: EmbeddingClient,
    settings: RepairMemorySettings,
    now: datetime,
    path: str | None = None,
) -> EmbeddingIndexSummary:
    """Index one bounded batch without exposing memory text or vector values."""
    batch_limit = min(settings.embedding_batch_size, MAX_EMBEDDING_BATCH)
    memories = list_memories_requiring_embedding(
        limit=batch_limit,
        now=now,
        path=path,
        settings=settings,
    )
    if not memories:
        return EmbeddingIndexSummary()

    texts_and_hashes = tuple(_source(memory, settings) for memory in memories)
    texts = tuple(item[0] for item in texts_and_hashes)
    source_hashes = tuple(item[1] for item in texts_and_hashes)
    try:
        batch = client.encode(texts, timeout_seconds=settings.embedding_batch_timeout_seconds)
        _validate_batch(batch, expected_count=len(memories), settings=settings)
        now_text = _iso(now)
        ready_rows = tuple(
            _ready_row(
                memory,
                source_hash,
                vector,
                settings=settings,
                now_text=now_text,
                path=path,
            )
            for memory, source_hash, vector in zip(
                memories, source_hashes, batch.vectors, strict=True
            )
        )
        if not upsert_memory_embeddings(ready_rows, path):
            return EmbeddingIndexSummary(selected=len(memories), failed=len(memories))
        return EmbeddingIndexSummary(selected=len(memories), indexed=len(memories))
    except Exception as error:
        code = _error_code(error)
        failed_rows = _failed_rows(
            memories,
            source_hashes,
            error_code=code,
            settings=settings,
            now=now,
            path=path,
        )
        upsert_memory_embeddings(failed_rows, path)
        get_logger().warning(
            f"Repair memory embedding batch failed: error_code={code} count={len(memories)}"
        )
        return EmbeddingIndexSummary(selected=len(memories), failed=len(memories))


def embedding_status_summary(
    *,
    settings: RepairMemorySettings | None = None,
    now: datetime | None = None,
    path: str | None = None,
) -> EmbeddingStatusSummary:
    """Return active-memory index counts without loading or logging vectors."""
    effective_settings = settings or load_repair_memory_settings()
    _ = now  # Reserved for future overdue-pending reporting without changing the API.
    ready = pending = failed = stale = missing = 0
    for memory in list_memories(status="active", path=path):
        _text, source_hash = _source(memory, effective_settings)
        embedding = load_memory_embedding(memory.memory_id, path)
        if embedding is None:
            missing += 1
        elif not _matches_source(embedding, source_hash, effective_settings):
            stale += 1
        elif embedding.status is EmbeddingStatus.READY:
            ready += 1
        elif embedding.status is EmbeddingStatus.FAILED:
            failed += 1
        else:
            pending += 1
    return EmbeddingStatusSummary(
        ready=ready,
        pending=pending,
        failed=failed,
        stale=stale,
        missing=missing,
    )
