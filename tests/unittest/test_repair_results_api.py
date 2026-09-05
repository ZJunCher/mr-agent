from dataclasses import replace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pr_agent.distributed.broker import StoredTask
from pr_agent.distributed.models import (
    MrKey,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardBinding,
)
from pr_agent.servers.repair_results import (
    _deduplicate_progress,
    _durable_snapshot,
    _live_snapshot,
    _repair_result_html,
    _snapshot_is_settled,
    configure_repair_results_broker,
    router,
)
from pr_agent.triage.failure_explanations import FailureExplanation
from pr_agent.triage.final_repair_report import (
    FinalFileExplanation,
    FinalRepairDiff,
    FinalRepairReport,
    FinalRepairReportInput,
    FinalRepairReportState,
    RepairReportStatus,
)
from pr_agent.triage.pipeline_repair import PipelineRepairPhase, PipelineRepairState
from pr_agent.triage.repair_details import RepairAction, RepairProgressEvent, sign_repair_details_task
from pr_agent.triage.repair_rollback import RepairRollbackState, RepairRollbackStatus


def _task() -> TaskEnvelope:
    task = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="feishu",
        mr=MrKey("eabot/cook", 536),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        command="/repair-pipeline",
        payload={"source_pipeline_id": 31221, "source_pipeline_sha": "source-sha"},
        idempotency_key="repair-result-api",
    )
    return replace(task, task_id="task-12345678")


def _stored(task: TaskEnvelope, status: TaskStatus = TaskStatus.RUNNING) -> StoredTask:
    return StoredTask(
        envelope=task,
        status=status,
        attempt=1,
        worker_id="agent-1",
        fencing_token=7,
        result="",
        error="",
        pipeline_repair_state=PipelineRepairState(
            phase=PipelineRepairPhase.TRIAGE_RUNNING,
            root_pipeline_id=31221,
            latest_pipeline_id=31221,
            latest_pipeline_sha="source-sha",
            selected_categories=("build",),
            repair_actions=(RepairAction.from_dict({
                "action_id": "build-root",
                "categories": ["build"],
                "job_names": ["build_release_arm64"],
                "root_cause": "missing dependency",
                "solution_summary": "移除残留依赖声明。",
                "rationale": "源码没有使用该依赖，移除后构建系统不再查找不存在的包。",
                "file_changes": [{
                    "path": "CMakeLists.txt",
                    "change_type": "modified",
                    "summary": "移除 rslidar_msg 依赖。",
                    "additions": 0,
                    "deletions": 1,
                    "hunks": [{
                        "old_start": 33,
                        "new_start": 33,
                        "lines": [{
                            "kind": "deletion",
                            "old_line": 33,
                            "new_line": None,
                            "content": "find_package(rslidar_msg REQUIRED)",
                        }],
                    }],
                }],
                "status": "diagnosing",
            }),),
        ),
        created_at=100.0,
        updated_at=120.0,
    )


def _binding(task: TaskEnvelope) -> TriageCardBinding:
    return TriageCardBinding.new(
        card_id="card-536",
        task_id=task.task_id,
        open_message_id="message-536",
        receive_id="user-1",
        mr_url=task.pr_url,
        project_id="eabot/cook",
        mr_iid=536,
        mr_title="Revert generated change",
        source_branch="feature/test",
        pipeline_id=31221,
        pipeline_sha="source-sha",
        original_markdown="pipeline failed",
        failed_job_names=("build_release_arm64",),
    )


def _dependency_blocker() -> dict:
    candidates = [
        {
            "branch": f"candidate/{index}",
            "resolved_sha": f"candidate-sha-{index}",
            "verification_complete": True,
            "matched_queries": ["PlanningStatus.msg"],
            "missing_queries": [],
            "file_paths": {"PlanningStatus.msg": "eabot_msgs/msg/PlanningStatus.msg"},
            "raw_log": "must not persist",
        }
        for index in range(7)
    ]
    return {
        "type": "external_dependency",
        "summary": "当前声明分支缺少 PlanningStatus.msg。",
        "suggested_action": "请维护者确认候选依赖分支。",
        "blocked_job_names": ["build_release_arm64"],
        "dependency_evidence": [{
            "project_path": "eabot/lhotse",
            "declared_branch": "dev",
            "declared_sha": "dev-sha",
            "queries": [{"filename": "PlanningStatus.msg", "raw_log": "drop"}],
            "current_branch": {
                "branch": "dev",
                "resolved_sha": "dev-sha",
                "verification_complete": True,
                "matched_queries": [],
                "missing_queries": ["PlanningStatus.msg"],
                "file_paths": {},
                "url": "javascript:alert(1)",
            },
            "candidate_kind": "multiple_verified_candidates",
            "verified_candidates": candidates,
            "checked_branch_count": 300,
            "catalog_truncated": True,
            "raw_log": "full compiler log must not persist",
            "unknown": "drop",
        }],
        "unknown": "drop",
    }


