import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401  # Initialize settings before eager ut_agent imports.
import ut_agent.agent as agent_module
from ut_agent.execution_policy import build_failure_explanation_records


def test_result_exposes_blocker_root_cause_and_manual_action(monkeypatch):
    attempt = SimpleNamespace(
        name="generate_code_tool",
        args={"job_name": "mr_title_check", "operation": "verify_blocker"},
        result={
            "status": "blocked",
            "blocker": {
                "root_cause": "需求节点不是代码合入",
                "suggested_action": "更换需求 ID 或调整需求节点",
            },
        },
    )
    monkeypatch.setattr(
        "ut_agent.execution_policy.build_execution_ledger",
        lambda messages: SimpleNamespace(tool_attempts=[attempt]),
    )

    result = build_failure_explanation_records(
        [],
        {"failed_jobs": [{"name": "mr_title_check"}]},
    )

    assert result == [{
        "job_name": "mr_title_check",
        "possible_reason": "需求节点不是代码合入",
        "suggested_action": "更换需求 ID 或调整需求节点",
        "confidence": "inferred",
    }]


def test_result_omits_inference_for_non_current_job(monkeypatch):
    attempt = SimpleNamespace(
        name="generate_code_tool",
        args={"job_name": "old_job", "operation": "investigate"},
        result={"status": "investigated", "diagnostic": "old failure"},
    )
    monkeypatch.setattr(
        "ut_agent.execution_policy.build_execution_ledger",
        lambda messages: SimpleNamespace(tool_attempts=[attempt]),
    )

    result = build_failure_explanation_records(
        [],
        {"failed_jobs": [{"name": "mr_title_check"}]},
    )

    assert result == []


def test_preflight_blocker_supplies_analysis_when_no_generate_attempt(monkeypatch):
    monkeypatch.setattr(
        "ut_agent.execution_policy.build_execution_ledger",
        lambda messages: SimpleNamespace(tool_attempts=[]),
    )

    result = build_failure_explanation_records(
        [],
        {"failed_jobs": [{
            "name": "build_release_arm64",
            "preflight_blocker": {
                "root_cause": "CI 依赖分发制品下载失败并回退到默认配置，导致构建缺少所需依赖。",
                "suggested_action": "检查 ci_deps 制品服务后重新运行流水线。",
            },
        }]},
    )

    assert len(result) == 1
    assert result[0]["job_name"] == "build_release_arm64"
    assert "CI 依赖分发制品" in result[0]["possible_reason"]
    assert "ci_deps" in result[0]["suggested_action"]


def test_upstream_dependency_evidence_supplies_analysis_as_last_resort(monkeypatch):
    attempt = SimpleNamespace(
        name="resolve_dependency_evidence_tool",
        args={"job_name": "build_release_arm64"},
        result={
            "owner_facing_analysis": (
                "上游包 eabot/eabot_msgs（当前声明分支 dev）中不存在 LidarUdpFrame.msg。"
                "分支 `feature/lidar-v2` 上仍包含该文件，可作为替代来源。"
            ),
        },
    )
    monkeypatch.setattr(
        "ut_agent.execution_policy.build_execution_ledger",
        lambda messages: SimpleNamespace(tool_attempts=[attempt]),
    )

    result = build_failure_explanation_records(
        [],
        {"failed_jobs": [{"name": "build_release_arm64"}]},
    )

    assert len(result) == 1
    assert result[0]["job_name"] == "build_release_arm64"
    assert "LidarUdpFrame.msg" in result[0]["possible_reason"]


def test_finish_tool_summary_supplies_analysis_as_final_fallback(monkeypatch):
    attempt = SimpleNamespace(
        name="finish_tool",
        args={"success": False, "summary": "本次自动修复未产生代码修改，也未形成完整的外部阻塞证据。"},
        result=None,
    )
    monkeypatch.setattr(
        "ut_agent.execution_policy.build_execution_ledger",
        lambda messages: SimpleNamespace(tool_attempts=[attempt]),
    )

    result = build_failure_explanation_records(
        [],
        {"failed_jobs": [{"name": "build_release_arm64"}, {"name": "x86_64_ut_coverage_check"}]},
    )

    assert {record["job_name"] for record in result} == {"build_release_arm64", "x86_64_ut_coverage_check"}
    assert all("未产生代码修改" in record["possible_reason"] for record in result)


