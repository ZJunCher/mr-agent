"""Task 1: native 工具截断阈值 + 事实提取 + 去重测试。

验证：
1. native 工具结果按各自阈值截断，不过度截断关键证据
2. extract_known_facts 识别 native 工具返回
3. 相同文件读取去重（学 Hermes _prune_old_tool_results）
"""
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing.
from ut_agent.llm import (
    _compact_native_safety_result,
    _truncate_tool_results,
    extract_known_facts,
    _prune_old_tool_results,
)

BASE_SHA = "a" * 40
DIFF_DIGEST = "sha256:" + "b" * 64


def _ai_msg(tool_calls: list[dict]) -> AIMessage:
    return AIMessage(content="", tool_calls=tool_calls)


def _tool_msg(call_id: str, content: dict) -> ToolMessage:
    return ToolMessage(content=json.dumps(content, ensure_ascii=False), tool_call_id=call_id)


class TestNativeToolTruncation:
    """native 工具结果按各自阈值截断。"""

    def test_apply_repo_patch_not_over_truncated(self):
        """apply_repo_patch_tool 的 changed_files + diff_check 是关键，不应截到 3000。"""
        # 构造一个 5000 字符的 apply_repo_patch 结果
        big_result = {
            "status": "changed",
            "changed_files": ["src/example.py"],
            "diff_check": {"passed": True, "message": ""},
            "reason": "x" * 4000,  # 超过默认 3000 阈值
        }
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "p1", "function": {"name": "apply_repo_patch_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "p1", "content": json.dumps(big_result, ensure_ascii=False)},
        ]
        result = _truncate_tool_results(messages)
        content = result[1]["content"]
        # apply_repo_patch_tool 阈值是 8000，不应被截断
        assert len(content) <= 8000
        assert "changed" in content

    def test_inspect_repo_diff_not_over_truncated(self):
        """inspect_repo_diff_tool 的 diff 是关键证据，阈值 10000。"""
        big_diff = "x" * 8000  # 超过默认 3000，但低于 10000
        big_result = {
            "status": "ok",
            "changed_files": ["src/example.py"],
            "diff_stat": "1 file changed",
            "diff": big_diff,
            "truncated": False,
        }
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "i1", "function": {"name": "inspect_repo_diff_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "i1", "content": json.dumps(big_result, ensure_ascii=False)},
        ]
        result = _truncate_tool_results(messages)
        content = result[1]["content"]
        # inspect_repo_diff_tool 阈值是 10000，8000 字符不应被截断
        assert "diff" in content
        assert len(content) > 3000  # 没被截到默认 3000

    def test_search_repo_truncated_at_threshold(self):
        """search_repo_tool 搜索结果可以截断，阈值 4000。"""
        big_result = {
            "status": "ok",
            "matches": [{"path": f"file_{i}.py", "line": i, "line_content": "x" * 100} for i in range(100)],
            "count": 100,
            "truncated": True,
        }
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "s1", "function": {"name": "search_repo_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "s1", "content": json.dumps(big_result, ensure_ascii=False)},
        ]
        result = _truncate_tool_results(messages)
        content = result[1]["content"]
        # search_repo_tool 阈值是 4000
        assert len(content) <= 4000

    def test_inspection_compaction_preserves_manifest_and_removes_diff_body(self):
        compacted = _compact_native_safety_result(
            "inspect_repo_diff_tool",
            json.dumps({
                "status": "ok",
                "base_sha": BASE_SHA,
                "diff_digest": DIFF_DIGEST,
                "total_lines": 900,
                "changed_files": ["src/example.py"],
                "page": {
                    "start_line": 1,
                    "end_line": 600,
                    "has_more": True,
                    "next_start_line": 601,
                },
                "diff": "x" * 20000,
            }),
            1000,
        )

        parsed = json.loads(compacted)
        assert parsed["base_sha"] == BASE_SHA
        assert parsed["diff_digest"] == DIFF_DIGEST
        assert parsed["changed_files"] == ["src/example.py"]
        assert parsed["page"]["next_start_line"] == 601
        assert parsed["diff_body_compacted"] is True
        assert "diff" not in parsed

    def test_patch_compaction_preserves_patch_identity(self):
        compacted = _compact_native_safety_result(
            "apply_repo_patch_tool",
            json.dumps({
                "status": "changed",
                "patch_applied": True,
                "base_sha": BASE_SHA,
                "diff_digest": DIFF_DIGEST,
                "changed_files": ["src/example.py"],
                "diff_check": {"passed": True, "message": ""},
                "reason": "x" * 20000,
            }),
            1000,
        )

        parsed = json.loads(compacted)
        assert parsed["patch_applied"] is True
        assert parsed["diff_digest"] == DIFF_DIGEST
        assert parsed["diff_check"]["passed"] is True
        assert "reason" not in parsed

    def test_validation_compaction_preserves_verdicts_and_bounds_output(self):
        compacted = _compact_native_safety_result(
            "run_repo_validation_tool",
            json.dumps({
                "status": "ok",
                "all_passed": False,
                "base_sha": BASE_SHA,
                "validated_diff_digest": DIFF_DIGEST,
                "required_checks": ["diff_check", "unit_test_check"],
                "executed_checks": [{
                    "name": "unit_test_check",
                    "check": "unit_test_check",
                    "passed": False,
                    "exit_code": 1,
                    "timed_out": True,
                    "output_truncated": True,
                    "output": "x" * 20000,
                }],
            }),
            1000,
        )

        parsed = json.loads(compacted)
        result = parsed["executed_checks"][0]
        assert parsed["validated_diff_digest"] == DIFF_DIGEST
        assert parsed["required_checks"] == ["diff_check", "unit_test_check"]
        assert result["passed"] is False
        assert result["timed_out"] is True
        assert result["output_truncated"] is True
        assert len(result.get("output", "")) <= 500

    def test_truncation_uses_native_compactor_before_generic_json(self):
        content = json.dumps({
            "status": "ok",
            "base_sha": BASE_SHA,
            "diff_digest": DIFF_DIGEST,
            "total_lines": 900,
            "changed_files": ["src/example.py"],
            "page": {"start_line": 1, "end_line": 600, "has_more": True, "next_start_line": 601},
            "diff": "x" * 20000,
        })
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "i1", "function": {"name": "inspect_repo_diff_tool", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "i1", "content": content},
        ]

        parsed = json.loads(_truncate_tool_results(messages)[1]["content"])

        assert parsed["diff_digest"] == DIFF_DIGEST
        assert parsed["page"]["next_start_line"] == 601
        assert parsed["diff_body_compacted"] is True


