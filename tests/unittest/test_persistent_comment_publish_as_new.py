"""publish_persistent_comment_with_history(publish_as_new_comment=True) must
never edit an existing comment (neither `progress_response` nor a previous
run's persisted summary) in place -- it must always delete whichever old
comment(s) exist and publish a brand new one. This is what manual /improve
needs: inline suggestions are published AFTER progress_response (and, on
repeat runs, after the previous summary comment) already exist in the MR
timeline, so editing either in place would leave the final summary comment
pinned at that earlier position -- BEFORE this run's inline suggestions.
Deleting and republishing puts it at its true (current) position instead.

The default (publish_as_new_comment=False) must remain byte-for-byte the
prior edit-in-place behavior, since mr_create-style callers (should any
exist) depend on it.
"""
from unittest.mock import MagicMock

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

INITIAL_HEADER = "## PR Code Suggestions ✨"


class _FakeComment:
    _next_id = 100

    def __init__(self, body):
        self.body = body
        self.id = _FakeComment._next_id
        _FakeComment._next_id += 1


def _make_provider(prev_comments=None):
    gp = MagicMock()
    gp.get_latest_commit_url.return_value = "https://gl/commit/abcdef1234567"
    gp.get_comment_url.side_effect = lambda c: f"https://gl/mr/1#note_{c.id}"
    gp.get_issue_comments.return_value = list(prev_comments or [])
    gp.edit_comment = MagicMock()
    gp.remove_comment = MagicMock()
    gp.publish_comment = MagicMock(side_effect=lambda body: _FakeComment(body))
    return gp


def setup_function(_):
    get_settings().set("pr_code_suggestions.code_suggestions_self_review_text", "self review")


# ---------- publish_as_new_comment=True: first run (no previous comment) ----------

def test_first_run_with_progress_response_deletes_placeholder_and_publishes_new():
    gp = _make_provider(prev_comments=[])
    progress = _FakeComment("## Generating PR code suggestions\n\nWork in progress ...")

    comment, body = PRCodeSuggestions.publish_persistent_comment_with_history(
        gp, f"{INITIAL_HEADER}\n\n<table>x</table>", INITIAL_HEADER,
        name="suggestions", progress_response=progress, publish_as_new_comment=True)

    gp.remove_comment.assert_called_once_with(progress)
    gp.edit_comment.assert_not_called()
    gp.publish_comment.assert_called_once()
    assert comment is not progress
    assert INITIAL_HEADER in body


def test_first_run_without_progress_response_just_publishes_new():
    gp = _make_provider(prev_comments=[])

    comment, body = PRCodeSuggestions.publish_persistent_comment_with_history(
        gp, f"{INITIAL_HEADER}\n\n<table>x</table>", INITIAL_HEADER,
        name="suggestions", progress_response=None, publish_as_new_comment=True)

    gp.remove_comment.assert_not_called()
    gp.edit_comment.assert_not_called()
    gp.publish_comment.assert_called_once()


# ---------- publish_as_new_comment=True: repeat run (previous summary exists) ----------

def test_repeat_run_deletes_both_old_summary_and_placeholder_then_publishes_new():
    old_summary = _FakeComment(f"{INITIAL_HEADER}\n\n<!-- aaaaaaa -->\n\n<table>old</table>")
    gp = _make_provider(prev_comments=[old_summary])
    progress = _FakeComment("## Generating PR code suggestions\n\nWork in progress ...")

    comment, body = PRCodeSuggestions.publish_persistent_comment_with_history(
        gp, f"{INITIAL_HEADER}\n\n<table>new</table>", INITIAL_HEADER,
        name="suggestions", progress_response=progress, publish_as_new_comment=True)

    # both the placeholder and the previous run's summary are deleted
    assert gp.remove_comment.call_count == 2
    deleted = {call.args[0] for call in gp.remove_comment.call_args_list}
    assert deleted == {old_summary, progress}
    gp.edit_comment.assert_not_called()
    gp.publish_comment.assert_called_once()
    assert comment is not old_summary
    assert comment is not progress
    # the old table got folded into a "previous suggestions" history section
    assert "<table>old</table>" in body
    assert "<table>new</table>" in body


def test_repeat_run_without_progress_response_deletes_old_summary_only():
    old_summary = _FakeComment(f"{INITIAL_HEADER}\n\n<!-- aaaaaaa -->\n\n<table>old</table>")
    gp = _make_provider(prev_comments=[old_summary])

    comment, body = PRCodeSuggestions.publish_persistent_comment_with_history(
        gp, f"{INITIAL_HEADER}\n\n<table>new</table>", INITIAL_HEADER,
        name="suggestions", progress_response=None, publish_as_new_comment=True)

    gp.remove_comment.assert_called_once_with(old_summary)
    gp.edit_comment.assert_not_called()
    gp.publish_comment.assert_called_once()


# ---------- default (publish_as_new_comment=False): behavior unchanged ----------

def test_default_first_run_with_progress_response_edits_in_place():
    gp = _make_provider(prev_comments=[])
    progress = _FakeComment("## Generating PR code suggestions\n\nWork in progress ...")

    comment, body = PRCodeSuggestions.publish_persistent_comment_with_history(
        gp, f"{INITIAL_HEADER}\n\n<table>x</table>", INITIAL_HEADER,
        name="suggestions", progress_response=progress)

    gp.edit_comment.assert_called_once()
    gp.remove_comment.assert_not_called()
    gp.publish_comment.assert_not_called()
    assert comment is progress


def test_default_repeat_run_edits_progress_response_and_deletes_old_summary():
    old_summary = _FakeComment(f"{INITIAL_HEADER}\n\n<!-- aaaaaaa -->\n\n<table>old</table>")
    gp = _make_provider(prev_comments=[old_summary])
    progress = _FakeComment("## Generating PR code suggestions\n\nWork in progress ...")

    comment, body = PRCodeSuggestions.publish_persistent_comment_with_history(
        gp, f"{INITIAL_HEADER}\n\n<table>new</table>", INITIAL_HEADER,
        name="suggestions", progress_response=progress)

    gp.edit_comment.assert_called_once_with(progress, body)
    gp.remove_comment.assert_called_once_with(old_summary)
    gp.publish_comment.assert_not_called()
    assert comment is progress
