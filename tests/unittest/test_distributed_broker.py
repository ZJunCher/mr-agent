import asyncio
import math
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent.distributed.broker import (
    ADMIT_REPAIR_ROLLBACK_LUA,
    EnqueueResult,
    LostLeaseError,
    MrLease,
    RedisBroker,
    RedisKeys,
    RepairManifestConflict,
    StaleCardActionError,
    StoredTask,
    SyncRedisBroker,
    UnauthorizedRepairRollback,
)
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.models import (
    DeliveryKind,
    MrKey,
    PipelineEvent,
    PipelineResumeClaim,
    RepairItemStatus,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.triage.failure_categories import pipeline_repair_item, repair_items_for_failed_jobs
from pr_agent.triage.final_repair_report import FinalRepairReportState, RepairReportStatus
from pr_agent.triage.pipeline_repair import PipelineRepairPhase, PipelineRepairState
from pr_agent.triage.repair_details import RepairProgressEvent
from pr_agent.triage.repair_rollback import (
    RepairCommitEntry,
    RepairCommitManifest,
    RepairRollbackState,
    RepairRollbackStatus,
    RollbackFailureCode,
)

BASE_SHA = "a" * 40
REPAIR_SHA = "b" * 40
BASE_TREE_SHA = "c" * 40
REPAIR_TREE_SHA = "d" * 40


def _repair_entry() -> RepairCommitEntry:
    return RepairCommitEntry(
        sequence=1,
        commit_sha=REPAIR_SHA,
        parent_sha=BASE_SHA,
        tree_sha=REPAIR_TREE_SHA,
        effect_id="effect-1",
        task_marker="[pr-agent-task:task-1:push-attempt:1:marker]",
        pushed_at="2026-08-13T00:00:00+00:00",
    )


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
    return load_distributed_settings(redis_url_override="redis://localhost:6379/0")


@pytest.fixture
def triage_task():
    return TaskEnvelope.new(
        kind=TaskKind.PR_COMMAND,
        source="gitlab",
        mr=MrKey("eabot/cook", 536),
        pr_url="https://gitlab.example/eabot/cook/-/merge_requests/536",
        command="/triage",
        payload={},
        idempotency_key="note:1:536:99:create",
    )


def test_duplicate_enqueue_returns_original_task_id(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.side_effect = [[1, triage_task.task_id], [0, triage_task.task_id]]
        broker = RedisBroker(redis_client, settings)
        broker.record_lifecycle_event = AsyncMock(return_value=True)

        first = await broker.enqueue_task(triage_task)
        second = await broker.enqueue_task(replace(triage_task, task_id="different"))

        assert first.created is True
        assert second.created is False
        assert second.task_id == first.task_id

    asyncio.run(run_test())


def test_dead_auto_workflow_uses_dedicated_retry_limit(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = 1
        broker = RedisBroker(redis_client, settings)
        auto_task = replace(triage_task, kind=TaskKind.AUTO_WORKFLOW)
        broker.get_task = AsyncMock(return_value=StoredTask(
            auto_task, TaskStatus.RUNNING, 0, "worker-1", 7, "", "",
        ))

        result = await broker.recover_dead_worker_task("worker-1", auto_task.task_id)

        assert result == "requeued"
        assert redis_client.eval.await_args.args[-2] == 1

    asyncio.run(run_test())


def test_dead_regular_task_keeps_general_retry_limit(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = 2
        broker = RedisBroker(redis_client, settings)
        broker.get_task = AsyncMock(return_value=StoredTask(
            triage_task, TaskStatus.RUNNING, 0, "worker-1", 7, "", "",
        ))

        result = await broker.recover_dead_worker_task("worker-1", triage_task.task_id)

        assert result == "failed"
        assert redis_client.eval.await_args.args[-2] == 3

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("redis_result", "expected"),
    [
        ([0, 0], ("ignored", 0)),
        ([1, 1], ("requeued", 1)),
        ([2, 1], ("failed", 1)),
    ],
)
def test_requeue_stale_auto_workflow_maps_atomic_result(settings, redis_result, expected):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = redis_result
        broker = RedisBroker(redis_client, settings)

        result = await broker.requeue_stale_auto_workflow(
            "auto-task", age_seconds=300, retry_limit=1,
        )

        assert result == expected
        args = redis_client.eval.await_args.args
        assert args[-3] == "auto-task"
        assert args[-2] == 1
        assert isinstance(args[-1], float)

    asyncio.run(run_test())


def test_append_and_freeze_repair_manifest_map_atomic_results(settings, triage_task):
    async def run_test():
        entry = _repair_entry()
        open_manifest = RepairCommitManifest(
            repair_task_id=triage_task.task_id,
            project_id="eabot/cook",
            mr_iid=536,
            source_branch="feature/x",
            base_commit_sha=BASE_SHA,
            base_tree_sha=BASE_TREE_SHA,
            authorized_actor_id="ou_owner",
            entries=(entry,),
        )
        frozen_manifest = replace(open_manifest, frozen=True, frozen_at="2026-08-13T00:01:00+00:00")
        redis_client = AsyncMock()
        redis_client.eval.side_effect = [[1, open_manifest.to_json()], [1, frozen_manifest.to_json()]]
        broker = RedisBroker(redis_client, settings)

        appended = await broker.append_repair_commit(
            triage_task.task_id,
            entry,
            base_tree_sha=BASE_TREE_SHA,
            source_branch="feature/x",
            authorized_actor_id="ou_owner",
            lease=None,
        )
        frozen = await broker.freeze_repair_commit_manifest(triage_task.task_id, None)

        assert appended == open_manifest
        assert frozen == frozen_manifest

    asyncio.run(run_test())


def test_report_admission_is_deterministic_and_uses_report_stream(settings, triage_task):
    async def run_test():
        manifest = RepairCommitManifest(
            repair_task_id=triage_task.task_id,
            project_id="eabot/cook",
            mr_iid=536,
            source_branch="feature/x",
            base_commit_sha=BASE_SHA,
            base_tree_sha=BASE_TREE_SHA,
            authorized_actor_id="ou_owner",
            entries=(_repair_entry(),),
            frozen=True,
            frozen_at="2026-08-13T00:01:00+00:00",
        )
        stored = StoredTask(triage_task, TaskStatus.COMPLETED, 0, "", None, "", "", repair_commit_manifest=manifest)
        redis_client = AsyncMock()
        broker = RedisBroker(redis_client, settings)
        broker.get_task = AsyncMock(return_value=stored)
        broker._eval = AsyncMock(side_effect=[[1, "expected"], [0, "expected"]])

        first = await broker.admit_final_repair_report(triage_task.task_id)
        second = await broker.admit_final_repair_report(triage_task.task_id)

        assert first.created is True
        assert second.created is False
        first_keys = broker._eval.await_args_list[0].args[1]
        assert broker.keys.report_ingress_stream in first_keys
        first_child = broker._eval.await_args_list[0].args[2][0]
        second_child = broker._eval.await_args_list[1].args[2][0]
        assert first_child == second_child

    asyncio.run(run_test())


def test_report_ingress_zero_block_is_a_nonblocking_read(settings):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.xautoclaim.return_value = ["0-0", [], []]
        redis_client.xreadgroup.return_value = []
        broker = RedisBroker(redis_client, settings)
        broker._ensure_stream_group = AsyncMock()

        deliveries = await broker.read_report_ingress_group("scheduler-1", limit=1, block_ms=0)

        assert deliveries == []
        redis_client.xreadgroup.assert_awaited_once_with(
            "report-scheduler",
            "scheduler-1",
            {broker.keys.report_ingress_stream: ">"},
            count=1,
        )

    asyncio.run(run_test())


def test_old_task_without_final_report_state_loads_none(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.hgetall.return_value = {
            "payload": triage_task.to_json(),
            "status": "completed",
            "attempt": "0",
            "result": "",
            "error": "",
        }
        broker = RedisBroker(redis_client, settings)

        stored = await broker.get_task(triage_task.task_id)

        assert stored.final_repair_report_state is None

    asyncio.run(run_test())


def test_report_completion_rejects_nonterminal_state(settings, triage_task):
    async def run_test():
        report_task = replace(
            triage_task,
            kind=TaskKind.REPAIR_REPORT,
            mr=None,
            payload={"repair_task_id": triage_task.task_id},
        )
        broker = RedisBroker(AsyncMock(), settings)
        with pytest.raises(ValueError, match="terminal state"):
            await broker.complete_final_repair_report(
                report_task,
                None,
                FinalRepairReportState(RepairReportStatus.GENERATING, report_task_id=report_task.task_id),
            )

    asyncio.run(run_test())


def test_append_repair_manifest_conflict_is_not_downgraded(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = [-5, "commit parent does not match"]
        broker = RedisBroker(redis_client, settings)

        with pytest.raises(RepairManifestConflict, match="parent"):
            await broker.append_repair_commit(
                triage_task.task_id,
                _repair_entry(),
                base_tree_sha=BASE_TREE_SHA,
                source_branch="feature/x",
                authorized_actor_id="ou_owner",
                lease=None,
            )

    asyncio.run(run_test())


def test_terminal_rollback_admission_is_owner_checked_and_queued(settings, triage_task, monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_REPAIR_ROLLBACK_ENABLED", "true")
        entry = _repair_entry()
        manifest = RepairCommitManifest(
            repair_task_id=triage_task.task_id,
            project_id="eabot/cook",
            mr_iid=536,
            source_branch="feature/x",
            base_commit_sha=BASE_SHA,
            base_tree_sha=BASE_TREE_SHA,
            authorized_actor_id="ou_owner",
            entries=(entry,),
            frozen=True,
            frozen_at="2026-08-13T00:01:00+00:00",
        )
        binding = TriageCardBinding.new(
            card_id="card-536",
            task_id=triage_task.task_id,
            open_message_id="om-536",
            receive_id="ou_owner",
            mr_url=triage_task.pr_url,
            project_id="eabot/cook",
            mr_iid=536,
            mr_title="repair",
            source_branch="feature/x",
            pipeline_id=1,
            pipeline_sha=BASE_SHA,
            original_markdown="failed",
        )
        stored = StoredTask(
            triage_task,
            TaskStatus.COMPLETED,
            0,
            "",
            None,
            "",
            "",
            repair_commit_manifest=manifest,
        )
        redis_client = AsyncMock()
        broker = RedisBroker(redis_client, settings)
        broker.get_task = AsyncMock(return_value=stored)
        broker.get_triage_card = AsyncMock(side_effect=[binding, replace(
            binding,
            state=TriageCardState.ROLLBACK_QUEUED,
            rollback_repair_task_id=triage_task.task_id,
            rollback_commit_count=1,
        )])
        broker._eval = AsyncMock(return_value=[1, "rollback-task"])
        broker.enqueue_notification = AsyncMock(return_value=True)

        result = await broker.request_repair_rollback(
            triage_task.task_id,
            binding.card_id,
            binding.open_message_id,
            "ou_owner",
            binding.revision,
        )

        assert result.created is True
        broker.enqueue_notification.assert_awaited_once()

        broker.get_triage_card = AsyncMock(return_value=replace(binding, receive_id="ou_other"))
        with pytest.raises(UnauthorizedRepairRollback):
            await broker.request_repair_rollback(
                triage_task.task_id,
                binding.card_id,
                binding.open_message_id,
                "ou_owner",
                binding.revision,
            )

    asyncio.run(run_test())


def test_auto_failure_rollback_admission_carries_durable_guard_and_trigger(settings, triage_task, monkeypatch):
    async def run_test():
        monkeypatch.setenv("PR_AGENT_REPAIR_ROLLBACK_ENABLED", "true")
        manifest = RepairCommitManifest(
            repair_task_id=triage_task.task_id,
            project_id="eabot/cook",
            mr_iid=536,
            source_branch="feature/x",
            base_commit_sha=BASE_SHA,
            base_tree_sha=BASE_TREE_SHA,
            authorized_actor_id="ou_owner",
            entries=(_repair_entry(),),
            frozen=True,
            frozen_at="2026-08-13T00:01:00+00:00",
        )
        binding = TriageCardBinding.new(
            card_id="card-536",
            task_id=triage_task.task_id,
            open_message_id="om-536",
            receive_id="ou_owner",
            mr_url=triage_task.pr_url,
            project_id="eabot/cook",
            mr_iid=536,
            mr_title="repair",
            source_branch="feature/x",
            pipeline_id=1,
            pipeline_sha=BASE_SHA,
            original_markdown="failed",
        )
        stored = StoredTask(
            triage_task,
            TaskStatus.RUNNING,
            0,
            "worker-1",
            7,
            "",
            "",
            pipeline_repair_state=PipelineRepairState(
                auto_rollback_required=True,
                verified_selected_success_count=0,
            ),
            repair_commit_manifest=manifest,
        )
        broker = RedisBroker(AsyncMock(), settings)
        broker.get_task = AsyncMock(return_value=stored)
        broker.get_triage_card = AsyncMock(side_effect=[binding, replace(
            binding,
            state=TriageCardState.ROLLBACK_QUEUED,
            rollback_trigger="auto_failure",
        )])
        broker._eval = AsyncMock(return_value=[1, "rollback-task"])
        broker.enqueue_notification = AsyncMock(return_value=True)

        result = await broker.request_repair_rollback(
            triage_task.task_id,
            binding.card_id,
            binding.open_message_id,
            binding.receive_id,
            binding.revision,
            trigger="auto_failure",
        )

        assert result.created is True
        assert broker._eval.await_args.args[0] == ADMIT_REPAIR_ROLLBACK_LUA
        assert broker._eval.await_args.args[2][9] == "auto_failure"
        assert "repair_state.auto_rollback_required ~= true" in ADMIT_REPAIR_ROLLBACK_LUA
        assert "verified_selected_success_count" in ADMIT_REPAIR_ROLLBACK_LUA
        assert "'status', 'completed'" in ADMIT_REPAIR_ROLLBACK_LUA
        assert "'rollback_trigger', ARGV[10]" in ADMIT_REPAIR_ROLLBACK_LUA

    asyncio.run(run_test())


@pytest.mark.parametrize("succeeded", [True, False])
def test_auto_failure_rollback_completion_always_sends_one_final_failure_reminder(
    settings, triage_task, succeeded
):
    async def run_test():
        rollback_task = TaskEnvelope.new(
            kind=TaskKind.REPAIR_ROLLBACK,
            source="feishu",
            mr=triage_task.mr,
            pr_url=triage_task.pr_url,
            command="/rollback-repair",
            payload={"repair_task_id": triage_task.task_id, "trigger": "auto_failure"},
            idempotency_key=f"rollback:{triage_task.task_id}",
        )
        binding = replace(
            TriageCardBinding.new(
                card_id="card-536",
                task_id=triage_task.task_id,
                open_message_id="om-536",
                receive_id="ou_owner",
                mr_url=triage_task.pr_url,
                project_id="eabot/cook",
                mr_iid=536,
                mr_title="repair",
                source_branch="feature/x",
                pipeline_id=1,
                pipeline_sha=BASE_SHA,
                original_markdown="failed",
            ),
            rollback_trigger="auto_failure",
            rollback_commit_count=1,
        )
        status = RepairRollbackStatus.SUCCEEDED if succeeded else RepairRollbackStatus.FAILED
        state = RepairRollbackState(
            rollback_task_id=rollback_task.task_id,
            repair_task_id=triage_task.task_id,
            status=status,
            trigger="auto_failure",
            requested_by="ou_owner",
            expected_remote_head=REPAIR_SHA,
            manifest_digest="digest",
            rollback_commit_sha="e" * 40 if succeeded else "",
            failure_code=None if succeeded else RollbackFailureCode.REMOTE_HEAD_CHANGED,
            failure_message="" if succeeded else "分支已有新提交",
        )
        broker = RedisBroker(AsyncMock(), settings)
        broker.get_task_triage_card = AsyncMock(return_value=binding)
        broker.get_triage_card = AsyncMock(return_value=binding)
        broker._eval = AsyncMock(return_value=1)
        broker.enqueue_notification = AsyncMock(return_value=True)

        completed = await broker.complete_repair_rollback(rollback_task, None, state)

        assert completed is True
        assert broker.enqueue_notification.await_count == 2
        reminder = broker.enqueue_notification.await_args_list[1].args[0]
        assert reminder.header_template == "red"
        expected = "修复失败，本次自动修改已撤回" if succeeded else "修复失败，自动撤回未完成"
        assert expected in reminder.content

    asyncio.run(run_test())


def test_repair_progress_uses_capped_stream(settings):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.xadd.return_value = "1710000000000-0"
        redis_client.expire.return_value = True
        broker = RedisBroker(redis_client, settings)
        event = RepairProgressEvent.new("task-12345678", "diagnosing", "正在诊断")

        event_id = await broker.append_repair_progress(event)

        assert event_id == "1710000000000-0"
        redis_client.xadd.assert_awaited_once()
        _, fields = redis_client.xadd.await_args.args
        assert RepairProgressEvent.from_json(fields["payload"]).summary == "正在诊断"
        assert redis_client.xadd.await_args.kwargs["approximate"] is True
        redis_client.expire.assert_awaited_once()

    asyncio.run(run_test())


def test_repair_progress_reads_after_exact_stream_id(settings):
    async def run_test():
        redis_client = AsyncMock()
        event = RepairProgressEvent.new("task-12345678", "editing", "正在修改代码")
        redis_client.xrange.return_value = [("1710000000001-0", {"payload": event.to_json()})]
        broker = RedisBroker(redis_client, settings)

        events = await broker.get_repair_progress("task-12345678", after_id="1710000000000-0")

        assert events[0].event_id == "1710000000001-0"
        assert events[0].phase == "editing"
        assert redis_client.xrange.await_args.kwargs["min"] == "(1710000000000-0"

    asyncio.run(run_test())


def test_repair_progress_blocking_read_returns_all_browser_events(settings):
    async def run_test():
        redis_client = AsyncMock()
        event = RepairProgressEvent.new("task-12345678", "validating", "正在检查最新流水线")
        redis_client.xread.return_value = [
            ("pr-agent:task:task-12345678:repair-progress", [("1710000000002-0", {"payload": event.to_json()})])
        ]
        broker = RedisBroker(redis_client, settings)

        events = await broker.read_repair_progress(
            "task-12345678",
            after_id="1710000000001-0",
            block_ms=10,
        )

        assert [item.event_id for item in events] == ["1710000000002-0"]
        redis_client.xread.assert_awaited_once_with(
            {"pr-agent:task:task-12345678:repair-progress": "1710000000001-0"},
            count=50,
            block=10,
        )

    asyncio.run(run_test())


def test_worker_inbox_reclaims_pending_delivery_before_reading_new(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group name already exists")
        redis_client.xautoclaim.return_value = [
            "0-0",
            [
                (
                    "1710000000000-0",
                    {
                        "task_id": triage_task.task_id,
                        "delivery_kind": DeliveryKind.RESUME_PIPELINE.value,
                        "payload": '{"project_id":"eabot/cook"}',
                    },
                )
            ],
            [],
        ]
        broker = RedisBroker(redis_client, settings)
        broker._ensure_stream_group = AsyncMock()
        broker.get_task = AsyncMock(
            return_value=StoredTask(triage_task, TaskStatus.WAITING_PIPELINE, 0, "worker-1", 7, "", "")
        )

        delivery = await broker.read_worker_inbox("worker-1", block_ms=1)

        assert delivery is not None
        assert delivery.message_id == "1710000000000-0"
        assert delivery.kind is DeliveryKind.RESUME_PIPELINE
        redis_client.xreadgroup.assert_not_awaited()

    asyncio.run(run_test())


def test_claim_pipeline_resume_maps_atomic_outcomes(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.side_effect = [1, 0, 2, -1]
        broker = RedisBroker(redis_client, settings)
        lease = MrLease(triage_task.mr, "worker-1", 7)
        event = PipelineEvent.new(
            project_id=triage_task.mr.project_id,
            pipeline_id=30388,
            sha="4aed",
            status="success",
            ref="feature/test",
        )

        assert await broker.claim_pipeline_resume(triage_task.task_id, event, lease) is PipelineResumeClaim.CLAIMED
        assert await broker.claim_pipeline_resume(triage_task.task_id, event, lease) is PipelineResumeClaim.DUPLICATE
        assert await broker.claim_pipeline_resume(triage_task.task_id, event, lease) is PipelineResumeClaim.STALE
        assert await broker.claim_pipeline_resume(triage_task.task_id, event, lease) is PipelineResumeClaim.LOST_LEASE

    asyncio.run(run_test())


def test_triage_persistence_health_round_trip(settings):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.hgetall.return_value = {
            "status": "ok",
            "task_id": "task-1",
            "updated_at": "1710000000.0",
            "error": "",
        }
        broker = RedisBroker(redis_client, settings)

        await broker.record_triage_persistence("task-1", True)
        health = await broker.triage_persistence_health()

        redis_client.hset.assert_awaited_once()
        assert health == {
            "status": "ok",
            "task_id": "task-1",
            "updated_at": "1710000000.0",
            "error": "",
        }

    asyncio.run(run_test())


def test_fail_stale_running_task_rechecks_heartbeat_atomically(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.side_effect = [0, 1, -1]
        broker = RedisBroker(redis_client, settings)
        lease = MrLease(triage_task.mr, "worker-1", 7)

        assert await broker.fail_stale_running_task(triage_task.task_id, lease, 100.0, "timeout") is False
        assert await broker.fail_stale_running_task(triage_task.task_id, lease, 100.0, "timeout") is True
        with pytest.raises(LostLeaseError):
            await broker.fail_stale_running_task(triage_task.task_id, lease, 100.0, "timeout")

    asyncio.run(run_test())


def test_cancel_request_is_owner_checked_and_idempotent(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        broker = RedisBroker(redis_client, settings)
        binding = TriageCardBinding.new(
            card_id="card-536",
            task_id=triage_task.task_id,
            open_message_id="om-536",
            receive_id="ou-1",
            mr_url=triage_task.pr_url,
            project_id=triage_task.mr.project_id,
            mr_iid=triage_task.mr.iid,
            mr_title="test",
            source_branch="feature/test",
            pipeline_id=30100,
            pipeline_sha="abc123",
            original_markdown="failed",
        )
        binding = replace(
            binding,
            active_task_id=triage_task.task_id,
            active_category="pipeline",
            revision=2,
            state=TriageCardState.REPAIR_RUNNING,
        )
        broker.get_task = AsyncMock(
            return_value=StoredTask(triage_task, TaskStatus.RUNNING, 0, "worker-1", 7, "", "")
        )
        broker.get_triage_card = AsyncMock(return_value=binding)
        redis_client.eval.side_effect = [[1, "running"], [0, "running"]]

        first = await broker.request_repair_cancel(
            triage_task.task_id, binding.card_id, binding.open_message_id, binding.receive_id, binding.revision
        )
        duplicate = await broker.request_repair_cancel(
            triage_task.task_id, binding.card_id, binding.open_message_id, binding.receive_id, binding.revision
        )

        assert first.accepted is True
        assert duplicate.accepted is False
        assert broker.keys.mr_triage_active(triage_task.mr) in redis_client.eval.await_args_list[0].args

    asyncio.run(run_test())


def test_save_triage_card_rejects_older_or_equal_pipeline_version(settings, triage_task):
    async def run_test():
        newer = TriageCardBinding.new(
            card_id="card-newer",
            task_id="",
            open_message_id="",
            receive_id="ou-1",
            mr_url=triage_task.pr_url,
            project_id=triage_task.mr.project_id,
            mr_iid=triage_task.mr.iid,
            mr_title="test",
            source_branch="feature/test",
            pipeline_id=30306,
            pipeline_sha="new-sha",
            original_markdown="failed",
        )
        older = replace(
            newer,
            card_id="card-older",
            pipeline_id=30305,
            current_pipeline_id=30305,
            pipeline_sha="old-sha",
            current_pipeline_sha="old-sha",
        )
        redis_client = AsyncMock()
        redis_client.eval.side_effect = [1, -1, -1]
        broker = RedisBroker(redis_client, settings)

        assert await broker.save_triage_card(newer, ttl_seconds=300) is True
        assert await broker.save_triage_card(older, ttl_seconds=300) is False
        assert await broker.save_triage_card(newer, ttl_seconds=300) is False

        eval_args = redis_client.eval.await_args_list[0].args
        assert broker.keys.mr_latest_repair_pipeline_id(triage_task.mr) in eval_args
        assert newer.current_pipeline_id in eval_args
        assert "HGET', current_card_key, 'current_pipeline_id'" in eval_args[0]
        assert "tonumber(current_pipeline_id or '0') >= tonumber(ARGV[3])" in eval_args[0]

    asyncio.run(run_test())


def test_resolve_unified_repair_card_accepts_latest_pipeline(settings, triage_task):
    async def run_test():
        binding = TriageCardBinding.new(
            card_id="card-536",
            task_id="",
            open_message_id="om-536",
            receive_id="ou-1",
            mr_url=triage_task.pr_url,
            project_id=triage_task.mr.project_id,
            mr_iid=triage_task.mr.iid,
            mr_title="test",
            source_branch="feature/test",
            pipeline_id=30305,
            pipeline_sha="abc123",
            original_markdown="failed",
            repair_items=(pipeline_repair_item(30305, "abc123"),),
        )
        redis_client = AsyncMock()
        redis_client.get.return_value = binding.card_id
        broker = RedisBroker(redis_client, settings)
        broker.get_triage_card = AsyncMock(return_value=binding)

        resolved = await broker.resolve_unified_repair_card(binding.card_id, triage_task.mr, 30305)

        assert resolved == binding
        redis_client.get.assert_awaited_once_with(broker.keys.mr_latest_repair_card(triage_task.mr))

    asyncio.run(run_test())


def test_resolve_repair_card_selection_accepts_actionable_categories(settings, triage_task):
    async def run_test():
        binding = TriageCardBinding.new(
            card_id="card-536",
            task_id="",
            open_message_id="om-536",
            receive_id="ou-1",
            mr_url=triage_task.pr_url,
            project_id=triage_task.mr.project_id,
            mr_iid=triage_task.mr.iid,
            mr_title="test",
            source_branch="feature/test",
            pipeline_id=30305,
            pipeline_sha="abc123",
            original_markdown="failed",
            repair_items=repair_items_for_failed_jobs(
                [{"name": "clang_tidy_check"}, {"name": "build_release_arm64"}],
                30305,
                "abc123",
            ),
            repair_card_mode="multi_select",
        )
        redis_client = AsyncMock()
        redis_client.get.return_value = binding.card_id
        broker = RedisBroker(redis_client, settings)
        broker.get_triage_card = AsyncMock(return_value=binding)

        resolved = await broker.resolve_repair_card_selection(
            binding.card_id,
            triage_task.mr,
            30305,
            ("clang", "build"),
        )

        assert resolved == binding

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("latest_card_id", "callback_pipeline_id"),
    [("card-newer", 30305), ("card-536", 30306)],
)
def test_resolve_unified_repair_card_rejects_stale_identity(
    settings, triage_task, latest_card_id, callback_pipeline_id
):
    async def run_test():
        binding = TriageCardBinding.new(
            card_id="card-536",
            task_id="",
            open_message_id="om-536",
            receive_id="ou-1",
            mr_url=triage_task.pr_url,
            project_id=triage_task.mr.project_id,
            mr_iid=triage_task.mr.iid,
            mr_title="test",
            source_branch="feature/test",
            pipeline_id=30305,
            pipeline_sha="abc123",
            original_markdown="failed",
            repair_items=(pipeline_repair_item(30305, "abc123"),),
        )
        redis_client = AsyncMock()
        redis_client.get.return_value = latest_card_id
        broker = RedisBroker(redis_client, settings)
        broker.get_triage_card = AsyncMock(return_value=binding)

        with pytest.raises(StaleCardActionError):
            await broker.resolve_unified_repair_card(binding.card_id, triage_task.mr, callback_pipeline_id)

    asyncio.run(run_test())


def test_pipeline_repair_state_survives_task_reload(settings, triage_task):
    async def run_test():
        state = PipelineRepairState(
            phase=PipelineRepairPhase.TRIAGE_WAITING,
            completed_steps=("triage_started",),
            latest_pipeline_id=30100,
            latest_pipeline_sha="abc123",
        )
        redis_client = AsyncMock()
        redis_client.hgetall.return_value = {
            "payload": replace(triage_task, command="/repair-pipeline").to_json(),
            "status": TaskStatus.RUNNING.value,
            "attempt": "0",
            "pipeline_repair_state": state.to_json(),
        }
        broker = RedisBroker(redis_client, settings)

        stored = await broker.get_task(triage_task.task_id)

        assert stored is not None
        assert stored.pipeline_repair_state == state

    asyncio.run(run_test())


def test_task_reload_accepts_legacy_iso_timestamps(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.hgetall.return_value = {
            "payload": triage_task.to_json(),
            "status": TaskStatus.COMPLETED.value,
            "attempt": "0",
            "created_at": "2026-08-07T10:23:36.188113+00:00",
            "updated_at": "1786098220.79923",
            "heartbeat_at": "",
        }
        broker = RedisBroker(redis_client, settings)

        stored = await broker.get_task(triage_task.task_id)

        assert stored is not None
        assert stored.created_at == pytest.approx(
            datetime.fromisoformat("2026-08-07T10:23:36.188113+00:00").timestamp()
        )
        assert stored.updated_at == pytest.approx(1786098220.79923)
        assert stored.heartbeat_at == 0.0

    asyncio.run(run_test())


def test_record_pipeline_repair_state_uses_fenced_task_transition(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = 1
        broker = RedisBroker(redis_client, settings)
        lease = MrLease(triage_task.mr, "worker-1", 7)
        state = PipelineRepairState(phase=PipelineRepairPhase.FORMAT_RUNNING)

        changed = await broker.record_pipeline_repair_state(triage_task.task_id, state, lease)

        assert changed is True
        call = redis_client.eval.await_args
        assert "pipeline_repair_state" in call.args
        assert state.to_json() in call.args

    asyncio.run(run_test())


def test_update_repair_progress_persists_pipeline_identity(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.get.return_value = "card-538"
        redis_client.eval.return_value = 1
        broker = RedisBroker(redis_client, settings)
        notification = Mock(
            notification_id="notification-1",
            created_at="2026-08-07T00:00:00+00:00",
            to_json=Mock(return_value="{}"),
        )

        changed = await broker.update_repair_progress_with_notification(
            triage_task.task_id,
            {TriageCardState.WAITING_PIPELINE},
            TriageCardState.REPAIR_RUNNING,
            "正在检查最新流水线",
            30101,
            "triage-sha",
            notification,
        )

        assert changed is True
        call = redis_client.eval.await_args
        assert 30101 in call.args
        assert "triage-sha" in call.args

    asyncio.run(run_test())


def test_correct_late_repair_terminal_uses_atomic_task_and_card_preconditions(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.get.return_value = "card-538"
        redis_client.eval.return_value = 1
        broker = RedisBroker(redis_client, settings)
        state = PipelineRepairState(
            phase=PipelineRepairPhase.TERMINAL,
            latest_pipeline_id=30391,
            latest_pipeline_sha="ccf6ebb7",
            final_pipeline_status="success",
        )
        item = replace(
            pipeline_repair_item(30385, "dc78f383"),
            status=RepairItemStatus.SUCCEEDED,
        )
        notification = Mock(
            notification_id="late-success-1",
            created_at="2026-08-11T00:00:00+00:00",
            to_json=Mock(return_value="{}"),
        )

        changed = await broker.correct_late_repair_terminal(
            task_id=triage_task.task_id,
            expected_task_status=TaskStatus.COMPLETED,
            terminal_state=state,
            expected_card_states={TriageCardState.PIPELINE_FAILED},
            expected_revision=4,
            repair_items=(item,),
            status_markdown="已更正为成功",
            current_pipeline_id=30391,
            current_pipeline_sha="ccf6ebb7",
            notification=notification,
        )

        assert changed is True
        call = redis_client.eval.await_args
        assert broker.keys.task(triage_task.task_id) in call.args
        assert broker.keys.triage_card("card-538") in call.args
        assert TaskStatus.COMPLETED.value in call.args
        assert state.to_json() in call.args
        assert TriageCardState.PIPELINE_FAILED.value in call.args

    asyncio.run(run_test())


def test_pause_auto_records_cursor_and_releases_delivery(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = 1
        broker = RedisBroker(redis_client, settings)
        auto_task = replace(
            triage_task,
            task_id="auto-536",
            kind=TaskKind.AUTO_WORKFLOW,
            command="/auto",
            payload={"commands": ["/describe", "/mr_create"]},
        )
        lease = MrLease(auto_task.mr, "worker-1", 7)

        changed = await broker.pause_auto_for_triage(
            auto_task.task_id,
            auto_task.mr,
            triage_task_id=triage_task.task_id,
            next_command_index=1,
            completed_commands=["/describe"],
            workflow_head_sha="abc123",
            lease=lease,
        )

        assert changed is True
        call = redis_client.eval.await_args
        assert broker.keys.mr_triage_active(auto_task.mr) in call.args
        assert broker.keys.mr_paused_auto(auto_task.mr) in call.args
        assert any('"/describe"' in str(argument) for argument in call.args)

    asyncio.run(run_test())


def test_resume_auto_queues_one_resume_delivery(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = 1
        broker = RedisBroker(redis_client, settings)
        lease = MrLease(triage_task.mr, "worker-1", 7)

        changed = await broker.resume_auto_after_triage(
            triage_task.mr,
            triage_task_id=triage_task.task_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
        )

        assert changed is True
        call = redis_client.eval.await_args
        assert DeliveryKind.RESUME_AUTO.value in call.args

    asyncio.run(run_test())


def test_expired_owner_gets_higher_fencing_token(settings):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.side_effect = [["worker-1", 1], ["worker-2", 2], 0]
        broker = RedisBroker(redis_client, settings)
        mr = MrKey("eabot/cook", 536)

        first = await broker.claim_mr(mr, "worker-1", lease_seconds=1)
        second = await broker.claim_mr(mr, "worker-2", lease_seconds=30)

        assert second.fencing_token > first.fencing_token
        assert await broker.renew_mr(mr, "worker-1", first.fencing_token, 30) is False

    asyncio.run(run_test())


def test_transition_rejects_lost_lease(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = -1
        broker = RedisBroker(redis_client, settings)
        lease = MrLease(triage_task.mr, "worker-1", 4)

        with pytest.raises(LostLeaseError, match=triage_task.task_id):
            await broker.transition_task(triage_task.task_id, {TaskStatus.RUNNING}, TaskStatus.COMPLETED, lease)

    asyncio.run(run_test())


def test_card_keys_are_separate_from_task_keys():
    keys = RedisKeys("pr-agent")

    assert keys.triage_card("card-1") == "pr-agent:feishu:card:card-1"
    assert keys.task_triage_card("task-1") == "pr-agent:feishu:task-card:task-1"


def test_pipeline_keys_can_bind_one_exact_child():
    keys = RedisKeys("pr-agent")

    assert keys.pipeline_event("eabot/cook", "abc") != keys.pipeline_event("eabot/cook", "abc", 29921)
    assert keys.pipeline_waiters("eabot/cook", "abc") != keys.pipeline_waiters("eabot/cook", "abc", 29921)


def test_priority_keys_are_scoped_to_one_mr():
    keys = RedisKeys("pr-agent")
    mr = MrKey("eabot/cook", 536)

    assert keys.mr_triage_active(mr) == "pr-agent:mr:eabot%2Fcook:536:triage-active"
    assert keys.mr_paused_auto(mr) == "pr-agent:mr:eabot%2Fcook:536:paused-auto"
    assert keys.active_repairs == "pr-agent:repairs:active"


def test_terminal_card_state_rejects_late_running_update(settings):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.get.return_value = "card-538"
        redis_client.eval.return_value = 0
        broker = RedisBroker(redis_client, settings)

        changed = await broker.transition_triage_card(
            "task-538",
            {TriageCardState.REPAIR_QUEUED},
            TriageCardState.REPAIR_RUNNING,
            "running",
        )

        assert changed is None

    asyncio.run(run_test())


def test_enqueue_task_with_card_returns_original_task_id(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = [0, "task-original"]
        broker = RedisBroker(redis_client, settings)
        broker.record_lifecycle_event = AsyncMock(return_value=True)

        result = await broker.enqueue_task_with_card(triage_task, "card-538", "om_538", 2_592_000)

        assert result.created is False
        assert result.task_id == "task-original"

    asyncio.run(run_test())


def test_enqueue_task_with_card_reports_recovered_admission(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = [0, "task-original", 1, "1786155734123-0"]
        broker = RedisBroker(redis_client, settings)
        broker.record_lifecycle_event = AsyncMock(return_value=True)

        result = await broker.enqueue_task_with_card(triage_task, "card-538", "om_538", 2_592_000)

        assert result.created is False
        assert result.recovered is True
        assert result.task_id == "task-original"

    asyncio.run(run_test())


def test_post_repair_ut_admission_uses_dedicated_task_kind_and_card_identity(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = [1, "ut-task", 0, "1-0"]
        broker = RedisBroker(redis_client, settings)
        broker.record_lifecycle_event = AsyncMock(return_value=True)
        task = replace(
            triage_task,
            task_id="ut-task",
            kind=TaskKind.POST_REPAIR_UT,
            source="feishu",
            command="/ut",
            payload={"coverage_before": 63.04, "coverage_status_before": "reported"},
        )

        result = await broker.admit_post_repair_ut(
            task,
            repair_task_id="repair-task",
            card_id="card-536",
            open_message_id="om-536",
            sender_id="ou-owner",
            pipeline_id=30305,
            pipeline_sha=BASE_SHA,
            revision=7,
            ttl_seconds=3600,
            coverage_threshold=80.0,
        )

        assert result == EnqueueResult(True, "ut-task")
        arguments = redis_client.eval.await_args.args
        assert any('"kind":"post_repair_ut"' in str(value) for value in arguments)
        assert any('"repair_task_id":"repair-task"' in str(value) for value in arguments)
        assert any('"status":"queued"' in str(value) for value in arguments)

    asyncio.run(run_test())


def test_multi_select_admission_passes_complete_selection_to_lua(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = [1, triage_task.task_id, 0]
        broker = RedisBroker(redis_client, settings)
        broker.record_lifecycle_event = AsyncMock(return_value=True)
        task = replace(
            triage_task,
            source="feishu",
            command="/repair-pipeline",
            payload={
                "repair_category": "batch",
                "selected_categories": ["clang", "build"],
                "source_pipeline_id": 30305,
                "source_pipeline_sha": "abc123",
            },
        )

        await broker.enqueue_task_with_card(
            task,
            "card-536",
            "om-536",
            3600,
            sender_id="ou-owner",
            category="batch",
            selected_categories=("clang", "build"),
            pipeline_id=30305,
            pipeline_sha="abc123",
            revision=0,
        )

        arguments = redis_client.eval.await_args.args
        assert '["clang","build"]' in arguments
        admission_context = next(
            value for value in arguments if isinstance(value, str) and '"card_id":"card-536"' in value
        )
        assert '"selected_categories":["clang","build"]' in admission_context

    asyncio.run(run_test())


def test_multi_select_admission_maps_unavailable_selection(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = [-10, "unknown"]
        broker = RedisBroker(redis_client, settings)

        with pytest.raises(ValueError, match="repair selection is not actionable"):
            await broker.enqueue_task_with_card(
                replace(triage_task, source="feishu", command="/repair-pipeline"),
                "card-536",
                "om-536",
                3600,
                category="batch",
                selected_categories=("build", "unknown"),
                pipeline_id=30305,
                pipeline_sha="abc123",
                revision=0,
            )

    asyncio.run(run_test())


@pytest.mark.parametrize("created_at", ["NaN", "Infinity", "-Infinity", "not-a-timestamp"])
def test_enqueue_rejects_invalid_timestamp_before_redis(settings, triage_task, created_at):
    async def run_test():
        redis_client = AsyncMock()
        broker = RedisBroker(redis_client, settings)
        task = replace(triage_task, created_at=created_at)

        with pytest.raises(ValueError, match="created_at"):
            await broker.enqueue_task(task)

        redis_client.eval.assert_not_awaited()

    asyncio.run(run_test())


@pytest.mark.parametrize("with_card", [False, True])
def test_task_enqueue_uses_numeric_redis_score(settings, triage_task, with_card):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = [0, "task-original"]
        broker = RedisBroker(redis_client, settings)
        broker.record_lifecycle_event = AsyncMock(return_value=True)

        if with_card:
            await broker.enqueue_task_with_card(triage_task, "card-538", "om_538", 2_592_000)
        else:
            await broker.enqueue_task(triage_task)

        eval_args = redis_client.eval.await_args.args
        key_count = eval_args[1]
        redis_arguments = eval_args[2 + key_count :]
        assert isinstance(redis_arguments[3], float)
        assert math.isfinite(redis_arguments[3])

    asyncio.run(run_test())


def test_stored_task_reads_admission_receipt(settings, triage_task):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.hgetall.return_value = {
            "payload": triage_task.to_json(),
            "status": "queued",
            "attempt": "0",
            "created_at": "1786155734.12",
            "updated_at": "1786155734.12",
            "admission_state": "enqueued",
            "ingress_message_id": "1786155734123-0",
            "admission_context": '{"card_id":"card-538"}',
        }
        broker = RedisBroker(redis_client, settings)

        stored = await broker.get_task(triage_task.task_id)

        assert stored is not None
        assert stored.admission_complete is True
        assert stored.ingress_message_id == "1786155734123-0"
        assert stored.admission_context == {"card_id": "card-538"}

    asyncio.run(run_test())


def test_repair_gate_key_round_trip():
    keys = RedisKeys("pr-agent")
    mr = MrKey("eabot/group:project with spaces", 530)

    assert keys.parse_triage_gate(keys.mr_triage_active(mr)) == mr
    assert keys.parse_triage_gate("pr-agent:mr:broken:triage-active") is None


def test_scan_repair_gates_finds_gate_without_active_index(settings):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.scan.return_value = (
            0,
            ["pr-agent:mr:eabot%2Fcook:530:triage-active", "pr-agent:mr:broken:triage-active"],
        )
        redis_client.get.return_value = "ghost-task"
        broker = RedisBroker(redis_client, settings)

        cursor, gates = await broker.scan_repair_gates(0, 32)

        assert cursor == 0
        assert gates == [(MrKey("eabot/cook", 530), "ghost-task")]

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("code", "outcome"),
    [(0, "healthy"), (1, "recovered"), (2, "failed"), (3, "released"), (4, "rebuilt")],
)
def test_reconcile_admission_gate_maps_owner_checked_outcome(settings, code, outcome):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.eval.return_value = code
        broker = RedisBroker(redis_client, settings)
        broker.record_lifecycle_event = AsyncMock(return_value=True)

        result = await broker.reconcile_admission_gate(MrKey("eabot/cook", 530), "ghost-task")

        assert result == outcome
        if outcome == "healthy":
            redis_client.hincrby.assert_not_awaited()
        else:
            redis_client.hincrby.assert_awaited_once_with(
                broker.keys.repair_reconciliation_metrics,
                outcome,
                1,
            )

    asyncio.run(run_test())


def test_repair_health_exposes_admission_reconciliation_counts(settings):
    async def run_test():
        redis_client = AsyncMock()
        redis_client.zrange.return_value = []
        redis_client.hgetall.return_value = {"recovered": "2", "released": "1"}
        broker = RedisBroker(redis_client, settings)

        snapshot = await broker.repair_health()

        assert snapshot["admission_reconciliation"] == {"recovered": 2, "released": 1}

    asyncio.run(run_test())


def test_sync_card_lookup_reconstructs_typed_binding(settings):
    redis_client = AsyncMock()
    redis_client.get = lambda _: "card-538"
    redis_client.hgetall = lambda _: {
        "schema_version": "1",
        "card_id": "card-538",
        "task_id": "task-538",
        "open_message_id": "om_538",
        "receive_id": "ou_owner",
        "mr_url": "https://gitlab.example/eabot/cook/-/merge_requests/538",
        "project_id": "eabot/cook",
        "mr_iid": "538",
        "mr_title": "lidar udp",
        "source_branch": "feature/lidar",
        "pipeline_id": "29415",
        "pipeline_sha": "abc123",
        "original_markdown": "build failed",
        "state": "repair_running",
        "status_markdown": "running",
        "fallback_sent": "0",
        "updated_at": "2026-08-06T00:00:00+00:00",
    }

    restored = SyncRedisBroker(redis_client, settings).get_task_triage_card("task-538")

    assert isinstance(restored, TriageCardBinding)
    assert restored.mr_iid == 538
    assert restored.state is TriageCardState.REPAIR_RUNNING
    assert restored.fallback_sent is False
