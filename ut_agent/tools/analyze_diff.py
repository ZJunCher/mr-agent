"""analyze_diff 工具 - 分析 MR 的代码变更，返回结构化的可测试单元清单。

从现有 agent.py 的 analyze_diff 节点包装，保留 LLM 调用逻辑。
"""
import os
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from pr_agent.log import get_logger
from ut_agent.llm import call_llm
from ut_agent.prompt import load_prompt
from ut_agent.tools.context import get_git_provider

logger = get_logger()

BATCH_SIZE = 5


@tool
async def analyze_diff_tool(state: Annotated[dict, InjectedState]) -> str:
    """分析 MR 的代码变更，返回结构化的可测试单元清单。

    自动从当前状态获取 diff_files，按批次调用 LLM 分析。
    分析结果发布为 MR 评论并返回摘要。

    返回: 分析结果摘要（JSON 格式），或错误描述。
    """
    diff_files = state.get("diff_files", [])
    # 过滤掉已删除的文件
    diff_files = [f for f in diff_files if f.get("edit_type", "").upper() not in ("DELETED", "DELETE")]
    # 过滤掉测试文件本身
    _TEST_INDICATORS = {"test_", "_test.", "_test_", "tests/", "test/"}
    _TEST_EXTENSIONS = {".cpp", ".cc", ".cxx", ".h", ".hpp", ".py"}
    test_files_in_diff = []
    filtered = []
    for f in diff_files:
        filename = f.get("filename", "")
        ext = os.path.splitext(filename)[1].lower()
        if ext not in _TEST_EXTENSIONS:
            filtered.append(f)
            continue
        path_lower = filename.replace("\\", "/").lower()
        if any(ind.lower() in path_lower for ind in _TEST_INDICATORS):
            test_files_in_diff.append(f)
        else:
            filtered.append(f)
    diff_files = filtered

    mr_id = state.get("mr_id", 0)

    if not diff_files:
        return "无需要分析的代码变更文件（已过滤删除文件和测试文件）"

    system_prompt = load_prompt("analyze_diff_system")
    user_template = load_prompt("analyze_diff_user")

    if len(diff_files) <= BATCH_SIZE:
        result = await _analyze_batch(diff_files, state, test_files_in_diff, system_prompt, user_template)
        if state.get("trigger_type") != "feishu_post_repair_ut":
            _publish_comment(result, mr_id, len(diff_files))
        return result
    else:
        # 多批次模式
        all_results = []
        for i in range(0, len(diff_files), BATCH_SIZE):
            batch = diff_files[i:i + BATCH_SIZE]
            batch_idx = i // BATCH_SIZE + 1
            logger.info(f"[analyze_diff] 处理批次 {batch_idx} ({len(batch)} 个文件)")
            result = await _analyze_batch(batch, state, test_files_in_diff, system_prompt, user_template)
            all_results.append(result)

        combined = "\n\n---\n\n".join(
            f"**Batch {i+1}:**\n\n```json\n{r}\n```"
            for i, r in enumerate(all_results)
        )
        if state.get("trigger_type") != "feishu_post_repair_ut":
            _publish_comment(combined, mr_id, len(diff_files), len(all_results))
        return combined


async def _analyze_batch(batch, state, test_files, system_prompt, user_template):
    """对一批 diff_files 调用 LLM 分析，返回 JSON 字符串。"""
    file_list = _build_file_list(batch)
    diff_content = _build_diff_content(batch)
    test_context = _build_existing_test_context(test_files)

    user_prompt = user_template.format(
        title=state.get("title", ""),
        author=state.get("author", ""),
        mr_id=state.get("mr_id", 0),
        source_branch=state.get("source_branch", ""),
        target_branch=state.get("target_branch", ""),
        file_count=len(batch),
        file_list=file_list,
        diff_content=diff_content,
        existing_test_context=test_context,
    )

    return await call_llm(system=system_prompt, user=user_prompt)


def _build_diff_content(diff_files):
    sections = []
    for f in diff_files:
        filename = f["filename"]
        language = f.get("language", "unknown")
        patch = f.get("patch", "")
        section = f"### {filename} ({language})\n\n```diff\n{patch}\n```"
        sections.append(section)
    return "\n\n".join(sections) if sections else "无 diff 内容。"


def _build_existing_test_context(test_files):
    if not test_files:
        return ""
    sections = []
    for f in test_files:
        filename = f["filename"]
        patch = f.get("patch", "")
        section = f"- `{filename}`:\n```diff\n{patch}\n```"
        sections.append(section)
    return "\n\n".join(sections)


def _build_file_list(diff_files):
    lines = []
    for f in diff_files:
        filename = f["filename"]
        edit_type = f.get("edit_type", "UNKNOWN")
        language = f.get("language", "unknown")
        lines.append(f"- `{filename}` ({edit_type}, {language})")
    return "\n".join(lines) if lines else "无文件变更。"


def _publish_comment(content, mr_id, file_count, batch_count=None):
    git_provider = get_git_provider()
    if not git_provider:
        return
    if batch_count:
        header = (
            f"## UT Agent - Diff 分析报告\n\n"
            f"**MR:** !{mr_id} | **文件数:** {file_count} | **批次:** {batch_count}\n\n"
        )
    else:
        header = f"## UT Agent - Diff 分析报告\n\n**MR:** !{mr_id} | **文件数:** {file_count}\n\n"
    body = f"{header}```json\n{content}\n```" if not batch_count else f"{header}{content}"
    git_provider.publish_comment(body)
