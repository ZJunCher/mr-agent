import json
import subprocess

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing the eager ut_agent package.
import ut_agent.execution_policy as execution_policy
import ut_agent.repair_safety as repair_safety_module
import ut_agent.tools.generate_code as generate_code_module
from ut_agent.blocker_evidence import (
    BLOCKER_JSON_BEGIN,
    BLOCKER_JSON_END,
    parse_blocker_record,
)
from ut_agent.prompt.agent_system import build_system_prompt
from ut_agent.tools.context import ToolContext


@pytest.fixture(autouse=True)
def hermes_backend(monkeypatch):
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "hermes")


def _valid_blocker(job_name: str = "build_release_arm64") -> dict:
    return {
        "schema_version": 1,
        "outcome": "blocked",
        "job_name": job_name,
        "blocker_type": "external_dependency",
        "root_cause": "SDK headers are required by compiled source but absent from the image.",
        "ci_evidence": [{
            "job_name": job_name,
            "observation": "The compiler reports missing sdk/header.h.",
        }],
        "repository_evidence": [{
            "kind": "source_reference",
            "locator": "src/main.cpp:12",
            "observation": "Compiled source includes sdk/header.h.",
        }],
        "attempted_repairs": ["Checked vendored sources and repository-local fallback headers."],
        "why_no_safe_repo_change": "Removing the include leaves required SDK symbols undefined.",
        "suggested_action": "Install the required SDK in CI and retry.",
    }


def test_pipeline_agent_prompt_requires_repair_after_investigation(monkeypatch):
    import ut_agent.config as config_module
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "hermes")
    prompt = build_system_prompt(
        {
            "trigger_type": "pipeline_failed",
            "mr_id": 536,
            "failed_jobs": [{"name": "build_release_arm64"}],
        },
        "generate_code_tool",
    )

    assert 'operation="investigate"' in prompt
    assert 'operation="repair"' in prompt
    assert 'operation="verify_blocker"' in prompt
    assert "调查结果不是终态" in prompt
    assert "真实 repair" in prompt


def test_pipeline_agent_prompt_forbids_using_history_as_repair_answer(monkeypatch):
    import ut_agent.config as config_module
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "hermes")
    prompt = build_system_prompt(
        {"trigger_type": "pipeline_failed", "mr_id": 536},
        "generate_code_tool",
    )

    assert "不能把历史 commit" in prompt
    assert "Revert" in prompt


def _render_blocker(record: dict) -> str:
    return (
        f"diagnosis before record\n{BLOCKER_JSON_BEGIN}\n"
        f"{json.dumps(record, ensure_ascii=False)}\n{BLOCKER_JSON_END}"
    )


def test_parse_blocker_record_accepts_complete_current_job_record():
    record, error = parse_blocker_record(_render_blocker(_valid_blocker()), "build_release_arm64")

    assert error is None
    assert record is not None
    assert record["blocker_type"] == "external_dependency"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("repository_evidence"),
        lambda value: value.update(job_name="some_other_job"),
        lambda value: value.update(blocker_type="made_up"),
        lambda value: value.update(why_no_safe_repo_change=""),
        lambda value: value.update(suggested_action=""),
        lambda value: value.update(ci_evidence=[]),
        lambda value: value.update(attempted_repairs=[]),
    ],
)
def test_parse_blocker_record_rejects_incomplete_or_wrong_job_records(mutation):
    value = _valid_blocker()
    mutation(value)

    record, error = parse_blocker_record(_render_blocker(value), "build_release_arm64")

    assert record is None
    assert error


def test_parse_blocker_record_rejects_ci_evidence_for_another_job():
    value = _valid_blocker()
    value["ci_evidence"][0]["job_name"] = "some_other_job"

    record, error = parse_blocker_record(_render_blocker(value), "build_release_arm64")

    assert record is None
    assert "CI" in error


@pytest.mark.parametrize(
    ("text", "expected_error"),
    [
        ("plain prose", "起始标记"),
        (f"{BLOCKER_JSON_BEGIN}\n{{}}", "结束标记"),
        (f"{BLOCKER_JSON_BEGIN}\nnot-json\n{BLOCKER_JSON_END}", "无法解析"),
    ],
)
def test_parse_blocker_record_rejects_missing_markers_and_malformed_json(text, expected_error):
    record, error = parse_blocker_record(text, "build_release_arm64")

    assert record is None
    assert expected_error in error


