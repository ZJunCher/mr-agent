import asyncio
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from pr_agent.distributed.broker import StoredTask
from pr_agent.distributed.models import (
    MrKey,
    PipelineEvent,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.triage.failure_categories import pipeline_repair_item
from pr_agent.triage.failure_explanations import FailureExplanation
from pr_agent.triage.pipeline_repair import PipelineRepairPhase, PipelineRepairState
from pr_agent.triage.repair_details import RepairAction
from pr_agent.triage.repair_outcome import CategoryRepairOutcome, CategoryRepairResult
from pr_agent.triage.repair_rollback import (
    RepairCommitEntry,
    RepairCommitManifest,
    RepairRollbackState,
    RepairRollbackStatus,
)
from pr_agent.triage.store import get_triage_run_task, save_triage_run, update_triage_run_repair_report


def _task() -> TaskEnvelope:
    return TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="feishu",
        mr=MrKey("eabot/cook", 546),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/546",
        command="/repair-pipeline",
        payload={
            "source_pipeline_id": 30385,
            "source_pipeline_sha": "dc78f383eb6b",
        },
        idempotency_key="repair:546:30385",
    )


def _direct_task(command: str) -> TaskEnvelope:
    task = _task()
    return replace(
        task,
        command=command,
        payload={
            "sender_id": "ou_actor",
            "source_pipeline_id": 30385,
            "source_pipeline_sha": "dc78f383eb6b",
        },
    )


def _binding(task: TaskEnvelope, state: TriageCardState) -> TriageCardBinding:
    return replace(
        TriageCardBinding.new(
            card_id="card-546",
            task_id=task.task_id,
            open_message_id="message-546",
            receive_id="user-1",
            mr_url=task.pr_url,
            project_id=task.mr.project_id,
            mr_iid=task.mr.iid,
            mr_title="repair regression",
            mr_author_username="jun.zhao",
            source_branch="feature/repair",
            pipeline_id=30385,
            pipeline_sha="dc78f383eb6b",
            original_markdown="pipeline failed",
            repair_items=(pipeline_repair_item(30385, "dc78f383eb6b"),),
            failed_job_names=("build_release_arm64", "clang_tidy_check"),
        ),
        state=state,
        current_pipeline_id=30391,
        current_pipeline_sha="ccf6ebb7",
    )


def _stored(task: TaskEnvelope, status: TaskStatus, *, pipeline_status: str, error: str = "") -> StoredTask:
    source_explanation = FailureExplanation(
        job_name="build_release_arm64",
        job_url="https://gitlab.example/eabot/cook/-/jobs/105279",
        confirmed_reason="fatal error: missing.hpp",
        job_id=105279,
        trace_line=1837,
        confidence="confirmed",
    )
    return StoredTask(
        envelope=task,
        status=status,
        attempt=0,
        worker_id="agent-1",
        fencing_token=7,
        result="",
        error=error,
        pipeline_repair_state=PipelineRepairState(
            phase=PipelineRepairPhase.TERMINAL,
            completed_steps=("诊断修复已完成",),
            latest_pipeline_id=30391,
            latest_pipeline_sha="ccf6ebb7",
            final_pipeline_status=pipeline_status,
            final_coverage=63.04,
            final_coverage_source="changed_lines",
            final_coverage_status="reported",
            failed_job_names=() if pipeline_status == "success" else ("build_release_arm64",),
            terminal_error=error,
            iterations=12,
            max_iterations=30,
            source_failure_explanations=(source_explanation,),
            failure_explanations=() if pipeline_status == "success" else (source_explanation,),
            repair_actions=(
                RepairAction.from_dict({
                    "action_id": "build-root",
                    "categories": ["build"],
                    "root_cause": "fatal error: missing.hpp",
                    "measures": ["补充缺失的头文件依赖。"],
                    "changed_files": ["src/a.cpp"],
                    "commit_sha": "ccf6ebb7",
                    "validation_pipeline_id": 30391,
                    "validation_status": pipeline_status,
                    "status": "verified" if pipeline_status == "success" else "failed",
                }),
            ),
        ),
        created_at=100.0,
        updated_at=223.4,
    )


