"""Reconcile real GitLab Discussions-API state into published_suggestions,
then recompute the inline-suggestion gate for that MR.

GitLab does not emit a webhook for a single "Resolve thread" click (verified:
resolved/resolved_by/resolved_at live on the existing note object, and no new
note is created; only a fully-resolved MR triggers a Merge Request Hook).
So every relevant webhook event (push / note / merge_request update) is
treated purely as a "go check now" trigger: this module queries the
Discussions API for the real state and updates the store accordingly. Never
raises.
"""

from typing import Callable, Optional

from pr_agent.log import get_logger
from pr_agent.suggestions import inline_gate_status
from pr_agent.suggestions.store import get_published_suggestions, sync_thread_state


def sync_mr_threads(git_provider, project, mr_iid,
                    fetch_discussions_fn: Callable[[object, object], list],
                    path: Optional[str] = None) -> None:
    """Sync applied/resolved state for every published suggestion on this MR,
    then recompute the gate's commit status. Never raises."""
    try:
        threads = get_published_suggestions(project, mr_iid, path=path)
        if not threads:
            return

        discussions = []
        try:
            discussions = fetch_discussions_fn(project, mr_iid) or []
        except Exception as e:
            get_logger().warning(f"inline_thread_sync: fetch_discussions_fn failed: {e}")
            discussions = []

        note_by_disc = {}
        for d in discussions:
            notes = d.get("notes") or []
            if notes:
                note_by_disc[str(d.get("id"))] = notes[0]

        for t in threads:
            disc_id = str(t.get("gitlab_discussion_id") or "")
            note = note_by_disc.get(disc_id)
            if not note:
                continue
            applied = any(sg.get("applied") for sg in (note.get("suggestions") or []))
            resolved = bool(note.get("resolved"))
            resolve_user = ((note.get("resolved_by") or {}).get("username") or "") if resolved else ""
            sync_thread_state(disc_id, applied=applied, resolved=resolved, resolve_user=resolve_user, path=path)

        inline_gate_status.recompute(git_provider, project, mr_iid, path=path)
    except Exception as e:
        get_logger().error(f"sync_mr_threads failed for {project}/{mr_iid}: {e}")
