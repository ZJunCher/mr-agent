"""Internal-only FastAPI service that owns the single BGE-M3 model copy."""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from ut_agent.repair_memory.embedding import (
    BGE_DIMENSIONS,
    BGE_MODEL_NAME,
    BGE_MODEL_REVISION,
    MAX_EMBEDDING_BATCH,
    MAX_EMBEDDING_TEXT_CHARS,
)

_READINESS_PROBE = "BGE-M3 readiness probe"
_MAX_CONCURRENT_REQUESTS = 2


class TextEncoder(Protocol):
    """Minimal synchronous encoder interface used by the HTTP service."""

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class EmbedRequest(BaseModel):
    """Bounded request accepted by the internal embedding endpoint."""

    texts: list[str] = Field(min_length=1, max_length=MAX_EMBEDDING_BATCH)
    normalize: Literal[True] = True

    @field_validator("texts")
    @classmethod
    def validate_texts(cls, texts: list[str]) -> list[str]:
        for text in texts:
            if not text.strip():
                raise ValueError("embedding texts must not be empty")
            if len(text) > MAX_EMBEDDING_TEXT_CHARS:
                raise ValueError(f"embedding text must not exceed {MAX_EMBEDDING_TEXT_CHARS} characters")
        return texts


class _EmbeddingState:
    def __init__(self, encoder: TextEncoder | None) -> None:
        self.encoder = encoder
        self.ready = False
        self.request_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)


class _SentenceTransformerEncoder:
    """Lazy production adapter; importing this module never imports Torch."""

    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(
            BGE_MODEL_NAME,
            revision=BGE_MODEL_REVISION,
            cache_folder="/models",
            local_files_only=True,
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts,
            batch_size=min(len(texts), 16),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.tolist()


def _normalized_vectors(raw_vectors: object, *, expected_count: int) -> list[list[float]]:
    if not isinstance(raw_vectors, (list, tuple)) or len(raw_vectors) != expected_count:
        raise ValueError("embedding encoder returned an invalid vector count")
    normalized: list[list[float]] = []
    for raw_vector in raw_vectors:
        if not isinstance(raw_vector, (list, tuple)) or len(raw_vector) != BGE_DIMENSIONS:
            raise ValueError("embedding encoder returned an invalid vector dimension")
        values: list[float] = []
        for value in raw_vector:
            if isinstance(value, bool):
                raise ValueError("embedding encoder returned a non-finite value")
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError("embedding encoder returned a non-finite value")
            values.append(parsed)
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:
            raise ValueError("embedding encoder returned a zero vector")
        normalized.append([value / norm for value in values])
    return normalized


def _probe(state: _EmbeddingState) -> None:
    if state.encoder is None:
        return
    _normalized_vectors(state.encoder.encode([_READINESS_PROBE]), expected_count=1)
    state.ready = True


def create_embedding_app(encoder: TextEncoder | None = None) -> FastAPI:
    """Create the internal service, optionally with a deterministic test encoder."""
    state = _EmbeddingState(encoder)
    if encoder is not None:
        _probe(state)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if state.encoder is None:
            try:
                state.encoder = _SentenceTransformerEncoder()
                _probe(state)
            except Exception as error:
                logging.getLogger(__name__).error(
                    "BGE-M3 model initialization failed: %s", type(error).__name__
                )
        yield

    service = FastAPI(title="BGE-M3 Repair Memory Embedding Service", lifespan=lifespan)

    @service.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "alive"}

    @service.get("/health/ready")
    def health_ready() -> dict[str, str | int]:
        if not state.ready:
            raise HTTPException(status_code=503, detail="embedding model is not ready")
        return {
            "status": "ready",
            "model": BGE_MODEL_NAME,
            "revision": BGE_MODEL_REVISION,
            "dimensions": BGE_DIMENSIONS,
        }

    @service.post("/embed")
    def embed(request: EmbedRequest) -> dict[str, object]:
        if not state.ready or state.encoder is None:
            raise HTTPException(status_code=503, detail="embedding model is not ready")
        if not state.request_slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="embedding service is busy")
        try:
            vectors = _normalized_vectors(state.encoder.encode(request.texts), expected_count=len(request.texts))
        except Exception as error:
            logging.getLogger(__name__).error("BGE-M3 encoding failed: %s", type(error).__name__)
            raise HTTPException(status_code=503, detail="embedding encoding failed") from error
        finally:
            state.request_slots.release()
        return {
            "model": BGE_MODEL_NAME,
            "revision": BGE_MODEL_REVISION,
            "dimensions": BGE_DIMENSIONS,
            "vectors": [{"values": vector} for vector in vectors],
        }

    return service


app = create_embedding_app()
