"""
工具运行时上下文 - 存放不适合放入 LangGraph state 的环境配置。

git_provider 和 output_dir 不可序列化，不放入图 state，
通过此模块级变量在 agent 启动时注入一次。
"""
import os
from contextvars import ContextVar
from hashlib import sha256

_tool_context: ContextVar[tuple[object | None, str]] = ContextVar(
    "ut_agent_tool_context",
    default=(None, ""),
)


class ToolContext:
    """兼容旧调用方的默认运行时环境配置。"""
    git_provider = None
    output_dir: str = ""


def init_context(git_provider, output_dir: str):
    """初始化当前异步请求的工具运行时上下文。"""
    return _tool_context.set((git_provider, output_dir))


def reset_context(token) -> None:
    """恢复先前的请求工具上下文。"""
    _tool_context.reset(token)


def get_git_provider():
    """返回当前请求的 Git provider。"""
    git_provider, _ = _tool_context.get()
    return git_provider if git_provider is not None else ToolContext.git_provider


def get_output_dir() -> str:
    """返回当前请求的 workspace 根目录。"""
    _, output_dir = _tool_context.get()
    return output_dir or ToolContext.output_dir


def workspace_key(project_id: str, mr_id: int) -> str:
    """Return a filesystem-safe identity for one project MR."""
    if not project_id:
        return f"mr_{mr_id}"
    project_digest = sha256(str(project_id).encode("utf-8")).hexdigest()[:12]
    return f"{project_digest}_mr_{mr_id}"


def workspace_path(output_dir: str, project_id: str, mr_id: int, *parts: str) -> str:
    """Build a project-scoped path below the UT Agent workspace."""
    return os.path.join(output_dir, workspace_key(project_id, mr_id), *parts)


def get_repo_dir(mr_id: int) -> str:
    """返回指定 MR 的克隆目录，不扫描其他 MR。"""
    output_dir = get_output_dir()
    if not output_dir or not mr_id:
        return ""
    git_provider = get_git_provider()
    project_id = getattr(git_provider, "id_project", "") if git_provider else ""
    repo_dir = workspace_path(output_dir, project_id, mr_id, "repo")
    return repo_dir if os.path.isdir(os.path.join(repo_dir, ".git")) else ""
