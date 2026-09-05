"""Focused tests for repair-memory operator CLI commands.

Covers Task 6 of the UT-Agent repair-memory implementation plan:
- disable and enable require a reason;
- supersede creates a new version and retains old audit;
- disabling project support revalidates global memory.
"""

import json

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before the eager ut_agent package import
import ut_agent.repair_memory.cli as cli_module
from tests.unittest.repair_memory_helpers import (
    sample_memory,
    seed_pending_episode,
    seed_promoted_global_memory,
    valid_candidate_payload,
)
from ut_agent.repair_memory.cli import cli_main
from ut_agent.repair_memory.config import parse_repair_memory_settings
from ut_agent.repair_memory.embedding import (
    BGE_DIMENSIONS,
    BGE_MODEL_NAME,
    BGE_MODEL_REVISION,
    build_memory_embedding_text,
    embedding_source_hash,
    vector_to_blob,
)
from ut_agent.repair_memory.models import EmbeddingStatus, MemoryStatus, RepairMemoryEmbedding
from ut_agent.repair_memory.store import (
    init_repair_memory_tables,
    list_memories,
    list_memory_events,
    load_episode,
    load_memory,
    load_memory_embedding,
    save_memory,
    update_memory_status,
    upsert_memory_embeddings,
)


@pytest.fixture
def memory_db(tmp_path) -> str:
    path = str(tmp_path / "repair-memory.db")
    init_repair_memory_tables(path)
    return path


def test_embedding_configuration_rejects_invalid_bounds():
    with pytest.raises(ValueError, match="repair_memory.semantic_timeout_ms"):
        parse_repair_memory_settings({"semantic_timeout_ms": 0})
    with pytest.raises(ValueError, match="repair_memory.semantic_min_similarity"):
        parse_repair_memory_settings({"semantic_min_similarity": 1.1})


def _seed_memory(db_path: str, memory_id: str = "mem-1", **overrides) -> None:
    save_memory(sample_memory(memory_id=memory_id, **overrides), db_path)


def test_disable_and_enable_require_reason(memory_db):
    _seed_memory(memory_db, "mem-1")
    assert cli_main(["disable", "mem-1", "--reason", "bad match"], path=memory_db) == 0
    assert load_memory("mem-1", memory_db).status is MemoryStatus.DISABLED
    assert cli_main(["enable", "mem-1", "--reason", "corrected evidence"], path=memory_db) == 0
    assert load_memory("mem-1", memory_db).status is MemoryStatus.ACTIVE


def test_disable_without_reason_fails(memory_db):
    _seed_memory(memory_db, "mem-1")
    assert cli_main(["disable", "mem-1"], path=memory_db) != 0
    assert load_memory("mem-1", memory_db).status is MemoryStatus.ACTIVE


def test_dashboard_status_transition_is_atomic_audited_and_idempotent(memory_db):
    _seed_memory(memory_db, "mem-dashboard")
    expected = frozenset({MemoryStatus.ACTIVE, MemoryStatus.NEEDS_REVIEW})

    disabled = update_memory_status(
        "mem-dashboard",
        MemoryStatus.DISABLED,
        "  删除错误经验  ",
        source="dashboard",
        expected_statuses=expected,
        path=memory_db,
    )
    assert disabled is not None and disabled.status is MemoryStatus.DISABLED
    events = list_memory_events("mem-dashboard", memory_db)
    assert len(events) == 1
    assert events[0].reason == "删除错误经验"
    assert events[0].metadata == {
        "source": "dashboard",
        "previous_status": "active",
        "new_status": "disabled",
        "changed_at": events[0].created_at,
    }

    repeated = update_memory_status(
        "mem-dashboard",
        MemoryStatus.DISABLED,
        "重复删除",
        source="dashboard",
        expected_statuses=expected,
        path=memory_db,
    )
    assert repeated is not None and repeated.status is MemoryStatus.DISABLED
    assert len(list_memory_events("mem-dashboard", memory_db)) == 1


def test_dashboard_status_transition_rejects_missing_conflict_and_invalid_reason(memory_db):
    _seed_memory(memory_db, "mem-active")
    assert update_memory_status(
        "missing",
        MemoryStatus.DISABLED,
        "不存在",
        source="dashboard",
        expected_statuses=frozenset({MemoryStatus.ACTIVE}),
        path=memory_db,
    ) is None
    assert update_memory_status(
        "mem-active",
        MemoryStatus.ACTIVE,
        "x" * 501,
        source="dashboard",
        expected_statuses=frozenset({MemoryStatus.DISABLED}),
        path=memory_db,
    ) is None
    assert update_memory_status(
        "mem-active",
        MemoryStatus.DISABLED,
        "状态冲突",
        source="dashboard",
        expected_statuses=frozenset({MemoryStatus.NEEDS_REVIEW}),
        path=memory_db,
    ) is None
    assert load_memory("mem-active", memory_db).status is MemoryStatus.ACTIVE
    assert list_memory_events("mem-active", memory_db) == ()


