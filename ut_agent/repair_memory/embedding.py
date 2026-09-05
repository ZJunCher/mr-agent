"""Safe text construction and transport for repair-memory embeddings."""

from __future__ import annotations

import hashlib
import json
import math
import socket
import struct
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol, Sequence
from urllib.parse import urlparse

from ut_agent.repair_memory.models import RepairMemory, RepairQuery

BGE_MODEL_NAME = "BAAI/bge-m3"
BGE_MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
BGE_DIMENSIONS = 1024
EMBEDDING_TEXT_VERSION = 1
MAX_EMBEDDING_BATCH = 32
MAX_EMBEDDING_TEXT_CHARS = 4000
MAX_EMBEDDING_RESPONSE_BYTES = 4 * 1024 * 1024


class EmbeddingServiceError(RuntimeError):
    """One bounded embedding-service failure safe for logs and audit rows."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class EmbeddingBatch:
    """Validated vectors returned by one embedding request."""

    model: str
    revision: str
    dimensions: int
    vectors: tuple[tuple[float, ...], ...]


class EmbeddingClient(Protocol):
    """Transport-independent embedding interface used by retrieval and indexing."""

    def encode(
        self,
        texts: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> EmbeddingBatch: ...


def _clean_fragment(value: object, *, max_chars: int = 800) -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    printable = "".join(" " if unicodedata.category(char).startswith("C") else char for char in normalized)
    return " ".join(printable.split())[:max_chars]


def _stable_items(values: Sequence[object], *, max_items: int = 20) -> tuple[str, ...]:
    cleaned = {_clean_fragment(value, max_chars=160) for value in values}
    return tuple(sorted((value for value in cleaned if value), key=str.casefold)[:max_items])


def _join_items(values: Sequence[object], *, max_items: int = 20) -> str:
    return "；".join(_stable_items(values, max_items=max_items))


def _build_embedding_text(
    *,
    failure_family: str,
    language: str,
    build_system: str,
    diagnostic_fingerprint: str,
    causal_tokens: Sequence[str],
    problem_pattern: str,
    applicability: Sequence[str],
    anti_conditions: Sequence[str],
    repair_guidance: str,
    validation_guidance: Sequence[str],
) -> str:
    fingerprint = _clean_fragment(diagnostic_fingerprint, max_chars=600)
    tokens = tuple(token for token in _stable_items(causal_tokens, max_items=30) if token != fingerprint)
    diagnostics = "；".join(value for value in (fingerprint, *tokens) if value)
    lines = (
        f"失败类型：{_clean_fragment(failure_family, max_chars=120)}",
        f"语言：{_clean_fragment(language, max_chars=80)}",
        f"构建系统：{_clean_fragment(build_system, max_chars=120)}",
        f"关键报错：{diagnostics}",
        f"问题模式：{_clean_fragment(problem_pattern)}",
        f"适用条件：{_join_items(applicability)}",
        f"不适用条件：{_join_items(anti_conditions)}",
        f"修复建议：{_clean_fragment(repair_guidance, max_chars=1000)}",
        f"验证方法：{_join_items(validation_guidance)}",
    )
    return "\n".join(lines)[:MAX_EMBEDDING_TEXT_CHARS]


def build_memory_embedding_text(memory: RepairMemory) -> str:
    """Return a stable, bounded text without project or execution metadata."""
    return _build_embedding_text(
        failure_family=memory.failure_family,
        language=memory.language,
        build_system=memory.build_system,
        diagnostic_fingerprint=memory.diagnostic_fingerprint,
        causal_tokens=memory.causal_tokens,
        problem_pattern=memory.problem_pattern,
        applicability=memory.applicability,
        anti_conditions=memory.anti_conditions,
        repair_guidance=memory.repair_guidance,
        validation_guidance=memory.validation_guidance,
    )


def build_query_embedding_text(query: RepairQuery) -> str:
    """Return one safe query text using the same field order as memory text."""
    problem_pattern = _join_items((query.failure_category, query.job_family), max_items=2)
    return _build_embedding_text(
        failure_family=query.failure_family,
        language=query.language,
        build_system=query.build_system,
        diagnostic_fingerprint=query.diagnostic_fingerprint,
        causal_tokens=query.causal_tokens,
        problem_pattern=problem_pattern,
        applicability=(),
        anti_conditions=(),
        repair_guidance="",
        validation_guidance=(),
    )


def embedding_source_hash(
    text: str,
    *,
    model_name: str,
    model_revision: str,
) -> str:
    """Hash the text together with every input that changes vector meaning."""
    payload = "\n".join(
        (
            f"template_version={EMBEDDING_TEXT_VERSION}",
            f"model={model_name.strip()}",
            f"revision={model_revision.strip()}",
            text,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finite_vector(vector: Sequence[float]) -> tuple[float, ...]:
    values: list[float] = []
    for value in vector:
        if isinstance(value, bool):
            raise ValueError("embedding vector values must be finite numbers")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("embedding vector values must be finite numbers") from error
        if not math.isfinite(parsed):
            raise ValueError("embedding vector values must be finite numbers")
        values.append(parsed)
    return tuple(values)


def vector_to_blob(vector: Sequence[float]) -> bytes:
    """Encode finite values as portable little-endian float32."""
    values = _finite_vector(vector)
    if not values:
        raise ValueError("embedding vector must not be empty")
    return struct.pack(f"<{len(values)}f", *values)


def blob_to_vector(blob: bytes, dimensions: int) -> tuple[float, ...]:
    """Decode one little-endian float32 BLOB and validate its shape."""
    if dimensions <= 0:
        raise ValueError("embedding dimensions must be positive")
    expected_length = dimensions * 4
    if len(blob) != expected_length:
        raise ValueError(f"embedding BLOB length must be {expected_length} bytes")
    return _finite_vector(struct.unpack(f"<{dimensions}f", blob))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity, treating a zero vector as no similarity."""
    if len(left) != len(right):
        raise ValueError("embedding vectors must have the same dimensions")
    left_values = _finite_vector(left)
    right_values = _finite_vector(right)
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left_values, right_values, strict=True)) / (left_norm * right_norm)


