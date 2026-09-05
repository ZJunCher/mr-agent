"""
ut_agent LLM 调用封装 - 基于 litellm 的统一接口，使用独立模型配置。
"""
import asyncio
import hashlib
import json as _json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import litellm
from langchain_core.messages import convert_to_openai_messages

from pr_agent.log import get_logger
from ut_agent.config import API_KEY, BASE_URL, DEFAULT_TEMPERATURE, MODEL_CANDIDATES
from ut_agent.model_failover import (
    LLMCallOutcome,
    ModelAttempt,
    ModelFailure,
    build_model_health_store,
    classify_model_failure,
    ordered_candidates,
)

_MODEL_HEALTH_STORE = build_model_health_store()
_DIRECT_FAILOVER_CODES = {"quota_exceeded", "model_unavailable", "tool_schema_unsupported"}
_SAME_MODEL_ATTEMPTS = 3


@dataclass(frozen=True)
class LLMTextOutcome:
    text: str
    model: str
    attempts: tuple[ModelAttempt, ...]
    terminal_error: str = ""


@dataclass(frozen=True)
class CompressionResult:
    messages: list[dict]
    state: dict[str, Any]


def _is_transient_error(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in (
        "timeout",
        "timed out",
        "rate limit",
        "ratelimit",
        "connection",
        "service unavailable",
        "temporarily unavailable",
        "429",
        "502",
        "503",
        "504",
    ))


def _model_call_owner() -> str:
    try:
        from pr_agent.distributed.runtime import get_execution_runtime

        runtime = get_execution_runtime()
        if runtime is not None and runtime.task_id:
            return runtime.task_id
    except Exception:
        pass
    task = asyncio.current_task()
    return f"{os.getpid()}:{id(task)}"


def _call_candidates(requested_model: str | None, active_model: str | None) -> tuple[str, ...]:
    if requested_model and requested_model not in MODEL_CANDIDATES:
        return (requested_model,)
    return ordered_candidates(MODEL_CANDIDATES, active_model or requested_model)


def _compact_failure_reason(failure: ModelFailure) -> str:
    reason = failure.reason.replace(API_KEY, "[REDACTED]") if API_KEY else failure.reason
    return " ".join(reason.split())[:240]


def _terminal_model_error(attempts: list[ModelAttempt]) -> str:
    model_names = list(dict.fromkeys(attempt.model.split("/", 1)[-1] for attempt in attempts))
    failure_codes = list(dict.fromkeys(attempt.failure_code for attempt in attempts if attempt.failure_code))
    models = "、".join(model_names) or "无可用候选模型"
    codes = "、".join(failure_codes) or "线路不可用"
    return f"模型服务暂时不可用；已尝试模型：{models}；原因：{codes}。"


async def _completion_with_failover(
    completion_kwargs: dict[str, Any],
    *,
    requested_model: str | None = None,
    active_model: str | None = None,
    response_validator: Callable[[Any], ModelFailure | None] | None = None,
    failure_classifier: Callable[[Exception], ModelFailure] = classify_model_failure,
) -> LLMCallOutcome:
    attempts: list[ModelAttempt] = []
    owner = _model_call_owner()
    candidates = _call_candidates(requested_model, active_model)

    for candidate_index, candidate in enumerate(candidates):
        if not _MODEL_HEALTH_STORE.candidate_allowed(candidate, owner):
            attempts.append(ModelAttempt(candidate, "cooldown", "shared cooldown"))
            continue

        for same_model_attempt in range(1, _SAME_MODEL_ATTEMPTS + 1):
            failure = None
            response = None
            try:
                response = await litellm.acompletion(model=candidate, **completion_kwargs)
                if response_validator is not None:
                    failure = response_validator(response)
            except Exception as error:
                failure = failure_classifier(error)

            if failure is None:
                _MODEL_HEALTH_STORE.mark_succeeded(candidate, owner)
                attempts.append(ModelAttempt(candidate))
                if candidate_index:
                    get_logger().warning(
                        f"Model failover selected {candidate} after {candidate_index} unavailable route(s)"
                    )
                return LLMCallOutcome(response=response, model=candidate, attempts=tuple(attempts))

            compact_reason = _compact_failure_reason(failure)
            attempts.append(ModelAttempt(candidate, failure.code, compact_reason))
            get_logger().error(
                f"Model call failed: model={candidate} code={failure.code} "
                f"attempt={same_model_attempt}/{_SAME_MODEL_ATTEMPTS}"
            )
            if not failure.switchable:
                return LLMCallOutcome(
                    response=None,
                    model=None,
                    attempts=tuple(attempts),
                    terminal_error=f"模型请求失败：{candidate.split('/', 1)[-1]}（{failure.code}）。",
                )
            if failure.code in _DIRECT_FAILOVER_CODES or same_model_attempt == _SAME_MODEL_ATTEMPTS:
                _MODEL_HEALTH_STORE.mark_failed(candidate, owner, failure)
                break
            await asyncio.sleep(2 ** (same_model_attempt - 1))

    return LLMCallOutcome(
        response=None,
        model=None,
        attempts=tuple(attempts),
        terminal_error=_terminal_model_error(attempts),
    )


async def call_llm(
    system: str,
    user: str,
    temperature: float = None,
    model: str = None,
    max_tokens: int = 16384,
) -> str:
    """
    调用 LLM 获取回复。

    参数:
        system: system prompt
        user: user prompt
        temperature: 温度参数，默认从 config 读取
        model: 模型名称，默认从 ut_agent.config 读取
        max_tokens: 最大输出 token 数，默认 16384

    返回:
        LLM 回复的文本内容
    """
    if temperature is None:
        temperature = DEFAULT_TEMPERATURE

    outcome = await call_llm_outcome(
        system,
        user,
        temperature=temperature,
        model=model,
        max_tokens=max_tokens,
    )
    if outcome.terminal_error:
        return f"ERROR: {outcome.terminal_error}"
    return outcome.text


