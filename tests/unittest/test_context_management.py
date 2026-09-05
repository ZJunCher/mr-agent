"""extract_known_facts 和分级截断单测。"""
import json

import pytest


def test_extract_known_facts_empty():
    from ut_agent.llm import extract_known_facts
    assert extract_known_facts([]) == ""


def test_extract_known_facts_from_tool_returns():
    from ut_agent.llm import extract_known_facts
    messages = [
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": "查日志", "tool_calls": [{"id": "c1", "function": {"name": "fetch_pipeline_logs_tool"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"failed_jobs": [{"name": "build"}], "pipeline_status": "failed"}'},
        {"role": "assistant", "content": "克隆", "tool_calls": [{"id": "c2", "function": {"name": "clone_source_branch_tool"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "已克隆到 mr_532/repo/"},
        {"role": "assistant", "content": "读文件", "tool_calls": [{"id": "c3", "function": {"name": "read_repo_file_tool"}}]},
        {"role": "tool", "tool_call_id": "c3", "content": "cmake_minimum_required(VERSION 3.8)\nfind_package(rslidar_msg REQUIRED)"},
    ]
    result = extract_known_facts(messages)
    assert "流水线结果" in result
    assert "已克隆仓库" in result
    assert "已读文件" in result
    assert "最近工具结果" in result


def test_extract_known_facts_ignores_assistant_text():
    """assistant 的纯文本思考不应被提取为事实。"""
    from ut_agent.llm import extract_known_facts
    messages = [
        {"role": "assistant", "content": "我猜测依赖可能缺失，需要检查"},
    ]
    result = extract_known_facts(messages)
    assert result == ""  # assistant 思考不算事实


def test_truncate_tool_results_code_not_truncated():
    """read_repo_file_tool 的返回不应被截断到 3000。"""
    from ut_agent.llm import _truncate_tool_results
    long_code = "x" * 5000  # 5000 字符，超过默认 3000
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read_repo_file_tool"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": long_code},
    ]
    result = _truncate_tool_results(messages)
    assert len(result[1]["content"]) == 5000  # 没截断


def test_truncate_tool_results_pipeline_log_compressed():
    """fetch_pipeline_logs_tool 的返回应被压缩。"""
    from ut_agent.llm import _truncate_tool_results
    long_log = '{"failed_jobs": [{"name": "build", "log_tail": "' + "x" * 5000 + '"}]}'
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "fetch_pipeline_logs_tool"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": long_log},
    ]
    result = _truncate_tool_results(messages)
    assert len(result[1]["content"]) <= 3000  # 被压缩


def test_truncate_tool_results_generate_code_not_truncated():
    """generate_code_tool 的返回不应被截断。"""
    from ut_agent.llm import _truncate_tool_results
    long_result = "patch content " * 500  # 约 6500 字符
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "generate_code_tool"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": long_result},
    ]
    result = _truncate_tool_results(messages)
    assert len(result[1]["content"]) == len(long_result)  # 没截断


def test_extract_known_facts_from_fact_marker():
    """[FACT] 标记的优先级高于关键词匹配。"""
    from ut_agent.llm import extract_known_facts
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "clone_source_branch_tool"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "[FACT] 已克隆仓库到: /app/ut_agent/workspace/mr_532/repo\n/app/ut_agent/workspace/mr_532/repo"},
    ]
    result = extract_known_facts(messages)
    assert "已克隆仓库到: /app/ut_agent/workspace/mr_532/repo" in result


def test_extract_known_facts_from_json_facts_field():
    """JSON _facts 字段也能被提取。"""
    from ut_agent.llm import extract_known_facts
    import json
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "fetch_pipeline_logs_tool"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"_facts": ["流水线状态: failed, 失败 jobs: build"], "failed_jobs": []})},
    ]
    result = extract_known_facts(messages)
    assert "流水线状态: failed" in result


def test_extract_known_facts_fallback_to_keywords():
    """没有 [FACT] 标记时降级用关键词匹配。"""
    from ut_agent.llm import extract_known_facts
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "some_tool"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "some random output without fact marker"},
    ]
    result = extract_known_facts(messages)
    assert "some random output" in result  # 降级用关键词


def test_native_manifest_is_not_dropped_to_meet_cosmetic_limit():
    from ut_agent.llm import _compact_native_safety_result

    changed_files = [f"src/file_{index}.py" for index in range(100)]
    compacted = _compact_native_safety_result(
        "apply_repo_patch_tool",
        json.dumps({
            "status": "changed",
            "patch_applied": True,
            "base_sha": "a" * 40,
            "diff_digest": "sha256:" + "b" * 64,
            "changed_files": changed_files,
            "diff_check": {"passed": True},
            "reason": "x" * 10000,
        }),
        80,
    )

    parsed = json.loads(compacted)
    assert parsed["changed_files"] == changed_files
    assert len(compacted) > 80
