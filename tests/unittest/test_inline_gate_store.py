"""Tests for the inline-suggestion gate's store helpers:
mark_resolved / sync_thread_state / all_threads_satisfied / has_published_suggestions.
"""

import os
import tempfile

from pr_agent.suggestions.store import (
    all_threads_satisfied,
    get_published_suggestions,
    has_published_suggestions,
    mark_applied,
    mark_resolved,
    migrate_schema,
    save_suggestion_thread,
    sync_thread_state,
)


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
        suggestion_content="content",
        existing_code="old",
        improved_code="new",
        gitlab_discussion_id=discussion_id,
        gitlab_note_id="n1",
        publish_status="published",
        state="published",
    )
    base.update(kw)
    return base


def test_mark_resolved_sets_fields_once():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_pub(), path=path)
        assert mark_resolved("d1", resolve_user="alice", path=path) is True
        rows = get_published_suggestions("p/cook", "10", path=path)
        assert rows[0]["resolved_at"]
        assert rows[0]["resolve_user"] == "alice"


def test_mark_resolved_does_not_overwrite_existing_timestamp():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_pub(), path=path)
        mark_resolved("d1", resolve_user="alice", resolved_at="2026-01-01T00:00:00", path=path)
        mark_resolved("d1", resolve_user="bob", resolved_at="2026-02-02T00:00:00", path=path)
        rows = get_published_suggestions("p/cook", "10", path=path)
        assert rows[0]["resolved_at"] == "2026-01-01T00:00:00"
        assert rows[0]["resolve_user"] == "alice"


def test_mark_resolved_missing_discussion_returns_false():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        assert mark_resolved("no-such-disc", resolve_user="alice", path=path) is False


def test_sync_thread_state_writes_applied_and_resolved():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_pub(discussion_id="d1"), path=path)
        save_suggestion_thread(_pub(suggestion_id="S2", discussion_id="d2"), path=path)
        sync_thread_state("d1", applied=True, apply_user="carol", path=path)
        sync_thread_state("d2", resolved=True, resolve_user="dave", path=path)
        rows = {r["gitlab_discussion_id"]: r for r in get_published_suggestions("p/cook", "10", path=path)}
        assert rows["d1"]["applied_at"]
        assert rows["d1"]["apply_user"] == "carol"
        assert not rows["d2"]["applied_at"]
        assert rows["d2"]["resolved_at"]
        assert rows["d2"]["resolve_user"] == "dave"


def test_sync_thread_state_noop_when_neither_flag_set():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_pub(), path=path)
        sync_thread_state("d1", path=path)  # no applied/resolved
        rows = get_published_suggestions("p/cook", "10", path=path)
        assert not rows[0]["applied_at"]
        assert not rows[0]["resolved_at"]


def test_all_threads_satisfied_true_when_no_rows():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        assert all_threads_satisfied("p/nothing", "1", path=path) is True


def test_all_threads_satisfied_false_when_one_unprocessed():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_pub(discussion_id="d1"), path=path)
        save_suggestion_thread(_pub(suggestion_id="S2", discussion_id="d2"), path=path)
        mark_applied("d1", apply_user="carol", path=path)
        assert all_threads_satisfied("p/cook", "10", path=path) is False


def test_all_threads_satisfied_true_when_all_applied_or_resolved():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_pub(discussion_id="d1"), path=path)
        save_suggestion_thread(_pub(suggestion_id="S2", discussion_id="d2"), path=path)
        mark_applied("d1", apply_user="carol", path=path)
        mark_resolved("d2", resolve_user="dave", path=path)
        assert all_threads_satisfied("p/cook", "10", path=path) is True


def test_has_published_suggestions():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        assert has_published_suggestions("p/cook", "10", path=path) is False
        save_suggestion_thread(_pub(), path=path)
        assert has_published_suggestions("p/cook", "10", path=path) is True
