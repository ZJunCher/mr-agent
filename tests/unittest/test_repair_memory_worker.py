"""Focused tests for the dedicated Repair Memory consolidation worker."""

from types import SimpleNamespace

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize settings before eager ut_agent imports
import ut_agent.repair_memory.worker as worker_module
from ut_agent.repair_memory.consolidate import (
    BatchSummary,
    LegacyMigrationSummary,
    PromotionSummary,
)
from ut_agent.repair_memory.embedding_store import EmbeddingIndexSummary
from ut_agent.repair_memory.store import PruneSummary


def _settings(**overrides):
    values = {
        "consolidation_batch_size": 50,
        "consolidation_lease_seconds": 300,
        "consolidation_poll_seconds": 60,
        "consolidation_model_timeout_seconds": 60,
        "promotion_enabled": True,
        "episode_retention_days": 365,
        "hit_retention_days": 365,
        "embedding_service_url": "http://embedding:8080",
        "embedding_batch_size": 16,
        "embedding_batch_timeout_seconds": 30.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _default_legacy_migration(monkeypatch):
    monkeypatch.setattr(
        worker_module,
        "migrate_legacy_memories",
        lambda **_kwargs: LegacyMigrationSummary(),
        raising=False,
    )


def test_run_cycle_consolidates_promotes_indexes_and_prunes(monkeypatch, tmp_path):
    db_path = str(tmp_path / "memory.db")
    observed = []

    def fake_consolidate(limit, owner, path, *, llm_call, lease_seconds):
        observed.append(("consolidate", limit, owner, path, llm_call, lease_seconds))
        return BatchSummary(claimed=2, completed=2, failed=0, invalid=0)

    async def fake_promote(path, *, llm_call):
        observed.append(("promote", path, llm_call))
        return PromotionSummary(promoted=1, skipped=0)

    def fake_migrate(*, limit, owner, llm_call, path):
        observed.append(("migrate", limit, owner, llm_call, path))
        return LegacyMigrationSummary(selected=1, migrated=1)

    def fake_prune(now, *, episode_retention_days, hit_retention_days, path):
        observed.append(("prune", now, episode_retention_days, hit_retention_days, path))
        return PruneSummary(deleted_episodes=3, deleted_hits=4)

    def fake_index(*, client, settings, now, path):
        observed.append(("index", client, settings, now, path))
        return EmbeddingIndexSummary(selected=2, indexed=2)

    monkeypatch.setattr(worker_module, "load_repair_memory_settings", _settings)
    monkeypatch.setattr(worker_module, "run_consolidation_batch", fake_consolidate)
    monkeypatch.setattr(worker_module, "migrate_legacy_memories", fake_migrate)
    monkeypatch.setattr(worker_module, "promote_ready_patterns", fake_promote)
    embedding_client = object()
    monkeypatch.setattr(worker_module, "HttpEmbeddingClient", lambda _url: embedding_client)
    monkeypatch.setattr(worker_module, "run_embedding_batch", fake_index)
    monkeypatch.setattr(worker_module, "prune_expired_memory_data", fake_prune)

    summary = worker_module.run_cycle("memory-worker:test", path=db_path)

    assert summary == worker_module.WorkerCycleSummary(
        claimed=2,
        completed=2,
        failed=0,
        invalid=0,
        promoted=1,
        skipped=0,
        legacy_selected=1,
        legacy_migrated=1,
        legacy_marked_for_review=0,
        legacy_failed=0,
        embeddings_selected=2,
        embeddings_indexed=2,
        embeddings_failed=0,
        deleted_episodes=3,
        deleted_hits=4,
    )
    assert observed[0][1:4] == (50, "memory-worker:test", db_path)
    assert observed[0][-1] == 300
    assert observed[1][0:3] == ("migrate", 5, "memory-worker:test")
    assert observed[2][0] == "promote"
    assert observed[3][0:3] == ("index", embedding_client, _settings())
    assert observed[3][-1] == db_path
    assert observed[4][2:] == (365, 365, db_path)


def test_run_cycle_skips_promotion_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(
        worker_module,
        "load_repair_memory_settings",
        lambda: _settings(promotion_enabled=False),
    )
    monkeypatch.setattr(
        worker_module,
        "run_consolidation_batch",
        lambda *_args, **_kwargs: BatchSummary(claimed=0, completed=0, failed=0, invalid=0),
    )

    async def unexpected_promotion(*_args, **_kwargs):
        raise AssertionError("promotion must remain disabled")

    monkeypatch.setattr(worker_module, "promote_ready_patterns", unexpected_promotion)
    monkeypatch.setattr(worker_module, "HttpEmbeddingClient", lambda _url: object())
    monkeypatch.setattr(
        worker_module,
        "run_embedding_batch",
        lambda **_kwargs: EmbeddingIndexSummary(),
    )
    monkeypatch.setattr(
        worker_module,
        "prune_expired_memory_data",
        lambda *_args, **_kwargs: PruneSummary(deleted_episodes=0, deleted_hits=0),
    )
    monkeypatch.setattr(worker_module, "HttpEmbeddingClient", lambda _url: object())
    monkeypatch.setattr(
        worker_module,
        "run_embedding_batch",
        lambda **_kwargs: EmbeddingIndexSummary(),
    )

    summary = worker_module.run_cycle("memory-worker:test", path=str(tmp_path / "memory.db"))

    assert summary.promoted == 0
    assert summary.skipped == 0


def test_run_cycle_contains_phase_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_module, "load_repair_memory_settings", _settings)
    monkeypatch.setattr(
        worker_module,
        "run_consolidation_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private episode body")),
    )
    monkeypatch.setattr(
        worker_module,
        "prune_expired_memory_data",
        lambda *_args, **_kwargs: PruneSummary(deleted_episodes=0, deleted_hits=0),
    )

    summary = worker_module.run_cycle("memory-worker:test", path=str(tmp_path / "memory.db"))

    assert summary.failed == 1


