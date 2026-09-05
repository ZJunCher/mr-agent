import sqlite3
from datetime import datetime
from unittest.mock import patch

from pr_agent.algo.model_resilience import ModelAttemptFailure, ModelFailureKind
from pr_agent.feedback.timez import to_cn
from pr_agent.suggestions.review_tracking import (
    activate_review_run,
    capture_attributable_evolution_case,
    claim_sync_lease,
    complete_sync,
    count_review_alert_signals,
    ensure_creation_review,
    get_creation_tracking_boundary,
    get_current_run_id,
    get_review_run,
    get_review_run_for_task,
    get_sync_metrics,
    get_sync_state,
    increment_sync_metric,
    init_review_tracking,
    list_review_events,
    list_active_review_alerts,
    mark_creation_recovery,
    mark_creation_tracking_started,
    project_webhook_suspected,
    record_review_event,
    start_review_run,
    update_review_run,
    update_review_alert_state,
    upsert_mr,
)
from pr_agent.feedback.store import list_evolution_cases
from pr_agent.suggestions.store import migrate_schema, save_filtered_suggestion
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


def test_mr_upsert_keeps_first_discovery_source(tmp_path):
    path = str(tmp_path / "tracking.db")
    assert upsert_mr({
        "project_id": 7, "project_path": "g/r", "mr_iid": 12,
        "title": "first", "discovered_by": "webhook",
    }, path=path)
    assert upsert_mr({
        "project_id": 7, "project_path": "g/r", "mr_iid": 12,
        "title": "second", "state": "merged", "discovered_by": "incremental_sync",
    }, path=path)

    conn = sqlite3.connect(path)
    row = conn.execute("SELECT title, state, discovered_by, webhook_received_at FROM mr_inventory").fetchone()
    conn.close()
    assert row[0:3] == ("second", "merged", "webhook")
    assert row[3]


def test_run_context_and_lifecycle(tmp_path):
    path = str(tmp_path / "tracking.db")
    run_id = start_review_run({
        "project_path": "g/r", "mr_iid": "12", "task_id": "task-1", "trigger": "manual_improve",
    }, path=path)
    assert run_id
    assert start_review_run({"task_id": "task-1"}, path=path) == run_id
    with activate_review_run(run_id):
        assert get_current_run_id() == run_id
        assert update_review_run(
            stage="filtered", generated_count=3, kept_count=1, filtered_count=2,
            inline_fallback_count=1, path=path,
        )
    assert get_current_run_id() is None

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT stage, generated_count, kept_count, filtered_count, inline_fallback_count "
        "FROM suggestion_review_runs"
    ).fetchone()
    conn.close()
    assert row == ("filtered", 3, 1, 2, 1)


def test_creation_review_is_idempotent_and_keeps_initial_sha(tmp_path):
    path = str(tmp_path / "tracking.db")
    first = {
        "project_path": "g/r", "mr_iid": "12", "commit_sha": "initial",
        "task_id": "auto-12", "webhook_id": "event-12",
    }
    run_id = ensure_creation_review(first, path=path)
    assert run_id
    assert ensure_creation_review({**first, "commit_sha": "later"}, path=path) == run_id
    run = get_review_run(run_id, path=path)
    assert (run["review_scope"], run["trigger"], run["commit_sha"]) == (
        "mr_creation", "auto_mr_create", "initial",
    )
    assert get_review_run_for_task("auto-12", path=path)["run_id"] == run_id


def test_creation_events_and_boundary_are_idempotent(tmp_path):
    path = str(tmp_path / "tracking.db")
    boundary = mark_creation_tracking_started(path=path)
    run_id = ensure_creation_review({
        "project_path": "g/r", "mr_iid": "12", "task_id": "auto-12",
    }, path=path)
    assert record_review_event(run_id, "workflow_queued", "queued", path=path)
    assert record_review_event(run_id, "workflow_queued", "queued", path=path)
    assert [event["event_key"] for event in list_review_events(run_id, path=path)] == [
        "creation_received", "workflow_queued",
    ]
    mark_creation_tracking_started(path=path)
    assert get_creation_tracking_boundary(path=path) == boundary


