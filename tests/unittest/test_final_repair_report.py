import asyncio
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from pr_agent.distributed.broker import StoredTask
from pr_agent.distributed.models import MrKey, TaskEnvelope, TaskKind, TaskStatus
from pr_agent.distributed.repair_report_tasks import (
    build_final_report_input,
    final_file_changes,
    generate_final_repair_report,
    parse_gitlab_compare_diffs,
)
from pr_agent.triage.failure_explanations import FailureExplanation
from pr_agent.triage.final_repair_report import (
    FinalRepairDiff,
    FinalRepairReportInput,
    FinalRepairReportState,
    RepairReportStatus,
    RepairReportValidationError,
    build_diff_fallback,
    parse_and_validate_report,
)
from pr_agent.triage.pipeline_repair import PipelineRepairPhase, PipelineRepairState
from pr_agent.triage.repair_rollback import RepairCommitEntry, RepairCommitManifest
from ut_agent.llm import LLMTextOutcome
from ut_agent.model_failover import LLMCallOutcome, ModelAttempt

BASE_SHA = "a" * 40
FINAL_SHA = "b" * 40


def _diff(path: str = "src/a.cpp", patch: str = "@@ -1 +1 @@\n-return false;\n+return true;") -> FinalRepairDiff:
    return FinalRepairDiff(path, "modified", 1, 1, patch)


def _input(*, diffs: tuple[FinalRepairDiff, ...] | None = None) -> FinalRepairReportInput:
    return FinalRepairReportInput(
        repair_task_id="task-12345678",
        project_id="eabot/cook",
        mr_iid=549,
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/549",
        source_pipeline_id=31709,
        source_sha="eac028f114cc",
        base_sha="eac028f114cc",
        final_sha="1ce85a7c9925",
        final_pipeline_id=31732,
        final_pipeline_status="success",
        final_coverage=63.04,
        selected_categories=("build",),
        failed_jobs=("build_release_arm64",),
        causal_lines=("error: request has no member named node_name",),
        diffs=diffs if diffs is not None else (_diff(),),
        final_coverage_source="changed_lines",
        final_coverage_status="reported",
    )


def _report(
    *,
    paths: tuple[str, ...] = ("src/a.cpp",),
    evidence: str = "return true;",
    root_cause: str = "代码访问了不存在的 node_name 成员，导致编译失败。",
    solution: str = "移除无效字段访问并使用现有接口。",
    rationale: str = "该修改直接消除了编译器指出的无效成员引用。",
) -> str:
    return json.dumps({
        "schema_version": 1,
        "root_cause_summary": root_cause,
        "solution_summary": solution,
        "rationale": rationale,
        "file_explanations": [
            {"path": path, "summary": "改用当前请求对象实际提供的接口。", "evidence": [evidence]}
            for path in paths
        ],
    })


def test_report_input_digest_covers_ci_and_normalized_diff():
    value = _input()
    restored = FinalRepairReportInput.from_json(value.to_json())
    assert restored.digest() == value.digest()
    assert restored.final_coverage_source == "changed_lines"
    assert restored.final_coverage_status == "reported"
    assert replace(value, final_pipeline_id=value.final_pipeline_id + 1).digest() != value.digest()


def test_old_task_report_state_can_be_absent():
    assert FinalRepairReportState.from_json("") is None


@pytest.mark.parametrize("payload", [
    _report(paths=("src/a.cpp", "src/invented.cpp")),
    _report(paths=()),
    "```json\n" + _report() + "\n```",
    _report(paths=("../secret",)),
])
def test_model_report_rejects_extra_missing_wrapped_or_unsafe_files(payload):
    with pytest.raises(RepairReportValidationError):
        parse_and_validate_report(payload, _input())


def test_model_file_evidence_must_be_an_exact_changed_line():
    with pytest.raises(RepairReportValidationError, match="diff evidence"):
        parse_and_validate_report(_report(evidence="installed a new package"), _input())


def test_valid_model_report_is_accepted():
    report = parse_and_validate_report(_report(), _input())
    assert report.source == "model"
    assert report.file_explanations[0].path == "src/a.cpp"


