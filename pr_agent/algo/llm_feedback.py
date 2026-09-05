from __future__ import annotations

from typing import Any

from pr_agent.algo.model_resilience import ModelFailureKind, classify_model_failure, sanitize_model_error
from pr_agent.config_loader import get_settings


LLM_FEEDBACK_KEY = "llm_feedback"


def record_llm_feedback(error: Exception, context: str = "LLM inference") -> None:
    data = _settings_data()
    feedback_items = list(data.get(LLM_FEEDBACK_KEY, []))
    item = {
        "context": context,
        "type": _classify_llm_error(error),
        "message": _sanitize_error_message(error),
    }
    if item not in feedback_items:
        feedback_items.append(item)
    data[LLM_FEEDBACK_KEY] = feedback_items
    get_settings().data = data


def get_llm_feedback() -> list[dict[str, str]]:
    return list(_settings_data().get(LLM_FEEDBACK_KEY, []))


def format_llm_feedback_markdown(feedback_items: list[dict[str, str]]) -> str:
    unique_items = []
    seen = set()
    for item in feedback_items:
        key = (item.get("context", ""), item.get("type", ""), item.get("message", ""))
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(item)

    if not unique_items:
        return ""

    is_zh = str(get_settings().config.get("response_language", "en-US")).lower().startswith("zh")
    if is_zh:
        lines = ["## LLM 调用状态提示 ⚠️", ""]
        for item in unique_items:
            lines.append(f"- **{item.get('context', 'LLM')}**：{_zh_feedback_text(item)}")
        lines.append("")
        lines.append("部分结果可能未完整生成。建议稍后重试，或降低并发/增大中转商限额后重新触发。")
    else:
        lines = ["## LLM Call Status ⚠️", ""]
        for item in unique_items:
            lines.append(f"- **{item.get('context', 'LLM')}**: {_en_feedback_text(item)}")
        lines.append("")
        lines.append("Some results may be incomplete. Try again later, or reduce concurrency/increase the gateway quota.")
    return "\n".join(lines)


def _settings_data() -> dict[str, Any]:
    data = getattr(get_settings(), "data", {})
    return data if isinstance(data, dict) else {}


def _classify_llm_error(error: Exception) -> str:
    kind = classify_model_failure(error)
    if kind is ModelFailureKind.RATE_LIMIT:
        return "rate_limit"
    if kind is ModelFailureKind.TIMEOUT:
        return "timeout"
    return "provider_error"


def _sanitize_error_message(error: Exception) -> str:
    return sanitize_model_error(error)


def _zh_feedback_text(item: dict[str, str]) -> str:
    feedback_type = item.get("type")
    if feedback_type == "rate_limit":
        return "LLM 中转商/供应商返回限流或容量不足，当前步骤可能没有完整结果。"
    if feedback_type == "timeout":
        return "LLM 调用超时，当前步骤可能没有完整结果。"
    return "LLM 中转商/供应商调用失败，当前步骤可能没有完整结果。"


def _en_feedback_text(item: dict[str, str]) -> str:
    feedback_type = item.get("type")
    if feedback_type == "rate_limit":
        return "The LLM gateway/provider returned a rate limit or capacity error, so this step may be incomplete."
    if feedback_type == "timeout":
        return "The LLM call timed out, so this step may be incomplete."
    return "The LLM gateway/provider call failed, so this step may be incomplete."
