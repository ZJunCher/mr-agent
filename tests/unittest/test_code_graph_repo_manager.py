import os
import subprocess
import tempfile
import time

import pytest

from pr_agent.algo.code_graph.repo_manager import (
    branch_storage_dir, changed_files_since, cleanup_stale, cleanup_stale_if_due, clone_or_update,
)


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def source_repo():
    """A tiny real git repo on disk with one commit on branch `main`, used
    as the `clone_url` (a local filesystem path works fine for `git clone`)."""
    with tempfile.TemporaryDirectory() as repo_dir:
        _run(["git", "init", "-b", "main"], cwd=repo_dir)
        _run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir)
        _run(["git", "config", "user.name", "Test"], cwd=repo_dir)
        with open(os.path.join(repo_dir, "a.py"), "w") as f:
            f.write("VALUE = 1\n")
        _run(["git", "add", "a.py"], cwd=repo_dir)
        _run(["git", "commit", "-m", "initial"], cwd=repo_dir)
        yield repo_dir


def test_clone_or_update_first_call_clones(source_repo):
    with tempfile.TemporaryDirectory() as storage_root:
        repo_dir = clone_or_update(source_repo, source_repo, "main", storage_root, timeout_seconds=30)
        assert repo_dir is not None
        assert os.path.isdir(os.path.join(repo_dir, ".git"))
        assert os.path.isfile(os.path.join(repo_dir, "a.py"))


def test_clone_or_update_second_call_updates_in_place(source_repo):
    with tempfile.TemporaryDirectory() as storage_root:
        repo_dir_1 = clone_or_update(source_repo, source_repo, "main", storage_root, timeout_seconds=30)

        with open(os.path.join(source_repo, "b.py"), "w") as f:
            f.write("VALUE = 2\n")
        _run(["git", "add", "b.py"], cwd=source_repo)
        _run(["git", "commit", "-m", "second"], cwd=source_repo)

        repo_dir_2 = clone_or_update(source_repo, source_repo, "main", storage_root, timeout_seconds=30)
        assert repo_dir_1 == repo_dir_2
        assert os.path.isfile(os.path.join(repo_dir_2, "b.py"))


def test_clone_or_update_returns_none_for_invalid_url():
    with tempfile.TemporaryDirectory() as storage_root:
        result = clone_or_update("/path/does/not/exist", "/path/does/not/exist", "main", storage_root, timeout_seconds=5)
        assert result is None


def test_branch_storage_dir_is_stable_and_filesystem_safe():
    d1 = branch_storage_dir("/data", "https://gitlab.example.com/team/repo.git", "release/1.2")
    d2 = branch_storage_dir("/data", "https://gitlab.example.com/team/repo.git", "release/1.2")
    assert d1 == d2
    assert os.path.dirname(d1) != "/data"  # repository and branch are intentionally separate storage segments
    assert "/" not in os.path.basename(d1)  # no raw slash from the branch name leaks into its segment


def test_changed_files_since_none_previous_head_signals_full_rebuild(source_repo):
    with tempfile.TemporaryDirectory() as storage_root:
        repo_dir = clone_or_update(source_repo, source_repo, "main", storage_root, timeout_seconds=30)
        changed, new_head = changed_files_since(repo_dir, previous_head=None)
        assert changed is None
        assert new_head  # a real commit sha string


def test_changed_files_since_lists_changed_paths(source_repo):
    with tempfile.TemporaryDirectory() as storage_root:
        repo_dir = clone_or_update(source_repo, source_repo, "main", storage_root, timeout_seconds=30)
        _, head_1 = changed_files_since(repo_dir, previous_head=None)

        with open(os.path.join(source_repo, "c.py"), "w") as f:
            f.write("VALUE = 3\n")
        _run(["git", "add", "c.py"], cwd=source_repo)
        _run(["git", "commit", "-m", "third"], cwd=source_repo)
        clone_or_update(source_repo, source_repo, "main", storage_root, timeout_seconds=30)

        changed, head_2 = changed_files_since(repo_dir, previous_head=head_1)
        assert changed == ["c.py"]
        assert head_2 != head_1


def test_cleanup_stale_removes_old_and_keeps_fresh(source_repo):
    with tempfile.TemporaryDirectory() as storage_root:
        clone_or_update(source_repo, source_repo, "main", storage_root, timeout_seconds=30)
        branch_dir = branch_storage_dir(storage_root, source_repo, "main")
        marker = os.path.join(branch_dir, ".last_used")

        old_time = time.time() - (20 * 86400)  # 20 days ago, older than the 15-day TTL
        with open(marker, "w") as f:
            f.write(str(old_time))

        removed = cleanup_stale(storage_root, ttl_days=15)
        assert branch_dir in removed
        assert not os.path.isdir(branch_dir)


def test_cleanup_stale_keeps_recently_used_branch(source_repo):
    with tempfile.TemporaryDirectory() as storage_root:
        clone_or_update(source_repo, source_repo, "main", storage_root, timeout_seconds=30)
        branch_dir = branch_storage_dir(storage_root, source_repo, "main")

        removed = cleanup_stale(storage_root, ttl_days=15)
        assert branch_dir not in removed
        assert os.path.isdir(branch_dir)


def test_cleanup_stale_if_due_runs_on_first_call(source_repo):
    with tempfile.TemporaryDirectory() as storage_root:
        clone_or_update(source_repo, source_repo, "main", storage_root, timeout_seconds=30)
        branch_dir = branch_storage_dir(storage_root, source_repo, "main")
        old_time = time.time() - (20 * 86400)
        with open(os.path.join(branch_dir, ".last_used"), "w") as f:
            f.write(str(old_time))

        removed = cleanup_stale_if_due(storage_root, ttl_days=15, min_interval_seconds=86400)
        assert branch_dir in removed
        assert os.path.isfile(os.path.join(storage_root, ".last_cleanup_at"))


def test_cleanup_stale_if_due_is_throttled_on_second_call(source_repo):
    with tempfile.TemporaryDirectory() as storage_root:
        clone_or_update(source_repo, source_repo, "main", storage_root, timeout_seconds=30)
        branch_dir = branch_storage_dir(storage_root, source_repo, "main")

        # First call establishes the throttle marker (nothing stale yet).
        cleanup_stale_if_due(storage_root, ttl_days=15, min_interval_seconds=86400)

        old_time = time.time() - (20 * 86400)
        with open(os.path.join(branch_dir, ".last_used"), "w") as f:
            f.write(str(old_time))

        # Second call within the same day is throttled: branch_dir survives
        # even though it is now stale, because the sweep did not run again.
        removed = cleanup_stale_if_due(storage_root, ttl_days=15, min_interval_seconds=86400)
        assert removed == []
        assert os.path.isdir(branch_dir)