def test_diff_fallback_uses_only_final_diff_facts():
    report = build_diff_fallback(_input(), "模型超时")
    assert report.source == "diff_fallback"
    assert report.file_explanations[0].evidence == ("return false;", "return true;")
    assert "success" in report.rationale


def _stored_task(*, latest_sha: str = FINAL_SHA) -> StoredTask:
    envelope = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="feishu",
        mr=MrKey("eabot/cook", 549),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/549",
        command="/repair-pipeline",
        payload={"source_pipeline_id": 31709, "source_pipeline_sha": "c" * 40},
        idempotency_key="repair-549",
    )
    manifest = RepairCommitManifest(
        repair_task_id=envelope.task_id,
        project_id="eabot/cook",
        mr_iid=549,
        source_branch="fix/test",
        base_commit_sha=BASE_SHA,
        base_tree_sha="d" * 40,
        authorized_actor_id="actor",
        entries=(RepairCommitEntry(1, FINAL_SHA, BASE_SHA, "e" * 40, "effect", "marker", "now"),),
        frozen=True,
        frozen_at="now",
    )
    return StoredTask(
        envelope=envelope,
        status=TaskStatus.COMPLETED,
        attempt=1,
        worker_id="worker-1",
        fencing_token=1,
        result="success",
        error="",
        pipeline_repair_state=PipelineRepairState(
            phase=PipelineRepairPhase.TERMINAL,
            latest_pipeline_id=31732,
            latest_pipeline_sha=latest_sha,
            final_pipeline_status="success",
            final_coverage=63.04,
            final_coverage_source="changed_lines",
            final_coverage_status="reported",
            selected_categories=("build",),
            failed_job_names=("build_release_arm64",),
        ),
        repair_commit_manifest=manifest,
    )


def test_compare_diff_tracks_rename_and_bounds_long_lines():
    raw = [{
        "old_path": "src/old.cpp",
        "new_path": "src/new.cpp",
        "renamed_file": True,
        "new_file": False,
        "deleted_file": False,
        "diff": "@@ -1 +1 @@\n-old_name\n+" + "x" * 900,
    }]
    diffs = parse_gitlab_compare_diffs(raw, max_line_chars=120)
    assert diffs[0].path == "src/new.cpp"
    assert diffs[0].change_type == "renamed"
    assert diffs[0].truncated is True
    assert len(max(diffs[0].patch.splitlines(), key=len)) <= 120


def test_compare_diff_enforces_a_total_prompt_budget_without_dropping_files():
    raw = [
        {
            "old_path": f"src/{index}.cpp",
            "new_path": f"src/{index}.cpp",
            "diff": "@@ -1 +1 @@\n-old\n+" + "x" * 3000,
        }
        for index in range(4)
    ]
    diffs = parse_gitlab_compare_diffs(raw, max_total_chars=4096, max_line_chars=3000)
    assert len(diffs) == 4
    assert sum(len(item.patch) for item in diffs) <= 4096
    assert all(item.truncated for item in diffs)


def test_snapshot_refuses_manifest_final_sha_mismatch():
    project = Mock()
    state = build_final_report_input(_stored_task(latest_sha="f" * 40), None, project)
    assert state.status.value == "fallback"
    assert "提交边界" in state.failure_reason
    project.repository_compare.assert_not_called()


def test_snapshot_uses_frozen_compare_boundary_and_builds_page_diff():
    project = Mock()
    project.repository_compare.return_value = {
        "commits": [{"id": FINAL_SHA}],
        "diffs": [{
            "old_path": "src/a.cpp",
            "new_path": "src/a.cpp",
            "diff": "@@ -1 +1 @@\n-return false;\n+return true;",
        }],
    }
    value = build_final_report_input(_stored_task(), None, project, report_task_id="report-1")
    assert isinstance(value, FinalRepairReportInput)
    assert value.final_coverage == 63.04
    assert value.final_coverage_source == "changed_lines"
    assert value.final_coverage_status == "reported"
    project.repository_compare.assert_called_once_with(BASE_SHA, FINAL_SHA)
    changes = final_file_changes(value)
    assert changes[0].path == "src/a.cpp"
    assert changes[0].hunks[0].lines[1].content == "return true;"


