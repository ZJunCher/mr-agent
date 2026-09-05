"""
parse_diff 工具 - 解析 patch 内容，将变更拆分为独立文件存放到中间目录。

功能：
1. 解析 diff_files 中每个文件的 patch（unified diff 格式）
2. 将每个文件的变更内容解析为新增行(+)和删除行(-)
3. 按原始 filename 生成对应的中间文件，保存到指定目录
"""
import os
import re
from dataclasses import dataclass, field
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from ut_agent.tools.context import get_output_dir


@dataclass
class HunkInfo:
    """一个 hunk 的解析结果"""
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header_context: str  # @@ 行尾部的函数上下文
    added_lines: list[tuple[int, str]] = field(default_factory=list)    # (行号, 内容)
    deleted_lines: list[tuple[int, str]] = field(default_factory=list)  # (行号, 内容)
    context_lines: list[tuple[int, str]] = field(default_factory=list)  # (行号, 内容)


@dataclass
class ParsedFile:
    """单个文件的解析结果"""
    filename: str
    language: str
    edit_type: str
    hunks: list[HunkInfo] = field(default_factory=list)

    @property
    def all_added(self) -> list[tuple[int, str]]:
        result = []
        for h in self.hunks:
            result.extend(h.added_lines)
        return result

    @property
    def all_deleted(self) -> list[tuple[int, str]]:
        result = []
        for h in self.hunks:
            result.extend(h.deleted_lines)
        return result


def parse_patch(patch: str) -> list[HunkInfo]:
    """解析 unified diff patch 文本，返回 hunk 列表"""
    hunks = []
    current_hunk = None
    new_line_no = 0
    old_line_no = 0

    for line in patch.split("\n"):
        # 匹配 hunk header
        hunk_match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\s*(.*)", line)
        if hunk_match:
            current_hunk = HunkInfo(
                old_start=int(hunk_match.group(1)),
                old_count=int(hunk_match.group(2) or 1),
                new_start=int(hunk_match.group(3)),
                new_count=int(hunk_match.group(4) or 1),
                header_context=hunk_match.group(5).strip(),
            )
            hunks.append(current_hunk)
            old_line_no = current_hunk.old_start
            new_line_no = current_hunk.new_start
            continue

        if current_hunk is None:
            continue

        if line.startswith("+"):
            content = line[1:]
            current_hunk.added_lines.append((new_line_no, content))
            new_line_no += 1
        elif line.startswith("-"):
            content = line[1:]
            current_hunk.deleted_lines.append((old_line_no, content))
            old_line_no += 1
        else:
            # 上下文行
            content = line[1:] if line.startswith(" ") else line
            current_hunk.context_lines.append((new_line_no, content))
            new_line_no += 1
            old_line_no += 1

    return hunks


def parse_diff_files(diff_files: list[dict]) -> list[ParsedFile]:
    """
    解析 diff_files 列表，返回每个文件的结构化解析结果。

    参数:
        diff_files: [{filename, patch, edit_type, language}, ...]
    """
    results = []
    for file_info in diff_files:
        filename = file_info["filename"]
        patch = file_info.get("patch", "")
        edit_type = file_info.get("edit_type", "UNKNOWN")
        language = file_info.get("language", "unknown")

        hunks = parse_patch(patch) if patch else []
        parsed_file = ParsedFile(
            filename=filename,
            language=language,
            edit_type=edit_type,
            hunks=hunks,
        )
        results.append(parsed_file)

    return results


def write_parsed_files(parsed_files: list[ParsedFile], output_dir: str, mr_id: int) -> list[str]:
    """
    将解析结果写入中间文件目录。

    每个变更文件生成一个对应的中间文件，内容格式：
    - 文件头部标注元信息
    - 按 hunk 分段，标注新增行和删除行

    参数:
        parsed_files: parse_diff_files 的返回结果
        output_dir: 中间文件根目录
        mr_id: MR 编号，用于在 output_dir 下创建独立子目录

    返回:
        生成的文件路径列表
    """
    # diff 文件放到 mr_{id}/diff/ 下
    diff_dir = os.path.join(output_dir, f"mr_{mr_id}", "diff")
    os.makedirs(diff_dir, exist_ok=True)
    written_files = []

    for pf in parsed_files:
        # 用 filename 的路径结构创建子目录
        safe_name = pf.filename.replace("/", os.sep).replace("\\", os.sep)
        file_path = os.path.join(diff_dir, safe_name)
        file_dir = os.path.dirname(file_path)
        if file_dir:
            os.makedirs(file_dir, exist_ok=True)

        lines = []
        lines.append(f"[文件信息]")
        lines.append(f"filename: {pf.filename}")
        lines.append(f"language: {pf.language}")
        lines.append(f"edit_type: {pf.edit_type}")
        lines.append(f"新增行数: {len(pf.all_added)}")
        lines.append(f"删除行数: {len(pf.all_deleted)}")
        lines.append("")

        for i, hunk in enumerate(pf.hunks, 1):
            lines.append(f"[Hunk {i}] {hunk.header_context}")
            lines.append(f"  范围: 旧文件第{hunk.old_start}行起{hunk.old_count}行, 新文件第{hunk.new_start}行起{hunk.new_count}行")
            lines.append("")

            if hunk.deleted_lines:
                lines.append("  [删除行]")
                for line_no, content in hunk.deleted_lines:
                    lines.append(f"    L{line_no}: {content}")
                lines.append("")

            if hunk.added_lines:
                lines.append("  [新增行]")
                for line_no, content in hunk.added_lines:
                    lines.append(f"    L{line_no}: {content}")
                lines.append("")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        written_files.append(file_path)

    return written_files


@tool
def parse_and_save_diff(state: Annotated[dict, InjectedState]) -> str:
    """解析 MR 的 diff 内容并落盘到 workspace。

    将所有变更文件的 patch 解析为结构化格式（新增行、删除行、hunk 信息），
    然后写入 workspace/mr_{id}/diff/ 目录。无需参数，自动使用当前 MR 上下文。

    返回: 落盘的文件路径列表（换行分隔）。
    """
    diff_files = state["diff_files"]
    output_dir = get_output_dir()
    mr_id = state["mr_id"]

    parsed = parse_diff_files(diff_files)
    written = write_parsed_files(parsed, output_dir, mr_id)
    return "\n".join(written) if written else "无变更文件需要解析。"
