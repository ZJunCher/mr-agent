import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent.suggestions.prompt_evolution.gitlab_publisher import (
    BaseBranchMoved,
    GitLabPromptPublisher,
    HumanModifiedBranch,
)
from pr_agent.suggestions.prompt_evolution.models import PromptFileChange


class RecordingManager:
    def __init__(self, create_result=None):
        self.created = []
        self.create_result = create_result
        self.items = []

    def create(self, payload):
        self.created.append(payload)
        if self.create_result is not None:
            self.items.append(self.create_result)
        return self.create_result

    def list(self, **kwargs):
        return list(self.items)

    def get(self, item_id):
        for item in self.items:
            if str(getattr(item, "iid", getattr(item, "id", ""))) == str(item_id):
                return item
        raise KeyError(item_id)


class FakeBranches(RecordingManager):
    def __init__(self, base_sha):
        super().__init__()
        self.items = {"main": SimpleNamespace(commit={"id": base_sha})}

    def get(self, name):
        if name not in self.items:
            error = RuntimeError("not found")
            error.response_code = 404
            raise error
        return self.items[name]

    def create(self, payload):
        self.created.append(payload)
        branch = SimpleNamespace(commit={"id": payload["ref"]})
        self.items[payload["branch"]] = branch
        return branch


class FakeProject:
    def __init__(self, base_sha):
        self.branches = FakeBranches(base_sha)
        self.commits = RecordingManager(SimpleNamespace(
            id="c" * 40, message="chore(prompt): weekly improve evolution\n\nPrompt-Evolution-Batch: 2026-w34-a1b2c3d4"))
        mr = SimpleNamespace(iid="1", web_url="https://gitlab.example/group/pr-agent/-/merge_requests/1",
                             state="opened", source_branch="codex/prompt-evolution/2026-w34-a1b2c3d4",
                             merge=Mock())
        self.mergerequests = RecordingManager(mr)


def _changes(*paths):
    return tuple(PromptFileChange(path=p, family="tier1_repair", expected_base_sha256="old",
                                  content="new", evidence_ids=("s1",)) for p in paths)


def test_publish_creates_one_atomic_commit_and_draft_mr():
    project = FakeProject(base_sha="a" * 40)
    publisher = GitLabPromptPublisher(project)
    changes = _changes("pr_agent/settings/pr_tier1_repair_prompts.toml")

    result = asyncio.run(publisher.publish_draft_mr(
        batch_id="2026-w34-a1b2c3d4",
        branch_name="codex/prompt-evolution/2026-w34-a1b2c3d4",
        target_branch="main",
        base_sha="a" * 40,
        changes=changes,
        description="evidence",
        assert_fence=AsyncMock(),
    ))

    assert len(project.commits.created) == 1
    assert project.commits.created[0]["actions"][0]["file_path"].endswith("pr_tier1_repair_prompts.toml")
    assert project.mergerequests.created[0]["title"].startswith("Draft:")
    assert result.mr_url.endswith("/merge_requests/1")


def test_existing_batch_branch_and_open_mr_are_reused():
    project = FakeProject(base_sha="a" * 40)
    publisher = GitLabPromptPublisher(project)
    # Pre-create the branch whose head commit carries the batch trailer, plus an open MR.
    batch_commit = SimpleNamespace(
        id="c" * 40,
        message="chore(prompt): weekly improve evolution\n\nPrompt-Evolution-Batch: 2026-w34-a1b2c3d4")
    project.commits.items.append(batch_commit)
    project.branches.items["codex/prompt-evolution/2026-w34-a1b2c3d4"] = SimpleNamespace(
        commit={"id": "c" * 40})
    project.mergerequests.items.append(SimpleNamespace(
        iid="1", web_url="https://gitlab.example/group/pr-agent/-/merge_requests/1",
        state="opened", source_branch="codex/prompt-evolution/2026-w34-a1b2c3d4", merge=Mock()))

    result = asyncio.run(publisher.publish_draft_mr(
        batch_id="2026-w34-a1b2c3d4",
        branch_name="codex/prompt-evolution/2026-w34-a1b2c3d4",
        target_branch="main",
        base_sha="a" * 40,
        changes=_changes("pr_agent/settings/pr_tier1_repair_prompts.toml"),
        description="evidence",
        assert_fence=AsyncMock(),
    ))

    assert len(project.branches.created) == 0
    assert len(project.commits.created) == 0
    assert len(project.mergerequests.created) == 0
    assert result.mr_iid == "1"


def test_human_modified_branch_fails_closed():
    project = FakeProject(base_sha="a" * 40)
    publisher = GitLabPromptPublisher(project)
    # Branch exists but head is neither base_sha nor recorded commit SHA nor batch trailer.
    project.branches.items["codex/prompt-evolution/2026-w34-a1b2c3d4"] = SimpleNamespace(
        commit={"id": "deadbeef"})

    with pytest.raises(HumanModifiedBranch):
        asyncio.run(publisher.publish_draft_mr(
            batch_id="2026-w34-a1b2c3d4",
            branch_name="codex/prompt-evolution/2026-w34-a1b2c3d4",
            target_branch="main",
            base_sha="a" * 40,
            changes=_changes("pr_agent/settings/pr_tier1_repair_prompts.toml"),
            description="evidence",
            assert_fence=AsyncMock(),
        ))
    assert len(project.commits.created) == 0
    assert len(project.mergerequests.created) == 0


