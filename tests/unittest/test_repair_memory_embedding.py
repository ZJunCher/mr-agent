"""Unit tests for repair-memory embedding text, vectors, and HTTP transport."""

from __future__ import annotations

import json
import math
from urllib.error import URLError

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before eager ut_agent imports
import ut_agent.repair_memory.embedding as embedding_module
from tests.unittest.repair_memory_helpers import sample_memory
from ut_agent.repair_memory.embedding import (
    BGE_DIMENSIONS,
    BGE_MODEL_NAME,
    BGE_MODEL_REVISION,
    EmbeddingServiceError,
    HttpEmbeddingClient,
    blob_to_vector,
    build_memory_embedding_text,
    build_query_embedding_text,
    cosine_similarity,
    embedding_source_hash,
    vector_to_blob,
)
from ut_agent.repair_memory.models import RepairQuery


def _query(**overrides) -> RepairQuery:
    values = {
        "project": "group/private-repo",
        "root_cause_group_id": "root-secret",
        "source_pipeline_id": 123,
        "source_sha": "a" * 40,
        "failure_category": "build",
        "job_family": "compile",
        "failure_family": "missing_header",
        "language": "cpp",
        "build_system": "cmake",
        "diagnostic_fingerprint": "std::unique_ptr is not a member of std",
        "causal_tokens": ("unique_ptr", "memory"),
    }
    values.update(overrides)
    return RepairQuery(**values)


def _unit_vector() -> list[float]:
    return [1.0] + [0.0] * (BGE_DIMENSIONS - 1)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self._body


def _payload(vector: list[float] | None = None, **overrides) -> dict:
    payload = {
        "model": BGE_MODEL_NAME,
        "revision": BGE_MODEL_REVISION,
        "dimensions": BGE_DIMENSIONS,
        "vectors": [{"values": vector or _unit_vector()}],
    }
    payload.update(overrides)
    return payload


def test_memory_embedding_text_is_stable_and_excludes_private_metadata():
    memory = sample_memory(
        scope_key="group/private-repo",
        manual_reason="secret-value",
        failure_family="compilation",
        diagnostic_fingerprint="std::unique_ptr is not a member of std",
        causal_tokens=("memory", "unique_ptr", "memory"),
        problem_pattern="编译器找不到 std::unique_ptr",
        repair_guidance="检查并补充 <memory> 头文件",
    )

    text = build_memory_embedding_text(memory)

    assert text.splitlines()[0] == "失败类型：compilation"
    assert "问题模式：编译器找不到 std::unique_ptr" in text
    assert "修复建议：检查并补充 <memory> 头文件" in text
    assert "关键报错：std::unique_ptr is not a member of std；memory；unique_ptr" in text
    assert "secret-value" not in text
    assert "group/private-repo" not in text
    assert "memory_id" not in text


def test_query_embedding_text_uses_safe_fields_only():
    text = build_query_embedding_text(_query())

    assert text.splitlines() == [
        "失败类型：missing_header",
        "语言：cpp",
        "构建系统：cmake",
        "关键报错：std::unique_ptr is not a member of std；memory；unique_ptr",
        "问题模式：build；compile",
        "适用条件：",
        "不适用条件：",
        "修复建议：",
        "验证方法：",
    ]
    assert "group/private-repo" not in text
    assert "root-secret" not in text
    assert "123" not in text
    assert "a" * 40 not in text


def test_embedding_source_hash_versions_text_model_and_revision():
    first = embedding_source_hash("text", model_name="model-a", model_revision="rev-a")
    assert first == embedding_source_hash("text", model_name="model-a", model_revision="rev-a")
    assert first != embedding_source_hash("changed", model_name="model-a", model_revision="rev-a")
    assert first != embedding_source_hash("text", model_name="model-b", model_revision="rev-a")
    assert first != embedding_source_hash("text", model_name="model-a", model_revision="rev-b")