def test_parse_blocker_record_uses_last_complete_record():
    wrong = _render_blocker(_valid_blocker("some_other_job"))
    current = _render_blocker(_valid_blocker())

    record, error = parse_blocker_record(f"{wrong}\n{current}", "build_release_arm64")

    assert error is None
    assert record is not None
    assert record["job_name"] == "build_release_arm64"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ci_evidence", ["not-an-object"]),
        ("repository_evidence", [{"kind": "source_reference", "locator": "", "observation": "found"}]),
        ("repository_evidence", [{"kind": "source_reference", "locator": "src/main.cpp", "observation": ""}]),
        ("attempted_repairs", [""]),
    ],
)
def test_parse_blocker_record_rejects_malformed_evidence_entries(field, value):
    blocker = _valid_blocker()
    blocker[field] = value

    record, error = parse_blocker_record(_render_blocker(blocker), "build_release_arm64")

    assert record is None
    assert error


def _run_pipeline_generate(
    monkeypatch,
    tmp_path,
    operation,
    task_description,
    diagnostic,
    changed_files=None,
    error=None,
):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_536" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    prompts = {
        "generate_investigate_system": "INVESTIGATE: never edit",
        "generate_fix_system": "REPAIR: 必须尝试最小修改",
        "verify_blocker_system": "VERIFY BLOCKER: never edit",
        "generate_fix_user": "FIX {task_description} {repo_dir} {mr_id} {iteration}",
    }
    monkeypatch.setattr(generate_code_module, "load_prompt", lambda name: prompts[name])
    captured = {"calls": 0}

    def fake_run_hermes(_repo, prompt, hide_git_metadata=False, **_kwargs):
        captured["calls"] += 1
        captured["prompt"] = prompt
        captured["hide_git_metadata"] = hide_git_metadata
        if error:
            return generate_code_module._provider_error_outcome(error, diagnostic=diagnostic)
        return generate_code_module.HermesRunOutcome(tuple(changed_files or ()), diagnostic)

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)
    monkeypatch.setattr(
        repair_safety_module,
        "validate_member_substitutions",
        lambda _repo, _evidence=(): (True, ""),
    )
    result = generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description=task_description,
        operation=operation,
        state={"mr_id": 536, "iteration": 1, "trigger_type": "pipeline_failed"},
    )
    return json.loads(result), captured


def test_pipeline_investigation_is_read_only_and_nonterminal(monkeypatch, tmp_path):
    payload, captured = _run_pipeline_generate(
        monkeypatch,
        tmp_path,
        "investigate",
        "search the repository",
        "dependency declaration found",
    )

    assert payload["status"] == "investigated"
    assert payload["operation"] == "investigate"
    assert "INVESTIGATE" in captured["prompt"]
    assert captured["hide_git_metadata"] is True


def test_pipeline_repair_without_diff_is_not_an_investigation(monkeypatch, tmp_path):
    payload, captured = _run_pipeline_generate(
        monkeypatch,
        tmp_path,
        "repair",
        "search for the missing dependency",
        "no safe edit produced",
    )

    assert payload["status"] == "repair_no_changes"
    assert payload["operation"] == "repair"
    assert "必须尝试" in captured["prompt"]


def test_pipeline_verify_blocker_parses_structured_record(monkeypatch, tmp_path):
    diagnostic = _render_blocker(_valid_blocker())

    payload, captured = _run_pipeline_generate(
        monkeypatch,
        tmp_path,
        "verify_blocker",
        "verify the external blocker",
        diagnostic,
    )

    assert payload["status"] == "blocked"
    assert payload["blocker"]["job_name"] == "build_release_arm64"
    assert "VERIFY BLOCKER" in captured["prompt"]


def test_pipeline_verify_blocker_rejects_long_unstructured_diagnosis(monkeypatch, tmp_path):
    payload, _captured = _run_pipeline_generate(
        monkeypatch,
        tmp_path,
        "verify_blocker",
        "verify the external blocker",
        "long diagnosis " * 500,
    )

    assert payload["status"] == "incomplete"
    assert "起始标记" in payload["validation_error"]


def test_pipeline_missing_operation_is_incomplete_without_calling_hermes(monkeypatch, tmp_path):
    payload, captured = _run_pipeline_generate(
        monkeypatch,
        tmp_path,
        None,
        "fix the build",
        "Hermes should not be called",
    )

    assert payload["status"] == "incomplete"
    assert captured["calls"] == 0


