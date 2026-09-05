"""
基于 ReAct 范式的 UT Agent。

外层 Agent（litellm + Claude）负责推理、规划、构造 task_description。
内层 Agent（Hermes CLI）负责编码执行。
State 只有 messages + iteration + 触发上下文——状态是对话历史的涌现。
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from langchain_core.messages import convert_to_messages
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from pr_agent.triage.model_availability import MODEL_SERVICE_UNAVAILABLE_FAILURE_KIND
from ut_agent.config import TEST_MODE as _CFG_TEST_MODE
from ut_agent.execution_policy import (
    build_failed_summary,
    is_recoverable_tool_rejection,
    validate_finish,
    validate_tool_call,
)
from ut_agent.llm import AGENT_LLM_PROTOCOL_ERROR_PREFIX, MODEL_UNAVAILABLE_PREFIX, call_agent_llm
from ut_agent.model_failover import LLMCallOutcome
from ut_agent.pipeline_actions import next_mandatory_pipeline_action
from ut_agent.prompt.agent_system import build_system_prompt
from ut_agent.repair_planner import repair_planner_node
from ut_agent.repair_memory.native import native_memory_required, repair_memory_node
from ut_agent.repair_verifier import repair_verifier_node
from ut_agent.state import AgentState
from ut_agent.tools.context import workspace_key
from ut_agent.tools.tool_registry import (
    create_tool_node,
    format_tool_descriptions,
    get_tool_contracts,
    get_tool_definitions,
)
from ut_agent.tools.tool_schema import validate_tool_calls

# workspace 默认在 ut_agent 包目录下的 workspace/ 子目录
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
UT_WORKSPACE = os.environ.get("UT_AGENT_WORKSPACE", os.path.join(_PACKAGE_DIR, "workspace"))
os.makedirs(UT_WORKSPACE, exist_ok=True)

# 日志配置
LOG_DIR = os.path.join(UT_WORKSPACE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "ut_agent.log")

logger = logging.getLogger("ut_agent")
logger.setLevel(logging.DEBUG)

# 控制台 handler
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
if not logger.handlers:
    logger.addHandler(_console_handler)

# 文件 handler
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_file_handler)

TEST_MODE = os.environ.get("UT_AGENT_TEST_MODE", "0") == "1" or _CFG_TEST_MODE
_SAFETY_CONVERGENCE_TOOLS = {"wait_pipeline_tool", "discard_workspace_tool", "finish_tool"}
_COMPRESSION_FIELDS = (
    "context_summary",
    "context_summary_covered_messages",
    "context_compression_ineffective_count",
    "context_compression_cooldown_until",
    "context_compression_last_input_hash",
)


# ──────────────────────────────────────────────────────────────────────────────
# ReAct Agent 节点
# ──────────────────────────────────────────────────────────────────────────────

# 对话日志文件（记录 Agent 的完整思考过程和工具调用）
CONVERSATION_LOG = os.path.join(LOG_DIR, "conversation.log")
_RUN_LOCKS: dict[str, asyncio.Lock] = {}


def _conversation_log_path(state: dict) -> str:
    """返回当前 MR 独立的对话日志路径。"""
    mr_id = state.get("mr_id")
    if not mr_id:
        return CONVERSATION_LOG
    base, extension = os.path.splitext(CONVERSATION_LOG)
    identity = workspace_key(state.get("project_id", ""), mr_id)
    return f"{base}_{identity}{extension}"


def _run_lock(state: dict) -> asyncio.Lock:
    identity = workspace_key(state.get("project_id", ""), state.get("mr_id", 0))
    return _RUN_LOCKS.setdefault(identity, asyncio.Lock())


def is_mr_being_fixed(project_id: str, mr_id: int) -> bool:
    """该 MR 是否有正在进行的 UT Agent 修复（修复锁被持有）。

    供 webhook 在推送流水线失败卡片前查询：锁被持有说明 Agent 仍在抢救，
    此时不应向用户发"失败"卡片以免误判为需要人工干预。
    """
    lock = _RUN_LOCKS.get(workspace_key(project_id, mr_id))
    return bool(lock and lock.locked())


def _log_conversation(iteration: int, response_dict: dict, log_path: str) -> None:
    """将 Agent 每一步的思考内容和工具调用完整落盘到对话日志文件。

    日志格式：
    ====== 第 N 轮 =====
    [思考] LLM 的 content 内容
    [工具调用] tool_name(参数)
    """
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n====== 第 {iteration} 轮 ======\n")
            content = response_dict.get("content", "")
            if content:
                f.write(f"[思考]\n{content}\n")
            tool_calls = response_dict.get("tool_calls", [])
            for tc in tool_calls:
                fn_name = tc.get("function", {}).get("name", "")
                fn_args = tc.get("function", {}).get("arguments", "")
                f.write(f"[工具调用] {fn_name}({fn_args[:2000]})\n")
            if not content and not tool_calls:
                f.write("(空响应)\n")
    except Exception as e:
        logger.warning(f"[UT Agent] 写入对话日志失败: {e}")


def _forced_failed_finish(reason: str, iteration: int, state: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": f"policy_finish_{iteration}",
            "type": "function",
            "function": {
                "name": "finish_tool",
                "arguments": json.dumps({
                    "success": False,
                    "summary": build_failed_summary(state, reason),
                }, ensure_ascii=False),
            },
        }],
    }


def _compression_state(state: dict) -> dict:
    return {key: state.get(key) for key in _COMPRESSION_FIELDS}


def _compression_updates(outcome: LLMCallOutcome) -> dict:
    values = outcome.context_compression or {}
    return {key: values[key] for key in _COMPRESSION_FIELDS if key in values}


async def agent_node(state: AgentState) -> dict:
    """ReAct Agent 节点：LLM 决定下一步调用什么工具。

    这是整个 Agent 的"大脑"。LLM 看到对话历史和工具列表，
    自己决定下一步做什么——不被图结构锁死。
    """
    messages = state.get("messages", [])
    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", 30)
    mandatory_action = next_mandatory_pipeline_action(state)

    # 模型决策预算耗尽后仍必须完成精确 Pipeline 等待、工作区清理和确定性终态。
    if iteration >= max_iter and (
        mandatory_action is None or mandatory_action.name not in _SAFETY_CONVERGENCE_TOOLS
    ):
        logger.warning(f"[UT Agent] 已达到最大迭代次数 {max_iter}，强制终止")
        return {
            "messages": [{"role": "assistant", "content":
                f"已达到最大迭代次数 {max_iter}，强制终止。请检查日志了解详情。"}],
        }

    if mandatory_action is not None:
        response_dict = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"policy_{mandatory_action.name}_{iteration + 1}",
                "type": "function",
                "function": {
                    "name": mandatory_action.name,
                    "arguments": json.dumps(mandatory_action.arguments, ensure_ascii=False),
                },
            }],
        }
        logger.info(
            "[UT Agent] 确定性流水线动作: %s (%s)",
            mandatory_action.name,
            mandatory_action.reason,
        )
        _log_conversation(iteration + 1, response_dict, _conversation_log_path(state))
        return {
            "messages": convert_to_messages([response_dict]),
            "iteration": iteration + 1,
        }

    # 空转检测：连续纯文本响应时分析模型意图，把提醒加到 system prompt（不注入 user 消息，避免破坏消息格式）
    no_tool_count = _count_consecutive_no_tool(messages)
    spin_warning = ""
    if no_tool_count >= _NO_TOOL_REMIND_THRESHOLD and no_tool_count < _NO_TOOL_FORCE_END_THRESHOLD:
        # 从最近几轮纯文本里提取模型想调的工具，生成精准提醒
        spin_warning = _detect_intent_from_messages(messages)
        logger.warning(
            f"[UT Agent] 检测到连续 {no_tool_count} 次无工具调用，提醒加到 system prompt: {spin_warning[:100]}"
        )

    # 构建系统 prompt（含工具描述 + 当前上下文 + 已知事实 + 空转提醒）
    from ut_agent.llm import extract_known_facts
    known_facts = extract_known_facts(messages)
    if spin_warning:
        known_facts = (known_facts + "\n\n" if known_facts else "") + spin_warning
    tool_descs = format_tool_descriptions()
    system_prompt = build_system_prompt(state, tool_descs, known_facts)

    # 获取工具定义（OpenAI function calling 格式）
    tool_defs = get_tool_definitions()

    # 调用 LLM（带工具定义）
    logger.info(f"[UT Agent] ReAct 循环第 {iteration + 1}/{max_iter} 轮")
    llm_result = await call_agent_llm(
        system_prompt=system_prompt,
        messages=messages,
        tools=tool_defs,
        active_model=state.get("active_model"),
        return_outcome=True,
        compression_state=_compression_state(state),
    )
    outcome = llm_result if isinstance(llm_result, LLMCallOutcome) else LLMCallOutcome(
        response=llm_result,
        model=state.get("active_model"),
        attempts=(),
    )
    attempted_models = list(dict.fromkeys([
        *state.get("attempted_models", []),
        *(attempt.model for attempt in outcome.attempts),
    ]))
    failed_models = {attempt.model for attempt in outcome.attempts if attempt.failure_code != ""}
    last_failure_code = next((
        attempt.failure_code for attempt in reversed(outcome.attempts) if attempt.failure_code
    ), state.get("last_model_failure_code"))
    model_updates = {
        "active_model": outcome.model or state.get("active_model"),
        "attempted_models": attempted_models,
        "model_failover_count": int(state.get("model_failover_count", 0)) + len(failed_models),
        "last_model_failure_code": last_failure_code,
        **_compression_updates(outcome),
    }
    if outcome.terminal_error:
        logger.error("[UT Agent] 模型线路全部不可用，立即结束")
        response_dict = {
            "role": "assistant",
            "content": f"{MODEL_UNAVAILABLE_PREFIX}：{outcome.terminal_error}",
        }
        _log_conversation(iteration + 1, response_dict, _conversation_log_path(state))
        return {
            "messages": convert_to_messages([response_dict]),
            "iteration": iteration + 1,
            "model_terminal_error": outcome.terminal_error,
            "model_terminal_failure_kind": MODEL_SERVICE_UNAVAILABLE_FAILURE_KIND,
            **model_updates,
        }
    response = outcome.response

    # 将 response 转换为可序列化的 dict
    response_dict = _message_to_dict(response)

    # 空响应检测：模型返回空 content + 空 tool_calls 是 API 层面异常，
    # 不是"想而不做"的空转——重试一次，不计入空转计数
    if not response_dict.get("content") and not response_dict.get("tool_calls"):
        logger.warning("[UT Agent] 检测到空响应（content 和 tool_calls 都为空），重试一次")
        retry_result = await call_agent_llm(
            system_prompt=system_prompt,
            messages=messages,
            tools=tool_defs,
            active_model=outcome.model or state.get("active_model"),
            return_outcome=True,
            compression_state=outcome.context_compression or _compression_state(state),
        )
        retry_outcome = retry_result if isinstance(retry_result, LLMCallOutcome) else LLMCallOutcome(
            response=retry_result,
            model=outcome.model or state.get("active_model"),
            attempts=(),
        )
        model_updates.update(_compression_updates(retry_outcome))
        if retry_outcome.terminal_error:
            response_dict = {
                "role": "assistant",
                "content": f"{MODEL_UNAVAILABLE_PREFIX}：{retry_outcome.terminal_error}",
            }
            _log_conversation(iteration + 1, response_dict, _conversation_log_path(state))
            return {
                "messages": convert_to_messages([response_dict]),
                "iteration": iteration + 1,
                "model_terminal_error": retry_outcome.terminal_error,
                "model_terminal_failure_kind": MODEL_SERVICE_UNAVAILABLE_FAILURE_KIND,
                **model_updates,
            }
        response = retry_outcome.response
        response_dict = _message_to_dict(response)
        if not response_dict.get("content") and not response_dict.get("tool_calls"):
            logger.error("[UT Agent] 重试后仍为空响应，注入错误提示让模型下一轮纠正")
            response_dict = {
                "role": "assistant",
                "content": "ERROR: 上一轮返回了空响应。请调用一个工具继续。",
            }

    raw_tool_calls = response_dict.get("tool_calls", [])
    if raw_tool_calls:
        validated = validate_tool_calls(raw_tool_calls, get_tool_contracts())
        if validated.error:
            logger.warning("[UT Agent] 工具调用 Schema 校验失败: %s", validated.error)
            response_dict = {
                "role": "assistant",
                "content": f"ERROR: {validated.error}。请按工具 Schema 修正参数后重新调用，上一批工具均未执行。",
            }
        else:
            response_dict["tool_calls"] = list(validated.calls)

    for tool_call in response_dict.get("tool_calls", []):
        function = tool_call.get("function", {})
        tool_name = function.get("name", "")
        try:
            tool_args = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            tool_args = {}
        if not isinstance(tool_args, dict):
            tool_args = {}
        tool_allowed, tool_reason = validate_tool_call(state, tool_name, tool_args)
        if not tool_allowed:
            logger.warning(f"[UT Agent] {tool_reason}")
            if is_recoverable_tool_rejection(tool_reason):
                response_dict = {
                    "role": "assistant",
                    "content": f"{tool_reason} 请按提示执行下一项工具操作后继续。",
                }
            else:
                # 安全上限触发的系统强制 finish(false) 是终局裁决，不再经过 work-item 校验：
                # 否则"禁止继续修复"与"必须先修复才能结束"互相锁死。
                response_dict = _forced_failed_finish(tool_reason, iteration, state)
            break
        if tool_name != "finish_tool":
            continue
        finish_allowed, finish_reason = validate_finish(state, tool_args)
        if not finish_allowed:
            logger.warning(f"[UT Agent] {finish_reason}")
            response_dict = {
                "role": "assistant",
                "content": f"{finish_reason} 请继续补齐规定的真实证据和修复动作。",
            }
            break
    # 检查是否有工具调用
    tool_calls = response_dict.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            fn_name = tc.get("function", {}).get("name", "")
            fn_args = tc.get("function", {}).get("arguments", "")
            logger.info(f"[UT Agent] LLM 决定调用工具: {fn_name}")
            logger.debug(f"[UT Agent] 工具参数: {fn_args[:500]}")
    else:
        content = response_dict.get("content", "")
        logger.info(f"[UT Agent] LLM 未调用工具，输出: {content[:200]}")

    # 将完整思考过程落盘到对话日志文件
    _log_conversation(iteration + 1, response_dict, _conversation_log_path(state))

    return {
        "messages": convert_to_messages([response_dict]),
        "iteration": iteration + 1,
        **model_updates,
    }


def _message_to_dict(message) -> dict:
    """将 litellm/OpenAI 的 message 对象转换为可序列化的 dict。

    LangGraph 的 ToolNode 需要 OpenAI 格式的 message dict。
    """
    if isinstance(message, dict):
        return {"content": "", **message}

    # OpenAI ChatCompletionMessage 对象
    result = {"role": "assistant", "content": ""}

    # content
    content = getattr(message, "content", None)
    if content:
        result["content"] = content

    # tool_calls
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = [
            {
                "id": getattr(tc, "id", f"call_{i}"),
                "type": "function",
                "function": {
                    "name": getattr(tc.function, "name", ""),
                    "arguments": getattr(tc.function, "arguments", "{}"),
                },
            }
            for i, tc in enumerate(tool_calls)
        ]

    return result


def _message_content(message) -> str:
    if isinstance(message, dict):
        return str(message.get("content", "") or "")
    return str(getattr(message, "content", "") or "")


# 连续纯文本响应（无工具调用）的容忍上限。
# - 第 5 次连续纯文本：注入系统提醒，给模型纠正机会
# - 第 10 次连续纯文本：强制结束（注入提醒后还有 5 次机会调工具）
# 阈值不能太低：模型收到提醒后可能需要多轮才能从"思考"切换到"行动"。
_NO_TOOL_REMIND_THRESHOLD = 5
_NO_TOOL_FORCE_END_THRESHOLD = 10


def _count_consecutive_no_tool(messages: list) -> int:
    """从末尾往前数连续无 tool_calls 的 assistant 消息数。

    messages 里可能是 dict（OpenAI 格式，role="assistant"）或 langchain Message
    对象（AIMessage.type="ai"）。两种都要处理。
    """
    count = 0
    for msg in reversed(messages):
        if isinstance(msg, dict):
            is_assistant = msg.get("role") == "assistant"
            tool_calls = msg.get("tool_calls", [])
        else:
            # langchain Message 对象：AIMessage.type == "ai"
            is_assistant = getattr(msg, "type", None) == "ai"
            tool_calls = getattr(msg, "tool_calls", []) or []
        if not is_assistant:
            break
        if tool_calls:
            break
        count += 1
    return count


def _detect_intent_from_messages(messages: list) -> str:
    """从最近几轮纯文本响应里提取模型想调的工具，生成精准提醒。

    分析模型说的内容，匹配关键词，告诉它具体该调哪个工具、传什么参数。
    不替模型做决定，只把它的意图翻译成具体的工具名+参数提示。
    """
    # 收集最近几轮 assistant 纯文本内容
    recent_texts = []
    for msg in reversed(messages):
        if isinstance(msg, dict):
            is_assistant = msg.get("role") == "assistant"
            content = msg.get("content", "")
        else:
            is_assistant = getattr(msg, "type", None) == "ai"
            content = getattr(msg, "content", "")
        if not is_assistant:
            break
        if getattr(msg, "tool_calls", None) if isinstance(msg, dict) else (getattr(msg, "tool_calls", None) or []):
            break
        if content:
            recent_texts.append(str(content).lower())
    combined = " ".join(recent_texts)

    # 硬约束前缀：禁止纯文字确认回复，必须输出 tool_call
    HARD_PREFIX = (
        "⛔ 系统强制指令：你已经连续多轮没有调用工具。"
        "不要回复\"你说得对\"\"明白\"\"立即行动\"等确认文字——这些不算行动。"
        "你必须在本次回复中输出一个 tool_call（function call），不是文字描述。"
    )

    # 关键词 → 工具名 + 参数提示
    # 注意顺序：finish 要在 generate_code 之前匹配，避免"无法继续修复"误匹配修复
    # 用精确匹配避免"找不到"误匹配"不到"→"无法"
    if any(kw in combined for kw in ["任务完成", "无法继续", "无法修复", "finish", "放弃修复", "转人工", "无法解决"]):
        return (
            f"{HARD_PREFIX}\n"
            "如果你确认任务已完成或确实无法继续，调用 finish_tool（参数 success=true 或 false）。"
        )
    if any(kw in combined for kw in ["克隆", "clone", "拉代码", "下载仓库", "拉取仓库"]):
        return (
            f"{HARD_PREFIX}\n"
            "你需要克隆仓库。调用 clone_source_branch_tool，只提供非空 reason 作为传输说明。"
        )
    read_keywords = ["查看", "读取", "read", "cmakelists", "package.xml", "源码", "配置文件", "依赖声明"]
    if any(kw in combined for kw in read_keywords):
        # 尝试提取文件路径
        import re
        path_match = re.search(r'([\w\-/]+(?:cmakelists|package\.xml|\.py|\.cpp|\.h|\.txt)[\w\-/]*)', combined)
        path_hint = f'，参数 file_path="{path_match.group(1)}"' if path_match else ""
        return (
            f"{HARD_PREFIX}\n"
            f"你需要查看源码文件。调用 read_repo_file_tool{path_hint}。"
        )
    if any(kw in combined for kw in ["修改", "生成", "修复代码", "generate", "改代码", "写代码", "补丁"]):
        return (
            f"{HARD_PREFIX}\n"
            "你需要生成/修改代码。流水线修复时先调用 generate_code_tool，传入当前失败 work_item 的"
            "精确 job_name、root_cause_id、operation=\"investigate\"；取得诊断证据后再调用 operation=\"repair\"。"
        )
    if any(kw in combined for kw in ["提交", "推送", "commit", "push"]):
        return (
            f"{HARD_PREFIX}\n"
            "你需要提交代码。调用 commit_and_push_tool。"
        )
    if any(kw in combined for kw in ["流水线", "pipeline", "等待", "wait", "检查结果"]):
        return (
            f"{HARD_PREFIX}\n"
            "你需要检查流水线结果。调用 wait_pipeline_tool 或 fetch_pipeline_logs_tool。"
        )
    # 通用提醒
    return (
        f"{HARD_PREFIX}\n"
        "调用任意一个工具继续。不要输出纯文字分析。"
    )


def route_after_agent(state: AgentState) -> str:
    """路由：工具调用进入 tools；纯文本响应注入提醒或强制结束。

    连续纯文本响应（无工具调用）是空转的信号：模型反复"思考"却不行动。
    - 第 2 次连续纯文本：注入系统提醒，给模型最后一次纠正机会
    - 第 3 次连续纯文本：强制结束，避免浪费迭代
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]
    last_content = _message_content(last_message)
    if last_content.startswith(AGENT_LLM_PROTOCOL_ERROR_PREFIX):
        logger.error(f"[UT Agent] {last_content}")
        return END
    if last_content.startswith(MODEL_UNAVAILABLE_PREFIX):
        logger.error("[UT Agent] %s", last_content)
        return END

    tool_calls = (
        last_message.get("tool_calls", [])
        if isinstance(last_message, dict)
        else getattr(last_message, "tool_calls", [])
    )
    if tool_calls:
        return "tools"

    if state.get("iteration", 0) >= state.get("max_iterations", 30):
        mandatory_action = next_mandatory_pipeline_action(state)
        if mandatory_action is not None and mandatory_action.name in _SAFETY_CONVERGENCE_TOOLS:
            return "agent"
        return END

    no_tool_count = _count_consecutive_no_tool(messages)
    logger.warning(f"[UT Agent] 模型未调用工具（连续第 {no_tool_count} 次）")

    if no_tool_count >= _NO_TOOL_FORCE_END_THRESHOLD:
        logger.error("[UT Agent] 连续 3 次未调用工具，强制结束避免空转")
        return END

    # 连续 2 次纯文本：agent_node 会在下一轮注入系统提醒（见 agent_node 的空转检测）
    return "agent"