class TestExtractKnownFactsNative:
    """extract_known_facts 识别 native 工具返回。"""

    def test_extracts_apply_repo_patch_facts(self):
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "p1", "function": {"name": "apply_repo_patch_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "p1", "content": json.dumps({
                "status": "changed",
                "changed_files": ["src/example.py", "src/test.py"],
            })},
        ]
        facts = extract_known_facts(messages)
        assert "src/example.py" in facts
        assert "已应用补丁" in facts or "changed" in facts.lower()

    def test_extracts_search_repo_facts(self):
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "s1", "function": {"name": "search_repo_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "s1", "content": json.dumps({
                "status": "ok",
                "count": 5,
                "matches": [],
            })},
        ]
        facts = extract_known_facts(messages)
        assert "5" in facts or "搜索" in facts

    def test_extracts_validation_facts(self):
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "v1", "function": {"name": "run_repo_validation_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "v1", "content": json.dumps({
                "status": "ok",
                "all_passed": True,
                "results": [],
            })},
        ]
        facts = extract_known_facts(messages)
        assert "验证" in facts or "通过" in facts

    def test_extracts_lossless_diff_page_fact(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "i1", "function": {"name": "inspect_repo_diff_tool", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "i1", "content": json.dumps({
                "status": "ok",
                "base_sha": BASE_SHA,
                "diff_digest": DIFF_DIGEST,
                "total_lines": 900,
                "changed_files": ["src/example.py"],
                "page": {"start_line": 1, "end_line": 600, "has_more": True, "next_start_line": 601},
                "diff": "x" * 20000,
            })},
        ]

        facts = extract_known_facts(messages)

        assert DIFF_DIGEST in facts
        assert "1-600/900" in facts
        assert "下一页 601" in facts
        assert "src/example.py" in facts

    def test_extracts_lossless_validation_fact(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "v1", "function": {"name": "run_repo_validation_tool", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "v1", "content": json.dumps({
                "status": "ok",
                "all_passed": True,
                "validated_diff_digest": DIFF_DIGEST,
                "required_checks": ["diff_check", "python_compile_check", "unit_test_check"],
                "executed_checks": [],
            })},
        ]

        facts = extract_known_facts(messages)

        assert DIFF_DIGEST in facts
        assert "本地验证通过" in facts
        assert "diff_check, python_compile_check, unit_test_check" in facts


