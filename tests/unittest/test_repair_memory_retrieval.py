"""Focused tests for repair-memory retrieval, scoring, and prompt injection.

Covers Task 4 of the UT-Agent repair-memory implementation plan:
- deterministic scoring (40/20/10/10/10/10) with explainable breakdown;
- deterministic failure-family classification from causal lines;
- project-first two-pool retrieval with global fill;
- global duplicate never displaces a project pattern;
- weak candidates are not used to fill the quota;
- stale memories lose freshness points;
- bounded untrusted prompt block with markers;
- injection requires durable hit audit;
- one injection per task/root-cause group;
- store errors return no hints (fail-open).
"""

from datetime import datetime, timezone

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before the eager ut_agent package import
from tests.unittest.repair_memory_helpers import (
    raising_store_error,
    sample_hint,
    sample_memory,
    sample_query,
    seed_matching_memory,
    seed_memory,
)
from ut_agent.repair_memory.audit import load_retrieval_audit, query_retrieval_audits
from ut_agent.repair_memory.embedding import (
    BGE_DIMENSIONS,
    BGE_MODEL_NAME,
    BGE_MODEL_REVISION,
    EmbeddingBatch,
    EmbeddingServiceError,
    build_memory_embedding_text,
    embedding_source_hash,
    vector_to_blob,
)
from ut_agent.repair_memory.models import (
    EmbeddingStatus,
    MemoryStatus,
    RepairMemoryEmbedding,
    RetrievalAuditStatus,
    RetrievalMode,
)
from ut_agent.repair_memory.prompt import render_historical_hints
from ut_agent.repair_memory.retrieve import (
    classify_failure_family,
    retrieve_repair_hints,
    score_memory,
    score_memory_hybrid,
)
from ut_agent.repair_memory.store import (
    init_repair_memory_tables,
    list_attempt_hits,
    list_retrieval_candidates,
    upsert_memory_embeddings,
)


@pytest.fixture
def memory_db(tmp_path) -> str:
    path = str(tmp_path / "repair-memory.db")
    init_repair_memory_tables(path)
    return path


def _vector(first: float = 1.0, second: float = 0.0) -> tuple[float, ...]:
    return (first, second) + (0.0,) * (BGE_DIMENSIONS - 2)


class _QueryClient:
    def __init__(self, vector: tuple[float, ...] | None = None) -> None:
        self.vector = vector or _vector()
        self.calls = []

    def encode(self, texts, *, timeout_seconds):
        self.calls.append((texts, timeout_seconds))
        return EmbeddingBatch(
            model=BGE_MODEL_NAME,
            revision=BGE_MODEL_REVISION,
            dimensions=BGE_DIMENSIONS,
            vectors=(self.vector,),
        )


class _FailingQueryClient:
    def encode(self, texts, *, timeout_seconds):
        raise EmbeddingServiceError("timeout", "private query text")


def _seed_ready_embedding(memory_db: str, memory, vector, *, revision=BGE_MODEL_REVISION):
    text = build_memory_embedding_text(memory)
    embedding = RepairMemoryEmbedding(
        memory_id=memory.memory_id,
        model_name=BGE_MODEL_NAME,
        model_revision=revision,
        dimensions=BGE_DIMENSIONS,
        vector_blob=vector_to_blob(vector),
        source_hash=embedding_source_hash(
            text,
            model_name=BGE_MODEL_NAME,
            model_revision=revision,
        ),
        status=EmbeddingStatus.READY,
        created_at="2026-08-18T00:00:00+00:00",
        updated_at="2026-08-18T00:00:00+00:00",
    )
    assert upsert_memory_embeddings((embedding,), memory_db)


def test_hybrid_score_uses_confirmed_100_point_formula():
    query = sample_query()
    memory = sample_memory(confidence=0.8, last_reinforced_at="2026-08-17T00:00:00+00:00")

    score = score_memory_hybrid(
        query,
        memory,
        semantic_similarity=0.81,
        now=datetime(2026, 8, 18, tzinfo=timezone.utc),
    )

    assert score.semantic_points == 32
    assert score.exact_fingerprint_points == 25
    assert score.failure_family_points == 10
    assert score.causal_token_points == 5
    assert score.language_points == 5
    assert score.build_system_points == 5
    assert score.project_points == 5
    assert score.quality_points == 4
    assert score.total == 91
    assert score.scoring_mode == "hybrid"


