"""Classification and sanitized evidence for model-provider failures."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

import openai


class ModelFailureKind(StrEnum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER = "server"
    AUTHORIZATION = "authorization"
    CONFIGURATION = "configuration"
    REQUEST = "request"
    UNKNOWN = "unknown"


TRANSIENT_MODEL_FAILURES = frozenset({
    ModelFailureKind.RATE_LIMIT,
    ModelFailureKind.TIMEOUT,
    ModelFailureKind.CONNECTION,
    ModelFailureKind.SERVER,
})


@dataclass(frozen=True)
class ModelAttemptFailure:
    model: str
    deployment_id: str | None
    attempt: int
    kind: ModelFailureKind
    message: str
    elapsed_ms: int


class ModelExhaustedError(RuntimeError):
    def __init__(self, failures: tuple[ModelAttemptFailure, ...]):
        self.failures = failures
        summary = "; ".join(
            f"{item.model}#{item.attempt} {item.kind.value}: {item.message}"
            for item in failures
        )
        super().__init__(f"Model attempts exhausted: {summary}")


def _is_openai_error(error: Exception, *names: str) -> bool:
    return any(
        isinstance(error, error_type)
        for name in names
        if isinstance((error_type := getattr(openai, name, None)), type)
    )


def classify_model_failure(error: Exception) -> ModelFailureKind:
    if _is_openai_error(error, "AuthenticationError", "PermissionDeniedError"):
        return ModelFailureKind.AUTHORIZATION
    if _is_openai_error(error, "RateLimitError"):
        return ModelFailureKind.RATE_LIMIT
    if isinstance(error, TimeoutError) or _is_openai_error(error, "APITimeoutError"):
        return ModelFailureKind.TIMEOUT
    if isinstance(error, ConnectionError) or _is_openai_error(error, "APIConnectionError"):
        return ModelFailureKind.CONNECTION
    if _is_openai_error(error, "NotFoundError"):
        return ModelFailureKind.CONFIGURATION
    if _is_openai_error(error, "BadRequestError", "UnprocessableEntityError"):
        return ModelFailureKind.REQUEST
    if _is_openai_error(error, "InternalServerError"):
        return ModelFailureKind.SERVER

    text = f"{error.__class__.__name__}: {error}".lower()
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if status in {401, 403} or any(token in text for token in (
        "invalid api key", "invalid_api_key", "authentication", "unauthorized", "forbidden", "permission denied",
    )):
        return ModelFailureKind.AUTHORIZATION
    if status == 429 or any(token in text for token in (
        "rate limit", "ratelimit", "too many requests", "http 429", "quota", "capacity",
    )):
        return ModelFailureKind.RATE_LIMIT
    if any(token in text for token in ("timeout", "timed out", "read timed out", "request timed out")):
        return ModelFailureKind.TIMEOUT
    if any(token in text for token in (
        "connection reset", "connection refused", "connection error", "network error", "dns failure",
    )):
        return ModelFailureKind.CONNECTION
    if status == 404 or any(token in text for token in (
        "model deployment not found", "model not found", "deployment not found", "unknown model", "invalid model",
    )):
        return ModelFailureKind.CONFIGURATION
    if status in {400, 409, 413, 422} or any(token in text for token in (
        "context length", "context window", "invalid request", "bad request", "unsupported parameter",
    )):
        return ModelFailureKind.REQUEST
    if isinstance(status, int) and 500 <= status <= 599:
        return ModelFailureKind.SERVER
    if any(token in text for token in (
        "http 500", "http 502", "http 503", "http 504", "internal server", "service unavailable", "bad gateway",
    )):
        return ModelFailureKind.SERVER
    return ModelFailureKind.UNKNOWN


def is_transient_model_failure(kind: ModelFailureKind) -> bool:
    return kind in TRANSIENT_MODEL_FAILURES


def sanitize_model_error(error: Exception, limit: int = 300) -> str:
    message = re.sub(r"\s+", " ", str(error)).strip() or error.__class__.__name__
    message = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+",
        r"\1[REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*[\"']?[^\s,;&\"']+",
        lambda match: f"{match.group(1)}=[REDACTED]",
        message,
    )
    return message[:max(0, int(limit))]
