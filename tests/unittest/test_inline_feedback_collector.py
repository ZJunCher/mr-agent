"""Tests for inline_feedback_collector — note event → save feedback."""

import os
import tempfile

from pr_agent.suggestions.store import (
    get_inline_feedbacks,
    migrate_schema,
    save_suggestion_thread,
)
from pr_agent.suggestions.inline_feedback_collector import handle_note_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _note_payload(note="looks good", discussion_id="d1",
                  username="zhangsan", mr_iid=42,
                  project_path="eabot/cook", note_id="note-99",
                  noteable_type="MergeRequest", system=False):
    return {
        "object_kind": "note",
        "user": {"username": username, "name": username},
        "project": {"path_with_namespace": project_path},
        "object_attributes": {
            "id": note_id,
            "note": note,
            "discussion_id": discussion_id,
            "noteable_type": noteable_type,
            "action": "create",
            "system": system,
        },
        "merge_request": {"iid": mr_iid},
    }


def _pub(discussion_id="d1", project="eabot/cook", mr_iid="42",
         mr_url="https://gitlab.example.com/eabot/cook/-/merge_requests/42"):
    return dict(
        suggestion_id="S1", review_id="run1", project=project, mr_iid=mr_iid,
        mr_url=mr_url,
        commit_sha="abc", file_path="a.cpp", line_start=1, line_end=2,
        label="bug", severity="High", score=8, one_sentence_summary="x",
        suggestion_content="y", existing_code="old", improved_code="new",
        gitlab_discussion_id=discussion_id, gitlab_note_id=10,
        publish_status="published", skip_reason="", state="published",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_saves_feedback_for_known_discussion():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="d1"), path=path)
        migrate_schema(path=path)

        handle_note_event(_note_payload(discussion_id="d1"), path=path)
        rows = get_inline_feedbacks("eabot/cook", "42", path=path)
        assert len(rows) == 1
        assert rows[0]["feedback_user"] == "zhangsan"
        assert rows[0]["comment"] == "looks good"


def test_ignores_note_on_unknown_discussion():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        migrate_schema(path=path)  # no suggestion threads saved

        handle_note_event(_note_payload(discussion_id="unknown-disc"), path=path)
        rows = get_inline_feedbacks("eabot/cook", "42", path=path)
        assert rows == []


def test_ignores_command_notes():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="d1"), path=path)
        migrate_schema(path=path)

        handle_note_event(_note_payload(note="/review", discussion_id="d1"), path=path)
        rows = get_inline_feedbacks("eabot/cook", "42", path=path)
        assert rows == []


def test_ignores_empty_note():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="d1"), path=path)
        migrate_schema(path=path)

        handle_note_event(_note_payload(note="", discussion_id="d1"), path=path)
        rows = get_inline_feedbacks("eabot/cook", "42", path=path)
        assert rows == []


def test_ignores_bot_user_notes():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="d1"), path=path)
        migrate_schema(path=path)

        handle_note_event(
            _note_payload(username="pr_agent_bot", discussion_id="d1"),
            bot_username="pr_agent_bot",
            path=path,
        )
        rows = get_inline_feedbacks("eabot/cook", "42", path=path)
        assert rows == []


def test_deduplicates_by_note_id():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="d1"), path=path)
        migrate_schema(path=path)

        handle_note_event(_note_payload(note_id="note-1", discussion_id="d1"), path=path)
        handle_note_event(_note_payload(note_id="note-1", discussion_id="d1"), path=path)
        rows = get_inline_feedbacks("eabot/cook", "42", path=path)
        assert len(rows) == 1


def test_never_raises_on_bad_path():
    with tempfile.NamedTemporaryFile() as f:
        bad = os.path.join(f.name, "nested", "s.db")
        # Should not raise even with bad path
        handle_note_event(_note_payload(), path=bad)


def test_records_correct_project_and_mr_iid():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="d1", project="eabot/chogori", mr_iid="7"), path=path)
        migrate_schema(path=path)

        handle_note_event(
            _note_payload(discussion_id="d1", mr_iid=7,
                          project_path="eabot/chogori"),
            path=path,
        )
        rows = get_inline_feedbacks("eabot/chogori", "7", path=path)
        assert len(rows) == 1
        assert rows[0]["mr_iid"] == "7"


def test_records_mr_url_from_matched_published_suggestion():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        mr_url = "https://gitlab.example.com/eabot/cook/-/merge_requests/42"
        save_suggestion_thread(_pub(discussion_id="d1", mr_url=mr_url), path=path)
        migrate_schema(path=path)

        handle_note_event(_note_payload(discussion_id="d1"), path=path)
        rows = get_inline_feedbacks("eabot/cook", "42", path=path)
        assert len(rows) == 1
        assert rows[0]["mr_url"] == mr_url


def test_ignores_system_notes():
    # GitLab-generated system notes (e.g. "resolved all threads" from clicking
    # "Resolve thread" without leaving a comment) must never be stored as
    # real user feedback.
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="d1"), path=path)
        migrate_schema(path=path)

        handle_note_event(
            _note_payload(note="resolved all threads", discussion_id="d1", system=True),
            path=path,
        )
        rows = get_inline_feedbacks("eabot/cook", "42", path=path)
        assert rows == []


def test_regular_note_still_stored_when_system_field_false():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_pub(discussion_id="d1"), path=path)
        migrate_schema(path=path)

        handle_note_event(
            _note_payload(note="this suggestion is wrong", discussion_id="d1", system=False),
            path=path,
        )
        rows = get_inline_feedbacks("eabot/cook", "42", path=path)
        assert len(rows) == 1
        assert rows[0]["comment"] == "this suggestion is wrong"