def _native_hybrid_enabled(state: dict) -> bool:
    if state.get("trigger_type") != "pipeline_failed":
        return False
    try:
        from ut_agent.config import REPAIR_BACKEND

        return REPAIR_BACKEND == "native"
    except Exception:
        return False


def route_from_start(state: AgentState) -> str:
    """Recover old checkpoints by planning before the ReAct executor when needed."""
    if not _native_hybrid_enabled(state):
        return "agent"
    from ut_agent.repair_plan import latest_repair_plan, latest_repair_verification, repair_plan_required

    if repair_plan_required(state):
        return "planner"
    plan = latest_repair_plan(state)
    verification = latest_repair_verification(state)
    if (
        plan is not None
        and verification is not None
        and verification.plan_id == plan.plan_id
        and verification.plan_version == plan.version
        and verification.verdict == "replan"
    ):
        return "planner"
    return "repair_memory" if native_memory_required(state) else "agent"


def route_after_planner(state: AgentState) -> str:
    """Retrieve task-scoped historical hints before executing a new plan Work Item."""
    return "repair_memory" if native_memory_required(state) else "agent"


def route_after_tools(state: AgentState) -> str:
    """finish 或确定不可重试的工具结果结束，其余工具结果回到 Agent。"""
    messages = state.get("messages", [])
    if not messages:
        return "agent"

    last_message = messages[-1]
    if isinstance(last_message, dict):
        content = last_message.get("content", "")
    else:
        content = getattr(last_message, "content", "")
    if str(content).startswith("FINISHED:"):
        return END
    try:
        payload = json.loads(str(content))
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict) and payload.get("status") == "blocked" and payload.get("retryable") is False:
        logger.warning(
            "[UT Agent] 工具返回不可重试终态，立即结束: %s",
            payload.get("error_code") or payload.get("message") or "blocked",
        )
        return END
    if _native_hybrid_enabled(state):
        from ut_agent.execution_ledger import build_execution_ledger
        from ut_agent.native_repair_state import evaluate_native_commit
        from ut_agent.repair_plan import (
            active_work_item,
            latest_repair_plan,
            latest_repair_verification,
            plan_scoped_attempts,
            repair_plan_required,
            verification_matches_plan,
        )

        if repair_plan_required(state):
            return "planner"
        ledger = build_execution_ledger(messages)
        latest_attempt = ledger.tool_attempts[-1] if ledger.tool_attempts else None
        if latest_attempt is not None and latest_attempt.name == "request_repair_replan_tool":
            if (latest_attempt.result or {}).get("status") == "success":
                return "planner"
        plan = latest_repair_plan(state)
        current = active_work_item(state)
        if plan is not None and current is not None and latest_attempt is not None:
            native = evaluate_native_commit(plan_scoped_attempts(state, ledger))
            verification = latest_repair_verification(state)
            already_verified = (
                verification is not None
                and verification_matches_plan(plan, verification)
                and verification.verdict == "pass"
                and verification.causal_alignment
                and verification.scope_compliant
                and verification.evidence_sufficient
                and verification.diff_digest == native.validated_diff_digest
                and current.work_item_id in verification.covered_work_item_ids
            )
            if latest_attempt.name == "run_repo_validation_tool" and native.allowed and not already_verified:
                return "verifier"
    if state.get("iteration", 0) >= state.get("max_iterations", 30):
        mandatory_action = next_mandatory_pipeline_action(state)
        if mandatory_action is not None and mandatory_action.name in _SAFETY_CONVERGENCE_TOOLS:
            return "agent"
        return END
    return "repair_memory" if _native_hybrid_enabled(state) and native_memory_required(state) else "agent"


