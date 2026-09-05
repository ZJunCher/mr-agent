"""
clone_branch 工具 - 将 MR 源分支浅克隆到 workspace。

通过 git clone --branch <source_branch> --depth 1 将仓库源分支下载到
workspace/mr_{id}/repo/ 目录，供后续 UT 生成使用。
"""
import json
import os
import subprocess
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from ut_agent.tools.context import get_git_provider, get_output_dir, get_repo_dir, workspace_path


def _refresh_existing_clone(repo_dir: str, source_branch: str) -> str:
    """刷新无本地变更的已有克隆到远端源分支。"""
    commands = [
        ["git", "status", "--porcelain"],
        ["git", "fetch", "origin", source_branch, "--depth", "1"],
        ["git", "checkout", "-B", source_branch, "FETCH_HEAD"],
        ["git", "submodule", "update", "--init", "--recursive", "--depth", "1"],
    ]
    try:
        for index, command in enumerate(commands):
            result = subprocess.run(
                command,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                return f"ERROR: {' '.join(command)} 失败 (exit={result.returncode}): {result.stderr.strip()}"
            if index == 0 and result.stdout.strip():
                return "ERROR: 已有仓库包含未提交变更，拒绝刷新以避免丢失修复"
    except subprocess.TimeoutExpired:
        return "ERROR: 刷新已有仓库超时 (300s)"
    except OSError as error:
        return f"ERROR: 刷新已有仓库失败: {error}"
    return repo_dir


def clone_source_branch(git_provider, output_dir: str, mr_id: int, source_branch: str) -> str:
    """
    浅克隆 MR 源分支到 workspace。

    参数:
        git_provider: pr-agent 的 git provider 实例（需要有 _prepare_clone_url_with_token）
        output_dir: workspace 根目录
        mr_id: MR 编号
        source_branch: 源分支名

    返回:
        成功: 克隆目标目录路径
        失败: 以 "ERROR:" 开头的错误信息
    """
    project_id = getattr(git_provider, "id_project", "")
    repo_dir = workspace_path(output_dir, project_id, mr_id, "repo")

    # 已有目录必须刷新，避免复用旧的 MR head。
    if os.path.isdir(os.path.join(repo_dir, ".git")):
        return _refresh_existing_clone(repo_dir, source_branch)

    os.makedirs(repo_dir, exist_ok=True)

    # 获取带 token 的 clone URL
    repo_url = git_provider.get_git_repo_url(git_provider.pr_url)
    if not repo_url:
        return "ERROR: 无法获取仓库 URL"

    clone_url = git_provider._prepare_clone_url_with_token(repo_url)
    if not clone_url:
        return f"ERROR: 无法生成带认证信息的 clone URL (repo: {repo_url})"

    # 浅克隆指定分支
    cmd = [
        "git", "clone",
        "--branch", source_branch,
        "--depth", "1",
        "--single-branch",
        "--recurse-submodules",
        "--shallow-submodules",
        clone_url,
        repo_dir,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return f"ERROR: git clone 失败 (exit={result.returncode}): {stderr}"
    except subprocess.TimeoutExpired:
        return "ERROR: git clone 超时 (300s)"
    except Exception as e:
        return f"ERROR: git clone 异常: {e}"

    # 确保子模块已初始化（兼容 .gitmodules 存在但 clone 时未拉取的场景）
    gitmodules_path = os.path.join(repo_dir, ".gitmodules")
    if os.path.isfile(gitmodules_path):
        try:
            subprocess.run(
                ["git", "submodule", "update", "--init", "--recursive", "--depth", "1"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except Exception:
            pass  # 非致命，submodule 可能已在 clone 时拉取

    return f"[FACT] 已克隆仓库到: {repo_dir}\n{repo_dir}"


@tool
def clone_mr_source_branch(state: Annotated[dict, InjectedState]) -> str:
    """将 MR 的源分支浅克隆到本地 workspace。

    执行 git clone --depth 1 将源分支代码下载到 workspace/mr_{id}/repo/ 目录。
    无需参数，自动使用当前 MR 的源分支和仓库信息。
    如果目录已存在则跳过。

    返回: 克隆目标目录路径，或错误描述。
    """
    git_provider = get_git_provider()
    output_dir = get_output_dir()
    mr_id = state["mr_id"]
    source_branch = state["source_branch"]

    return clone_source_branch(git_provider, output_dir, mr_id, source_branch)


@tool
def clone_source_branch_tool(state: Annotated[dict, InjectedState]) -> str:
    """将 MR 的源分支浅克隆到本地 workspace。

    执行 git clone --depth 1 将源分支代码下载到 workspace/mr_{id}/repo/ 目录。
    无需参数，自动使用当前 MR 的源分支和仓库信息。
    如果目录已存在则跳过。

    返回: 克隆目标目录路径，或错误描述。
    """
    git_provider = get_git_provider()
    output_dir = get_output_dir()
    mr_id = state.get("mr_id", 0)
    source_branch = state.get("source_branch", "")

    if not source_branch:
        return "ERROR: 无 source_branch"

    repo_dir = get_repo_dir(mr_id)
    if repo_dir and isinstance(state.get("workspace_snapshot"), dict):
        from ut_agent.workspace import validate_state_workspace

        validation = validate_state_workspace(state, repo_dir, allow_dirty=True)
        return json.dumps({
            "status": "ready" if validation.ok else "blocked",
            "repo_dir": repo_dir,
            "local_sha": validation.local_sha,
            "remote_sha": validation.remote_sha,
            "error_code": validation.error_code,
            "message": validation.message or "已使用入口准备的当前 MR 工作区。",
        }, ensure_ascii=False)

    return clone_source_branch(git_provider, output_dir, mr_id, source_branch)