@pytest.mark.parametrize("operation", ["investigate", "verify_blocker"])
def test_pipeline_read_only_operations_report_unexpected_changes(monkeypatch, tmp_path, operation):
    payload, _captured = _run_pipeline_generate(
        monkeypatch,
        tmp_path,
        operation,
        "inspect only",
        "unexpected edit",
        changed_files=["src/main.cpp"],
    )

    assert payload["status"] == "unexpected_changes"
    assert payload["changed_files"] == ["src/main.cpp"]


def test_pipeline_repair_with_diff_is_changed(monkeypatch, tmp_path):
    payload, _captured = _run_pipeline_generate(
        monkeypatch,
        tmp_path,
        "repair",
        "fix the build",
        "removed stale dependency",
        changed_files=["src/CMakeLists.txt"],
    )

    assert payload["status"] == "changed"
    assert payload["operation"] == "repair"


def test_pipeline_api_error_keeps_operation_and_infra_status(monkeypatch, tmp_path):
    payload, _captured = _run_pipeline_generate(
        monkeypatch,
        tmp_path,
        "repair",
        "fix the build",
        "API Error: Error code: 400",
        error="API Error: Error code: 400 - invalid tool schema",
    )

    assert payload["status"] == "coding_infra_error"
    assert payload["operation"] == "repair"


def test_non_pipeline_generation_keeps_existing_prompt_and_status(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_300" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    prompts = {
        "generate_patch_system": "GENERATE TESTS",
        "generate_patch_cpp": "CPP RULES",
        "generate_patch_python": "PYTHON RULES",
        "generate_patch_user": "{task_description} {repo_dir} {mr_id} {iteration}",
    }
    monkeypatch.setattr(generate_code_module, "load_prompt", lambda name: prompts[name])
    captured = {}

    def fake_run_hermes(_repo, prompt, hide_git_metadata=False, **_kwargs):
        captured["prompt"] = prompt
        captured["hide_git_metadata"] = hide_git_metadata
        return generate_code_module.HermesRunOutcome((), "no tests needed")

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)

    result = generate_code_module.generate_code_tool.func(
        job_name="unit_test_generation",
        task_description="generate tests",
        state={"mr_id": 300, "iteration": 1, "trigger_type": "mr_created"},
    )

    assert json.loads(result)["status"] == "no_changes"
    assert "GENERATE TESTS" in captured["prompt"]
    assert captured["hide_git_metadata"] is False


def test_git_metadata_is_restored_after_hermes_isolation(tmp_path):
    repo = tmp_path / "repo"
    marker = repo / ".git" / "HEAD"
    marker.parent.mkdir(parents=True)
    marker.write_text("ref: refs/heads/test\n", encoding="utf-8")

    with generate_code_module._hide_git_metadata(str(repo), enabled=True):
        assert not (repo / ".git").exists()
        result = subprocess.run(["git", "log", "-1"], cwd=repo, capture_output=True, text=True)
        assert result.returncode != 0

    assert marker.read_text(encoding="utf-8") == "ref: refs/heads/test\n"


def test_git_metadata_is_restored_when_hermes_raises(tmp_path):
    repo = tmp_path / "repo"
    marker = repo / ".git" / "HEAD"
    marker.parent.mkdir(parents=True)
    marker.write_text("current commit metadata\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Hermes failed"):
        with generate_code_module._hide_git_metadata(str(repo), enabled=True):
            raise RuntimeError("Hermes failed")

    assert marker.read_text(encoding="utf-8") == "current commit metadata\n"


def _tool_exchange(name: str, call_id: str, result: dict, args: dict | None = None) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}]),
        ToolMessage(content=json.dumps(result), tool_call_id=call_id),
    ]


def _failed_pipeline_messages(pipeline_id: int = 29442, matched_sha: str = "current-sha") -> list:
    return _tool_exchange(
        "fetch_pipeline_logs_tool",
        f"pipeline-{pipeline_id}",
        {
            "status": "success",
            "requested_commit_sha": matched_sha,
            "matched_commit_sha": matched_sha,
            "pipeline_id": pipeline_id,
            "pipeline_status": "failed",
            "failed_jobs": [{
                "job_id": 94815,
                "name": "build_release_arm64",
                "status": "failed",
                "log_tail": "Could not find a package configuration file provided by rslidar_msg",
            }],
            "work_items": [{
                "job_id": 94815,
                "pipeline_id": pipeline_id,
                "job_name": "build_release_arm64",
                "kind": "build",
                "required_tool": "generate_code_tool",
            }],
        },
    )


