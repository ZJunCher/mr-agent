"""Pure Pipeline failure reconciliation and Native repair-attribution helpers."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any


MAX_RECONCILIATION_ITEMS = 20
MAX_OBSERVED_JOBS = 100
_TERMINAL_PIPELINE_STATUSES = {"success", "failed", "canceled", "skipped"}


def _bounded_unique_strings(values: Any) -> tuple[tuple[str, ...], bool]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return (), False
    result = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in result:
            continue
        if len(result) >= MAX_RECONCILIATION_ITEMS:
            return tuple(result), True
        result.append(normalized)
    return tuple(result), False


def _root_map_with_truncation(
    pipeline: dict | None,
) -> tuple[dict[str, tuple[str, ...]], bool, frozenset[str]]:
    if not isinstance(pipeline, dict):
        return {}, False, frozenset()
    groups = [group for group in pipeline.get("root_cause_groups") or () if isinstance(group, dict)]
    if not groups:
        from ut_agent.repair_progress import build_root_cause_groups

        groups = [group.to_dict() for group in build_root_cause_groups(pipeline.get("failed_jobs") or [])]

    roots = {}
    truncated = False
    truncated_job_roots = set()
    for group in groups:
        root_id = str(group.get("root_cause_id") or "").strip()
        if not root_id or root_id in roots:
            continue
        if len(roots) >= MAX_RECONCILIATION_ITEMS:
            truncated = True
            continue
        job_names, job_names_truncated = _bounded_unique_strings(group.get("job_names") or ())
        roots[root_id] = job_names
        if job_names_truncated:
            truncated = True
            truncated_job_roots.add(root_id)
    return roots, truncated, frozenset(truncated_job_roots)


def _root_map(pipeline: dict | None) -> dict[str, tuple[str, ...]]:
    return _root_map_with_truncation(pipeline)[0]


def _pipeline_id(pipeline: dict | None) -> int | str | None:
    if not isinstance(pipeline, dict):
        return None
    value = pipeline.get("validation_pipeline_id") or pipeline.get("pipeline_id")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _commit_sha(pipeline: dict | None, key: str) -> str:
    return str(pipeline.get(key) or "") if isinstance(pipeline, dict) else ""


def _statuses_by_name(observed_jobs: Any) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    if not isinstance(observed_jobs, (list, tuple)):
        return result
    for job in observed_jobs[:MAX_OBSERVED_JOBS]:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or job.get("job_name") or "").strip()
        status = str(job.get("status") or "").strip().lower()
        if name and status:
            result.setdefault(name, set()).add(status)
    return result


def _missing_root_status(
    previous_job_names: tuple[str, ...],
    current_job_statuses: dict[str, set[str]],
    current_roots: dict[str, tuple[str, ...]],
    *,
    previous_job_names_truncated: bool = False,
) -> str:
    if not previous_job_names or previous_job_names_truncated:
        return "unknown"
    statuses = [current_job_statuses.get(name, set()) for name in previous_job_names]
    if all(values == {"success"} for values in statuses):
        return "resolved"
    current_root_jobs = {name for names in current_roots.values() for name in names}
    if any("failed" in values and name in current_root_jobs for name, values in zip(previous_job_names, statuses)):
        return "superseded"
    return "unknown"


def reconcile_pipeline_failures(previous: dict | None, current: dict) -> dict:
    """Compare two exact-SHA Pipeline observations without model inference."""
    previous_roots, previous_truncated, previous_job_names_truncated = _root_map_with_truncation(previous)
    current_roots, current_truncated, _current_job_names_truncated = _root_map_with_truncation(current)
    job_statuses = _statuses_by_name(current.get("observed_jobs") or ())
    observed_jobs_truncated = bool(current.get("observed_jobs_truncated"))
    evidence_truncated = previous_truncated or current_truncated or observed_jobs_truncated
    # Previous roots come first so the bounded payload always retains the outcome
    # of every root that may already have a retry streak. New roots remain present
    # in the current Pipeline's root_cause_groups even if this display list truncates.
    root_ids = [
        *sorted(previous_roots),
        *sorted(set(current_roots) - set(previous_roots)),
    ]
    selected_root_ids = root_ids[:MAX_RECONCILIATION_ITEMS]

    transitions = []
    for root_id in selected_root_ids:
        in_previous = root_id in previous_roots
        in_current = root_id in current_roots
        if in_previous and in_current:
            status = "persistent"
        elif in_current:
            status = "introduced"
        else:
            status = _missing_root_status(
                previous_roots[root_id],
                job_statuses,
                current_roots,
                previous_job_names_truncated=(
                    evidence_truncated or root_id in previous_job_names_truncated
                ),
            )
        transitions.append({
            "root_cause_id": root_id,
            "status": status,
            "previous_job_names": list(previous_roots.get(root_id, ())),
            "current_job_names": list(current_roots.get(root_id, ())),
        })

    return {
        "previous_pipeline_id": _pipeline_id(previous),
        "previous_requested_commit_sha": _commit_sha(previous, "requested_commit_sha"),
        "previous_matched_commit_sha": _commit_sha(previous, "matched_commit_sha"),
        "current_pipeline_id": _pipeline_id(current),
        "current_requested_commit_sha": _commit_sha(current, "requested_commit_sha"),
        "current_matched_commit_sha": _commit_sha(current, "matched_commit_sha"),
        "transitions": transitions,
        "transitions_truncated": (
            previous_truncated
            or current_truncated
            or observed_jobs_truncated
            or len(root_ids) > MAX_RECONCILIATION_ITEMS
        ),
        "current_observed_jobs_truncated": observed_jobs_truncated,
    }


def _job_value(job: Any, *keys: str) -> Any:
    if isinstance(job, dict):
        return next((job.get(key) for key in keys if job.get(key) not in (None, "")), None)
    return next((getattr(job, key, None) for key in keys if getattr(job, key, None) not in (None, "")), None)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def observed_jobs_from_group_jobs(group_jobs: Iterable[tuple[int, Any]]) -> list[dict]:
    """Return bounded JSON-safe Job observations from a selected Pipeline group."""
    observations: dict[tuple[int | None, int | None, str], dict] = {}
    for raw_pipeline_id, job in group_jobs:
        pipeline_id = _optional_int(raw_pipeline_id)
        job_id = _optional_int(_job_value(job, "id", "job_id"))
        name = str(_job_value(job, "name", "job_name") or "unknown").strip()[:300]
        status = str(_job_value(job, "status") or "unknown").strip().lower()[:40]
        identity = (pipeline_id, job_id, name)
        observations[identity] = {
            "pipeline_id": pipeline_id,
            "job_id": job_id,
            "name": name,
            "status": status,
        }

    def sort_key(item: dict) -> tuple[int, str, int]:
        return int(item["pipeline_id"] or -1), item["name"], int(item["job_id"] or -1)

    return sorted(observations.values(), key=sort_key)[:MAX_OBSERVED_JOBS]


def _stored_transition_statuses(previous: dict, current: dict) -> dict[str, str]:
    """Read exact-pair transition facts retained before context compaction."""
    raw = current.get("failure_reconciliation")
    if not isinstance(raw, dict):
        return {}
    if (
        raw.get("previous_pipeline_id") != _pipeline_id(previous)
        or raw.get("current_pipeline_id") != _pipeline_id(current)
        or str(raw.get("previous_requested_commit_sha") or "") != _commit_sha(previous, "requested_commit_sha")
        or str(raw.get("previous_matched_commit_sha") or "") != _commit_sha(previous, "matched_commit_sha")
        or str(raw.get("current_requested_commit_sha") or "") != _commit_sha(current, "requested_commit_sha")
        or str(raw.get("current_matched_commit_sha") or "") != _commit_sha(current, "matched_commit_sha")
    ):
        return {}
    return {
        str(item.get("root_cause_id") or ""): str(item.get("status") or "")
        for item in raw.get("transitions") or ()
        if isinstance(item, dict)
    }


def _passing_verification_coverage(state: dict) -> dict[tuple[str, str], set[str]]:
    from ut_agent.repair_plan import RepairPlan, RepairVerification, verification_matches_plan

    plans = {}
    for raw_plan in state.get("repair_plans") or ():
        try:
            plan = RepairPlan.model_validate(raw_plan)
        except (TypeError, ValueError):
            continue
        key = (plan.plan_id, plan.lineage_id, plan.version, plan.baseline_sha)
        plans[key] = plan

    result: dict[tuple[str, str], set[str]] = {}
    for raw_verification in state.get("repair_verifications") or ():
        try:
            verification = RepairVerification.model_validate(raw_verification)
        except (TypeError, ValueError):
            continue
        if (
            verification.verdict != "pass"
            or not verification.causal_alignment
            or not verification.scope_compliant
            or not verification.evidence_sufficient
        ):
            continue
        plan = plans.get((
            verification.plan_id,
            verification.lineage_id,
            verification.plan_version,
            verification.baseline_sha,
        ))
        covered_ids = set(verification.covered_work_item_ids)
        if (
            plan is None
            or not verification_matches_plan(plan, verification)
            or not verification.diff_digest
        ):
            continue
        result.setdefault(
            (verification.baseline_sha, verification.diff_digest),
            set(),
        ).update(covered_ids)
    return result


def _successful_push_attempts(state: dict) -> list[Any]:
    from ut_agent.execution_ledger import build_execution_ledger

    ledger = build_execution_ledger(state.get("messages") or [])
    result = []
    seen_attempt_ids: set[str] = set()
    seen_commit_shas: set[str] = set()
    for attempt in ledger.tool_attempts:
        push = attempt.result or {}
        if (
            attempt.name != "commit_and_push_tool"
            or push.get("status") != "success"
            or push.get("changed") is not True
            or not push.get("commit_sha")
        ):
            continue
        attempt_id = str(push.get("attempt_id") or "")
        commit_sha = str(push.get("commit_sha") or "")
        if (attempt_id and attempt_id in seen_attempt_ids) or commit_sha in seen_commit_shas:
            continue
        if attempt_id:
            seen_attempt_ids.add(attempt_id)
        seen_commit_shas.add(commit_sha)
        result.append(attempt)
    return result


def _latest_exact_terminal_pipeline(pipelines: list[dict], push_attempt: Any) -> dict | None:
    """Bind a push to its latest exact terminal observation.

    A GitLab retry may update the same Pipeline and SHA after an earlier terminal
    observation.  Proofs are still applied in push order, so replaying an older
    commit later cannot reorder the repair history.
    """
    push = push_attempt.result or {}
    commit_sha = str(push.get("commit_sha") or "")
    attempt_id = str(push.get("attempt_id") or "")
    candidates = []
    for pipeline in pipelines:
        pipeline_attempt_id = str(pipeline.get("attempt_id") or "")
        if (
            int(pipeline.get("_sequence") or 0) <= int(push_attempt.sequence)
            or str(pipeline.get("status") or "").lower() != "success"
            or str(pipeline.get("requested_commit_sha") or "") != commit_sha
            or str(pipeline.get("matched_commit_sha") or "") != commit_sha
            or str(pipeline.get("pipeline_status") or "").lower() not in _TERMINAL_PIPELINE_STATUSES
            or (attempt_id and pipeline_attempt_id and pipeline_attempt_id != attempt_id)
        ):
            continue
        candidates.append(pipeline)
    return candidates[-1] if candidates else None


def _latest_exact_failed_base_pipeline(pipelines: list[dict], push_attempt: Any) -> dict | None:
    push = push_attempt.result or {}
    base_sha = str(push.get("base_sha") or "")
    candidates = [
        pipeline
        for pipeline in pipelines
        if int(pipeline.get("_sequence") or 0) < int(push_attempt.sequence)
        and str(pipeline.get("status") or "").lower() == "success"
        and str(pipeline.get("pipeline_status") or "").lower() == "failed"
        and str(pipeline.get("requested_commit_sha") or "") == base_sha
        and str(pipeline.get("matched_commit_sha") or "") == base_sha
    ]
    return candidates[-1] if candidates else None


def native_failed_validation_counts(state: dict) -> dict[str, int]:
    """Count consecutive exact-SHA failures only for Verifier-covered Native roots."""
    from ut_agent.execution_ledger import build_execution_ledger

    ledger = build_execution_ledger(state.get("messages") or [])
    verification_coverage = _passing_verification_coverage(state)
    failed_validation_streaks: Counter[str] = Counter()
    streak_job_names: dict[str, set[str]] = {}
    streak_job_names_truncated: set[str] = set()
    seen_pipeline_proofs: set[tuple[str, int | str | None]] = set()

    pipeline_proofs = []
    for push_attempt in _successful_push_attempts(state):
        push = push_attempt.result or {}
        pipeline = _latest_exact_terminal_pipeline(ledger.pipelines, push_attempt)
        if pipeline is None:
            continue
        proof = (str(push.get("commit_sha") or ""), _pipeline_id(pipeline))
        if proof in seen_pipeline_proofs:
            continue
        seen_pipeline_proofs.add(proof)
        pipeline_proofs.append((push_attempt, pipeline))

    for push_attempt, pipeline in sorted(
        pipeline_proofs,
        key=lambda item: int(item[0].sequence),
    ):
        push = push_attempt.result or {}
        base_pipeline = _latest_exact_failed_base_pipeline(ledger.pipelines, push_attempt)
        if base_pipeline is None:
            continue
        source_root_map, source_roots_truncated, source_job_names_truncated = _root_map_with_truncation(
            base_pipeline
        )
        current_root_map, current_roots_truncated, current_job_names_truncated = _root_map_with_truncation(
            pipeline
        )
        current_roots = set(current_root_map)
        for root_id, job_names in (*source_root_map.items(), *current_root_map.items()):
            streak_job_names.setdefault(root_id, set()).update(job_names)
        streak_job_names_truncated.update(source_job_names_truncated)
        streak_job_names_truncated.update(current_job_names_truncated)

        reconciliation = reconcile_pipeline_failures(base_pipeline, pipeline)
        direct_transitions = {
            str(item.get("root_cause_id") or ""): str(item.get("status") or "")
            for item in reconciliation.get("transitions") or ()
            if isinstance(item, dict)
        }
        stored_transitions = _stored_transition_statuses(base_pipeline, pipeline)
        stored_reconciliation = pipeline.get("failure_reconciliation")
        stored_evidence_truncated = bool(
            isinstance(stored_reconciliation, dict)
            and (
                stored_reconciliation.get("transitions_truncated")
                or stored_reconciliation.get("current_observed_jobs_truncated")
            )
        )
        evidence_truncated = bool(
            source_roots_truncated
            or current_roots_truncated
            or pipeline.get("observed_jobs_truncated")
            or stored_evidence_truncated
        )
        job_statuses = _statuses_by_name(pipeline.get("observed_jobs") or ())
        for root_id in tuple(failed_validation_streaks):
            if evidence_truncated or root_id in streak_job_names_truncated:
                continue
            transition = stored_transitions.get(root_id) or direct_transitions.get(root_id)
            if root_id not in current_roots:
                historical_transition = _missing_root_status(
                    tuple(sorted(streak_job_names.get(root_id, ()))),
                    job_statuses,
                    current_root_map,
                )
                if historical_transition != "unknown":
                    transition = historical_transition
            if transition in {"resolved", "superseded"}:
                del failed_validation_streaks[root_id]
                streak_job_names.pop(root_id, None)
                streak_job_names_truncated.discard(root_id)
        if str(pipeline.get("pipeline_status") or "").lower() != "failed":
            continue

        covered_ids = verification_coverage.get((
            str(push.get("base_sha") or ""),
            str(push.get("diff_digest") or ""),
        ), set())
        if not covered_ids:
            continue
        source_roots = set(source_root_map)
        for root_id in sorted(covered_ids & source_roots & current_roots):
            failed_validation_streaks[root_id] += 1

    return dict(sorted(failed_validation_streaks.items()))


def native_exhausted_root_ids(state: dict, limit: int) -> frozenset[str]:
    """Return Native roots whose evidence-backed failed validation budget is exhausted."""
    threshold = max(1, int(limit))
    return frozenset(
        root_id
        for root_id, count in native_failed_validation_counts(state).items()
        if count >= threshold
    )