def test_successful_outer_repair_persists_latest_pipeline_once(tmp_path):
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _task()
        broker = AsyncMock()
        broker.get_task.return_value = _stored(task, TaskStatus.COMPLETED, pipeline_status="success")
        broker.get_task_triage_card.return_value = _binding(task, TriageCardState.REPAIR_SUCCEEDED)
        db_path = str(tmp_path / "triage.db")

        with patch(
            "pr_agent.triage.terminal.save_triage_run",
            side_effect=lambda record: save_triage_run(record, path=db_path),
        ):
            assert await persist_repair_terminal(broker, task.task_id) is True
            assert await persist_repair_terminal(broker, task.task_id) is True

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM triage_runs WHERE task_id = ?", (task.task_id,)).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["success"] == 1
        assert rows[0]["pipeline_id"] == "30385"
        assert rows[0]["pushed_sha"] == "ccf6ebb7"
        assert rows[0]["final_pipeline_status"] == "success"
        assert rows[0]["final_coverage"] == 63.04
        assert rows[0]["iterations"] == 12
        assert rows[0]["max_iterations"] == 30
        extra = __import__("json").loads(rows[0]["extra_json"])
        assert extra["selected_categories"] == []
        assert extra["effective_categories"] == []
        assert extra["coverage_source"] == "changed_lines"
        assert extra["coverage_status"] == "reported"
        assert extra["failure_explanations"] == []
        assert extra["source_failure_explanations"][0]["job_name"] == "build_release_arm64"
        assert extra["source_failure_explanations"][0]["confirmed_reason"] == "fatal error: missing.hpp"
        assert extra["source_failure_explanations"][0]["trace_line"] == 1837
        assert extra["repair_report"]["source_jobs"][0]["job_id"] == 105279
        assert extra["repair_report"]["actions"][0]["changed_files"] == ["src/a.cpp"]
        assert extra["repair_report"]["schema_version"] == 2
        assert extra["repair_report"]["final_pipeline"]["id"] == 30391
        assert extra["repair_report"]["final_pipeline"]["coverage_source"] == "changed_lines"
        assert extra["repair_report"]["final_pipeline"]["coverage_status"] == "reported"
        broker.record_triage_persistence.assert_awaited()

    asyncio.run(run_test())


def test_failed_repair_without_commit_persists_pipeline_as_evidence_only():
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _task()
        stored = _stored(task, TaskStatus.FAILED, pipeline_status="failed", error="模型服务暂时不可用")
        stored = replace(
            stored,
            pipeline_repair_state=replace(stored.pipeline_repair_state, repair_actions=()),
        )
        broker = AsyncMock()
        broker.get_task.return_value = stored
        broker.get_task_triage_card.return_value = _binding(task, TriageCardState.REPAIR_FAILED)
        broker.get_repair_progress.return_value = []
        saved = []

        with patch("pr_agent.triage.terminal.save_triage_run", side_effect=lambda record: saved.append(record) or True):
            assert await persist_repair_terminal(broker, task.task_id) is True

        assert saved[0]["pushed_sha"] == ""
        assert saved[0]["final_pipeline_status"] == "unknown"
        assert saved[0]["extra"]["final_pipeline_id"] is None
        assert saved[0]["extra"]["evidence_pipeline_id"] == 30391
        assert saved[0]["extra"]["repair_report"]["final_pipeline"] is None
        assert saved[0]["extra"]["repair_report"]["evidence_pipeline"]["id"] == 30391

    asyncio.run(run_test())


def test_partial_repair_persists_as_failed_metric_with_distinct_outcome(tmp_path):
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _task()
        stored = _stored(task, TaskStatus.COMPLETED, pipeline_status="failed")
        stored = replace(
            stored,
            pipeline_repair_state=replace(
                stored.pipeline_repair_state,
                selected_categories=("format", "clang"),
                repair_outcome="partial_success",
                category_results=(
                    CategoryRepairResult("format", CategoryRepairOutcome.SUCCEEDED, "selected"),
                    CategoryRepairResult("clang", CategoryRepairOutcome.FAILED, "selected"),
                ),
            ),
        )
        broker = AsyncMock()
        broker.get_task.return_value = stored
        broker.get_task_triage_card.return_value = _binding(task, TriageCardState.REPAIR_PARTIAL)
        db_path = str(tmp_path / "triage-partial.db")

        with patch(
            "pr_agent.triage.terminal.save_triage_run",
            side_effect=lambda record: save_triage_run(record, path=db_path),
        ):
            assert await persist_repair_terminal(broker, task.task_id) is True

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM triage_runs WHERE task_id = ?", (task.task_id,)).fetchone()
        conn.close()
        assert row["success"] == 0
        assert row["repair_outcome"] == "partial_success"
        assert row["final_pipeline_status"] == "failed"
        assert __import__("json").loads(row["failure_categories"]) == ["format", "clang"]

    asyncio.run(run_test())