class TestPruneOldToolResults:
    """相同文件读取去重（学 Hermes _prune_old_tool_results）。"""

    def test_deduplicates_identical_tool_results(self):
        """同一个文件被读多次，只保留最新完整副本。"""
        same_content = "def foo():\n    return 1\n" * 100  # 足够长
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "r1", "function": {"name": "read_repo_file_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "r1", "content": same_content},
            {"role": "assistant", "tool_calls": [{"id": "r2", "function": {"name": "read_repo_file_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "r2", "content": same_content},
        ]
        result = _prune_old_tool_results(messages)
        # 第一份应该被替换为回引
        assert "同上" in result[1]["content"] or len(result[1]["content"]) < len(same_content)
        # 第二份保持完整
        assert result[3]["content"] == same_content

    def test_does_not_deduplicate_short_results(self):
        """短结果不去重。"""
        short_content = "ok"
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "r1", "function": {"name": "some_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "r1", "content": short_content},
            {"role": "assistant", "tool_calls": [{"id": "r2", "function": {"name": "some_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "r2", "content": short_content},
        ]
        result = _prune_old_tool_results(messages)
        # 短结果不去重
        assert result[1]["content"] == short_content
        assert result[3]["content"] == short_content


# ── Task 2: LLM 结构化摘要 + 迭代更新 ──

from unittest.mock import AsyncMock, patch
from ut_agent.llm import CompressionResult, compress_messages_if_needed, _reset_compression_state


class TestLLMSummary:
    """LLM 结构化摘要（学 Hermes _generate_summary）。"""

    @pytest.mark.asyncio
    async def test_compress_uses_llm_summary_when_over_threshold(self):
        """超过阈值时用 LLM 生成摘要，不用粗暴截断。"""
        _reset_compression_state()
        # 构造超过阈值的对话（大量消息）
        messages = []
        for i in range(30):
            messages.append({"role": "assistant", "content": f"思考第 {i} 轮" + "x" * 2000})
            messages.append({"role": "user", "content": f"继续 {i}" + "y" * 2000})

        # mock LLM 返回结构化摘要
        mock_summary = "## 当前任务\n修复编译错误\n## 已完成动作\n1. 读取了文件\n## 关键文件\nsrc/example.py"
        with patch("ut_agent.llm._llm_summarize_messages", new_callable=AsyncMock, return_value=mock_summary):
            with patch("ut_agent.llm._estimate_tokens", return_value=100000):
                with patch("ut_agent.llm._get_compress_threshold", return_value=50000):
                    result = await compress_messages_if_needed(messages)

        # 结果应该包含摘要
        assert any("[之前对话的摘要]" in str(msg.get("content", "")) for msg in result)
        # 摘要内容应该是 LLM 生成的
        assert any("修复编译错误" in str(msg.get("content", "")) for msg in result)

    @pytest.mark.asyncio
    async def test_no_compress_when_under_threshold(self):
        """未超阈值时不压缩，不调 LLM。"""
        _reset_compression_state()
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        with patch("ut_agent.llm._llm_summarize_messages", new_callable=AsyncMock) as mock_llm:
            with patch("ut_agent.llm._estimate_tokens", return_value=100):
                result = await compress_messages_if_needed(messages)
                # 不应该调 LLM
                mock_llm.assert_not_called()
        # 原样返回（可能经过截断但不应压缩）
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_iterative_summary_updates_previous(self):
        """多次压缩时迭代更新之前的摘要（学 Hermes）。"""
        _reset_compression_state()
        messages = []
        for i in range(30):
            messages.append({"role": "assistant", "content": f"第 {i} 轮" + "x" * 2000})
            messages.append({"role": "user", "content": f"继续 {i}" + "y" * 2000})

        first_summary = "## 当前任务\n第一次修复"
        second_summary = "## 当前任务\n第二次修复（更新）"

        previous_values = []
        summarized_batches = []

        async def mock_summarize(msgs, prev=None):
            previous_values.append(prev)
            summarized_batches.append(tuple(msg.get("content", "") for msg in msgs))
            if len(previous_values) == 1:
                return first_summary
            return second_summary

        with patch("ut_agent.llm._llm_summarize_messages", side_effect=mock_summarize):
            with patch("ut_agent.llm._estimate_tokens", return_value=100000):
                with patch("ut_agent.llm._get_compress_threshold", return_value=50000):
                    # 第一次压缩
                    result1 = await compress_messages_if_needed(messages, return_state=True)
                    # 第二次压缩（模拟更多消息后再次超阈值）
                    result2 = await compress_messages_if_needed(
                        messages + messages,
                        compression_state=result1.state,
                        return_state=True,
                    )

        # 第二次调用时应该传入了 previous_summary
        assert isinstance(result1, CompressionResult)
        assert isinstance(result2, CompressionResult)
        assert previous_values == [None, first_summary]
        assert len(summarized_batches[1]) < len(messages + messages)
        assert result2.state["context_summary"] == second_summary

    @pytest.mark.asyncio
    async def test_compression_state_is_isolated_between_tasks(self):
        messages = [
            {"role": "user", "content": f"message-{index}" + "x" * 2000}
            for index in range(30)
        ]
        summaries = ["task-a-summary", "task-b-summary"]

        with patch("ut_agent.llm._llm_summarize_messages", new_callable=AsyncMock, side_effect=summaries):
            with patch("ut_agent.llm._estimate_tokens", return_value=100000):
                first = await compress_messages_if_needed(messages, max_tokens=50000, return_state=True)
                second = await compress_messages_if_needed(messages, max_tokens=50000, return_state=True)

        assert first.state["context_summary"] == "task-a-summary"
        assert second.state["context_summary"] == "task-b-summary"

    @pytest.mark.asyncio
    async def test_summary_failure_does_not_advance_checkpoint_cursor(self):
        messages = [
            {"role": "user", "content": f"message-{index}" + "x" * 2000}
            for index in range(30)
        ]
        prior = {
            "context_summary": "stable summary",
            "context_summary_covered_messages": 3,
        }

        with patch("ut_agent.llm._llm_summarize_messages", new_callable=AsyncMock, return_value=""):
            with patch("ut_agent.llm._estimate_tokens", return_value=100000):
                result = await compress_messages_if_needed(
                    messages,
                    max_tokens=50000,
                    compression_state=prior,
                    return_state=True,
                    clock=lambda: 1000.0,
                )

        assert result.state["context_summary"] == "stable summary"
        assert result.state["context_summary_covered_messages"] == 3
        assert result.state["context_compression_cooldown_until"] == 1060.0

    @pytest.mark.asyncio
    async def test_restored_cooldown_uses_wall_clock_and_skips_llm(self):
        messages = [
            {"role": "user", "content": f"message-{index}" + "x" * 2000}
            for index in range(30)
        ]
        state = {
            "context_summary": "restored summary",
            "context_summary_covered_messages": 10,
            "context_compression_cooldown_until": 1060.0,
        }

        with patch("ut_agent.llm._llm_summarize_messages", new_callable=AsyncMock) as summarize:
            with patch("ut_agent.llm._estimate_tokens", return_value=100000):
                result = await compress_messages_if_needed(
                    messages,
                    max_tokens=50000,
                    compression_state=state,
                    return_state=True,
                    clock=lambda: 1001.0,
                )

        summarize.assert_not_called()
        assert result.state["context_compression_cooldown_until"] == 1060.0
        assert "restored summary" in result.messages[0]["content"]
        assert len(result.messages) < len(messages)

    @pytest.mark.asyncio
    async def test_fallback_to_truncate_on_llm_failure(self):
        """LLM 摘要失败时降级为粗暴截断。"""
        _reset_compression_state()
        messages = []
        for i in range(30):
            messages.append({"role": "assistant", "content": f"第 {i} 轮" + "x" * 2000})
            messages.append({"role": "user", "content": f"继续 {i}" + "y" * 2000})

        with patch("ut_agent.llm._llm_summarize_messages", new_callable=AsyncMock, side_effect=Exception("LLM 不可用")):
            with patch("ut_agent.llm._estimate_tokens", return_value=100000):
                with patch("ut_agent.llm._get_compress_threshold", return_value=50000):
                    result = await compress_messages_if_needed(messages)

        # 应该降级为粗暴截断，不抛异常
        assert len(result) < len(messages)
        assert any("[之前对话的摘要]" in str(msg.get("content", "")) for msg in result)

    def test_preserves_different_results(self):
        """不同内容不去重。"""
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "r1", "function": {"name": "read_repo_file_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "r1", "content": "x" * 200},
            {"role": "assistant", "tool_calls": [{"id": "r2", "function": {"name": "read_repo_file_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "r2", "content": "y" * 200},
        ]
        result = _prune_old_tool_results(messages)
        assert result[1]["content"] == "x" * 200
        assert result[3]["content"] == "y" * 200


# ── Task 3: 按 token 预算保护尾部 + 孤立 tool_call 清理 ──

from ut_agent.llm import _find_tail_cut_by_tokens, _cleanup_orphaned_tool_calls


class TestFindTailCutByTokens:
    """按 token 预算保护尾部（学 Hermes _find_tail_cut_by_tokens）。"""

    def test_protects_recent_messages_within_budget(self):
        """尾部按 token 预算保护，不是固定 N 条。"""
        messages = [{"role": "user", "content": "x" * 100} for _ in range(20)]
        # 每条 100 字符，预算 500 → 保护最近约 5 条
        # 用 mock 固定 token 数，避免 litellm token_counter 差异
        with patch("ut_agent.llm._estimate_tokens", side_effect=lambda msgs: sum(len(m.get("content", "")) for m in msgs)):
            tail_start = _find_tail_cut_by_tokens(messages, head_end=0, token_budget=500)
        # 尾部应该包含最近几条（预算 500 / 每条 100 = 5 条，但 min_tail 上限 10）
        assert tail_start < 20
        assert tail_start >= 10  # 至少保护 10 条（min_tail 上限）

    def test_respects_min_tail_floor(self):
        """至少保护 3 条消息。"""
        messages = [{"role": "user", "content": "x" * 10000} for _ in range(10)]
        # 每条 10000 字符，预算 500 → 超出预算但仍要保护至少 3 条
        tail_start = _find_tail_cut_by_tokens(messages, head_end=0, token_budget=500)
        assert tail_start <= 7  # 至少保护 3 条（10-3=7）

    def test_never_cuts_into_head(self):
        """尾部起始不能在头部之前。"""
        messages = [{"role": "user", "content": "x"} for _ in range(10)]
        tail_start = _find_tail_cut_by_tokens(messages, head_end=5, token_budget=100)
        assert tail_start > 5  # 必须在头部之后


class TestCleanupOrphanedToolCalls:
    """清理孤立 tool_call/tool_result（学 Hermes）。"""

    def test_removes_orphaned_tool_results(self):
        """没有对应 tool_call 的 tool result 被删除。"""
        messages = [
            {"role": "assistant", "content": "hello"},
            # 孤立的 tool result——没有对应的 tool_call
            {"role": "tool", "tool_call_id": "orphan", "content": "orphaned result"},
            {"role": "user", "content": "next"},
        ]
        result = _cleanup_orphaned_tool_calls(messages)
        # 孤立的 tool result 被删除
        assert not any(msg.get("role") == "tool" for msg in result)

    def test_removes_orphaned_tool_calls(self):
        """没有对应 tool result 的 tool_calls 被删除。"""
        messages = [
            {"role": "assistant", "content": "thinking", "tool_calls": [
                {"id": "c1", "function": {"name": "some_tool", "arguments": "{}"}},
            ]},
            # 没有 tool result——tool_call 是孤立的
            {"role": "user", "content": "next"},
        ]
        result = _cleanup_orphaned_tool_calls(messages)
        # 孤立的 tool_calls 被删除
        assistant_msg = next(msg for msg in result if msg.get("role") == "assistant")
        assert not assistant_msg.get("tool_calls")

    def test_preserves_matched_pairs(self):
        """匹配的 tool_call/tool_result 对保留。"""
        messages = [
            {"role": "assistant", "content": "thinking", "tool_calls": [
                {"id": "c1", "function": {"name": "some_tool", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
            {"role": "user", "content": "next"},
        ]
        result = _cleanup_orphaned_tool_calls(messages)
        # 匹配的对保留
        assert any(msg.get("role") == "tool" and msg.get("tool_call_id") == "c1" for msg in result)
        assistant_msg = next(msg for msg in result if msg.get("role") == "assistant")
        assert assistant_msg.get("tool_calls")