def test_finish_tool_success_is_never_used_as_failure_fallback(monkeypatch):
    attempt = SimpleNamespace(
        name="finish_tool",
        args={"success": True, "summary": "修复已完成并通过验证。"},
        result=None,
    )
    monkeypatch.setattr(
        "ut_agent.execution_policy.build_execution_ledger",
        lambda messages: SimpleNamespace(tool_attempts=[attempt]),
    )

    result = build_failure_explanation_records(
        [],
        {"failed_jobs": [{"name": "build_release_arm64"}]},
    )

    assert result == []


def test_hermes_diagnostic_machine_blocks_are_stripped(monkeypatch):
    diagnostic = (
        "构建失败根因是外部包缺失。\n"
        "BEGIN_TRIAGE_BLOCKER_JSON\n"
        '{"schema_version": 1, "outcome": "blocked", "job_name": "build_release_arm64"}\n'
        "END_TRIAGE_BLOCKER_JSON\n"
    )
    attempt = SimpleNamespace(
        name="generate_code_tool",
        args={"job_name": "build_release_arm64", "operation": "investigate"},
        result={"status": "investigated", "diagnostic": diagnostic},
    )
    monkeypatch.setattr(
        "ut_agent.execution_policy.build_execution_ledger",
        lambda messages: SimpleNamespace(tool_attempts=[attempt]),
    )

    result = build_failure_explanation_records(
        [],
        {"failed_jobs": [{"name": "build_release_arm64"}]},
    )

    assert len(result) == 1
    assert "schema_version" not in result[0]["possible_reason"]
    assert "BEGIN_TRIAGE_BLOCKER_JSON" not in result[0]["possible_reason"]
    assert "外部包缺失" in result[0]["possible_reason"]


def test_hermes_stdout_tail_that_is_just_quoted_source_is_rejected(monkeypatch):
    diagnostic = (
        "; L49: rclcpp::init(argc, argv); L50: } L51: } L52: L53: void TearDown() "
        "override { L54: if (rclcpp::ok()) { L55: rclcpp::shutdown(); L56: } L57: } "
        "L58: }; L59: L60: class RecordingStatisticsTest : public ::testing::Test {"
    )
    attempt = SimpleNamespace(
        name="generate_code_tool",
        args={"job_name": "build_release_arm64", "operation": "repair_session"},
        result={"status": "blocked", "diagnostic": diagnostic},
    )
    monkeypatch.setattr(
        "ut_agent.execution_policy.build_execution_ledger",
        lambda messages: SimpleNamespace(tool_attempts=[attempt]),
    )

    result = build_failure_explanation_records(
        [],
        {"failed_jobs": [{"name": "build_release_arm64"}]},
    )

    assert result == []


def test_extract_result_carries_current_job_inference():
    messages = [
        AIMessage(content="", tool_calls=[{
            "name": "fetch_pipeline_logs_tool",
            "args": {"pipeline_id": 31089},
            "id": "pipeline-1",
            "type": "tool_call",
        }]),
        ToolMessage(
            content=json.dumps({
                "status": "failed",
                "pipeline_status": "failed",
                "pipeline_id": 31089,
                "failed_jobs": [{"name": "mr_title_check"}],
            }),
            tool_call_id="pipeline-1",
        ),
        AIMessage(content="", tool_calls=[{
            "name": "generate_code_tool",
            "args": {"job_name": "mr_title_check", "operation": "investigate"},
            "id": "investigate-1",
            "type": "tool_call",
        }]),
        ToolMessage(
            content=json.dumps({
                "status": "investigated",
                "operation": "investigate",
                "diagnostic": "MR 标题关联的需求节点不是代码合入",
            }, ensure_ascii=False),
            tool_call_id="investigate-1",
        ),
    ]

    result = agent_module._extract_result({"iteration": 4, "max_iterations": 30}, messages)

    assert result["failure_explanations"] == [{
        "job_name": "mr_title_check",
        "possible_reason": "MR 标题关联的需求节点不是代码合入",
        "suggested_action": "",
        "confidence": "inferred",
    }]
