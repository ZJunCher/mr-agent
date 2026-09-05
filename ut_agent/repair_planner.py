"""Read-only Planner node for Native Pipeline repair."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ut_agent.llm import call_llm_outcome
from ut_agent.repair_plan import (
    RepairPlan,
    active_work_item,
    build_initial_repair_plan,
    latest_repair_plan,
    normalize_repair_path,
    plan_identity_for_revision,
    plan_matches_latest_pipeline,
    repair_plan_required,
)


class PlannerHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_item_id: str = Field(min_length=1, max_length=80)
    hypothesis: str = Field(min_length=1, max_length=1_000)


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hypotheses: tuple[PlannerHypothesis, ...] = Field(max_length=20)

    @field_validator("hypotheses")
    @classmethod
    def _unique_ids(cls, values: tuple[PlannerHypothesis, ...]) -> tuple[PlannerHypothesis, ...]:
        ids = [value.work_item_id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("planner Work Item identities must be unique")
        return values


def _json_object(text: str) -> dict:
    value = str(text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("planner output must be a JSON object")
    return decoded


async def _initial_plan(state: dict) -> RepairPlan:
    fallback = build_initial_repair_plan(state)
    pending_items = tuple(item for item in fallback.work_items if item.status == "pending")
    if not pending_items:
        return fallback
    evidence = [{
        "work_item_id": item.work_item_id,
        "jobs": list(item.job_names),
        "failure_evidence": list(item.failure_evidence),
        "allowed_paths": list(item.allowed_paths),
    } for item in pending_items]
    system = (
        "You are a read-only CI repair planner. Return only strict JSON. "
        "For each supplied work_item_id, write a concise causal hypothesis grounded only in the supplied evidence. "
        "Schema: {\"hypotheses\":[{\"work_item_id\":\"...\",\"hypothesis\":\"...\"}]}."
    )
    try:
        outcome = await call_llm_outcome(system, json.dumps(evidence, ensure_ascii=False), max_tokens=1_200)
        if outcome.terminal_error or not outcome.text:
            raise ValueError(outcome.terminal_error or "empty planner output")
        output = PlannerOutput.model_validate(_json_object(outcome.text))
        expected_ids = {item.work_item_id for item in pending_items}
        hypotheses = {
            value.work_item_id: value.hypothesis
            for value in output.hypotheses
            if value.work_item_id in expected_ids
        }
        if set(hypotheses) != expected_ids:
            raise ValueError("planner output did not cover every Work Item")
        return build_initial_repair_plan(
            state,
            hypotheses=hypotheses,
            planning_mode="model",
            planner_model=outcome.model,
        )
    except Exception as error:
        return fallback.model_copy(update={
            "planner_error_code": f"planner_fallback_{type(error).__name__.lower()}",
        })


def _latest_replan_request(state: dict, plan: RepairPlan) -> dict | None:
    from ut_agent.execution_ledger import build_execution_ledger

    ledger = build_execution_ledger(state.get("messages", []))
    return next((
        request for request in reversed(ledger.replan_requests)
        if request.get("plan_id") == plan.plan_id
        and request.get("lineage_id") == plan.lineage_id
        and request.get("expected_version") == plan.version
        and int(request.get("_sequence", -1)) > plan.evidence_cursor
    ), None)


def _verifier_replan_request(state: dict, plan: RepairPlan) -> dict | None:
    from ut_agent.execution_ledger import build_execution_ledger
    from ut_agent.repair_plan import latest_repair_verification

    verification = latest_repair_verification(state)
    if (
        verification is None
        or verification.plan_id != plan.plan_id
        or verification.plan_version != plan.version
        or verification.verdict != "replan"
    ):
        return None
    ledger = build_execution_ledger(state.get("messages", []))
    return {
        "work_item_id": verification.work_item_id,
        "reason": verification.reason,
        "hypothesis": verification.reason,
        "proposed_paths": [],
        "_sequence": max(
            (attempt.sequence for attempt in ledger.tool_attempts),
            default=plan.evidence_cursor,
        ),
    }


def build_revised_repair_plan(state: dict, plan: RepairPlan, request: dict) -> RepairPlan:
    if plan.version >= 50:
        raise ValueError("RepairPlan version limit reached")
    current_plan = latest_repair_plan(state)
    current_item = active_work_item(state)
    if current_plan is None or current_plan.plan_id != plan.plan_id:
        raise ValueError("replan is not based on the latest RepairPlan")
    requested_id = str(request.get("work_item_id") or "")
    if current_item is None or requested_id != current_item.work_item_id:
        raise ValueError("replan does not target the active Work Item")
    evidence_cursor = int(request.get("_sequence", plan.evidence_cursor))
    if evidence_cursor <= plan.evidence_cursor:
        raise ValueError("replan has no evidence newer than the current plan")
    proposed_paths = tuple(
        dict.fromkeys(normalize_repair_path(str(path)) for path in request.get("proposed_paths") or ())
    )
    revised_items = tuple(
        item.model_copy(update={
            "hypothesis": str(request.get("hypothesis") or item.hypothesis)[:1_000],
            "allowed_paths": tuple(dict.fromkeys((*item.allowed_paths, *proposed_paths))),
        }) if item.work_item_id == requested_id else item
        for item in plan.work_items
    )
    version = plan.version + 1
    return RepairPlan.model_validate({
        **plan.model_dump(mode="json"),
        "plan_id": plan_identity_for_revision(plan, revised_items, version),
        "version": version,
        "evidence_cursor": evidence_cursor,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "revision_reason": str(request.get("reason") or "new_repair_evidence")[:500],
        "planning_mode": "deterministic_fallback",
        "planner_model": "",
        "planner_error_code": "",
        "work_items": revised_items,
    })


async def repair_planner_node(state: dict) -> dict:
    """Append Plan v1 or exactly one evidence-backed successor event."""
    if repair_plan_required(state):
        plan = await _initial_plan(state)
        return {"repair_plans": [plan.model_dump(mode="json")]}
    plan = latest_repair_plan(state)
    if plan is None or not plan_matches_latest_pipeline(state, plan):
        return {}
    request = _latest_replan_request(state, plan) or _verifier_replan_request(state, plan)
    if request is None:
        return {}
    try:
        revised = build_revised_repair_plan(state, plan, request)
    except (TypeError, ValueError, ValidationError):
        return {}
    return {"repair_plans": [revised.model_dump(mode="json")]}
