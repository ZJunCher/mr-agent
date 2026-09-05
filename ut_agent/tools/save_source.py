"""
save_source 工具 - 将变更文件的全量源码（head_file）落盘到 workspace。

落盘目录: workspace/mr_{id}/changed_files/
"""
import os
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from ut_agent.tools.context import get_output_dir


def get_workspace_dir(mr_id: int, base_dir: str = None) -> str:
    """
    获取指定 MR 的 workspace 根目录路径。

    参数:
        mr_id: MR 编号
        base_dir: workspace 根目录，默认为 /tmp/ut_agent/（容器友好）
    """
    if base_dir is None:
        base_dir = os.environ.get("UT_AGENT_WORKSPACE", "/tmp/ut_agent")
    return os.path.join(base_dir, f"mr_{mr_id}")


def write_source_files(diff_files: list[dict], output_dir: str, mr_id: int) -> list[str]:
    """
    将变更文件的完整内容（head_file）落盘到 workspace/mr_{id}/changed_files/ 目录。

    参数:
        diff_files: 包含 head_file 字段的 diff_files 列表
        output_dir: 中间文件根目录
        mr_id: MR 编号

    返回:
        写入的文件路径列表
    """
    src_dir = os.path.join(output_dir, f"mr_{mr_id}", "changed_files")
    os.makedirs(src_dir, exist_ok=True)
    written_files = []

    for f in diff_files:
        head_content = f.get("head_file")
        if not head_content:
            continue

        filename = f["filename"]
        safe_name = filename.replace("/", os.sep).replace("\\", os.sep)
        file_path = os.path.join(src_dir, safe_name)
        file_dir = os.path.dirname(file_path)
        if file_dir:
            os.makedirs(file_dir, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as fp:
            fp.write(head_content)

        written_files.append(file_path)

    return written_files


@tool
def save_changed_files(state: Annotated[dict, InjectedState]) -> str:
    """将变更文件的完整源码落盘到 workspace。

    将所有变更文件的全量内容（head_file）写入 workspace/mr_{id}/changed_files/ 目录。
    无需参数，自动使用当前 MR 上下文。

    返回: 落盘的文件路径列表（换行分隔）。
    """
    diff_files = state["diff_files"]
    output_dir = get_output_dir()
    mr_id = state["mr_id"]

    written = write_source_files(diff_files, output_dir, mr_id)
    return "\n".join(written) if written else "无变更文件有完整源码。"
