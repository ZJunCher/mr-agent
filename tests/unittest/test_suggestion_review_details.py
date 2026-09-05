from pr_agent.suggestions.review_reporting import collect_creation_review_detail
from pr_agent.suggestions.review_tracking import (
    ensure_creation_review,
    finish_review_run,
    record_review_event,
    start_review_run,
    upsert_mr,
)
from pr_agent.suggestions.store import save_filtered_suggestion, save_published_suggestion


def _seed_inventory(path: str, iid: str = "12") -> None:
    assert upsert_mr({
        "project_id": "7", "project_path": "g/r", "mr_iid": iid,
        "mr_url": f"https://gl/g/r/-/merge_requests/{iid}", "title": "Guard state",
        "author": "alice", "created_at": "2026-08-07T12:00:00+08:00",
        "updated_at": "2026-08-07T12:00:00+08:00",
    }, path=path)


def test_detail_returns_only_the_creation_run(tmp_path):
    path = str(tmp_path / "details.db")
    _seed_inventory(path)
    creation = ensure_creation_review({
        "project_path": "g/r", "mr_iid": "12", "task_id": "auto-1", "commit_sha": "initial-sha",
    }, path=path)
    manual = start_review_run({
        "project_path": "g/r", "mr_iid": "12", "task_id": "manual-1", "trigger": "manual_improve",
    }, path=path)
    assert creation and manual
    assert save_filtered_suggestion({
        "project": "g/r", "mr_iid": "12", "run_id": creation,
        "suggestion_content": "Use guard", "file_path": "src/a.py", "line_start": 4, "line_end": 7,
        "improved_code": "if ready:\n    run()", "skip_reason": "场景约束不满足",
    }, path=path)
    assert save_published_suggestion({
        "project": "g/r", "mr_iid": "12", "run_id": manual,
        "suggestion_content": "manual-only", "gitlab_note_id": "99",
    }, path=path)
    finish_review_run(
        "completed", creation, path=path, stage="published", generated_count=1, filtered_count=1,
        unpublished_reason="secondary_review_filtered",
    )

    detail = collect_creation_review_detail("g/r", "12", path=path, gitlab_url="https://gl")

    assert detail["mr"]["run_id"] == creation
    assert detail["mr"]["initial_commit_sha"] == "initial-sha"
    assert [item["suggestion"] for item in detail["filtered_suggestions"]] == ["Use guard"]
    assert detail["published_suggestions"] == []
    assert detail["counts"]["filtered"] == 1
    assert detail["detail_state"] == "available"


def test_detail_includes_published_url_timeline_and_failure(tmp_path):
    path = str(tmp_path / "details.db")
    _seed_inventory(path)
    run_id = ensure_creation_review({
        "project_path": "g/r", "mr_iid": "12", "task_id": "auto-1", "commit_sha": "initial-sha",
    }, path=path)
    assert run_id
    assert record_review_event(run_id, "workflow_started", "workflow_started", path=path)
    assert record_review_event(
        run_id, "publish_failed", "publish_failed", status="failed", error_code="GitLabError",
        error_message="x" * 1200, path=path,
    )
    assert save_published_suggestion({
        "project": "g/r", "mr_iid": "12", "run_id": run_id, "file_path": "src/a.py",
        "suggestion_content": "Use guard", "gitlab_note_id": "77",
    }, path=path)
    finish_review_run("completed", run_id, path=path, generated_count=1, inline_published_count=1)

    detail = collect_creation_review_detail("g/r", "12", path=path)

    assert [event["event_key"] for event in detail["timeline"]] == [
        "creation_received", "workflow_started", "publish_failed",
    ]
    assert detail["published_suggestions"][0]["discussion_url"].endswith("#note_77")
    assert detail["errors"][0]["error_code"] == "GitLabError"
    assert len(detail["errors"][0]["message"]) == 1000


def test_detail_distinguishes_unavailable_from_successful_empty(tmp_path):
    path = str(tmp_path / "details.db")
    _seed_inventory(path)
    assert collect_creation_review_detail("g/r", "12", path=path)["detail_state"] == "unavailable"
    run_id = ensure_creation_review({
        "project_path": "g/r", "mr_iid": "12", "task_id": "auto-1",
    }, path=path)
    finish_review_run("completed", run_id, path=path, generated_count=0)
    assert collect_creation_review_detail("g/r", "12", path=path)["detail_state"] == "available_empty"
