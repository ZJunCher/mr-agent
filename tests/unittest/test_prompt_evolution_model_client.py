import asyncio

import pytest
from pydantic import BaseModel

from pr_agent.suggestions.prompt_evolution.model_client import (
    PromptEvolutionModelExhausted,
    ToolCallingModelClient,
)


class Result(BaseModel):
    value: str


async def fake_completion(**kwargs):
    function = type("Function", (), {"name": "submit_result", "arguments": '{"value":"ok"}'})()
    call = type("Call", (), {"function": function})()
    message = type("Message", (), {"tool_calls": [call]})()
    choice = type("Choice", (), {"message": message})()
    return type("Response", (), {"choices": [choice]})()


def _completion_with(name: str | None, arguments: str):
    async def complete(**kwargs):
        calls = []
        if name is not None:
            function = type("Function", (), {"name": name, "arguments": arguments})()
            calls = [type("Call", (), {"function": function})()]
        message = type("Message", (), {"tool_calls": calls})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()
    return complete


class SequenceCompletion:
    def __init__(self, results):
        self.results = iter(results)
        self.models = []

    async def __call__(self, **kwargs):
        self.models.append(kwargs["model"])
        value = next(self.results)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeHealthStore:
    def __init__(self, denied=()):
        self.denied = set(denied)
        self.failed = []
        self.succeeded = []

    def candidate_allowed(self, model, owner):
        return model not in self.denied

    def mark_failed(self, model, owner, failure):
        self.failed.append((model, failure.code))

    def mark_succeeded(self, model, owner):
        self.succeeded.append(model)


def _tool_response(name: str = "submit_result", arguments: str = '{"value":"ok"}'):
    function = type("Function", (), {"name": name, "arguments": arguments})()
    call = type("Call", (), {"function": function})()
    message = type("Message", (), {"tool_calls": [call]})()
    choice = type("Choice", (), {"message": message})()
    return type("Response", (), {"choices": [choice]})()


def _response_without_tool_call():
    message = type("Message", (), {"tool_calls": [], "content": "prose only"})()
    choice = type("Choice", (), {"message": message})()
    return type("Response", (), {"choices": [choice]})()