def test_embedding_source_hash_versions_input_template(monkeypatch):
    first = embedding_source_hash("text", model_name="model-a", model_revision="rev-a")
    monkeypatch.setattr(
        embedding_module,
        "EMBEDDING_TEXT_VERSION",
        embedding_module.EMBEDDING_TEXT_VERSION + 1,
    )

    assert first != embedding_source_hash("text", model_name="model-a", model_revision="rev-a")


def test_float32_blob_round_trip_and_cosine_similarity():
    vector = tuple(math.sin(index) / 32.0 for index in range(BGE_DIMENSIONS))
    restored = blob_to_vector(vector_to_blob(vector), BGE_DIMENSIONS)

    assert len(restored) == BGE_DIMENSIONS
    assert restored == pytest.approx(vector, abs=1e-7)
    assert cosine_similarity(vector, vector) == pytest.approx(1.0)
    assert cosine_similarity((0.0, 0.0), (1.0, 0.0)) == 0.0


def test_vector_helpers_reject_invalid_dimensions_and_values():
    with pytest.raises(ValueError, match="finite"):
        vector_to_blob((float("nan"),))
    with pytest.raises(ValueError, match="dimensions"):
        blob_to_vector(b"", 0)
    with pytest.raises(ValueError, match="length"):
        blob_to_vector(b"\x00\x00\x00\x00", 2)
    with pytest.raises(ValueError, match="same dimensions"):
        cosine_similarity((1.0,), (1.0, 0.0))


def test_http_embedding_client_validates_request_and_response(monkeypatch):
    observed = {}

    def fake_urlopen(request, timeout):
        observed["body"] = json.loads(request.data)
        observed["timeout"] = timeout
        observed["url"] = request.full_url
        return _Response(_payload())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = HttpEmbeddingClient("http://embedding:8080/").encode(
        ("编译失败",), timeout_seconds=1.5
    )

    assert observed == {
        "body": {"texts": ["编译失败"], "normalize": True},
        "timeout": 1.5,
        "url": "http://embedding:8080/embed",
    }
    assert result.model == BGE_MODEL_NAME
    assert result.revision == BGE_MODEL_REVISION
    assert result.dimensions == BGE_DIMENSIONS
    assert result.vectors[0] == tuple(_unit_vector())


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (_payload(model="wrong"), "model_mismatch"),
        (_payload(revision="wrong"), "revision_mismatch"),
        (_payload(dimensions=3), "dimension_mismatch"),
        (_payload([float("nan")] + [0.0] * (BGE_DIMENSIONS - 1)), "invalid_vector"),
        (_payload([0.5] + [0.0] * (BGE_DIMENSIONS - 1)), "invalid_vector"),
        (_payload(vectors=[{"wrong": _unit_vector()}]), "invalid_response"),
    ],
)
def test_http_embedding_client_rejects_incompatible_results(monkeypatch, payload, error_code):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: _Response(payload))

    with pytest.raises(EmbeddingServiceError) as error:
        HttpEmbeddingClient("http://embedding:8080").encode(("safe",), timeout_seconds=1.5)

    assert error.value.code == error_code
    assert "safe" not in str(error.value)


def test_http_embedding_client_maps_transport_errors_without_text(monkeypatch):
    def fail(*_args, **_kwargs):
        raise URLError("contains-private-network-detail")

    monkeypatch.setattr("urllib.request.urlopen", fail)

    with pytest.raises(EmbeddingServiceError) as error:
        HttpEmbeddingClient("http://embedding:8080").encode(("secret input",), timeout_seconds=1.5)

    assert error.value.code == "unavailable"
    assert str(error.value) == "embedding service unavailable"


@pytest.mark.parametrize("texts", [(), ("",), tuple("x" for _ in range(33)), ("x" * 4001,)])
def test_http_embedding_client_rejects_invalid_input_without_calling_network(monkeypatch, texts):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: pytest.fail("network should not be called"),
    )

    with pytest.raises(ValueError):
        HttpEmbeddingClient("http://embedding:8080").encode(texts, timeout_seconds=1.5)
