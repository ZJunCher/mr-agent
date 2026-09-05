import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pr_agent.distributed.broker import StoredTask
from pr_agent.distributed.executor import TaskExecutor
from pr_agent.distributed.models import (
    MrKey,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.feishu.feishu_git_provider import FeishuGitProvider
from pr_agent.servers.repair_results import _durable_snapshot, _live_snapshot
from pr_agent.triage.failure_categories import pipeline_repair_item
from pr_agent.triage.pipeline_coverage import CoverageResult
from pr_agent.triage.pipeline_repair import (
    CoverageContinuationPhase,
    PipelineRepairPhase,
    PipelineRepairState,
)
from pr_agent.triage.repair_details import RepairAction


class Collection:
    def __init__(self, values=()):
        self.values = list(values)

    def list(self, **_kwargs):
        return list(self.values)


class PipelineCollection(Collection):
    def __init__(self, values):
        super().__init__(values)
        self.by_id = {value.id: value for value in values}

    def get(self, pipeline_id):
        return self.by_id[int(pipeline_id)]


def _job(job_id: int, name: str):
    return SimpleNamespace(id=job_id, name=name, status="success", attributes={
        "id": job_id,
        "name": name,
        "status": "success",
    })


def _pipeline(pipeline_id: int, jobs, *, source: str, downstream=()):
    return SimpleNamespace(
        id=pipeline_id,
        sha="final-sha",
        status="success",
        source=source,
        coverage=None,
        attributes={"coverage": None},
        jobs=Collection(jobs),
        bridges=Collection([
            SimpleNamespace(downstream_pipeline={"id": child.id})
            for child in downstream
        ]),
    )


def _task_and_binding():
    task = TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="feishu",
        mr=MrKey("example/service", 549),
        pr_url="https://gitlab.example/example/service/-/merge_requests/549",
        command="/repair-pipeline",
        payload={"source_pipeline_id": 33333, "source_pipeline_sha": "source-sha"},
        idempotency_key="repair-coverage-process",
    )
    task = replace(task, task_id="task-coverage-process")
    item = replace(pipeline_repair_item(33333, "source-sha"), task_id=task.task_id)
    binding = TriageCardBinding.new(
        card_id="card-coverage",
        task_id=task.task_id,
        open_message_id="message-coverage",
        receive_id="user-coverage",
        mr_url=task.pr_url,
        project_id="example/service",
        mr_iid=549,
        mr_title="Repair coverage fixture",
        source_branch="fix/coverage",
        pipeline_id=33333,
        pipeline_sha="source-sha",
        original_markdown="pipeline failed",
        repair_items=(item,),
    )
    return task, binding


