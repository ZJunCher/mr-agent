"""Manage local clones of a repository's target branches, keyed so that
multiple PRs targeting the same branch share one clone and one graph.

Storage layout under `storage_root` (the persistent volume PR-Agent already
mounts for feedback storage, e.g. `/app/data/code_graph`):

    <storage_root>/<repo_key>/<branch_key>/repo/       - shallow git clone
    <storage_root>/<repo_key>/<branch_key>/graph.db     - GraphStore SQLite file
    <storage_root>/<repo_key>/<branch_key>/graph_head.txt - last-indexed commit sha
    <storage_root>/<repo_key>/<branch_key>/.last_used   - touched on every use, drives cleanup_stale()

`repo_key` / `branch_key` are filesystem-safe hashes of the repo URL and
branch name, since branch names may contain characters that are not valid
path segments (e.g. slashes in "release/1.2").
"""

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

# Serializes clone/update operations per (repo, branch) key. PR-Agent's
# GitLab webhook server runs as a single uvicorn process (see
# `pr_agent/servers/gitlab_webhook.py`'s `uvicorn.run(...)`, no gunicorn
# multi-worker fan-out), the same assumption `pr_agent/feedback/store.py`
# already relies on for its write lock - so an in-process lock (not a
# filesystem lock) is sufficient to stop two concurrent MR reviews
# targeting the same branch from racing on the same `git fetch`/`reset
# --hard` working directory.
_clone_locks_guard = threading.Lock()
_clone_locks: Dict[str, threading.Lock] = {}
_logger = logging.getLogger(__name__)


def _lock_for(branch_dir: str) -> threading.Lock:
    with _clone_locks_guard:
        if branch_dir not in _clone_locks:
            _clone_locks[branch_dir] = threading.Lock()
        return _clone_locks[branch_dir]


def _safe_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def branch_storage_dir(storage_root: str, repo_url: str, branch: str) -> str:
    return os.path.join(storage_root, _safe_key(repo_url), _safe_key(branch))


def clone_or_update(clone_url: str, repo_url_for_key: str, branch: str, storage_root: str,
                     timeout_seconds: int) -> Optional[str]:
    """Ensure a local shallow clone of `branch` exists and is up to date.

    `clone_url` is the (possibly token-embedded) URL actually passed to
    `git`; `repo_url_for_key` is the plain repo URL used only to compute a
    stable storage key, so rotating an embedded access token does not
    change where the clone lives.

    Concurrent calls for the *same* (repo, branch) - e.g. two MRs targeting
    `main` reviewed at the same time - are serialized via an in-process
    lock keyed by the branch's storage directory, so they cannot race on
    the same `git` working directory. Calls for *different* branches never
    block each other.

    Returns the local repo directory path on success, or None on failure
    (never raises - callers should treat None as "skip code-graph context
    for this PR").
    """
    branch_dir = branch_storage_dir(storage_root, repo_url_for_key, branch)
    repo_dir = os.path.join(branch_dir, "repo")
    marker_path = os.path.join(branch_dir, ".last_used")

    with _lock_for(branch_dir):
        try:
            os.makedirs(branch_dir, exist_ok=True)
            if os.path.isdir(os.path.join(repo_dir, ".git")):
                _run_git(["fetch", "--depth", "1", "origin", branch], cwd=repo_dir, timeout=timeout_seconds)
                _run_git(["reset", "--hard", f"origin/{branch}"], cwd=repo_dir, timeout=timeout_seconds)
            else:
                if os.path.isdir(repo_dir):
                    shutil.rmtree(repo_dir, ignore_errors=True)
                _run_git(
                    ["clone", "--branch", branch, "--depth", "1", "--single-branch", clone_url, repo_dir],
                    cwd=None, timeout=timeout_seconds,
                )

            with open(marker_path, "w") as f:
                f.write(str(time.time()))

            return repo_dir
        except Exception as e:
            _logger.warning("code_graph: clone/update failed for branch '%s': %s", branch, e)
        return None


def changed_files_since(repo_dir: str, previous_head: Optional[str]) -> Tuple[Optional[List[str]], str]:
    """Return (changed_file_relpaths, new_head_sha) since `previous_head`.

    If `previous_head` is None or the diff against it fails (e.g. history
    was rewritten and the old commit is unreachable), returns None as the
    changed-files list to signal "caller must do a full rebuild" - callers
    distinguish this from an empty list (which means "no changes").
    """
    new_head = _run_git(["rev-parse", "HEAD"], cwd=repo_dir, timeout=30).strip()
    if not previous_head:
        return None, new_head
    try:
        diff_output = _run_git(["diff", "--name-only", previous_head, new_head], cwd=repo_dir, timeout=30)
    except subprocess.CalledProcessError:
        return None, new_head
    changed = [line.strip() for line in diff_output.splitlines() if line.strip()]
    return changed, new_head


def cleanup_stale(storage_root: str, ttl_days: int) -> List[str]:
    """Delete branch storage directories not used in the last `ttl_days`.

    Returns the list of directories removed, for logging/testing.
    """
    removed: List[str] = []
    if not os.path.isdir(storage_root):
        return removed

    cutoff = time.time() - (ttl_days * 86400)
    for repo_key in os.listdir(storage_root):
        repo_dir = os.path.join(storage_root, repo_key)
        if not os.path.isdir(repo_dir):
            continue
        for branch_key in os.listdir(repo_dir):
            branch_dir = os.path.join(repo_dir, branch_key)
            marker_path = os.path.join(branch_dir, ".last_used")
            if not os.path.isfile(marker_path):
                continue
            try:
                with open(marker_path) as f:
                    last_used = float(f.read().strip())
            except (OSError, ValueError):
                continue
            if last_used < cutoff:
                shutil.rmtree(branch_dir, ignore_errors=True)
                removed.append(branch_dir)

    return removed


def cleanup_stale_if_due(storage_root: str, ttl_days: int, min_interval_seconds: int = 86400) -> List[str]:
    """Run `cleanup_stale` at most once per `min_interval_seconds`.

    Re-scanning every branch directory's `.last_used` marker on every
    single PR review would be wasteful (most reviews touch one branch that
    is obviously not stale). This throttles the sweep behind a single
    marker file at the storage root, so it runs at most once a day
    (default) regardless of how many reviews happen in between.
    """
    if not os.path.isdir(storage_root):
        os.makedirs(storage_root, exist_ok=True)

    throttle_marker = os.path.join(storage_root, ".last_cleanup_at")
    now = time.time()
    if os.path.isfile(throttle_marker):
        try:
            with open(throttle_marker) as f:
                last_cleanup_at = float(f.read().strip())
            if now - last_cleanup_at < min_interval_seconds:
                return []
        except (OSError, ValueError):
            pass  # malformed marker: fall through and run the sweep

    removed = cleanup_stale(storage_root, ttl_days)
    with open(throttle_marker, "w") as f:
        f.write(str(now))
    return removed


def _run_git(args: List[str], cwd: Optional[str], timeout: int) -> str:
    result = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=True)
    return result.stdout
