"""Deterministic offline metrics for repair-memory retrieval."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Callable, Sequence

from ut_agent.repair_memory.models import RepairMemoryHint, RepairQuery

_CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CASE_KINDS = frozenset({"exact", "semantic", "coverage", "negative"})


@dataclass(frozen=True)
class EvaluationCase:
    """One sanitized query with explicit relevant and forbidden memories."""

    case_id: str
    kind: str
    query: RepairQuery
    relevant_memory_ids: tuple[str, ...]
    forbidden_memory_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalEvaluation:
    """Offline quality and controlled-latency metrics for Top-3 retrieval."""

    exact_recall_at_3: float
    semantic_recall_at_3: float
    irrelevant_top3_rate: float
    chinese_experience_rate: float
    latency_p95_ms: float


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    values = tuple(str(item).strip() for item in value)
    if any(not item for item in values):
        raise ValueError(f"{field_name} must not contain empty values")
    return values


def _parse_case(value: object) -> EvaluationCase:
    if not isinstance(value, dict):
        raise ValueError("evaluation case must be an object")
    case_id = str(value.get("case_id") or "").strip()
    kind = str(value.get("kind") or "").strip()
    query_payload = value.get("query")
    if not case_id or kind not in _CASE_KINDS or not isinstance(query_payload, dict):
        raise ValueError("evaluation case id, kind, or query is invalid")
    query = RepairQuery(
        project=str(query_payload.get("project") or "").strip(),
        root_cause_group_id=case_id,
        source_pipeline_id=int(query_payload.get("source_pipeline_id") or 0),
        source_sha=str(query_payload.get("source_sha") or "").strip(),
        failure_category=str(query_payload.get("failure_category") or "").strip(),
        job_family=str(query_payload.get("job_family") or "").strip(),
        failure_family=str(query_payload.get("failure_family") or "").strip(),
        language=str(query_payload.get("language") or "").strip(),
        build_system=str(query_payload.get("build_system") or "").strip(),
        diagnostic_fingerprint=str(query_payload.get("diagnostic_fingerprint") or "").strip(),
        causal_tokens=_string_tuple(query_payload.get("causal_tokens", []), "query.causal_tokens"),
    )
    return EvaluationCase(
        case_id=case_id,
        kind=kind,
        query=query,
        relevant_memory_ids=_string_tuple(value.get("relevant_memory_ids", []), "relevant_memory_ids"),
        forbidden_memory_ids=_string_tuple(value.get("forbidden_memory_ids", []), "forbidden_memory_ids"),
    )


def load_evaluation_cases(path: str | Path) -> tuple[EvaluationCase, ...]:
    """Load a bounded, sanitized JSON fixture."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not 1 <= len(payload) <= 500:
        raise ValueError("evaluation fixture must contain 1 to 500 cases")
    cases = tuple(_parse_case(item) for item in payload)
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("evaluation case IDs must be unique")
    return cases


def _has_chinese_experience(hint: RepairMemoryHint) -> bool:
    user_fields = " ".join(
        (
            hint.problem_pattern,
            *hint.applicability,
            *hint.anti_conditions,
            hint.repair_guidance,
            *hint.validation_guidance,
        )
    )
    return _CHINESE_RE.search(user_fields) is not None


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _p95_ms(samples_seconds: list[float]) -> float:
    if not samples_seconds:
        return 0.0
    ordered = sorted(max(0.0, value) for value in samples_seconds)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return round(ordered[index] * 1000.0, 3)


def evaluate_retrieval_cases(
    cases: Sequence[EvaluationCase],
    *,
    retrieve: Callable[[RepairQuery], tuple[RepairMemoryHint, ...]],
    clock: Callable[[], float] = perf_counter,
) -> RetrievalEvaluation:
    """Evaluate Top-3 results without reading production data or calling an LLM."""
    exact_hits = exact_total = semantic_hits = semantic_total = 0
    irrelevant = returned = chinese = 0
    latencies: list[float] = []
    for case in cases:
        started = clock()
        hints = tuple(retrieve(case.query))[:3]
        latencies.append(clock() - started)
        result_ids = {hint.memory_id for hint in hints}
        relevant = set(case.relevant_memory_ids)
        forbidden = set(case.forbidden_memory_ids)
        if case.kind == "exact":
            exact_total += 1
            exact_hits += int(bool(result_ids & relevant))
        elif case.kind == "semantic":
            semantic_total += 1
            semantic_hits += int(bool(result_ids & relevant))
        for hint in hints:
            returned += 1
            irrelevant += int(hint.memory_id not in relevant or hint.memory_id in forbidden)
            chinese += int(_has_chinese_experience(hint))
    return RetrievalEvaluation(
        exact_recall_at_3=_ratio(exact_hits, exact_total),
        semantic_recall_at_3=_ratio(semantic_hits, semantic_total),
        irrelevant_top3_rate=_ratio(irrelevant, returned),
        chinese_experience_rate=_ratio(chinese, returned),
        latency_p95_ms=_p95_ms(latencies),
    )