def route_after_verifier(state: AgentState) -> str:
    """A replan verdict changes intent; pass/block returns to deterministic scheduling."""
    from ut_agent.repair_plan import latest_repair_verification

    verification = latest_repair_verification(state)
    if verification is not None and verification.verdict == "replan":
        return "planner"
    return "repair_memory" if native_memory_required(state) else "agent"


# ──────────────────────────────────────────────────────────────────────────────
# 图构建
# ──────────────────────────────────────────────────────────────────────────────


def build_graph(checkpointer=None) -> CompiledStateGraph:
    """Build the hybrid Planner -> constrained ReAct -> Verifier repair graph."""
    workflow = StateGraph(AgentState)

    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", create_tool_node())
    workflow.add_node("planner", repair_planner_node)
    workflow.add_node("repair_memory", repair_memory_node)
    workflow.add_node("verifier", repair_verifier_node)

    workflow.add_conditional_edges(
        START,
        route_from_start,
        {"agent": "agent", "planner": "planner", "repair_memory": "repair_memory"},
    )
    workflow.add_conditional_edges(
        "agent",
        route_after_agent,
        {"agent": "agent", "tools": "tools", END: END},
    )
    workflow.add_conditional_edges(
        "tools",
        route_after_tools,
        {"agent": "agent", "planner": "planner", "repair_memory": "repair_memory", "verifier": "verifier", END: END},
    )
    workflow.add_conditional_edges(
        "planner",
        route_after_planner,
        {"agent": "agent", "repair_memory": "repair_memory"},
    )
    workflow.add_edge("repair_memory", "agent")
    workflow.add_conditional_edges(
        "verifier",
        route_after_verifier,
        {"agent": "agent", "planner": "planner", "repair_memory": "repair_memory"},
    )

    return workflow.compile(checkpointer=checkpointer)


