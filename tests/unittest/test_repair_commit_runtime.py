from dataclasses import replace

import pytest

from pr_agent.distributed.broker import RepairManifestConflict
from pr_agent.distributed.runtime import reconcile_pushed_repair_commit, resolve_repair_manifest_base_tree
from pr_agent.triage.repair_rollback import RepairCommitEntry, RepairCommitManifest

BASE_SHA = "a" * 40
REPAIR_SHA = "b" * 40
FORMAT_SHA = "c" * 40
BASE_TREE_SHA = "d" * 40
REPAIR_TREE_SHA = "e" * 40
FORMAT_TREE_SHA = "f" * 40


def _entry(sequence: int, commit_sha: str, parent_sha: str, tree_sha: str) -> RepairCommitEntry:
    return RepairCommitEntry(
        sequence=sequence,
        commit_sha=commit_sha,
        parent_sha=parent_sha,
        tree_sha=tree_sha,
        effect_id=f"effect-{sequence}",
        task_marker=f"[pr-agent-task:task-1:push-attempt:{sequence}:marker]",
        pushed_at=f"2026-08-18T00:0{sequence}:00+00:00",
    )


FIRST_ENTRY = _entry(1, REPAIR_SHA, BASE_SHA, REPAIR_TREE_SHA)
SECOND_ENTRY = _entry(2, FORMAT_SHA, REPAIR_SHA, FORMAT_TREE_SHA)


def _manifest(entries: tuple[RepairCommitEntry, ...]) -> RepairCommitManifest:
    return RepairCommitManifest(
        repair_task_id="task-1",
        project_id="eabot/cook",
        mr_iid=549,
        source_branch="feature/fix",
        base_commit_sha=BASE_SHA,
        base_tree_sha=BASE_TREE_SHA,
        authorized_actor_id="ou_owner",
        entries=entries,
    )


def test_first_entry_uses_parent_tree_as_manifest_base():
    assert resolve_repair_manifest_base_tree(None, FIRST_ENTRY, BASE_TREE_SHA) == BASE_TREE_SHA


def test_second_entry_reuses_immutable_manifest_base():
    assert (
        resolve_repair_manifest_base_tree(_manifest((FIRST_ENTRY,)), SECOND_ENTRY, REPAIR_TREE_SHA)
        == BASE_TREE_SHA
    )


def test_exact_second_entry_retry_reuses_immutable_manifest_base():
    assert (
        resolve_repair_manifest_base_tree(
            _manifest((FIRST_ENTRY, SECOND_ENTRY)),
            SECOND_ENTRY,
            REPAIR_TREE_SHA,
        )
        == BASE_TREE_SHA
    )


def test_reconcile_pushed_commit_uses_authoritative_parent_tree():
    assert reconcile_pushed_repair_commit(
        _manifest((FIRST_ENTRY,)),
        SECOND_ENTRY,
        expected_parent_sha=REPAIR_SHA,
        authoritative_parent_tree_sha=REPAIR_TREE_SHA,
        branch_head_sha=FORMAT_SHA,
    ) == BASE_TREE_SHA


@pytest.mark.parametrize(
    ("expected_parent_sha", "parent_tree_sha", "branch_head_sha", "message"),
    [
        (BASE_SHA, REPAIR_TREE_SHA, FORMAT_SHA, "expected parent does not match"),
        (REPAIR_SHA, BASE_TREE_SHA, FORMAT_SHA, "parent tree does not match"),
        (REPAIR_SHA, REPAIR_TREE_SHA, REPAIR_SHA, "branch head does not match"),
    ],
)
def test_reconcile_pushed_commit_rejects_unsafe_remote_facts(
    expected_parent_sha,
    parent_tree_sha,
    branch_head_sha,
    message,
):
    with pytest.raises(RepairManifestConflict, match=message):
        reconcile_pushed_repair_commit(
            _manifest((FIRST_ENTRY,)),
            SECOND_ENTRY,
            expected_parent_sha=expected_parent_sha,
            authoritative_parent_tree_sha=parent_tree_sha,
            branch_head_sha=branch_head_sha,
        )


@pytest.mark.parametrize(
    ("entry", "parent_tree_sha", "message"),
    [
        (replace(SECOND_ENTRY, parent_sha=BASE_SHA), REPAIR_TREE_SHA, "parent does not match"),
        (SECOND_ENTRY, "0" * 40, "parent tree does not match"),
        (replace(SECOND_ENTRY, sequence=3), REPAIR_TREE_SHA, "sequence is not continuous"),
    ],
)
def test_broken_commit_chain_is_rejected(entry, parent_tree_sha, message):
    with pytest.raises(RepairManifestConflict, match=message):
        resolve_repair_manifest_base_tree(_manifest((FIRST_ENTRY,)), entry, parent_tree_sha)
