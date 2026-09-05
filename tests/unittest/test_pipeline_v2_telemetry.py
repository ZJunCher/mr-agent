"""Tests for the Pipeline v2 telemetry columns on published_suggestions
(resolved_by_stage, tier2_duration_ms)."""
import os
import tempfile

from pr_agent.suggestions.store import get_published_suggestions, save_suggestion_thread


def _rec(**kw):
    base = {
        "suggestion_id": "SUG-001", "review_id": "run-1", "project": "group/cook", "mr_iid": "10",
        "commit_sha": "abc123", "file_path": "src/a.cpp", "line_start": 10, "line_end": 12,
        "label": "possible bug", "severity": "High", "score": 9,
        "one_sentence_summary": "fix off-by-one", "suggestion_content": "why...\nfix...",
        "existing_code": "old", "improved_code": "new",
        "gitlab_discussion_id": "d1", "gitlab_note_id": 101,
        "publish_status": "published", "skip_reason": "", "state": "published",
    }
    base.update(kw)
    return base


def test_resolved_by_stage_and_tier2_duration_persist_and_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        assert save_suggestion_thread(
            _rec(resolved_by_stage="tier2_heavy", tier2_duration_ms=45210), path=path) is True
        rows = get_published_suggestions("group/cook", "10", path=path)
        assert len(rows) == 1
        assert rows[0]["resolved_by_stage"] == "tier2_heavy"
        assert rows[0]["tier2_duration_ms"] == 45210


def test_columns_default_to_none_when_absent():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        assert save_suggestion_thread(_rec(), path=path) is True
        rows = get_published_suggestions("group/cook", "10", path=path)
        assert rows[0]["resolved_by_stage"] is None
        assert rows[0]["tier2_duration_ms"] is None


def test_non_numeric_tier2_duration_does_not_crash():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        assert save_suggestion_thread(_rec(tier2_duration_ms="not-a-number"), path=path) is True
        rows = get_published_suggestions("group/cook", "10", path=path)
        assert rows[0]["tier2_duration_ms"] is None