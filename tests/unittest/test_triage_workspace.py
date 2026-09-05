import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pr_agent.tools.pr_triage as pr_triage_module
import ut_agent.workspace as workspace_module
from pr_agent.config_loader import get_settings, task_settings_context  # noqa: F401
from pr_agent.tools.pr_triage import PRTriage
from ut_agent.tools.context import workspace_path
from ut_agent.workspace import (
    WorkspaceSnapshot,
    prepare_workspace,
    reconcile_workspace_after_remote_commit,
    validate_state_workspace,
    validate_workspace,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _remote_with_branch(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.com")
    (source / "src.cpp").write_text("first\n", encoding="utf-8")
    _git(source, "add", "src.cpp")
    _git(source, "commit", "-m", "initial")
    _git(source, "branch", "-M", "feature/fix")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-u", "origin", "feature/fix")
    return remote, source


def _provider(remote: Path):
    return type(
        "Provider",
        (),
        {
            "id_project": "eabot/cook",
            "pr_url": "https://gitlab.example/eabot/cook/-/merge_requests/541",
            "get_git_repo_url": staticmethod(lambda _url: str(remote)),
            "_prepare_clone_url_with_token": staticmethod(lambda url: url),
        },
    )()


def test_prepare_workspace_refreshes_clean_clone_to_remote_head(tmp_path):
    remote, source = _remote_with_branch(tmp_path)
    root = tmp_path / "workspace"
    provider = _provider(remote)
    first = prepare_workspace(provider, str(root), 541, "feature/fix")
    assert first.status == "ready"

    (source / "src.cpp").write_text("second\n", encoding="utf-8")
    _git(source, "add", "src.cpp")
    _git(source, "commit", "-m", "advance")
    _git(source, "push", "origin", "feature/fix")
    expected_sha = _git(source, "rev-parse", "HEAD")

    refreshed = prepare_workspace(provider, str(root), 541, "feature/fix")

    assert refreshed.status == "ready"
    assert refreshed.local_sha == refreshed.remote_sha == expected_sha
    repo = Path(workspace_path(str(root), "eabot/cook", 541, "repo"))
    assert (repo / "src.cpp").read_text(encoding="utf-8") == "second\n"


def test_reconcile_workspace_marks_api_committed_tree_clean(tmp_path):
    remote, source = _remote_with_branch(tmp_path)
    snapshot = prepare_workspace(_provider(remote), str(tmp_path / "workspace"), 541, "feature/fix")

    Path(snapshot.repo_dir, "src.cpp").write_text("formatted\n", encoding="utf-8")
    (source / "src.cpp").write_text("formatted\n", encoding="utf-8")
    _git(source, "add", "src.cpp")
    _git(source, "commit", "-m", "style: format")
    _git(source, "push", "origin", "feature/fix")
    pushed_sha = _git(source, "rev-parse", "HEAD")

    result = reconcile_workspace_after_remote_commit(snapshot.repo_dir, "feature/fix", pushed_sha)

    assert result.status == "reconciled"
    assert result.old_sha == snapshot.local_sha
    assert result.new_sha == pushed_sha
    assert _git(Path(snapshot.repo_dir), "rev-parse", "HEAD") == pushed_sha
    assert _git(Path(snapshot.repo_dir), "status", "--porcelain") == ""


def test_reconcile_workspace_skips_clean_old_workspace(tmp_path):
    remote, _source = _remote_with_branch(tmp_path)
    snapshot = prepare_workspace(_provider(remote), str(tmp_path / "workspace"), 541, "feature/fix")

    result = reconcile_workspace_after_remote_commit(snapshot.repo_dir, "feature/fix", "unused-sha")

    assert result.status == "not_applicable"
    assert result.error_code == "workspace_clean"
    assert _git(Path(snapshot.repo_dir), "rev-parse", "HEAD") == snapshot.local_sha


def test_reconcile_workspace_rejects_remote_branch_advance(tmp_path):
    remote, source = _remote_with_branch(tmp_path)
    snapshot = prepare_workspace(_provider(remote), str(tmp_path / "workspace"), 541, "feature/fix")
    Path(snapshot.repo_dir, "src.cpp").write_text("formatted\n", encoding="utf-8")
    (source / "src.cpp").write_text("formatted\n", encoding="utf-8")
    _git(source, "add", "src.cpp")
    _git(source, "commit", "-m", "style: format")
    pushed_sha = _git(source, "rev-parse", "HEAD")
    (source / "other.cpp").write_text("newer\n", encoding="utf-8")
    _git(source, "add", "other.cpp")
    _git(source, "commit", "-m", "newer user commit")
    _git(source, "push", "origin", "feature/fix")

    result = reconcile_workspace_after_remote_commit(snapshot.repo_dir, "feature/fix", pushed_sha)

    assert result.status == "blocked"
    assert result.error_code == "remote_branch_changed"
    assert _git(Path(snapshot.repo_dir), "rev-parse", "HEAD") == snapshot.local_sha
    assert "src.cpp" in _git(Path(snapshot.repo_dir), "status", "--porcelain")


def test_reconcile_workspace_rejects_extra_tracked_change(tmp_path):
    remote, source = _remote_with_branch(tmp_path)
    snapshot = prepare_workspace(_provider(remote), str(tmp_path / "workspace"), 541, "feature/fix")
    Path(snapshot.repo_dir, "src.cpp").write_text("local-only\n", encoding="utf-8")
    (source / "src.cpp").write_text("formatted\n", encoding="utf-8")
    _git(source, "add", "src.cpp")
    _git(source, "commit", "-m", "style: format")
    _git(source, "push", "origin", "feature/fix")
    pushed_sha = _git(source, "rev-parse", "HEAD")

    result = reconcile_workspace_after_remote_commit(snapshot.repo_dir, "feature/fix", pushed_sha)

    assert result.status == "blocked"
    assert result.error_code == "workspace_tree_mismatch"
    assert _git(Path(snapshot.repo_dir), "rev-parse", "HEAD") == snapshot.local_sha


def test_reconcile_workspace_rejects_untracked_file(tmp_path):
    remote, source = _remote_with_branch(tmp_path)
    snapshot = prepare_workspace(_provider(remote), str(tmp_path / "workspace"), 541, "feature/fix")
    Path(snapshot.repo_dir, "src.cpp").write_text("formatted\n", encoding="utf-8")
    Path(snapshot.repo_dir, "scratch.txt").write_text("keep me\n", encoding="utf-8")
    (source / "src.cpp").write_text("formatted\n", encoding="utf-8")
    _git(source, "add", "src.cpp")
    _git(source, "commit", "-m", "style: format")
    _git(source, "push", "origin", "feature/fix")
    pushed_sha = _git(source, "rev-parse", "HEAD")

    result = reconcile_workspace_after_remote_commit(snapshot.repo_dir, "feature/fix", pushed_sha)

    assert result.status == "blocked"
    assert result.error_code == "workspace_untracked_files"
    assert Path(snapshot.repo_dir, "scratch.txt").read_text(encoding="utf-8") == "keep me\n"


def test_prepare_workspace_quarantines_dirty_clone_and_recreates_latest_head(tmp_path):
    remote, source = _remote_with_branch(tmp_path)
    root = tmp_path / "workspace"
    provider = _provider(remote)
    first = prepare_workspace(provider, str(root), 541, "feature/fix")
    Path(first.repo_dir, "src.cpp").write_text("uncommitted repair\n", encoding="utf-8")

    (source / "src.cpp").write_text("remote head\n", encoding="utf-8")
    _git(source, "add", "src.cpp")
    _git(source, "commit", "-m", "remote advance")
    _git(source, "push", "origin", "feature/fix")
    expected_sha = _git(source, "rev-parse", "HEAD")

    refreshed = prepare_workspace(provider, str(root), 541, "feature/fix")

    assert refreshed.status == "ready"
    assert refreshed.local_sha == refreshed.remote_sha == expected_sha
    quarantine_root = Path(first.repo_dir).parent / "quarantine"
    quarantined = list(quarantine_root.glob("*/repo"))
    assert len(quarantined) == 1
    assert Path(quarantined[0], "src.cpp").read_text(encoding="utf-8") == "uncommitted repair\n"
    manifest = json.loads((quarantined[0].parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["project_id"] == "eabot/cook"
    assert manifest["mr_iid"] == 541
    assert manifest["source_branch"] == "feature/fix"
    assert manifest["dirty_files"] == ["src.cpp"]
    assert "uncommitted repair" not in json.dumps(manifest)


def test_prepare_workspace_preserves_quarantine_when_reclone_fails(monkeypatch, tmp_path):
    remote, _source = _remote_with_branch(tmp_path)
    root = tmp_path / "workspace"
    provider = _provider(remote)
    first = prepare_workspace(provider, str(root), 541, "feature/fix")
    Path(first.repo_dir, "src.cpp").write_text("uncommitted repair\n", encoding="utf-8")
    original_run = workspace_module._run

    def fail_clone(command, **kwargs):
        if command[:2] == ["git", "clone"]:
            return workspace_module._CommandResult(False, error="clone unavailable")
        return original_run(command, **kwargs)

    monkeypatch.setattr(workspace_module, "_run", fail_clone)

    failed = prepare_workspace(provider, str(root), 541, "feature/fix")

    assert failed.status == "error"
    assert failed.error_code == "workspace_reclone_failed"
    quarantined = list((Path(first.repo_dir).parent / "quarantine").glob("*/repo"))
    assert len(quarantined) == 1
    assert Path(quarantined[0], "src.cpp").read_text(encoding="utf-8") == "uncommitted repair\n"


def test_prune_quarantine_removes_only_expired_or_excess_children(tmp_path):
    quarantine_root = tmp_path / "mr" / "quarantine"
    children = []
    for index in range(4):
        child = quarantine_root / f"entry-{index}"
        child.mkdir(parents=True)
        (child / "manifest.json").write_text("{}", encoding="utf-8")
        timestamp = time.time() - index * 60
        os.utime(child, (timestamp, timestamp))
        children.append(child)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    workspace_module._prune_quarantine(quarantine_root, max_copies=2, retention_days=7)

    assert children[0].exists()
    assert children[1].exists()
    assert not children[2].exists()
    assert not children[3].exists()
    assert unrelated.exists()


def test_prune_quarantine_removes_expired_child_within_count_limit(tmp_path):
    quarantine_root = tmp_path / "mr" / "quarantine"
    current = quarantine_root / "current"
    expired = quarantine_root / "expired"
    current.mkdir(parents=True)
    expired.mkdir()
    old_timestamp = time.time() - 8 * 86400
    os.utime(expired, (old_timestamp, old_timestamp))

    workspace_module._prune_quarantine(quarantine_root, max_copies=3, retention_days=7)

    assert current.exists()
    assert not expired.exists()


def test_validate_workspace_detects_remote_advance_with_local_changes(tmp_path):
    remote, source = _remote_with_branch(tmp_path)
    root = tmp_path / "workspace"
    snapshot = prepare_workspace(_provider(remote), str(root), 541, "feature/fix")
    Path(snapshot.repo_dir, "src.cpp").write_text("local repair\n", encoding="utf-8")
    (source / "other.cpp").write_text("remote advance\n", encoding="utf-8")
    _git(source, "add", "other.cpp")
    _git(source, "commit", "-m", "remote advance")
    _git(source, "push", "origin", "feature/fix")

    validation = validate_workspace(snapshot, allow_dirty=True)

    assert validation.ok is False
    assert validation.error_code == "remote_branch_changed"


def test_workspace_snapshot_round_trip():
    snapshot = WorkspaceSnapshot(
        status="ready",
        repo_dir="/workspace/repo",
        project_id="eabot/cook",
        mr_iid=541,
        source_branch="feature/fix",
        local_sha="abc",
        remote_sha="abc",
        generation="generation",
    )

    assert WorkspaceSnapshot.from_dict(snapshot.to_dict()) == snapshot


def test_required_workspace_snapshot_cannot_be_bypassed():
    validation = validate_state_workspace(
        {"require_workspace_snapshot": True},
        "/workspace/repo",
        allow_dirty=True,
    )

    assert validation.ok is False
    assert validation.error_code == "workspace_snapshot_missing"


def test_triage_prepares_workspace_before_starting_agent(monkeypatch, tmp_path):
    order = []
    snapshot = WorkspaceSnapshot(
        status="ready",
        repo_dir=str(tmp_path / "repo"),
        project_id="eabot/cook",
        mr_iid=541,
        source_branch="feature/fix",
        local_sha="abc",
        remote_sha="abc",
        generation="generation",
    )

    class FakeAgent:
        async def run(self, triage_info):
            order.append("agent")
            assert triage_info["workspace_snapshot"] == snapshot.to_dict()
            assert triage_info["require_workspace_snapshot"] is True
            return {"response": "done", "result": {"success": False}}

    provider = type("Provider", (), {"remove_initial_comment": staticmethod(lambda: None)})()
    triage = PRTriage.__new__(PRTriage)
    triage.pr_url = "https://gitlab.example/eabot/cook/-/merge_requests/541"
    triage.git_provider = provider
    monkeypatch.setattr(triage, "_collect_triage_info", lambda: {
        "mr_id": 541,
        "source_branch": "feature/fix",
        "project_id": "eabot/cook",
        "failed_jobs": [],
    })
    monkeypatch.setattr(triage, "_persist_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pr_triage_module, "UTAgent", FakeAgent)

    def fake_prepare(*_args):
        order.append("workspace")
        return snapshot

    monkeypatch.setattr("ut_agent.workspace.prepare_workspace", fake_prepare)
    with task_settings_context() as settings:
        settings.set("CONFIG.PUBLISH_OUTPUT", False)
        asyncio.run(triage.run())

    assert order == ["workspace", "agent"]