def test_run_cycle_contains_embedding_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_module, "load_repair_memory_settings", _settings)
    monkeypatch.setattr(
        worker_module,
        "run_consolidation_batch",
        lambda *_args, **_kwargs: BatchSummary(claimed=0, completed=0, failed=0, invalid=0),
    )
    monkeypatch.setattr(worker_module, "promote_ready_patterns", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_module, "HttpEmbeddingClient", lambda _url: object())
    monkeypatch.setattr(
        worker_module,
        "run_embedding_batch",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("private vector")),
    )
    monkeypatch.setattr(
        worker_module,
        "prune_expired_memory_data",
        lambda *_args, **_kwargs: PruneSummary(deleted_episodes=0, deleted_hits=0),
    )

    summary = worker_module.run_cycle("memory-worker:test", path=str(tmp_path / "memory.db"))

    assert summary.embeddings_failed == 1


class _StopAfterWaits:
    def __init__(self, limit: int):
        self.limit = limit
        self.waits = []

    def is_set(self):
        return len(self.waits) >= self.limit

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.is_set()

    def set(self):
        self.limit = 0


def test_run_forever_initializes_runs_immediately_and_uses_poll_interval(monkeypatch, tmp_path):
    stop = _StopAfterWaits(1)
    observed = []
    db_path = str(tmp_path / "memory.db")
    monkeypatch.setattr(worker_module, "load_repair_memory_settings", _settings)
    monkeypatch.setattr(worker_module, "get_db_path", lambda: db_path)
    monkeypatch.setattr(worker_module, "init_repair_memory_tables", lambda path: observed.append(("init", path)))
    monkeypatch.setattr(worker_module, "run_cycle", lambda owner, path: observed.append(("cycle", owner, path)))

    worker_module.run_forever(stop_event=stop, install_signal_handlers=False)

    assert observed[0] == ("init", db_path)
    assert observed[1][0] == "cycle"
    assert observed[1][2] == db_path
    assert stop.waits == [60]


def test_run_forever_continues_after_unexpected_cycle_error(monkeypatch, tmp_path):
    stop = _StopAfterWaits(2)
    calls = []
    monkeypatch.setattr(worker_module, "load_repair_memory_settings", _settings)
    monkeypatch.setattr(worker_module, "get_db_path", lambda: str(tmp_path / "memory.db"))
    monkeypatch.setattr(worker_module, "init_repair_memory_tables", lambda _path: None)

    def flaky_cycle(_owner, _path):
        calls.append("cycle")
        if len(calls) == 1:
            raise RuntimeError("first cycle failed")

    monkeypatch.setattr(worker_module, "run_cycle", flaky_cycle)

    worker_module.run_forever(stop_event=stop, install_signal_handlers=False)

    assert calls == ["cycle", "cycle"]
    assert stop.waits == [60, 60]
