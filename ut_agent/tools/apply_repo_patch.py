"""apply_repo_patch 工具 - 在 MR 工作区应用 unified diff 补丁。

这是 native repair backend 的核心写工具。Agent 通过此工具应用最小补丁，
不直接执行 git commit/push/reset/checkout。

安全约束：
- 只接受 unified diff 格式补丁。
- 先解析 --- /+++ 路径，拒绝绝对路径、..、.git、工作区外路径。
- 先执行 git apply --check，通过后才执行 git apply。
- 不使用 shell 字符串拼接；subprocess.run() 必须传 argv 列表。
- 失败时不得留下半应用补丁；验证工作区与调用前 diff 一致。
"""
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from pr_agent.log import get_logger
from ut_agent.tools._vendored.path_security import has_traversal_component
from ut_agent.tools.context import get_repo_dir
from ut_agent.tools.repo_snapshot import RepoSnapshotError, capture_worktree_snapshot, check_worktree_diff

logger = get_logger()

# 补丁头路径解析正则：--- a/path 和 +++ b/path
_DIFF_PATH_RE = re.compile(r"^(?:---|\+\+\+)\s+(?:a/|b/)?(.+)$", re.MULTILINE)
# 禁止的路径组件
_FORBIDDEN_PATH_PARTS = {".git", ".gitignore"}


def extract_patch_paths(patch: str) -> list[str]:
    """从 unified diff 提取所有 --- /+++ 路径（去掉 a/ b/ 前缀）。"""
    paths = []
    for match in _DIFF_PATH_RE.finditer(patch):
        raw = match.group(1).strip()
        # 跳过 /dev/null
        if raw == "/dev/null":
            continue
        # 去掉 a/ b/ 前缀
        if raw.startswith("a/") or raw.startswith("b/"):
            raw = raw[2:]
        paths.append(raw)
    return list(dict.fromkeys(paths))  # 去重保序


def _validate_patch_paths(paths: list[str], repo_root: str) -> str | None:
    """校验补丁路径安全性。返回错误消息或 None。"""
    for path in paths:
        # 拒绝绝对路径
        if os.path.isabs(path):
            return f"补丁路径不得为绝对路径: {path}"
        # 拒绝 .. 遍历
        if has_traversal_component(path):
            return f"补丁路径不得包含 .. 遍历: {path}"
        # 拒绝 .git 路径
        parts_lower = {p.lower() for p in os.path.normpath(path).split(os.sep)}
        if parts_lower & _FORBIDDEN_PATH_PARTS:
            return f"补丁路径不得进入 .git 目录: {path}"
        # 拒绝工作区外路径（解析后必须在 repo_root 下）
        resolved = Path(os.path.realpath(os.path.join(repo_root, path)))
        try:
            resolved.relative_to(repo_root)
        except ValueError:
            return f"补丁路径超出工作区: {path}"
    return None


def _git_apply_check(repo_dir: str, patch: str) -> tuple[bool, str]:
    """执行 git apply --check，返回 (是否通过, 错误消息)。"""
    proc = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=repo_dir,
        input=patch,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True, ""
    return False, proc.stderr.strip() or "git apply --check 失败"


