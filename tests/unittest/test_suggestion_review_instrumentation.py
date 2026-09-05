import asyncio
import sqlite3
from unittest.mock import patch

import pytest

from pr_agent.suggestions.review_tracking import get_current_run_id, track_review_run, update_review_run
from pr_agent.tools.pr_mr_create import PRMrCreate


class Provider:
    id_project = "g/r"
    id_mr = "8"
    pr_url = "https://gl/g/r/-/merge_requests/8"

    def get_diff_refs(self):
        return {"head_sha": "abc"}


class Tool:
    def __init__(self):
        self.git_provider = Provider()
        self.pr_url = self.git_provider.pr_url

    @track_review_run("manual_improve")
    async def run(self):
        assert get_current_run_id()
        update_review_run(stage="validated", generated_count=2, kept_count=1, filtered_count=1)


class FailingTool(Tool):
    @track_review_run("manual_improve")
    async def run(self):
        raise ValueError("broken")


def test_decorator_records_completed_run(tmp_path):
    path = str(tmp_path / "tracking.db")
    with patch("pr_agent.suggestions.store.get_db_path", return_value=path):
        asyncio.run(Tool().run())

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT trigger, status, stage, generated_count, kept_count, filtered_count "
        "FROM suggestion_review_runs"
    ).fetchone()
    conn.close()
    assert row == ("manual_improve", "completed", "validated", 2, 1, 1)


def test_decorator_records_uncaught_failure(tmp_path):
    path = str(tmp_path / "tracking.db")
    with patch("pr_agent.suggestions.store.get_db_path", return_value=path), pytest.raises(ValueError):
        asyncio.run(FailingTool().run())

    conn = sqlite3.connect(path)
    row = conn.execute("SELECT status, error_code, error_message FROM suggestion_review_runs").fetchone()
    conn.close()
    assert row == ("failed", "ValueError", "broken")


def test_mr_create_records_caught_improve_failure():
    tool = object.__new__(PRMrCreate)
    tool.llm_feedback = []

    async def fail_improve():
        raise RuntimeError("provider unavailable")

    with (
        patch.object(tool, "_collect_llm_feedback"),
        patch("pr_agent.tools.pr_mr_create.update_review_run") as update,
    ):
        result = asyncio.run(tool._safe_tool_run("improve", fail_improve))

    assert result == ""
    update.assert_called_once_with(
        None, stage="execution_failed", status="failed",
        error_code="RuntimeError", error_message="provider unavailable",
    )
