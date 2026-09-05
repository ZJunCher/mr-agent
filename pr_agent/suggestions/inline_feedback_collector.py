"""Collect user replies on inline-suggestion discussions as feedback.

Called from the webhook server when ``object_kind == "note"`` and
``action == "create"``.  Checks whether the note's ``discussion_id`` matches
a known suggestion thread; if so, and the note is not a bot/command, saves
the text as feedback in ``inline_suggestion_feedback``.

Never raises.
"""

from __future__ import annotations

from typing import Optional

from pr_agent.log import get_logger
from pr_agent.suggestions.store import (
    get_published_suggestions,
    migrate_schema,
    save_inline_feedback,
)


def handle_note_event(
    payload: dict,
    bot_username: str = "",
    path: Optional[str] = None,
) -> None:
    """Process a GitLab note webhook payload to collect user feedback.

    Saves to ``inline_suggestion_feedback`` only when:
    - ``noteable_type`` is ``MergeRequest``
    - ``discussion_id`` matches a row in ``published_suggestions``
    - note author is not the bot
    - note text is not empty and does not start with ``/`` (command)

    Never raises.
    """
    try:
        migrate_schema(path=path)

        obj = payload.get("object_attributes") or {}
        noteable_type = obj.get("noteable_type") or ""
        if noteable_type != "MergeRequest":
            return

        # GitLab-generated system notes (e.g. "resolved all threads" when a
        # user clicks "Resolve thread" without leaving a comment) must never
        # be treated as user feedback.
        if obj.get("system"):
            return

        note_text = (obj.get("note") or "").strip()
        if not note_text or note_text.startswith("/"):
            return

        user = payload.get("user") or {}
        sender_username = user.get("username") or ""
        if bot_username and sender_username == bot_username:
            return

        discussion_id = obj.get("discussion_id") or ""
        if not discussion_id:
            return

        project_info = payload.get("project") or {}
        project = project_info.get("path_with_namespace") or ""
        mr_info = payload.get("merge_request") or {}
        mr_iid = str(mr_info.get("iid") or "")
        gitlab_note_id = str(obj.get("id") or "")

        # Only save if this discussion belongs to a known published suggestion
        threads = get_published_suggestions(project, mr_iid, path=path)
        matched = next(
            (t for t in threads if t.get("gitlab_discussion_id") == discussion_id),
            None,
        )
        if matched is None:
            return

        record = {
            "project": project,
            "mr_iid": mr_iid,
            "mr_url": matched.get("mr_url"),
            "suggestion_id": matched.get("suggestion_id"),
            "discussion_id": discussion_id,
            "feedback_user": sender_username,
            "comment": note_text,
            "gitlab_note_id": gitlab_note_id,
        }
        save_inline_feedback(record, path=path)
        get_logger().info(
            f"inline_feedback_collector: saved feedback user={sender_username} "
            f"discussion={discussion_id}"
        )
    except Exception as e:
        get_logger().error(f"handle_note_event failed: {e}")
