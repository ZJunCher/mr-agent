"""discard_workspace 工具 - 丢弃当前 MR 工作区的全部未提交修改。

当 Hermes 产生了与修复任务无关的修改（如误生成测试文件）时，
Agent 必须用此工具显式丢弃，而不是被迫提交垃圾修改污染用户分支。
"""
import json
import subprocess
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from ut_agent.tools.context import get_repo_dir


@tool
def discard_workspace_tool(
    reason: str,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """丢弃当前 MR 工作区中所有未提交的修改（含未跟踪的新文件）。

    当 generate_code_tool 产生的修改与修复任务无关或有害时，用此工具回滚，
    避免把垃圾修改提交到用户分支。已提交的 commit 不受影响。

    参数:
        reason: 丢弃原因，用于审计记录。
    """
    mr_id = state.get("mr_id", 0) if state else 0
    repo_dir = get_repo_dir(mr_id)
    if not repo_dir:
        return json.dumps({
            "status": "error",
            "discarded_files": [],
            "message": f"MR !{mr_id} 仓库未克隆，无可丢弃的工作区。",
        }, ensure_ascii=False)

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir, capture_output=True, text=True, timeout=60,
        )
        if status.returncode != 0:
            return json.dumps({
                "status": "error",
                "discarded_files": [],
                "message": f"git status 失败: {status.stderr.strip()}",
            }, ensure_ascii=False)

        discarded = []
        for line in status.stdout.splitlines():
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            discarded.append(path)

        if not discarded:
            return json.dumps({
                "status": "success",
                "discarded_files": [],
                "message": "工作区已干净，无需丢弃。",
            }, ensure_ascii=False)

        for cmd in (["git", "checkout", "--", "."], ["git", "clean", "-fd"]):
            result = subprocess.run(
                cmd, cwd=repo_dir, capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                return json.dumps({
                    "status": "error",
                    "discarded_files": [],
                    "message": f"{' '.join(cmd)} 失败: {result.stderr.strip()}",
                }, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        return json.dumps({
            "status": "error",
            "discarded_files": [],
            "message": "丢弃工作区修改超时 (60s)。",
        }, ensure_ascii=False)

    return json.dumps({
        "status": "success",
        "discarded_files": sorted(discarded),
        "message": f"已丢弃 {len(discarded)} 个文件的未提交修改。原因: {reason}",
    }, ensure_ascii=False)
