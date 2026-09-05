"""Resolve the Pipeline identity produced by one repair task."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class RepairResultIdentity:
    pipeline_id: int = 0
    commit_sha: str = ""
    pipeline_status: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.pipeline_id and self.commit_sha)


def resolve_repair_result_identity(
    manifest,
    repair_actions: Iterable,
    *,
    current_pipeline_id: int = 0,
    current_pipeline_sha: str = "",
    current_pipeline_status: str = "",
) -> RepairResultIdentity:
    """Return only a Pipeline proven to validate a commit produced by this repair task."""
    entries = tuple(getattr(manifest, "entries", ()) or ())
    manifest_sha = str(getattr(entries[-1], "commit_sha", "") or "") if entries else ""
    actions = tuple(repair_actions or ())
    candidates = [
        action
        for action in actions
        if str(getattr(action, "commit_sha", "") or "")
        and (not manifest_sha or str(getattr(action, "commit_sha", "") or "") == manifest_sha)
    ]
    action = candidates[-1] if candidates else None
    commit_sha = manifest_sha or str(getattr(action, "commit_sha", "") or "")
    if not commit_sha:
        return RepairResultIdentity()

    action_pipeline_id = int(getattr(action, "validation_pipeline_id", 0) or 0) if action is not None else 0
    action_status = str(getattr(action, "validation_status", "") or "") if action is not None else ""
    if action_pipeline_id:
        return RepairResultIdentity(action_pipeline_id, commit_sha, action_status)
    if current_pipeline_id and current_pipeline_sha == commit_sha:
        return RepairResultIdentity(current_pipeline_id, commit_sha, current_pipeline_status)
    return RepairResultIdentity()
