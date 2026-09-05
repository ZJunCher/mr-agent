"""Prepare and validate the current MR source-branch workspace."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pr_agent.log import get_logger
from ut_agent.tools.context import workspace_path

_CREDENTIAL_URL_RE = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    status: str
    repo_dir: str
    project_id: str
    mr_iid: int
    source_branch: str
    local_sha: str = ""
    remote_sha: str = ""
    generation: str = ""
    error_code: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkspaceSnapshot":
        return cls(
            status=str(value.get("status") or ""),
            repo_dir=str(value.get("repo_dir") or ""),
            project_id=str(value.get("project_id") or ""),
            mr_iid=int(value.get("mr_iid") or 0),
            source_branch=str(value.get("source_branch") or ""),
            local_sha=str(value.get("local_sha") or ""),
            remote_sha=str(value.get("remote_sha") or ""),
            generation=str(value.get("generation") or ""),
            error_code=str(value.get("error_code") or ""),
            message=str(value.get("message") or ""),
        )


@dataclass(frozen=True)
class WorkspaceValidation:
    ok: bool
    error_code: str = ""
    message: str = ""
    local_sha: str = ""
    remote_sha: str = ""
    dirty_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceReconciliation:
    status: str
    old_sha: str = ""
    new_sha: str = ""
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class _CommandResult:
    ok: bool
    stdout: str = ""
    error: str = ""


def _redact(value: str, *secrets: str) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED_URL]")
    return _CREDENTIAL_URL_RE.sub(r"\1[REDACTED]@", redacted)


def _run(
    command: list[str],
    *,
    cwd: str | None = None,
    timeout: int = 300,
    secrets: tuple[str, ...] = (),
) -> _CommandResult:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _CommandResult(False, error=f"命令超时（{timeout}s）: {command[0]} {command[1]}")
    except OSError as error:
        return _CommandResult(False, error=f"无法执行 {command[0]}: {error}")
    if result.returncode != 0:
        detail = _redact((result.stderr or result.stdout).strip(), *secrets)
        return _CommandResult(False, result.stdout.strip(), f"{command[0]} {command[1]} 失败: {detail}")
    return _CommandResult(True, result.stdout.strip())


def _git(repo_dir: str, *args: str, timeout: int = 300) -> _CommandResult:
    return _run(["git", *args], cwd=repo_dir, timeout=timeout)


def _generation(project_id: str, mr_iid: int, source_branch: str, remote_sha: str) -> str:
    value = json.dumps(
        [project_id, mr_iid, source_branch, remote_sha],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot(
    *,
    status: str,
    repo_dir: str,
    project_id: str,
    mr_iid: int,
    source_branch: str,
    local_sha: str = "",
    remote_sha: str = "",
    error_code: str = "",
    message: str = "",
) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        status=status,
        repo_dir=repo_dir,
        project_id=project_id,
        mr_iid=mr_iid,
        source_branch=source_branch,
        local_sha=local_sha,
        remote_sha=remote_sha,
        generation=_generation(project_id, mr_iid, source_branch, remote_sha) if remote_sha else "",
        error_code=error_code,
        message=message,
    )


def _remote_branch_sha(repo_dir: str, source_branch: str) -> _CommandResult:
    result = _git(repo_dir, "ls-remote", "origin", f"refs/heads/{source_branch}", timeout=120)
    if not result.ok:
        return result
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    sha = first_line.split()[0] if first_line else ""
    if not sha:
        return _CommandResult(False, error=f"远端分支不存在: {source_branch}")
    return _CommandResult(True, sha)


def _status_paths(status_output: str) -> tuple[str, ...]:
    paths = []
    for line in status_output.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        path = parts[1].split(" -> ", 1)[-1]
        if path:
            paths.append(path)
    return tuple(paths)


def _ready_snapshot(repo_dir: str, project_id: str, mr_iid: int, source_branch: str) -> WorkspaceSnapshot:
    local = _git(repo_dir, "rev-parse", "HEAD", timeout=30)
    remote = _remote_branch_sha(repo_dir, source_branch)
    if not local.ok or not remote.ok:
        message = local.error or remote.error
        return _snapshot(
            status="error",
            repo_dir=repo_dir,
            project_id=project_id,
            mr_iid=mr_iid,
            source_branch=source_branch,
            error_code="workspace_sha_unavailable",
            message=message,
        )
    if local.stdout != remote.stdout:
        return _snapshot(
            status="blocked",
            repo_dir=repo_dir,
            project_id=project_id,
            mr_iid=mr_iid,
            source_branch=source_branch,
            local_sha=local.stdout,
            remote_sha=remote.stdout,
            error_code="workspace_head_mismatch",
            message=f"本地 HEAD {local.stdout} 与远端源分支 {remote.stdout} 不一致。",
        )
    return _snapshot(
        status="ready",
        repo_dir=repo_dir,
        project_id=project_id,
        mr_iid=mr_iid,
        source_branch=source_branch,
        local_sha=local.stdout,
        remote_sha=remote.stdout,
    )


def _reconciliation_result(
    status: str,
    *,
    old_sha: str = "",
    new_sha: str = "",
    error_code: str = "",
    message: str = "",
) -> WorkspaceReconciliation:
    result = WorkspaceReconciliation(
        status=status,
        old_sha=old_sha,
        new_sha=new_sha,
        error_code=error_code,
        message=message,
    )
    event = "WORKSPACE_RECONCILED" if status == "reconciled" else "WORKSPACE_RECONCILE_SKIPPED"
    log = get_logger().info if status in {"reconciled", "not_applicable"} else get_logger().warning
    log(
        f"{event}: status={status}, old_sha={old_sha[:12]}, new_sha={new_sha[:12]}, "
        f"error_code={error_code or 'none'}"
    )
    return result


def reconcile_workspace_after_remote_commit(
    repo_dir: str,
    source_branch: str,
    pushed_sha: str,
) -> WorkspaceReconciliation:
    """Mark a local dirty tree clean only when it exactly matches a proven remote commit."""
    if not Path(repo_dir, ".git").is_dir():
        return _reconciliation_result(
            "not_applicable",
            error_code="workspace_missing",
            message="本地 MR 工作区不存在。",
        )
    status = _git(repo_dir, "status", "--porcelain", timeout=30)
    if not status.ok:
        return _reconciliation_result("error", error_code="workspace_status_failed", message=status.error)
    local = _git(repo_dir, "rev-parse", "HEAD", timeout=30)
    if not local.ok:
        return _reconciliation_result("error", error_code="workspace_sha_unavailable", message=local.error)
    if not status.stdout:
        return _reconciliation_result(
            "not_applicable",
            old_sha=local.stdout,
            error_code="workspace_clean",
            message="本地工作区没有需要对账的未提交修改。",
        )
    pushed_sha = str(pushed_sha or "").strip()
    if not pushed_sha:
        return _reconciliation_result(
            "blocked",
            old_sha=local.stdout,
            error_code="pushed_sha_missing",
            message="GitLab 提交未返回 SHA，无法安全对账。",
        )
    remote = _remote_branch_sha(repo_dir, source_branch)
    if not remote.ok:
        return _reconciliation_result(
            "error",
            old_sha=local.stdout,
            error_code="workspace_remote_unavailable",
            message=remote.error,
        )
    if remote.stdout != pushed_sha:
        return _reconciliation_result(
            "blocked",
            old_sha=local.stdout,
            new_sha=remote.stdout,
            error_code="remote_branch_changed",
            message=f"远端源分支已推进到 {remote.stdout}，不再等于格式提交 {pushed_sha}。",
        )
    fetched = _git(repo_dir, "fetch", "origin", source_branch, "--depth", "1")
    if not fetched.ok:
        return _reconciliation_result(
            "error",
            old_sha=local.stdout,
            new_sha=pushed_sha,
            error_code="workspace_fetch_failed",
            message=fetched.error,
        )
    fetch_head = _git(repo_dir, "rev-parse", "FETCH_HEAD", timeout=30)
    if not fetch_head.ok or fetch_head.stdout != pushed_sha:
        return _reconciliation_result(
            "blocked",
            old_sha=local.stdout,
            new_sha=fetch_head.stdout,
            error_code="fetched_sha_mismatch",
            message="获取到的远端提交与 GitLab 返回的提交 SHA 不一致。",
        )
    if any(line.startswith("??") for line in status.stdout.splitlines()):
        return _reconciliation_result(
            "blocked",
            old_sha=local.stdout,
            new_sha=pushed_sha,
            error_code="workspace_untracked_files",
            message="工作区包含未跟踪文件，拒绝移动本地分支。",
        )
    difference = _git(repo_dir, "diff", "--name-only", pushed_sha, "--", timeout=30)
    if not difference.ok:
        return _reconciliation_result(
            "error",
            old_sha=local.stdout,
            new_sha=pushed_sha,
            error_code="workspace_diff_failed",
            message=difference.error,
        )
    if difference.stdout:
        return _reconciliation_result(
            "blocked",
            old_sha=local.stdout,
            new_sha=pushed_sha,
            error_code="workspace_tree_mismatch",
            message="本地工作树包含未进入格式提交的额外修改，拒绝移动本地分支。",
        )
    reset = _git(repo_dir, "reset", "--mixed", pushed_sha, timeout=30)
    if not reset.ok:
        return _reconciliation_result(
            "error",
            old_sha=local.stdout,
            new_sha=pushed_sha,
            error_code="workspace_reset_failed",
            message=reset.error,
        )
    final_head = _git(repo_dir, "rev-parse", "HEAD", timeout=30)
    final_status = _git(repo_dir, "status", "--porcelain", timeout=30)
    if not final_head.ok or final_head.stdout != pushed_sha or not final_status.ok or final_status.stdout:
        return _reconciliation_result(
            "error",
            old_sha=local.stdout,
            new_sha=final_head.stdout,
            error_code="workspace_reconcile_verification_failed",
            message="移动本地分支后工作区仍未达到干净状态。",
        )
    return _reconciliation_result("reconciled", old_sha=local.stdout, new_sha=pushed_sha)


def _prune_quarantine(quarantine_root: Path, *, max_copies: int, retention_days: int) -> None:
    """Remove only bounded, explicit quarantine children below one MR directory."""
    if not quarantine_root.is_dir():
        return
    root = quarantine_root.resolve()
    max_copies = max(1, int(max_copies))
    retention_days = max(1, int(retention_days))
    cutoff = time.time() - retention_days * 86400
    children = []
    for child in root.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                children.append((child.stat().st_mtime, child))
        except OSError as error:
            get_logger().warning(f"Failed to inspect workspace quarantine entry {child.name}: {error}")
    for index, (modified_at, child) in enumerate(sorted(children, key=lambda item: item[0], reverse=True)):
        resolved = child.resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            continue
        if index >= max_copies or modified_at < cutoff:
            try:
                shutil.rmtree(resolved)
            except OSError as error:
                get_logger().warning(f"Failed to prune workspace quarantine entry {child.name}: {error}")


def _quarantine_limits() -> tuple[int, int]:
    from pr_agent.config_loader import get_settings

    config = get_settings().get("triage", {}) or {}
    try:
        max_copies = max(1, int(config.get("workspace_quarantine_max_copies", 3)))
    except (TypeError, ValueError):
        max_copies = 3
    try:
        retention_days = max(1, int(config.get("workspace_quarantine_retention_days", 7)))
    except (TypeError, ValueError):
        retention_days = 7
    return max_copies, retention_days


def _quarantine_dirty_workspace(
    repo_dir: str,
    *,
    project_id: str,
    mr_iid: int,
    source_branch: str,
    local_sha: str,
    remote_sha: str,
    dirty_files: tuple[str, ...],
) -> _CommandResult:
    """Atomically preserve an unknown dirty repo before a fresh clone replaces it."""
    repo = Path(repo_dir).resolve()
    mr_root = repo.parent.resolve()
    quarantine_root = (mr_root / "quarantine").resolve()
    if not quarantine_root.is_relative_to(mr_root) or quarantine_root == mr_root:
        return _CommandResult(False, error="隔离目录超出当前 MR 工作区。")
    quarantine_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    identifier = f"{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}-{(local_sha or 'unknown')[:12]}"
    entry = (quarantine_root / identifier).resolve()
    if not entry.is_relative_to(quarantine_root) or entry == quarantine_root:
        return _CommandResult(False, error="隔离条目标识无效。")
    try:
        entry.mkdir()
        manifest = {
            "project_id": project_id,
            "mr_iid": mr_iid,
            "source_branch": source_branch,
            "local_sha": local_sha,
            "remote_sha": remote_sha,
            "dirty_files": list(dirty_files),
            "quarantined_at": timestamp.isoformat(),
            "reason": "workspace_dirty",
        }
        (entry / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(repo, entry / "repo")
    except OSError as error:
        if entry.exists() and not (entry / "repo").exists():
            shutil.rmtree(entry)
        return _CommandResult(False, error=f"隔离脏工作区失败: {error}")
    max_copies, retention_days = _quarantine_limits()
    _prune_quarantine(quarantine_root, max_copies=max_copies, retention_days=retention_days)
    get_logger().warning(
        f"WORKSPACE_QUARANTINED: project={project_id}, mr=!{mr_iid}, branch={source_branch}, "
        f"entry={identifier}, dirty_files={len(dirty_files)}"
    )
    return _CommandResult(True, identifier)


def prepare_workspace(git_provider, workspace_root: str, mr_iid: int, source_branch: str) -> WorkspaceSnapshot:
    """Prepare one clean clone at the exact current source-branch head."""
    project_id = str(getattr(git_provider, "id_project", "") or "")
    repo_dir = workspace_path(workspace_root, project_id, mr_iid, "repo")
    base = {
        "repo_dir": repo_dir,
        "project_id": project_id,
        "mr_iid": mr_iid,
        "source_branch": source_branch,
    }
    if not project_id or mr_iid <= 0 or not source_branch:
        return _snapshot(
            status="error",
            error_code="workspace_identity_missing",
            message="缺少 project_id、MR IID 或 source_branch。",
            **base,
        )

    quarantine_id = ""
    git_dir = Path(repo_dir, ".git")
    if git_dir.is_dir():
        status = _git(repo_dir, "status", "--porcelain", timeout=30)
        if not status.ok:
            return _snapshot(status="error", error_code="workspace_status_failed", message=status.error, **base)
        if status.stdout:
            local = _git(repo_dir, "rev-parse", "HEAD", timeout=30)
            remote = _remote_branch_sha(repo_dir, source_branch)
            dirty_files = _status_paths(status.stdout)
            quarantined = _quarantine_dirty_workspace(
                repo_dir,
                project_id=project_id,
                mr_iid=mr_iid,
                source_branch=source_branch,
                local_sha=local.stdout,
                remote_sha=remote.stdout,
                dirty_files=dirty_files,
            )
            if not quarantined.ok:
                return _snapshot(
                    status="blocked",
                    error_code="workspace_dirty",
                    message=(
                        "已有 MR 工作区包含未提交变更，且无法安全隔离；"
                        f"拒绝刷新以避免丢失修复。{quarantined.error}"
                    ),
                    **base,
                )
            quarantine_id = quarantined.stdout
        else:
            for args in (
                ("fetch", "origin", source_branch, "--depth", "1"),
                ("checkout", "-B", source_branch, "FETCH_HEAD"),
                ("submodule", "update", "--init", "--recursive", "--depth", "1"),
            ):
                result = _git(repo_dir, *args)
                if not result.ok:
                    return _snapshot(
                        status="error",
                        error_code="workspace_refresh_failed",
                        message=result.error,
                        **base,
                    )
            return _ready_snapshot(**base)

    target = Path(repo_dir)
    if target.exists() and any(target.iterdir()):
        return _snapshot(
            status="blocked",
            error_code="workspace_invalid",
            message="MR 工作区目录存在但不是有效 Git 仓库，拒绝覆盖。",
            **base,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    repo_url = str(git_provider.get_git_repo_url(git_provider.pr_url) or "")
    clone_url = str(git_provider._prepare_clone_url_with_token(repo_url) or "")
    if not repo_url or not clone_url:
        return _snapshot(
            status="error",
            error_code="clone_url_unavailable",
            message="无法获取 MR 仓库克隆地址。",
            **base,
        )

    if target.exists():
        try:
            target.rmdir()
        except OSError:
            return _snapshot(
                status="blocked",
                error_code="workspace_invalid",
                message="MR 工作区目录无法安全替换。",
                **base,
            )
    with tempfile.TemporaryDirectory(prefix="pr-agent-clone-", dir=target.parent) as temp_root:
        temporary_repo = str(Path(temp_root, "repo"))
        result = _run(
            [
                "git",
                "clone",
                "--branch",
                source_branch,
                "--depth",
                "1",
                "--single-branch",
                "--recurse-submodules",
                "--shallow-submodules",
                clone_url,
                temporary_repo,
            ],
            timeout=300,
            secrets=(clone_url,),
        )
        if not result.ok:
            error_code = "workspace_reclone_failed" if quarantine_id else "workspace_clone_failed"
            message = result.error
            if quarantine_id:
                message = f"旧工作区已安全隔离为 {quarantine_id}，但重新克隆失败：{result.error}"
            return _snapshot(status="error", error_code=error_code, message=message, **base)
        os.replace(temporary_repo, repo_dir)
    if quarantine_id:
        get_logger().info(
            f"WORKSPACE_QUARANTINED: project={project_id}, mr=!{mr_iid}, entry={quarantine_id}, rebuild=ready"
        )
    return _ready_snapshot(**base)


def validate_workspace(snapshot: WorkspaceSnapshot, *, allow_dirty: bool) -> WorkspaceValidation:
    """Ensure the local base still equals the live remote source branch."""
    if snapshot.status != "ready":
        return WorkspaceValidation(False, snapshot.error_code or "workspace_not_ready", snapshot.message)
    repo_dir = os.path.realpath(snapshot.repo_dir)
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return WorkspaceValidation(False, "workspace_missing", "MR 工作区不存在或不是 Git 仓库。")
    branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD", timeout=30)
    if not branch.ok or branch.stdout != snapshot.source_branch:
        return WorkspaceValidation(
            False,
            "workspace_branch_mismatch",
            f"工作区分支为 {branch.stdout or 'unknown'}，预期 {snapshot.source_branch}。",
        )
    local = _git(repo_dir, "rev-parse", "HEAD", timeout=30)
    remote = _remote_branch_sha(repo_dir, snapshot.source_branch)
    if not local.ok or not remote.ok:
        return WorkspaceValidation(
            False,
            "workspace_sha_unavailable",
            local.error or remote.error,
            local_sha=local.stdout,
            remote_sha=remote.stdout,
        )
    status = _git(repo_dir, "status", "--porcelain", timeout=30)
    if not status.ok:
        return WorkspaceValidation(False, "workspace_status_failed", status.error)
    dirty_files = _status_paths(status.stdout)
    if local.stdout != remote.stdout:
        return WorkspaceValidation(
            False,
            "remote_branch_changed",
            f"远端源分支已从本地基线 {local.stdout} 变化为 {remote.stdout}，拒绝继续写入。",
            local_sha=local.stdout,
            remote_sha=remote.stdout,
            dirty_files=dirty_files,
        )
    if dirty_files and not allow_dirty:
        return WorkspaceValidation(
            False,
            "workspace_dirty",
            "工作区包含未提交变更。",
            local_sha=local.stdout,
            remote_sha=remote.stdout,
            dirty_files=dirty_files,
        )
    return WorkspaceValidation(
        True,
        local_sha=local.stdout,
        remote_sha=remote.stdout,
        dirty_files=dirty_files,
    )


def validate_state_workspace(
    state: dict[str, Any] | None,
    repo_dir: str,
    *,
    allow_dirty: bool,
) -> WorkspaceValidation:
    """Validate a serialized snapshot while preserving legacy non-triage callers."""
    state = state or {}
    raw_snapshot = state.get("workspace_snapshot")
    if not isinstance(raw_snapshot, dict):
        if state.get("require_workspace_snapshot"):
            return WorkspaceValidation(False, "workspace_snapshot_missing", "缺少修复工作区快照。")
        return WorkspaceValidation(True)
    snapshot = WorkspaceSnapshot.from_dict(raw_snapshot)
    if os.path.realpath(snapshot.repo_dir) != os.path.realpath(repo_dir):
        return WorkspaceValidation(False, "workspace_path_mismatch", "工具工作区与已准备快照不一致。")
    return validate_workspace(snapshot, allow_dirty=allow_dirty)
