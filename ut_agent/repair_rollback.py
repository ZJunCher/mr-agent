"""Deterministic Git engine for reverting exactly one CI repair task."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from pr_agent.triage.repair_rollback import (
    RepairCommitManifest,
    RepairRollbackStatus,
    RollbackFailureCode,
)
from ut_agent.tools.context import workspace_path

_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class RollbackRequest:
    project_id: str
    mr_iid: int
    mr_url: str
    source_branch: str
    manifest: RepairCommitManifest
    rollback_task_id: str
    repository_url: str


@dataclass(frozen=True)
class RollbackResult:
    status: RepairRollbackStatus
    rollback_commit_sha: str = ""
    failure_code: RollbackFailureCode | None = None
    message: str = ""
    retryable: bool = False

    def to_dict(self) -> dict:
        value = asdict(self)
        value["status"] = self.status.value
        value["failure_code"] = self.failure_code.value if self.failure_code else ""
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "RollbackResult":
        failure_code = str(value.get("failure_code") or "")
        return cls(
            status=RepairRollbackStatus(value["status"]),
            rollback_commit_sha=str(value.get("rollback_commit_sha") or ""),
            failure_code=RollbackFailureCode(failure_code) if failure_code else None,
            message=str(value.get("message") or ""),
            retryable=bool(value.get("retryable", False)),
        )


@dataclass(frozen=True)
class TargetedRevertRequest:
    project_id: str
    mr_iid: int
    source_branch: str
    repository_url: str
    repair_task_id: str
    target_commit_sha: str
    expected_parent_sha: str
    target_task_marker: str


@dataclass(frozen=True)
class TargetedRevertResult:
    status: RepairRollbackStatus
    rollback_commit_sha: str = ""
    parent_sha: str = ""
    tree_sha: str = ""
    task_marker: str = ""
    failure_code: RollbackFailureCode | None = None
    message: str = ""


@dataclass(frozen=True)
class _CommandResult:
    ok: bool
    output: str = ""
    error: str = ""


def execute_targeted_commit_revert(
    request: TargetedRevertRequest,
    workspace_root: str,
    assert_fence: Callable[[], None],
) -> TargetedRevertResult:
    """Revert exactly one task-owned commit on the current remote branch head."""
    if not all((
        request.project_id,
        request.source_branch,
        request.repository_url,
        request.repair_task_id,
        request.target_task_marker,
    )) or request.mr_iid <= 0:
        return _targeted_failed(RollbackFailureCode.MANIFEST_INCOMPLETE, "补测撤回身份不完整")
    if not re.fullmatch(r"[0-9a-f]{40}", request.target_commit_sha) or not re.fullmatch(
        r"[0-9a-f]{40}", request.expected_parent_sha
    ):
        return _targeted_failed(RollbackFailureCode.MANIFEST_INCOMPLETE, "补测提交 SHA 无效")

    repo_dir = Path(workspace_path(
        workspace_root,
        request.project_id,
        request.mr_iid,
        "coverage-revert",
        request.repair_task_id,
        "repo",
    ))
    marker = f"[pr-agent-coverage-revert:{request.repair_task_id}:{request.target_commit_sha}]"
    try:
        _recreate_targeted_workspace(repo_dir, workspace_root, request.repair_task_id)
        clone = _run(None, "clone", "--no-checkout", request.repository_url, str(repo_dir))
        if not clone.ok:
            return _targeted_failed(RollbackFailureCode.INFRASTRUCTURE_ERROR, clone.error)
        fetch = _run(
            repo_dir,
            "fetch",
            "--no-tags",
            "origin",
            f"refs/heads/{request.source_branch}:refs/remotes/origin/{request.source_branch}",
        )
        if not fetch.ok:
            return _targeted_failed(RollbackFailureCode.SOURCE_BRANCH_MISSING, fetch.error)
        remote_head = _remote_head(repo_dir, request.source_branch)
        if not remote_head:
            return _targeted_failed(RollbackFailureCode.SOURCE_BRANCH_MISSING, "MR 源分支不存在")
        existing_message = _run(repo_dir, "log", "-1", "--format=%B", remote_head)
        if existing_message.ok and marker in existing_message.output:
            return TargetedRevertResult(
                RepairRollbackStatus.SUCCEEDED,
                rollback_commit_sha=remote_head,
                parent_sha=_run(repo_dir, "rev-parse", f"{remote_head}^").output,
                tree_sha=_run(repo_dir, "rev-parse", f"{remote_head}^{{tree}}").output,
                task_marker=marker,
                message="补测提交已撤回",
            )

        parents = _run(repo_dir, "rev-list", "--parents", "-n", "1", request.target_commit_sha)
        parent_values = parents.output.split() if parents.ok else []
        target_message = _run(repo_dir, "log", "-1", "--format=%B", request.target_commit_sha)
        ancestor = _run(repo_dir, "merge-base", "--is-ancestor", request.target_commit_sha, remote_head)
        if (
            len(parent_values) != 2
            or parent_values[1] != request.expected_parent_sha
            or not target_message.ok
            or request.target_task_marker not in target_message.output
            or not ancestor.ok
        ):
            return _targeted_failed(
                RollbackFailureCode.COMMIT_CHAIN_MISMATCH,
                "补测提交身份、父链或任务标记不匹配",
            )

        checkout = _run(repo_dir, "checkout", "-B", f"coverage-revert-{request.repair_task_id[:12]}", remote_head)
        if not checkout.ok:
            return _targeted_failed(RollbackFailureCode.INFRASTRUCTURE_ERROR, checkout.error)
        reverted = _run(repo_dir, "revert", "--no-commit", request.target_commit_sha)
        if not reverted.ok:
            _run(repo_dir, "revert", "--abort")
            return _targeted_failed(RollbackFailureCode.REVERT_CONFLICT, reverted.error)
        for key, value in (("user.name", "PR-Agent"), ("user.email", "pr-agent@noreply.local")):
            configured = _run(repo_dir, "config", key, value)
            if not configured.ok:
                return _targeted_failed(RollbackFailureCode.INFRASTRUCTURE_ERROR, configured.error)
        committed = _run(repo_dir, "commit", "-m", f"revert: 撤回覆盖率补测\n\n{marker}")
        if not committed.ok:
            return _targeted_failed(RollbackFailureCode.ROLLBACK_COMMIT_INVALID, committed.error)
        rollback_sha = _run(repo_dir, "rev-parse", "HEAD").output
        parent_sha = _run(repo_dir, "rev-parse", "HEAD^").output
        tree_sha = _run(repo_dir, "rev-parse", "HEAD^{tree}").output
        if parent_sha != remote_head or not all((rollback_sha, tree_sha)):
            return _targeted_failed(RollbackFailureCode.ROLLBACK_COMMIT_INVALID, "补测撤回提交身份校验失败")
        assert_fence()
        if _remote_head(repo_dir, request.source_branch) != remote_head:
            return _targeted_failed(RollbackFailureCode.REMOTE_HEAD_CHANGED, "推送前 MR 源分支已更新")
        pushed = _run(repo_dir, "push", "origin", f"HEAD:refs/heads/{request.source_branch}")
        if not pushed.ok or _remote_head(repo_dir, request.source_branch) != rollback_sha:
            return _targeted_failed(
                RollbackFailureCode.INFRASTRUCTURE_ERROR,
                pushed.error or "补测撤回提交未在远端确认",
            )
        return TargetedRevertResult(
            RepairRollbackStatus.SUCCEEDED,
            rollback_sha,
            parent_sha,
            tree_sha,
            marker,
            message="覆盖率补测提交已撤回",
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return _targeted_failed(RollbackFailureCode.INFRASTRUCTURE_ERROR, _redact(str(error)))


def execute_repair_rollback(
    request: RollbackRequest,
    workspace_root: str,
    assert_fence: Callable[[], None],
) -> RollbackResult:
    validation = request.manifest.validate_static()
    if not validation.ok:
        return _failed(validation.failure_code or RollbackFailureCode.MANIFEST_INCOMPLETE, validation.message)
    identity_error = _validate_request_identity(request)
    if identity_error:
        return _failed(RollbackFailureCode.MANIFEST_INCOMPLETE, identity_error)
    repo_dir = Path(
        workspace_path(
            workspace_root,
            request.project_id,
            request.mr_iid,
            "rollback",
            request.rollback_task_id,
            "repo",
        )
    )
    try:
        _recreate_workspace(repo_dir, workspace_root, request.rollback_task_id)
        clone = _run(None, "clone", "--no-checkout", request.repository_url, str(repo_dir))
        if not clone.ok:
            return _failed(RollbackFailureCode.INFRASTRUCTURE_ERROR, clone.error, retryable=True)
        fetch = _run(
            repo_dir,
            "fetch",
            "--no-tags",
            "origin",
            f"refs/heads/{request.source_branch}:refs/remotes/origin/{request.source_branch}",
        )
        if not fetch.ok:
            return _failed(RollbackFailureCode.SOURCE_BRANCH_MISSING, fetch.error, retryable=True)
        remote_head = _remote_head(repo_dir, request.source_branch)
        marker = f"[pr-agent-rollback:{request.manifest.repair_task_id}:{request.rollback_task_id}]"
        existing = _existing_rollback(repo_dir, remote_head, request.manifest, marker)
        if existing:
            return RollbackResult(RepairRollbackStatus.SUCCEEDED, rollback_commit_sha=remote_head, message="已撤回")
        if remote_head != request.manifest.final_repair_sha:
            return _failed(RollbackFailureCode.REMOTE_HEAD_CHANGED, "源分支已有新提交，拒绝自动撤回")
        verified = _verify_entries(repo_dir, request.manifest)
        if verified is not None:
            return verified
        checkout = _run(repo_dir, "checkout", "-B", f"rollback-{request.rollback_task_id[:12]}", remote_head)
        if not checkout.ok:
            return _failed(RollbackFailureCode.INFRASTRUCTURE_ERROR, checkout.error, retryable=True)
        if _run(repo_dir, "status", "--porcelain").output:
            return _failed(RollbackFailureCode.INFRASTRUCTURE_ERROR, "独立撤回工作区不干净")
        for entry in reversed(request.manifest.entries):
            reverted = _run(repo_dir, "revert", "--no-commit", entry.commit_sha)
            if not reverted.ok:
                _run(repo_dir, "revert", "--abort")
                return _failed(RollbackFailureCode.REVERT_CONFLICT, reverted.error)
        tree = _run(repo_dir, "write-tree")
        if not tree.ok or tree.output != request.manifest.base_tree_sha:
            return _failed(
                RollbackFailureCode.TREE_MISMATCH,
                "撤回后的文件树与修复前不一致，未创建提交",
            )
        assert_fence()
        for key, value in (("user.name", "PR-Agent"), ("user.email", "pr-agent@noreply.local")):
            configured = _run(repo_dir, "config", key, value)
            if not configured.ok:
                return _failed(RollbackFailureCode.INFRASTRUCTURE_ERROR, configured.error, retryable=True)
        committed = _run(repo_dir, "commit", "-m", f"revert: 撤回 CI 自动修复\n\n{marker}")
        if not committed.ok:
            return _failed(RollbackFailureCode.ROLLBACK_COMMIT_INVALID, committed.error)
        rollback_sha = _run(repo_dir, "rev-parse", "HEAD").output
        parent = _run(repo_dir, "rev-parse", "HEAD^").output
        rollback_tree = _run(repo_dir, "rev-parse", "HEAD^{tree}").output
        if parent != request.manifest.final_repair_sha or rollback_tree != request.manifest.base_tree_sha:
            return _failed(RollbackFailureCode.ROLLBACK_COMMIT_INVALID, "撤回提交身份校验失败")
        assert_fence()
        if _remote_head(repo_dir, request.source_branch) != request.manifest.final_repair_sha:
            return _failed(RollbackFailureCode.REMOTE_HEAD_CHANGED, "推送前源分支已产生新提交")
        pushed = _run(repo_dir, "push", "origin", f"HEAD:refs/heads/{request.source_branch}")
        confirmed_head = _remote_head(repo_dir, request.source_branch)
        if confirmed_head == rollback_sha:
            return RollbackResult(
                RepairRollbackStatus.SUCCEEDED,
                rollback_commit_sha=rollback_sha,
                message="本次自动修复已完整撤回",
            )
        if confirmed_head != request.manifest.final_repair_sha:
            return _failed(RollbackFailureCode.REMOTE_HEAD_CHANGED, "推送期间源分支被其他提交更新")
        return _failed(
            RollbackFailureCode.INFRASTRUCTURE_ERROR,
            pushed.error or "撤回提交未在远端确认",
            retryable=True,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return _failed(RollbackFailureCode.INFRASTRUCTURE_ERROR, _redact(str(error)), retryable=True)


def _validate_request_identity(request: RollbackRequest) -> str:
    manifest = request.manifest
    if manifest.project_id != request.project_id or manifest.mr_iid != request.mr_iid:
        return "撤回请求与提交清单的 MR 不一致"
    if manifest.source_branch != request.source_branch:
        return "撤回请求与提交清单的源分支不一致"
    if not request.rollback_task_id or not request.repository_url:
        return "撤回任务或仓库地址缺失"
    return ""


def _recreate_workspace(repo_dir: Path, workspace_root: str, rollback_task_id: str) -> None:
    root = Path(workspace_root).resolve()
    resolved = repo_dir.resolve()
    if not rollback_task_id or root not in resolved.parents or resolved.name != "repo":
        raise RuntimeError("invalid rollback workspace")
    rollback_dir = resolved.parent
    if rollback_dir.name != rollback_task_id or rollback_dir.parent.name != "rollback":
        raise RuntimeError("invalid rollback workspace identity")
    if rollback_dir.exists():
        shutil.rmtree(rollback_dir)
    rollback_dir.mkdir(parents=True, exist_ok=False)


def _recreate_targeted_workspace(repo_dir: Path, workspace_root: str, repair_task_id: str) -> None:
    root = Path(workspace_root).resolve()
    resolved = repo_dir.resolve()
    target_dir = resolved.parent
    if (
        root not in resolved.parents
        or resolved.name != "repo"
        or target_dir.name != repair_task_id
        or target_dir.parent.name != "coverage-revert"
    ):
        raise RuntimeError("invalid targeted revert workspace identity")
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=False)


def _verify_entries(repo_dir: Path, manifest: RepairCommitManifest) -> RollbackResult | None:
    for entry in manifest.entries:
        exists = _run(repo_dir, "cat-file", "-e", f"{entry.commit_sha}^{{commit}}")
        if not exists.ok:
            return _failed(RollbackFailureCode.COMMIT_CHAIN_MISMATCH, "修复提交在仓库中不存在")
        parents = _run(repo_dir, "rev-list", "--parents", "-n", "1", entry.commit_sha).output.split()
        if len(parents) != 2 or parents[1] != entry.parent_sha:
            return _failed(RollbackFailureCode.COMMIT_CHAIN_MISMATCH, "修复提交父链不匹配")
        tree = _run(repo_dir, "rev-parse", f"{entry.commit_sha}^{{tree}}").output
        if tree != entry.tree_sha:
            return _failed(RollbackFailureCode.COMMIT_CHAIN_MISMATCH, "修复提交 tree 不匹配")
        message = _run(repo_dir, "log", "-1", "--format=%B", entry.commit_sha).output
        if entry.task_marker not in message:
            return _failed(RollbackFailureCode.COMMIT_CHAIN_MISMATCH, "修复提交任务标记不匹配")
    return None


def _existing_rollback(
    repo_dir: Path,
    remote_head: str,
    manifest: RepairCommitManifest,
    marker: str,
) -> bool:
    if not remote_head or remote_head == manifest.final_repair_sha:
        return False
    parent = _run(repo_dir, "rev-parse", f"{remote_head}^")
    tree = _run(repo_dir, "rev-parse", f"{remote_head}^{{tree}}")
    message = _run(repo_dir, "log", "-1", "--format=%B", remote_head)
    return (
        parent.ok
        and tree.ok
        and message.ok
        and parent.output == manifest.final_repair_sha
        and tree.output == manifest.base_tree_sha
        and marker in message.output
    )


def _remote_head(repo_dir: Path, source_branch: str) -> str:
    result = _run(repo_dir, "ls-remote", "origin", f"refs/heads/{source_branch}")
    return result.output.split()[0] if result.ok and result.output else ""


def _run(repo_dir: Path | None, *args: str) -> _CommandResult:
    command = ["git", *args]
    try:
        result = subprocess.run(
            command,
            cwd=str(repo_dir) if repo_dir is not None else None,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _CommandResult(False, error=f"git {args[0]} 超时")
    except OSError as error:
        return _CommandResult(False, error=_redact(str(error)))
    if result.returncode != 0:
        return _CommandResult(False, result.stdout.strip(), _redact(result.stderr.strip()))
    return _CommandResult(True, result.stdout.strip())


def _redact(value: str) -> str:
    text = str(value or "")
    for match in re.findall(r"https?://[^\s]+", text):
        try:
            parsed = urlsplit(match)
            if parsed.username or parsed.password:
                host = parsed.hostname or ""
                if parsed.port:
                    host += f":{parsed.port}"
                safe_url = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
                text = text.replace(match, safe_url)
        except ValueError:
            text = text.replace(match, "[REDACTED_URL]")
    return re.sub(r"(?i)(token|password|authorization)=\S+", r"\1=[REDACTED]", text)[:2000]


def _failed(code: RollbackFailureCode, message: str, retryable: bool = False) -> RollbackResult:
    return RollbackResult(
        RepairRollbackStatus.FAILED,
        failure_code=code,
        message=_redact(message),
        retryable=retryable,
    )


def _targeted_failed(code: RollbackFailureCode, message: str) -> TargetedRevertResult:
    return TargetedRevertResult(
        RepairRollbackStatus.FAILED,
        failure_code=code,
        message=_redact(message),
    )
