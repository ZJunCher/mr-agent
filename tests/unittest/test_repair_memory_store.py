"""Focused tests for the repair-memory store, configuration, and value objects.

Covers Task 1 of the UT-Agent repair-memory implementation plan:
- configuration defaults and validation;
- value-object enum stability;
- SQLite schema idempotency and table separation;
- episode/memory/evidence uniqueness and idempotent writes;
- operator audit events with a required non-empty reason.
"""

import sqlite3
import tomllib
from pathlib import Path

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before the eager ut_agent package import
from tests.unittest.repair_memory_helpers import (
    count_rows,
    sample_episode,
    sample_memory,
)
from ut_agent.repair_memory.config import parse_repair_memory_settings, project_allowed
from ut_agent.repair_memory.models import (
    MemoryScope,
    MemoryStatus,
    RetrievalMode,
)
from ut_agent.repair_memory.store import (
    init_repair_memory_tables,
    list_memories,
    list_memory_events,
    load_episode,
    load_memory,
    save_episode,
    save_memory,
    save_memory_with_evidence,
    update_memory_status,
)


def test_repair_memory_defaults_are_disabled():
    settings = parse_repair_memory_settings({})
    assert settings.capture_enabled is False
    assert settings.retrieval_mode is RetrievalMode.OFF
    assert settings.promotion_enabled is False
    assert settings.max_hints == 3
    assert settings.max_prompt_chars == 2000
    assert settings.global_min_projects == 2
    assert settings.consolidation_poll_seconds == 60
    assert settings.embedding_service_url == "http://bge-m3-service:8080"
    assert settings.embedding_model_name == "BAAI/bge-m3"
    assert settings.embedding_model_revision == "5617a9f61b028005a4858fdac845db406aefb181"
    assert settings.embedding_dimensions == 1024
    assert settings.semantic_timeout_ms == 1500
    assert settings.embedding_batch_timeout_seconds == 30.0
    assert settings.embedding_batch_size == 16
    assert settings.semantic_min_similarity == 0.55
    assert settings.semantic_candidate_limit_per_scope == 500


def test_repair_memory_rejects_invalid_bounds():
    with pytest.raises(ValueError, match="repair_memory.max_hints"):
        parse_repair_memory_settings({"max_hints": 0})


def test_repair_memory_rejects_invalid_poll_interval():
    with pytest.raises(ValueError, match="repair_memory.consolidation_poll_seconds"):
        parse_repair_memory_settings({"consolidation_poll_seconds": 0})


def test_production_configuration_enables_repair_memory_for_all_projects():
    config_path = Path(__file__).parents[2] / "pr_agent" / "settings" / "configuration.toml"
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)

    assert config["repair_report"]["enabled"] is True
    assert config["repair_memory"]["capture_enabled"] is True
    assert config["repair_memory"]["retrieval_mode"] == "inject"
    assert config["repair_memory"]["promotion_enabled"] is True
    assert config["repair_memory"]["project_allowlist"] == ["*"]
    assert config["repair_memory"]["consolidation_poll_seconds"] == 60
    assert config["repair_memory"]["embedding_service_url"] == "http://bge-m3-service:8080"
    assert config["repair_memory"]["embedding_model_name"] == "BAAI/bge-m3"
    assert config["repair_memory"]["embedding_dimensions"] == 1024
    assert config["repair_memory"]["semantic_timeout_ms"] == 1500
    assert config["repair_memory"]["embedding_batch_size"] == 16
    assert config["repair_memory"]["semantic_candidate_limit_per_scope"] == 500


def test_memory_enums_are_stable():
    assert MemoryScope.PROJECT.value == "project"
    assert MemoryScope.GLOBAL.value == "global"
    assert MemoryStatus.NEEDS_REVIEW.value == "needs_review"


def test_empty_allowlist_enables_nothing_and_star_is_explicit():
    assert project_allowed("group/a", ()) is False
    assert project_allowed("group/a", ("*",)) is True
    assert project_allowed("group/a", ("group/b",)) is False


def test_memory_schema_is_idempotent_and_separates_run_data(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_repair_memory_tables(db_path)
    init_repair_memory_tables(db_path)

    conn = sqlite3.connect(db_path)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()

    assert {
        "repair_memory_episodes",
        "repair_memories",
        "repair_memory_evidence",
        "repair_memory_hits",
        "repair_memory_events",
        "repair_memory_embeddings",
    } <= names
    assert "triage_runs" not in names


def test_schema_migrates_legacy_memories_and_adds_embedding_storage(tmp_path):
    db_path = str(tmp_path / "memory.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE repair_memories (
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
            supersedes_id TEXT,
            manual_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_reinforced_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    init_repair_memory_tables(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    memory_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(repair_memories)").fetchall()
    }
    embedding_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(repair_memory_embeddings)").fetchall()
    }
    conn.close()

    assert "content_locale" in memory_columns
    assert embedding_columns >= {
        "memory_id",
        "model_name",
        "model_revision",
        "dimensions",
        "vector_blob",
        "source_hash",
        "status",
        "last_error_code",
        "attempt_count",
        "next_retry_at",
        "created_at",
        "updated_at",
    }


def test_episode_identity_and_evidence_links_are_idempotent(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_repair_memory_tables(db_path)
    episode = sample_episode(task_id="task-1", project="group/a", action_identity="root-1")

    assert save_episode(episode, db_path) is True
    assert save_episode(episode, db_path) is True
    assert count_rows(db_path, "repair_memory_episodes") == 1

    memory = sample_memory(memory_id="mem-1", scope=MemoryScope.PROJECT, scope_key="group/a")
    assert save_memory_with_evidence(memory, episode.episode_id, db_path) is True
    assert save_memory_with_evidence(memory, episode.episode_id, db_path) is True
    assert count_rows(db_path, "repair_memory_evidence") == 1


def test_operator_event_requires_a_reason(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_repair_memory_tables(db_path)
    memory = sample_memory(memory_id="mem-1", scope=MemoryScope.PROJECT, scope_key="group/a")
    save_memory(memory, db_path)

    assert update_memory_status("mem-1", MemoryStatus.DISABLED, "", path=db_path) is None
    updated = update_memory_status("mem-1", MemoryStatus.DISABLED, "incorrect guidance", path=db_path)
    assert updated is not None and updated.status is MemoryStatus.DISABLED
    assert load_memory("mem-1", db_path).status is MemoryStatus.DISABLED
    assert list_memory_events("mem-1", db_path)[-1].reason == "incorrect guidance"


def test_load_missing_episode_and_memory_return_none(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_repair_memory_tables(db_path)
    assert load_episode("missing", db_path) is None
    assert load_memory("missing", db_path) is None


def test_list_memories_filters_by_scope_and_status(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_repair_memory_tables(db_path)
    save_memory(sample_memory(memory_id="mem-1", scope=MemoryScope.PROJECT, scope_key="group/a"), db_path)
    save_memory(sample_memory(memory_id="mem-2", scope=MemoryScope.GLOBAL, scope_key="*"), db_path)
    save_memory(
        sample_memory(
            memory_id="mem-3",
            scope=MemoryScope.PROJECT,
            scope_key="group/a",
            pattern_key="pattern-2",
            status=MemoryStatus.DISABLED,
        ),
        db_path,
    )

    project_active = list_memories(scope="project", scope_key="group/a", status="active", path=db_path)
    assert [m.memory_id for m in project_active] == ["mem-1"]
    global_active = list_memories(scope="global", status="active", path=db_path)
    assert [m.memory_id for m in global_active] == ["mem-2"]
