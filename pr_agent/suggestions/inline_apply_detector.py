"""Detect when a GitLab user applies an inline suggestion via a push event.

Called from the webhook server when ``object_kind == "push"``. Processes each
commit in the payload, and for commits that look like "Apply suggestion ..."
calls ``fetch_applied_fn`` to retrieve the discussion IDs that were applied,
then writes ``applied_at`` / ``apply_user`` into ``suggestion_threads``.

``fetch_applied_fn(project_id: int, commit_sha: str) -> list[str]`` is injected
so the module is easy to unit-test without real HTTP.  The production webhook
server provides a real GitLab API caller.  Never raises.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional

from pr_agent.log import get_logger
from pr_agent.suggestions.store import mark_applied, migrate_schema

# GitLab's commit message when a user clicks "Apply suggestion".  The exact
# wording varies by version, e.g.:
#   "Apply suggestion to <file>"           (older / single, no count)
#   "Apply 1 suggestion(s) to 1 file(s)"   (current GitLab default)
# Match "apply" followed by "suggestion", allowing an optional count between.
_APPLY_RE = re.compile(r"apply\s+(?:\d+\s+)?suggestion", re.IGNORECASE)


def is_apply_commit(message: str) -> bool:
    """Return True if the commit message indicates a suggestion was applied."""
    return bool(message and _APPLY_RE.search(message))


def handle_push_event(
    payload: dict,
    fetch_applied_fn: Callable[..., list[str]],
    path: Optional[str] = None,
    max_attempts: int = 1,
    retry_delay: float = 0.0,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> None:
    """Process a GitLab push webhook payload to detect applied suggestions.

    For every commit whose message matches ``is_apply_commit``, calls
    ``fetch_applied_fn(project_id, sha, ref)`` to get the list of discussion IDs
    that were applied, then stamps each with ``applied_at`` / ``apply_user``.

    GitLab's ``commits/{sha}/merge_requests`` association and a suggestion's
    ``applied`` flag are eventually consistent: right after the apply push the
    lookup may briefly return nothing.  When ``max_attempts > 1`` the fetch is
    retried with ``retry_delay`` seconds between attempts until it returns a
    non-empty result (or attempts are exhausted).

    Never raises.
    """
    sleep_fn = sleep_fn or time.sleep
    try:
        migrate_schema(path=path)
        project_id = (payload.get("project") or {}).get("id")
        ref = payload.get("ref") or ""
        apply_user = payload.get("user_username") or ""
        commits = payload.get("commits") or []

        for commit in commits:
            message = commit.get("message") or ""
            sha = commit.get("id") or ""
            if not is_apply_commit(message):
                continue
            get_logger().info(
                f"inline_apply_detector: apply commit detected sha={sha} ref={ref} user={apply_user}"
            )
            discussion_ids: list[str] = []
            for attempt in range(1, max(1, max_attempts) + 1):
                try:
                    discussion_ids = fetch_applied_fn(project_id, sha, ref) or []
                except Exception as e:
                    get_logger().warning(
                        f"inline_apply_detector: fetch_applied_fn failed for {sha} (attempt {attempt}): {e}"
                    )
                    discussion_ids = []
                if discussion_ids or attempt >= max(1, max_attempts):
                    break
                if retry_delay > 0:
                    sleep_fn(retry_delay)
            if not discussion_ids:
                get_logger().warning(
                    f"inline_apply_detector: no applied discussions resolved for apply commit "
                    f"sha={sha} ref={ref} after {max(1, max_attempts)} attempt(s)"
                )
                continue
            get_logger().info(
                f"inline_apply_detector: fetch resolved {len(discussion_ids)} applied discussion(s) for {sha}"
            )
            for disc_id in discussion_ids:
                marked = mark_applied(disc_id, apply_user=apply_user, path=path)
                get_logger().info(
                    f"inline_apply_detector: mark_applied discussion={disc_id} "
                    f"user={apply_user} matched={marked}"
                )
    except Exception as e:
        get_logger().error(f"handle_push_event failed: {e}")
