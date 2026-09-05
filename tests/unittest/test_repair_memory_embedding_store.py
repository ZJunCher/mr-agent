"""Unit tests for repair-memory embedding persistence and incremental indexing."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before eager ut_agent imports
import ut_agent.repair_memory.cli as cli_module
from tests.unittest.repair_memory_helpers import enabled_memory_settings, sample_memory
from ut_agent.repair_memory.cli import cli_main
from ut_agent.repair_memory.embedding import (
    BGE_DIMENSIONS,
    BGE_MODEL_NAME,
    BGE_MODEL_REVISION,
    EmbeddingBatch,
    EmbeddingServiceError,
    blob_to_vector,
)
from ut_agent.repair_memory.embedding_store import (
    embedding_status_summary,
    list_memories_requiring_embedding,
    run_embedding_batch,
)
from ut_agent.repair_memory.models import EmbeddingStatus, MemoryStatus
from ut_agent.repair_memory.store import (
    init_repair_memory_tables,
    load_memory_embedding,
    save_memory,
)

NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def memory_db(tmp_path) -> str:
    path = str(tmp_path / "repair-memory.db")
    init_repair_memory_tables(path)
    return path


def _settings(**overrides):
    values = {
        "embedding_batch_size": 16,
        "embedding_batch_timeout_seconds": 30.0,
        "embedding_model_name": BGE_MODEL_NAME,
        "embedding_model_revision": BGE_MODEL_REVISION,
        "embedding_dimensions": BGE_DIMENSIONS,
    }
    values.update(overrides)
    return enabled_memory_settings(**values)


def _unit_vector(index: int = 0) -> tuple[float, ...]:
    values = [0.0] * BGE_DIMENSIONS
    values[index] = 1.0
    return tuple(values)


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def encode(self, texts, *, timeout_seconds):
        self.calls.append((texts, timeout_seconds))
        return EmbeddingBatch(
            model=BGE_MODEL_NAME,
            revision=BGE_MODEL_REVISION,
            dimensions=BGE_DIMENSIONS,
            vectors=tuple(_unit_vector(index % 2) for index, _text in enumerate(texts)),
        )


class _FailingClient:
    def __init__(self, code: str = "timeout") -> None:
        self.code = code

    def encode(self, texts, *, timeout_seconds):
        raise EmbeddingServiceError(self.code, "private service response must not be stored")


def test_only_active_memories_are_selected_for_embedding(memory_db):
    assert save_memory(sample_memory("active", pattern_key="active"), memory_db)
    assert save_memory(
        sample_memory("disabled", pattern_key="disabled", status=MemoryStatus.DISABLED), memory_db
    )
    assert save_memory(
        sample_memory("superseded", pattern_key="superseded", status=MemoryStatus.SUPERSEDED), memory_db
    )

    selected = list_memories_requiring_embedding(limit=16, now=NOW, path=memory_db, settings=_settings())

    assert [memory.memory_id for memory in selected] == ["active"]


def test_embedding_batch_is_bounded_and_persists_ready_float32_vectors(memory_db):
    for index in range(20):
        assert save_memory(
            sample_memory(f"mem-{index:02d}", pattern_key=f"pattern-{index:02d}"), memory_db
        )
    client = _FakeClient()

    summary = run_embedding_batch(client=client, settings=_settings(), now=NOW, path=memory_db)

    assert summary.selected == 16
    assert summary.indexed == 16
    assert summary.failed == 0
    assert len(client.calls) == 1
    assert len(client.calls[0][0]) == 16
    stored = load_memory_embedding("mem-00", memory_db)
    assert stored is not None
    assert stored.status is EmbeddingStatus.READY
    assert len(blob_to_vector(stored.vector_blob, stored.dimensions)) == BGE_DIMENSIONS


def test_unchanged_ready_embedding_is_skipped_but_changed_memory_is_reindexed(memory_db):
    assert save_memory(sample_memory("mem-1"), memory_db)
    first_client = _FakeClient()
    first = run_embedding_batch(client=first_client, settings=_settings(), now=NOW, path=memory_db)
    first_embedding = load_memory_embedding("mem-1", memory_db)

    unchanged_client = _FakeClient()
    unchanged = run_embedding_batch(
        client=unchanged_client,
        settings=_settings(),
        now=NOW + timedelta(minutes=1),
        path=memory_db,
    )
    changed = replace(sample_memory("mem-1"), repair_guidance="按照新接口调整测试夹具")
    assert save_memory(changed, memory_db)
    changed_client = _FakeClient()
    reindexed = run_embedding_batch(
        client=changed_client,
        settings=_settings(),
        now=NOW + timedelta(minutes=2),
        path=memory_db,
    )
    changed_embedding = load_memory_embedding("mem-1", memory_db)

    assert first.indexed == 1
    assert unchanged.selected == 0
    assert unchanged_client.calls == []
    assert reindexed.indexed == 1
    assert first_embedding is not None and changed_embedding is not None
    assert changed_embedding.source_hash != first_embedding.source_hash


def test_model_or_revision_change_requires_reindexing(memory_db):
    assert save_memory(sample_memory("mem-1"), memory_db)
    run_embedding_batch(client=_FakeClient(), settings=_settings(), now=NOW, path=memory_db)

    changed_model = list_memories_requiring_embedding(
        limit=16,
        now=NOW,
        path=memory_db,
        settings=_settings(embedding_model_name="BAAI/bge-m3-next"),
    )
    changed_revision = list_memories_requiring_embedding(
        limit=16,
        now=NOW,
        path=memory_db,
        settings=_settings(embedding_model_revision="new-revision"),
    )
    changed_dimensions = list_memories_requiring_embedding(
        limit=16,
        now=NOW,
        path=memory_db,
        settings=_settings(embedding_dimensions=BGE_DIMENSIONS + 1),
    )

    assert [item.memory_id for item in changed_model] == ["mem-1"]
    assert [item.memory_id for item in changed_revision] == ["mem-1"]
    assert [item.memory_id for item in changed_dimensions] == ["mem-1"]


@pytest.mark.parametrize(
    ("failure_number", "delay"),
    [(1, 60), (2, 300), (3, 1800), (4, 21600), (5, 21600)],
)
def test_embedding_failures_use_bounded_retry_schedule(memory_db, failure_number, delay):
    assert save_memory(sample_memory("mem-1"), memory_db)
    settings = _settings()
    attempt_time = NOW
    for _attempt in range(failure_number):
        summary = run_embedding_batch(
            client=_FailingClient(),
            settings=settings,
            now=attempt_time,
            path=memory_db,
        )
        assert summary.failed == 1
        stored = load_memory_embedding("mem-1", memory_db)
        assert stored is not None
        attempt_time = datetime.fromisoformat(stored.next_retry_at)

    stored = load_memory_embedding("mem-1", memory_db)
    assert stored is not None
    assert stored.status is EmbeddingStatus.FAILED
    assert stored.attempt_count == failure_number
    assert datetime.fromisoformat(stored.next_retry_at) == attempt_time
    previous_attempt_time = NOW if failure_number == 1 else attempt_time - timedelta(seconds=delay)
    assert int((attempt_time - previous_attempt_time).total_seconds()) == delay
    assert stored.last_error_code == "timeout"
    assert "private service response" not in stored.last_error_code


def test_failed_embedding_is_not_selected_before_retry_time(memory_db):
    assert save_memory(sample_memory("mem-1"), memory_db)
    run_embedding_batch(client=_FailingClient(), settings=_settings(), now=NOW, path=memory_db)

    assert list_memories_requiring_embedding(
        limit=16,
        now=NOW + timedelta(seconds=59),
        path=memory_db,
        settings=_settings(),
    ) == ()
    assert [
        memory.memory_id
        for memory in list_memories_requiring_embedding(
            limit=16,
            now=NOW + timedelta(seconds=60),
            path=memory_db,
            settings=_settings(),
        )
    ] == ["mem-1"]


def test_embedding_status_summary_reports_ready_failed_and_missing(memory_db):
    assert save_memory(sample_memory("ready", pattern_key="ready"), memory_db)
    assert save_memory(sample_memory("failed", pattern_key="failed"), memory_db)
    assert save_memory(sample_memory("missing", pattern_key="missing"), memory_db)
    run_embedding_batch(client=_FakeClient(), settings=_settings(embedding_batch_size=1), now=NOW, path=memory_db)
    run_embedding_batch(
        client=_FailingClient(),
        settings=_settings(embedding_batch_size=1),
        now=NOW,
        path=memory_db,
    )

    status = embedding_status_summary(settings=_settings(), now=NOW, path=memory_db)

    assert status.ready == 1
    assert status.failed == 1
    assert status.missing == 1


def test_embedding_cli_backfill_and_status(memory_db, monkeypatch, capsys):
    assert save_memory(sample_memory("mem-1"), memory_db)
    client = _FakeClient()
    monkeypatch.setattr(cli_module, "HttpEmbeddingClient", lambda _url: client)

    assert cli_main(["embeddings", "backfill", "--limit", "1"], path=memory_db) == 0
    assert load_memory_embedding("mem-1", memory_db).status is EmbeddingStatus.READY
    assert cli_main(["embeddings", "status"], path=memory_db) == 0

    output = capsys.readouterr().out
    assert "indexed=1" in output
    assert '"ready": 1' in output
