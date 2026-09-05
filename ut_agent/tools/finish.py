"""finish 工具 - 结束 Agent 循环。

Agent 节点检测到 finish 工具调用后终止 ReAct 循环。
"""
from langchain_core.tools import tool


@tool
def finish_tool(summary: str, success: bool) -> str:
    """结束 Agent 循环，提交最终报告。

    参数:
        summary: 对本次工作的总结（将作为 MR 评论发布）
        success: 是否成功达成目标

    返回: 固定字符串 "FINISHED"，Agent 节点检测到后终止循环。
    """
    return f"FINISHED: success={success}, summary={summary}"