def test_suggestion_model_failure_is_recorded_as_sanitized_event(tmp_path, monkeypatch):
    path = str(tmp_path / "tracking.db")
    run_id = start_review_run({
        "project_path": "g/r", "mr_iid": "12", "trigger": "manual_improve",
    }, path=path)
    monkeypatch.setattr("pr_agent.suggestions.review_tracking._db_path", lambda _path=None: path)
    instance = object.__new__(PRCodeSuggestions)
    failure = ModelAttemptFailure(
        model="anthropic/claude-haiku",
        deployment_id=None,
        attempt=1,
        kind=ModelFailureKind.TIMEOUT,
        message="request timed out",
        elapsed_ms=12,
    )

    with activate_review_run(run_id):
        instance._record_model_attempt_failure(failure)

    event = list_review_events(run_id, path=path)[0]
    assert event["event_key"] == "model_attempt_failed:1:anthropic_claude-haiku"
    assert event["stage"] == "generating"
    assert event["error_code"] == "timeout"
    assert event["error_message"] == "request timed out"
    assert event["details"] == {
        "attempt": 1,
        "deployment_configured": False,
        "elapsed_ms": 12,
        "model": "anthropic/claude-haiku",
    }


def test_attributable_execution_failure_becomes_reproducible_case(tmp_path):
    path = str(tmp_path / "tracking.db")
    run_id = start_review_run({
        "project_path": "g/r",
        "mr_iid": "12",
        "commit_sha": "a" * 40,
        "trigger": "manual_improve",
    }, path=path)
    assert update_review_run(run_id, path=path, review_id="improve-review-12")

    assert update_review_run(
        run_id,
        path=path,
        stage="failed",
        status="failed",
        error_code="SuggestionOutputSchemaError",
        error_message="code_suggestions must be an array",
    )

    cases = list_evolution_cases(path)
    assert len(cases) == 1
    assert cases[0]["kind"] == "output_schema_error"
    assert cases[0]["review_id"] == "improve-review-12"
    assert cases[0]["error_code"] == "output_schema_error"
    assert capture_attributable_evolution_case(run_id, path=path)
    assert len(list_evolution_cases(path)) == 1


def test_infrastructure_failure_is_not_an_evolution_case(tmp_path):
    path = str(tmp_path / "tracking.db")
    run_id = start_review_run({
        "project_path": "g/r",
        "mr_iid": "12",
        "commit_sha": "a" * 40,
        "trigger": "manual_improve",
    }, path=path)
    assert update_review_run(run_id, path=path, review_id="improve-review-12")

    assert update_review_run(
        run_id,
        path=path,
        stage="failed",
        status="failed",
        error_code="timeout",
        error_message="model service timed out",
    )

    assert list_evolution_cases(path) == []


def test_lease_is_exclusive_and_released(tmp_path):
    path = str(tmp_path / "tracking.db")
    assert claim_sync_lease("sync", "worker-a", path=path)
    assert not claim_sync_lease("sync", "worker-b", path=path)
    assert complete_sync("sync", "worker-a", cursor_at="2026-08-07T10:00:00+08:00", path=path)
    assert claim_sync_lease("sync", "worker-b", path=path)
    state = get_sync_state("sync", path=path)
    assert state["cursor_at"] == "2026-08-07T10:00:00+08:00"


def test_existing_suggestion_tables_gain_and_populate_run_id(tmp_path):
    path = str(tmp_path / "tracking.db")
    migrate_schema(path)
    with activate_review_run("run-42"):
        assert save_filtered_suggestion({"project": "g/r", "mr_iid": "12"}, path=path)

    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(filtered_suggestions)")}
    run_id = conn.execute("SELECT run_id FROM filtered_suggestions").fetchone()[0]
    conn.close()
    assert "run_id" in columns
    assert run_id == "run-42"


