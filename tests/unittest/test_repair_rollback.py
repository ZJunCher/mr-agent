import subprocess
from dataclasses import replace

from pr_agent.triage.repair_rollback import (
    RepairCommitEntry,
    RepairCommitManifest,
    RepairRollbackState,
    RepairRollbackStatus,
    RollbackFailureCode,
)
from ut_agent.repair_rollback import (
    RollbackRequest,
    TargetedRevertRequest,
    execute_repair_rollback,
    execute_targeted_commit_revert,
)

BASE = "a" * 40
R1 = "b" * 40
R2 = "c" * 40
TREE_BASE = "d" * 40
TREE_R1 = "e" * 40
TREE_R2 = "f" * 40


def _entry(sequence, sha, parent, tree):
    return RepairCommitEntry(
        sequence=sequence,
        commit_sha=sha,
        parent_sha=parent,
        tree_sha=tree,
        effect_id=f"effect-{sequence}",
        task_marker=f"[pr-agent-task:{'1' * 32}:push-attempt:{sequence}:{'2' * 20}]",
        pushed_at="2026-08-13T00:00:00+00:00",
    )


def _manifest(entries=(_entry(1, R1, BASE, TREE_R1),)):
    return RepairCommitManifest(
        repair_task_id="1" * 32,
        project_id="eabot/cook",
        mr_iid=536,
        source_branch="feature/x",
        base_commit_sha=BASE,
        base_tree_sha=TREE_BASE,
        authorized_actor_id="ou_owner",
        entries=entries,
        frozen=True,
        frozen_at="2026-08-13T00:01:00+00:00",
    )


def test_manifest_requires_one_continuous_single_parent_chain():
    value = _manifest(entries=(_entry(1, R1, BASE, TREE_R1), _entry(2, R2, BASE, TREE_R2)))
    result = value.validate_static()
    assert result.ok is False
    assert result.failure_code is RollbackFailureCode.COMMIT_CHAIN_MISMATCH


def test_manifest_digest_is_stable_and_covers_actor_and_entries():
    value = _manifest()
    first = value.digest()
    assert first == RepairCommitManifest.from_json(value.to_json()).digest()
    assert first != replace(value, authorized_actor_id="ou_other").digest()


def test_manifest_rejects_unfrozen_empty_and_duplicate_commits():
    assert replace(_manifest(), frozen=False).validate_static().failure_code is RollbackFailureCode.MANIFEST_INCOMPLETE
    assert replace(_manifest(), entries=()).validate_static().failure_code is RollbackFailureCode.MANIFEST_INCOMPLETE
    duplicate = _manifest(entries=(_entry(1, R1, BASE, TREE_R1), _entry(2, R1, R1, TREE_R2)))
    assert duplicate.validate_static().failure_code is RollbackFailureCode.COMMIT_CHAIN_MISMATCH


def test_rollback_state_round_trip():
    state = RepairRollbackState(
        rollback_task_id="2" * 32,
        repair_task_id="1" * 32,
        status=RepairRollbackStatus.FAILED,
        trigger="post_repair",
        requested_by="ou_owner",
        expected_remote_head=R1,
        manifest_digest=_manifest().digest(),
        failure_code=RollbackFailureCode.REVERT_CONFLICT,
        failure_message="conflict",
    )
    assert RepairRollbackState.from_json(state.to_json()) == state


def _git(path, *args):
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _rollback_repo(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "source"
    _git(tmp_path, "init", "--bare", str(remote))
    work.mkdir()
    _git(work, "init")
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "user.email", "test@example.com")
    (work / "value.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", "value.txt")
    _git(work, "commit", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    base_tree = _git(work, "rev-parse", "HEAD^{tree}")
    entries = []
    parent = base
    for sequence, content in ((1, "first\n"), (2, "second\n")):
        marker = f"[pr-agent-task:{'1' * 32}:push-attempt:{sequence}:{'2' * 20}]"
        (work / "value.txt").write_text(content, encoding="utf-8")
        _git(work, "add", "value.txt")
        _git(work, "commit", "-m", f"repair {sequence}\n\n{marker}")
        sha = _git(work, "rev-parse", "HEAD")
        entries.append(
            RepairCommitEntry(
                sequence=sequence,
                commit_sha=sha,
                parent_sha=parent,
                tree_sha=_git(work, "rev-parse", "HEAD^{tree}"),
                effect_id=f"effect-{sequence}",
                task_marker=marker,
                pushed_at=f"2026-08-13T00:0{sequence}:00+00:00",
            )
        )
        parent = sha
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "HEAD:refs/heads/feature/x")
    manifest = RepairCommitManifest(
        repair_task_id="1" * 32,
        project_id="eabot/cook",
        mr_iid=536,
        source_branch="feature/x",
        base_commit_sha=base,
        base_tree_sha=base_tree,
        authorized_actor_id="ou_owner",
        entries=tuple(entries),
        frozen=True,
        frozen_at="2026-08-13T00:03:00+00:00",
    )
    return remote, work, manifest


