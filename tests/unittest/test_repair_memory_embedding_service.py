"""API tests for the isolated BGE-M3 service using deterministic fake encoders."""

from __future__ import annotations

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before eager ut_agent imports
from ut_agent.repair_memory.embedding import BGE_DIMENSIONS, BGE_MODEL_NAME, BGE_MODEL_REVISION
from ut_agent.repair_memory.embedding_service import create_embedding_app


def test_embedding_service_cold_import_does_not_load_the_full_agent():
    repo_root = Path(__file__).parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ut_agent.repair_memory.embedding_service import create_embedding_app",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


class FakeEncoder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[2.0] + [0.0] * (BGE_DIMENSIONS - 1) for _ in texts]


def test_liveness_does_not_require_model_readiness():
    client = TestClient(create_embedding_app())

    assert client.get("/health/live").json() == {"status": "alive"}
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "embedding model is not ready"


def test_readiness_reports_fixed_model_identity_after_probe():
    with TestClient(create_embedding_app(FakeEncoder())) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "model": BGE_MODEL_NAME,
        "revision": BGE_MODEL_REVISION,
        "dimensions": BGE_DIMENSIONS,
    }


def test_embed_preserves_order_and_returns_normalized_vectors():
    with TestClient(create_embedding_app(FakeEncoder())) as client:
        response = client.post("/embed", json={"texts": ["first", "second"], "normalize": True})

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == BGE_MODEL_NAME
    assert body["revision"] == BGE_MODEL_REVISION
    assert body["dimensions"] == BGE_DIMENSIONS
    assert len(body["vectors"]) == 2
    assert body["vectors"][0]["values"] == [1.0] + [0.0] * (BGE_DIMENSIONS - 1)
    assert body["vectors"][1]["values"] == [1.0] + [0.0] * (BGE_DIMENSIONS - 1)


def test_embed_rejects_invalid_request_shapes():
    with TestClient(create_embedding_app(FakeEncoder())) as client:
        assert client.post("/embed", json={"texts": [], "normalize": True}).status_code == 422
        assert client.post("/embed", json={"texts": ["x"], "normalize": False}).status_code == 422
        assert client.post("/embed", json={"texts": ["x"] * 33, "normalize": True}).status_code == 422
        assert client.post("/embed", json={"texts": ["x" * 4001], "normalize": True}).status_code == 422


class BlockingEncoder:
    def __init__(self) -> None:
        self.entered = threading.Condition()
        self.active = 0
        self.release = threading.Event()

    def encode(self, texts: list[str]) -> list[list[float]]:
        if texts == ["BGE-M3 readiness probe"]:
            return [[1.0] + [0.0] * (BGE_DIMENSIONS - 1)]
        with self.entered:
            self.active += 1
            self.entered.notify_all()
        self.release.wait(timeout=5)
        return [[1.0] + [0.0] * (BGE_DIMENSIONS - 1) for _ in texts]


def test_embed_rejects_work_above_the_two_request_concurrency_limit():
    encoder = BlockingEncoder()
    app = create_embedding_app(encoder)
    with TestClient(app) as first_client, TestClient(app) as second_client, TestClient(app) as third_client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                first_client.post,
                "/embed",
                json={"texts": ["first"], "normalize": True},
            )
            second = pool.submit(
                second_client.post,
                "/embed",
                json={"texts": ["second"], "normalize": True},
            )
            with encoder.entered:
                assert encoder.entered.wait_for(lambda: encoder.active == 2, timeout=3)
            rejected = third_client.post("/embed", json={"texts": ["third"], "normalize": True})
            encoder.release.set()
            assert first.result(timeout=3).status_code == 200
            assert second.result(timeout=3).status_code == 200

    assert rejected.status_code == 429
    assert rejected.json()["detail"] == "embedding service is busy"
