"""Tests for the dedicated ``published_suggestions`` table.

Published inline suggestions live in their own table so adoption rate can be
computed quickly and the audit table (``suggestion_threads``) is not bloated by
them.  Skipped / failed suggestions stay in ``suggestion_threads``.
"""

import os
import sqlite3
import tempfile

from pr_agent.suggestions.store import (
    get_apply_stats,
    get_published_suggestions,
    get_suggestion_threads,
    mark_applied,
    migrate_schema,
    save_suggestion_thread,
)


def _rec(suggestion_id="S1", discussion_id="d1", project="p/cook", mr_iid="10",
         publish_status="published", **kw):
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
        publish_status=publish_status,
        skip_reason="",
        state=publish_status,
    )
    base.update(kw)
    return base


def _count(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

def test_migrate_creates_published_table():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "published_suggestions" in tables


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

def test_published_row_goes_to_published_table():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_rec(publish_status="published"), path=path)
        assert _count(path, "published_suggestions") == 1
        assert _count(path, "suggestion_threads") == 0


def test_skipped_row_goes_to_threads_table():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_rec(publish_status="skipped"), path=path)
        assert _count(path, "suggestion_threads") == 1
        assert _count(path, "published_suggestions") == 0


def test_failed_row_goes_to_threads_table():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_rec(publish_status="failed"), path=path)
        assert _count(path, "suggestion_threads") == 1
        assert _count(path, "published_suggestions") == 0


# ---------------------------------------------------------------------------
# get_published_suggestions
# ---------------------------------------------------------------------------

def test_get_published_suggestions_returns_only_published():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_rec(suggestion_id="S1", discussion_id="d1"), path=path)
        save_suggestion_thread(_rec(suggestion_id="S2", publish_status="skipped"), path=path)
        rows = get_published_suggestions("p/cook", "10", path=path)
        assert len(rows) == 1
        assert rows[0]["suggestion_id"] == "S1"


# ---------------------------------------------------------------------------
# mark_applied on the published table
# ---------------------------------------------------------------------------

def test_mark_applied_updates_published_table():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_rec(discussion_id="disc-1"), path=path)
        assert mark_applied("disc-1", apply_user="alice", path=path) is True
        rows = get_published_suggestions("p/cook", "10", path=path)
        assert rows[0]["apply_user"] == "alice"
        assert rows[0]["applied_at"] is not None


# ---------------------------------------------------------------------------
# mr_url persistence
# ---------------------------------------------------------------------------

def test_mr_url_persisted_on_published_row():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        url = "https://gitlab.example.com/data-platform/yangtze-client/-/merge_requests/334"
        save_suggestion_thread(_rec(publish_status="published", mr_url=url), path=path)
        rows = get_published_suggestions("p/cook", "10", path=path)
        assert rows[0]["mr_url"] == url


def test_mr_url_persisted_on_skipped_row():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        url = "https://gitlab.example.com/eabot/cook/-/merge_requests/451"
        save_suggestion_thread(_rec(publish_status="skipped", mr_url=url), path=path)
        rows = get_suggestion_threads("p/cook", "10", path=path)
        assert rows[0]["mr_url"] == url


def test_mr_url_column_added_to_pre_existing_db_without_it():
    # Simulate a production DB created before mr_url existed: build the tables
    # with the OLD schema (no mr_url column), then confirm save still works.
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE published_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, updated_at TEXT,
            suggestion_id TEXT, review_id TEXT, project TEXT, mr_iid TEXT,
            commit_sha TEXT, file_path TEXT, line_start INTEGER, line_end INTEGER,
            label TEXT, severity TEXT, score INTEGER, one_sentence_summary TEXT,
            suggestion_content TEXT, existing_code TEXT, improved_code TEXT,
            gitlab_discussion_id TEXT, gitlab_note_id TEXT, state TEXT,
            extra_json TEXT, applied_at TEXT, apply_user TEXT
        )""")
        conn.commit()
        conn.close()

        migrate_schema(path=path)  # should ALTER TABLE to add mr_url, not raise
        url = "https://gitlab.example.com/eabot/chogori/-/merge_requests/236"
        assert save_suggestion_thread(_rec(publish_status="published", mr_url=url), path=path) is True
        rows = get_published_suggestions("p/cook", "10", path=path)
        assert rows[0]["mr_url"] == url


def test_mark_applied_keeps_first_timestamp():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_rec(discussion_id="d-x"), path=path)
        mark_applied("d-x", apply_user="alice", path=path)
        ts1 = get_published_suggestions("p/cook", "10", path=path)[0]["applied_at"]
        mark_applied("d-x", apply_user="bob", path=path)
        ts2 = get_published_suggestions("p/cook", "10", path=path)[0]["applied_at"]
        assert ts2 == ts1


# ---------------------------------------------------------------------------
# adoption rate: get_apply_stats reads the published table
# ---------------------------------------------------------------------------

def test_apply_stats_from_published_table():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        for i, disc in enumerate(["d1", "d2", "d3"]):
            save_suggestion_thread(_rec(suggestion_id=f"S{i}", discussion_id=disc), path=path)
        # a skipped one must not inflate the denominator
        save_suggestion_thread(_rec(suggestion_id="Sk", discussion_id="dk",
                                    publish_status="skipped"), path=path)
        mark_applied("d1", "alice", path=path)
        mark_applied("d2", "bob", path=path)
        stats = get_apply_stats("p/cook", "10", path=path)
        assert stats["published"] == 3
        assert stats["applied"] == 2


def test_apply_stats_empty_mr():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        assert get_apply_stats("p/cook", "99", path=path) == {"published": 0, "applied": 0}


# ---------------------------------------------------------------------------
# prompt provenance persistence (Task 5)
# ---------------------------------------------------------------------------
def test_published_row_persists_prompt_provenance():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "s.db")
        migrate_schema(path=path)
        record = _rec(
            global_prompt_set_hash="global-1",
            project_rules_hash="rules-1",
            prompt_bundle_hash="bundle-1",
            prompt_version="2026-w34",
        )
        assert save_suggestion_thread(record, path=path)
        row = get_published_suggestions("p/cook", "10", path=path)[0]
        assert row["global_prompt_set_hash"] == "global-1"
        assert row["project_rules_hash"] == "rules-1"
        assert row["prompt_bundle_hash"] == "bundle-1"
        assert row["prompt_version"] == "2026-w34"
