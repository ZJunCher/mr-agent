"""Canonical Git snapshots shared by Native Repair safety gates."""

import hashlib
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

_GIT_TIMEOUT_SECONDS = 120
_ERROR_LIMIT = 2000


class RepoSnapshotError(RuntimeError):
    """Raised when a canonical repository snapshot cannot be produced."""


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    additions: int | None
    deletions: int | None


@dataclass(frozen=True)
class RepoSnapshot:
    base_sha: str
    diff_bytes: bytes
    diff_digest: str
    total_lines: int
    changed_files: tuple[ChangedFile, ...]
    diff_stat: str

    @property
    def diff_text(self) -> str:
        return self.diff_bytes.decode("utf-8", errors="replace")


def digest_diff(diff: bytes | str) -> str:
    """Return the full SHA-256 identity for canonical Diff content."""
    raw = diff if isinstance(diff, bytes) else diff.encode("utf-8", errors="surrogateescape")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _run_git(
    repo_dir: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            env=env,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoSnapshotError(f"git {args[0]} 超时 ({_GIT_TIMEOUT_SECONDS}s)") from exc
    except OSError as exc:
        raise RepoSnapshotError(f"git {args[0]} 无法启动: {exc}") from exc
    if result.returncode not in allowed_returncodes:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")[:_ERROR_LIMIT].strip()
        raise RepoSnapshotError(f"git {args[0]} 失败 (exit={result.returncode}): {detail}")
    return result


@contextmanager
def _temporary_worktree_index(repo_dir: str) -> Iterator[dict[str, str]]:
    """Yield an environment whose index represents HEAD plus all worktree changes."""
    fd, index_path = tempfile.mkstemp(prefix="ut-agent-index-")
    os.close(fd)
    os.unlink(index_path)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = index_path
    try:
        _run_git(repo_dir, ["read-tree", "HEAD"], env=env)
        _run_git(repo_dir, ["add", "-A"], env=env)
        yield env
    finally:
        try:
            os.unlink(index_path)
        except FileNotFoundError:
            pass


def _decode_path(value: bytes) -> str:
    return os.fsdecode(value)


def _parse_name_status(raw: bytes) -> list[tuple[str, str]]:
    tokens = raw.split(b"\0")
    changes = []
    index = 0
    while index < len(tokens) and tokens[index]:
        status_code = tokens[index].decode("ascii", errors="replace")
        index += 1
        kind = status_code[:1]
        if kind in {"R", "C"}:
            if index + 1 >= len(tokens):
                raise RepoSnapshotError("git name-status rename output is incomplete")
            index += 1  # The old path is evidence only; the final path identifies the committed file.
            path = _decode_path(tokens[index])
            index += 1
        else:
            if index >= len(tokens):
                raise RepoSnapshotError("git name-status output is incomplete")
            path = _decode_path(tokens[index])
            index += 1
        status = {
            "A": "added",
            "C": "added",
            "D": "deleted",
            "M": "modified",
            "R": "renamed",
            "T": "type_changed",
        }.get(kind, "modified")
        changes.append((path, status))
    return changes


def _parse_numstat(raw: bytes) -> dict[str, tuple[int | None, int | None]]:
    tokens = raw.split(b"\0")
    values = {}
    index = 0
    while index < len(tokens) and tokens[index]:
        fields = tokens[index].split(b"\t", 2)
        index += 1
        if len(fields) != 3:
            raise RepoSnapshotError("git numstat output is malformed")
        added, deleted, path_value = fields
        if not path_value:
            if index + 1 >= len(tokens):
                raise RepoSnapshotError("git numstat rename output is incomplete")
            index += 1  # Skip the old path.
            path_value = tokens[index]
            index += 1
        additions = None if added == b"-" else int(added)
        deletions = None if deleted == b"-" else int(deleted)
        values[_decode_path(path_value)] = (additions, deletions)
    return values


def _capture_snapshot(repo_dir: str, env: dict[str, str] | None) -> RepoSnapshot:
    base_sha = _run_git(repo_dir, ["rev-parse", "HEAD"]).stdout.decode("ascii").strip()
    diff_bytes = _run_git(
        repo_dir,
        ["diff", "--cached", "--binary", "--full-index", "--no-ext-diff"],
        env=env,
    ).stdout
    status_entries = _parse_name_status(
        _run_git(repo_dir, ["diff", "--cached", "--name-status", "-z"], env=env).stdout
    )
    numstat = _parse_numstat(
        _run_git(repo_dir, ["diff", "--cached", "--numstat", "-z"], env=env).stdout
    )
    changed_files = tuple(
        ChangedFile(
            path=path,
            status=status,
            additions=numstat.get(path, (None, None))[0],
            deletions=numstat.get(path, (None, None))[1],
        )
        for path, status in status_entries
    )
    diff_stat = _run_git(repo_dir, ["diff", "--cached", "--stat"], env=env).stdout.decode(
        "utf-8", errors="replace"
    ).strip()
    return RepoSnapshot(
        base_sha=base_sha,
        diff_bytes=diff_bytes,
        diff_digest=digest_diff(diff_bytes),
        total_lines=len(diff_bytes.splitlines()),
        changed_files=changed_files,
        diff_stat=diff_stat,
    )


def capture_worktree_snapshot(repo_dir: str) -> RepoSnapshot:
    """Capture HEAD plus staged, unstaged, and untracked worktree changes."""
    with _temporary_worktree_index(repo_dir) as env:
        return _capture_snapshot(repo_dir, env)


def capture_staged_snapshot(repo_dir: str) -> RepoSnapshot:
    """Capture the real Git index after the caller has staged changes."""
    return _capture_snapshot(repo_dir, None)


def check_worktree_diff(repo_dir: str) -> tuple[bool, str]:
    """Run whitespace validation over the complete worktree snapshot."""
    with _temporary_worktree_index(repo_dir) as env:
        result = _run_git(
            repo_dir,
            ["diff", "--cached", "--check"],
            env=env,
            allowed_returncodes=(0, 2),
        )
    output = (result.stdout or result.stderr).decode("utf-8", errors="replace")[:_ERROR_LIMIT].strip()
    return result.returncode == 0, output
