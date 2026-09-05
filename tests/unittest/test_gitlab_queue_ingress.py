import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from pr_agent.config_loader import task_settings_context
from pr_agent.distributed.broker import EnqueueResult
from pr_agent.distributed.ingress import QueueIngress, build_creation_idempotency_key
from pr_agent.distributed.models import TaskKind
from pr_agent.servers.gitlab_webhook import evaluate_pr_logic, should_process_pr_logic


def note_payload(command="/review", note_id=99, project="eabot/cook"):
    return {
        "object_kind": "note",
        "project": {"id": 1, "path_with_namespace": project},
        "object_attributes": {"id": note_id, "action": "create", "note": command},
        "merge_request": {
            "iid": 536,
            "url": f"https://gitlab.example/{project}/-/merge_requests/536",
        },
        "user": {"username": "alice"},
    }


def merge_request_payload():
    return {
        "object_kind": "merge_request",
        "project": {"id": 1, "path_with_namespace": "eabot/cook"},
        "object_attributes": {
            "id": 20,
            "iid": 536,
            "action": "open",
            "title": "Add feature",
            "source_branch": "feature/test",
            "target_branch": "main",
            "labels": [{"title": "backend"}],
        },
        "user": {"username": "alice"},
    }


@pytest.mark.parametrize(
    ("config_key", "config_value", "payload_change", "reason_code"),
    [
        (None, None, lambda payload: payload.pop("object_attributes"), "missing_object_attributes"),
        (None, None, lambda payload: payload["object_attributes"].update(action="update"), "unsupported_action"),
        ("CONFIG.IGNORE_REPOSITORIES", ["eabot/cook"], lambda _payload: None, "ignored_repository"),
        ("CONFIG.IGNORE_PR_AUTHORS", ["alice"], lambda _payload: None, "ignored_author"),
        ("CONFIG.IGNORE_PR_SOURCE_BRANCHES", ["feature/.*"], lambda _payload: None, "ignored_source_branch"),
        ("CONFIG.IGNORE_PR_TARGET_BRANCHES", ["main"], lambda _payload: None, "ignored_target_branch"),
        ("CONFIG.IGNORE_PR_LABELS", ["backend"], lambda _payload: None, "ignored_label"),
        ("CONFIG.IGNORE_PR_TITLE", ["Add feature"], lambda _payload: None, "ignored_title"),
    ],
)
def test_gitlab_pr_evaluation_returns_stable_skip_reason(
    config_key, config_value, payload_change, reason_code,
):
    payload = merge_request_payload()
    payload_change(payload)
    with task_settings_context() as settings:
        for key in (
            "CONFIG.IGNORE_REPOSITORIES",
            "CONFIG.IGNORE_PR_AUTHORS",
            "CONFIG.IGNORE_PR_SOURCE_BRANCHES",
            "CONFIG.IGNORE_PR_TARGET_BRANCHES",
            "CONFIG.IGNORE_PR_LABELS",
            "CONFIG.IGNORE_PR_TITLE",
        ):
            settings.set(key, [])
        if config_key:
            settings.set(config_key, config_value)

        decision = evaluate_pr_logic(payload)

        assert decision.allowed is False
        assert decision.reason_code == reason_code
        assert decision.reason
        assert should_process_pr_logic(payload) is False


def test_gitlab_pr_evaluation_allows_matching_open_event():
    with task_settings_context() as settings:
        for key in (
            "CONFIG.IGNORE_REPOSITORIES",
            "CONFIG.IGNORE_PR_AUTHORS",
            "CONFIG.IGNORE_PR_SOURCE_BRANCHES",
            "CONFIG.IGNORE_PR_TARGET_BRANCHES",
            "CONFIG.IGNORE_PR_LABELS",
            "CONFIG.IGNORE_PR_TITLE",
        ):
            settings.set(key, [])

        decision = evaluate_pr_logic(merge_request_payload())

    assert decision.allowed is True
    assert decision.reason_code == ""


def test_note_command_maps_to_pr_command_task():
    async def run_test():
        broker = AsyncMock()
        broker.enqueue_task.side_effect = lambda task: EnqueueResult(True, task.task_id)
        ingress = QueueIngress(broker)

        result = await ingress.enqueue_gitlab_event(note_payload(), {})

        task = broker.enqueue_task.await_args.args[0]
        assert result.task_id == task.task_id
        assert task.kind is TaskKind.PR_COMMAND
        assert task.command == "/review"
        assert task.mr.project_id == "eabot/cook"
        assert task.mr.iid == 536
        assert task.payload["reviewer_user"] == "alice"

    asyncio.run(run_test())


