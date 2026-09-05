"""Restricted GitLab adapter for Prompt evolution Draft MRs.

Creates at most one branch, one Commit, and one Draft MR per batch. Never
merges. All paths are validated against ``prompt_surface.is_allowed_prompt_path``
before any GitLab write. Timeouts trigger read-after-write discovery so a
timeout after a successful write is not retried as a duplicate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from pr_agent.suggestions.prompt_evolution.models import MISSING_FILE_HASH, PromptFileChange, PublishedDraft
from pr_agent.suggestions.prompt_evolution.prompt_surface import is_allowed_prompt_path

_BATCH_TRAILER_PREFIX = "Prompt-Evolution-Batch:"
_MAX_FILES_PER_MR = 20


class BaseBranchMoved(Exception):
    """Target branch moved between workspace snapshot and commit."""


class HumanModifiedBranch(Exception):
    """Source branch head was modified by a human, not the runner."""


@dataclass(frozen=True)
class PromptWorkspace:
    project_path: str
    target_branch: str
    base_sha: str
    files: dict[str, str | None]


def _decode_file(file_obj) -> str:
    value = file_obj.decode()
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def validate_publisher_paths(changes: Iterable[PromptFileChange]) -> tuple[PromptFileChange, ...]:
    """Defense-in-depth: reject empty, duplicate, non-TOML, or off-whitelist paths."""
    validated = tuple(changes)
    if not validated:
        raise ValueError("empty change set")
    if len(validated) > _MAX_FILES_PER_MR:
        raise ValueError("change set exceeds max_files_per_mr")
    seen: set[str] = set()
    for change in validated:
        if change.path in seen:
            raise ValueError(f"duplicate path: {change.path}")
        seen.add(change.path)
        if not change.path.endswith(".toml"):
            raise ValueError(f"non-TOML path: {change.path}")
        if not is_allowed_prompt_path(change.path):
            raise ValueError(f"path outside prompt whitelist: {change.path}")
    return validated


class GitLabPromptPublisher:
    """Restricted GitLab adapter: branch + Commit + Draft MR, never merge."""

    def __init__(self, project) -> None:
        self.project = project

    def get_target_head(self, target_branch: str) -> str:
        return str(self.project.branches.get(target_branch).commit["id"])

    def get_default_branch(self) -> str:
        return str(getattr(self.project, "default_branch", "") or "main")

    def load_workspace(self, project_path: str, target_branch: str, base_sha: str,
                       paths: tuple[str, ...]) -> PromptWorkspace:
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                raise ValueError(f"duplicate path: {path}")
            seen.add(path)
            if not path.endswith(".toml"):
                raise ValueError(f"non-TOML path: {path}")
            if not is_allowed_prompt_path(path):
                raise ValueError(f"path outside prompt whitelist: {path}")
        files: dict[str, str | None] = {}
        for path in paths:
            try:
                files[path] = _decode_file(self.project.files.get(path, ref=base_sha))
            except Exception as exc:
                if getattr(exc, "response_code", None) == 404:
                    files[path] = None
                else:
                    raise
        return PromptWorkspace(project_path, target_branch, base_sha, files)

    def get_mr_state(self, mr_iid: str) -> str:
        return str(self.project.mergerequests.get(mr_iid).state or "").lower()

    def _get_or_create_owned_branch(self, branch_name: str, base_sha: str, batch_id: str):
        try:
            branch = self.project.branches.get(branch_name)
        except Exception as exc:
            if getattr(exc, "response_code", None) != 404:
                raise
            branch = None
        if branch is not None:
            head = str(branch.commit["id"])
            if head == base_sha:
                return branch  # branch created but commit not yet written
            # Check if head is the recorded batch commit (has trailer).
            if self._branch_head_has_batch_trailer(branch_name, head, batch_id):
                return branch
            raise HumanModifiedBranch(f"branch {branch_name} head {head} is not owned by batch {batch_id}")
        # Create the branch.
        self.project.branches.create({"branch": branch_name, "ref": base_sha})
        return self.project.branches.get(branch_name)

    def _branch_head_has_batch_trailer(self, branch_name: str, head: str, batch_id: str) -> bool:
        # Look up commits on the branch to find the batch trailer.
        try:
            commits = self.project.commits.list(branch_name=branch_name) if hasattr(self.project.commits, "list") else []
        except Exception:
            commits = []
        for commit in commits:
            cid = str(getattr(commit, "id", "") or "")
            message = str(getattr(commit, "message", "") or "")
            if cid == head and f"{_BATCH_TRAILER_PREFIX} {batch_id}" in message:
                return True
        return False

    def _get_or_create_batch_commit(self, branch, batch_id: str, changes: tuple[PromptFileChange, ...]):
        # If the branch head already has the batch trailer, the commit exists.
        head = str(branch.commit["id"])
        if self._branch_head_has_batch_trailer(str(self._branch_name_from_ref(branch)), head, batch_id):
            return self.project.commits.get(head) if hasattr(self.project.commits, "get") else self._find_commit_by_sha(head)
        payload = self._commit_payload(str(self._branch_name_from_ref(branch)), batch_id, changes)
        try:
            return self.project.commits.create(payload)
        except Exception as exc:
            if not self._is_timeout(exc):
                raise
            # Read-after-write discovery: the commit may have been stored before the timeout.
            found = self._find_commit_by_trailer(str(self._branch_name_from_ref(branch)), batch_id)
            if found is not None:
                return found
            raise

    def _branch_name_from_ref(self, branch) -> str:
        # Branch objects from python-gitlab don't carry their name directly in tests;
        # we look it up by matching the head. For production, branch.name exists.
        name = getattr(branch, "name", None)
        if name:
            return name
        for bname, bobj in getattr(self.project.branches, "items", {}).items():
            if bobj is branch or str(bobj.commit.get("id", "")) == str(branch.commit.get("id", "")):
                return bname
        return ""

    def _commit_payload(self, branch_name: str, batch_id: str, changes: tuple[PromptFileChange, ...]) -> dict:
        project_skill_only = all(change.family == "project_rule" for change in changes)
        subject = (
            "chore(review): evolve project Skill"
            if project_skill_only
            else "chore(prompt): weekly improve evolution"
        )
        return {
            "branch": branch_name,
            "commit_message": f"{subject}\n\n{_BATCH_TRAILER_PREFIX} {batch_id}",
            "actions": [
                {
                    "action": "update" if change.expected_base_sha256 != MISSING_FILE_HASH else "create",
                    "file_path": change.path,
                    "content": change.content,
                }
                for change in changes
            ],
        }

    def _get_or_create_draft_mr(self, branch_name: str, target_branch: str, batch_id: str, description: str):
        # Look for an existing open MR on this source branch.
        try:
            existing = self.project.mergerequests.list(source_branch=branch_name) if hasattr(self.project.mergerequests, "list") else []
        except Exception:
            existing = []
        for mr in existing:
            if str(getattr(mr, "source_branch", "") or "") == branch_name and str(getattr(mr, "state", "")).lower() == "opened":
                return mr
        is_project_skill = "Project Review Skill evolution" in description
        payload = {
            "source_branch": branch_name,
            "target_branch": target_branch,
            "title": (
                f"Draft: Project Review Skill evolution ({batch_id})"
                if is_project_skill
                else f"Draft: weekly /improve Prompt evolution ({batch_id})"
            ),
            "description": description,
            "remove_source_branch": False,
            "labels": "prompt-evolution,human-review-required",
        }
        try:
            return self.project.mergerequests.create(payload)
        except Exception as exc:
            if not self._is_timeout(exc):
                raise
            found = self._find_open_mr_by_branch(branch_name)
            if found is not None:
                return found
            raise

    def _find_open_mr_by_branch(self, branch_name: str):
        try:
            items = self.project.mergerequests.list(source_branch=branch_name) if hasattr(self.project.mergerequests, "list") else []
        except Exception:
            items = []
        for mr in items:
            if str(getattr(mr, "source_branch", "") or "") == branch_name and str(getattr(mr, "state", "")).lower() == "opened":
                return mr
        return None

    def _find_commit_by_trailer(self, branch_name: str, batch_id: str):
        try:
            commits = self.project.commits.list(branch_name=branch_name) if hasattr(self.project.commits, "list") else []
        except Exception:
            commits = []
        for commit in commits:
            message = str(getattr(commit, "message", "") or "")
            if f"{_BATCH_TRAILER_PREFIX} {batch_id}" in message:
                return commit
        return None

    def _find_commit_by_sha(self, sha: str):
        try:
            return self.project.commits.get(sha) if hasattr(self.project.commits, "get") else None
        except Exception:
            return None

    @staticmethod
    def _is_timeout(exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        code = getattr(exc, "response_code", None)
        return code in (408, 504)

    async def publish_draft_mr(
        self,
        *,
        batch_id: str,
        branch_name: str,
        target_branch: str,
        base_sha: str,
        changes: Iterable[PromptFileChange],
        description: str,
        assert_fence: Callable[[], Awaitable[None]],
    ) -> PublishedDraft:
        validated_changes = validate_publisher_paths(changes)
        await assert_fence()
        if self.get_target_head(target_branch) != base_sha:
            raise BaseBranchMoved("target branch moved after workspace snapshot")
        branch = self._get_or_create_owned_branch(branch_name, base_sha, batch_id)
        await assert_fence()
        commit = self._get_or_create_batch_commit(branch, batch_id, validated_changes)
        await assert_fence()
        mr = self._get_or_create_draft_mr(branch_name, target_branch, batch_id, description)
        return PublishedDraft(str(getattr(commit, "id", "")), str(mr.iid), str(mr.web_url))