# ──────────────────────────────────────────────────────────────────────────────
# 高层接口
# ──────────────────────────────────────────────────────────────────────────────


def _iteration_limit_error(state: dict, messages: list) -> str:
    """Explain the unresolved ledger transition when the decision budget is exhausted."""
    iterations = int(state.get("iteration", 0) or 0)
    max_iterations = int(state.get("max_iterations", 30) or 30)
    if iterations < max_iterations:
        return ""
    if not messages:
        return "已达到最大诊断轮次。"
    content = _message_content(messages[-1])
    if content.startswith("FINISHED:"):
        return ""
    try:
        from ut_agent.execution_policy import build_execution_ledger

        ledger = build_execution_ledger(messages)
    except Exception:
        ledger = None
    if ledger is not None:
        last_push_attempt = next((
            attempt
            for attempt in reversed(ledger.tool_attempts)
            if attempt.name == "commit_and_push_tool"
            and (attempt.result or {}).get("status") == "success"
            and (attempt.result or {}).get("changed") is True
            and (attempt.result or {}).get("commit_sha")
        ), None)
        last_changed_repair = next((
            attempt
            for attempt in reversed(ledger.tool_attempts)
            if attempt.name == "generate_code_tool"
            and (attempt.args.get("operation") or (attempt.result or {}).get("operation")) == "repair"
            and (attempt.result or {}).get("status") in {"changed", "partial_changes"}
        ), None)
        if last_changed_repair is not None and (
            last_push_attempt is None or last_changed_repair.sequence > last_push_attempt.sequence
        ):
            changed_files = ", ".join((last_changed_repair.result or {}).get("changed_files") or []) or "未知文件"
            return f"自动修复已产生修改但达到最大诊断轮次，尚未提交：{changed_files}。"
        if last_push_attempt is not None:
            pushed_sha = str((last_push_attempt.result or {}).get("commit_sha") or "")
            verified = any(
                pipeline.get("requested_commit_sha") == pushed_sha
                and pipeline.get("matched_commit_sha") == pushed_sha
                and pipeline.get("pipeline_status") not in {None, "", "running", "pending", "created"}
                for pipeline in ledger.pipelines
                if int(pipeline.get("_sequence") or 0) > last_push_attempt.sequence
            )
            if not verified:
                return f"修复提交 {pushed_sha} 已推送，但达到最大诊断轮次，尚未完成精确流水线验证。"
        last_generate = next((
            attempt
            for attempt in reversed(ledger.tool_attempts)
            if attempt.name == "generate_code_tool" and attempt.result
        ), None)
        if last_generate is not None:
            status = str((last_generate.result or {}).get("status") or "")
            failure_kind = str((last_generate.result or {}).get("failure_kind") or "")
            operation = str(last_generate.args.get("operation") or (last_generate.result or {}).get("operation") or "")
            if status == "repair_timeout" or (
                failure_kind == "execution_budget_exhausted" and operation == "repair"
            ):
                return "自动修复执行超时：Hermes 达到单次执行上限，修复尚未完成。"
            if status == "investigation_timeout" or failure_kind == "search_loop":
                return "自动调查超时：Hermes 在搜索/读取循环中达到执行上限，调查尚未完成。"
    try:
        result = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        result = {}
    status = str(result.get("status") or "unknown")
    return f"已执行最后一个工具，但任务达到最大诊断轮次；最后工具状态：{status}。"