def _generate_exchange(operation: str, status: str, call_id: str, **extra) -> list:
    result = {
        "status": status,
        "operation": operation,
        "job_name": "build_release_arm64",
        "changed_files": [],
        "diagnostic": extra.pop("diagnostic", "current repository and CI evidence inspected"),
        "message": "structured generate result",
        **extra,
    }
    return _tool_exchange(
        "generate_code_tool",
        call_id,
        result,
        {
            "job_name": "build_release_arm64",
            "operation": operation,
            "task_description": "根据当前流水线证据处理失败",
        },
    )


def _failed_finish(messages: list) -> tuple[bool, str]:
    return execution_policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "当前 job 无法自动修复。"},
    )


def test_finish_failure_rejects_long_investigation_without_repair():
    messages = _failed_pipeline_messages()
    messages += _generate_exchange("investigate", "investigated", "investigate", diagnostic="x" * 5000)

    accepted, reason = _failed_finish(messages)

    assert accepted is False
    assert 'operation="repair"' in reason
    assert "build_release_arm64" in reason


def test_finish_failure_requires_blocker_verification_after_repair_no_changes():
    messages = _failed_pipeline_messages()
    messages += _generate_exchange("repair", "repair_no_changes", "repair")

    accepted, reason = _failed_finish(messages)

    assert accepted is False
    assert 'operation="verify_blocker"' in reason


def test_finish_failure_accepts_valid_blocker_after_real_repair_attempt():
    messages = _failed_pipeline_messages()
    messages += _generate_exchange("repair", "repair_no_changes", "repair")
    messages += _generate_exchange("verify_blocker", "blocked", "verify", blocker=_valid_blocker())

    assert _failed_finish(messages) == (True, "")


def test_finish_failure_rejects_blocker_verification_without_repair():
    messages = _failed_pipeline_messages()
    messages += _generate_exchange("verify_blocker", "blocked", "verify", blocker=_valid_blocker())

    accepted, reason = _failed_finish(messages)

    assert accepted is False
    assert 'operation="repair"' in reason


def test_finish_failure_rejects_blocker_for_another_job():
    blocker = _valid_blocker("some_other_job")
    messages = _failed_pipeline_messages()
    messages += _generate_exchange("repair", "repair_no_changes", "repair")
    messages += _generate_exchange("verify_blocker", "blocked", "verify", blocker=blocker)

    accepted, reason = _failed_finish(messages)

    assert accepted is False
    assert "job_name" in reason


def test_finish_failure_requires_commit_for_repair_changes():
    messages = _failed_pipeline_messages()
    messages += _generate_exchange(
        "repair",
        "changed",
        "repair",
        changed_files=["src/eabot_das_data_recorder/CMakeLists.txt"],
    )

    accepted, reason = _failed_finish(messages)

    assert accepted is False
    assert "提交并验证" in reason


def test_discarded_repair_changes_do_not_count_as_completed_repair():
    messages = _failed_pipeline_messages()
    messages += _generate_exchange("repair", "changed", "junk", changed_files=["junk_test_plan.md"])
    messages += _tool_exchange(
        "discard_workspace_tool",
        "discard",
        {"status": "success", "discarded_files": ["junk_test_plan.md"], "message": "discarded"},
        {"reason": "unrelated change"},
    )

    accepted, reason = _failed_finish(messages)

    assert accepted is False
    assert 'operation="repair"' in reason


@pytest.mark.parametrize("status", ["coding_infra_error", "incomplete", "unexpected_changes"])
def test_finish_failure_rejects_nonterminal_repair_results(status):
    messages = _failed_pipeline_messages()
    messages += _generate_exchange("repair", status, "repair")

    accepted, reason = _failed_finish(messages)

    assert accepted is False
    assert "未完成" in reason or "丢弃" in reason


def test_mr536_shaped_long_diagnosis_cannot_expose_or_replace_repair_attempt():
    messages = _failed_pipeline_messages()
    messages += _generate_exchange(
        "investigate",
        "investigated",
        "investigate",
        diagnostic=(
            "Current source contains rslidar_msg dependency declarations and no matching package source. "
            "The investigation used only the current failed log and current repository snapshot."
        ),
    )

    accepted, reason = _failed_finish(messages)

    assert accepted is False
    assert 'operation="repair"' in reason