def test_snapshot_builds_report_from_original_source_failures():
    project = Mock()
    project.repository_compare.return_value = {
        "commits": [{"id": FINAL_SHA}],
        "diffs": [{
            "old_path": "src/a.cpp",
            "new_path": "src/a.cpp",
            "diff": "@@ -1 +1 @@\n-request->node_name;\n+request->command;",
        }],
    }
    stored = _stored_task()
    source_explanation = FailureExplanation(
        job_name="build_release_arm64",
        confirmed_reason="/builds/eabot/cook/src/a.cpp:1:9: error: no member named node_name",
        confidence="confirmed",
    )
    stored = replace(stored, pipeline_repair_state=replace(
        stored.pipeline_repair_state,
        final_pipeline_status="success",
        failed_job_names=(),
        source_failure_explanations=(source_explanation,),
        failure_explanations=(),
    ))

    value = build_final_report_input(stored, None, project, report_task_id="report-1")

    assert isinstance(value, FinalRepairReportInput)
    assert value.failed_jobs == ("build_release_arm64",)
    assert value.causal_lines[0].endswith("error: no member named node_name")
def test_valid_model_report_is_persisted_as_model_generated():
    outcome = LLMTextOutcome(_report(), "anthropic/claude-sonnet-5", (ModelAttempt("anthropic/claude-sonnet-5"),))
    state = asyncio.run(generate_final_repair_report(_input(), outcome=outcome))
    assert state.status is RepairReportStatus.MODEL_GENERATED
    assert state.report.source == "model"


def test_completed_legacy_effect_remains_readable():
    outcome = LLMTextOutcome(_report(), "legacy-model", ())

    state = asyncio.run(generate_final_repair_report(_input(), outcome=outcome))

    assert state.status is RepairReportStatus.MODEL_GENERATED
    assert state.report.source == "model"


def test_invalid_model_report_becomes_diff_fallback():
    outcome = LLMTextOutcome("not-json", "anthropic/claude-sonnet-5", ())
    state = asyncio.run(generate_final_repair_report(_input(), outcome=outcome))
    assert state.status is RepairReportStatus.FALLBACK
    assert state.report.source == "diff_fallback"


def _tool_report_outcome(payload: str, *, tool_name: str = "submit_final_repair_report") -> LLMCallOutcome:
    return LLMCallOutcome(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "report-1",
                "type": "function",
                "function": {"name": tool_name, "arguments": payload},
            }],
        },
        "test-model",
        (ModelAttempt("test-model"),),
    )


def test_strict_report_rejects_unknown_fields_and_string_schema_version():
    extra = json.loads(_report())
    extra["unexpected"] = "ignored"
    with pytest.raises(RepairReportValidationError, match="extra_forbidden"):
        parse_and_validate_report(json.dumps(extra, ensure_ascii=False), _input())

    string_version = json.loads(_report())
    string_version["schema_version"] = "1"
    with pytest.raises(RepairReportValidationError, match="literal_error"):
        parse_and_validate_report(json.dumps(string_version, ensure_ascii=False), _input())


def test_invalid_tool_report_is_corrected_once_without_echoing_rejected_body():
    prompts = []
    outcomes = iter((
        _tool_report_outcome("{}"),
        _tool_report_outcome(_report()),
    ))

    async def llm_call(system, user, **kwargs):
        del system, kwargs
        prompts.append(user)
        return next(outcomes)

    state = asyncio.run(generate_final_repair_report(_input(), llm_call=llm_call))

    assert state.status is RepairReportStatus.MODEL_GENERATED
    assert len(prompts) == 2
    assert "schema:" in prompts[1]
    assert '"unexpected"' not in prompts[1]


def test_two_invalid_tool_reports_use_diff_fallback():
    calls = 0

    async def llm_call(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        return _tool_report_outcome("{}")

    state = asyncio.run(generate_final_repair_report(_input(), llm_call=llm_call))

    assert calls == 2
    assert state.status is RepairReportStatus.FALLBACK
    assert state.report.source == "diff_fallback"