def _extract_result(state: dict, final_messages: list) -> dict:
    """从 agent 最终状态提取结构化判定结果。

    用 build_execution_ledger 从 messages 派生（纯函数，不依赖 agent_node 内部）。
    success/finish_reason 从最后一条 FINISHED 消息或降级推断。
    """
    from pr_agent.triage.pipeline_coverage import normalize_coverage
    from ut_agent.dependency_evidence import dependency_blockers_from_messages
    from ut_agent.execution_policy import (
        build_execution_ledger,
        build_failure_explanation_records,
        build_repair_action_records,
    )
    from ut_agent.repair_coordinator import build_repair_snapshot
    from ut_agent.repair_plan import latest_repair_verification, repair_plan_audit

    iterations = int(state.get("iteration", 0) or 0)
    max_iterations = int(state.get("max_iterations", 30) or 30)
    protocol_error = str(state.get("model_terminal_error") or "")
    terminal_failure_kind = str(state.get("model_terminal_failure_kind") or "")
    terminal_validation_error_code = str(state.get("terminal_validation_error_code") or "")
    terminal_validation_summary = str(state.get("terminal_validation_summary") or "")
    normalized_diagnostic_alias_count = int(state.get("normalized_diagnostic_alias_count", 0) or 0)
    plan_audit = repair_plan_audit(state)
    latest_verification = latest_repair_verification(state)
    verification_audit = latest_verification.model_dump(mode="json") if latest_verification is not None else None
    if final_messages:
        final_content = _message_content(final_messages[-1])
        if final_content.startswith(AGENT_LLM_PROTOCOL_ERROR_PREFIX):
            protocol_error = final_content

    try:
        ledger = build_execution_ledger(final_messages)
        snapshot = build_repair_snapshot(final_messages)
    except Exception as e:
        return {
            "success": 0,
            "finish_reason": "",
            "iterations": iterations,
            "max_iterations": max_iterations,
            "pushed_sha": None,
            "push_attempts": [],
            "pipeline_groups": [],
            "observed_jobs": [],
            "observed_jobs_truncated": False,
            "failure_reconciliation": None,
            "result_pipeline_id": 0,
            "result_pipeline_sha": "",
            "final_pipeline_status": "unknown",
            "coverage_source": "",
            "coverage_status": "",
            "failure_signatures": [],
            "failure_explanations": [],
            "repair_actions": [],
            "dependency_blockers": [],
            "blocked_job_names": [],
            "error": protocol_error or f"ledger build failed: {e}",
            "active_model": state.get("active_model"),
            "attempted_models": state.get("attempted_models", []),
            "model_failover_count": int(state.get("model_failover_count", 0) or 0),
            "last_model_failure_code": state.get("last_model_failure_code"),
            "terminal_failure_kind": terminal_failure_kind,
            "terminal_validation_error_code": terminal_validation_error_code,
            "terminal_validation_summary": terminal_validation_summary,
            "normalized_diagnostic_alias_count": normalized_diagnostic_alias_count,
            "repair_plan": plan_audit,
            "repair_verification": verification_audit,
        }

    pushed_sha = snapshot.latest_pushed_sha or None
    terminal_push = next((
        push for push in reversed(ledger.pushes)
        if push.get("status") == "blocked" and push.get("retryable") is False
    ), None)
    finish_reason = str((terminal_push or {}).get("error_code") or "")
    if not pushed_sha and not finish_reason and ledger.pushes:
        finish_reason = "repair_not_pushed"
    # 修复结果只能来自最后推送 commit 的精确流水线。源码或下游流水线仍作为证据保留，
    # 但没有修复提交时不能伪装成“结果 Pipeline”。
    matched = snapshot.latest_exact_pipeline if pushed_sha else None
    confirmed_pipelines = [pipeline for pipeline in ledger.pipelines if pipeline.get("pipeline_status")]
    evidence_pipeline = confirmed_pipelines[-1] if confirmed_pipelines else None
    final_pipeline_evidence = matched or evidence_pipeline or {}
    observed_jobs = [
        dict(job)
        for job in final_pipeline_evidence.get("observed_jobs") or []
        if isinstance(job, dict)
    ]
    observed_jobs_truncated = bool(final_pipeline_evidence.get("observed_jobs_truncated"))
    raw_reconciliation = final_pipeline_evidence.get("failure_reconciliation")
    failure_reconciliation = dict(raw_reconciliation) if isinstance(raw_reconciliation, dict) else None
    final_status = "unknown"
    if matched is not None:
        candidate_status = str(matched.get("pipeline_status") or "unknown").lower()
        final_status = (
            candidate_status if candidate_status in {"success", "failed", "canceled", "skipped"} else "unknown"
        )
    success = 1 if (final_status == "success" and not (matched or {}).get("failed_jobs")) else 0
    dependency_blockers = dependency_blockers_from_messages(final_messages)
    blocked_job_names = list(dict.fromkeys(
        str(record.get("job_name") or "")
        for record in dependency_blockers
        if str(record.get("job_name") or "")
    ))

    # 提取变更行覆盖率（软指标，仅供看板展示，不影响 success 判定）
    final_coverage = None
    if matched is not None:
        final_coverage = normalize_coverage(matched.get("coverage"))

    pipeline_groups = []
    group_positions = {}
    for pipeline in ledger.pipelines:
        validation_id = pipeline.get("validation_pipeline_id") or pipeline.get("pipeline_id")
        identity = (
            pipeline.get("attempt_id") or pipeline.get("requested_commit_sha"),
            pipeline.get("root_pipeline_id") or pipeline.get("pipeline_id"),
            validation_id,
        )
        if not validation_id:
            continue
        group_result = {
            "attempt_id": pipeline.get("attempt_id", ""),
            "requested_commit_sha": pipeline.get("requested_commit_sha"),
            "root_pipeline_id": pipeline.get("root_pipeline_id") or pipeline.get("pipeline_id"),
            "validation_pipeline_id": validation_id,
            "pipeline_ids": pipeline.get("pipeline_ids") or [validation_id],
            "status": pipeline.get("pipeline_status") or "unknown",
            "coverage": pipeline.get("coverage"),
            "coverage_source": pipeline.get("coverage_source", ""),
            "coverage_status": pipeline.get("coverage_status", ""),
            "failed_jobs": [job.get("name") for job in pipeline.get("failed_jobs", []) if isinstance(job, dict)],
            "observed_jobs": [
                dict(job) for job in pipeline.get("observed_jobs") or [] if isinstance(job, dict)
            ],
            "observed_jobs_truncated": bool(pipeline.get("observed_jobs_truncated")),
            "failure_reconciliation": (
                dict(pipeline["failure_reconciliation"])
                if isinstance(pipeline.get("failure_reconciliation"), dict)
                else None
            ),
        }
        if identity in group_positions:
            pipeline_groups[group_positions[identity]] = group_result
        else:
            group_positions[identity] = len(pipeline_groups)
            pipeline_groups.append(group_result)

    pending_attempt = (
        {"attempt_id": snapshot.latest_attempt_id, "commit_sha": snapshot.latest_pushed_sha}
        if snapshot.requires_exact_pipeline
        else None
    )
    pending_error = (
        f"修复提交 {snapshot.latest_pushed_sha} 已推送，但尚未完成精确流水线验证。"
        if pending_attempt
        else ""
    )
    coding_infra_error = ""
    last_generate = next((
        attempt
        for attempt in reversed(ledger.tool_attempts)
        if attempt.name == "generate_code_tool" and attempt.result
    ), None)
    if last_generate is not None:
        generate_result = last_generate.result or {}
        terminal_failure_kind = str(generate_result.get("failure_kind") or terminal_failure_kind)
        terminal_validation_error_code = str(
            generate_result.get("terminal_validation_error_code") or ""
        )[:80]
        terminal_validation_summary = str(
            generate_result.get("terminal_validation_summary") or ""
        )[:500]
        normalized_diagnostic_alias_count = max(
            0,
            int(generate_result.get("normalized_diagnostic_alias_count") or 0),
        )
    if not pushed_sha:
        infra_attempt = next((
            attempt
            for attempt in reversed(ledger.tool_attempts)
            if attempt.name == "generate_code_tool"
            and str((attempt.result or {}).get("status") or "") == "coding_infra_error"
        ), None)
        if infra_attempt is not None:
            coding_infra_error = str((infra_attempt.result or {}).get("message") or "").strip()
            if not terminal_failure_kind:
                terminal_failure_kind = str((infra_attempt.result or {}).get("failure_kind") or "")
    result_pipeline_id = int(
        (matched or {}).get("validation_pipeline_id") or (matched or {}).get("pipeline_id") or 0
    )
    result_pipeline_sha = str((matched or {}).get("matched_commit_sha") or pushed_sha or "")
    return {
        "success": success,
        "finish_reason": finish_reason,
        "iterations": iterations,
        "max_iterations": max_iterations,
        "pushed_sha": pushed_sha,
        "push_attempts": ledger.pushes,
        "pipeline_groups": pipeline_groups,
        "observed_jobs": observed_jobs,
        "observed_jobs_truncated": observed_jobs_truncated,
        "failure_reconciliation": failure_reconciliation,
        "result_pipeline_id": result_pipeline_id,
        "result_pipeline_sha": result_pipeline_sha,
        "final_pipeline_status": final_status,
        "coordinator_phase": snapshot.publication_phase.value,
        "terminal_proof": snapshot.terminal_proof.to_dict() if snapshot.terminal_proof else None,
        "pending_attempt": pending_attempt,
        "final_coverage": final_coverage,
        "coverage_source": str((matched or {}).get("coverage_source") or ""),
        "coverage_status": str((matched or {}).get("coverage_status") or ""),
        "failure_signatures": ledger.failure_signatures,
        "failure_explanations": build_failure_explanation_records(final_messages, matched or evidence_pipeline),
        "repair_actions": build_repair_action_records(final_messages),
        "dependency_blockers": dependency_blockers,
        "blocked_job_names": blocked_job_names,
        "error": (
            protocol_error
            or pending_error
            or coding_infra_error
            or terminal_validation_summary
            or _iteration_limit_error(state, final_messages)
            or None
        ),
        "active_model": state.get("active_model"),
        "attempted_models": state.get("attempted_models", []),
        "model_failover_count": int(state.get("model_failover_count", 0) or 0),
        "last_model_failure_code": state.get("last_model_failure_code"),
        "terminal_failure_kind": terminal_failure_kind,
        "terminal_validation_error_code": terminal_validation_error_code,
        "terminal_validation_summary": terminal_validation_summary,
        "normalized_diagnostic_alias_count": normalized_diagnostic_alias_count,
        "repair_plan": plan_audit,
        "repair_verification": verification_audit,
    }