def test_changed_lines_coverage_survives_the_complete_repair_terminal_path(monkeypatch):
    coverage_job = _job(107440, "x86_64_ut_coverage_check")
    validation = _pipeline(
        33334,
        [
            _job(107439, "build_release_arm64"),
            coverage_job,
            _job(107441, "clang_tidy_check"),
        ],
        source="parent_pipeline",
    )
    root = _pipeline(33333, [_job(107438, "generate_joblist")], source="push", downstream=(validation,))
    loaded_coverage_job = Mock()
    loaded_coverage_job.artifact.return_value = """
        <div>总修改行数</div><strong>92</strong>
        <div>已覆盖行数</div><strong>58</strong>
        <div>未覆盖行数</div><strong>34</strong>
        <div>覆盖率</div><strong>63.04%</strong>
    """
    project = SimpleNamespace(
        pipelines=PipelineCollection((root, validation)),
        jobs=SimpleNamespace(get=lambda _job_id: loaded_coverage_job),
    )
    provider = SimpleNamespace(
        id_project="example/service",
        gl=SimpleNamespace(projects=SimpleNamespace(get=lambda _project_id: project)),
    )
    monkeypatch.setattr(
        "pr_agent.git_providers.gitlab_provider.GitLabProvider",
        lambda _pr_url: provider,
    )

    task, binding = _task_and_binding()
    broker = AsyncMock()
    broker.get_task_triage_card.return_value = binding
    broker.record_pipeline_repair_state.return_value = True
    reconcile = AsyncMock(return_value=True)
    monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
    executor = TaskExecutor(broker, Mock(), "worker-coverage", max_active_tasks=1)

    categories, failed_jobs, coverage, _ = asyncio.run(executor._inspect_pipeline(task, 33333))

    assert categories == []
    assert failed_jobs == []
    assert coverage == CoverageResult(63.04, "changed_lines", "reported", 107440)

    asyncio.run(executor._finish_pipeline_repair(
        task,
        None,
        PipelineRepairState(
            phase=PipelineRepairPhase.TRIAGE_WAITING,
            root_pipeline_id=33333,
            completed_steps=("诊断修复已完成",),
            repair_actions=(RepairAction(
                commit_sha="final-sha",
                validation_pipeline_id=33334,
                validation_status="success",
                status="verified",
            ),),
        ),
        33334,
        "final-sha",
        "success",
        categories,
        failed_jobs,
        coverage,
    ))
    terminal = broker.record_pipeline_repair_state.await_args.args[1]

    assert terminal.final_pipeline_status == "success"
    assert terminal.final_coverage == 63.04
    assert terminal.final_coverage_source == "changed_lines"
    assert terminal.final_coverage_status == "reported"
    assert reconcile.await_args.args[3] is TriageCardState.REPAIR_SUCCEEDED

    stored = StoredTask(
        envelope=task,
        status=TaskStatus.COMPLETED,
        attempt=1,
        worker_id="worker-coverage",
        fencing_token=1,
        result="success",
        error="",
        pipeline_repair_state=terminal,
    )
    live = _live_snapshot(stored, binding, [], None)
    durable = _durable_snapshot({
        "task_id": task.task_id,
        "success": 1,
        "pushed_sha": "final-sha",
        "final_pipeline_status": "success",
        "final_coverage": terminal.final_coverage,
        "extra": {
            "final_pipeline_id": terminal.latest_pipeline_id,
            "coverage_source": terminal.final_coverage_source,
            "coverage_status": terminal.final_coverage_status,
        },
    })

    assert live["final_pipeline"]["coverage"] == 63.04
    assert live["final_pipeline"]["coverage_source"] == "changed_lines"
    assert live["final_pipeline"]["coverage_status"] == "reported"
    assert durable["final_pipeline"]["coverage"] == 63.04
    assert durable["final_pipeline"]["coverage_source"] == "changed_lines"
    assert durable["final_pipeline"]["coverage_status"] == "reported"
    assert "变更行覆盖率：63.04%" in FeishuGitProvider._format_triage_result(
        "修复成功",
        {
            "final_pipeline_status": terminal.final_pipeline_status,
            "final_coverage": terminal.final_coverage,
            "coverage_source": terminal.final_coverage_source,
            "coverage_status": terminal.final_coverage_status,
        },
    )

def test_rolled_back_coverage_attempt_preserves_build_success(monkeypatch):
    task, binding = _task_and_binding()
    broker = AsyncMock()
    broker.get_task_triage_card.return_value = binding
    broker.record_pipeline_repair_state.return_value = True
    reconcile = AsyncMock(return_value=True)
    monkeypatch.setattr("pr_agent.distributed.executor.queue_repair_reconciliation", reconcile)
    executor = TaskExecutor(broker, Mock(), "worker-coverage", max_active_tasks=1)
    state = PipelineRepairState(
        selected_categories=("build",),
        source_failed_job_names=("build_release_arm64",),
        coverage_phase=CoverageContinuationPhase.COMPLETED,
        coverage_attempts=1,
        coverage_baseline_sha="a" * 40,
        coverage_enhancement_sha="b" * 40,
        coverage_rollback_sha="c" * 40,
        coverage_before=63.04,
        coverage_threshold=80.0,
        coverage_result="rolled_back",
        coverage_failure_reason="补测流水线未通过。",
    )
    failed_jobs = [{"name": "x86_64_ut_coverage_check", "id": 18}]

    asyncio.run(executor._finish_pipeline_repair(
        task,
        None,
        state,
        33335,
        "c" * 40,
        "failed",
        [],
        failed_jobs,
        CoverageResult(63.04, "changed_lines", "reported", 18, 80.0),
    ))

    terminal = broker.record_pipeline_repair_state.await_args.args[1]
    assert terminal.repair_outcome == "success"
    assert terminal.auto_rollback_required is False
    assert terminal.coverage_result == "rolled_back"
    assert reconcile.await_args.args[3] is TriageCardState.REPAIR_SUCCEEDED
    assert "编译修复成功，覆盖率补测失败，已撤回补测提交" in reconcile.await_args.args[4]