def test_blocked_repair_persists_dependency_evidence_idempotently(tmp_path):
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _task()
        stored = _stored(task, TaskStatus.COMPLETED, pipeline_status="failed")
        stored = replace(
            stored,
            pipeline_repair_state=replace(
                stored.pipeline_repair_state,
                selected_categories=("build",),
                repair_outcome="blocked",
                category_results=(
                    CategoryRepairResult("build", CategoryRepairOutcome.BLOCKED, "selected"),
                ),
                blocker_type="external_dependency",
                blocker_summary="当前声明分支缺少接口。",
                blocker_suggested_action="请维护者确认候选依赖分支。",
                blocked_job_names=("build_release_arm64",),
                dependency_evidence=({
                    "project_path": "eabot/lhotse",
                    "declared_branch": "dev",
                    "declared_sha": "dev-sha",
                },),
            ),
        )
        broker = AsyncMock()
        broker.get_task.return_value = stored
        broker.get_task_triage_card.return_value = _binding(task, TriageCardState.REPAIR_BLOCKED)
        db_path = str(tmp_path / "triage-blocked.db")

        with patch(
            "pr_agent.triage.terminal.save_triage_run",
            side_effect=lambda record: save_triage_run(record, path=db_path),
        ), patch("ut_agent.repair_memory.outcomes.settle_without_validation") as settle:
            assert await persist_repair_terminal(broker, task.task_id) is True
            assert await persist_repair_terminal(broker, task.task_id) is True

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM triage_runs WHERE task_id = ?", (task.task_id,)).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["success"] == 0
        assert rows[0]["repair_outcome"] == "blocked"
        assert rows[0]["finish_reason"] == "external_dependency_blocked"
        extra = __import__("json").loads(rows[0]["extra_json"])
        assert extra["blocker_type"] == "external_dependency"
        assert extra["blocked_job_names"] == ["build_release_arm64"]
        assert extra["dependency_evidence"][0]["project_path"] == "eabot/lhotse"
        assert extra["repair_report"]["blocker"]["suggested_action"] == "请维护者确认候选依赖分支。"
        assert all(call.args[1] == "external_dependency_blocked" for call in settle.call_args_list)

    asyncio.run(run_test())


def test_model_unavailable_repair_persists_distinct_finish_reason(tmp_path):
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _task()
        stored = _stored(
            task,
            TaskStatus.COMPLETED,
            pipeline_status="failed",
            error="模型服务暂时不可用；已尝试全部模型。",
        )
        stored = replace(
            stored,
            pipeline_repair_state=replace(
                stored.pipeline_repair_state,
                repair_actions=(),
                repair_outcome="failed",
                terminal_failure_kind="provider_unavailable",
                terminal_validation_error_code="diagnostic_identity_mismatch",
                terminal_validation_summary="缺少 16 条诊断身份，存在 16 条未知身份。",
                normalized_diagnostic_alias_count=4,
            ),
        )
        broker = AsyncMock()
        broker.get_task.return_value = stored
        broker.get_task_triage_card.return_value = _binding(task, TriageCardState.REPAIR_MODEL_UNAVAILABLE)
        db_path = str(tmp_path / "triage-model-unavailable.db")

        with patch(
            "pr_agent.triage.terminal.save_triage_run",
            side_effect=lambda record: save_triage_run(record, path=db_path),
        ):
            assert await persist_repair_terminal(broker, task.task_id) is True

        row = get_triage_run_task(task.task_id, path=db_path)
        assert row is not None
        assert row["finish_reason"] == "model_service_unavailable"
        assert row["extra"]["terminal_failure_kind"] == "provider_unavailable"
        assert row["extra"]["terminal_validation_error_code"] == "diagnostic_identity_mismatch"
        assert row["extra"]["terminal_validation_summary"] == "缺少 16 条诊断身份，存在 16 条未知身份。"
        assert row["extra"]["normalized_diagnostic_alias_count"] == 4

    asyncio.run(run_test())


