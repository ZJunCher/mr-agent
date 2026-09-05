from pr_agent.suggestions.review_tracking import get_review_run_for_task
from scripts.calibrate_creation_review_failures import calibrate


def record():
    return {
        "task_id": "task-1", "project_path": "g/r", "mr_iid": "12",
        "commit_sha": "abc", "created_at": "2026-08-06T15:09:00+08:00",
        "error": "worker lost and retry limit exceeded",
    }


def test_calibration_is_dry_run_by_default_and_idempotent(tmp_path):
    path = str(tmp_path / "tracking.db")
    assert calibrate([record()], path=path) == {"inserted": 0, "skipped": 0, "invalid": 0, "would_insert": 1}
    assert get_review_run_for_task("task-1", path=path) == {}

    assert calibrate([record()], path=path, dry_run=False)["inserted"] == 1
    run = get_review_run_for_task("task-1", path=path)
    assert (run["trigger"], run["status"], run["error_code"]) == (
        "historical_auto_mr_create", "failed", "worker_lost",
    )
    assert calibrate([record()], path=path, dry_run=False)["skipped"] == 1


def test_calibration_rejects_incomplete_evidence(tmp_path):
    path = str(tmp_path / "tracking.db")
    assert calibrate([{"task_id": "missing-mr"}], path=path, dry_run=False)["invalid"] == 1
