import json
from typing import Literal

import pr_agent.config_loader  # noqa: F401 - initialize settings before ut_agent package
from ut_agent.model_failover import LLMCallOutcome, ModelAttempt
from ut_agent.structured_output import StrictOutputModel, call_structured_output, validate_structured_message


class _Payload(StrictOutputModel):
    schema_version: Literal[1]
    enabled: bool


def _message(arguments, *, name: str = "submit_payload", count: int = 1) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call-{index}",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            for index in range(count)
        ],
    }


def test_strict_message_accepts_one_exact_tool_call():
    value, error = validate_structured_message(
        _message(json.dumps({"schema_version": 1, "enabled": False})),
        output_model=_Payload,
        tool_name="submit_payload",
    )

    assert error == ""
    assert value == _Payload(schema_version=1, enabled=False)


def test_strict_message_rejects_missing_wrong_or_multiple_tool_calls():
    _, missing = validate_structured_message({}, output_model=_Payload, tool_name="submit_payload")
    _, wrong = validate_structured_message(
        _message("{}", name="other"), output_model=_Payload, tool_name="submit_payload"
    )
    _, multiple = validate_structured_message(
        _message("{}", count=2), output_model=_Payload, tool_name="submit_payload"
    )

    assert missing == "tool_call_missing"
    assert wrong == "tool_name:other"
    assert multiple == "tool_call_count"


def test_strict_message_rejects_coercion_and_unknown_fields():
    _, string_bool = validate_structured_message(
        _message(json.dumps({"schema_version": 1, "enabled": "false"})),
        output_model=_Payload,
        tool_name="submit_payload",
    )
    _, string_version = validate_structured_message(
        _message(json.dumps({"schema_version": "1", "enabled": False})),
        output_model=_Payload,
        tool_name="submit_payload",
    )
    _, extra = validate_structured_message(
        _message(json.dumps({"schema_version": 1, "enabled": False, "secret": "ignored"})),
        output_model=_Payload,
        tool_name="submit_payload",
    )

    assert "enabled:bool_type" in string_bool
    assert "schema_version:literal_error" in string_version
    assert "secret:extra_forbidden" in extra
    assert "ignored" not in extra


def test_call_structured_output_forces_target_tool_and_preserves_metadata():
    captured = {}

    async def fake_call(system, user, **kwargs):
        captured.update(kwargs)
        return LLMCallOutcome(
            _message(json.dumps({"schema_version": 1, "enabled": True})),
            "test-model",
            (ModelAttempt("test-model"),),
        )

    import asyncio

    outcome = asyncio.run(call_structured_output(
        "system",
        "user",
        output_model=_Payload,
        tool_name="submit_payload",
        tool_description="submit",
        llm_call=fake_call,
    ))

    assert outcome.value == _Payload(schema_version=1, enabled=True)
    assert outcome.model == "test-model"
    assert captured["tool_choice"]["function"]["name"] == "submit_payload"
    assert captured["tools"][0]["function"]["parameters"]["additionalProperties"] is False


def test_call_structured_output_does_not_parse_text_fallback():
    async def fake_call(*args, **kwargs):
        del args, kwargs
        return LLMCallOutcome(
            {"role": "assistant", "content": '{"schema_version":1,"enabled":true}'},
            "test-model",
            (),
        )

    import asyncio

    outcome = asyncio.run(call_structured_output(
        "system",
        "user",
        output_model=_Payload,
        tool_name="submit_payload",
        tool_description="submit",
        llm_call=fake_call,
    ))

    assert outcome.value is None
    assert outcome.validation_error == "tool_call_missing"
