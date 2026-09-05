import json

import pr_agent.config_loader  # noqa: F401 - initialize settings before ut_agent package
from ut_agent.tools.tool_registry import get_tool_contracts, get_tool_definitions
from ut_agent.tools.tool_schema import validate_tool_calls


def _call(name: str, arguments) -> dict:
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_model_schema_and_local_validation_share_strict_contract():
    definitions = {item["function"]["name"]: item["function"] for item in get_tool_definitions()}
    finish = definitions["finish_tool"]["parameters"]

    assert finish["additionalProperties"] is False
    assert set(finish["required"]) == {"summary", "success"}
    assert "state" not in finish["properties"]

    result = validate_tool_calls(
        [_call("finish_tool", json.dumps({"summary": "done", "success": "false"}))],
        get_tool_contracts(),
    )

    assert result.calls == ()
    assert "success" in result.error
    assert "bool_type" in result.error


def test_unknown_and_missing_tool_arguments_are_rejected():
    unknown = validate_tool_calls([_call("invented_tool", "{}")], get_tool_contracts())
    missing = validate_tool_calls([_call("finish_tool", "{}")], get_tool_contracts())
    extra = validate_tool_calls(
        [_call("finish_tool", json.dumps({"summary": "done", "success": False, "extra": 1}))],
        get_tool_contracts(),
    )

    assert unknown.calls == ()
    assert "未知工具" in unknown.error
    assert missing.calls == ()
    assert "missing" in missing.error
    assert extra.calls == ()
    assert "extra_forbidden" in extra.error


def test_invalid_call_makes_whole_batch_non_executable():
    result = validate_tool_calls(
        [
            _call("finish_tool", json.dumps({"summary": "done", "success": False})),
            _call("finish_tool", json.dumps({"summary": "bad", "success": "false"})),
        ],
        get_tool_contracts(),
    )

    assert result.calls == ()


def test_valid_arguments_are_normalized_for_tool_node():
    result = validate_tool_calls(
        [_call("finish_tool", {"summary": "done", "success": False})],
        get_tool_contracts(),
    )

    assert result.error == ""
    assert json.loads(result.calls[0]["function"]["arguments"]) == {
        "summary": "done",
        "success": False,
    }


def test_noarg_transport_reason_is_required_then_removed_before_runtime():
    missing = validate_tool_calls([_call("analyze_diff_tool", "{}")], get_tool_contracts())
    valid = validate_tool_calls(
        [_call("analyze_diff_tool", json.dumps({"reason": "检查最终差异"}))],
        get_tool_contracts(),
    )

    assert missing.calls == ()
    assert "reason" in missing.error
    assert valid.error == ""
    assert json.loads(valid.calls[0]["function"]["arguments"]) == {}
