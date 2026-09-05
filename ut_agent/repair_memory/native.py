"""Task-scoped Repair Memory adapter for the Native repair graph."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ut_agent.repair_memory.audit import (
    initialize_retrieval_audit,
    mark_retrieval_not_attempted,
    record_retrieval_error,
    record_retrieval_injection,
)
from ut_agent.repair_memory.config import load_repair_memory_settings, project_allowed
from ut_agent.repair_memory.models import RepairQuery, RetrievalMode
from ut_agent.repair_memory.prompt import render_historical_hints
from ut_agent.repair_memory.retrieve import classify_failure_family, retrieve_repair_hints
from ut_agent.repair_plan import active_work_item, latest_repair_plan
from ut_agent.repair_progress import diagnostic_fingerprint

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_:.\-/]{2,}")
_LANGUAGES = (
    ((".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"), "cpp"),
    ((".c", ".h"), "c"),
    ((".py",), "python"),
    ((".rs",), "rust"),
    ((".go",), "go"),
    ((".java",), "java"),
    ((".js", ".jsx", ".ts", ".tsx"), "javascript"),
)


class NativeRepairMemoryContext(BaseModel):
    """One immutable retrieval decision bound to one plan Work Item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=64, max_length=64)
    plan_version: int = Field(ge=1, le=50)
    work_item_id: str = Field(min_length=1, max_length=80)
    status: Literal["disabled", "shadow", "no_match", "injected", "error"]
    attempt_id: str = Field(default="", max_length=128)
    memory_ids: tuple[str, ...] = Field(default=(), max_length=3)
    prompt_block: str = Field(default="", max_length=2_000)
    error_code: str = Field(default="", max_length=100)
    created_at: str = Field(min_length=1, max_length=80)


def _language(paths: tuple[str, ...], evidence: tuple[str, ...]) -> str:
    values = tuple(value.lower() for value in (*paths, *evidence))
    for extensions, language in _LANGUAGES:
        if any(extension in value for value in values for extension in extensions):
            return language
    return "other"


def _build_system(job_names: tuple[str, ...], evidence: tuple[str, ...]) -> str:
    text = " ".join((*job_names, *evidence)).lower()
    for marker, value in (
        ("cmake", "cmake"),
        ("colcon", "colcon"),
        ("bazel", "bazel"),
        ("gradle", "gradle"),
        ("maven", "maven"),
        ("make", "make"),
    ):
        if marker in text:
            return value
    return "other"


def _causal_tokens(evidence: tuple[str, ...], hypothesis: str) -> tuple[str, ...]:
    values = []
    for source in (*evidence, hypothesis):
        for match in _TOKEN_RE.findall(source):
            token = match.strip(".:-/")
            if token and token.casefold() not in {"error", "failed", "failure", "with", "from"} and token not in values:
                values.append(token)
            if len(values) >= 20:
                return tuple(values)
    return tuple(values)


def build_native_repair_query(state: dict) -> RepairQuery:
    """Build a bounded query from the latest valid plan and active Work Item."""
    plan = latest_repair_plan(state)
    item = active_work_item(state)
    if plan is None or item is None:
        raise ValueError("active Native Repair Work Item is unavailable")
    evidence = tuple(item.failure_evidence)
    first_job = item.job_names[0] if item.job_names else ""
    return RepairQuery(
        project=plan.project_id,
        root_cause_group_id=item.work_item_id,
        source_pipeline_id=int(plan.source_pipeline_id or 0),
        source_sha=plan.source_commit_sha,
        failure_category=item.kind,
        job_family=first_job.casefold()[:80] or "other",
        failure_family=classify_failure_family(evidence),
        language=_language(item.allowed_paths, evidence),
        build_system=_build_system(item.job_names, evidence),
        diagnostic_fingerprint=diagnostic_fingerprint(evidence[0], job_name=first_job) if evidence else "",
        causal_tokens=_causal_tokens(evidence, item.hypothesis),
    )


