"""read_repo_file 工具 - 读取克隆仓库中的文件内容。

让 Agent 能在生成代码前自主浏览仓库，了解实际代码结构、接口签名、已有测试等。
"""
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from ut_agent.tools.context import get_repo_dir


@tool
def read_repo_file_tool(
    file_path: str,
    start_line: int = 1,
    max_lines: int = 200,
    work_item_id: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """读取克隆仓库中的指定行范围。

    用于在生成代码前了解实际代码结构、接口签名、已有测试等。

    参数:
        file_path: 仓库内的相对路径（如 "src/modules/acc/foo.cpp"）
        start_line: 起始行号（从 1 开始）
        max_lines: 最多读取行数，默认 200

    返回: 文件内容，或错误描述。
    """
    mr_id = state.get("mr_id", 0) if state else 0
    repo_dir = get_repo_dir(mr_id)
    if not repo_dir:
        return f"ERROR: MR !{mr_id} 仓库未克隆，请先调用 clone_source_branch_tool"

    if start_line < 1:
        return "ERROR: start_line 必须大于等于 1"
    if max_lines < 1:
        return "ERROR: max_lines 必须大于等于 1"

    import os
    repo_root = os.path.realpath(repo_dir)
    full_path = os.path.realpath(os.path.join(repo_root, file_path))
    if os.path.commonpath((repo_root, full_path)) != repo_root:
        return "ERROR: 文件路径超出当前 MR 仓库"
    if not os.path.isfile(full_path):
        return f"ERROR: 文件不存在: {file_path}"

    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = []
            end_line = start_line + max_lines - 1
            for line_number, line in enumerate(f, start=1):
                if line_number < start_line:
                    continue
                if line_number > end_line:
                    lines.append(f"...(仅显示 L{start_line}-L{end_line})")
                    break
                lines.append(f"L{line_number}: {line}")
            content = "".join(lines)
            fact = (
                f"[FACT] Work Item: {work_item_id}; 已读文件: {file_path}"
                if work_item_id
                else f"[FACT] 已读文件: {file_path}"
            )
            return f"{fact} (L{start_line}-L{end_line})\n[CONTENT]\n{content}"
    except Exception as e:
        return f"ERROR: 读取文件失败: {e}"
