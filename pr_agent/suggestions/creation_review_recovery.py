"""Recover recently missed automatic MR-creation reviews."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime

from pr_agent.config_loader import get_settings
from pr_agent.distributed.broker import RedisBroker
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.ingress import QueueIngress, build_creation_task
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.feedback.timez import now_cn, to_cn
from pr_agent.log import get_logger
from pr_agent.suggestions.review_tracking import (
    finish_review_run,
    get_creation_review_for_mr,
    get_inventory_mr,
    increment_sync_metric,
    mark_creation_recovery,
    record_review_event,
)


@dataclass(frozen=True)
class RecoveryOutcome:
    state: str
    reason_code: str = ""
    task_id: str = ""


def _setting(name: str, default):
    return get_settings().get(f"suggestion_review_dashboard.{name}", default)


async def recover_creation_review(
    record: dict,
    broker: RedisBroker,
    *,
    now: datetime | None = None,
    path: str | None = None,
) -> RecoveryOutcome:
    """Recover one recent sync-only MR without duplicating its creation review."""
    project_path = str(record.get("project_path") or "")
    mr_iid = str(record.get("mr_iid") or "")
    if not project_path or not mr_iid:
        increment_sync_metric("recovery_invalid", path=path)
        return RecoveryOutcome("invalid", "historical_evidence_missing")
    inventory = get_inventory_mr(project_path, mr_iid, path=path)
    if str(inventory.get("creation_recovery_state") or "") in {"outside_window", "invalid"}:
        return RecoveryOutcome("duplicate_suppressed", str(inventory.get("creation_reason_code") or ""))
    existing_run = get_creation_review_for_mr(project_path, mr_iid, path=path)
    settings = load_distributed_settings()
    if (
        existing_run
        and str(existing_run.get("status") or "") == "running"
        and str(existing_run.get("stage") or "") == "queued"
        and existing_run.get("task_id")
    ):
        task_id = str(existing_run["task_id"])
        recovery, retry_count = await broker.requeue_stale_auto_workflow(
            task_id,
            age_seconds=settings.queued_dispatch_seconds,
            retry_limit=settings.auto_workflow_retry_limit,
        )
        run_id = str(existing_run.get("run_id") or "")
        if recovery == "requeued":
            record_review_event(
                run_id,
                f"workflow_requeued:{retry_count}",
                "queued",
                details={"retry_count": retry_count, "reason_code": "queue_startup_timeout"},
                path=path,
            )
            mark_creation_recovery(project_path, mr_iid, "requeued", "queue_startup_timeout", path=path)
            increment_sync_metric("startup_requeued", path=path)
            return RecoveryOutcome("requeued", "queue_startup_timeout", task_id)
        if recovery == "failed":
            finish_review_run(
                "failed",
                run_id,
                path=path,
                stage="startup_failed",
                error_code="QueueStartupTimeout",
                error_message="Automatic workflow queue startup retry exhausted",
            )
            record_review_event(
                run_id,
                "startup_retry_exhausted",
                "startup_failed",
                status="failed",
                error_code="QueueStartupTimeout",
                error_message="Automatic workflow queue startup retry exhausted",
                details={"retry_count": retry_count},
                path=path,
            )
            mark_creation_recovery(project_path, mr_iid, "failed", "queue_startup_timeout", path=path)
            increment_sync_metric("startup_retry_exhausted", path=path)
            return RecoveryOutcome("failed", "queue_startup_timeout", task_id)
    retry_queue_failure = bool(
        existing_run
        and str(existing_run.get("status") or "") == "failed"
        and str(existing_run.get("stage") or "") == "queue_failed"
    )
    if existing_run and not retry_queue_failure:
        mark_creation_recovery(project_path, mr_iid, "duplicate_suppressed", path=path)
        increment_sync_metric("duplicate_suppressed", path=path)
        return RecoveryOutcome("duplicate_suppressed")

    created_at = to_cn(record.get("created_at"))
    current = now or now_cn()
    if not created_at or created_at > current:
        mark_creation_recovery(
            project_path, mr_iid, "invalid", "historical_evidence_missing", path=path,
        )
        increment_sync_metric("recovery_invalid", path=path)
        return RecoveryOutcome("invalid", "historical_evidence_missing")
    window_seconds = max(1, int(_setting("creation_recovery_window_seconds", 1800)))
    if (current - created_at).total_seconds() > window_seconds:
        mark_creation_recovery(
            project_path, mr_iid, "outside_window", "recovery_window_expired", path=path,
        )
        increment_sync_metric("recovery_outside_window", path=path)
        return RecoveryOutcome("outside_window", "recovery_window_expired")

    task = build_creation_task(record, source="gitlab_sync_recovery")
    if retry_queue_failure and existing_run.get("task_id"):
        task = replace(task, task_id=str(existing_run["task_id"]))
    try:
        result = await QueueIngress(broker).enqueue_creation_task(
            task, webhook_id=task.idempotency_key, tracking_path=path,
        )
    except Exception as exc:
        get_logger().warning(f"Creation review recovery failed for {project_path}!{mr_iid}: {exc}")
        mark_creation_recovery(
            project_path, mr_iid, "failed", "queue_admission_failed", path=path,
        )
        increment_sync_metric("recovery_failed", path=path)
        return RecoveryOutcome("failed", "queue_admission_failed", task.task_id)
    state = "recovered" if result.created else "duplicate_suppressed"
    mark_creation_recovery(project_path, mr_iid, state, path=path)
    increment_sync_metric(state, path=path)
    return RecoveryOutcome(state, task_id=result.task_id)


async def _recover_batch(records: list[dict], path: str | None) -> dict[str, int]:
    settings = load_distributed_settings()
    if settings.execution_mode != "queue":
        return {"skipped": len(records)}
    factory = RedisClientFactory(settings.redis_url)
    client = factory.create_async()
    counts: dict[str, int] = {}
    try:
        broker = RedisBroker(client, settings)
        for record in records:
            if not settings.should_queue(str(record.get("project_path") or "")):
                counts["skipped"] = counts.get("skipped", 0) + 1
                continue
            outcome = await recover_creation_review(record, broker, path=path)
            counts[outcome.state] = counts.get(outcome.state, 0) + 1
    finally:
        await client.aclose()
    return counts


def recover_synced_mrs(records: list[dict], *, path: str | None = None) -> dict[str, int]:
    """Run the async recovery batch from the inventory sync's dedicated thread."""
    if not records:
        return {}
    return asyncio.run(_recover_batch(records, path))