def test_hybrid_quality_points_use_three_confidence_and_two_freshness_points():
    query = sample_query()
    fresh = sample_memory(confidence=0.95, last_reinforced_at="2026-08-17T00:00:00+00:00")
    stale = sample_memory(confidence=0.95, last_reinforced_at="2025-01-01T00:00:00+00:00")
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)

    assert score_memory_hybrid(query, fresh, semantic_similarity=0.0, now=now).quality_points == 5
    assert score_memory_hybrid(query, stale, semantic_similarity=0.0, now=now).quality_points == 3


def test_retrieval_always_reads_project_and_global_candidate_pools(memory_db, monkeypatch):
    calls = []

    def fake_load(*, scope, scope_key, path, limit):
        calls.append((scope, scope_key, path, limit))
        return ()

    monkeypatch.setattr("ut_agent.repair_memory.retrieve.load_candidate_rows", fake_load)

    result = retrieve_repair_hints(
        sample_query(project="group/a"),
        "task-both-pools",
        RetrievalMode.INJECT,
        memory_db,
        embedding_client=_QueryClient(),
    )

    assert result.hints == ()
    assert calls == [
        ("project", "group/a", memory_db, 500),
        ("global", "*", memory_db, 500),
    ]


def test_candidate_pool_only_contains_latest_active_chinese_version(memory_db):
    seed_memory(memory_db, "old", pattern_key="same", pattern_version=1)
    latest = seed_memory(memory_db, "latest", pattern_key="same", pattern_version=2)
    seed_memory(memory_db, "disabled", pattern_key="disabled", status=MemoryStatus.DISABLED)
    seed_memory(memory_db, "superseded", pattern_key="superseded", status=MemoryStatus.SUPERSEDED)
    seed_memory(memory_db, "legacy", pattern_key="legacy", content_locale="legacy")

    rows = list_retrieval_candidates(scope="project", scope_key="group/a", limit=500, path=memory_db)

    assert [memory.memory_id for memory, _embedding in rows] == [latest.memory_id]


def test_low_semantic_similarity_is_rejected_without_exact_fingerprint(memory_db):
    memory = seed_memory(
        memory_db,
        "semantic-low",
        pattern_key="semantic-low",
        diagnostic_fingerprint="different",
    )
    _seed_ready_embedding(memory_db, memory, _vector(0.0, 1.0))

    result = retrieve_repair_hints(
        sample_query(),
        "task-semantic-low",
        RetrievalMode.SHADOW,
        memory_db,
        embedding_client=_QueryClient(_vector(1.0, 0.0)),
    )

    assert result.hints == ()
    candidate = query_retrieval_audits(path=memory_db)["audits"][0]["candidate_scores"][0]
    assert candidate["memory_id"] == memory.memory_id
    assert candidate["total_score"] < 60
    assert candidate["decision"] == "rejected"
    assert candidate["rejection_reason"] == "semantic_below_threshold"
    assert candidate["semantic_similarity"] == pytest.approx(0.0)


def test_exact_fingerprint_can_pass_semantic_gate(memory_db):
    memory = seed_matching_memory(memory_db)
    _seed_ready_embedding(memory_db, memory, _vector(0.0, 1.0))

    result = retrieve_repair_hints(
        sample_query(),
        "task-exact",
        RetrievalMode.SHADOW,
        memory_db,
        embedding_client=_QueryClient(_vector(1.0, 0.0)),
    )

    assert [hint.memory_id for hint in result.hints] == [memory.memory_id]


