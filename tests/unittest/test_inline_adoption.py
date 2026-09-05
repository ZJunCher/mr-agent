"""Tests for suggestion adoption tracking (apply detection + user feedback).

Covers the new store functions:
  - migrate_schema / mark_applied / get_apply_stats
  - save_inline_feedback / get_inline_feedbacks
"""

import os
import sqlite3
import tempfile

import pytest

from pr_agent.suggestions.store import (
    get_apply_stats,
    get_inline_feedbacks,
    get_published_suggestions,
    mark_applied,
    migrate_schema,
    save_inline_feedback,
    save_suggestion_thread,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pub(suggestion_id="S1", discussion_id="d1", project="p/cook", mr_iid="10", **kw):
    base = dict(
        suggestion_id=suggestion_id,
        review_id="run-1",
        project=project,
        mr_iid=mr_iid,
        commit_sha="abc",
        file_path="a.cpp",
        line_start=1,
        line_end=3,
        label="bug",
        severity="High",
        score=8,
        one_sentence_summary="fix it",
        suggestion_content="...",
        existing_code="old",
        improved_code="new",
        gitlab_discussion_id=discussion_id,
        gitlab_note_id=100,
        publish_status="published",
        skip_reason="",
        state="published",
    )
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# migrate_schema
# ---------------------------------------------------------------------------

def test_migrate_adds_applied_columns():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(), path=path)  # creates table without new cols
        migrate_schema(path=path)
        conn = sqlite3.connect(path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(suggestion_threads)")}
        conn.close()
        assert "applied_at" in cols
        assert "apply_user" in cols


def test_migrate_creates_feedback_table():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        conn = sqlite3.connect(path)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "inline_suggestion_feedback" in tables


def test_migrate_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        migrate_schema(path=path)  # second call must not raise


# ---------------------------------------------------------------------------
# mark_applied
# ---------------------------------------------------------------------------

def test_mark_applied_updates_record():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="disc-1"), path=path)
        migrate_schema(path=path)
        result = mark_applied("disc-1", apply_user="alice", path=path)
        assert result is True
        rows = get_published_suggestions("p/cook", "10", path=path)
        assert rows[0]["apply_user"] == "alice"
        assert rows[0]["applied_at"] is not None


def test_mark_applied_returns_false_when_no_match():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        result = mark_applied("nonexistent", apply_user="alice", path=path)
        assert result is False


def test_mark_applied_is_idempotent_keeps_first_timestamp():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="d-x"), path=path)
        migrate_schema(path=path)
        mark_applied("d-x", apply_user="alice", path=path)
        rows1 = get_published_suggestions("p/cook", "10", path=path)
        ts1 = rows1[0]["applied_at"]
        mark_applied("d-x", apply_user="bob", path=path)  # second call
        rows2 = get_published_suggestions("p/cook", "10", path=path)
        # timestamp must not change on second apply
        assert rows2[0]["applied_at"] == ts1


# ---------------------------------------------------------------------------
# get_apply_stats
# ---------------------------------------------------------------------------

def test_get_apply_stats_counts_published_and_applied():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        for i, disc in enumerate(["d1", "d2", "d3"]):
            save_suggestion_thread(_pub(suggestion_id=f"S{i}", discussion_id=disc), path=path)
        migrate_schema(path=path)
        mark_applied("d1", "alice", path=path)
        mark_applied("d2", "bob", path=path)
        stats = get_apply_stats("p/cook", "10", path=path)
        assert stats["published"] == 3
        assert stats["applied"] == 2


def test_get_apply_stats_empty_mr():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        stats = get_apply_stats("p/cook", "99", path=path)
        assert stats == {"published": 0, "applied": 0}


def test_get_apply_stats_only_counts_published_status():
    """Skipped suggestions should not inflate the published count."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(suggestion_id="S1", publish_status="published"), path=path)
        save_suggestion_thread(_pub(suggestion_id="S2", publish_status="skipped"), path=path)
        migrate_schema(path=path)
        stats = get_apply_stats("p/cook", "10", path=path)
        assert stats["published"] == 1


# ---------------------------------------------------------------------------
# save_inline_feedback / get_inline_feedbacks
# ---------------------------------------------------------------------------

def _fb(discussion_id="d1", project="p/cook", mr_iid="10", **kw):
    base = dict(
        project=project,
        mr_iid=mr_iid,
        discussion_id=discussion_id,
        suggestion_id="S1",
        feedback_user="zhangsan",
        comment="looks good",
        gitlab_note_id="note-42",
    )
    base.update(kw)
    return base


def test_save_and_get_inline_feedback():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        assert save_inline_feedback(_fb(), path=path) is True
        rows = get_inline_feedbacks("p/cook", "10", path=path)
        assert len(rows) == 1
        assert rows[0]["feedback_user"] == "zhangsan"
        assert rows[0]["comment"] == "looks good"


def test_save_inline_feedback_deduplicates_by_note_id():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_inline_feedback(_fb(gitlab_note_id="note-1"), path=path)
        save_inline_feedback(_fb(gitlab_note_id="note-1"), path=path)  # duplicate
        rows = get_inline_feedbacks("p/cook", "10", path=path)
        assert len(rows) == 1


def test_get_inline_feedbacks_isolated_by_project():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_inline_feedback(_fb(project="p/a", gitlab_note_id="note-a"), path=path)
        save_inline_feedback(_fb(project="p/b", gitlab_note_id="note-b"), path=path)
        assert len(get_inline_feedbacks("p/a", "10", path=path)) == 1
        assert len(get_inline_feedbacks("p/b", "10", path=path)) == 1


def test_save_inline_feedback_never_raises_on_bad_path():
    with tempfile.NamedTemporaryFile() as f:
        bad = os.path.join(f.name, "nested", "s.db")
        assert save_inline_feedback(_fb(), path=bad) is False
