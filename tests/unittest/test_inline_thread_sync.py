"""Tests for inline_thread_sync.sync_mr_threads: reconciling real GitLab
Discussions-API state into published_suggestions, then recomputing the gate."""

import os
import tempfile
from unittest.mock import MagicMock

from pr_agent.suggestions.store import get_published_suggestions, migrate_schema, save_suggestion_thread
from pr_agent.suggestions.inline_thread_sync import sync_mr_threads


def _pub(discussion_id, project="cook", mr_iid="10", suggestion_id="S1"):
    return dict(
        suggestion_id=suggestion_id, review_id="run-1", project=project, mr_iid=mr_iid,
        commit_sha="abc", file_path="a.cpp", line_start=1, line_end=3,
        label="bug", severity="High", score=8, one_sentence_summary="fix it",
        suggestion_content="content", existing_code="old", improved_code="new",
        gitlab_discussion_id=discussion_id, gitlab_note_id="n1",
        publish_status="published", state="published",
    )


def _discussion(disc_id, applied=False, resolved=False, resolved_by="alice"):
    note = {"suggestions": [{"applied": applied}], "resolved": resolved}
    if resolved:
        note["resolved_by"] = {"username": resolved_by}
    return {"id": disc_id, "notes": [note]}


def test_sync_marks_applied_and_resolved_then_recomputes(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_pub("d1"), path=path)
        save_suggestion_thread(_pub("d2", suggestion_id="S2"), path=path)

        def fetch(project, mr_iid):
            return [
                _discussion("d1", applied=True),
                _discussion("d2", resolved=True, resolved_by="bob"),
            ]

        recompute_calls = []
        monkeypatch.setattr(
            "pr_agent.suggestions.inline_gate_status.recompute",
            lambda gp, project, mr_iid, path=None: recompute_calls.append((project, mr_iid)),
        )
        gp = MagicMock()
        sync_mr_threads(gp, "cook", "10", fetch, path=path)

        rows = {r["gitlab_discussion_id"]: r for r in get_published_suggestions("cook", "10", path=path)}
        assert rows["d1"]["applied_at"]
        assert rows["d2"]["resolved_at"]
        assert rows["d2"]["resolve_user"] == "bob"
        assert recompute_calls == [("cook", "10")]


def test_sync_noop_when_no_published_suggestions(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        called = {"n": 0}
        monkeypatch.setattr(
            "pr_agent.suggestions.inline_gate_status.recompute",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )

        def fetch(project, mr_iid):
            raise AssertionError("fetch_discussions_fn should not be called when nothing is published")

        gp = MagicMock()
        sync_mr_threads(gp, "cook", "999", fetch, path=path)
        assert called["n"] == 0


def test_sync_survives_fetch_exception(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_pub("d1"), path=path)

        def fetch(project, mr_iid):
            raise RuntimeError("network down")

        recompute_calls = []
        monkeypatch.setattr(
            "pr_agent.suggestions.inline_gate_status.recompute",
            lambda gp, project, mr_iid, path=None: recompute_calls.append((project, mr_iid)),
        )
        gp = MagicMock()
        # Must not raise, and should still attempt a recompute with unchanged state.
        sync_mr_threads(gp, "cook", "10", fetch, path=path)
        assert recompute_calls == [("cook", "10")]


def test_sync_ignores_discussions_not_in_published_suggestions(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        save_suggestion_thread(_pub("d1"), path=path)

        def fetch(project, mr_iid):
            # "d-unrelated" is some human-authored discussion on the same MR;
            # it must not affect our own suggestion's state.
            return [_discussion("d-unrelated", resolved=True), _discussion("d1", applied=False, resolved=False)]

        monkeypatch.setattr("pr_agent.suggestions.inline_gate_status.recompute", lambda *a, **k: None)
        gp = MagicMock()
        sync_mr_threads(gp, "cook", "10", fetch, path=path)
        rows = get_published_suggestions("cook", "10", path=path)
        assert not rows[0]["applied_at"]
        assert not rows[0]["resolved_at"]