def test_terminal_persistence_admits_one_final_report_when_enabled():
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _task()
        stored = _stored(task, TaskStatus.COMPLETED, pipeline_status="success")
        manifest = RepairCommitManifest(
            repair_task_id=task.task_id,
            project_id=task.mr.project_id,
            mr_iid=task.mr.iid,
            source_branch="feature/repair",
            base_commit_sha="a" * 40,
            base_tree_sha="b" * 40,
            authorized_actor_id="user-1",
            entries=(RepairCommitEntry(1, "c" * 40, "a" * 40, "d" * 40, "effect", "marker", "now"),),
            frozen=True,
            frozen_at="now",
        )
        broker = AsyncMock()
        broker.get_task.return_value = replace(stored, repair_commit_manifest=manifest)
        broker.get_task_triage_card.return_value = _binding(task, TriageCardState.REPAIR_SUCCEEDED)
        with (
            patch("pr_agent.triage.terminal.save_triage_run", return_value=True),
            patch("pr_agent.triage.final_repair_report.final_repair_report_enabled", return_value=True),
        ):
            assert await persist_repair_terminal(broker, task.task_id) is True
        broker.admit_final_repair_report.assert_awaited_once_with(task.task_id)

    asyncio.run(run_test())


def test_terminal_report_persists_source_jobs():
    from pr_agent.triage.terminal import _repair_report

    async def run_test():
        task = _task()
        original = _stored(task, TaskStatus.COMPLETED, pipeline_status="success")
        state = replace(
            original.pipeline_repair_state,
            failure_explanations=(FailureExplanation(
                job_name="build_release_arm64",
                job_id=105279,
                job_url="https://gitlab.example/eabot/cook/-/jobs/105279",
                trace_line=1837,
            ),),
        )
        stored = replace(
            original,
            pipeline_repair_state=state,
        )
        broker = AsyncMock()
        broker.get_repair_progress.return_value = []
        broker.get_final_repair_report_input.return_value = None

        report = await _repair_report(
            broker,
            task.task_id,
            stored,
            _binding(task, TriageCardState.REPAIR_SUCCEEDED),
            "success",
        )

        assert report["source_job_names"] == ["build_release_arm64"]
        assert report["source_jobs"] == [{
            "job_name": "build_release_arm64",
            "job_id": 105279,
            "job_url": "https://gitlab.example/eabot/cook/-/jobs/105279",
            "trace_line": 1837,
        }]

    asyncio.run(run_test())


def test_existing_detailed_triage_row_merges_owner_report_without_replacement(tmp_path):
    db_path = str(tmp_path / "triage.db")
    task_id = "task-existing-triage"
    assert save_triage_run({
        "task_id": task_id,
        "project": "eabot/cook",
        "mr_iid": 536,
        "iterations": 12,
        "extra": {"pipeline_groups": [{"validation_pipeline_id": 30391}]},
    }, path=db_path)

    assert update_triage_run_repair_report(
        task_id,
        {"schema_version": 1, "actions": [{"changed_files": ["src/a.cpp"]}]},
        path=db_path,
    )

    stored = get_triage_run_task(task_id, path=db_path)
    assert stored["iterations"] == 12
    assert stored["extra"]["pipeline_groups"] == [{"validation_pipeline_id": 30391}]
    assert stored["extra"]["repair_report"]["actions"][0]["changed_files"] == ["src/a.cpp"]


def test_auto_failure_rollback_audit_does_not_rewrite_failed_repair_metrics():
    from pr_agent.triage.terminal import persist_repair_rollback

    async def run_test():
        task = _task()
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
        broker = AsyncMock()
        broker.get_task.return_value = replace(
            _stored(task, TaskStatus.COMPLETED, pipeline_status="failed"),
            repair_rollback_state=rollback,
        )
        original = {
            "task_id": task.task_id,
            "success": 0,
            "repair_outcome": "failed",
            "extra": {"repair_report": {"repair_outcome": "failed"}},
        }
        captured = []
        with (
            patch("pr_agent.triage.store.get_triage_run_task", return_value=original),
            patch(
                "pr_agent.triage.terminal.update_triage_run_repair_report",
                side_effect=lambda _task_id, report: captured.append(report) or True,
            ),
        ):
            assert await persist_repair_rollback(broker, task.task_id) is True

        assert original["success"] == 0
        assert original["repair_outcome"] == "failed"
        assert captured[0]["repair_outcome"] == "failed"
        assert captured[0]["rollback"]["trigger"] == "auto_failure"
        assert captured[0]["rollback"]["status"] == "succeeded"
        assert captured[0]["rollback"]["rollback_commit_sha"] == "d" * 40

    asyncio.run(run_test())


