"""Tests for inline_apply_detector — push event → mark_applied."""

import os
import tempfile

from pr_agent.suggestions.store import (
    get_published_suggestions,
    mark_applied,
    migrate_schema,
    save_suggestion_thread,
)
from pr_agent.suggestions.inline_apply_detector import (
    handle_push_event,
    is_apply_commit,
)


# ---------------------------------------------------------------------------
# is_apply_commit
# ---------------------------------------------------------------------------

def test_is_apply_commit_matches_standard_message():
    assert is_apply_commit("Apply suggestion to src/foo.cpp") is True


def test_is_apply_commit_matches_lowercase():
    assert is_apply_commit("apply suggestion to src/bar.hpp") is True


def test_is_apply_commit_matches_gitlab_real_single():
    # GitLab's actual apply-suggestion commit message format
    assert is_apply_commit("Apply 1 suggestion(s) to 1 file(s)") is True


def test_is_apply_commit_matches_gitlab_real_multiple():
    assert is_apply_commit("Apply 3 suggestion(s) to 2 file(s)") is True


def test_is_apply_commit_matches_gitlab_real_with_coauthor():
    msg = "Apply 1 suggestion(s) to 1 file(s)\n\nCo-authored-by: eabot <bot@x>"
    assert is_apply_commit(msg) is True


def test_is_apply_commit_ignores_regular_commit():
    assert is_apply_commit("fix: correct off-by-one error") is False


def test_is_apply_commit_ignores_empty():
    assert is_apply_commit("") is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _push_payload(commit_message="Apply suggestion to src/a.cpp",
                  commit_sha="sha111", username="alice", project_id=2,
                  project_path="eabot/cook",
                  ref="refs/heads/feature/x"):
    return {
        "object_kind": "push",
        "user_username": username,
        "ref": ref,
        "project": {"id": project_id, "path_with_namespace": project_path},
        "commits": [{"id": commit_sha, "message": commit_message}],
    }


def _pub(discussion_id="d1", project="eabot/cook", mr_iid="10"):
    return dict(
        suggestion_id="S1", review_id="run1", project=project, mr_iid=mr_iid,
        commit_sha="abc", file_path="a.cpp", line_start=1, line_end=2,
        label="bug", severity="High", score=8, one_sentence_summary="x",
        suggestion_content="y", existing_code="old", improved_code="new",
        gitlab_discussion_id=discussion_id, gitlab_note_id=10,
        publish_status="published", skip_reason="", state="published",
    )


# ---------------------------------------------------------------------------
# handle_push_event
# ---------------------------------------------------------------------------

def test_marks_applied_for_apply_commit():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="disc-1"), path=path)
        migrate_schema(path=path)

        def fetch_applied(_project_id, _sha, _ref=""):
            return ["disc-1"]

        handle_push_event(_push_payload(), fetch_applied_fn=fetch_applied, path=path)
        rows = get_published_suggestions("eabot/cook", "10", path=path)
        assert rows[0]["apply_user"] == "alice"
        assert rows[0]["applied_at"] is not None


def test_skips_non_apply_commits():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="disc-1"), path=path)
        migrate_schema(path=path)

        fetch_called = []

        def fetch_applied(_project_id, _sha, _ref=""):
            fetch_called.append(1)
            return []

        handle_push_event(
            _push_payload(commit_message="fix: refactor cache logic"),
            fetch_applied_fn=fetch_applied,
            path=path,
        )
        assert fetch_called == []  # fetch not called for non-apply commits
        rows = get_published_suggestions("eabot/cook", "10", path=path)
        assert rows[0]["applied_at"] is None


def test_handles_fetch_failure_gracefully():
    """fetch_applied_fn raising must not propagate."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)

        def fetch_applied(_project_id, _sha, _ref=""):
            raise RuntimeError("API down")

        # Should not raise
        handle_push_event(_push_payload(), fetch_applied_fn=fetch_applied, path=path)


def test_handles_empty_commits_payload():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)
        payload = {"object_kind": "push", "user_username": "alice",
                   "project": {"id": 2, "path_with_namespace": "eabot/cook"},
                   "commits": []}
        handle_push_event(payload, fetch_applied_fn=lambda *_: [], path=path)


def test_apply_user_comes_from_payload():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="d-u"), path=path)
        migrate_schema(path=path)

        handle_push_event(
            _push_payload(username="bob", commit_sha="s1"),
            fetch_applied_fn=lambda *_: ["d-u"],
            path=path,
        )
        rows = get_published_suggestions("eabot/cook", "10", path=path)
        assert rows[0]["apply_user"] == "bob"


def test_ref_is_passed_to_fetch():
    """handle_push_event must forward the push branch ref to fetch_applied_fn."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="disc-1"), path=path)
        migrate_schema(path=path)
        seen = {}

        def fetch_applied(_project_id, _sha, ref=""):
            seen["ref"] = ref
            return ["disc-1"]

        handle_push_event(
            _push_payload(ref="refs/heads/feature/test-branch"),
            fetch_applied_fn=fetch_applied,
            path=path,
        )
        assert seen["ref"] == "refs/heads/feature/test-branch"


def test_retries_until_fetch_returns_results():
    """When fetch is empty at first (GitLab eventual consistency), retry and succeed."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="disc-1"), path=path)
        migrate_schema(path=path)
        calls = {"n": 0}

        def fetch_applied(_project_id, _sha, _ref=""):
            calls["n"] += 1
            return ["disc-1"] if calls["n"] >= 3 else []

        handle_push_event(
            _push_payload(),
            fetch_applied_fn=fetch_applied,
            path=path,
            max_attempts=5,
            retry_delay=0.0,
            sleep_fn=lambda _s: None,
        )
        assert calls["n"] == 3
        rows = get_published_suggestions("eabot/cook", "10", path=path)
        assert rows[0]["applied_at"] is not None


def test_no_retry_when_max_attempts_is_one():
    """Default single attempt: empty result leaves applied_at untouched."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="disc-1"), path=path)
        migrate_schema(path=path)
        calls = {"n": 0}

        def fetch_applied(_project_id, _sha, _ref=""):
            calls["n"] += 1
            return []

        handle_push_event(_push_payload(), fetch_applied_fn=fetch_applied, path=path)
        assert calls["n"] == 1
        rows = get_published_suggestions("eabot/cook", "10", path=path)
        assert rows[0]["applied_at"] is None