def latest_native_memory_context(state: dict) -> NativeRepairMemoryContext | None:
    """Return only a valid event matching the current plan version and Work Item."""
    plan = latest_repair_plan(state)
    item = active_work_item(state)
    if plan is None or item is None:
        return None
    for raw in reversed(state.get("repair_memory_contexts") or ()):
        try:
            event = NativeRepairMemoryContext.model_validate(raw)
        except (TypeError, ValueError):
            continue
        if (
            event.plan_id == plan.plan_id
            and event.plan_version == plan.version
            and event.work_item_id == item.work_item_id
        ):
            return event
    return None


def native_memory_required(state: dict) -> bool:
    return active_work_item(state) is not None and latest_native_memory_context(state) is None


def native_memory_prompt(state: dict) -> str:
    event = latest_native_memory_context(state)
    return event.prompt_block if event is not None and event.status == "injected" else ""


def _task_id(state: dict, plan_id: str) -> str:
    explicit = str(state.get("task_id") or "").strip()
    if explicit:
        return explicit[:128]
    try:
        from pr_agent.distributed.runtime import get_execution_runtime

        runtime = get_execution_runtime()
        if runtime is not None and runtime.task_id:
            return str(runtime.task_id)[:128]
    except Exception:
        pass
    return f"native:{state.get('project_id', '')}:{state.get('mr_id', 0)}:{plan_id[:16]}"[:128]


async def repair_memory_node(state: dict) -> dict:
    """Retrieve once for the active Work Item and checkpoint the decision."""
    plan = latest_repair_plan(state)
    item = active_work_item(state)
    if plan is None or item is None:
        return {"repair_memory_contexts": []}
    created_at = datetime.now(timezone.utc).isoformat()
    identity = {
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "work_item_id": item.work_item_id,
        "created_at": created_at,
    }
    task_id = _task_id(state, plan.plan_id)
    try:
        settings = load_repair_memory_settings()
        query = build_native_repair_query(state)
        initialize_retrieval_audit(
            task_id=task_id,
            project=plan.project_id,
            mr_iid=plan.mr_id,
            source_pipeline_id=query.source_pipeline_id,
            source_sha=query.source_sha,
            mode=settings.retrieval_mode,
            reason_code="native_work_item_not_reached",
        )
        if settings.retrieval_mode is RetrievalMode.OFF or not project_allowed(
            plan.project_id, settings.project_allowlist
        ):
            reason = "memory_mode_off" if settings.retrieval_mode is RetrievalMode.OFF else "project_not_allowed"
            mark_retrieval_not_attempted(task_id, mode=settings.retrieval_mode, reason_code=reason)
            event = NativeRepairMemoryContext(status="disabled", **identity)
            return {"repair_memory_contexts": [event.model_dump(mode="json")]}

        retrieval = await asyncio.to_thread(
            retrieve_repair_hints,
            query=query,
            task_id=task_id,
            mode=settings.retrieval_mode,
        )
        prompt_block = ""
        status: Literal["shadow", "no_match", "injected"] = "no_match"
        if retrieval.mode is RetrievalMode.SHADOW:
            status = "shadow"
        elif retrieval.mode is RetrievalMode.INJECT and retrieval.hints:
            prompt_block = render_historical_hints(retrieval.hints, retrieval.max_prompt_chars)
            if prompt_block:
                status = "injected"
                record_retrieval_injection(task_id, retrieval.attempt_id, len(retrieval.hints))
        event = NativeRepairMemoryContext(
            status=status,
            attempt_id=retrieval.attempt_id,
            memory_ids=tuple(hint.memory_id for hint in retrieval.hints),
            prompt_block=prompt_block,
            **identity,
        )
    except Exception as error:
        record_retrieval_error(task_id, error_code=type(error).__name__)
        event = NativeRepairMemoryContext(status="error", error_code=type(error).__name__, **identity)
    return {"repair_memory_contexts": [event.model_dump(mode="json")]}
