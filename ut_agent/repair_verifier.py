"""Independent semantic Verifier for a validated Native Repair Diff."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ut_agent.config import MODEL_CANDIDATES
from ut_agent.execution_ledger import build_execution_ledger
from ut_agent.native_repair_state import build_native_repair_evidence, evaluate_native_commit
from ut_agent.repair_plan import (
    RepairVerification,
    active_work_item,
    latest_failed_pipeline,
    latest_repair_plan,
    plan_scoped_attempts,
    required_verification_work_item_ids,
)
from ut_agent.structured_output import call_structured_output

MAX_VERIFIER_DIFF_CHARS = 120_000


class VerifierOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["pass", "replan", "block"]
    causal_alignment: bool
    scope_compliant: bool
    evidence_sufficient: bool
    covered_work_item_ids: tuple[str, ...] = Field(max_length=20)
    reason: str = Field(min_length=1, max_length=1_000)
    risks: tuple[str, ...] = Field(default=(), max_length=10)

    @field_validator("covered_work_item_ids")
    @classmethod
    def _unique_coverage(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("covered Work Item identities must be unique")
        return values


def _verification_event(
    state: dict,
    *,
    verdict: Literal["pass", "replan", "block"],
    reason: str,
    diff_digest: str = "",
    causal_alignment: bool = False,
    scope_compliant: bool = False,
    evidence_sufficient: bool = False,
    covered_work_item_ids: tuple[str, ...] = (),
    risks: tuple[str, ...] = (),
    model: str = "",
    error_code: str = "",
) -> dict:
    plan = latest_repair_plan(state)
    item = active_work_item(state)
    if plan is None:
        return {}
    effective_coverage = covered_work_item_ids
    if verdict == "block" and item is not None:
        effective_coverage = (item.work_item_id,)
    return RepairVerification(
        plan_id=plan.plan_id,
        lineage_id=plan.lineage_id,
        plan_version=plan.version,
        work_item_id=item.work_item_id if item is not None else "plan",
        baseline_sha=plan.baseline_sha,
        diff_digest=diff_digest,
        verdict=verdict,
        causal_alignment=causal_alignment,
        scope_compliant=scope_compliant,
        evidence_sufficient=evidence_sufficient,
        covered_work_item_ids=effective_coverage,
        reason=" ".join(str(reason or "Verifier rejected the repair.").split())[:1_000],
        risks=tuple(" ".join(str(risk).split())[:500] for risk in risks[:10] if str(risk).strip()),
        model=model,
        error_code=error_code,
        created_at=datetime.now(timezone.utc).isoformat(),
    ).model_dump(mode="json")


def _complete_diff(state: dict, base_sha: str, diff_digest: str, last_patch_sequence: int) -> str:
    ledger = build_execution_ledger(state.get("messages", []))
    line_map: dict[int, str] = {}
    total_lines = 0
    for attempt in ledger.tool_attempts:
        result = attempt.result or {}
        if (
            attempt.sequence <= last_patch_sequence
            or attempt.name != "inspect_repo_diff_tool"
            or result.get("status") != "ok"
            or str(result.get("base_sha") or "") != base_sha
            or str(result.get("diff_digest") or "") != diff_digest
        ):
            continue
        page = result.get("page") if isinstance(result.get("page"), dict) else {}
        try:
            start = int(page.get("start_line") or 0)
            end = int(page.get("end_line") or 0)
            page_total = int(result.get("total_lines") or 0)
        except (TypeError, ValueError):
            continue
        lines = str(result.get("diff") or "").splitlines()
        if start < 1 or end < start or len(lines) != end - start + 1:
            continue
        if total_lines not in {0, page_total}:
            raise ValueError("inconsistent Diff page totals")
        total_lines = page_total
        for offset, line in enumerate(lines):
            line_map[start + offset] = line
    if total_lines <= 0 or any(number not in line_map for number in range(1, total_lines + 1)):
        raise ValueError("complete Diff pages are unavailable")
    value = "\n".join(line_map[number] for number in range(1, total_lines + 1))
    if len(value) > MAX_VERIFIER_DIFF_CHARS:
        raise OverflowError("complete Diff exceeds verifier budget")
    return value


def _required_coverage(state: dict) -> tuple[str, ...]:
    return required_verification_work_item_ids(state)


def _verifier_payload(state: dict, diff_text: str, required_coverage: tuple[str, ...]) -> str:
    plan = latest_repair_plan(state)
    item = active_work_item(state)
    pipeline, ledger = latest_failed_pipeline(state.get("messages", []))
    validation = next((
        attempt.result for attempt in reversed(ledger.tool_attempts)
        if attempt.name == "run_repo_validation_tool" and isinstance(attempt.result, dict)
    ), {}) or {}
    executed_checks = [{
        "name": str(check.get("name") or check.get("check") or ""),
        "passed": check.get("passed") is True,
        "exit_code": check.get("exit_code"),
        "timed_out": check.get("timed_out") is True,
    } for check in validation.get("executed_checks") or () if isinstance(check, dict)][:10]
    return json.dumps({
        "plan": {
            "plan_id": plan.plan_id,
            "version": plan.version,
            "baseline_sha": plan.baseline_sha,
        },
        "work_items": [work_item.model_dump(mode="json") for work_item in plan.work_items],
        "active_work_item": item.model_dump(mode="json"),
        "required_coverage": list(required_coverage),
        "failed_jobs": (pipeline or {}).get("failed_jobs") or [],
        "validation": {
            "required_checks": list(validation.get("required_checks") or ())[:10],
            "executed_checks": executed_checks,
            "all_passed": validation.get("all_passed") is True,
        },
        "diff": diff_text,
    }, ensure_ascii=False)


async def repair_verifier_node(state: dict) -> dict:
    """Append a strict fail-closed verdict after deterministic Native validation."""
    plan = latest_repair_plan(state)
    item = active_work_item(state)
    if plan is None or item is None:
        return {}
    ledger = build_execution_ledger(state.get("messages", []))
    scoped_attempts = plan_scoped_attempts(state, ledger)
    native = evaluate_native_commit(scoped_attempts)
    evidence = build_native_repair_evidence(scoped_attempts)
    if not native.allowed:
        event = _verification_event(
            state,
            verdict="block",
            reason=native.message,
            diff_digest=native.validated_diff_digest,
            error_code=native.error_code,
        )
        return {"repair_verifications": [event]}
    if native.validated_base_sha != plan.baseline_sha:
        event = _verification_event(
            state,
            verdict="block",
            reason="Validated worktree baseline does not match the current RepairPlan.",
            diff_digest=native.validated_diff_digest,
            error_code="repair_baseline_mismatch",
        )
        return {"repair_verifications": [event]}
    try:
        diff_text = _complete_diff(
            state,
            native.validated_base_sha,
            native.validated_diff_digest,
            evidence.last_patch_sequence,
        )
    except OverflowError as error:
        event = _verification_event(
            state,
            verdict="block",
            reason=str(error),
            diff_digest=native.validated_diff_digest,
            error_code="verifier_diff_over_budget",
        )
        return {"repair_verifications": [event]}
    except ValueError as error:
        event = _verification_event(
            state,
            verdict="block",
            reason=str(error),
            diff_digest=native.validated_diff_digest,
            error_code="verifier_diff_incomplete",
        )
        return {"repair_verifications": [event]}

    active_model = str(state.get("active_model") or "")
    candidates = tuple(model for model in MODEL_CANDIDATES if model and model != active_model)
    if not candidates:
        event = _verification_event(
            state,
            verdict="block",
            reason="No model route independent from the ReAct executor is configured.",
            diff_digest=native.validated_diff_digest,
            error_code="independent_model_unavailable",
        )
        return {"repair_verifications": [event]}

    required_coverage = _required_coverage(state)
    system = (
        "You are an independent, read-only repair verifier. Judge whether the complete Diff causally fixes the "
        "recorded CI failure, stays within the Work Item paths, and has enough validation evidence. "
        "Use pass only when all three booleans are true and covered_work_item_ids includes required_coverage. "
        "You may include additional RepairPlan Work Items in covered_work_item_ids only when the cumulative Diff "
        "and executed validation directly support each additional item. "
        "Use replan for a repository-local fix needing new evidence; use block when automation should stop."
    )
    try:
        outcome = await call_structured_output(
            system,
            _verifier_payload(state, diff_text, required_coverage),
            output_model=VerifierOutput,
            tool_name="submit_repair_verification",
            tool_description="Submit the strict independent repair verdict.",
            model=candidates[0],
            max_tokens=1_500,
        )
    except Exception as error:
        event = _verification_event(
            state,
            verdict="block",
            reason=f"Verifier call failed: {type(error).__name__}",
            diff_digest=native.validated_diff_digest,
            error_code="verifier_model_unavailable",
        )
        return {"repair_verifications": [event]}
    selected_model = str(outcome.model or "")
    if selected_model == active_model:
        event = _verification_event(
            state,
            verdict="block",
            reason="Verifier did not use an independent configured model route.",
            diff_digest=native.validated_diff_digest,
            model=selected_model,
            error_code="independent_model_unavailable",
        )
        return {"repair_verifications": [event]}
    if outcome.value is None:
        code = "verifier_model_unavailable" if outcome.terminal_error else "verifier_protocol_invalid"
        reason = outcome.terminal_error or outcome.validation_error or "Verifier returned invalid structured output."
        event = _verification_event(
            state,
            verdict="block",
            reason=reason,
            diff_digest=native.validated_diff_digest,
            model=selected_model,
            error_code=code,
        )
        return {"repair_verifications": [event]}
    if selected_model not in candidates:
        event = _verification_event(
            state,
            verdict="block",
            reason="Verifier did not use an independent configured model route.",
            diff_digest=native.validated_diff_digest,
            model=selected_model,
            error_code="independent_model_unavailable",
        )
        return {"repair_verifications": [event]}

    value = outcome.value
    work_items = {work_item.work_item_id: work_item for work_item in plan.work_items}
    safe_covered = tuple(
        work_item_id
        for work_item_id in value.covered_work_item_ids
        if work_item_id in work_items
    )
    unknown_covered = set(value.covered_work_item_ids) - set(safe_covered)

    def canonical_check(name: str) -> str:
        return "test_check" if name == "unit_test_check" else name

    executed_checks = {canonical_check(name) for name in evidence.executed_checks}
    missing_checks = {
        canonical_check(check)
        for work_item_id in safe_covered
        for check in work_items[work_item_id].required_checks
        if canonical_check(check) not in executed_checks
    }
    covered = set(safe_covered)
    pass_valid = (
        value.verdict == "pass"
        and value.causal_alignment
        and value.scope_compliant
        and value.evidence_sufficient
        and set(required_coverage).issubset(covered)
        and not unknown_covered
        and not missing_checks
    )
    verdict = "pass" if pass_valid else (value.verdict if value.verdict != "pass" else "replan")
    if pass_valid:
        error_code = ""
    elif unknown_covered:
        error_code = "repair_verification_unknown_work_items"
    elif missing_checks:
        error_code = "repair_verification_checks_missing"
    else:
        error_code = "repair_verification_rejected"
    event = _verification_event(
        state,
        verdict=verdict,
        reason=value.reason,
        diff_digest=native.validated_diff_digest,
        causal_alignment=value.causal_alignment,
        scope_compliant=value.scope_compliant,
        evidence_sufficient=value.evidence_sufficient,
        covered_work_item_ids=safe_covered,
        risks=value.risks,
        model=selected_model,
        error_code=error_code,
    )
    return {"repair_verifications": [event]}
