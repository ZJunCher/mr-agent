"""triage_runs 表存储层单测。"""
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "triage_test.db")


def _sample_record(**overrides):
    base = {
        "pr_url": "https://gitlab.example.com/group/repo/-/merge_requests/1",
        "project": "group/repo",
        "mr_iid": "1",
        "mr_author": "alice",
        "feishu_user_name": "赵军",
        "source_branch": "feature/x",
        "target_branch": "main",
        "commit_sha": "abc123",
        "pipeline_id": "42",
        "trigger_type": "pipeline_failed",
        "failed_job_names": ["build", "clang-format"],
        "failure_categories": ["build", "format"],
        "success": 1,
        "finish_reason": "",
        "iterations": 5,
        "max_iterations": 30,
        "pushed_sha": "def456",
        "final_pipeline_status": "success",
        "repair_outcome": "success",
        "category_results": [{"category": "build", "outcome": "succeeded"}],
        "failure_signatures": [],
        "fix_duration_ms": 120000,
        "model": "claude-3-5-sonnet",
        "error": None,
    }
    base.update(overrides)
    return base


def test_save_and_query_triage_run(tmp_db):
    from pr_agent.triage import store

    with patch("pr_agent.feedback.store.get_db_path", return_value=tmp_db):
        store.init_triage_table(tmp_db)
        ok = store.save_triage_run(_sample_record(), path=tmp_db)
    assert ok is True

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM triage_runs WHERE mr_iid = ?", ("1",)).fetchone()
    conn.close()
    assert row is not None
    assert row["success"] == 1
    assert json.loads(row["failed_job_names"]) == ["build", "clang-format"]
    assert json.loads(row["failure_categories"]) == ["build", "format"]
    assert row["feishu_user_name"] == "赵军"
    assert row["repair_outcome"] == "success"
    assert json.loads(row["category_results"])[0]["outcome"] == "succeeded"


def test_structured_lifecycle_details_are_kept_in_extra_json(tmp_db):
    from pr_agent.triage import store

    extra = {
        "duration_breakdown": {"processing_total_ms": 90_000, "pipeline_wait_duration_ms": 60_000},
        "push_attempts": [{"attempt_sequence": 1, "commit_sha": "def456"}],
        "pipeline_groups": [{"root_pipeline_id": 42, "validation_pipeline_id": 43}],
        "coverage_source": "changed_lines",
        "coverage_status": "report_missing",
    }

    assert store.save_triage_run(_sample_record(extra=extra), path=tmp_db) is True

    conn = sqlite3.connect(tmp_db)
    stored = json.loads(conn.execute("SELECT extra_json FROM triage_runs").fetchone()[0])
    conn.close()
    assert stored == extra


def test_save_triage_run_never_raises(tmp_db):
    from pr_agent.triage import store

    with patch("pr_agent.triage.store._connect", side_effect=sqlite3.OperationalError("disk full")):
        ok = store.save_triage_run(_sample_record(), path=tmp_db)
    assert ok is False  # 不抛异常，返回 False


def test_has_triage_run(tmp_db):
    from pr_agent.triage import store

    with patch("pr_agent.feedback.store.get_db_path", return_value=tmp_db):
        store.init_triage_table(tmp_db)
        assert store.has_triage_run("group/repo", "1", "abc123", path=tmp_db) is False
        store.save_triage_run(_sample_record(), path=tmp_db)
        assert store.has_triage_run("group/repo", "1", "abc123", path=tmp_db) is True


def test_has_triage_run_never_raises(tmp_db):
    from pr_agent.triage import store

    with patch("pr_agent.triage.store._connect", side_effect=sqlite3.OperationalError("locked")):
        assert store.has_triage_run("p", "1", "sha", path=tmp_db) is False


def test_has_triage_run_task_uses_task_identity(tmp_db):
    from pr_agent.triage import store

    assert store.save_triage_run(_sample_record(task_id="task-author"), path=tmp_db)
    assert store.has_triage_run_task("task-author", path=tmp_db)
    assert not store.has_triage_run_task("missing-task", path=tmp_db)


def test_init_triage_table_idempotent(tmp_db):
    from pr_agent.triage import store

    store.init_triage_table(tmp_db)
    store.init_triage_table(tmp_db)  # 第二次不报错
    conn = sqlite3.connect(tmp_db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "triage_runs" in tables
    assert "review_feedback" not in tables  # 不创建 feedback 表


def test_same_task_updates_existing_triage_row(tmp_db):
    from pr_agent.triage import store

    first = _sample_record(task_id="task-1", final_coverage=60.0)
    second = _sample_record(task_id="task-1", final_coverage=63.04, final_pipeline_status="success")

    assert store.save_triage_run(first, path=tmp_db) is True
    assert store.save_triage_run(second, path=tmp_db) is True

    conn = sqlite3.connect(tmp_db)
    count, coverage = conn.execute("SELECT COUNT(*), final_coverage FROM triage_runs").fetchone()
    conn.close()
    assert count == 1
    assert coverage == 63.04


def test_update_triage_run_identity_enriches_existing_task(tmp_db):
    from pr_agent.triage import store

    assert store.save_triage_run(_sample_record(task_id="task-actor", feishu_user_name=None), path=tmp_db) is True
    assert store.update_triage_run_identity("task-actor", "赵军", path=tmp_db) is True

    conn = sqlite3.connect(tmp_db)
    name = conn.execute(
        "SELECT feishu_user_name FROM triage_runs WHERE task_id = ?",
        ("task-actor",),
    ).fetchone()[0]
    conn.close()
    assert name == "赵军"


def test_update_triage_run_identity_reports_missing_task(tmp_db):
    from pr_agent.triage import store

    store.init_triage_table(tmp_db)

    assert store.update_triage_run_identity("missing", "赵军", path=tmp_db) is False
