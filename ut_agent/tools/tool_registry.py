"""
工具注册表 - 收集所有 @tool 并创建 LangGraph ToolNode。

各 @tool 定义在各自的工具文件中。
运行时环境配置（git_provider, output_dir）通过 context.init_context() 注入。
图 state 中的数据（diff_files, mr_id, source_branch）通过 InjectedState 自动注入。
"""
from langgraph.prebuilt import ToolNode

from ut_agent.tools.analyze_diff import analyze_diff_tool
from ut_agent.tools.apply_format_report import apply_format_report_tool
from ut_agent.tools.apply_repo_patch import apply_repo_patch_tool
from ut_agent.tools.clone_branch import clone_source_branch_tool
from ut_agent.tools.commit_push import commit_and_push_tool
from ut_agent.tools.context import init_context
from ut_agent.tools.discard_workspace import discard_workspace_tool
from ut_agent.tools.fetch_coverage_report import fetch_coverage_report_tool
from ut_agent.tools.fetch_dependency import fetch_dependency_file
from ut_agent.tools.fetch_pipeline import fetch_pipeline_logs_tool, wait_pipeline_tool
from ut_agent.tools.finish import finish_tool
from ut_agent.tools.generate_code import generate_code_tool
from ut_agent.tools.inspect_repo_diff import inspect_repo_diff_tool
from ut_agent.tools.read_repo import read_repo_file_tool
from ut_agent.tools.request_repair_replan import request_repair_replan_tool
from ut_agent.tools.resolve_dependency import resolve_dependency_evidence_tool
from ut_agent.tools.run_repo_validation import run_repo_validation_tool
from ut_agent.tools.search_repo import search_repo_tool
from ut_agent.tools.tool_schema import build_tool_contracts, tool_definitions


def init_tool_context(git_provider, output_dir: str, **kwargs):
    """
    初始化工具运行时上下文。在 agent 启动时调用一次。

    参数:
        git_provider: pr-agent 的 git provider 实例
        output_dir: workspace 根目录
    """
    init_context(git_provider, output_dir)


_COMMON_TOOLS = (
    # 查询类
    fetch_pipeline_logs_tool,        # 查流水线失败日志（非阻塞）
    fetch_coverage_report_tool,      # 查覆盖率报告
    read_repo_file_tool,             # 读仓库文件
    fetch_dependency_file,           # 拉取依赖文件
    resolve_dependency_evidence_tool,  # 只读解析当前声明依赖的接口
    analyze_diff_tool,               # 分析代码变更

    # 执行类
    clone_source_branch_tool,         # 克隆源分支
    apply_format_report_tool,         # 应用 CI 生成的格式补丁
    generate_code_tool,              # 生成代码（委托 Hermes CLI）
    discard_workspace_tool,          # 丢弃工作区无关修改
    commit_and_push_tool,            # 提交推送
    wait_pipeline_tool,              # 等待流水线完成

    # 终止
    finish_tool,                     # 结束循环
)

_NATIVE_REPAIR_TOOLS = (
    search_repo_tool,
    request_repair_replan_tool,
    apply_repo_patch_tool,
    inspect_repo_diff_tool,
    run_repo_validation_tool,
)


def get_all_tools(backend: str | None = None) -> list:
    """Return the executable tool set for the configured repair backend."""
    from ut_agent import config

    value = backend if backend is not None else config.REPAIR_BACKEND
    selected = config.parse_repair_backend(value)
    tools = _COMMON_TOOLS + (_NATIVE_REPAIR_TOOLS if selected == "native" else ())
    return list(tools)


# Compatibility snapshot for callers that still import this name directly.
ALL_TOOLS = get_all_tools()
_TOOL_CONTRACTS = build_tool_contracts(ALL_TOOLS)


def create_tool_node() -> ToolNode:
    """创建 LangGraph ToolNode，包含所有已注册的工具。"""
    return ToolNode(get_all_tools())


def get_tool_definitions() -> list[dict]:
    """返回 OpenAI function calling 格式的工具定义列表。

    用于 litellm.acompletion 的 tools 参数。
    """
    return tool_definitions(_TOOL_CONTRACTS)


def get_tool_contracts():
    """Return the strict contracts also used for pre-execution validation."""
    return _TOOL_CONTRACTS


def _extract_params(tool) -> dict:
    """Compatibility wrapper returning the complete strict parameter Schema."""
    name = str(getattr(tool, "name", "") or "")
    contract = _TOOL_CONTRACTS.get(name)
    contracts = {name: contract} if contract is not None else build_tool_contracts([tool])
    return tool_definitions(contracts)[0]["function"]["parameters"]


def format_tool_descriptions() -> str:
    """格式化工具描述供 system prompt 使用。"""
    lines = []
    for t in get_all_tools():
        name = t.name if hasattr(t, "name") else getattr(t, "__name__", "unknown")
        desc = " ".join((t.description or "").split())[:300]
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)
