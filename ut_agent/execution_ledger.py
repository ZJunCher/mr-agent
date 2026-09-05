"""Canonical message-derived execution facts for UT Agent repair runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolAttempt:
    name: str
    args: dict[str, Any]
    result: dict[str, Any] | None
    sequence: int
    result_text: str = ""


@dataclass
class ExecutionLedger:
    pushes: list[dict[str, Any]] = field(default_factory=list)
    pipelines: list[dict[str, Any]] = field(default_factory=list)
    tool_attempts: list[ToolAttempt] = field(default_factory=list)
    replan_requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def last_push(self) -> dict[str, Any] | None:
        candidates = [
            (index, push)
            for index, push in enumerate(self.pushes)
            if push.get("status") == "success" and push.get("changed") and push.get("commit_sha")
        ]
        if not candidates:
            return None

        def sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int]:
            index, push = item
            try:
                sequence = int(push.get("attempt_sequence") or 0)
            except (TypeError, ValueError):
                sequence = 0
            return sequence, index

        return max(candidates, key=sort_key)[1]

    @property
    def last_pushed_sha(self) -> str | None:
        push = self.last_push
        return str(push["commit_sha"]) if push is not None else None

    @property
    def failure_signatures(self) -> list[str]:
        from ut_agent.repair_progress import build_root_cause_groups

        signatures = []
        seen_pipelines = set()
        for result in self.pipelines:
            if result.get("pipeline_status") == "success":
                continue
            pipeline_identity = (
                result.get("matched_commit_sha")
                or result.get("requested_commit_sha")
                or result.get("pipeline_id")
            )
            if pipeline_identity in seen_pipelines:
                continue
            seen_pipelines.add(pipeline_identity)
            for group in build_root_cause_groups(result.get("failed_jobs") or []):
                signatures.append(group.root_cause_id)
        return signatures


def _tool_calls(message) -> list[dict]:
    return message.get("tool_calls", []) if isinstance(message, dict) else getattr(message, "tool_calls", [])


def _content(message) -> str:
    return str(message.get("content", "")) if isinstance(message, dict) else str(getattr(message, "content", ""))


def _tool_call_id(message) -> str:
    if isinstance(message, dict):
        return str(message.get("tool_call_id", ""))
    return str(getattr(message, "tool_call_id", ""))


def build_execution_ledger(messages: list) -> ExecutionLedger:
    """Normalize tool calls and results into one ordered, idempotent fact ledger."""
    ledger = ExecutionLedger()
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    push_positions: dict[str, int] = {}

    for sequence, message in enumerate(messages):
        for tool_call in _tool_calls(message):
            call_id = str(tool_call.get("id", ""))
            name = tool_call.get("name") or tool_call.get("function", {}).get("name", "")
            args = tool_call.get("args")
            if args is None:
                args = tool_call.get("function", {}).get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            if call_id and name:
                calls[call_id] = (name, args)

        call_id = _tool_call_id(message)
        call = calls.get(call_id)
        if not call:
            continue
        name, args = call
        result_text = _content(message)
        try:
            result = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            ledger.tool_attempts.append(ToolAttempt(
                name=name, args=args, result=None, sequence=sequence, result_text=result_text,
            ))
            continue
        if not isinstance(result, dict):
            ledger.tool_attempts.append(ToolAttempt(
                name=name, args=args, result=None, sequence=sequence, result_text=result_text,
            ))
            continue
        ledger.tool_attempts.append(ToolAttempt(
            name=name, args=args, result=result, sequence=sequence, result_text=result_text,
        ))
        if name == "commit_and_push_tool":
            attempt_id = str(result.get("attempt_id") or "")
            if attempt_id and attempt_id in push_positions:
                ledger.pushes[push_positions[attempt_id]] = result
            else:
                if attempt_id:
                    push_positions[attempt_id] = len(ledger.pushes)
                ledger.pushes.append(result)
        elif name in {"wait_pipeline_tool", "fetch_pipeline_logs_tool"}:
            ledger.pipelines.append({**result, "_sequence": sequence})
        elif name == "request_repair_replan_tool" and result.get("status") == "success":
            ledger.replan_requests.append({**result, "_sequence": sequence})

    return ledger