def _client(monkeypatch, broker) -> TestClient:
    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_ENABLED", "true")
    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_SIGNING_SECRET", "test-secret-with-enough-entropy")
    configure_repair_results_broker(lambda: broker)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_snapshot_rejects_changed_signature_without_lookup(monkeypatch):
    broker = AsyncMock()
    client = _client(monkeypatch, broker)

    response = client.get("/api/repair-results/task-12345678?sig=wrong")

    assert response.status_code == 404
    broker.get_task.assert_not_awaited()


def test_snapshot_returns_live_repair_state_and_progress(monkeypatch):
    task = _task()
    broker = AsyncMock()
    broker.get_task.return_value = _stored(task)
    broker.get_task_triage_card.return_value = _binding(task)
    broker.get_repair_progress.return_value = [
        RepairProgressEvent.new(task.task_id, "diagnosing", "正在读取相关源码").with_event_id("10-0")
    ]
    client = _client(monkeypatch, broker)
    signature = sign_repair_details_task(task.task_id)

    response = client.get(f"/api/repair-results/{task.task_id}?sig={signature}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    payload = response.json()
    assert payload["source"] == "live"
    assert payload["mr"]["title"] == "Revert generated change"
    assert payload["phase"] == "triage_running"
    assert payload["actions"][0]["root_cause"] == "missing dependency"
    assert payload["actions"][0]["solution_summary"] == "移除残留依赖声明。"
    assert payload["actions"][0]["file_changes"][0]["path"] == "CMakeLists.txt"
    assert payload["progress"][0]["event_id"] == "10-0"
    assert payload["progress"][0]["count"] == 1


def test_live_snapshot_preserves_final_coverage_source():
    task = _task()
    action = replace(
        _stored(task).pipeline_repair_state.repair_actions[0],
        commit_sha="repair-sha",
        validation_pipeline_id=31222,
        validation_status="success",
        status="verified",
    )
    state = replace(
        _stored(task).pipeline_repair_state,
        final_pipeline_status="success",
        final_coverage=63.04,
        final_coverage_source="changed_lines",
        final_coverage_status="reported",
        failed_job_names=(),
        repair_actions=(action,),
    )

    snapshot = _live_snapshot(replace(_stored(task, TaskStatus.COMPLETED), pipeline_repair_state=state), _binding(task), [], None)

    assert snapshot["final_pipeline"]["coverage"] == 63.04
    assert snapshot["final_pipeline"]["coverage_source"] == "changed_lines"
    assert snapshot["final_pipeline"]["coverage_status"] == "reported"


def test_live_failed_result_keeps_final_failed_jobs():
    task = _task()
    state = replace(
        _stored(task).pipeline_repair_state,
        final_pipeline_status="failed",
        failed_job_names=("clang_tidy_check",),
    )
    stored = replace(_stored(task, TaskStatus.FAILED), pipeline_repair_state=state)

    assert _live_snapshot(stored, _binding(task), [], None)["failed_job_names"] == ["clang_tidy_check"]


def test_live_snapshot_exposes_terminal_validation_details_and_prefers_summary():
    task = _task()
    state = replace(
        _stored(task).pipeline_repair_state,
        terminal_error="",
        terminal_validation_error_code="diagnostic_identity_mismatch",
        terminal_validation_summary="缺少 16 条诊断身份，存在 16 条未知身份。",
        normalized_diagnostic_alias_count=0,
    )
    stored = replace(_stored(task, TaskStatus.FAILED), pipeline_repair_state=state)

    snapshot = _live_snapshot(stored, _binding(task), [], None)

    assert snapshot["error"] == state.terminal_validation_summary
    assert snapshot["terminal_validation_error_code"] == "diagnostic_identity_mismatch"
    assert snapshot["terminal_validation_summary"] == state.terminal_validation_summary
    assert snapshot["normalized_diagnostic_alias_count"] == 0


def test_live_snapshot_exposes_selected_repair_outcome_separately_from_pipeline():
    task = _task()
    action = replace(
        _stored(task).pipeline_repair_state.repair_actions[0],
        commit_sha="source-sha",
        validation_pipeline_id=31221,
        validation_status="failed",
        status="verified",
    )
    state = replace(
        _stored(task).pipeline_repair_state,
        final_pipeline_status="failed",
        repair_outcome="success",
        selected_categories=("format",),
        failed_job_names=("build_release_arm64",),
        repair_actions=(action,),
    )
    stored = replace(_stored(task, TaskStatus.COMPLETED), pipeline_repair_state=state)

    snapshot = _live_snapshot(stored, _binding(task), [], None)

    assert snapshot["repair_outcome"] == "success"
    assert snapshot["final_pipeline"]["status"] == "failed"
    assert snapshot["failed_job_names"] == ["build_release_arm64"]


def test_live_and_durable_blocker_snapshots_share_bounded_public_contract():
    task = _task()
    blocker = _dependency_blocker()
    state = replace(
        _stored(task).pipeline_repair_state,
        phase=PipelineRepairPhase.TERMINAL,
        final_pipeline_status="failed",
        repair_outcome="blocked",
        failed_job_names=("build_release_arm64",),
        blocker_type=blocker["type"],
        blocker_summary=blocker["summary"],
        blocker_suggested_action=blocker["suggested_action"],
        blocked_job_names=tuple(blocker["blocked_job_names"]),
        dependency_evidence=tuple(blocker["dependency_evidence"]),
        repair_actions=(),
    )
    stored = replace(_stored(task, TaskStatus.COMPLETED), pipeline_repair_state=state)

    live = _live_snapshot(stored, _binding(task), [], None)
    durable = _durable_snapshot({
        "extra": {
            "repair_report": {
                "terminal": True,
                "repair_outcome": "blocked",
                "blocker": blocker,
                "mr": {"url": task.pr_url},
                "final_pipeline": {"status": "failed"},
                "failed_job_names": ["build_release_arm64"],
                "final_file_changes": [],
            },
        },
    })

    assert live["repair_outcome"] == durable["repair_outcome"] == "blocked"
    assert live["blocker"] == durable["blocker"]
    assert live["blocker"]["type"] == "external_dependency"
    assert live["blocker"]["dependency_evidence"][0]["project_path"] == "eabot/lhotse"
    assert live["blocker"]["dependency_evidence"][0]["checked_branch_count"] == 300
    assert len(live["blocker"]["dependency_evidence"][0]["verified_candidates"]) == 5
    assert "raw_log" not in str(live["blocker"])
    assert "unknown" not in str(live["blocker"])
    assert "javascript:" not in str(live["blocker"])
    assert live["final_pipeline"] is None
    assert live["evidence_pipeline"]["status"] == "failed"
    assert live["final_file_changes"] == []


def test_live_snapshot_exposes_auto_rollback_without_changing_repair_failure():
    task = _task()
    state = replace(
        _stored(task).pipeline_repair_state,
        phase=PipelineRepairPhase.TERMINAL,
        final_pipeline_status="failed",
        repair_outcome="failed",
    )
    rollback = RepairRollbackState(
        rollback_task_id="rollback-task",
        repair_task_id=task.task_id,
        status=RepairRollbackStatus.SUCCEEDED,
        trigger="auto_failure",
        requested_by="user-1",
        expected_remote_head="c" * 40,
        manifest_digest="digest",
        rollback_commit_sha="d" * 40,
    )
    stored = replace(
        _stored(task, TaskStatus.COMPLETED),
        pipeline_repair_state=state,
        repair_rollback_state=rollback,
    )

    snapshot = _live_snapshot(stored, _binding(task), [], None)

    assert snapshot["repair_outcome"] == "failed"
    assert snapshot["rollback"]["trigger"] == "auto_failure"
    assert snapshot["rollback"]["status"] == "succeeded"


def test_durable_snapshot_preserves_final_coverage_source():
    snapshot = _durable_snapshot({
        "task_id": "task-coverage",
        "success": 1,
        "final_pipeline_status": "success",
        "final_coverage": 63.04,
        "pushed_sha": "repair-sha",
        "extra": {
            "final_pipeline_id": 33334,
            "coverage_source": "changed_lines",
            "coverage_status": "reported",
        },
    })

    assert snapshot["final_pipeline"] == {
        "id": 33334,
        "sha": "repair-sha",
        "status": "success",
        "coverage": 63.04,
        "coverage_source": "changed_lines",
        "coverage_status": "reported",
    }


def test_live_snapshot_exposes_source_job_navigation():
    task = _task()
    state = replace(
        _stored(task).pipeline_repair_state,
        source_failure_explanations=(FailureExplanation(
            job_name="build_release_arm64",
            job_id=105279,
            job_url="https://gitlab.example/eabot/cook/-/jobs/105279",
            trace_line=1837,
        ),),
        failure_explanations=(),
    )
    stored = replace(_stored(task), pipeline_repair_state=state)

    snapshot = _live_snapshot(stored, _binding(task), [], None)

    assert snapshot["source_jobs"][0]["trace_line"] == 1837


def test_terminal_repair_with_queued_report_is_not_settled():
    task = _task()
    stored = replace(
        _stored(task, TaskStatus.COMPLETED),
        final_repair_report_state=FinalRepairReportState(
            RepairReportStatus.QUEUED,
            report_task_id="report-12345678",
        ),
    )
    snapshot = _live_snapshot(stored, _binding(task), [], None)
    assert snapshot["terminal"] is True
    assert snapshot["report"]["status"] == "queued"
    assert _snapshot_is_settled(snapshot) is False


def test_durable_snapshot_keeps_new_report_source_jobs():
    snapshot = _durable_snapshot({"extra": {"repair_report": {
        "terminal": True,
        "mr": {"url": ""},
        "source_jobs": [{
            "job_name": "build",
            "job_url": "https://gitlab.example/eabot/cook/-/jobs/12",
            "job_id": 12,
            "trace_line": 27,
        }],
    }}})

    assert snapshot["source_jobs"][0]["trace_line"] == 27


def test_durable_snapshot_backfills_legacy_report_from_failure_explanations():
    snapshot = _durable_snapshot({"extra": {
        "repair_report": {"terminal": True, "mr": {"url": ""}},
        "failure_explanations": [{
            "job_name": "build",
            "job_url": "https://gitlab.example/eabot/cook/-/jobs/12",
            "job_id": 12,
        }],
    }})

    assert snapshot["source_jobs"] == [{
        "job_name": "build",
        "job_id": 12,
        "job_url": "https://gitlab.example/eabot/cook/-/jobs/12",
        "trace_line": 0,
    }]


def test_durable_snapshot_prefers_source_failures_over_empty_current_failures():
    snapshot = _durable_snapshot({"extra": {
        "repair_report": {"terminal": True, "mr": {"url": ""}},
        "source_failure_explanations": [{
            "job_name": "build",
            "job_url": "https://gitlab.example/eabot/cook/-/jobs/12",
            "job_id": 12,
            "trace_line": 27,
        }],
        "failure_explanations": [],
    }})

    assert snapshot["source_jobs"][0]["trace_line"] == 27


def test_model_report_and_final_compare_diff_survive_durable_reload():
    value = FinalRepairReportInput(
        repair_task_id="task-12345678",
        project_id="eabot/cook",
        mr_iid=536,
        pr_url="https://gitlab.example/mr/536",
        source_pipeline_id=1,
        source_sha="a" * 40,
        base_sha="a" * 40,
        final_sha="b" * 40,
        final_pipeline_id=2,
        final_pipeline_status="success",
        final_coverage=None,
        selected_categories=("build",),
        failed_jobs=("build",),
        causal_lines=("error: build failed",),
        diffs=(FinalRepairDiff("src/a.cpp", "modified", 1, 1, "@@ -1 +1 @@\n-old\n+new"),),
    )
    report = FinalRepairReport(
        "build failed",
        "updated value",
        "pipeline passed",
        (FinalFileExplanation("src/a.cpp", "updated value", ("new",)),),
        "model",
    )
    report_state = FinalRepairReportState(
        RepairReportStatus.MODEL_GENERATED,
        input_digest=value.digest(),
        report=report,
    )
    durable = _durable_snapshot({
        "task_id": value.repair_task_id,
        "extra": {"repair_report": {
            "terminal": True,
            "report": report_state.to_public_dict(),
            "final_file_changes": [{"path": "src/a.cpp"}],
        }},
    })
    assert durable["report"]["source"] == "model"
    assert durable["final_file_changes"][0]["path"] == "src/a.cpp"


def test_snapshot_falls_back_to_durable_terminal_report(monkeypatch):
    broker = AsyncMock()
    broker.get_task.return_value = None
    task_id = "task-87654321"
    durable = {
        "task_id": task_id,
        "extra": {
            "repair_report": {
                "schema_version": 1,
                "task_id": task_id,
                "source": "durable",
                "status": "completed",
                "phase": "terminal",
                "mr": {"project": "eabot/cook", "iid": 536, "title": "fallback", "url": ""},
                "actions": [],
                "progress": [],
            }
        },
    }
    client = _client(monkeypatch, broker)
    signature = sign_repair_details_task(task_id)

    with patch("pr_agent.servers.repair_results.get_triage_run_task", return_value=durable):
        response = client.get(f"/api/repair-results/{task_id}?sig={signature}")

    assert response.status_code == 200
    assert response.json()["source"] == "durable"
    assert response.json()["mr"]["title"] == "fallback"


def test_unknown_signed_task_returns_404(monkeypatch):
    broker = AsyncMock()
    broker.get_task.return_value = None
    task_id = "task-00000000"
    client = _client(monkeypatch, broker)
    signature = sign_repair_details_task(task_id)

    with patch("pr_agent.servers.repair_results.get_triage_run_task", return_value=None):
        response = client.get(f"/api/repair-results/{task_id}?sig={signature}")

    assert response.status_code == 404


def test_owner_page_has_accessible_live_regions_and_polling_fallback():
    html = _repair_result_html("task-12345678", "safe-signature")

    assert '<meta name="viewport"' in html
    assert "<main" in html
    assert 'aria-live="polite"' in html
    assert "new EventSource" in html
    assert "setInterval" in html
    assert "prefers-reduced-motion: reduce" in html
    assert "CI 修复报告" in html
    assert "失败原因" in html
    assert "修复方案" in html
    assert "代码改动" in html
    assert "验证结果" in html
    assert "运行记录" in html
    assert "并排" in html
    assert "统一" in html
    assert "修复动作" not in html
    assert "实时进度" not in html
    assert "textContent" in html
    assert "createElement('details')" in html
    assert "min-height: 44px" in html
    assert "https://cdn" not in html
    assert "http://cdn" not in html


def test_progress_deduplication_collapses_only_repeated_activity():
    progress = [
        {"phase": "diagnosing", "summary": "正在读取相关源码", "event_id": "1-0"},
        {"phase": "diagnosing", "summary": "正在读取相关源码", "event_id": "2-0"},
        {"phase": "editing", "summary": "正在应用代码修复", "event_id": "3-0"},
        {"phase": "committing", "summary": "修复提交已推送", "event_id": "4-0"},
        {"phase": "committing", "summary": "修复提交已推送", "event_id": "5-0"},
    ]

    collapsed = _deduplicate_progress(progress)

    assert [(item["summary"], item["count"]) for item in collapsed] == [
        ("正在读取相关源码", 2),
        ("正在应用代码修复", 1),
        ("修复提交已推送", 1),
        ("修复提交已推送", 1),
    ]


def test_owner_page_route_has_private_security_headers(monkeypatch):
    task = _task()
    broker = AsyncMock()
    broker.get_task.return_value = _stored(task)
    broker.get_task_triage_card.return_value = _binding(task)
    broker.get_repair_progress.return_value = []
    client = _client(monkeypatch, broker)
    signature = sign_repair_details_task(task.task_id)

    response = client.get(f"/repair-results/{task.task_id}?sig={signature}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_repair_result_page_supports_signed_embedded_mode(monkeypatch):
    task = _task()
    broker = AsyncMock()
    broker.get_task.return_value = _stored(task)
    broker.get_task_triage_card.return_value = _binding(task)
    broker.get_repair_progress.return_value = []
    broker.get_final_repair_report_input.return_value = None
    client = _client(monkeypatch, broker)
    signature = sign_repair_details_task(task.task_id)

    response = client.get(f"/repair-results/{task.task_id}?sig={signature}&embed=1")

    assert response.status_code == 200
    assert '<body class="embedded">' in response.text
    assert "repair-detail-height" in response.text
    assert "ResizeObserver" in response.text
    assert "window.location.origin" in response.text


def test_standalone_repair_result_page_keeps_normal_chrome(monkeypatch):
    task = _task()
    broker = AsyncMock()
    broker.get_task.return_value = _stored(task)
    broker.get_task_triage_card.return_value = _binding(task)
    broker.get_repair_progress.return_value = []
    broker.get_final_repair_report_input.return_value = None
    client = _client(monkeypatch, broker)
    signature = sign_repair_details_task(task.task_id)

    response = client.get(f"/repair-results/{task.task_id}?sig={signature}")

    assert response.status_code == 200
    assert '<body class="standalone">' in response.text
    assert "CI 修复报告" in response.text
