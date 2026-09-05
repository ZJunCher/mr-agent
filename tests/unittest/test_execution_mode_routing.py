import asyncio
import json
from unittest.mock import AsyncMock

import redis
from starlette.background import BackgroundTasks
from starlette_context import request_cycle_context

import pr_agent.servers.gitlab_webhook as webhook
from pr_agent.distributed.broker import EnqueueResult
from pr_agent.distributed.ingress import QueueIngress


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {"X-Gitlab-Event-UUID": f"uuid-{payload['object_attributes']['id']}"}

    async def json(self):
        return self.payload


def note_payload(note_id=99, project="eabot/cook"):
    return {
        "object_kind": "note",
        "project": {"id": 1, "path_with_namespace": project},
        "object_attributes": {"id": note_id, "action": "create", "note": "/review"},
        "merge_request": {
            "iid": 536,
            "url": f"https://gitlab.example/{project}/-/merge_requests/536",
        },
        "user": {"username": "alice"},
    }


def call_webhook(payload):
    async def run_test():
        with request_cycle_context({}):
            return await webhook.gitlab_webhook(BackgroundTasks(), FakeRequest(payload))

    return asyncio.run(run_test())


def test_queue_mode_returns_after_enqueue(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
    monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("PR_AGENT_QUEUE_ALLOWLISTED_PROJECTS", raising=False)
    broker = AsyncMock()
    broker.enqueue_task.return_value = EnqueueResult(created=True, task_id="task-1")
    monkeypatch.setattr(webhook, "_get_queue_ingress", lambda: QueueIngress(broker))

    response = call_webhook(note_payload())

    assert response.status_code == 200
    assert json.loads(response.body) == {"message": "Queued", "task_id": "task-1"}
    broker.enqueue_task.assert_awaited_once()


def test_queue_mode_redis_failure_returns_retryable_503(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
    monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("PR_AGENT_QUEUE_ALLOWLISTED_PROJECTS", raising=False)
    broker = AsyncMock()
    broker.enqueue_task.side_effect = redis.ConnectionError("down")
    monkeypatch.setattr(webhook, "_get_queue_ingress", lambda: QueueIngress(broker))

    response = call_webhook(note_payload(note_id=100))

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"


def test_queue_allowlist_keeps_other_projects_inline(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
    monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("PR_AGENT_QUEUE_ALLOWLISTED_PROJECTS", "eabot/cook")
    webhook._recent_webhook_events.clear()
    broker = AsyncMock()
    monkeypatch.setattr(webhook, "_get_queue_ingress", lambda: QueueIngress(broker))

    response = call_webhook(note_payload(note_id=101, project="eabot/other"))

    assert response.status_code == 200
    assert json.loads(response.body) == {"message": "Received"}
    broker.enqueue_task.assert_not_awaited()