def test_queue_completion_with_failed_card_is_business_failure():
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _task()
        broker = AsyncMock()
        broker.get_task.return_value = _stored(task, TaskStatus.COMPLETED, pipeline_status="failed")
        broker.get_task_triage_card.return_value = _binding(task, TriageCardState.REPAIR_FAILED)
        saved = []
        with patch("pr_agent.triage.terminal.save_triage_run", side_effect=lambda record: saved.append(record) or True):
            assert await persist_repair_terminal(broker, task.task_id) is True

        assert saved[0]["success"] == 0
        assert saved[0]["final_pipeline_status"] == "failed"
        assert saved[0]["failed_job_names"] == ["build_release_arm64", "clang_tidy_check"]

    asyncio.run(run_test())


def test_canceled_outer_repair_persists_terminal_reason():
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _task()
        broker = AsyncMock()
        broker.get_task.return_value = _stored(
            task,
            TaskStatus.CANCELED,
            pipeline_status="canceled",
            error="用户取消修复",
        )
        broker.get_task_triage_card.return_value = _binding(task, TriageCardState.CANCELED)
        saved = []
        with patch("pr_agent.triage.terminal.save_triage_run", side_effect=lambda record: saved.append(record) or True):
            assert await persist_repair_terminal(broker, task.task_id) is True

        assert saved[0]["success"] == 0
        assert saved[0]["finish_reason"] == "canceled"
        assert saved[0]["error"] == "用户取消修复"

    asyncio.run(run_test())


def test_pre_diagnosis_outer_repair_persists_zero_iterations():
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _task()
        broker = AsyncMock()
        stored = _stored(task, TaskStatus.CANCELED, pipeline_status="canceled", error="用户取消修复")
        broker.get_task.return_value = replace(stored, pipeline_repair_state=PipelineRepairState())
        broker.get_task_triage_card.return_value = _binding(task, TriageCardState.CANCELED)
        saved = []
        with patch("pr_agent.triage.terminal.save_triage_run", side_effect=lambda record: saved.append(record) or True):
            assert await persist_repair_terminal(broker, task.task_id) is True

        assert saved[0]["iterations"] == 0
        assert saved[0]["max_iterations"] == 0

    asyncio.run(run_test())


def test_outer_repair_persists_gitlab_author_without_feishu_lookup():
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _task()
        broker = AsyncMock()
        broker.get_task.return_value = _stored(task, TaskStatus.COMPLETED, pipeline_status="success")
        broker.get_task_triage_card.return_value = replace(
            _binding(task, TriageCardState.REPAIR_SUCCEEDED),
            mr_author_username="xiaoyu.li",
        )
        saved = []
        with patch(
            "pr_agent.triage.terminal.save_triage_run",
            side_effect=lambda record: saved.append(record) or True,
        ), patch(
            "pr_agent.feishu.feishu_client.FeishuClient.get_user_display_name",
            AsyncMock(side_effect=AssertionError("Feishu lookup must not run")),
        ):
            assert await persist_repair_terminal(broker, task.task_id) is True

        assert saved[0]["task_id"] == task.task_id
        assert saved[0]["mr_author"] == "xiaoyu.li"
        assert saved[0]["feishu_user_name"] is None
        assert saved[0]["pipeline_id"] == 30385

    asyncio.run(run_test())


def test_direct_triage_preserves_the_detailed_pr_triage_row():
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _direct_task("/triage")
        broker = AsyncMock()
        broker.get_task.return_value = _stored(task, TaskStatus.COMPLETED, pipeline_status="failed")
        broker.get_task_triage_card.return_value = None
        with patch(
            "pr_agent.triage.terminal.has_triage_run_task",
            return_value=True,
        ) as exists, patch(
            "pr_agent.triage.terminal.save_triage_run",
            side_effect=AssertionError("must not replace detailed /triage row"),
        ) as save, patch(
            "pr_agent.triage.terminal.update_triage_run_repair_report",
            return_value=True,
        ) as update:
            assert await persist_repair_terminal(broker, task.task_id) is True

        exists.assert_called_once_with(task.task_id)
        save.assert_not_called()
        update.assert_called_once()
        assert update.call_args.args[0] == task.task_id
        assert update.call_args.args[1]["actions"][0]["changed_files"] == ["src/a.cpp"]

    asyncio.run(run_test())


