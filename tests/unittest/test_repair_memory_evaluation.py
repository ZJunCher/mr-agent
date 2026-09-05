"""Offline evaluation and real-service smoke helpers for repair-memory retrieval."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.repair_memory_embedding_smoke import run_embedding_smoke
from tests.unittest.repair_memory_helpers import sample_hint
from ut_agent.repair_memory.embedding import BGE_DIMENSIONS, BGE_MODEL_NAME, BGE_MODEL_REVISION, EmbeddingBatch
from ut_agent.repair_memory.evaluation import EvaluationCase, evaluate_retrieval_cases, load_evaluation_cases
from ut_agent.repair_memory.models import RepairMemoryHint, RepairQuery

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "repair_memory_semantic_cases.json"


class _ControlledClock:
    def __init__(self, step_seconds: float = 0.001) -> None:
        self._value = 0.0
        self._step_seconds = step_seconds

    def __call__(self) -> float:
        value = self._value
        self._value += self._step_seconds
        return value


class _FakeSmokeClient:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def encode(self, texts: tuple[str, ...], *, timeout_seconds: float) -> EmbeddingBatch:
        assert timeout_seconds > 0
        self.batch_sizes.append(len(texts))
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            prefix = (0.0, 1.0) if "undefined reference" in text else (1.0, 0.0)
            vectors.append(prefix + (0.0,) * (BGE_DIMENSIONS - len(prefix)))
        return EmbeddingBatch(
            model=BGE_MODEL_NAME,
            revision=BGE_MODEL_REVISION,
            dimensions=BGE_DIMENSIONS,
            vectors=tuple(vectors),
        )


def _hint(memory_id: str) -> RepairMemoryHint:
    return sample_hint(memory_id=memory_id)


def _successful_fake_retriever(query: RepairQuery) -> tuple[RepairMemoryHint, ...]:
    expected = {
        "exact-cross-project": ("memory:global:missing-header",),
        "semantic-unique-ptr": ("memory:global:memory-header",),
        "semantic-keyword-distractor": ("memory:global:link-library",),
        "project-and-global": ("memory:project:ready-state", "memory:global:ready-state"),
        "semantic-mixed-chinese-english": ("memory:global:optional-value",),
        "disabled-memory-is-excluded": (),
    }
    return tuple(_hint(memory_id) for memory_id in expected[query.root_cause_group_id])


def test_sanitized_fixture_covers_required_retrieval_cases() -> None:
    cases = load_evaluation_cases(_FIXTURE)
    assert {case.kind for case in cases} == {"exact", "semantic", "coverage", "negative"}
    assert len(cases) == 6
    fixture_text = _FIXTURE.read_text(encoding="utf-8").casefold()
    for private_value in ("gitlab.eabot", "eabot/", "http://", "https://", "x-mcp-token"):
        assert private_value not in fixture_text


def test_evaluation_meets_offline_quality_gates_with_controlled_clock() -> None:
    result = evaluate_retrieval_cases(
        load_evaluation_cases(_FIXTURE),
        retrieve=_successful_fake_retriever,
        clock=_ControlledClock(),
    )
    assert result.exact_recall_at_3 == 1.0
    assert result.semantic_recall_at_3 >= 0.85
    assert result.irrelevant_top3_rate <= 0.05
    assert result.chinese_experience_rate == 1.0
    assert result.latency_p95_ms == 1.0


def test_evaluation_counts_forbidden_and_unrelated_top3_results() -> None:
    case = EvaluationCase(
        case_id="negative",
        kind="negative",
        query=load_evaluation_cases(_FIXTURE)[-1].query,
        relevant_memory_ids=(),
        forbidden_memory_ids=("memory:disabled:stale-api",),
    )
    result = evaluate_retrieval_cases(
        (case,),
        retrieve=lambda _query: (_hint("memory:disabled:stale-api"),),
        clock=_ControlledClock(),
    )
    assert result.irrelevant_top3_rate == 1.0


def test_fixture_loader_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    fixture = tmp_path / "duplicate.json"
    original = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    fixture.write_text(json.dumps([*original, *original]), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_evaluation_cases(fixture)


def test_embedding_smoke_checks_batch_semantics_and_controlled_p95() -> None:
    client = _FakeSmokeClient()
    result = run_embedding_smoke(
        client,
        attempts=5,
        timeout_seconds=1.0,
        p95_limit_ms=1500.0,
        clock=_ControlledClock(step_seconds=0.01),
    )
    assert client.batch_sizes[:3] == [1, 16, 3]
    assert client.batch_sizes[3:] == [1] * 5
    assert result.synonym_similarity == 1.0
    assert result.distractor_similarity == 0.0
    assert result.latency_p95_ms == 10.0
