"""Single strict Schema source for model-visible UT-Agent tool arguments."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model


class _StrictToolArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class _NoArgumentTransport(_StrictToolArguments):
    reason: str = Field(min_length=1, max_length=300, description="调用此工具的简要原因。")


@dataclass(frozen=True)
class ToolArgumentContract:
    tool: Any
    visible_model: type[BaseModel]
    no_runtime_arguments: bool


@dataclass(frozen=True)
class ToolCallValidation:
    calls: tuple[dict[str, Any], ...]
    error: str = ""


def _is_injected(name: str, field: Any) -> bool:
    if name == "state":
        return True
    return any(getattr(item, "__name__", "") == "InjectedState" for item in field.metadata)


def _visible_model(tool: Any) -> tuple[type[BaseModel], bool]:
    source = getattr(tool, "args_schema", None)
    fields: dict[str, tuple[Any, Any]] = {}
    if source is not None:
        for name, field in source.model_fields.items():
            if _is_injected(name, field):
                continue
            fields[name] = (field.annotation, copy.deepcopy(field))
    if not fields:
        return _NoArgumentTransport, True
    model = create_model(
        f"{str(getattr(tool, 'name', 'Tool')).title().replace('_', '')}StrictArguments",
        __base__=_StrictToolArguments,
        **fields,
    )
    return model, False


def build_tool_contracts(tools: list[Any]) -> dict[str, ToolArgumentContract]:
    """Build strict contracts once from registered LangChain tools."""
    contracts: dict[str, ToolArgumentContract] = {}
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        if not name:
            raise ValueError("registered tool is missing a name")
        model, no_runtime_arguments = _visible_model(tool)
        contracts[name] = ToolArgumentContract(tool, model, no_runtime_arguments)
    return contracts


def tool_definitions(contracts: dict[str, ToolArgumentContract]) -> list[dict[str, Any]]:
    """Return complete OpenAI function definitions from strict local models."""
    definitions = []
    for name, contract in contracts.items():
        schema = contract.visible_model.model_json_schema()
        schema["additionalProperties"] = False
        description = str(getattr(contract.tool, "description", "") or "")
        if contract.no_runtime_arguments:
            description = f"{description}\n调用时必须提供非空 reason 作为传输说明。"
        definitions.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description[:1000],
                "parameters": schema,
            },
        })
    return definitions


def _parts(tool_call: dict[str, Any]) -> tuple[str, Any]:
    function = tool_call.get("function") or {}
    name = function.get("name") or tool_call.get("name") or ""
    arguments = function.get("arguments")
    if arguments is None:
        arguments = tool_call.get("args")
    return str(name), arguments


def _error(error: ValidationError) -> str:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    if not details:
        return "工具参数不符合 Schema"
    first = details[0]
    path = ".".join(str(item) for item in first.get("loc") or ()) or "root"
    error_type = str(first.get("type") or "invalid")
    return f"工具参数校验失败：{path}（{error_type}）"[:240]


def validate_tool_calls(
    tool_calls: list[dict[str, Any]],
    contracts: dict[str, ToolArgumentContract],
) -> ToolCallValidation:
    """Validate a whole call batch before any tool may execute."""
    normalized: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            return ToolCallValidation((), "工具调用格式无效")
        name, arguments = _parts(tool_call)
        contract = contracts.get(name)
        if contract is None:
            return ToolCallValidation((), f"未知工具：{name or 'missing'}"[:240])
        try:
            if isinstance(arguments, str):
                value = contract.visible_model.model_validate_json(arguments)
            elif isinstance(arguments, dict):
                value = contract.visible_model.model_validate(arguments)
            else:
                return ToolCallValidation((), f"工具 {name} 的参数必须是 JSON 对象"[:240])
        except ValidationError as error:
            return ToolCallValidation((), _error(error))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ToolCallValidation((), f"工具 {name} 的参数不是合法 JSON"[:240])
        runtime_arguments = {} if contract.no_runtime_arguments else value.model_dump(exclude_unset=True)
        normalized.append({
            **tool_call,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(runtime_arguments, ensure_ascii=False, separators=(",", ":")),
            },
        })
    return ToolCallValidation(tuple(normalized))
