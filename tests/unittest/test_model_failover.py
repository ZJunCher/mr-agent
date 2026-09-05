from __future__ import annotations

import asyncio
import time

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before the eager ut_agent package import
import ut_agent.llm as llm_module
from pr_agent.triage.model_availability import is_model_service_unavailable
from ut_agent.config import MODEL_CANDIDATES, MODEL_FAILURE_COOLDOWN_SECONDS, MODEL_PROBE_LEASE_SECONDS
from ut_agent.model_failover import (
    LLMCallOutcome,
    ModelAttempt,
    ModelFailure,
    ModelHealthStore,
    classify_model_failure,
    ordered_candidates,
)


class FakeClock:
    def __init__(self):
        self.value = 1_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeRedis:
    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.expirations = {}

    def _expire(self, key):
        expires_at = self.expirations.get(key)
        if expires_at is not None and expires_at <= self.clock():
            self.values.pop(key, None)
            self.expirations.pop(key, None)

    def get(self, key):
        self._expire(key)
        return self.values.get(key)

    def set(self, key, value, *, ex=None, nx=False):
        self._expire(key)
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = self.clock() + ex
        return True

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.expirations.pop(key, None)

    def eval(self, _script, _numkeys, key, owner):
        if self.get(key) != owner:
            return 0
        self.delete(key)
        return 1


class BrokenRedis:
    def get(self, _key):
        raise ConnectionError("redis unavailable")


def test_configured_model_candidates_use_requested_order():
    assert MODEL_CANDIDATES == (
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-4-6",
    )
    assert MODEL_FAILURE_COOLDOWN_SECONDS == 300
    assert MODEL_PROBE_LEASE_SECONDS == 30


def test_ordered_candidates_starts_with_active_model():
    models = ("anthropic/sonnet", "anthropic/opus-a", "anthropic/opus-b")

    assert ordered_candidates(models, "anthropic/opus-a") == (
        "anthropic/opus-a",
        "anthropic/opus-b",
    )


def test_ordered_candidates_deduplicates_models_stably():
    models = ("anthropic/sonnet", "anthropic/sonnet", "anthropic/opus")

    assert ordered_candidates(models, None) == ("anthropic/sonnet", "anthropic/opus")


@pytest.mark.parametrize(
    "error",
    [
        "402 Payment Required",
        "429 Too Many Requests",
        "503 Service Unavailable",
        "model_not_found: 无可用渠道（distributor）",
        TimeoutError("relay timed out"),
    ],
)
def test_provider_failures_are_switchable(error):
    assert classify_model_failure(error).switchable is True


@pytest.mark.parametrize(
    "error",
    [
        "Invalid API response (attempt 1/3): response.content invalid (not a non-empty list)",
        "Max retries (3) exceeded for invalid responses. Giving up.",
    ],
)
def test_invalid_provider_responses_are_switchable_protocol_failures(error):
    failure = classify_model_failure(error)

    assert failure.code == "tool_protocol_error"
    assert failure.switchable is True


@pytest.mark.parametrize(
    "error",
    [
        "400 context length exceeded",
        "401 invalid API key",
        "403 forbidden",
        "pipeline still failed",
    ],
)
def test_non_provider_failures_do_not_switch(error):
    assert classify_model_failure(error).switchable is False


@pytest.mark.parametrize("reason", [
    "搜索循环 - 执行了 96 次搜索/读取操作后超时",
    "Hermes 整体执行超时",
])
def test_human_execution_timeout_text_is_not_provider_failure(reason):
    assert classify_model_failure(reason).switchable is False


def test_model_unavailable_compatibility_never_overrides_structured_failure_kind():
    legacy_error = "模型服务暂时不可用；已尝试全部模型。"

    assert is_model_service_unavailable("provider_unavailable", "") is True
    assert is_model_service_unavailable("", legacy_error) is True
    assert is_model_service_unavailable("search_loop", legacy_error) is False


def test_protocol_failure_is_switchable_without_starting_shared_cooldown():
    store, _clock = _health_store()
    failure = classify_model_failure(
        "Invalid API response (attempt 3/3): response.content invalid (not a non-empty list)"
    )

    assert failure.code == "tool_protocol_error"
    assert failure.switchable is True
    assert failure.cooldown_eligible is False

    store.mark_failed("anthropic/sonnet", "worker-1", failure)

    assert store.candidate_allowed("anthropic/sonnet", "worker-2") is True


def _health_store():
    clock = FakeClock()
    redis = FakeRedis(clock)
    return ModelHealthStore(
        redis,
        base_url="https://relay.example",
        cooldown_seconds=300,
        probe_lease_seconds=30,
        clock=clock,
    ), clock


