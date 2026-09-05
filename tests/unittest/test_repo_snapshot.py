"""Canonical repository snapshot tests for Native Repair."""

import os
import subprocess

import pytest

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing ut_agent.
from ut_agent.tools.repo_snapshot import (
    capture_staged_snapshot,
    capture_worktree_snapshot,
    check_worktree_diff,
)


def _git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")
    _git(repo_dir, "config", "core.filemode", "true")
    (repo_dir / "src.py").write_text("VALUE = 1\n")
    (repo_dir / "obsolete.txt").write_text("remove me\n")
    (repo_dir / "script.sh").write_text("#!/bin/sh\nexit 0\n")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "initial")
    return repo_dir


def test_worktree_snapshot_includes_all_commit_relevant_files(repo):
    (repo / "src.py").write_text("VALUE = 2\n")
    (repo / "new_test.py").write_text("def test_value():\n    assert 2 == 2\n")
    (repo / "obsolete.txt").unlink()

    snapshot = capture_worktree_snapshot(str(repo))

    assert {item.path: item.status for item in snapshot.changed_files} == {
        "new_test.py": "added",
        "obsolete.txt": "deleted",
        "src.py": "modified",
    }
    assert snapshot.diff_digest.startswith("sha256:")
    assert len(snapshot.diff_digest) == 71
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_snapshot_digest_is_stable_and_changes_with_content(repo):
    (repo / "src.py").write_text("VALUE = 2\n")

    first = capture_worktree_snapshot(str(repo))
    second = capture_worktree_snapshot(str(repo))

    assert second.diff_digest == first.diff_digest
    (repo / "src.py").write_text("VALUE = 3\n")
    assert capture_worktree_snapshot(str(repo)).diff_digest != first.diff_digest


def test_staged_snapshot_matches_worktree_snapshot_after_add_all(repo):
    (repo / "src.py").write_text("VALUE = 2\n")
    (repo / "new.py").write_text("NEW = True\n")
    worktree = capture_worktree_snapshot(str(repo))

    _git(repo, "add", "-A")
    staged = capture_staged_snapshot(str(repo))

    assert staged.base_sha == worktree.base_sha
    assert staged.diff_bytes == worktree.diff_bytes
    assert staged.diff_digest == worktree.diff_digest


def test_snapshot_includes_rename_and_executable_mode(repo):
    _git(repo, "mv", "obsolete.txt", "renamed.txt")
    os.chmod(repo / "script.sh", 0o755)

    snapshot = capture_worktree_snapshot(str(repo))
    statuses = {item.path: item.status for item in snapshot.changed_files}

    assert statuses["renamed.txt"] == "renamed"
    assert statuses["script.sh"] in {"modified", "type_changed"}


def test_snapshot_includes_binary_file(repo):
    (repo / "blob.bin").write_bytes(b"\x00\x01\xff\x02")

    snapshot = capture_worktree_snapshot(str(repo))
    binary = next(item for item in snapshot.changed_files if item.path == "blob.bin")

    assert binary.status == "added"
    assert binary.additions is None
    assert binary.deletions is None
    assert b"GIT binary patch" in snapshot.diff_bytes


def test_check_worktree_diff_includes_untracked_files(repo):
    (repo / "new.py").write_text("VALUE = 1  \n")

    passed, output = check_worktree_diff(str(repo))

    assert passed is False
    assert "trailing whitespace" in output
    assert _git(repo, "diff", "--cached", "--name-only") == ""