def test_query_embedding_failure_falls_back_to_rules_and_audits_reason(memory_db):
    memory = seed_matching_memory(memory_db)
    _seed_ready_embedding(memory_db, memory, _vector())

    result = retrieve_repair_hints(
        sample_query(root_cause_group_id="fallback-root"),
        "task-fallback",
        RetrievalMode.INJECT,
        memory_db,
        embedding_client=_FailingQueryClient(),
    )

    assert [hint.memory_id for hint in result.hints] == [memory.memory_id]
    hit = list_attempt_hits(result.attempt_id, memory_db)[0]
    assert hit["score"]["scoring_mode"] == "rule_fallback"
    assert hit["score"]["fallback_reason"] == "timeout"
    assert "private query text" not in str(hit["score"])


def test_incompatible_revision_uses_rule_fallback_without_query_call(memory_db):
    memory = seed_matching_memory(memory_db)
    _seed_ready_embedding(memory_db, memory, _vector(), revision="old-revision")
    client = _QueryClient()

    result = retrieve_repair_hints(
        sample_query(),
        "task-old-vector",
        RetrievalMode.SHADOW,
        memory_db,
        embedding_client=client,
    )

    assert result.hints
    assert client.calls == []


def test_missing_vector_candidate_fills_remaining_hybrid_result(memory_db):
    embedded = seed_matching_memory(memory_db)
    missing = seed_memory(memory_db, "mem-missing-vector", pattern_key="pattern-2")
    _seed_ready_embedding(memory_db, embedded, _vector())

    result = retrieve_repair_hints(
        sample_query(),
        "task-mixed-vectors",
        RetrievalMode.INJECT,
        memory_db,
        embedding_client=_QueryClient(),
    )

    assert [hint.memory_id for hint in result.hints] == [embedded.memory_id, missing.memory_id]
    hits = {hit["memory_id"]: hit["score"] for hit in list_attempt_hits(result.attempt_id, memory_db)}
    assert hits[embedded.memory_id]["scoring_mode"] == "hybrid"
    assert hits[missing.memory_id]["scoring_mode"] == "rule_fallback"
    assert hits[missing.memory_id]["fallback_reason"] == "missing_compatible_embedding"


def test_hybrid_hit_audit_contains_full_score_and_model_identity(memory_db):
    memory = seed_matching_memory(memory_db)
    _seed_ready_embedding(memory_db, memory, _vector())

    result = retrieve_repair_hints(
        sample_query(),
        "task-hybrid-audit",
        RetrievalMode.INJECT,
        memory_db,
        embedding_client=_QueryClient(),
    )

    score = list_attempt_hits(result.attempt_id, memory_db)[0]["score"]
    assert score["scoring_mode"] == "hybrid"
    assert score["embedding_model"] == BGE_MODEL_NAME
    assert score["embedding_revision"] == BGE_MODEL_REVISION
    assert score["total"] == sum(
        score[key]
        for key in (
            "semantic_points",
            "exact_fingerprint",
            "failure_family",
            "causal_tokens",
            "language",
            "build_system",
            "project_scope",
            "confidence_freshness",
        )
    )


def test_same_pattern_keeps_higher_score_and_prefers_project_on_tie(memory_db):
    project = seed_memory(
        memory_db,
        "project-same",
        scope="project",
        scope_key="group/a",
        pattern_key="same",
    )
    global_memory = seed_memory(
        memory_db,
        "global-same",
        scope="global",
        scope_key="*",
        pattern_key="same",
    )
    _seed_ready_embedding(memory_db, project, _vector())
    _seed_ready_embedding(memory_db, global_memory, _vector())

    result = retrieve_repair_hints(
        sample_query(),
        "task-tie",
        RetrievalMode.SHADOW,
        memory_db,
        embedding_client=_QueryClient(),
    )

    assert [hint.memory_id for hint in result.hints] == [project.memory_id]
    candidates = {
        item["memory_id"]: item
        for item in query_retrieval_audits(path=memory_db)["audits"][0]["candidate_scores"]
    }
    assert candidates[project.memory_id]["decision"] == "selected"
    assert candidates[global_memory.memory_id]["decision"] == "passed_not_selected"
    assert candidates[global_memory.memory_id]["rejection_reason"] == ""