class HttpError(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_tool_client_requires_named_call():
    client = ToolCallingModelClient(completion=fake_completion)
    result = asyncio.run(client.call("model", "system", "user", "submit_result", Result))
    assert result == Result(value="ok")


@pytest.mark.parametrize(
    ("name", "arguments", "error"),
    [
        (None, '{"value":"ok"}', "expected exactly one"),
        ("wrong_name", '{"value":"ok"}', "expected exactly one"),
        ("submit_result", "not-json", "arguments did not match"),
        ("submit_result", '{"wrong":"field"}', "arguments did not match"),
    ],
)
def test_tool_client_rejects_invalid_calls(name, arguments, error):
    client = ToolCallingModelClient(completion=_completion_with(name, arguments))
    with pytest.raises(ValueError, match=error):
        asyncio.run(client.call("model", "system", "user", "submit_result", Result))


def test_tool_client_switches_models_only_for_transient_provider_errors():
    completion = SequenceCompletion([TimeoutError("relay timeout"), _tool_response()])
    health = FakeHealthStore()
    client = ToolCallingModelClient(
        completion=completion,
        models=("sonnet", "opus-4.8", "opus-4.6"),
        attempts_per_model=1,
        health_store=health,
        owner="prompt-evolution-test",
    )

    result = asyncio.run(client.call("sonnet", "s", "u", "submit_result", Result))

    assert result == Result(value="ok")
    assert completion.models == ["sonnet", "opus-4.8"]
    assert health.failed == [("sonnet", "connection_error")]
    assert health.succeeded == ["opus-4.8"]


def test_pair_call_restarts_both_outputs_on_one_model_after_partial_failure():
    completion = SequenceCompletion([
        _tool_response(arguments='{"value":"baseline-a"}'),
        TimeoutError("candidate timeout"),
        _tool_response(arguments='{"value":"baseline-b"}'),
        _tool_response(arguments='{"value":"candidate-b"}'),
    ])
    client = ToolCallingModelClient(
        completion=completion,
        models=("model-a", "model-b"),
        attempts_per_model=1,
        health_store=FakeHealthStore(),
        owner="paired-replay",
    )

    baseline, candidate, model = asyncio.run(client.call_pair_same_model(
        "model-a",
        "system",
        "baseline user",
        "candidate user",
        "submit_result",
        Result,
    ))

    assert completion.models == ["model-a", "model-a", "model-b", "model-b"]
    assert baseline == Result(value="baseline-b")
    assert candidate == Result(value="candidate-b")
    assert model == "model-b"


def test_pair_call_never_returns_partial_schema_invalid_result():
    completion = SequenceCompletion([
        _tool_response(arguments='{"value":"baseline-a"}'),
        _tool_response(arguments='{"wrong":"candidate-a"}'),
        _tool_response(arguments='{"value":"baseline-b"}'),
        _tool_response(arguments='{"value":"candidate-b"}'),
    ])
    client = ToolCallingModelClient(
        completion=completion,
        models=("model-a", "model-b"),
        attempts_per_model=1,
        health_store=FakeHealthStore(),
        owner="paired-replay",
    )

    baseline, candidate, model = asyncio.run(client.call_pair_same_model(
        "model-a", "system", "baseline", "candidate", "submit_result", Result,
    ))

    assert baseline.value == "baseline-b"
    assert candidate.value == "candidate-b"
    assert model == "model-b"


def test_tool_client_switches_model_for_missing_required_tool_call_without_shared_cooldown():
    completion = SequenceCompletion([_response_without_tool_call(), _tool_response()])
    health = FakeHealthStore()
    client = ToolCallingModelClient(
        completion=completion,
        models=("sonnet", "opus-4.8", "opus-4.6"),
        attempts_per_model=2,
        health_store=health,
        owner="prompt-evolution-test",
    )

    result = asyncio.run(client.call("sonnet", "s", "u", "submit_result", Result))

    assert result == Result(value="ok")
    assert completion.models == ["sonnet", "opus-4.8"]
    assert health.failed == []
    assert health.succeeded == ["opus-4.8"]


def test_tool_client_switches_model_for_invalid_tool_arguments_without_accepting_them():
    completion = SequenceCompletion([
        _tool_response(arguments='{"wrong":"field"}'),
        _tool_response(arguments='{"value":"ok"}'),
    ])
    health = FakeHealthStore()
    client = ToolCallingModelClient(
        completion=completion,
        models=("sonnet", "opus-4.8"),
        attempts_per_model=2,
        health_store=health,
        owner="prompt-evolution-test",
    )

    result = asyncio.run(client.call("sonnet", "s", "u", "submit_result", Result))

    assert result == Result(value="ok")
    assert completion.models == ["sonnet", "opus-4.8"]
    assert health.failed == []


@pytest.mark.parametrize("error", [HttpError(400), ValueError("invalid tool schema")])
def test_tool_client_does_not_switch_for_non_transient_errors(error):
    completion = SequenceCompletion([error, _tool_response()])
    client = ToolCallingModelClient(
        completion=completion,
        models=("sonnet", "opus-4.8"),
        attempts_per_model=1,
        health_store=FakeHealthStore(),
        owner="test",
    )

    with pytest.raises(type(error)):
        asyncio.run(client.call("sonnet", "s", "u", "submit_result", Result))
    assert completion.models == ["sonnet"]


def test_tool_client_reports_all_exhausted_transient_models():
    completion = SequenceCompletion([TimeoutError("one"), TimeoutError("two"), TimeoutError("three")])
    client = ToolCallingModelClient(
        completion=completion,
        models=("sonnet", "opus-4.8", "opus-4.6"),
        attempts_per_model=1,
        health_store=FakeHealthStore(),
        owner="test",
    )

    with pytest.raises(PromptEvolutionModelExhausted) as caught:
        asyncio.run(client.call("sonnet", "s", "u", "submit_result", Result))

    assert [attempt.model for attempt in caught.value.attempts] == ["sonnet", "opus-4.8", "opus-4.6"]
    assert completion.models == ["sonnet", "opus-4.8", "opus-4.6"]


def test_tool_client_skips_models_in_shared_cooldown():
    completion = SequenceCompletion([_tool_response()])
    client = ToolCallingModelClient(
        completion=completion,
        models=("sonnet", "opus-4.8"),
        attempts_per_model=1,
        health_store=FakeHealthStore(denied={"sonnet"}),
        owner="test",
    )

    result = asyncio.run(client.call("sonnet", "s", "u", "submit_result", Result))

    assert result.value == "ok"
    assert completion.models == ["opus-4.8"]