def test_dashboard_disable_and_restore_preserve_compatible_embedding(memory_db):
    memory = sample_memory("mem-with-vector", pattern_key="with-vector")
    assert save_memory(memory, memory_db)
    text = build_memory_embedding_text(memory)
    embedding = RepairMemoryEmbedding(
        memory_id=memory.memory_id,
        model_name=BGE_MODEL_NAME,
        model_revision=BGE_MODEL_REVISION,
        dimensions=BGE_DIMENSIONS,
        vector_blob=vector_to_blob((1.0,) + (0.0,) * (BGE_DIMENSIONS - 1)),
        source_hash=embedding_source_hash(
            text,
            model_name=BGE_MODEL_NAME,
            model_revision=BGE_MODEL_REVISION,
        ),
        status=EmbeddingStatus.READY,
        created_at="2026-08-18T00:00:00+00:00",
        updated_at="2026-08-18T00:00:00+00:00",
    )
    assert upsert_memory_embeddings((embedding,), memory_db)

    assert update_memory_status(
        memory.memory_id,
        MemoryStatus.DISABLED,
        "人工删除",
        source="dashboard",
        expected_statuses=frozenset({MemoryStatus.ACTIVE}),
        path=memory_db,
    )
    assert update_memory_status(
        memory.memory_id,
        MemoryStatus.ACTIVE,
        "复核恢复",
        source="dashboard",
        expected_statuses=frozenset({MemoryStatus.DISABLED}),
        path=memory_db,
    )
    restored_embedding = load_memory_embedding(memory.memory_id, memory_db)
    assert restored_embedding is not None
    assert restored_embedding.status is EmbeddingStatus.READY
    assert restored_embedding.source_hash == embedding.source_hash
    assert restored_embedding.vector_blob == embedding.vector_blob


def test_supersede_creates_new_version_and_retains_old_audit(memory_db, tmp_path):
    _seed_memory(memory_db, "mem-1", pattern_version=1)
    correction = tmp_path / "correction.json"
    correction.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "language": "cpp",
                "build_system": "cmake",
                "failure_family": "missing_member",
                "root_cause_class": "interface_drift",
                "repair_action_class": "align_current_interface",
                "problem_pattern": "测试代码仍使用旧接口成员",
                "applicability": ["编译器报告当前类型缺少目标成员"],
                "anti_conditions": ["当前接口中仍存在该成员"],
                "repair_guidance": "按照当前接口调整测试代码",
                "validation_guidance": ["运行对应精确 SHA 的 Pipeline"],
            }
        )
    )

    code = cli_main(
        ["supersede", "mem-1", "--from-json", str(correction), "--reason", "old guidance was too broad"],
        path=memory_db,
    )

    assert code == 0
    old = load_memory("mem-1", memory_db)
    new = list_memories(pattern_key=old.pattern_key, status="active", path=memory_db)[0]
    assert old.status is MemoryStatus.SUPERSEDED
    assert new.pattern_version == 2
    assert new.supersedes_id == "mem-1"


def test_disabling_project_support_revalidates_global_memory(memory_db):
    global_memory = seed_promoted_global_memory(memory_db, projects=("group/a", "group/b"))
    from tests.unittest.repair_memory_helpers import project_memory_for

    project_memory = project_memory_for(memory_db, "group/b", global_memory.pattern_key)
    assert project_memory is not None
    assert cli_main(["disable", project_memory.memory_id, "--reason", "incorrect"], path=memory_db) == 0
    assert load_memory(global_memory.memory_id, memory_db).status is MemoryStatus.NEEDS_REVIEW


def test_list_returns_zero(memory_db):
    _seed_memory(memory_db, "mem-1")
    assert cli_main(["list", "--status", "active"], path=memory_db) == 0


def test_show_returns_zero(memory_db):
    _seed_memory(memory_db, "mem-1")
    assert cli_main(["show", "mem-1"], path=memory_db) == 0


def test_consolidate_uses_shared_model_client(memory_db, monkeypatch):
    episode = seed_pending_episode(memory_db, project="group/a")
    from ut_agent.model_failover import LLMCallOutcome

    outcome = LLMCallOutcome(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "memory-1",
                "type": "function",
                "function": {
                    "name": "submit_repair_memory",
                    "arguments": json.dumps(valid_candidate_payload(), ensure_ascii=False),
                },
            }],
        },
        "test-model",
        (),
    )

    async def fake_llm_call(*_args, **_kwargs):
        return outcome

    monkeypatch.setattr(cli_module, "call_tool_llm_outcome", fake_llm_call)

    assert cli_main(["consolidate", "--limit", "1"], path=memory_db) == 0
    assert load_episode(episode.episode_id, memory_db).consolidation_status == "complete"


def test_promote_uses_shared_model_client(memory_db, monkeypatch):
    observed = []

    async def fake_llm_call(*_args, **_kwargs):
        raise AssertionError("the publisher stub should receive but not invoke this callback")

    async def fake_promote(path, *, dry_run, llm_call):
        observed.append((path, dry_run, llm_call))
        return type("Summary", (), {"promoted": 0, "skipped": 0})()

    monkeypatch.setattr(cli_module, "call_tool_llm_outcome", fake_llm_call)
    monkeypatch.setattr(cli_module, "promote_ready_patterns", fake_promote)

    assert cli_main(["promote", "--dry-run"], path=memory_db) == 0
    assert observed == [(memory_db, True, fake_llm_call)]