async def call_llm_outcome(
    system: str,
    user: str,
    *,
    temperature: float = 0.0,
    model: str | None = None,
    max_tokens: int = 1800,
) -> LLMTextOutcome:
    """Return bounded assistant text together with the existing failover metadata."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    outcome = await _completion_with_failover(
        {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "api_key": API_KEY,
            "api_base": BASE_URL,
        },
        requested_model=model,
    )
    if outcome.terminal_error:
        return LLMTextOutcome("", outcome.model or "", outcome.attempts, outcome.terminal_error)
    response = outcome.response
    content = str(response.choices[0].message.content or "")
    finish_reason = response.choices[0].finish_reason
    if finish_reason == "length":
        get_logger().warning(f"LLM 输出被截断 (max_tokens={max_tokens})，finish_reason=length")
    return LLMTextOutcome(content, outcome.model or "", outcome.attempts)


def _classify_tool_schema_failure(error: Exception) -> ModelFailure:
    """Allow failover when one route explicitly rejects Tool Calling parameters."""
    failure = classify_model_failure(error)
    normalized = str(error).lower()
    if failure.code == "http_400" and any(marker in normalized for marker in (
        "tool_choice",
        "tool schema",
        "tools is not supported",
        "function calling",
        "unsupported tool",
    )):
        return ModelFailure("tool_schema_unsupported", failure.reason, True)
    return failure


async def call_tool_llm_outcome(
    system: str,
    user: str,
    *,
    tools: list[dict[str, Any]],
    tool_choice: dict[str, Any] | str,
    temperature: float = 0.0,
    model: str | None = None,
    active_model: str | None = None,
    max_tokens: int = 1800,
) -> LLMCallOutcome:
    """Call a model with a caller-selected Tool Calling contract."""
    outcome = await _completion_with_failover(
        {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "api_key": API_KEY,
            "api_base": BASE_URL,
            "tools": tools,
            "tool_choice": tool_choice,
        },
        requested_model=model,
        active_model=active_model,
        failure_classifier=_classify_tool_schema_failure,
    )
    if outcome.response is None:
        return outcome
    return LLMCallOutcome(
        response=outcome.response.choices[0].message,
        model=outcome.model,
        attempts=outcome.attempts,
    )


async def call_llm_with_continuation(
    system: str,
    user: str,
    temperature: float = None,
    model: str = None,
    max_tokens: int = 32000,
    max_continuations: int = 3,
) -> str:
    """
    调用 LLM 并在截断时自动续写（适用于超长 JSON 输出如测试计划）。

    当 finish_reason == "length" 时，将已有输出拼接到 messages 中，
    追加续写指令让 LLM 从断点继续输出，最多续写 max_continuations 次。

    参数:
        system: system prompt
        user: user prompt
        temperature: 温度参数
        model: 模型名称
        max_tokens: 每次调用的最大输出 token 数，默认 32000
        max_continuations: 最大续写次数，默认 3

    返回:
        拼接后的完整文本
    """
    if temperature is None:
        temperature = DEFAULT_TEMPERATURE

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    full_content = ""
    active_model = model

    for i in range(1 + max_continuations):
        outcome = await _completion_with_failover(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "api_key": API_KEY,
                "api_base": BASE_URL,
            },
            requested_model=model,
            active_model=active_model,
        )
        if outcome.terminal_error:
            if full_content:
                break
            return f"ERROR: {outcome.terminal_error}"
        active_model = outcome.model
        response = outcome.response
        chunk = response.choices[0].message.content or ""
        full_content += chunk
        finish_reason = response.choices[0].finish_reason

        if finish_reason != "length":
            break

        get_logger().warning(
            f"LLM 输出截断 (第 {i+1} 段, max_tokens={max_tokens})，尝试续写..."
        )
        messages.append({"role": "assistant", "content": chunk})
        messages.append({
            "role": "user",
            "content": "输出被截断了，请从断点处继续输出剩余的 JSON 内容"
            "（不要重复已输出的部分，直接从上次结束的位置继续）：",
        })

    return full_content


# ──────────────────────────────────────────────────────────────────────────────
# ReAct Agent 专用：支持 function calling 的 LLM 调用 + 上下文压缩
# ──────────────────────────────────────────────────────────────────────────────

AGENT_LLM_PROTOCOL_ERROR_PREFIX = "ERROR: Agent LLM 工具调用响应异常"
MODEL_UNAVAILABLE_PREFIX = "ERROR: 模型服务暂时不可用"
_TOOL_CALL_FINISH_REASONS = {"tool_calls", "tool_use"}


def _message_tool_calls(message) -> list:
    if isinstance(message, dict):
        return message.get("tool_calls") or []
    return getattr(message, "tool_calls", None) or []


async def call_agent_llm(
    system_prompt: str,
    messages: list[dict],
    tools: list[dict],
    temperature: float = None,
    model: str = None,
    active_model: str | None = None,
    return_outcome: bool = False,
    compression_state: dict[str, Any] | None = None,
) -> dict | LLMCallOutcome:
    """ReAct Agent 的 LLM 调用：支持 function calling（tool_calls）.

    参数:
        system_prompt: 系统 prompt（含工具描述 + 当前上下文）
        messages: 对话历史（OpenAI 格式）
        tools: 工具定义列表（OpenAI function calling 格式）
        temperature: 温度参数，默认从 config 读取
        model: 模型名称，默认从 ut_agent.config 读取

    返回:
        OpenAI response.choices[0].message 对象，含 tool_calls 或 content。
        出错时返回一个无工具调用的 dict。
    """
    if temperature is None:
        temperature = DEFAULT_TEMPERATURE

    # 上下文压缩：防止对话历史超出 token 预算
    compression = await compress_messages_if_needed(
        convert_to_openai_messages(messages),
        compression_state=compression_state,
        return_state=True,
    )

    def validate_tool_response(response) -> ModelFailure | None:
        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason in _TOOL_CALL_FINISH_REASONS and not _message_tool_calls(choice.message):
            return ModelFailure(
                "tool_protocol_error",
                f"{AGENT_LLM_PROTOCOL_ERROR_PREFIX}: finish_reason={finish_reason}, tool_calls=0",
                True,
            )
        return None

    outcome = await _completion_with_failover(
        {
            "messages": [{"role": "system", "content": system_prompt}] + compression.messages,
            "temperature": temperature,
            "api_key": API_KEY,
            "api_base": BASE_URL,
            "tools": tools,
            "tool_choice": "auto",
        },
        requested_model=model,
        active_model=active_model,
        response_validator=validate_tool_response,
    )
    outcome = LLMCallOutcome(
        response=outcome.response.choices[0].message if outcome.response is not None else None,
        model=outcome.model,
        attempts=outcome.attempts,
        terminal_error=outcome.terminal_error,
        context_compression=compression.state,
    )
    if return_outcome:
        return outcome
    if outcome.terminal_error:
        return {"role": "assistant", "content": f"{MODEL_UNAVAILABLE_PREFIX}：{outcome.terminal_error}"}
    return outcome.response


# ── 上下文压缩（简单版）──


def extract_known_facts(messages: list) -> str:
    """从对话历史提取已知事实，只从 tool 返回提取（不碰 assistant 思考）。

    优先从 [FACT] 标记提取（思路 3，工具自己声明事实），降级用关键词匹配（思路 1）。
    """
    facts = []
    tool_summaries = []

    # 先建 tool_call_id → tool_name 映射
    tool_name_map = {}
    for msg in messages:
        if isinstance(msg, dict):
            for tc in (msg.get("tool_calls") or []):
                call_id = tc.get("id", "")
                name = tc.get("function", {}).get("name", "")
                if call_id and name:
                    tool_name_map[call_id] = name

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not content:
            continue

        call_id = msg.get("tool_call_id", "")
        tool_name = tool_name_map.get(call_id, "")

        # 优先：从 [FACT] 标记提取（思路 3）
        fact_lines = [line[6:].strip() for line in content.splitlines() if line.startswith("[FACT]")]
        # 也支持 JSON 格式的 _facts 字段
        parsed = None
        try:
            parsed = _json.loads(content)
            if isinstance(parsed, dict) and "_facts" in parsed:
                fact_lines.extend(parsed["_facts"])
        except (ValueError, TypeError):
            pass

        if isinstance(parsed, dict):
            fact_lines.extend(_native_safety_facts(tool_name, parsed))
            fact_lines.extend(_pipeline_reconciliation_fact_lines(parsed))

        if fact_lines:
            facts.extend(fact_lines)
        else:
            if tool_name == "search_repo_tool":
                try:
                    parsed = _json.loads(content)
                    count = parsed.get("count", 0)
                    facts.append(f"已搜索源码，找到 {count} 个匹配")
                except (ValueError, TypeError):
                    pass
            # 降级：关键词匹配（思路 1）
            if tool_name == "clone_source_branch_tool" or ("repo/" in content and "clone" in content.lower()):
                facts.append(f"已克隆仓库: {content[:100]}")
            elif tool_name == "read_repo_file_tool" or any(
                marker in content.lower() for marker in ["cmake_minimum", "find_package", "package.xml", "ament_"]
            ):
                facts.append(f"已读文件（含构建配置）: {content[:200]}")
            elif "failed_jobs" in content or "pipeline_status" in content or "work_items" in content:
                facts.append(f"流水线结果: {content[:200]}")
            elif "commit_sha" in content or "pushed" in content.lower() or "commit" in content.lower():
                facts.append(f"已推送代码: {content[:100]}")
            elif "applied" in content.lower() or "patch" in content.lower():
                facts.append(f"已应用补丁: {content[:100]}")
            else:
                facts.append(f"工具返回({tool_name or 'unknown'}): {content[:150]}")

        # 保留最近 3 个工具结果摘要
        tool_summaries.append(f"[{tool_name or 'tool'}] {content[:500]}")

    if not facts:
        return ""

    facts_text = "\n".join(f"- {f}" for f in facts[-10:])
    recent_tools = "\n".join(tool_summaries[-3:])
    return f"{facts_text}\n\n## 最近工具结果\n{recent_tools}"


def _native_safety_facts(tool_name: str, parsed: dict[str, Any]) -> list[str]:
    """Return immutable Native identities without depending on long result bodies."""
    if tool_name == "apply_repo_patch_tool" and parsed.get("status") == "changed":
        files = ", ".join(str(path) for path in parsed.get("changed_files") or () if str(path))
        digest = str(parsed.get("diff_digest") or "")
        detail = f"，Diff {digest}" if digest else ""
        return [f"已应用补丁{detail}，修改文件: {files or '无'}"]

    if tool_name == "inspect_repo_diff_tool" and parsed.get("status") == "ok":
        digest = str(parsed.get("diff_digest") or "unknown")
        page = parsed.get("page") if isinstance(parsed.get("page"), dict) else {}
        start = page.get("start_line")
        end = page.get("end_line")
        total = parsed.get("total_lines")
        if not all(isinstance(value, int) for value in (start, end, total)):
            return []
        next_line = page.get("next_start_line")
        next_text = f"，下一页 {next_line}" if isinstance(next_line, int) else ""
        files = ", ".join(str(path) for path in parsed.get("changed_files") or () if str(path))
        file_text = f"，文件: {files}" if files else ""
        return [f"Diff {digest} 已读取 {start}-{end}/{total}{next_text}{file_text}"]

    if tool_name == "run_repo_validation_tool":
        digest = str(parsed.get("validated_diff_digest") or "unknown")
        passed = parsed.get("status") == "ok" and parsed.get("all_passed") is True
        required = ", ".join(str(check) for check in parsed.get("required_checks") or () if str(check))
        required_text = f"；必需检查: {required}" if required else ""
        return [f"Diff {digest} 本地验证{'通过' if passed else '失败'}{required_text}"]

    return []


# 压缩阈值（学 Hermes：context window 的 80%）
CONTEXT_WINDOW_RATIO = 0.8
DEFAULT_CONTEXT_WINDOW = 200000


def _reset_compression_state() -> None:
    """Backward-compatible no-op: compression state is task-scoped now."""


def _get_compress_threshold() -> int:
    """压缩阈值：context window 的 80%。"""
    return int(DEFAULT_CONTEXT_WINDOW * CONTEXT_WINDOW_RATIO)


def _pipeline_reconciliation_facts_from_messages(messages: list[dict]) -> list[str]:
    facts = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        try:
            parsed = _json.loads(message.get("content") or "")
        except (TypeError, _json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            facts.extend(_pipeline_reconciliation_fact_lines(parsed))
    return list(dict.fromkeys(facts))[-10:]


async def _llm_summarize_messages(messages: list[dict], previous_summary: str | None = None) -> str:
    """用 LLM 把早期对话历史压缩成结构化摘要。

    学 Hermes _generate_summary：
    - 结构化模板（Goal/Progress/Decisions/Resolved/Pending/Files）
    - 迭代更新（多次压缩时更新之前的摘要）
    - 时间锚定（把"待做"改成"已完成 on 日期"）
    """
    from datetime import date

    # 序列化对话历史
    conversation_text = _summarize_messages(messages)

    # 学 Hermes：结构化模板（简化版，去掉 skill/memory 相关）
    template = """## 当前任务
