"""Stable identities for exactly-once commit/push attempts."""

import hashlib
import re
from dataclasses import dataclass
from typing import Callable

from ut_agent.tools.repo_snapshot import digest_diff as _canonical_diff_digest

GitRunner = Callable[[str, list[str]], str]


@dataclass(frozen=True)
class PushAttemptIdentity:
    task_id: str
    sequence: int
    base_sha: str
    diff_digest: str
    attempt_id: str
    effect_name: str
    marker: str

    def result_fields(self) -> dict[str, str | int]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_sequence": self.sequence,
            "base_sha": self.base_sha,
            "diff_digest": self.diff_digest,
        }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def diff_digest(diff_text: str) -> str:
    return _canonical_diff_digest(diff_text)


def _identity(task_id: str, sequence: int, base_sha: str, staged_digest: str) -> PushAttemptIdentity:
    attempt_id = f"{task_id}:{sequence}:{base_sha}:{staged_digest}"
    identity_digest = _digest(attempt_id)
    return PushAttemptIdentity(
        task_id=task_id,
        sequence=sequence,
        base_sha=base_sha,
        diff_digest=staged_digest,
        attempt_id=attempt_id,
        effect_name=f"commit-push:{sequence}:{base_sha[:12]}:{staged_digest}",
        marker=f"[pr-agent-task:{task_id}:push-attempt:{sequence}:{identity_digest}]",
    )


def _previous_sequences(previous_pushes: list[dict]) -> list[int]:
    sequences = []
    legacy_shas = set()
    for push in previous_pushes:
        try:
            sequence = int(push.get("attempt_sequence") or 0)
        except (TypeError, ValueError):
            sequence = 0
        if sequence > 0:
            sequences.append(sequence)
        elif push.get("status") == "success" and push.get("changed") and push.get("commit_sha"):
            legacy_shas.add(str(push["commit_sha"]))
    if sequences:
        return sequences
    return list(range(1, len(legacy_shas) + 1))


def build_push_attempt(
    repo_dir: str,
    task_id: str,
    previous_pushes: list[dict],
    git_runner: GitRunner,
) -> PushAttemptIdentity:
    base_sha = git_runner(repo_dir, ["rev-parse", "HEAD"])
    if base_sha.startswith("ERROR:"):
        raise RuntimeError(base_sha)
    staged_diff = git_runner(
        repo_dir,
        ["diff", "--cached", "--binary", "--full-index", "--no-ext-diff"],
    )
    if staged_diff.startswith("ERROR:"):
        raise RuntimeError(staged_diff)
    sequence = max(_previous_sequences(previous_pushes), default=0) + 1
    return _identity(task_id, sequence, base_sha.strip(), diff_digest(staged_diff))


def recover_push_attempt(
    repo_dir: str,
    task_id: str,
    previous_pushes: list[dict],
    git_runner: GitRunner,
) -> PushAttemptIdentity | None:
    """Rebuild the latest marked attempt after commit cleaned the index."""
    message = git_runner(repo_dir, ["log", "-1", "--pretty=%B"])
    if not message or message.startswith("ERROR:"):
        return None
    pattern = re.compile(
        rf"\[pr-agent-task:{re.escape(task_id)}:push-attempt:(\d+):([0-9a-f]{{20}})\]"
    )
    match = pattern.search(message)
    if not match:
        return None
    sequence = int(match.group(1))
    completed_ids = {
        str(push.get("attempt_id"))
        for push in previous_pushes
        if push.get("status") == "success" and push.get("changed") and push.get("attempt_id")
    }
    head_sha = git_runner(repo_dir, ["rev-parse", "HEAD"])
    base_sha = git_runner(repo_dir, ["rev-parse", "HEAD^"])
    committed_diff = git_runner(
        repo_dir,
        ["diff", "HEAD^", "HEAD", "--binary", "--full-index", "--no-ext-diff"],
    )
    if any(value.startswith("ERROR:") for value in (head_sha, base_sha, committed_diff)):
        return None
    identity = _identity(task_id, sequence, base_sha.strip(), diff_digest(committed_diff))
    if identity.attempt_id in completed_ids or identity.marker != match.group(0):
        return None
    return identity
