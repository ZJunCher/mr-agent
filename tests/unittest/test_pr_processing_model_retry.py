import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pr_agent.algo import pr_processing
from pr_agent.algo.model_resilience import ModelExhaustedError, ModelFailureKind


class FakeSettings:
    def __init__(self, independent=None):
        self.config = SimpleNamespace(independent_fallback_models=independent or [])
        self.values = {"openai.deployment_id": "original-deployment"}

    def get(self, key, default=None):
        if key == "config.independent_fallback_models":
            return self.config.independent_fallback_models
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


def configure_targets(monkeypatch, models, deployments=None, independent=None):
    settings = FakeSettings(independent)
    monkeypatch.setattr(pr_processing, "get_settings", lambda: settings)
    monkeypatch.setattr(pr_processing, "_get_all_models", lambda _model_type: models)
    monkeypatch.setattr(
        pr_processing,
        "_get_all_deployments",
        lambda _models: deployments or [None] * len(models),
    )
    return settings


def test_transient_failures_retry_twice_with_exponential_backoff(monkeypatch):
    settings = configure_targets(monkeypatch, ["gpt-primary"], ["deployment-1"])
    attempts = 0
    delays = []
    failures = []

    async def predict(_model):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("timed out")
        return "ok"

    async def fake_sleep(delay):
        delays.append(delay)

    result = asyncio.run(pr_processing.retry_with_fallback_models(
        predict,
        retry_limit=2,
        sleep=fake_sleep,
        jitter=lambda: 0.0,
        on_failure=failures.append,
    ))

    assert result == "ok"
    assert attempts == 3
    assert delays == [1.0, 2.0]
    assert [item.attempt for item in failures] == [1, 2]
    assert settings.get("openai.deployment_id") == "original-deployment"


def test_permanent_failure_moves_directly_to_next_target(monkeypatch):
    configure_targets(monkeypatch, ["gpt-primary", "gpt-fallback"])
    calls = []

    async def predict(model):
        calls.append(model)
        if model == "gpt-primary":
            raise RuntimeError("HTTP 401 invalid api key")
        return "fallback-ok"

    result = asyncio.run(pr_processing.retry_with_fallback_models(predict, retry_limit=2))

    assert result == "fallback-ok"
    assert calls == ["gpt-primary", "gpt-fallback"]


def test_exhausted_ordinary_targets_reach_independent_provider(monkeypatch):
    configure_targets(
        monkeypatch,
        ["gpt-primary", "gpt-fallback"],
        independent=["anthropic/claude-haiku"],
    )
    calls = []

    async def predict(model):
        calls.append(model)
        if model.startswith("gpt"):
            raise RuntimeError("invalid request")
        return "independent-ok"

    result = asyncio.run(pr_processing.retry_with_fallback_models(
        predict,
        include_independent=True,
    ))

    assert result == "independent-ok"
    assert calls == ["gpt-primary", "gpt-fallback", "anthropic/claude-haiku"]


def test_all_failures_raise_exhausted_error_with_every_attempt(monkeypatch):
    configure_targets(monkeypatch, ["gpt-primary"], independent=["anthropic/claude-haiku"])

    async def predict(model):
        raise TimeoutError(f"{model} timed out")

    with pytest.raises(ModelExhaustedError) as raised:
        asyncio.run(pr_processing.retry_with_fallback_models(
            predict,
            retry_limit=1,
            include_independent=True,
            sleep=lambda _delay: asyncio.sleep(0),
            jitter=lambda: 0.0,
        ))

    assert len(raised.value.failures) == 4
    assert [item.attempt for item in raised.value.failures] == [1, 2, 1, 2]
    assert all(item.kind is ModelFailureKind.TIMEOUT for item in raised.value.failures)


def test_default_retry_policy_calls_each_model_once(monkeypatch):
    configure_targets(monkeypatch, ["gpt-primary", "gpt-fallback"])
    calls = []

    async def predict(model):
        calls.append(model)
        raise TimeoutError("timed out")

    with pytest.raises(ModelExhaustedError):
        asyncio.run(pr_processing.retry_with_fallback_models(predict))

    assert calls == ["gpt-primary", "gpt-fallback"]


def test_primary_provider_is_rejected_from_independent_targets(monkeypatch):
    configure_targets(
        monkeypatch,
        ["gpt-primary"],
        independent=["openai/gpt-other", "gpt-unprefixed"],
    )
    logger = Mock()
    monkeypatch.setattr(pr_processing, "get_logger", lambda: logger)

    targets = pr_processing._get_model_targets(pr_processing.ModelType.REGULAR, True)

    assert targets == [pr_processing.ModelTarget("gpt-primary", None)]
    assert logger.warning.call_count == 2