def _request(remote, manifest):
    return RollbackRequest(
        project_id="eabot/cook",
        mr_iid=536,
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        source_branch="feature/x",
        manifest=manifest,
        rollback_task_id="3" * 32,
        repository_url=str(remote),
    )


def test_two_repair_commits_become_one_rollback_commit(tmp_path):
    remote, _work, manifest = _rollback_repo(tmp_path)
    result = execute_repair_rollback(_request(remote, manifest), str(tmp_path / "workspace"), lambda: None)

    assert result.status is RepairRollbackStatus.SUCCEEDED
    assert _git(remote, "rev-list", "--count", f"{manifest.final_repair_sha}..refs/heads/feature/x") == "1"
    assert _git(remote, "rev-parse", "refs/heads/feature/x^") == manifest.final_repair_sha
    assert _git(remote, "rev-parse", "refs/heads/feature/x^{tree}") == manifest.base_tree_sha


def test_remote_head_advance_never_changes_remote(tmp_path):
    remote, work, manifest = _rollback_repo(tmp_path)
    (work / "user.txt").write_text("user\n", encoding="utf-8")
    _git(work, "add", "user.txt")
    _git(work, "commit", "-m", "user change")
    _git(work, "push", "origin", "HEAD:refs/heads/feature/x")
    before = _git(remote, "rev-parse", "refs/heads/feature/x")

    result = execute_repair_rollback(_request(remote, manifest), str(tmp_path / "workspace"), lambda: None)

    assert result.failure_code is RollbackFailureCode.REMOTE_HEAD_CHANGED
    assert _git(remote, "rev-parse", "refs/heads/feature/x") == before


def test_repeated_execution_recognizes_same_rollback_commit(tmp_path):
    remote, _work, manifest = _rollback_repo(tmp_path)
    request = _request(remote, manifest)
    first = execute_repair_rollback(request, str(tmp_path / "workspace"), lambda: None)
    second = execute_repair_rollback(request, str(tmp_path / "workspace"), lambda: None)

    assert second.status is RepairRollbackStatus.SUCCEEDED
    assert second.rollback_commit_sha == first.rollback_commit_sha


def test_targeted_revert_removes_only_last_repair_commit(tmp_path):
    remote, _work, manifest = _rollback_repo(tmp_path)
    target = manifest.entries[-1]
    request = TargetedRevertRequest(
        project_id=manifest.project_id,
        mr_iid=manifest.mr_iid,
        source_branch=manifest.source_branch,
        repository_url=str(remote),
        repair_task_id=manifest.repair_task_id,
        target_commit_sha=target.commit_sha,
        expected_parent_sha=target.parent_sha,
        target_task_marker=target.task_marker,
    )

    result = execute_targeted_commit_revert(request, str(tmp_path / "workspace"), lambda: None)

    assert result.status is RepairRollbackStatus.SUCCEEDED
    assert _git(remote, "rev-parse", "refs/heads/feature/x^{tree}") == manifest.entries[0].tree_sha
    assert _git(remote, "rev-parse", "refs/heads/feature/x^") == target.commit_sha


def test_targeted_revert_is_idempotent(tmp_path):
    remote, _work, manifest = _rollback_repo(tmp_path)
    target = manifest.entries[-1]
    request = TargetedRevertRequest(
        manifest.project_id,
        manifest.mr_iid,
        manifest.source_branch,
        str(remote),
        manifest.repair_task_id,
        target.commit_sha,
        target.parent_sha,
        target.task_marker,
    )

    first = execute_targeted_commit_revert(request, str(tmp_path / "workspace"), lambda: None)
    second = execute_targeted_commit_revert(request, str(tmp_path / "workspace"), lambda: None)

    assert second.status is RepairRollbackStatus.SUCCEEDED
    assert second.rollback_commit_sha == first.rollback_commit_sha


def test_targeted_revert_preserves_non_conflicting_commit_after_target(tmp_path):
    remote, work, manifest = _rollback_repo(tmp_path)
    target = manifest.entries[-1]
    (work / "user.txt").write_text("keep me\n", encoding="utf-8")
    _git(work, "add", "user.txt")
    _git(work, "commit", "-m", "user change")
    _git(work, "push", "origin", "HEAD:refs/heads/feature/x")
    request = TargetedRevertRequest(
        manifest.project_id,
        manifest.mr_iid,
        manifest.source_branch,
        str(remote),
        manifest.repair_task_id,
        target.commit_sha,
        target.parent_sha,
        target.task_marker,
    )

    result = execute_targeted_commit_revert(request, str(tmp_path / "workspace"), lambda: None)

    assert result.status is RepairRollbackStatus.SUCCEEDED
    assert _git(remote, "show", "refs/heads/feature/x:user.txt") == "keep me"
    assert _git(remote, "show", "refs/heads/feature/x:value.txt") == "first"
