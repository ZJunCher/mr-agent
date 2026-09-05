"""UT Agent 空转检测单测：连续纯文本响应时注入提醒、达到上限强制结束。"""
from langgraph.graph import END

from ut_agent.agent import _NO_TOOL_FORCE_END_THRESHOLD, _NO_TOOL_REMIND_THRESHOLD, _count_consecutive_no_tool
from ut_agent.llm import AGENT_LLM_PROTOCOL_ERROR_PREFIX


def test_count_zero_when_last_has_tool_calls():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "thinking", "tool_calls": [{"id": "1", "function": {"name": "x"}}]},
    ]
    assert _count_consecutive_no_tool(messages) == 0


def test_count_one_when_last_is_pure_text():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "我需要克隆仓库", "tool_calls": []},
    ]
    assert _count_consecutive_no_tool(messages) == 1


def test_count_three_consecutive_pure_text():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "分析1", "tool_calls": []},
        {"role": "assistant", "content": "分析2", "tool_calls": []},
        {"role": "assistant", "content": "分析3", "tool_calls": []},
    ]
    assert _count_consecutive_no_tool(messages) == 3


def test_count_resets_after_tool_call():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "分析1", "tool_calls": []},
        {"role": "assistant", "content": "调了工具", "tool_calls": [{"id": "1", "function": {"name": "x"}}]},
        {"role": "assistant", "content": "又空转", "tool_calls": []},
    ]
    assert _count_consecutive_no_tool(messages) == 1


def test_thresholds():
    assert _NO_TOOL_REMIND_THRESHOLD == 5
    assert _NO_TOOL_FORCE_END_THRESHOLD == 10


def test_protocol_error_ends_agent_without_spin_retry():
    from ut_agent.agent import route_after_agent

    state = {
        "messages": [{
            "role": "assistant",
            "content": f"{AGENT_LLM_PROTOCOL_ERROR_PREFIX}：中转响应缺少工具内容。",
        }],
        "iteration": 1,
        "max_iterations": 30,
    }

    assert route_after_agent(state) == END


def test_count_with_langchain_message_objects():
    """langchain Message 对象（AIMessage.type='ai'）也要能正确计数。"""
    from langchain_core.messages import AIMessage, HumanMessage

    messages = [
        HumanMessage(content="hi"),
        AIMessage(content="分析1"),  # 无 tool_calls
        AIMessage(content="分析2"),  # 无 tool_calls
        AIMessage(content="分析3"),  # 无 tool_calls
    ]
    assert _count_consecutive_no_tool(messages) == 3


def test_count_resets_with_langchain_message_objects():
    """langchain Message 对象有 tool_calls 时计数归零。"""
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.messages.tool import ToolCall

    messages = [
        HumanMessage(content="hi"),
        AIMessage(content="分析1"),
        AIMessage(content="调了工具", tool_calls=[ToolCall(id="1", name="x", args={})]),
        AIMessage(content="又空转"),
    ]
    assert _count_consecutive_no_tool(messages) == 1


def test_detect_intent_clone():
    from ut_agent.agent import _detect_intent_from_messages
    msgs = [{"role": "assistant", "content": "我需要先克隆仓库查看源码", "tool_calls": []}]
    result = _detect_intent_from_messages(msgs)
    assert "clone_source_branch_tool" in result


def test_detect_intent_read_file():
    from ut_agent.agent import _detect_intent_from_messages
    msgs = [{"role": "assistant", "content": "我需要查看 CMakeLists.txt 的依赖声明", "tool_calls": []}]
    result = _detect_intent_from_messages(msgs)
    assert "read_repo_file_tool" in result


def test_detect_intent_generate_code():
    from ut_agent.agent import _detect_intent_from_messages
    msgs = [{"role": "assistant", "content": "我需要修改这个文件修复编译错误", "tool_calls": []}]
    result = _detect_intent_from_messages(msgs)
    assert "generate_code_tool" in result
    assert 'operation="investigate"' in result
    assert "精确 job_name" in result


def test_detect_intent_finish():
    from ut_agent.agent import _detect_intent_from_messages
    msgs = [{"role": "assistant", "content": "任务已完成，无法继续修复", "tool_calls": []}]
    result = _detect_intent_from_messages(msgs)
    assert "finish_tool" in result


def test_detect_intent_generic_fallback():
    from ut_agent.agent import _detect_intent_from_messages
    msgs = [{"role": "assistant", "content": "根据分析这个问题很复杂", "tool_calls": []}]
    result = _detect_intent_from_messages(msgs)
    assert "工具" in result