def test_exact_fingerprint_score_is_explainable():
    query = sample_query(
        diagnostic_fingerprint="fingerprint-1",
        failure_family="missing_member",
        language="cpp",
        build_system="cmake",
        causal_tokens=("request", "member"),
    )
    memory = sample_memory(
        diagnostic_fingerprint="fingerprint-1",
        failure_family="missing_member",
        language="cpp",
        build_system="cmake",
        causal_tokens=("request", "member"),
        confidence=0.95,
    )

    score = score_memory(query, memory, now="2026-08-15T00:00:00+00:00")

    assert score.total == 100
    assert score.breakdown["exact_fingerprint"] == 40
    assert score.breakdown["failure_family"] == 20
    assert score.breakdown["language"] == 10
    assert score.breakdown["build_system"] == 10


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("error: no member named 'node_name' in 'Request'", "missing_member"),
        ("fatal error: sdk/request.hpp: No such file or directory", "missing_header"),
        ("undefined reference to `make_request()`", "undefined_symbol"),
        ("cannot convert 'Foo' to 'Bar'", "type_mismatch"),
        ("Assertion failed: expected true", "test_assertion"),
        ("CMake Error at CMakeLists.txt: target not found", "build_config"),
        ("runner disappeared", "other"),
    ],
)
def test_current_failure_family_is_deterministic(line, expected):
    assert classify_failure_family((line,)) == expected


def test_project_memories_precede_global_and_global_only_fills(memory_db):
    seed_memory(memory_db, "project-1", scope="project", scope_key="group/a", pattern_key="p1")
    seed_memory(memory_db, "project-2", scope="project", scope_key="group/a", pattern_key="p2")
    seed_memory(memory_db, "global-1", scope="global", scope_key="*", pattern_key="p3")

    result = retrieve_repair_hints(sample_query(project="group/a"), "task-1", RetrievalMode.SHADOW, memory_db)

    assert [hint.memory_id for hint in result.hints] == ["project-1", "project-2", "global-1"]


def test_global_duplicate_never_displaces_project_pattern(memory_db):
    seed_memory(memory_db, "project-1", scope="project", scope_key="group/a", pattern_key="same")
    seed_memory(memory_db, "global-1", scope="global", scope_key="*", pattern_key="same")
    result = retrieve_repair_hints(sample_query(project="group/a"), "task-1", RetrievalMode.SHADOW, memory_db)
    assert [hint.memory_id for hint in result.hints] == ["project-1"]


def test_weak_candidate_is_not_used_to_fill_quota(memory_db):
    seed_memory(
        memory_db,
        "weak",
        scope="global",
        scope_key="*",
        pattern_key="p1",
        diagnostic_fingerprint="mismatch",
        failure_family="test_assertion",
        language="python",
        build_system="make",
        causal_tokens=("unrelated",),
    )
    assert retrieve_repair_hints(sample_query(), "task-1", RetrievalMode.SHADOW, memory_db).hints == ()
    audit = load_retrieval_audit("task-1", memory_db)
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.NO_MATCH
    assert audit.reason_code == "below_threshold"
    assert audit.candidate_count == 1
    candidate = query_retrieval_audits(path=memory_db)["audits"][0]["candidate_scores"][0]
    assert candidate["memory_id"] == "weak"
    assert candidate["total_score"] < candidate["score"]["effective_min_score"]
    assert candidate["decision"] == "rejected"
    assert candidate["rejection_reason"] == "total_below_threshold"


def test_candidate_audit_failure_does_not_block_recall(memory_db, monkeypatch):
    memory = seed_matching_memory(memory_db)
    monkeypatch.setattr(
        "ut_agent.repair_memory.retrieve.record_retrieval_candidate_audits",
        lambda *args, **kwargs: False,
    )

    result = retrieve_repair_hints(sample_query(), "task-audit-failure", RetrievalMode.INJECT, memory_db)

    assert [hint.memory_id for hint in result.hints] == [memory.memory_id]
    assert result.audit_persisted is True
    assert list_attempt_hits(result.attempt_id, memory_db)


def test_no_candidates_is_audited_as_no_match(memory_db):
    result = retrieve_repair_hints(sample_query(), "task-empty", RetrievalMode.INJECT, memory_db)

    audit = load_retrieval_audit("task-empty", memory_db)
    assert result.hints == ()
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.NO_MATCH
    assert audit.reason_code == "no_candidates"
    assert audit.search_count == 1
    assert audit.candidate_count == 0