def test_failed_model_is_skipped_during_shared_cooldown():
    store, _clock = _health_store()
    failure = ModelFailure("model_unavailable", "no distributor", True)

    store.mark_failed("anthropic/sonnet", "worker-1", failure)

    assert store.candidate_allowed("anthropic/sonnet", "worker-2") is False


def test_only_one_worker_gets_probe_after_cooldown():
    store, clock = _health_store()
    failure = ModelFailure("model_unavailable", "no distributor", True)
    store.mark_failed("anthropic/sonnet", "worker-0", failure)
    clock.advance(301)

    assert store.candidate_allowed("anthropic/sonnet", "worker-1") is True
    assert store.candidate_allowed("anthropic/sonnet", "worker-2") is False


def test_success_clears_shared_cooldown_and_probe():
    store, clock = _health_store()
    failure = ModelFailure("model_unavailable", "no distributor", True)
    store.mark_failed("anthropic/sonnet", "worker-0", failure)
    clock.advance(301)
    assert store.candidate_allowed("anthropic/sonnet", "worker-1") is True

    store.mark_succeeded("anthropic/sonnet", "worker-1")

    assert store.candidate_allowed("anthropic/sonnet", "worker-2") is True


def test_failed_probe_restarts_cooldown_and_releases_its_lease():
    store, clock = _health_store()
    failure = ModelFailure("model_unavailable", "no distributor", True)
    store.mark_failed("anthropic/sonnet", "worker-0", failure)
    clock.advance(301)
    assert store.candidate_allowed("anthropic/sonnet", "worker-1") is True

    store.mark_failed("anthropic/sonnet", "worker-1", failure)

    assert store.candidate_allowed("anthropic/sonnet", "worker-2") is False


def test_redis_failure_degrades_to_local_attempts():
    store = ModelHealthStore(
        BrokenRedis(),
        base_url="https://relay.example",
        cooldown_seconds=300,
        probe_lease_seconds=30,
        clock=time.time,
    )

    assert store.candidate_allowed("anthropic/sonnet", "worker-1") is True


def _completion_response(content="done", *, finish_reason="stop", tool_calls=None):
    message = type("Message", (), {"content": content, "tool_calls": tool_calls or []})()
    choice = type("Choice", (), {"message": message, "finish_reason": finish_reason})()
    return type("Response", (), {"id": "response-1", "choices": [choice]})()


def _without_shared_cooldown(monkeypatch):
    monkeypatch.setattr(
        llm_module,
        "_MODEL_HEALTH_STORE",
        ModelHealthStore(
            None,
            base_url="https://relay.example",
            cooldown_seconds=300,
            probe_lease_seconds=30,
        ),
    )
    monkeypatch.setattr(llm_module, "MODEL_CANDIDATES", (
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-4-6",
    ))


def test_agent_llm_fails_over_to_next_model(monkeypatch):
    calls = []
    _without_shared_cooldown(monkeypatch)

    async def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "anthropic/claude-sonnet-5":
            raise RuntimeError("model_not_found: 无可用渠道（distributor）")
        return _completion_response()

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)

    outcome = asyncio.run(llm_module.call_agent_llm(
        "system",
        [],
        [],
        return_outcome=True,
    ))

    assert calls == ["anthropic/claude-sonnet-5", "anthropic/claude-opus-4-8"]
    assert outcome.model == "anthropic/claude-opus-4-8"
    assert outcome.response.content == "done"
    assert outcome.terminal_error == ""


def test_call_llm_outcome_returns_selected_model_and_attempts(monkeypatch):
    async def run_test():
        attempts = (ModelAttempt("anthropic/claude-sonnet-5", "model_unavailable", "down"),)
        monkeypatch.setattr(
            llm_module,
            "_completion_with_failover",
            AsyncMock(return_value=LLMCallOutcome(
                response=_completion_response('{"ok":true}'),
                model="anthropic/claude-opus-4-8",
                attempts=attempts,
            )),
        )
        result = await llm_module.call_llm_outcome("system", "user", temperature=0.0, max_tokens=200)
        assert result.text == '{"ok":true}'
        assert result.model == "anthropic/claude-opus-4-8"
        assert result.attempts[0].failure_code == "model_unavailable"

    from unittest.mock import AsyncMock

    asyncio.run(run_test())


