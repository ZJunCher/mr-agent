"""Dedicated LiteLLM tool-calling client for Prompt evolution.

Kept injectable (``completion`` parameter) so tests stay network-free. The
production factory instantiates ``LiteLLMAIHandler()`` once before using this
client so existing LiteLLM keys/base settings are loaded.
"""
from __future__ import annotations

import asyncio

from pydantic import BaseModel, ValidationError

from pr_agent.config_loader import get_settings
from ut_agent.model_failover import ModelAttempt, classify_model_failure, ordered_candidates


class PromptEvolutionModelExhausted(RuntimeError):
    """All configured model routes failed with switchable provider errors."""

    def __init__(self, attempts: tuple[ModelAttempt, ...]):
        self.attempts = attempts
        models = ", ".join(dict.fromkeys(attempt.model for attempt in attempts)) or "none"
        codes = ", ".join(dict.fromkeys(attempt.failure_code for attempt in attempts)) or "unavailable"
        super().__init__(f"prompt evolution models exhausted: models={models}; failures={codes}")


class PromptEvolutionToolProtocolError(ValueError):
    """The model returned no single call to the required result tool."""


class PromptEvolutionToolSchemaError(ValueError):
    """The model called the required tool with arguments outside its schema."""


class _LocalHealthStore:
    def candidate_allowed(self, model: str, owner: str) -> bool:
        return True

    def mark_failed(self, model: str, owner: str, failure) -> None:
        return None

    def mark_succeeded(self, model: str, owner: str) -> None:
        return None


def _sanitize_reason(value: str, limit: int = 300) -> str:
    return " ".join(str(value).split())[:limit]


def _backoff_seconds(attempt_number: int) -> float:
    return min(8.0, float(2 ** (attempt_number - 1)))


class ToolCallingModelClient:
    def __init__(self, completion=None, *, models: tuple[str, ...] = (), attempts_per_model: int = 1,
                 health_store=None, owner: str = "prompt-evolution", sleep=asyncio.sleep):
        if completion is None:
            from litellm import acompletion
            completion = acompletion
        self._completion = completion
        self.models = tuple(models)
        self.attempts_per_model = max(1, int(attempts_per_model))
        self.health_store = health_store or _LocalHealthStore()
        self.owner = owner
        self.sleep = sleep

    async def call(self, model: str, system: str, user: str, tool_name: str,
                   result_model: type[BaseModel]) -> BaseModel:
        attempts: list[ModelAttempt] = []
        configured_models = self.models or (model,)
        candidates = ordered_candidates(configured_models, model)
        for candidate in candidates:
            if not self.health_store.candidate_allowed(candidate, self.owner):
                attempts.append(ModelAttempt(candidate, "cooldown", "shared cooldown"))
                continue
            for attempt_number in range(1, self.attempts_per_model + 1):
                try:
                    result = await self._call_once(candidate, system, user, tool_name, result_model)
                except (PromptEvolutionToolProtocolError, PromptEvolutionToolSchemaError) as exc:
                    code = "tool_protocol_error" if isinstance(exc, PromptEvolutionToolProtocolError) else "tool_schema_error"
                    attempts.append(ModelAttempt(candidate, code, _sanitize_reason(str(exc))))
                    if candidate == candidates[-1]:
                        raise
                    break
                except Exception as exc:
                    failure = classify_model_failure(exc)
                    attempts.append(ModelAttempt(candidate, failure.code, _sanitize_reason(failure.reason)))
                    if not failure.switchable:
                        raise
                    self.health_store.mark_failed(candidate, self.owner, failure)
                    if attempt_number < self.attempts_per_model:
                        await self.sleep(_backoff_seconds(attempt_number))
                    continue
                self.health_store.mark_succeeded(candidate, self.owner)
                return result
        raise PromptEvolutionModelExhausted(tuple(attempts))

    async def call_pair_same_model(
        self,
        model: str,
        system: str,
        first_user: str,
        second_user: str,
        tool_name: str,
        result_model: type[BaseModel],
    ) -> tuple[BaseModel, BaseModel, str]:
        """Return a complete pair produced by one model, restarting after partial failure."""
        attempts: list[ModelAttempt] = []
        configured_models = self.models or (model,)
        candidates = ordered_candidates(configured_models, model)
        for candidate_index, candidate in enumerate(candidates):
            if not self.health_store.candidate_allowed(candidate, self.owner):
                attempts.append(ModelAttempt(candidate, "cooldown", "shared cooldown"))
                continue
            for attempt_number in range(1, self.attempts_per_model + 1):
                try:
                    first = await self._call_once(candidate, system, first_user, tool_name, result_model)
                    second = await self._call_once(candidate, system, second_user, tool_name, result_model)
                except (PromptEvolutionToolProtocolError, PromptEvolutionToolSchemaError) as exc:
                    code = (
                        "tool_protocol_error"
                        if isinstance(exc, PromptEvolutionToolProtocolError)
                        else "tool_schema_error"
                    )
                    attempts.append(ModelAttempt(candidate, code, _sanitize_reason(str(exc))))
                    if candidate_index == len(candidates) - 1:
                        raise
                    break
                except Exception as exc:
                    failure = classify_model_failure(exc)
                    attempts.append(ModelAttempt(candidate, failure.code, _sanitize_reason(failure.reason)))
                    if not failure.switchable:
                        raise
                    self.health_store.mark_failed(candidate, self.owner, failure)
                    if attempt_number < self.attempts_per_model:
                        await self.sleep(_backoff_seconds(attempt_number))
                    continue
                self.health_store.mark_succeeded(candidate, self.owner)
                return first, second, candidate
        raise PromptEvolutionModelExhausted(tuple(attempts))

    async def _call_once(self, model: str, system: str, user: str, tool_name: str,
                         result_model: type[BaseModel]) -> BaseModel:
        response = await self._completion(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[{
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": f"Return validated {tool_name} output",
                    "parameters": result_model.model_json_schema(),
                },
            }],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            timeout=get_settings().config.ai_timeout,
        )
        choices = response.get("choices") if isinstance(response, dict) else response.choices
        message = choices[0].get("message") if isinstance(choices[0], dict) else choices[0].message
        calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
        calls = list(calls or [])
        function = (
            calls[0].get("function") if calls and isinstance(calls[0], dict)
            else (calls[0].function if calls else None)
        )
        name = (
            function.get("name") if isinstance(function, dict)
            else (function.name if function else "")
        )
        arguments = (
            function.get("arguments") if isinstance(function, dict)
            else (function.arguments if function else "")
        )
        if len(calls) != 1 or name != tool_name:
            raise PromptEvolutionToolProtocolError(f"expected exactly one {tool_name} tool call")
        try:
            return result_model.model_validate_json(arguments)
        except ValidationError as exc:
            raise PromptEvolutionToolSchemaError(
                f"{tool_name} arguments did not match the required schema"
            ) from exc