def test_initialization_never_raises_for_invalid_path(monkeypatch):
    monkeypatch.setattr("pr_agent.suggestions.review_tracking._run_write", lambda *_: (_ for _ in ()).throw(OSError()))
    init_review_tracking("ignored")


def test_recovery_state_metrics_and_webhook_suspicion(tmp_path):
    path = str(tmp_path / "tracking.db")
    for iid in ("1", "2", "3"):
        assert upsert_mr({
            "project_id": "g/r", "project_path": "g/r", "mr_iid": iid,
            "created_at": f"2026-08-0{int(iid) + 4}T12:00:00+08:00",
            "discovered_by": "incremental_sync",
        }, path=path)
    assert mark_creation_recovery(
        "g/r", "3", "outside_window", "recovery_window_expired", path=path,
    )
    assert increment_sync_metric("recovery_outside_window", path=path)
    assert increment_sync_metric("recovery_outside_window", path=path)
    assert get_sync_metrics(path)["recovery_outside_window"] == 2
    assert project_webhook_suspected(
        "g/r", now=to_cn("2026-08-08T12:00:00+08:00"), path=path,
    )

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT creation_recovery_state, creation_reason_code, creation_reason_at "
        "FROM mr_inventory WHERE project_path = 'g/r' AND mr_iid = '3'"
    ).fetchone()
    conn.close()
    assert row[0:2] == ("outside_window", "recovery_window_expired")
    assert row[2]


def test_alert_signals_count_distinct_review_runs(tmp_path):
    path = str(tmp_path / "tracking.db")
    model_run = start_review_run({"run_id": "model-run"}, path=path)
    startup_run = start_review_run({"run_id": "startup-run"}, path=path)
    fallback_run = start_review_run({"run_id": "fallback-run"}, path=path)
    skipped_run = start_review_run({"run_id": "skipped-run"}, path=path)
    record_review_event(model_run, "model_attempt_failed:1:gpt", "generating", path=path)
    record_review_event(model_run, "model_attempt_failed:2:gpt", "generating", path=path)
    record_review_event(skipped_run, "workflow_skipped", "skipped", status="skipped", path=path)
    update_review_run(
        startup_run, path=path, stage="startup_failed", status="failed", error_code="QueueStartupTimeout",
    )
    update_review_run(fallback_run, path=path, inline_fallback_count=2, stage="published")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE suggestion_review_events SET created_at = '2026-08-18T10:30:00+08:00'")
    conn.execute(
        "UPDATE suggestion_review_runs SET updated_at = '2026-08-18T10:30:00+08:00' "
        "WHERE run_id IN ('startup-run', 'fallback-run')"
    )
    conn.commit()
    conn.close()

    assert count_review_alert_signals("2026-08-18T10:00:00+08:00", path=path) == {
        "model_failures": 1,
        "startup_retry_exhausted": 1,
        "publish_fallbacks": 1,
    }


def test_alert_state_emits_once_per_activation_and_cooldown(tmp_path):
    path = str(tmp_path / "tracking.db")
    with patch(
        "pr_agent.suggestions.review_tracking.now_cn",
        return_value=datetime.fromisoformat("2026-08-18T10:00:00+08:00"),
    ):
        first = update_review_alert_state(
            "model_failures", active=True, count=3, cooldown_seconds=3600, path=path,
        )
        duplicate = update_review_alert_state(
            "model_failures", active=True, count=4, cooldown_seconds=3600, path=path,
        )
    with patch(
        "pr_agent.suggestions.review_tracking.now_cn",
        return_value=datetime.fromisoformat("2026-08-18T11:01:00+08:00"),
    ):
        reminder = update_review_alert_state(
            "model_failures", active=True, count=5, cooldown_seconds=3600, path=path,
        )
        resolved = update_review_alert_state(
            "model_failures", active=False, count=0, cooldown_seconds=3600, path=path,
        )

    assert first.should_emit is True
    assert duplicate.should_emit is False
    assert reminder.should_emit is True
    assert resolved.resolved is True
    assert list_active_review_alerts(path=path) == []