def test_direct_fix_format_persists_gitlab_author_without_feishu_actor():
    from pr_agent.triage.terminal import persist_repair_terminal

    async def run_test():
        task = _direct_task("/fix_format")
        broker = AsyncMock()
        broker.get_task.return_value = _stored(task, TaskStatus.FAILED, pipeline_status="error")
        broker.get_task_triage_card.return_value = _binding(task, TriageCardState.REPAIR_FAILED)
        saved = []
        with patch(
            "pr_agent.triage.terminal.save_triage_run",
            side_effect=lambda record: saved.append(record) or True,
        ):
            assert await persist_repair_terminal(broker, task.task_id, error="lookup failed") is True

        assert saved[0]["mr_author"] == "jun.zhao"
        assert saved[0]["feishu_user_name"] is None
        assert saved[0]["failure_categories"] == ["format"]
        assert saved[0]["error"] == "lookup failed"

    asyncio.run(run_test())


def test_late_success_corrects_only_the_exact_latest_repair_sha():
    from pr_agent.triage.terminal import reconcile_late_repair_success

    async def run_test():
        task = _task()
        stored = _stored(
            task,
            TaskStatus.FAILED,
            pipeline_status="error",
            error="修复子进程心跳超时，任务已自动结束",
        )
        stored = replace(
            stored,
            pipeline_repair_state=replace(
                stored.pipeline_repair_state,
                phase=PipelineRepairPhase.TRIAGE_WAITING,
                final_pipeline_status="",
                latest_pipeline_sha="ccf6ebb7",
            ),
        )
        binding = _binding(task, TriageCardState.REPAIR_FAILED)
        broker = AsyncMock()
        broker.list_terminal_repair_candidates.return_value = [stored]
        broker.get_task_triage_card.return_value = binding
        broker.correct_late_repair_terminal.return_value = True
        event = PipelineEvent.new(
            project_id="eabot/cook",
            pipeline_id=30391,
            sha="ccf6ebb7",
            status="success",
            ref="feature/repair",
        )

        with patch("pr_agent.triage.terminal.persist_repair_terminal", return_value=True) as persist:
            assert await reconcile_late_repair_success(broker, event) == [task.task_id]

        correction = broker.correct_late_repair_terminal.await_args.kwargs
        corrected = correction["terminal_state"]
        assert corrected.phase is PipelineRepairPhase.TERMINAL
        assert corrected.final_pipeline_status == "success"
        assert correction["expected_task_status"] is TaskStatus.FAILED
        assert correction["repair_items"][0].failure_explanations == ()
        broker.enqueue_notification.assert_awaited_once()
        persist.assert_awaited_once_with(broker, task.task_id)

        unrelated = replace(event, sha="user-commit")
        broker.list_terminal_repair_candidates.return_value = [stored]
        assert await reconcile_late_repair_success(broker, unrelated) == []

    asyncio.run(run_test())


def test_late_success_corrects_completed_business_failure():
    from pr_agent.triage.terminal import reconcile_late_repair_success

    async def run_test():
        task = _task()
        stored = _stored(task, TaskStatus.COMPLETED, pipeline_status="failed")
        binding = _binding(task, TriageCardState.PIPELINE_FAILED)
        broker = AsyncMock()
        broker.list_terminal_repair_candidates.return_value = [stored]
        broker.get_task_triage_card.return_value = binding
        broker.correct_late_repair_terminal.return_value = True
        event = PipelineEvent.new(
            project_id="eabot/cook",
            pipeline_id=30391,
            sha="ccf6ebb7",
            status="success",
            ref="feature/repair",
        )

        with patch("pr_agent.triage.terminal.persist_repair_terminal", return_value=True):
            assert await reconcile_late_repair_success(broker, event) == [task.task_id]

        assert broker.correct_late_repair_terminal.await_args.kwargs["expected_task_status"] is TaskStatus.COMPLETED

    asyncio.run(run_test())


def test_late_success_rejects_wrong_pipeline_id_even_when_sha_matches():
    from pr_agent.triage.terminal import reconcile_late_repair_success

    async def run_test():
        task = _task()
        stored = _stored(task, TaskStatus.COMPLETED, pipeline_status="failed")
        broker = AsyncMock()
        broker.list_terminal_repair_candidates.return_value = [stored]
        event = PipelineEvent.new(
            project_id="eabot/cook",
            pipeline_id=99999,
            sha="ccf6ebb7",
            status="success",
            ref="feature/repair",
        )

        assert await reconcile_late_repair_success(broker, event) == []

        broker.get_task_triage_card.assert_not_awaited()
        broker.correct_late_repair_terminal.assert_not_awaited()

    asyncio.run(run_test())
