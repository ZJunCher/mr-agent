"""inspect_repo_diff 工具 - 查看 MR 工作区的当前 diff。

返回完整 Diff 清单、规范化哈希和有界 unified diff 页面。
"""
import json
from dataclasses import asdict
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from pr_agent.log import get_logger
from ut_agent.config import DIFF_VIEW_MAX_LINES
from ut_agent.tools.context import get_repo_dir
from ut_agent.tools.repo_snapshot import RepoSnapshotError, capture_worktree_snapshot

logger = get_logger()


@tool
def inspect_repo_diff_tool(
    start_line: int = 1,
    max_lines: int = 600,
    work_item_id: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """查看 MR 工作区的当前 diff。

    返回完整变更清单、Diff 哈希和指定全局行范围的 unified diff 页面。

    参数:
        start_line: 当前页面从第几行开始（从 1 开始）
        max_lines: diff 最多显示行数，默认 600

    返回: JSON 格式结果，包含 Diff 清单、分页信息和当前页面正文。
    """
    mr_id = state.get("mr_id", 0) if state else 0
    repo_dir = get_repo_dir(mr_id)
    if not repo_dir:
        return json.dumps({
            "status": "error", "message": f"MR !{mr_id} 仓库未克隆", "work_item_id": work_item_id,
        }, ensure_ascii=False)

    if start_line <= 0 or max_lines <= 0:
        return json.dumps({
            "status": "blocked",
            "message": "start_line 和 max_lines 必须为正整数",
            "work_item_id": work_item_id,
        }, ensure_ascii=False)
    bounded_max_lines = min(max_lines, DIFF_VIEW_MAX_LINES)
    try:
        snapshot = capture_worktree_snapshot(repo_dir)
    except RepoSnapshotError as exc:
        return json.dumps({
            "status": "error", "message": f"无法读取工作区 Diff: {exc}", "work_item_id": work_item_id,
        }, ensure_ascii=False)

    lines = snapshot.diff_text.splitlines()
    if not lines:
        page = {
            "start_line": 0,
            "end_line": 0,
            "max_lines": bounded_max_lines,
            "has_more": False,
            "next_start_line": None,
        }
        diff_text = ""
    else:
        if start_line > snapshot.total_lines:
            return json.dumps({
                "status": "blocked",
                "message": f"start_line={start_line} 超出 Diff 总行数 {snapshot.total_lines}",
                "base_sha": snapshot.base_sha,
                "diff_digest": snapshot.diff_digest,
                "total_lines": snapshot.total_lines,
                "work_item_id": work_item_id,
            }, ensure_ascii=False)
        start_index = start_line - 1
        page_lines = lines[start_index:start_index + bounded_max_lines]
        end_line = start_index + len(page_lines)
        has_more = end_line < snapshot.total_lines
        page = {
            "start_line": start_line,
            "end_line": end_line,
            "max_lines": bounded_max_lines,
            "has_more": has_more,
            "next_start_line": end_line + 1 if has_more else None,
        }
        diff_text = "\n".join(page_lines)

    return json.dumps({
        "status": "ok",
        "base_sha": snapshot.base_sha,
        "diff_digest": snapshot.diff_digest,
        "total_lines": snapshot.total_lines,
        "changed_files": [item.path for item in snapshot.changed_files],
        "file_stats": [asdict(item) for item in snapshot.changed_files],
        "diff_stat": snapshot.diff_stat,
        "page": page,
        "diff": diff_text,
        "truncated": page["has_more"],
        "work_item_id": work_item_id,
    }, ensure_ascii=False)
