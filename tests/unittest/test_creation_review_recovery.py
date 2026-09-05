import asyncio
from unittest.mock import AsyncMock, Mock, patch

from pr_agent.distributed.broker import EnqueueResult
from pr_agent.feedback.timez import to_cn
from pr_agent.suggestions.creation_review_recovery import recover_creation_review
from pr_agent.suggestions.review_tracking import (
    ensure_creation_review,
    finish_review_run,
    get_creation_review_for_mr,
    get_sync_metrics,
    update_review_run,
    upsert_mr,
)


def api_record(created_at="2026-08-08T11:30:00+08:00"):
    return {
        "project_id": 7,
        "project_path": "g/r",
        "mr_iid": 12,
        "mr_url": "https://gitlab.example/g/r/-/merge_requests/12",
        "commit_sha": "initial",
        "created_at": created_at,
        "discovered_by": "incremental_sync",
    }


def broker_that_accepts():
    broker = AsyncMock()
    broker.enqueue_task.side_effect = lambda task: EnqueueResult(True, task.task_id)
    return broker


def test_recovery_at_exact_boundary_is_eligible(tmp_path):
    path = str(tmp_path / "tracking.db")
    record = api_record()
    assert upsert_mr(record, path=path)
    broker = broker_that_accepts()

    outcome = asyncio.run(recover_creation_review(
        record, broker, now=to_cn("2026-08-08T12:00:00+08:00"), path=path,
    ))

    assert outcome.state == "recovered"
    assert outcome.task_id
    broker.enqueue_task.assert_awaited_once()


def test_recovery_after_boundary_is_not_enqueued(tmp_path):
    path = str(tmp_path / "tracking.db")
    record = api_record("2026-08-08T11:29:59+08:00")
    assert upsert_mr(record, path=path)
    broker = broker_that_accepts()

    outcome = asyncio.run(recover_creation_review(
        record, broker, now=to_cn("2026-08-08T12:00:00+08:00"), path=path,
    ))

    assert (outcome.state, outcome.reason_code) == ("outside_window", "recovery_window_expired")
    broker.enqueue_task.assert_not_awaited()
    second = asyncio.run(recover_creation_review(
        record, broker, now=to_cn("2026-08-08T12:10:00+08:00"), path=path,
    ))
    assert second.state == "duplicate_suppressed"
    assert get_sync_metrics(path)["recovery_outside_window"] == 1


def test_existing_creation_review_suppresses_recovery(tmp_path):
    path = str(tmp_path / "tracking.db")
    record = api_record()
    assert upsert_mr(record, path=path)
    assert ensure_creation_review({
        **record, "task_id": "existing-task", "webhook_id": "existing-event",
    }, path=path)
    broker = broker_that_accepts()

    outcome = asyncio.run(recover_creation_review(
        record, broker, now=to_cn("2026-08-08T12:00:00+08:00"), path=path,
    ))

    assert outcome.state == "duplicate_suppressed"
    broker.enqueue_task.assert_not_awaited()


def test_stale_queued_creation_review_is_requeued_once(tmp_path):
    path = str(tmp_path / "tracking.db")
    record = api_record()
    assert upsert_mr(record, path=path)
    run_id = ensure_creation_review({
        **record, "task_id": "existing-task", "webhook_id": "existing-event",
    }, path=path)
    update_review_run(run_id, path=path, stage="queued")
    broker = broker_that_accepts()
    broker.requeue_stale_auto_workflow.return_value = ("requeued", 1)
    settings = Mock(queued_dispatch_seconds=300, auto_workflow_retry_limit=1)

    with patch("pr_agent.suggestions.creation_review_recovery.load_distributed_settings", return_value=settings):
        outcome = asyncio.run(recover_creation_review(
            record, broker, now=to_cn("2026-08-08T12:00:00+08:00"), path=path,
        ))

    assert outcome.state == "requeued"
    assert outcome.reason_code == "queue_startup_timeout"
    assert outcome.task_id == "existing-task"
    broker.requeue_stale_auto_workflow.assert_awaited_once_with(
        "existing-task", age_seconds=300, retry_limit=1,
    )
    assert get_sync_metrics(path)["startup_requeued"] == 1


def test_stale_queued_creation_review_fails_after_retry_limit(tmp_path):
    path = str(tmp_path / "tracking.db")
    record = api_record()
    assert upsert_mr(record, path=path)
    run_id = ensure_creation_review({
        **record, "task_id": "existing-task", "webhook_id": "existing-event",
    }, path=path)
    update_review_run(run_id, path=path, stage="queued")
    broker = broker_that_accepts()
    broker.requeue_stale_auto_workflow.return_value = ("failed", 1)
    settings = Mock(queued_dispatch_seconds=300, auto_workflow_retry_limit=1)

    with patch("pr_agent.suggestions.creation_review_recovery.load_distributed_settings", return_value=settings):
        outcome = asyncio.run(recover_creation_review(
            record, broker, now=to_cn("2026-08-08T12:00:00+08:00"), path=path,
        ))

    assert outcome.state == "failed"
    assert outcome.reason_code == "queue_startup_timeout"
    run = get_creation_review_for_mr("g/r", "12", path=path)
    assert run["status"] == "failed"
    assert run["stage"] == "startup_failed"
    assert run["error_code"] == "QueueStartupTimeout"
    assert get_sync_metrics(path)["startup_retry_exhausted"] == 1


def test_skipped_creation_review_is_never_requeued(tmp_path):
    path = str(tmp_path / "tracking.db")
    record = api_record()
    assert upsert_mr(record, path=path)
    run_id = ensure_creation_review({
        **record, "task_id": "existing-task", "webhook_id": "existing-event",
    }, path=path)
    finish_review_run("skipped", run_id, path=path, stage="skipped", error_code="ignored_label")
    broker = broker_that_accepts()

    outcome = asyncio.run(recover_creation_review(
        record, broker, now=to_cn("2026-08-08T12:00:00+08:00"), path=path,
    ))

    assert outcome.state == "duplicate_suppressed"
    broker.requeue_stale_auto_workflow.assert_not_awaited()


def test_recovery_failure_is_bounded_and_recorded(tmp_path):
    path = str(tmp_path / "tracking.db")
    record = api_record()
    assert upsert_mr(record, path=path)
    broker = AsyncMock()
    broker.enqueue_task.side_effect = RuntimeError("redis unavailable")

    with patch("pr_agent.suggestions.creation_review_recovery.get_settings") as settings:
        settings.return_value.get.side_effect = lambda key, default=None: {
            "suggestion_review_dashboard.creation_recovery_window_seconds": 1800,
            "gitlab.pr_commands": ["/mr_create"],
        }.get(key, default)
        outcome = asyncio.run(recover_creation_review(
            record, broker, now=to_cn("2026-08-08T12:00:00+08:00"), path=path,
        ))

    assert (outcome.state, outcome.reason_code) == ("failed", "queue_admission_failed")
