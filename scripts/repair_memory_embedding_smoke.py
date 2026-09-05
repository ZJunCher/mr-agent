#!/usr/bin/env python3
"""Run a bounded smoke test against the internal repair-memory embedding service."""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from time import perf_counter
from typing import Callable, Protocol, Sequence

from ut_agent.repair_memory.embedding import (
    BGE_DIMENSIONS,
    BGE_MODEL_NAME,
    BGE_MODEL_REVISION,
    EmbeddingBatch,
    HttpEmbeddingClient,
    cosine_similarity,
)

_SYNONYM_EN = "error: 'unique_ptr' is not a member of 'std'; did you forget #include <memory>?"
_SYNONYM_ZH = "使用智能指针时编译器找不到 std::unique_ptr，请检查是否缺少 <memory> 头文件。"
_DISTRACTOR = "链接器报告 undefined reference to database_client::connect，需要检查链接库。"
_MAX_READY_RESPONSE_BYTES = 16 * 1024


class SmokeEmbeddingClient(Protocol):
    """Minimal client surface used by the smoke runner and its unit test."""

    def encode(self, texts: tuple[str, ...], *, timeout_seconds: float) -> EmbeddingBatch: ...


@dataclass(frozen=True)
class EmbeddingSmokeResult:
    """Validated semantic and latency measurements."""

    synonym_similarity: float
    distractor_similarity: float
    latency_p95_ms: float


def _assert_batch(batch: EmbeddingBatch, *, expected_count: int) -> None:
    if batch.model != BGE_MODEL_NAME:
        raise RuntimeError("embedding smoke model mismatch")
    if batch.revision != BGE_MODEL_REVISION:
        raise RuntimeError("embedding smoke revision mismatch")
    if batch.dimensions != BGE_DIMENSIONS:
        raise RuntimeError("embedding smoke dimension mismatch")
    if len(batch.vectors) != expected_count:
        raise RuntimeError("embedding smoke vector count mismatch")
    for vector in batch.vectors:
        if len(vector) != BGE_DIMENSIONS or any(not math.isfinite(value) for value in vector):
            raise RuntimeError("embedding smoke returned an invalid vector")


def _p95_ms(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(max(0.0, value) for value in values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index] * 1000.0


def check_readiness(url: str, *, timeout_seconds: float) -> None:
    """Validate readiness metadata without printing the target URL or response body."""
    request = urllib.request.Request(
        f"{url.rstrip('/')}/health/ready",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(_MAX_READY_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise RuntimeError("embedding smoke readiness request failed") from error
    if len(body) > _MAX_READY_RESPONSE_BYTES:
        raise RuntimeError("embedding smoke readiness response is too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("embedding smoke readiness response is invalid") from error
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        raise RuntimeError("embedding smoke service is not ready")
    if payload.get("model") != BGE_MODEL_NAME or payload.get("revision") != BGE_MODEL_REVISION:
        raise RuntimeError("embedding smoke readiness identity mismatch")
    if payload.get("dimensions") != BGE_DIMENSIONS:
        raise RuntimeError("embedding smoke readiness dimension mismatch")


def run_embedding_smoke(
    client: SmokeEmbeddingClient,
    *,
    attempts: int = 10,
    timeout_seconds: float = 5.0,
    p95_limit_ms: float = 1500.0,
    clock: Callable[[], float] = perf_counter,
) -> EmbeddingSmokeResult:
    """Check shape, semantic ordering, batching, and warmed single-query P95."""
    if attempts < 1 or timeout_seconds <= 0 or p95_limit_ms <= 0:
        raise ValueError("smoke attempts, timeout, and P95 limit must be positive")

    single = client.encode((_SYNONYM_EN,), timeout_seconds=timeout_seconds)
    _assert_batch(single, expected_count=1)
    batch_texts = tuple(f"脱敏批量样本 {index}：检查编译错误和对应修复。" for index in range(16))
    batch = client.encode(batch_texts, timeout_seconds=timeout_seconds)
    _assert_batch(batch, expected_count=16)

    semantic = client.encode((_SYNONYM_EN, _SYNONYM_ZH, _DISTRACTOR), timeout_seconds=timeout_seconds)
    _assert_batch(semantic, expected_count=3)
    synonym_similarity = cosine_similarity(semantic.vectors[0], semantic.vectors[1])
    distractor_similarity = cosine_similarity(semantic.vectors[0], semantic.vectors[2])
    if synonym_similarity <= distractor_similarity:
        raise RuntimeError("embedding smoke semantic ordering failed")

    latencies: list[float] = []
    for _ in range(attempts):
        started = clock()
        measured = client.encode((_SYNONYM_ZH,), timeout_seconds=timeout_seconds)
        latencies.append(clock() - started)
        _assert_batch(measured, expected_count=1)
    latency_p95_ms = _p95_ms(latencies)
    if latency_p95_ms > p95_limit_ms:
        raise RuntimeError("embedding smoke P95 exceeded the configured limit")
    return EmbeddingSmokeResult(
        synonym_similarity=round(synonym_similarity, 6),
        distractor_similarity=round(distractor_similarity, 6),
        latency_p95_ms=round(latency_p95_ms, 3),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke-test the internal BGE-M3 embedding service")
    parser.add_argument("--url", required=True, help="temporary local or forwarded service URL")
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--p95-limit-ms", type=float, default=1500.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        check_readiness(args.url, timeout_seconds=args.timeout_seconds)
        result = run_embedding_smoke(
            HttpEmbeddingClient(args.url),
            attempts=args.attempts,
            timeout_seconds=args.timeout_seconds,
            p95_limit_ms=args.p95_limit_ms,
        )
    except (RuntimeError, ValueError) as error:
        print(f"FAIL: {type(error).__name__}", file=sys.stderr)
        return 1
    print(
        "PASS: "
        f"model={BGE_MODEL_NAME} revision={BGE_MODEL_REVISION} dimensions={BGE_DIMENSIONS} "
        f"synonym={result.synonym_similarity:.6f} distractor={result.distractor_similarity:.6f} "
        f"p95_ms={result.latency_p95_ms:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