def _git_apply(repo_dir: str, patch: str) -> tuple[bool, str]:
    """执行 git apply，返回 (是否成功, 错误消息)。"""
    proc = subprocess.run(
        ["git", "apply", "-"],
        cwd=repo_dir,
        input=patch,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True, ""
    return False, proc.stderr.strip() or "git apply 失败"


@tool
def apply_repo_patch_tool(
    patch: str,
    reason: str,
    work_item_id: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """在 MR 工作区应用 unified diff 补丁。

    只接受 unified diff 格式。补丁路径必须在工作区内，不得进入 .git 或工作区外。
    先执行 git apply --check 验证，通过后才实际应用。失败时不留半应用补丁。

    参数:
        patch: unified diff 格式的补丁文本
        reason: 应用此补丁的原因（用于审计）

    返回: JSON 格式结果，包含 status/changed_files/diff_check 等字段。
    """
    mr_id = state.get("mr_id", 0) if state else 0
    repo_dir = get_repo_dir(mr_id)
    if not repo_dir:
        return json.dumps({
            "status": "error", "message": f"MR !{mr_id} 仓库未克隆", "work_item_id": work_item_id,
        }, ensure_ascii=False)

    if not patch or not patch.strip():
        return json.dumps({
            "status": "error", "message": "补丁内容为空", "work_item_id": work_item_id,
        }, ensure_ascii=False)

    repo_root = os.path.realpath(repo_dir)

    # 1. 提取并校验补丁路径
    paths = extract_patch_paths(patch)
    if not paths:
        return json.dumps({
            "status": "error", "message": "补丁中未找到有效路径", "work_item_id": work_item_id,
        }, ensure_ascii=False)

    path_error = _validate_patch_paths(paths, repo_root)
    if path_error:
        return json.dumps({
            "status": "blocked", "message": path_error, "work_item_id": work_item_id,
        }, ensure_ascii=False)

    # 2. 记录调用前规范化快照（用于失败后完整性验证）
    try:
        before_snapshot = capture_worktree_snapshot(repo_dir)
    except RepoSnapshotError as exc:
        return json.dumps({
            "status": "error", "message": f"无法读取补丁前工作区: {exc}", "work_item_id": work_item_id,
        }, ensure_ascii=False)

    # 3. git apply --check
    check_ok, check_err = _git_apply_check(repo_dir, patch)
    if not check_ok:
        return json.dumps({
            "status": "error",
            "message": f"补丁校验失败: {check_err}",
            "changed_files": [],
            "work_item_id": work_item_id,
        }, ensure_ascii=False)

    # 4. git apply
    apply_ok, apply_err = _git_apply(repo_dir, patch)
    if not apply_ok:
        try:
            after_snapshot = capture_worktree_snapshot(repo_dir)
        except RepoSnapshotError as exc:
            return json.dumps({
                "status": "blocked",
                "error_code": "patch_failure_snapshot_unavailable",
                "message": f"应用补丁失败且无法验证工作区状态: {apply_err}; {exc}",
                "work_item_id": work_item_id,
            }, ensure_ascii=False)
        if after_snapshot.diff_digest != before_snapshot.diff_digest:
            return json.dumps({
                "status": "blocked",
                "error_code": "patch_failure_changed_workspace",
                "message": f"应用补丁失败且工作区发生变化: {apply_err}",
                "before_diff_digest": before_snapshot.diff_digest,
                "after_diff_digest": after_snapshot.diff_digest,
                "changed_files": [item.path for item in after_snapshot.changed_files],
                "work_item_id": work_item_id,
            }, ensure_ascii=False)
        return json.dumps({
            "status": "error",
            "message": f"应用补丁失败: {apply_err}",
            "changed_files": [],
            "work_item_id": work_item_id,
        }, ensure_ascii=False)

    # 5. 获取完整工作区快照和 diff check
    try:
        after_snapshot = capture_worktree_snapshot(repo_dir)
        diff_check_ok, diff_check_msg = check_worktree_diff(repo_dir)
    except RepoSnapshotError as exc:
        return json.dumps({
            "status": "blocked",
            "error_code": "patch_snapshot_unavailable",
            "message": f"补丁已应用但无法生成安全快照: {exc}",
            "work_item_id": work_item_id,
        }, ensure_ascii=False)
    if not after_snapshot.diff_bytes or after_snapshot.diff_digest == before_snapshot.diff_digest:
        return json.dumps({
            "status": "error",
            "message": "补丁未产生新的工作区 Diff",
            "changed_files": [item.path for item in after_snapshot.changed_files],
            "work_item_id": work_item_id,
        }, ensure_ascii=False)

    return json.dumps({
        "status": "changed",
        "patch_applied": True,
        "base_sha": after_snapshot.base_sha,
        "diff_digest": after_snapshot.diff_digest,
        "changed_files": [item.path for item in after_snapshot.changed_files],
        "diff_check": {"passed": diff_check_ok, "message": diff_check_msg},
        "reason": reason,
        "work_item_id": work_item_id,
    }, ensure_ascii=False)