def test_tool_llm_outcome_preserves_forced_tool_contract(monkeypatch):
    observed = {}
    _without_shared_cooldown(monkeypatch)

    async def fake_completion(**kwargs):
        observed.update(kwargs)
        return _completion_response(tool_calls=[{"name": "submit_result"}], finish_reason="tool_calls")

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)
    tools = [{"type": "function", "function": {"name": "submit_result", "parameters": {"type": "object"}}}]
    choice = {"type": "function", "function": {"name": "submit_result"}}

    outcome = asyncio.run(llm_module.call_tool_llm_outcome(
        "system",
        "user",
        tools=tools,
        tool_choice=choice,
        max_tokens=300,
    ))

    assert outcome.response.tool_calls == [{"name": "submit_result"}]
    assert observed["tools"] == tools
    assert observed["tool_choice"] == choice
    assert observed["max_tokens"] == 300


def test_tool_schema_unsupported_route_fails_over_immediately(monkeypatch):
    calls = []
    _without_shared_cooldown(monkeypatch)

    async def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "anthropic/claude-sonnet-5":
            raise RuntimeError("400 tool_choice is unsupported on this route")
        return _completion_response(tool_calls=[{"name": "submit_result"}], finish_reason="tool_calls")

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)

    outcome = asyncio.run(llm_module.call_tool_llm_outcome(
        "system",
        "user",
        tools=[],
        tool_choice={"type": "function", "function": {"name": "submit_result"}},
    ))

    assert calls == ["anthropic/claude-sonnet-5", "anthropic/claude-opus-4-8"]
    assert outcome.model == "anthropic/claude-opus-4-8"
    assert outcome.attempts[0].failure_code == "tool_schema_unsupported"


def test_agent_llm_starts_with_task_active_model(monkeypatch):
    calls = []
    _without_shared_cooldown(monkeypatch)

    async def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        return _completion_response()

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)

    outcome = asyncio.run(llm_module.call_agent_llm(
        "system",
        [],
        [],
        active_model="anthropic/claude-opus-4-8",
        return_outcome=True,
    ))

    assert calls == ["anthropic/claude-opus-4-8"]
    assert outcome.model == "anthropic/claude-opus-4-8"


def test_agent_llm_does_not_fail_over_on_auth_error(monkeypatch):
    calls = []
    _without_shared_cooldown(monkeypatch)

    async def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        raise RuntimeError("401 invalid API key")

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)

    outcome = asyncio.run(llm_module.call_agent_llm("system", [], [], return_outcome=True))

    assert calls == ["anthropic/claude-sonnet-5"]
    assert outcome.model is None
    assert outcome.terminal_error.startswith("模型请求失败")


def test_agent_llm_exhaustion_is_a_structured_terminal_result(monkeypatch):
    calls = []
    _without_shared_cooldown(monkeypatch)

    async def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        raise RuntimeError("model_not_found: 无可用渠道（distributor）")

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)

    outcome = asyncio.run(llm_module.call_agent_llm("system", [], [], return_outcome=True))

    assert calls == list(llm_module.MODEL_CANDIDATES)
    assert outcome.response is None
    assert outcome.terminal_error.startswith("模型服务暂时不可用")
    assert "claude-sonnet-5" in outcome.terminal_error
    assert "claude-opus-4-6" in outcome.terminal_error


def test_malformed_tool_responses_fail_over_after_same_model_retries(monkeypatch):
    calls = []
    _without_shared_cooldown(monkeypatch)

    async def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "anthropic/claude-sonnet-5":
            return _completion_response("plan", finish_reason="tool_calls")
        return _completion_response()

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)

    outcome = asyncio.run(llm_module.call_agent_llm("system", [], [], return_outcome=True))

    assert calls == [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-sonnet-5",
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
    ]
    assert outcome.model == "anthropic/claude-opus-4-8"


def test_plain_llm_call_uses_fallback_model(monkeypatch):
    calls = []
    _without_shared_cooldown(monkeypatch)

    async def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "anthropic/claude-sonnet-5":
            raise RuntimeError("model_not_found: no available distributor")
        return _completion_response("fallback answer")

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)

    result = asyncio.run(llm_module.call_llm("system", "user"))

    assert result == "fallback answer"
    assert calls == ["anthropic/claude-sonnet-5", "anthropic/claude-opus-4-8"]


def test_continuation_stays_on_selected_fallback_model(monkeypatch):
    calls = []
    _without_shared_cooldown(monkeypatch)

    async def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "anthropic/claude-sonnet-5":
            raise RuntimeError("model_not_found: no available distributor")
        if len(calls) == 2:
            return _completion_response("part-1", finish_reason="length")
        return _completion_response("part-2")

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)

    result = asyncio.run(llm_module.call_llm_with_continuation("system", "user"))

    assert result == "part-1part-2"
    assert calls == [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-4-8",
    ]