def test_mr_open_freezes_auto_workflow_commands():
    async def run_test():
        broker = AsyncMock()
        broker.enqueue_task.side_effect = lambda task: EnqueueResult(True, task.task_id)
        ingress = QueueIngress(broker)
        payload = {
            "object_kind": "merge_request",
            "project": {"id": 1, "path_with_namespace": "eabot/cook"},
            "object_attributes": {
                "id": 20,
                "iid": 536,
                "action": "open",
                "updated_at": "2026-08-06T00:00:00Z",
                "url": "https://gitlab.example/eabot/cook/-/merge_requests/536",
                "last_commit": {"id": "creation-sha"},
            },
        }

        with (
            patch("pr_agent.distributed.ingress.ensure_creation_review", return_value="run-1") as ensure,
            patch("pr_agent.distributed.ingress.record_review_event") as event,
        ):
            await ingress.enqueue_gitlab_event(payload, {})

        task = broker.enqueue_task.await_args.args[0]
        assert task.kind is TaskKind.AUTO_WORKFLOW
        assert isinstance(task.payload["commands"], list)
        assert ensure.call_args.args[0]["task_id"] == task.task_id
        assert ensure.call_args.args[0]["commit_sha"] == "creation-sha"
        event.assert_called_once_with("run-1", "workflow_queued", "queued")

    asyncio.run(run_test())


def test_creation_identity_is_shared_by_webhook_and_recovery():
    assert build_creation_idempotency_key("g/r", 12, "abc") == \
        build_creation_idempotency_key("g/r", "12", "later-sha")


def test_mr_open_records_queue_failure():
    async def run_test():
        broker = AsyncMock()
        broker.enqueue_task.side_effect = RuntimeError("redis unavailable")
        payload = {
            "object_kind": "merge_request",
            "project": {"id": 1, "path_with_namespace": "eabot/cook"},
            "object_attributes": {"id": 20, "iid": 536, "action": "open", "updated_at": "2026-08-06T00:00:00Z"},
        }
        with (
            patch("pr_agent.distributed.ingress.ensure_creation_review", return_value="run-1"),
            patch("pr_agent.distributed.ingress.finish_review_run") as finish,
            patch("pr_agent.distributed.ingress.record_review_event") as event,
        ):
            with pytest.raises(RuntimeError, match="redis unavailable"):
                await QueueIngress(broker).enqueue_gitlab_event(payload, {})
        finish.assert_called_once_with(
            "failed", "run-1", stage="queue_failed", error_code="RuntimeError",
            error_message="redis unavailable", unpublished_reason="queue_admission_failed",
        )
        assert event.call_args.kwargs["status"] == "failed"

    asyncio.run(run_test())


def test_non_command_note_remains_durable_gitlab_event():
    async def run_test():
        broker = AsyncMock()
        broker.enqueue_task.side_effect = lambda task: EnqueueResult(True, task.task_id)

        await QueueIngress(broker).enqueue_gitlab_event(note_payload("looks good"), {})

        assert broker.enqueue_task.await_args.args[0].kind is TaskKind.GITLAB_EVENT

    asyncio.run(run_test())


def test_running_child_pipeline_event_is_cached_without_resuming_waiters():
    async def run_test():
        broker = AsyncMock()
        broker.enqueue_task.side_effect = lambda task: EnqueueResult(True, task.task_id)
        broker.publish_pipeline_event.return_value = []
        payload = {
            "object_kind": "pipeline",
            "project": {"id": 1, "path_with_namespace": "eabot/cook"},
            "object_attributes": {
                "id": 29921,
                "sha": "abc",
                "status": "running",
                "ref": "feature/x",
                "source": "parent_pipeline",
            },
        }

        await QueueIngress(broker).enqueue_gitlab_event(payload, {})

        event = broker.publish_pipeline_event.await_args.args[0]
        assert event.pipeline_id == 29921
        assert event.status == "running"
        assert event.source == "parent_pipeline"

    asyncio.run(run_test())