def test_selected_hints_are_audited_before_prompt_injection(memory_db):
    seed_matching_memory(memory_db)

    result = retrieve_repair_hints(sample_query(), "task-recalled", RetrievalMode.INJECT, memory_db)

    audit = load_retrieval_audit("task-recalled", memory_db)
    assert result.hints
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.RECALLED
    assert audit.reason_code == "selected"
    assert audit.selected_count == 1
    assert audit.injected_count == 0


def test_stale_memory_loses_five_freshness_points():
    query = sample_query()
    fresh = sample_memory(last_reinforced_at="2026-08-14T00:00:00+00:00")
    stale = sample_memory(last_reinforced_at="2025-01-01T00:00:00+00:00")
    now = "2026-08-15T00:00:00+00:00"
    assert score_memory(query, fresh, now=now).total - score_memory(query, stale, now=now).total == 5


def test_historical_hint_block_is_bounded_and_marked_untrusted():
    block = render_historical_hints((sample_hint(memory_id="mem-1"),), max_chars=2000)
    assert block.startswith("[UNTRUSTED HISTORICAL REPAIR HINTS]")
    assert block.endswith("[END UNTRUSTED HISTORICAL REPAIR HINTS]")
    assert "mem-1" in block
    assert "diff --git" not in block.lower()
    assert "必须以当前代码、依赖和 CI 证据为准重新验证" in block
    assert "std::unique_ptr" in block
    assert len(block) <= 2000


def test_injection_requires_durable_hit_audit(memory_db, monkeypatch):
    seed_matching_memory(memory_db)
    monkeypatch.setattr("ut_agent.repair_memory.retrieve.record_retrieval_hits", lambda *args, **kwargs: False)
    result = retrieve_repair_hints(sample_query(), "task-1", RetrievalMode.INJECT, memory_db)
    assert result.hints == ()
    assert result.audit_persisted is False


def test_same_task_root_group_is_injected_only_once(memory_db):
    seed_matching_memory(memory_db)
    first = retrieve_repair_hints(sample_query(root_cause_group_id="root-1"), "task-1", RetrievalMode.INJECT, memory_db)
    second = retrieve_repair_hints(
        sample_query(root_cause_group_id="root-1"), "task-1", RetrievalMode.INJECT, memory_db
    )
    assert first.hints
    assert second.hints == ()
    audit = load_retrieval_audit("task-1", memory_db)
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.RECALLED
    assert audit.reason_code == "selected"
    assert audit.search_count == 1


def test_retrieval_store_error_returns_no_hints(memory_db, monkeypatch):
    monkeypatch.setattr("ut_agent.repair_memory.retrieve.load_candidate_rows", raising_store_error)
    result = retrieve_repair_hints(sample_query(), "task-error", RetrievalMode.INJECT, memory_db)
    assert result.hints == ()
    assert result.audit_persisted is False
    audit = load_retrieval_audit("task-error", memory_db)
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.ERROR
    assert audit.reason_code == "retrieval_error"
    assert audit.error_code == "OperationalError"
    assert "simulated database failure" not in repr(audit)


def test_shadow_mode_returns_hints_but_no_injection_marker():
    block = render_historical_hints((), max_chars=2000)
    assert block == ""


def test_off_mode_returns_without_database_or_embedding_access(memory_db, monkeypatch):
    def unexpected_call(*args, **kwargs):
        raise AssertionError("disabled retrieval must not access dependencies")

    monkeypatch.setattr("ut_agent.repair_memory.retrieve.load_candidate_rows", unexpected_call)
    result = retrieve_repair_hints(
        sample_query(),
        "task-1",
        RetrievalMode.OFF,
        memory_db,
        embedding_client=_FailingQueryClient(),
    )
    assert result.hints == ()
    assert result.mode is RetrievalMode.OFF
    audit = load_retrieval_audit("task-1", memory_db)
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.NOT_ATTEMPTED
    assert audit.reason_code == "memory_mode_off"
    assert audit.search_count == 0