class HttpEmbeddingClient:
    """Bounded synchronous client for the internal BGE-M3 service."""

    def __init__(self, base_url: str) -> None:
        parsed = urlparse(str(base_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("embedding service URL must be an absolute HTTP(S) URL")
        self._base_url = str(base_url).strip().rstrip("/")

    def encode(
        self,
        texts: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> EmbeddingBatch:
        validated_texts = self._validate_texts(texts)
        if timeout_seconds <= 0:
            raise ValueError("embedding timeout must be positive")
        request = urllib.request.Request(
            f"{self._base_url}/embed",
            data=json.dumps(
                {"texts": list(validated_texts), "normalize": True},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
                body = response.read(MAX_EMBEDDING_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise EmbeddingServiceError("http_error", "embedding service returned an HTTP error") from error
        except (TimeoutError, socket.timeout) as error:
            raise EmbeddingServiceError("timeout", "embedding service timed out") from error
        except (urllib.error.URLError, OSError) as error:
            raise EmbeddingServiceError("unavailable", "embedding service unavailable") from error
        if len(body) > MAX_EMBEDDING_RESPONSE_BYTES:
            raise EmbeddingServiceError("invalid_response", "embedding service response is too large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EmbeddingServiceError("invalid_response", "embedding service returned invalid JSON") from error
        return self._parse_payload(payload, expected_count=len(validated_texts))

    @staticmethod
    def _validate_texts(texts: tuple[str, ...]) -> tuple[str, ...]:
        if not texts or len(texts) > MAX_EMBEDDING_BATCH:
            raise ValueError(f"embedding request must contain 1 to {MAX_EMBEDDING_BATCH} texts")
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise ValueError("embedding texts must be non-empty strings")
            if len(text) > MAX_EMBEDDING_TEXT_CHARS:
                raise ValueError(f"embedding text must not exceed {MAX_EMBEDDING_TEXT_CHARS} characters")
        return texts

    @staticmethod
    def _parse_payload(payload: object, *, expected_count: int) -> EmbeddingBatch:
        if not isinstance(payload, dict):
            raise EmbeddingServiceError("invalid_response", "embedding service returned an invalid object")
        if payload.get("model") != BGE_MODEL_NAME:
            raise EmbeddingServiceError("model_mismatch", "embedding service model mismatch")
        if payload.get("revision") != BGE_MODEL_REVISION:
            raise EmbeddingServiceError("revision_mismatch", "embedding service revision mismatch")
        if payload.get("dimensions") != BGE_DIMENSIONS:
            raise EmbeddingServiceError("dimension_mismatch", "embedding service dimension mismatch")
        raw_vectors = payload.get("vectors")
        if not isinstance(raw_vectors, list) or len(raw_vectors) != expected_count:
            raise EmbeddingServiceError("invalid_response", "embedding service returned invalid vectors")
        vectors: list[tuple[float, ...]] = []
        for raw_vector in raw_vectors:
            if not isinstance(raw_vector, dict) or not isinstance(raw_vector.get("values"), list):
                raise EmbeddingServiceError("invalid_response", "embedding service returned invalid vectors")
            if len(raw_vector["values"]) != BGE_DIMENSIONS:
                raise EmbeddingServiceError("dimension_mismatch", "embedding service dimension mismatch")
            try:
                vector = _finite_vector(raw_vector["values"])
            except ValueError as error:
                raise EmbeddingServiceError("invalid_vector", "embedding service returned invalid vectors") from error
            norm = math.sqrt(sum(value * value for value in vector))
            if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=0.01):
                raise EmbeddingServiceError("invalid_vector", "embedding service returned invalid vectors")
            vectors.append(vector)
        return EmbeddingBatch(
            model=BGE_MODEL_NAME,
            revision=BGE_MODEL_REVISION,
            dimensions=BGE_DIMENSIONS,
            vectors=tuple(vectors),
        )
