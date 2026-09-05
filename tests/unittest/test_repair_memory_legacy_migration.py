"""Tests for resumable migration of legacy English repair memories."""

from __future__ import annotations

import json
import sqlite3

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before eager ut_agent imports
import ut_agent.repair_memory.cli as cli_module
from tests.unittest.repair_memory_helpers import (
    sample_memory,
    seed_pending_episode,
    valid_candidate_payload,
)
from ut_agent.model_failover import LLMCallOutcome
from ut_agent.repair_memory.cli import cli_main
from ut_agent.repair_memory.consolidate import migrate_legacy_memories
from ut_agent.repair_memory.models import EmbeddingStatus, MemoryStatus
from ut_agent.repair_memory.store import (
    init_repair_memory_tables,
    list_memories,
    list_memory_events,
    load_memory,
    load_memory_embedding,
    save_memory,
    save_memory_with_evidence,
)


@pytest.fixture
def memory_db(tmp_path) -> str:
    path = str(tmp_path / "repair-memory.db")
    init_repair_memory_tables(path)
    return path


def _legacy_memory(memory_id: str = "legacy-1", **overrides):
    values = {
        "memory_id": memory_id,
        "pattern_key": "legacy-pattern",
        "content_locale": "legacy",
        "problem_pattern": "A request member is absent.",
        "applicability": ("The compiler reports a missing member.",),
        "anti_conditions": ("The member still exists.",),
        "repair_guidance": "Align the test with the current interface.",
        "validation_guidance": ("Run the exact-SHA Pipeline.",),
    }
    values.update(overrides)
    return sample_memory(**values)


def _outcome(payload: dict):
    return LLMCallOutcome(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "memory-1",
                "type": "function",
                "function": {
                    "name": "submit_repair_memory",
                    "arguments": json.dumps(payload, ensure_ascii=False),
                },
            }],
        },
        "test-model",
        (),
    )


def _evidence_ids(path: str, memory_id: str) -> tuple[str, ...]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT episode_id FROM repair_memory_evidence WHERE memory_id = ? ORDER BY episode_id",
            (memory_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)
    finally:
        connection.close()


def test_legacy_memory_with_evidence_is_regenerated_in_chinese_and_superseded(memory_db):
    episode = seed_pending_episode(memory_db, episode_id="episode:legacy")
    legacy = _legacy_memory()
    assert save_memory_with_evidence(legacy, episode.episode_id, memory_db)
    prompts: list[str] = []

    async def llm_call(system, user, **kwargs):
        prompts.append(user)
        return _outcome(valid_candidate_payload())

    summary = migrate_legacy_memories(
        limit=10,
        owner="migration-worker",
        llm_call=llm_call,
        path=memory_db,
    )

    old = load_memory(legacy.memory_id, memory_db)
    active = list_memories(pattern_key=legacy.pattern_key, status="active", path=memory_db)
    assert summary.selected == 1
    assert summary.migrated == 1
    assert old is not None and old.status is MemoryStatus.SUPERSEDED
    assert len(active) == 1
    migrated = active[0]
    assert migrated.content_locale == "zh-CN"
    assert migrated.supersedes_id == legacy.memory_id
    assert _evidence_ids(memory_db, migrated.memory_id) == (episode.episode_id,)
    embedding = load_memory_embedding(migrated.memory_id, memory_db)
    assert embedding is not None and embedding.status is EmbeddingStatus.PENDING
    assert "[LEGACY_REPAIR_MEMORY]" in prompts[0]
    assert "[SUPPORTING_EPISODE]" in prompts[0]
    assert episode.project not in prompts[0]
    events = list_memory_events(legacy.memory_id, memory_db)
    assert events[-1].event_type == "legacy_memory_migrated"
    assert events[-1].metadata["replacement_memory_id"] == migrated.memory_id


def test_legacy_migration_is_idempotent(memory_db):
    episode = seed_pending_episode(memory_db, episode_id="episode:legacy")
    legacy = _legacy_memory()
    assert save_memory_with_evidence(legacy, episode.episode_id, memory_db)

    async def llm_call(*_args, **_kwargs):
        return _outcome(valid_candidate_payload())

    first = migrate_legacy_memories(limit=10, owner="worker", llm_call=llm_call, path=memory_db)
    second = migrate_legacy_memories(limit=10, owner="worker", llm_call=llm_call, path=memory_db)

    assert first.migrated == 1
    assert second.selected == 0
    assert len(list_memories(pattern_key=legacy.pattern_key, path=memory_db)) == 2


def test_legacy_memory_without_evidence_is_marked_for_review(memory_db):
    legacy = _legacy_memory()
    assert save_memory(legacy, memory_db)

    async def llm_call(*_args, **_kwargs):
        pytest.fail("legacy memory without evidence must not be translated")

    summary = migrate_legacy_memories(
        limit=10,
        owner="worker",
        llm_call=llm_call,
        path=memory_db,
    )

    stored = load_memory(legacy.memory_id, memory_db)
    assert summary.marked_for_review == 1
    assert stored is not None and stored.status is MemoryStatus.NEEDS_REVIEW
    event = list_memory_events(legacy.memory_id, memory_db)[-1]
    assert event.event_type == "legacy_memory_needs_review"
    assert event.reason == "missing_supporting_episode"


def test_failed_legacy_migration_keeps_old_memory_active_and_records_attempt(memory_db):
    episode = seed_pending_episode(memory_db, episode_id="episode:legacy")
    legacy = _legacy_memory()
    assert save_memory_with_evidence(legacy, episode.episode_id, memory_db)

    async def llm_call(*_args, **_kwargs):
        raise TimeoutError("private provider detail")

    summary = migrate_legacy_memories(
        limit=10,
        owner="worker",
        llm_call=llm_call,
        path=memory_db,
    )

    assert summary.failed == 1
    assert load_memory(legacy.memory_id, memory_db).status is MemoryStatus.ACTIVE
    event = list_memory_events(legacy.memory_id, memory_db)[-1]
    assert event.event_type == "legacy_memory_migration_failed"
    assert event.reason == "TimeoutError"
    assert event.metadata["attempt"] == 1
    assert "private provider detail" not in json.dumps(event.to_dict())


def test_migrate_legacy_cli_uses_shared_model_client(memory_db, monkeypatch, capsys):
    episode = seed_pending_episode(memory_db, episode_id="episode:legacy")
    legacy = _legacy_memory()
    assert save_memory_with_evidence(legacy, episode.episode_id, memory_db)

    async def llm_call(*_args, **_kwargs):
        return _outcome(valid_candidate_payload())

    monkeypatch.setattr(cli_module, "call_tool_llm_outcome", llm_call)

    assert cli_main(["migrate-legacy", "--limit", "1"], path=memory_db) == 0
    assert "migrated=1" in capsys.readouterr().out
