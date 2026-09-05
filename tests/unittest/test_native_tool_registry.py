"""Backend-aware Native Repair tool registry tests."""

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing ut_agent.
import ut_agent.config as config_module
import ut_agent.tools.tool_registry as tool_registry
from ut_agent.prompt.agent_system import build_system_prompt

NATIVE_NAMES = {
    "search_repo_tool",
    "apply_repo_patch_tool",
    "inspect_repo_diff_tool",
    "run_repo_validation_tool",
}


def test_native_registry_exposes_all_native_tools():
    assert NATIVE_NAMES <= {item.name for item in tool_registry.get_all_tools("native")}


def test_hermes_registry_excludes_native_tools():
    assert NATIVE_NAMES.isdisjoint({item.name for item in tool_registry.get_all_tools("hermes")})


def test_native_definitions_and_descriptions_match_registry(monkeypatch):
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")
    names = {item.name for item in tool_registry.get_all_tools()}
    definitions = {item["function"]["name"] for item in tool_registry.get_tool_definitions()}
    descriptions = {
        line.removeprefix("- ").split(":", 1)[0]
        for line in tool_registry.format_tool_descriptions().splitlines()
    }

    assert definitions == names
    assert descriptions == names


def test_tool_node_uses_same_backend_registry(monkeypatch):
    captured = {}
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")
    monkeypatch.setattr(tool_registry, "ToolNode", lambda tools: captured.setdefault("tools", tools))

    result = tool_registry.create_tool_node()

    assert result == captured["tools"]
    assert {item.name for item in result} == {item.name for item in tool_registry.get_all_tools()}


def test_native_prompt_uses_native_modification_and_validation_tools(monkeypatch):
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")

    prompt = build_system_prompt(
        {"trigger_type": "pipeline_failed", "mr_id": 1},
        "native tools",
    )

    assert "apply_repo_patch_tool" in prompt
    assert "inspect_repo_diff_tool" in prompt
    assert "run_repo_validation_tool" in prompt
    assert "不得调用 generate_code_tool" in prompt
    assert "编码任务委托给 generate_code 工具" not in prompt


def test_hermes_prompt_preserves_generate_code_protocol(monkeypatch):
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "hermes")

    prompt = build_system_prompt(
        {"trigger_type": "pipeline_failed", "mr_id": 1},
        "hermes tools",
    )

    assert "generate_code_tool" in prompt
    assert "operation=\"investigate\"" in prompt