def _coverage_note(messages: list) -> str:
    """从流水线结果提取变更行覆盖率提示（软指标，仅提醒，不影响成败）。"""
    from ut_agent.execution_policy import build_execution_ledger
    try:
        ledger = build_execution_ledger(messages)
    except Exception:
        return ""
    for pipeline in reversed(ledger.pipelines):
        cov = pipeline.get("coverage")
        thr = pipeline.get("coverage_threshold")
        if cov is None:
            continue
        try:
            if thr is not None and float(cov) < float(thr):
                return (f"提示：变更行覆盖率 {cov}% 未达 {thr}%"
                        "（CI 已放行，不影响修复结果），建议人工补充测试。")
        except (TypeError, ValueError):
            pass
        return f"变更行覆盖率 {cov}%。"
    return ""


def _build_finished_card(state: dict, messages: list, result_dict: dict, raw_response: str) -> str:
    """统一生成 FINISHED 总结卡片，保证任何结束路径都有规范结果。

    success 只看流水线结果；覆盖率未达标只在 summary 提醒，不判为失败。
    """
    import re

    success = bool(result_dict.get("success"))
    final_status = result_dict.get("final_pipeline_status", "unknown")
    pushed_sha = result_dict.get("pushed_sha")
    finish_reason = str(result_dict.get("finish_reason") or "")
    infrastructure_error = str(result_dict.get("error") or "")

    # 若模型已正常调用 finish_tool，复用它写的 summary
    model_summary = ""
    model_claimed_success = None
    if raw_response and "FINISHED:" in raw_response:
        success_match = re.search(r"FINISHED:\s*success=(True|False)", raw_response)
        if success_match:
            model_claimed_success = success_match.group(1) == "True"
        m = re.search(r"summary=(.*)", raw_response, re.S)
        if m:
            model_summary = m.group(1).strip()
    # 系统内部占位/错误文本不能当作 summary
    if (model_summary.startswith("ERROR")
            or "空响应" in model_summary
            or model_summary.startswith("已达到最大迭代")
            or model_summary.startswith("UT Agent 完成，但未生成明确")
            or (model_claimed_success is not None and model_claimed_success != success)):
        model_summary = ""

    cov_note = _coverage_note(messages)

    if success:
        sha_hint = f"（{pushed_sha[:8]}）" if pushed_sha else ""
        summary = model_summary or f"流水线已修复通过{sha_hint}。"
    elif model_summary:
        summary = model_summary
    elif finish_reason == "remote_branch_changed":
        summary = (
            "自动修复未完成：MR 源分支在修复期间发生变化，为避免覆盖新提交，修复提交未能推送；"
            "原流水线仍为失败，没有创建新的验证流水线。请基于最新分支重新触发修复。"
        )
    elif finish_reason in {"commit_recovery_mismatch", "push_target_mismatch", "repair_not_pushed"}:
        summary = "自动修复未完成：修复提交未能推送，因此没有创建新的验证流水线；原流水线仍为失败。"
    elif infrastructure_error:
        summary = infrastructure_error.removeprefix("ERROR: ")
    elif final_status == "failed":
        summary = "自动修复未能使流水线通过，需人工介入处理。"
    else:
        summary = (f"未能在本次运行内确认流水线结果（最后状态：{final_status}）；"
                   "可能仍在运行或运行被中断，请稍后手动查看 MR 流水线状态。")

    if cov_note:
        summary = f"{summary} {cov_note}"

    return f"FINISHED: success={success}, summary={summary}"


