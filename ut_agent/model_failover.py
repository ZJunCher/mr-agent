"""Shared model failover policy for the outer Agent and Hermes CLI."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("ut_agent.model_failover")

_COMPARE_AND_DELETE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


@dataclass(frozen=True)
class ModelFailure:
    code: str
    reason: str
    switchable: bool
    cooldown_eligible: bool = True


@dataclass(frozen=True)
class ModelAttempt:
    model: str
    failure_code: str = ""
    reason: str = ""


@dataclass(frozen=True)
class LLMCallOutcome:
    response: Any | None
    model: str | None
    attempts: tuple[ModelAttempt, ...]
    terminal_error: str = ""
    context_compression: dict[str, Any] | None = None


class ModelHealthStore:
    """Best-effort shared cooldown state for model routes."""

    def __init__(
        self,
        redis_client=None,
        *,
        base_url: str,
        cooldown_seconds: int,
        probe_lease_seconds: int,
        clock: Callable[[], float] = time.time,
    ):
        self.redis = redis_client
        self.base_url = base_url.rstrip("/").lower()
        self.cooldown_seconds = cooldown_seconds
        self.probe_lease_seconds = probe_lease_seconds
        self.clock = clock

    def _keys(self, model: str) -> tuple[str, str, str]:
        identity = f"{self.base_url}|{model.strip().lower()}".encode()
        digest = hashlib.sha256(identity).hexdigest()[:24]
        prefix = f"pr-agent:model-health:{digest}"
        return f"{prefix}:failure", f"{prefix}:cooldown", f"{prefix}:probe"

    def _redis_warning(self, operation: str, error: Exception) -> None:
        logger.warning("Model health Redis %s failed; using local attempts: %s", operation, type(error).__name__)

    def candidate_allowed(self, model: str, owner: str) -> bool:
        if self.redis is None:
            return True
        failure_key, cooldown_key, probe_key = self._keys(model)
        try:
            if not self.redis.get(failure_key):
                return True
            if self.redis.get(cooldown_key):
                return False
            acquired = self.redis.set(probe_key, owner, nx=True, ex=self.probe_lease_seconds)
            return bool(acquired) or self.redis.get(probe_key) == owner
        except Exception as error:
            self._redis_warning("candidate check", error)
            return True

    def mark_failed(self, model: str, owner: str, failure: ModelFailure) -> None:
        if self.redis is None or not failure.switchable or not failure.cooldown_eligible:
            return
        failure_key, cooldown_key, probe_key = self._keys(model)
        payload = json.dumps({
            "code": failure.code,
            "failed_at": int(self.clock()),
        }, separators=(",", ":"))
        try:
            self.redis.set(failure_key, payload, ex=max(86_400, self.cooldown_seconds * 12))
            self.redis.set(cooldown_key, failure.code, ex=self.cooldown_seconds)
            self.redis.eval(_COMPARE_AND_DELETE, 1, probe_key, owner)
        except Exception as error:
            self._redis_warning("failure update", error)

    def mark_succeeded(self, model: str, owner: str) -> None:
        if self.redis is None:
            return
        failure_key, cooldown_key, probe_key = self._keys(model)
        try:
            self.redis.delete(failure_key, cooldown_key)
            self.redis.eval(_COMPARE_AND_DELETE, 1, probe_key, owner)
        except Exception as error:
            self._redis_warning("success update", error)


def build_model_health_store() -> ModelHealthStore:
    """Build a process-local facade over the shared Redis model state."""
    from ut_agent.config import BASE_URL, MODEL_FAILURE_COOLDOWN_SECONDS, MODEL_PROBE_LEASE_SECONDS

    redis_client = None
    redis_url = os.getenv("PR_AGENT_REDIS_URL", "").strip()
    if redis_url:
        try:
            from pr_agent.distributed.redis_client import RedisClientFactory

            redis_client = RedisClientFactory(redis_url).create_sync()
        except Exception as error:
            logger.warning("Model health Redis initialization failed; using local attempts: %s", type(error).__name__)
    return ModelHealthStore(
        redis_client,
        base_url=BASE_URL,
        cooldown_seconds=MODEL_FAILURE_COOLDOWN_SECONDS,
        probe_lease_seconds=MODEL_PROBE_LEASE_SECONDS,
    )


def ordered_candidates(models: tuple[str, ...], active_model: str | None) -> tuple[str, ...]:
    """Return stable unique candidates, keeping a task on its active model."""
    candidates = tuple(dict.fromkeys(model.strip() for model in models if model.strip()))
    if active_model and active_model in candidates:
        return candidates[candidates.index(active_model):]
    return candidates


def _status_code(error: BaseException | str) -> int | None:
    if isinstance(error, BaseException):
        for source in (error, getattr(error, "response", None)):
            for field in ("status_code", "http_status", "status"):
                value = getattr(source, field, None)
                try:
                    if value is not None:
                        return int(value)
                except (TypeError, ValueError):
                    continue
    match = re.search(r"(?<!\d)([45]\d{2})(?!\d)", str(error))
    return int(match.group(1)) if match else None


def classify_model_failure(error: BaseException | str) -> ModelFailure:
    """Classify whether retrying the same request through another model is safe."""
    reason = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    normalized = reason.lower()
    status = _status_code(error)

    if status in {401, 403}:
        return ModelFailure(f"http_{status}", reason, False)
    if status == 400:
        return ModelFailure("http_400", reason, False)
    if status == 402 or "overage_request_limit_exceeded" in normalized:
        return ModelFailure("quota_exceeded", reason, True)
    if status == 429:
        return ModelFailure("rate_limited", reason, True)
    if any(marker in normalized for marker in (
        "model_not_found",
        "无可用渠道",
        "no available distributor",
        "service unavailable",
        "temporarily unavailable",
    )):
        return ModelFailure("model_unavailable", reason, True)
    if any(marker in normalized for marker in (
        "invalid api response",
        "response.content invalid",
        "exceeded for invalid responses",
    )):
        return ModelFailure("tool_protocol_error", reason, True, cooldown_eligible=False)
    if status is not None and 500 <= status <= 599:
        return ModelFailure(f"http_{status}", reason, True)
    if isinstance(error, (TimeoutError, ConnectionError)) or any(marker in normalized for marker in (
        "timeout",
        "timed out",
        "connectionerror",
        "connection error",
        "connection failed",
    )):
        return ModelFailure("connection_error", reason, True)
    if "agent llm 工具调用响应异常" in normalized:
        return ModelFailure("tool_protocol_error", reason, True, cooldown_eligible=False)
    return ModelFailure("request_failed", reason, False)