[用户最近的未完成请求或当前修复目标]

## 已完成动作
[编号列表，每项包含：工具名、目标、结果]
示例：
1. 读取 src/example.py:42 — 发现 request.node_name 不存在 [tool: read_repo_file]
2. 应用补丁 — 修改 src/example.py [tool: apply_repo_patch]
3. 检查 diff — 确认修改正确 [tool: inspect_repo_diff]

## 当前状态
[工作区状态：已修改文件、验证结果、流水线状态]

## 阻塞
[未解决的错误或问题，包含确切错误信息]

## 关键文件
[读取过、修改过的文件，附简要说明]

## 关键上下文
[不能丢失的具体值、错误信息、配置细节]"""

    # 学 Hermes：迭代摘要
    if previous_summary:
        prompt = f"""你在更新一个上下文压缩摘要。之前的摘要如下，新对话发生了，需要合并。

之前摘要：
{previous_summary}

新对话：
{conversation_text}

更新摘要，保留所有仍然相关的信息，添加新的已完成动作（继续编号），更新当前状态。只在信息明显过时时删除。

{template}"""
    else:
        prompt = f"""你在创建一个上下文检查点摘要。把下面的对话压缩成结构化摘要，保留足够细节让后续工作能继续。

对话：
{conversation_text}

{template}"""

    # 学 Hermes：时间锚定
    today = date.today().isoformat()
    prompt += f"\n\n时间锚定：当前日期是 {today}。已完成的动作用过去时描述，不要写成待办。"

    try:
        outcome = await _completion_with_failover(
            {
                "messages": [
                    {"role": "system", "content": "你是对话历史压缩器。只输出结构化摘要，不加前言。用对话的语言输出。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.0,
                "api_key": API_KEY,
                "api_base": BASE_URL,
                "max_tokens": 800,
            },
            requested_model=MODEL_CANDIDATES[0] if MODEL_CANDIDATES else None,
        )
        if outcome.terminal_error or outcome.response is None:
            get_logger().error(f"LLM 摘要失败: {outcome.terminal_error}")
            return ""
        summary = str(outcome.response.choices[0].message.content or "")
        reconciliation_facts = _pipeline_reconciliation_facts_from_messages(messages)
        if reconciliation_facts:
            summary = (
                f"{summary}\n\n## Pipeline 对账事实\n"
                + "\n".join(f"- {fact}" for fact in reconciliation_facts)
            ).strip()
        return summary
    except Exception as e:
        get_logger().error(f"LLM 摘要异常: {e}")
        return ""


def _compression_state(value: dict[str, Any] | None) -> dict[str, Any]:
    raw = value or {}
    return {
        "context_summary": str(raw.get("context_summary") or "")[:20_000],
        "context_summary_covered_messages": max(0, int(raw.get("context_summary_covered_messages") or 0)),
        "context_compression_ineffective_count": max(
            0, int(raw.get("context_compression_ineffective_count") or 0)
        ),
        "context_compression_cooldown_until": max(
            0.0, float(raw.get("context_compression_cooldown_until") or 0.0)
        ),
        "context_compression_last_input_hash": str(
            raw.get("context_compression_last_input_hash") or ""
        )[:64],
    }


def _compression_input_hash(messages: list[dict], end: int) -> str:
    payload = _json.dumps(messages[:end], ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compressed_view(summary: str, messages: list[dict]) -> list[dict]:
    if not summary:
        return _cleanup_orphaned_tool_calls(_truncate_tool_results(messages))
    result = [{"role": "user", "content": f"[之前对话的摘要]\n{summary}"}]
    result.extend(_truncate_tool_results(messages))
    return _cleanup_orphaned_tool_calls(result)


async def compress_messages_if_needed(
    messages: list[dict],
    max_tokens: int = None,
    *,
    compression_state: dict[str, Any] | None = None,
    return_state: bool = False,
    clock: Callable[[], float] = time.time,
) -> list[dict] | CompressionResult:
    """当对话历史超过 token 预算时，压缩早期消息。

    学 Hermes ContextCompressor 的 5 步算法：
    1. 预剪枝（不调 LLM）：去重相同工具返回
    2. 保护头部 + 按 token 预算保护尾部
    3. LLM 结构化摘要中间部分（迭代更新）
    4. 清理孤立 tool_call/tool_result
    5. 防抖 + 失败冷却
    """
    task_state = _compression_state(compression_state)
    threshold = max_tokens or _get_compress_threshold()
    token_count = _estimate_tokens(messages)

    def finish(result_messages: list[dict]) -> list[dict] | CompressionResult:
        result = CompressionResult(result_messages, dict(task_state))
        return result if return_state else result.messages

    # 学 Hermes：防抖——没超阈值就不压缩
    if token_count <= threshold:
        return finish(_truncate_tool_results(messages))

    RECENT_KEEP = 10
    if len(messages) <= RECENT_KEEP:
        return finish(_truncate_tool_results(messages))

    # Phase 1: 预剪枝（学 Hermes，不调 LLM）——去重相同工具返回
    messages = _prune_old_tool_results(messages)

    # Phase 2: 确定边界（学 Hermes：按 token 预算保护尾部）
    head_end = 1  # 保护第一条消息（通常是初始 user 消息）
    tail_start = _find_tail_cut_by_tokens(messages, head_end, token_budget=20000)
    if tail_start <= head_end + 1:
        # 中间没有可压缩的内容
        task_state["context_compression_ineffective_count"] += 1
        return finish(_truncate_tool_results(messages))

    covered = min(task_state["context_summary_covered_messages"], tail_start)
    old_messages = messages[covered:tail_start]
    recent_messages = messages[tail_start:]
    input_hash = _compression_input_hash(messages, tail_start)
    previous_summary = task_state["context_summary"]

    def fallback_view() -> list[dict]:
        rough = _summarize_messages(old_messages)
        rendered = (
            f"{previous_summary}\n\n[新增历史]\n{rough}"
            if previous_summary and rough
            else previous_summary or rough
        )
        return _compressed_view(rendered, recent_messages)

    # 冷却和连续无效只禁止再次调用摘要模型，仍需给当前 LLM 一个有界视图。
    if clock() < task_state["context_compression_cooldown_until"]:
        get_logger().warning("上下文压缩在冷却期内，使用本地有界摘要")
        return finish(fallback_view())
    if task_state["context_compression_ineffective_count"] >= 3:
        get_logger().warning("上下文压缩连续 3 次无效，使用本地有界摘要")
        return finish(fallback_view())

    if not old_messages and previous_summary:
        task_state["context_compression_last_input_hash"] = input_hash
        return finish(_compressed_view(previous_summary, recent_messages))

    # Phase 2: LLM 摘要中间部分（学 Hermes：迭代更新）
    try:
        summary = await _llm_summarize_messages(old_messages, previous_summary or None)
    except Exception as e:
        get_logger().error(f"LLM 摘要异常，降级为粗暴截断: {e}")
        summary = ""
    if summary:
        task_state["context_summary"] = summary
        task_state["context_summary_covered_messages"] = tail_start
        task_state["context_compression_ineffective_count"] = 0
        task_state["context_compression_cooldown_until"] = 0.0
        task_state["context_compression_last_input_hash"] = input_hash
        rendered_summary = summary
    else:
        # 摘要失败时只为本次调用生成粗摘要；不推进持久化游标，恢复后仍可重试。
        get_logger().warning("LLM 摘要失败，降级为粗暴截断")
        rough = _summarize_messages(old_messages)
        rendered_summary = f"{previous_summary}\n\n[新增历史]\n{rough}" if previous_summary else rough
        task_state["context_compression_cooldown_until"] = clock() + 60

    result = _compressed_view(rendered_summary, recent_messages)

    new_token_count = _estimate_tokens(result)
    get_logger().info(
        f"上下文压缩: {len(old_messages)} 条中间消息 → 1 条摘要 "
        f"(估算 {token_count} → {new_token_count} tokens)"
    )

    # 学 Hermes：检查压缩是否有效
    if new_token_count >= token_count * 0.9:
        task_state["context_compression_ineffective_count"] += 1
        get_logger().warning(
            f"压缩效果不佳（仅减少 {100 - int(new_token_count / token_count * 100)}%），"
            f"防抖计数={task_state['context_compression_ineffective_count']}"
        )

    return finish(result)


def _estimate_tokens(messages: list[dict]) -> int:
    """估算 token 数。优先用 litellm token_counter，降级用字符数。

    学 Hermes estimate_messages_tokens_rough：用真正的 tokenizer 更准确。
    """
    try:
        model = MODEL_CANDIDATES[0] if MODEL_CANDIDATES else "gpt-4"
        return litellm.token_counter(model=model, messages=messages)
    except Exception:
        # 降级：字符数估算（1 字符 ≈ 1 token）
        total = 0
        for msg in messages:
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total += len(str(part.get("text", "")))
        return total


def _truncate_tool_results(messages: list[dict], max_chars: int = 3000) -> list[dict]:
    """截断工具返回中的长文本，按工具类型分级阈值。

    代码文件（read_repo_file_tool）和生成结果（generate_code_tool）不截断——
    丢了内容模型就修不了。日志（fetch_pipeline_logs_tool）保持压缩。
    """
    # 先建 tool_call_id → tool_name 映射
    tool_name_map = {}
    for msg in messages:
        if isinstance(msg, dict):
            for tc in (msg.get("tool_calls") or []):
                call_id = tc.get("id", "")
                name = tc.get("function", {}).get("name", "")
                if call_id and name:
                    tool_name_map[call_id] = name

    # 按工具名分级阈值
    TOOL_THRESHOLDS = {
        # Hermes 路径
        "read_repo_file_tool": 15000,       # 代码文件不截断
        "generate_code_tool": 15000,        # 生成结果不截断
        "fetch_coverage_report_tool": 5000, # 覆盖率报告多留点
        # native 路径
        "apply_repo_patch_tool": 8000,       # changed_files + diff_check 是关键
        "inspect_repo_diff_tool": 10000,     # diff 是关键证据，多留
        "search_repo_tool": 4000,           # 搜索结果可以截断
        "run_repo_validation_tool": 3000,    # 验证结果简短
        "fetch_pipeline_logs_tool": 5000,   # 保留失败集、Job 状态和跨 SHA 对账
        "wait_pipeline_tool": 5000,         # 与 fetch 使用相同的 Pipeline 证据预算
    }

    result = []
    for msg in messages:
        if not isinstance(msg, dict):
            result.append(msg)
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) <= max_chars:
            result.append(msg)
            continue

        # 查这条 tool 消息是哪个工具返回的
        call_id = msg.get("tool_call_id", "")
        tool_name = tool_name_map.get(call_id, "")
        threshold = TOOL_THRESHOLDS.get(tool_name, max_chars)

        if len(content) <= threshold:
            result.append(msg)  # 在该工具的阈值内，不截断
            continue

        # 超过阈值，按工具类型选择压缩策略
        compacted = (
            _compact_native_safety_result(tool_name, content, threshold)
            or _compact_pipeline_result(content, threshold)
            or _compact_json_result(content, threshold)
        )
        msg = {
            **msg,
            "content": compacted if compacted is not None else content[:threshold] + "\n...(已截断)",
        }
        result.append(msg)
    return result


def _compact_native_safety_result(tool_name: str, content: str, max_chars: int) -> str | None:
    """Compact Native results without dropping identity, page, or validation evidence."""
    if tool_name not in {
        "apply_repo_patch_tool",
        "inspect_repo_diff_tool",
        "run_repo_validation_tool",
    }:
        return None
    try:
        parsed = _json.loads(content)
    except (TypeError, _json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None

    if tool_name == "apply_repo_patch_tool":
        keys = (
            "status",
            "error_code",
            "patch_applied",
            "base_sha",
            "diff_digest",
            "changed_files",
            "diff_check",
        )
        compacted = {key: parsed[key] for key in keys if key in parsed}
        return _json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))

    if tool_name == "inspect_repo_diff_tool":
        compacted = dict(parsed)
        compacted.pop("diff", None)
        compacted["diff_body_compacted"] = True
        return _json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))

    executed_source = parsed.get("executed_checks")
    if not isinstance(executed_source, list):
        executed_source = parsed.get("results") if isinstance(parsed.get("results"), list) else []
    executed = []
    for source in executed_source:
        if not isinstance(source, dict):
            continue
        item = {
            key: source[key]
            for key in (
                "name",
                "check",
                "passed",
                "exit_code",
                "timed_out",
                "output_truncated",
                "error",
            )
            if key in source
        }
        if isinstance(source.get("output"), str):
            item["output"] = source["output"][:500]
        executed.append(item)
    keys = (
        "status",
        "error_code",
        "all_passed",
        "base_sha",
        "validated_diff_digest",
        "required_checks",
    )
    compacted = {key: parsed[key] for key in keys if key in parsed}
    compacted["executed_checks"] = executed
    compacted["validation_output_compacted"] = True
    serialized = _json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= max_chars:
        return serialized

    for item in executed:
        item.pop("output", None)
        if isinstance(item.get("error"), str):
            item["error"] = item["error"][:200]
    return _json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))


def _compact_json_result(content: str, max_chars: int) -> str | None:
    try:
        parsed = _json.loads(content)
    except (TypeError, _json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None

    compacted = dict(parsed)
    compacted.pop("raw_html_compact", None)
    files = compacted.get("files")
    if isinstance(files, list):
        compacted["files"] = [
            {
                "path": file.get("path"),
                "uncovered": file.get("uncovered", [])[:10],
            }
            for file in files[:20]
            if isinstance(file, dict)
        ]
    for key in ("diagnostic", "report_text", "message", "reason"):
        if isinstance(compacted.get(key), str):
            compacted[key] = compacted[key][:1600]

    while True:
        serialized = _json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= max_chars:
            return serialized
        string_fields = [
            (key, value) for key, value in compacted.items()
            if isinstance(value, str) and len(value) > 80
        ]
        if not string_fields:
            break
        key, value = max(string_fields, key=lambda item: len(item[1]))
        compacted[key] = value[:max(80, len(value) - (len(serialized) - max_chars) - 30)]

    essential_keys = {
        "status", "job_name", "job_id", "pipeline_id", "changed_files",
        "available", "diagnostic", "message", "reason",
    }
    essential = {key: value for key, value in compacted.items() if key in essential_keys}
    serialized = _json.dumps(essential, ensure_ascii=False, separators=(",", ":"))
    return serialized if len(serialized) <= max_chars else None


_PIPELINE_IDENTITY_KEYS = (
    "status",
    "requested_commit_sha",
    "matched_commit_sha",
    "pipeline_id",
    "root_pipeline_id",
    "validation_pipeline_id",
    "pipeline_status",
    "coverage",
    "coverage_source",
    "coverage_status",
    "attempt_id",
)

_MAX_PIPELINE_RECONCILIATION_ITEMS = 20
_MAX_PIPELINE_OBSERVED_JOBS = 100


def _bounded_text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _compact_observed_jobs(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    jobs = []
    for source in value[:_MAX_PIPELINE_OBSERVED_JOBS]:
        if not isinstance(source, dict):
            continue
        jobs.append({
            "pipeline_id": source.get("pipeline_id"),
            "job_id": source.get("job_id"),
            "name": _bounded_text(source.get("name") or source.get("job_name"), 300),
            "status": _bounded_text(source.get("status"), 40).lower(),
        })
    return jobs


def _compact_failure_reconciliation(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keys = (
        "previous_pipeline_id",
        "previous_requested_commit_sha",
        "previous_matched_commit_sha",
        "current_pipeline_id",
        "current_requested_commit_sha",
        "current_matched_commit_sha",
        "transitions_truncated",
        "current_observed_jobs_truncated",
    )
    result = {key: value[key] for key in keys if key in value}
    transitions = []
    for source in (value.get("transitions") or [])[:_MAX_PIPELINE_RECONCILIATION_ITEMS]:
        if not isinstance(source, dict):
            continue
        transitions.append({
            "root_cause_id": _bounded_text(source.get("root_cause_id"), 80),
            "status": _bounded_text(source.get("status"), 40).lower(),
            "previous_job_names": [
                _bounded_text(name, 300)
                for name in (source.get("previous_job_names") or [])[:_MAX_PIPELINE_RECONCILIATION_ITEMS]
                if _bounded_text(name, 300)
            ],
            "current_job_names": [
                _bounded_text(name, 300)
                for name in (source.get("current_job_names") or [])[:_MAX_PIPELINE_RECONCILIATION_ITEMS]
                if _bounded_text(name, 300)
            ],
        })
    result["transitions"] = transitions
    return result


def _pipeline_reconciliation_fact_lines(parsed: dict[str, Any]) -> list[str]:
    observed_jobs = _compact_observed_jobs(parsed.get("observed_jobs"))
    reconciliation = _compact_failure_reconciliation(parsed.get("failure_reconciliation"))
    facts = []
    if observed_jobs:
        facts.append("Pipeline Job 状态: " + ", ".join(
            f"{job['name']}={job['status']}"
            for job in observed_jobs[:_MAX_PIPELINE_RECONCILIATION_ITEMS]
        ))
    transitions = (reconciliation or {}).get("transitions") or []
    if transitions:
        facts.append("Pipeline 根因变化: " + ", ".join(
            f"{transition['root_cause_id']}={transition['status']}" for transition in transitions
        ))
    return facts


def _minimal_pipeline_reconciliation_result(compacted: dict[str, Any], max_chars: int) -> str:
    """Keep reconciliation machine facts when the full Pipeline evidence cannot fit."""
    minimal = {key: compacted[key] for key in _PIPELINE_IDENTITY_KEYS if key in compacted}
    observed = _compact_observed_jobs(compacted.get("observed_jobs"))
    source_observed_count = len(compacted.get("observed_jobs") or ())
    source_observed_truncated = bool(compacted.get("observed_jobs_truncated"))
    reconciliation = _compact_failure_reconciliation(compacted.get("failure_reconciliation"))
    if reconciliation is not None:
        source_transitions = reconciliation.get("transitions") or []
        stripped_associations = any(
            transition.get("previous_job_names") or transition.get("current_job_names")
            for transition in source_transitions
        )
        reconciliation["transitions"] = [{
            "root_cause_id": transition.get("root_cause_id", ""),
            "status": transition.get("status", ""),
        } for transition in source_transitions]
        reconciliation["transitions_truncated"] = bool(
            reconciliation.get("transitions_truncated") or stripped_associations
        )
        minimal["failure_reconciliation"] = reconciliation
    minimal["observed_jobs"] = observed
    minimal["observed_jobs_truncated"] = source_observed_truncated or source_observed_count > len(observed)

    def serialized() -> str:
        return _json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))

    # Preserve root transition identities before Job observations: transition facts
    # are the only direct record of resolved/persistent/introduced outcomes.
    while len(serialized()) > max_chars and observed:
        observed.pop()
        minimal["observed_jobs_truncated"] = True
    transitions = (reconciliation or {}).get("transitions") or []
    while len(serialized()) > max_chars and transitions:
        transitions.pop()
        reconciliation["transitions_truncated"] = True
    return serialized()


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _compact_pipeline_result(content: str, max_chars: int) -> str | None:
    try:
        parsed = _json.loads(content)
    except (TypeError, _json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("failed_jobs"), list):
        return None

    from ut_agent.repair_progress import extract_causal_lines

    compacted = {key: parsed[key] for key in _PIPELINE_IDENTITY_KEYS if key in parsed}
    compacted["message"] = str(parsed.get("message") or "")[:200]
    if "observed_jobs" in parsed:
        compacted["observed_jobs"] = _compact_observed_jobs(parsed.get("observed_jobs"))
        compacted["observed_jobs_truncated"] = bool(parsed.get("observed_jobs_truncated")) or (
            len(parsed.get("observed_jobs") or ()) > len(compacted["observed_jobs"])
        )
    if "failure_reconciliation" in parsed:
        reconciliation = _compact_failure_reconciliation(parsed.get("failure_reconciliation"))
        if reconciliation is not None:
            compacted["failure_reconciliation"] = reconciliation
    jobs = []
    for source in parsed["failed_jobs"]:
        if not isinstance(source, dict):
            continue
        log_context = str(source.get("log_context") or source.get("log_tail") or "")
        causal_lines = [str(line) for line in (source.get("causal_lines") or []) if str(line).strip()]
        if not causal_lines:
            causal_lines = extract_causal_lines(log_context)
        job = {
            key: source[key]
            for key in ("job_id", "pipeline_id", "name", "status", "is_target")
            if key in source
        }
        job["causal_lines"] = [line[:1000] for line in causal_lines[:3]]
        raw_candidates = source.get("diagnostic_candidates") or ()
        candidates = []
        candidate_values_seen = 0
        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, dict):
                continue
            candidate_values_seen += 1
            if len(candidates) >= 12:
                continue
            candidates.append({
                "candidate_id": str(raw_candidate.get("candidate_id") or "")[:40],
                "line_number": _nonnegative_int(raw_candidate.get("line_number")),
                "signal": str(raw_candidate.get("signal") or "")[:24],
                "text": str(raw_candidate.get("text") or "")[:1000],
                "diagnostic_identity": str(raw_candidate.get("diagnostic_identity") or "")[:40],
            })
        if candidates or any(
            key in source
            for key in (
                "diagnostic_candidates",
                "diagnostic_candidate_count",
                "diagnostic_candidates_truncated",
            )
        ):
            job["diagnostic_candidates"] = candidates
            job["diagnostic_candidate_count"] = max(
                len(candidates),
                _nonnegative_int(source.get("diagnostic_candidate_count")),
            )
            job["diagnostic_candidates_truncated"] = bool(
                source.get("diagnostic_candidates_truncated")
            ) or candidate_values_seen > len(candidates)
            retained_identity_count = len({
                candidate["diagnostic_identity"]
                for candidate in candidates
                if candidate["diagnostic_identity"]
            })
            identity_count = max(
                retained_identity_count,
                _nonnegative_int(source.get("diagnostic_identity_count")),
            )
            job["diagnostic_identity_count"] = identity_count
            job["omitted_diagnostic_identity_count"] = max(
                _nonnegative_int(source.get("omitted_diagnostic_identity_count")),
                identity_count - retained_identity_count,
            )
        if causal_lines:
            job["log_tail"] = "\n".join(causal_lines)[:1000]
        jobs.append(job)
    compacted["failed_jobs"] = jobs

    work_items = []
    for source in parsed.get("work_items") or []:
        if not isinstance(source, dict):
            continue
        work_items.append({
            key: source[key]
            for key in (
                "job_id",
                "pipeline_id",
                "job_name",
                "kind",
                "required_tool",
                "root_cause_id",
                "canonical_job_name",
            )
            if key in source
        })
    if work_items:
        compacted["work_items"] = work_items

    root_cause_groups = []
    for source in parsed.get("root_cause_groups") or []:
        if not isinstance(source, dict):
            continue
        root_cause_groups.append({
            "root_cause_id": source.get("root_cause_id"),
            "canonical_job_name": source.get("canonical_job_name"),
            "job_names": list(source.get("job_names") or []),
            "canonical_diagnostic": str(source.get("canonical_diagnostic") or "")[:1000],
        })
    if root_cause_groups:
        compacted["root_cause_groups"] = root_cause_groups

    serialized = _json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > max_chars:
        compacted["message"] = ""
        for job in jobs:
            job["causal_lines"] = [line[:400] for line in job["causal_lines"][:1]]
            for candidate in job.get("diagnostic_candidates") or ():
                candidate["text"] = str(candidate.get("text") or "")[:400]
            if job.get("log_tail"):
                job["log_tail"] = job["causal_lines"][0] if job["causal_lines"] else ""
        for group in root_cause_groups:
            group["canonical_diagnostic"] = group["canonical_diagnostic"][:400]
        serialized = _json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= max_chars:
        return serialized

    # A pipeline result has a fixed evidence schema. Never fall through to generic JSON
    # compaction, which can silently discard every compiler diagnostic.
    compacted.pop("message", None)
    compacted.pop("work_items", None)
    for job in jobs:
        job.pop("log_tail", None)
    while True:
        serialized = _json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= max_chars:
            return serialized
        shrinkable = []
        for job in jobs:
            for index, line in enumerate(job.get("causal_lines") or []):
                if len(line) > 80:
                    shrinkable.append((job["causal_lines"], index, line))
            for candidate in job.get("diagnostic_candidates") or ():
                text = str(candidate.get("text") or "")
                if len(text) > 80:
                    shrinkable.append((candidate, "text", text))
        for group in root_cause_groups:
            diagnostic = group.get("canonical_diagnostic") or ""
            if len(diagnostic) > 80:
                shrinkable.append((group, "canonical_diagnostic", diagnostic))
        if not shrinkable:
            reducible = [job for job in jobs if len(job.get("diagnostic_candidates") or ()) > 2]
            if not reducible:
                return _minimal_pipeline_reconciliation_result(compacted, max_chars)
            target = max(reducible, key=lambda item: len(item["diagnostic_candidates"]))
            candidates = target["diagnostic_candidates"]
            candidates.pop(len(candidates) // 2)
            target["diagnostic_candidates_truncated"] = True
            retained_identity_count = len({
                candidate.get("diagnostic_identity")
                for candidate in candidates
                if candidate.get("diagnostic_identity")
            })
            target["omitted_diagnostic_identity_count"] = max(
                _nonnegative_int(target.get("omitted_diagnostic_identity_count")),
                _nonnegative_int(target.get("diagnostic_identity_count")) - retained_identity_count,
            )
            continue
        container, key, value = max(shrinkable, key=lambda item: len(item[2]))
        container[key] = value[:max(80, len(value) - (len(serialized) - max_chars) - 20)]


def _summarize_messages(messages: list[dict]) -> str:
    """把早期对话历史压缩成一段文本摘要。"""
    lines = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")

        if tool_calls:
            for tc in tool_calls:
                fn_name = (tc.get("function", {}).get("name", "")) if isinstance(tc, dict) else ""
                lines.append(f"[{role}] 调用工具: {fn_name}")
        elif content and isinstance(content, str):
            # Preserve the existing coarse content preview, then append deterministic
            # reconciliation facts that may appear beyond the first 200 characters.
            lines.append(f"[{role}] {content[:200]}")
            try:
                parsed = _json.loads(content)
            except (TypeError, _json.JSONDecodeError):
                parsed = None
            reconciliation_facts = (
                _pipeline_reconciliation_fact_lines(parsed) if isinstance(parsed, dict) else []
            )
            if reconciliation_facts:
                lines.extend(f"[{role}] {fact}" for fact in reconciliation_facts)
    return "\n".join(lines[-50:])  # 最多保留 50 行摘要


def _prune_old_tool_results(messages: list[dict], protect_tail_count: int = 10) -> list[dict]:
    """预剪枝：修剪旧工具返回，去重相同文件读取。不调 LLM。

    学 Hermes _prune_old_tool_results 的去重逻辑：
    同一个文件被读多次时，只保留最新完整副本，旧的替换为回引。
    从后往前遍历——先看到的是最新的，保留；后续遇到相同内容的是旧的，替换。
    """
    import hashlib

    content_hashes: set = set()
    # 从后往前遍历，先看到的是最新的
    result = list(messages)
    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        content = msg.get("content", "") or ""
        if len(content) < 100:  # 太短的不去重
            continue
        content_hash = hashlib.md5(content.encode()).hexdigest()
        if content_hash in content_hashes:
            # 旧副本替换为回引
            result[i] = {**msg, "content": "[同上，见后续完整读取]"}
        else:
            content_hashes.add(content_hash)
    return result


def _find_tail_cut_by_tokens(messages: list[dict], head_end: int, token_budget: int = 20000) -> int:
    """从后往前累积 token，超过预算就停。返回尾部起始索引。

    学 Hermes _find_tail_cut_by_tokens：
    - 按 token 预算保护尾部，不是固定 N 条
    - 允许 1.5x 超出（soft_ceiling）
    - 至少保护 3 条消息（min_tail_floor）
    """
    n = len(messages)
    min_tail = max(3, min(10, n - head_end - 1))
    soft_ceiling = int(token_budget * 1.5)
    accumulated = 0
    cut_idx = n

    for i in range(n - 1, head_end - 1, -1):
        msg = messages[i]
        msg_tokens = _estimate_tokens([msg]) if isinstance(msg, dict) else len(str(msg))
        if accumulated + msg_tokens > soft_ceiling and (n - i) >= min_tail:
            break
        accumulated += msg_tokens
        cut_idx = i

    # 确保至少保护 min_tail 条
    cut_idx = min(cut_idx, n - min_tail)
    return max(cut_idx, head_end + 1)


def _cleanup_orphaned_tool_calls(messages: list[dict]) -> list[dict]:
    """清理孤立的 tool_call/tool_result 对。

    学 Hermes：压缩后可能产生不匹配的 tool_call_id
    （assistant 消息有 tool_calls 但对应的 tool 结果被压缩掉了）。
    """
    # 收集所有 tool_call_id
    valid_call_ids = set()
    for msg in messages:
        if isinstance(msg, dict):
            for tc in (msg.get("tool_calls") or []):
                if isinstance(tc, dict):
                    valid_call_ids.add(tc.get("id", ""))

    # 收集所有 tool_result 的 tool_call_id
    valid_result_ids = set()
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            valid_result_ids.add(msg.get("tool_call_id", ""))

    # 删除没有对应 result 的 tool_calls，和没有对应 call 的 result
    result = []
    for msg in messages:
        if not isinstance(msg, dict):
            result.append(msg)
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            kept_calls = [tc for tc in msg["tool_calls"]
                          if isinstance(tc, dict) and tc.get("id", "") in valid_result_ids]
            if kept_calls:
                result.append({**msg, "tool_calls": kept_calls})
            else:
                # 所有 tool_calls 都没有 result，去掉 tool_calls 只保留 content
                result.append({**msg, "tool_calls": None})
        elif msg.get("role") == "tool":
            if msg.get("tool_call_id", "") in valid_call_ids:
                result.append(msg)
            # else: 孤立的 tool result，丢弃
        else:
            result.append(msg)
    return result