def test_target_branch_moved_before_commit():
    project = FakeProject(base_sha="a" * 40)
    publisher = GitLabPromptPublisher(project)
    # Target branch head differs from frozen base_sha.
    project.branches.items["main"] = SimpleNamespace(commit={"id": "b" * 40})

    with pytest.raises(BaseBranchMoved):
        asyncio.run(publisher.publish_draft_mr(
            batch_id="2026-w34-a1b2c3d4",
            branch_name="codex/prompt-evolution/2026-w34-a1b2c3d4",
            target_branch="main",
            base_sha="a" * 40,
            changes=_changes("pr_agent/settings/pr_tier1_repair_prompts.toml"),
            description="evidence",
            assert_fence=AsyncMock(),
        ))
    assert len(project.commits.created) == 0
    assert len(project.mergerequests.created) == 0


def test_commit_timeout_uses_read_after_write_discovery():
    project = FakeProject(base_sha="a" * 40)
    publisher = GitLabPromptPublisher(project)
    # First commit create stores the commit then raises timeout.
    call_count = [0]
    original_create = project.commits.create

    def flaky_create(payload):
        call_count[0] += 1
        if call_count[0] == 1:
            project.commits.items.append(original_create.__self__.create_result)
            err = TimeoutError("timeout")
            err.response_code = 408
            raise err
        return original_create(payload)

    project.commits.create = flaky_create

    result = asyncio.run(publisher.publish_draft_mr(
        batch_id="2026-w34-a1b2c3d4",
        branch_name="codex/prompt-evolution/2026-w34-a1b2c3d4",
        target_branch="main",
        base_sha="a" * 40,
        changes=_changes("pr_agent/settings/pr_tier1_repair_prompts.toml"),
        description="evidence",
        assert_fence=AsyncMock(),
    ))
    assert result.mr_iid == "1"


def test_mr_timeout_uses_source_branch_discovery():
    project = FakeProject(base_sha="a" * 40)
    publisher = GitLabPromptPublisher(project)
    call_count = [0]
    original_create = project.mergerequests.create

    def flaky_create(payload):
        call_count[0] += 1
        if call_count[0] == 1:
            project.mergerequests.items.append(original_create.__self__.create_result)
            err = TimeoutError("timeout")
            err.response_code = 408
            raise err
        return original_create(payload)

    project.mergerequests.create = flaky_create

    result = asyncio.run(publisher.publish_draft_mr(
        batch_id="2026-w34-a1b2c3d4",
        branch_name="codex/prompt-evolution/2026-w34-a1b2c3d4",
        target_branch="main",
        base_sha="a" * 40,
        changes=_changes("pr_agent/settings/pr_tier1_repair_prompts.toml"),
        description="evidence",
        assert_fence=AsyncMock(),
    ))
    assert result.mr_iid == "1"


def test_publisher_has_no_merge_operation():
    project = FakeProject(base_sha="a" * 40)
    publisher = GitLabPromptPublisher(project)
    asyncio.run(publisher.publish_draft_mr(
        batch_id="2026-w34-a1b2c3d4",
        branch_name="codex/prompt-evolution/2026-w34-a1b2c3d4",
        target_branch="main",
        base_sha="a" * 40,
        changes=_changes("pr_agent/settings/pr_tier1_repair_prompts.toml"),
        description="evidence",
        assert_fence=AsyncMock(),
    ))
    assert not hasattr(publisher, "merge")
    project.mergerequests.items[0].merge.assert_not_called()


def test_project_skill_publish_surface_is_one_fixed_manifest_and_draft_title():
    project = FakeProject(base_sha="a" * 40)
    publisher = GitLabPromptPublisher(project)
    changes = (PromptFileChange(
        path=".pr_agent/skills/review/skill.toml",
        family="project_rule",
        expected_base_sha256="old",
        content='schema_version = 1\nproject = "group/project"\n',
        evidence_ids=("s1",),
    ),)

    asyncio.run(publisher.publish_draft_mr(
        batch_id="2026-w34-project",
        branch_name="codex/review-skill-evolution/2026-w34-project",
        target_branch="main",
        base_sha="a" * 40,
        changes=changes,
        description="## Project Review Skill evolution",
        assert_fence=AsyncMock(),
    ))

    assert project.commits.created[0]["actions"] == [{
        "action": "update",
        "file_path": ".pr_agent/skills/review/skill.toml",
        "content": 'schema_version = 1\nproject = "group/project"\n',
    }]
    assert project.mergerequests.created[0]["title"].startswith("Draft: Project Review Skill")