class UTAgent:
    """UT Agent 的高层接口（保持外部调用方式不变）。"""

    def __init__(self, checkpointer=None):
        if checkpointer is None:
            try:
                from pr_agent.distributed.runtime import get_execution_runtime

                runtime = get_execution_runtime()
                checkpointer = runtime.checkpointer if runtime is not None else None
            except Exception:
                checkpointer = None
        self.checkpointer = checkpointer
        self.graph = build_graph(checkpointer=checkpointer)

    async def run(self, mr_info: dict) -> str:
        runtime = None
        try:
            from pr_agent.distributed.runtime import get_execution_runtime

            runtime = get_execution_runtime()
        except Exception:
            pass
        if runtime is not None and runtime.mode == "queue":
            return await self._run(mr_info)
        async with _run_lock(mr_info):
            return await self._run(mr_info)

    async def resume(self, task_id: str, pipeline_event) -> dict:
        config = {
            "configurable": {"thread_id": task_id, "checkpoint_ns": ""},
            "recursion_limit": 300,
        }
        try:
            from ut_agent.repair_memory.outcomes import settle_immediate_pipeline

            await asyncio.to_thread(settle_immediate_pipeline, task_id, pipeline_event)
        except Exception:
            logger.exception(f"Repair memory outcome settlement failed: task_id={task_id}")
        event_payload = pipeline_event.to_dict() if hasattr(pipeline_event, "to_dict") else pipeline_event
        result = await self._invoke_graph(Command(resume=event_payload), config)
        return self._finalize_result(result)

    async def _run(self, mr_info: dict) -> str:
        """
        运行 UT Agent，传入 MR 信息，返回要发布为评论的响应字符串。

        mr_info 应包含：
        - trigger_type: "mr_created" | "pipeline_failed" | "manual_triage"
        - pr_url, mr_id, title, author, source_branch, target_branch
        - diff_files (mr_created 时)
        - failed_jobs, pipeline_id (pipeline_failed 时)
        """
        if not mr_info.get("messages"):
            mr_info["messages"] = [{"role": "user", "content": "请根据当前上下文完成诊断、修复和验证。"}]
        mr_info.setdefault("iteration", 0)
        mr_info.setdefault("max_iterations", 30)
        mr_info.setdefault("repair_plans", [])
        mr_info.setdefault("repair_verifications", [])
        mr_info["timestamp"] = datetime.now(timezone.utc).isoformat()

        logger.info(f"[UT Agent] 启动，触发类型: {mr_info.get('trigger_type', 'manual_triage')}")
        logger.info(f"[UT Agent] MR: !{mr_info.get('mr_id', '?')} {mr_info.get('title', '')}")

        # 初始化对话日志文件（每次运行清空旧日志）
        try:
            with open(_conversation_log_path(mr_info), "w", encoding="utf-8") as f:
                f.write("UT Agent 对话日志\n")
                f.write(f"触发类型: {mr_info.get('trigger_type', 'manual_triage')}\n")
                f.write(f"MR: !{mr_info.get('mr_id', '?')} {mr_info.get('title', '')}\n")
                f.write(f"分支: {mr_info.get('source_branch', '?')} -> {mr_info.get('target_branch', '?')}\n")
                f.write(f"开始时间: {mr_info.get('timestamp', '')}\n")
        except Exception:
            pass

        # 初始化工具上下文
        from ut_agent.tools.context import get_git_provider
        if not get_git_provider():
            logger.warning("[UT Agent] ToolContext.git_provider 未初始化，工具可能无法正常工作")

        # 显式放宽递归上限：迭代扩展后一轮 = agent + tools 两步，默认 25 步不够
        config = {"recursion_limit": 300}
        try:
            from pr_agent.distributed.runtime import get_execution_runtime

            runtime = get_execution_runtime()
            if runtime is not None and runtime.mode == "queue":
                config["configurable"] = {"thread_id": runtime.task_id, "checkpoint_ns": ""}
        except Exception:
            pass
        result = await self._invoke_graph(mr_info, config)

        return self._finalize_result(result)

    async def _invoke_graph(self, graph_input, config: dict) -> dict:
        if getattr(self, "checkpointer", None) is not None:
            result = await self.graph.ainvoke(graph_input, config=config, durability="sync")
        else:
            result = await self.graph.ainvoke(graph_input, config=config)
        interrupts = result.get("__interrupt__", [])
        if interrupts:
            value = getattr(interrupts[0], "value", None) or {}
            wait_kind = str(value.get("kind") or value.get("wait") or "unknown")
            project_id = str(value.get("project_id") or "")
            commit_sha = str(value.get("commit_sha") or value.get("sha") or "")
            attempt_id = str(value.get("attempt_id") or "")
            pipeline_id = value.get("pipeline_id")
            task_id = str((config.get("configurable") or {}).get("thread_id") or "")
            from pr_agent.distributed.runtime import TaskSuspended

            if attempt_id or pipeline_id is not None:
                from pr_agent.distributed.models import PipelineWaitIdentity

                wait_identity = PipelineWaitIdentity(
                    project_id=project_id,
                    sha=commit_sha,
                    attempt_id=attempt_id,
                    pipeline_id=int(pipeline_id) if pipeline_id is not None else None,
                ).to_json()
            else:
                wait_identity = f"{project_id}:{commit_sha}"
            raise TaskSuspended(task_id, wait_kind, wait_identity)
        return result

    @staticmethod
    def _finalize_result(result: dict) -> dict:

        # 从最后一条消息提取结果
        messages = result.get("messages", [])
        response_str = "ERROR: Agent 未生成任何响应"
        if messages:
            last_msg = messages[-1]
            if isinstance(last_msg, dict):
                content = last_msg.get("content", "")
                response_str = content or "UT Agent 完成，但未生成明确的结果报告。"
            else:
                content = getattr(last_msg, "content", None)
                response_str = str(content) if content else str(last_msg)

        result_dict = _extract_result(result, messages)

        # 无论走哪条结束路径（模型正常 finish / 迭代超限 / 空转强制结束 / 空响应占位），
        # 都统一输出一张 "FINISHED: success=X, summary=..." 总结卡片：
        # - success 严格由流水线结果决定（覆盖率软警告只在 summary 提醒，不判失败）
        # - result_dict 已带正确 success/字段，供 CI triage 看板捕获
        response_str = _build_finished_card(result, messages, result_dict, response_str)

        return {"response": response_str, "result": result_dict, "state": result}
