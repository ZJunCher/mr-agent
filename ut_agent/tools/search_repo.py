"""search_repo 工具 - 在 MR 工作区搜索文件内容。

优先使用 ripgrep (rg)，不可用时回退到 Python 标准库 grep。
搜索范围限制在当前 MR 仓库内，禁止搜索 .git、证据目录、运行日志。
"""
import json
import os
import re
import shutil
import subprocess
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from pr_agent.log import get_logger
from ut_agent.config import REPO_SEARCH_MAX_RESULTS
from ut_agent.tools.context import get_repo_dir

logger = get_logger()

# 禁止搜索的目录
_FORBIDDEN_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules"}


def _is_forbidden_path(rel_path: str) -> bool:
    """检查相对路径是否进入禁止目录。"""
    parts = {p.lower() for p in os.path.normpath(rel_path).split(os.sep)}
    return bool(parts & _FORBIDDEN_DIRS)


def _search_with_ripgrep(repo_dir: str, query: str, path_glob: str, max_results: int) -> list[dict]:
    """使用 ripgrep 搜索。"""
    cmd = ["rg", "--json", "-i", "--max-count", str(max_results)]
    if path_glob:
        cmd.extend(["-g", path_glob])
    cmd.append(query)
    proc = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
    if proc.returncode not in (0, 1):  # 0=有匹配, 1=无匹配
        return []
    results = []
    for line in proc.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") == "match":
                data = entry.get("data", {})
                results.append({
                    "path": data.get("path", {}).get("text", ""),
                    "line_number": data.get("line_number", 0),
                    "line": data.get("lines", {}).get("text", ""),
                })
                if len(results) >= max_results:
                    break
        except json.JSONDecodeError:
            continue
    return results


def _search_with_python(repo_dir: str, query: str, path_glob: str, max_results: int) -> list[dict]:
    """Python 标准库回退搜索。"""
    results = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    glob_pattern = None
    if path_glob:
        import fnmatch
        glob_pattern = path_glob

    for root, dirs, files in os.walk(repo_dir):
        # 跳过禁止目录
        dirs[:] = [d for d in dirs if d.lower() not in _FORBIDDEN_DIRS]
        for fname in files:
            rel_path = os.path.relpath(os.path.join(root, fname), repo_dir)
            if _is_forbidden_path(rel_path):
                continue
            if glob_pattern:
                import fnmatch
                if not fnmatch.fnmatch(rel_path, glob_pattern):
                    continue
            full_path = os.path.join(root, fname)
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, start=1):
                        if pattern.search(line):
                            results.append({
                                "path": rel_path,
                                "line_number": line_no,
                                "line": line.rstrip(),
                            })
                            if len(results) >= max_results:
                                return results
            except (OSError, PermissionError):
                continue
    return results


@tool
def search_repo_tool(
    query: str,
    path_glob: str = "",
    max_results: int = 50,
    work_item_id: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """在 MR 工作区搜索文件内容。

    搜索范围限制在当前 MR 仓库内。禁止搜索 .git、__pycache__ 等目录。
    优先使用 ripgrep，不可用时回退到 Python 标准库。

    参数:
        query: 搜索关键词（不区分大小写）
        path_glob: 文件名 glob 过滤（如 "*.py"），默认不过滤
        max_results: 最多返回结果数，默认 50

    返回: JSON 格式结果，包含 status 和 matches 列表。
    """
    mr_id = state.get("mr_id", 0) if state else 0
    repo_dir = get_repo_dir(mr_id)
    if not repo_dir:
        return json.dumps({
            "status": "error", "message": f"MR !{mr_id} 仓库未克隆", "work_item_id": work_item_id,
        }, ensure_ascii=False)

    # 校验 path_glob 不进入禁止目录
    if path_glob and _is_forbidden_path(path_glob):
        return json.dumps({
            "status": "blocked", "message": f"禁止搜索路径: {path_glob}", "work_item_id": work_item_id,
        }, ensure_ascii=False)

    # 限制 max_results
    max_results = min(max_results, REPO_SEARCH_MAX_RESULTS)

    if shutil.which("rg"):
        matches = _search_with_ripgrep(repo_dir, query, path_glob, max_results)
    else:
        matches = _search_with_python(repo_dir, query, path_glob, max_results)

    return json.dumps({
        "status": "ok",
        "matches": matches,
        "count": len(matches),
        "truncated": len(matches) >= max_results,
        "work_item_id": work_item_id,
    }, ensure_ascii=False)
