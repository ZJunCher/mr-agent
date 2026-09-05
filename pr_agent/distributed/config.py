import os
from dataclasses import dataclass
from typing import Any

from pr_agent.config_loader import get_settings


@dataclass(frozen=True)
class DistributedSettings:
    execution_mode: str
    redis_url: str
    web_workers: int
    agent_workers: int
    worker_max_active_tasks: int
    report_max_active_per_worker: int
    worker_inbox_prefetch: int
    worker_heartbeat_seconds: int
    worker_dead_seconds: int
    worker_degraded_lag_seconds: int
    worker_shutdown_grace_seconds: int
    mr_lease_seconds: int
    mr_idle_release_seconds: int
    task_retry_limit: int
    auto_workflow_retry_limit: int
    dedup_ttl_seconds: int
    pipeline_event_ttl_seconds: int
    pipeline_fallback_scan_seconds: int
    task_heartbeat_seconds: int
    running_orphan_seconds: int
    assigned_start_seconds: int
    queued_dispatch_seconds: int
    repair_reconcile_seconds: int
    notification_retry_limit: int
    triage_priority_over_auto: bool
    auto_pause_at_command_boundary: bool
    queue_allowlisted_projects: tuple[str, ...]

    def should_queue(self, project_id: str) -> bool:
        if self.execution_mode != "queue":
            return False
        return not self.queue_allowlisted_projects or project_id in self.queue_allowlisted_projects


def _setting(name: str, default: Any) -> Any:
    return get_settings().get(f"DISTRIBUTED.{name.upper()}", default)


def _positive_int(name: str, default: int) -> int:
    value = int(_setting(name, default))
    if value <= 0:
        raise ValueError(f"distributed.{name} must be positive")
    return value


def _non_negative_int(name: str, default: int) -> int:
    value = int(_setting(name, default))
    if value < 0:
        raise ValueError(f"distributed.{name} must be non-negative")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = _setting(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"distributed.{name} must be a boolean")


def _repair_report_positive_int(name: str, default: int) -> int:
    value = int(get_settings().get(f"REPAIR_REPORT.{name.upper()}", default) or default)
    if value <= 0:
        raise ValueError(f"repair_report.{name} must be positive")
    return value


def _allowlisted_projects() -> tuple[str, ...]:
    environment_value = os.getenv("PR_AGENT_QUEUE_ALLOWLISTED_PROJECTS")
    configured_value = (
        environment_value if environment_value is not None else _setting("queue_allowlisted_projects", [])
    )
    if isinstance(configured_value, str):
        projects = configured_value.split(",")
    elif isinstance(configured_value, (list, tuple)):
        projects = configured_value
    else:
        raise ValueError("distributed.queue_allowlisted_projects must be a list or comma-separated string")
    return tuple(str(project).strip() for project in projects if str(project).strip())


def load_distributed_settings(*, redis_url_override: str | None = None) -> DistributedSettings:
    execution_mode = os.getenv("PR_AGENT_EXECUTION_MODE", str(_setting("execution_mode", "inline"))).strip().lower()
    if execution_mode not in {"inline", "queue"}:
        raise ValueError("PR_AGENT_EXECUTION_MODE must be inline or queue")

    redis_url = (
        redis_url_override
        if redis_url_override is not None
        else os.getenv("PR_AGENT_REDIS_URL", str(_setting("redis_url", "")))
    ).strip()
    if execution_mode == "queue" and not redis_url:
        raise ValueError("queue execution mode requires a Redis URL")

    settings = DistributedSettings(
        execution_mode=execution_mode,
        redis_url=redis_url,
        web_workers=_positive_int("web_workers", 2),
        agent_workers=_positive_int("agent_workers", 3),
        worker_max_active_tasks=_positive_int("worker_max_active_tasks", 4),
        report_max_active_per_worker=_repair_report_positive_int("max_active_per_worker", 1),
        worker_inbox_prefetch=_positive_int("worker_inbox_prefetch", 32),
        worker_heartbeat_seconds=_positive_int("worker_heartbeat_seconds", 5),
        worker_dead_seconds=_positive_int("worker_dead_seconds", 20),
        worker_degraded_lag_seconds=_positive_int("worker_degraded_lag_seconds", 5),
        worker_shutdown_grace_seconds=_positive_int("worker_shutdown_grace_seconds", 120),
        mr_lease_seconds=_positive_int("mr_lease_seconds", 30),
        mr_idle_release_seconds=_positive_int("mr_idle_release_seconds", 120),
        task_retry_limit=_positive_int("task_retry_limit", 3),
        auto_workflow_retry_limit=_non_negative_int("auto_workflow_retry_limit", 1),
        dedup_ttl_seconds=_positive_int("dedup_ttl_seconds", 600),
        pipeline_event_ttl_seconds=_positive_int("pipeline_event_ttl_seconds", 86400),
        pipeline_fallback_scan_seconds=_positive_int("pipeline_fallback_scan_seconds", 120),
        task_heartbeat_seconds=_positive_int("task_heartbeat_seconds", 15),
        running_orphan_seconds=_positive_int("running_orphan_seconds", 120),
        assigned_start_seconds=_positive_int("assigned_start_seconds", 120),
        queued_dispatch_seconds=_positive_int("queued_dispatch_seconds", 300),
        repair_reconcile_seconds=_positive_int("repair_reconcile_seconds", 120),
        notification_retry_limit=_positive_int("notification_retry_limit", 5),
        triage_priority_over_auto=_boolean("triage_priority_over_auto", True),
        auto_pause_at_command_boundary=_boolean("auto_pause_at_command_boundary", True),
        queue_allowlisted_projects=_allowlisted_projects(),
    )
    if settings.worker_dead_seconds <= settings.worker_heartbeat_seconds:
        raise ValueError("distributed.worker_dead_seconds must exceed worker_heartbeat_seconds")
    if settings.mr_lease_seconds <= settings.worker_heartbeat_seconds:
        raise ValueError("distributed.mr_lease_seconds must exceed worker_heartbeat_seconds")
    return settings
