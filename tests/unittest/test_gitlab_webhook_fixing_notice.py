import asyncio
from unittest.mock import AsyncMock

import pr_agent.distributed.runtime as runtime_module
import pr_agent.servers.gitlab_webhook as webhook


def test_suppressed_agent_pipeline_does_not_send_standalone_fixing_notice(monkeypatch):
    class Settings:
        @staticmethod
        def get(key, default=None):
            values = {
                "FEISHU.NOTIFY_ON_PIPELINE_FAILURE": True,
                "GITLAB.URL": "https://gitlab.example",
                "GITLAB.PERSONAL_ACCESS_TOKEN": "",
            }
            return values.get(key, default)

    class Response:
        ok = True

        @staticmethod
        def json():
            return [{
                "iid": 541,
                "title": "Revert intrinsic_reader",
                "web_url": "https://gitlab.example/eabot/cook/-/merge_requests/541",
                "author": {"username": "developer", "email": "developer@example.com"},
            }]

    broker = type("Broker", (), {"enqueue_notification": AsyncMock()})()
    runtime = type("Runtime", (), {"mode": "queue", "task_id": "pipeline-event", "broker": broker})()
    monkeypatch.setattr(runtime_module, "get_execution_runtime", lambda: runtime)
    monkeypatch.setattr(webhook, "get_settings", lambda: Settings())
    monkeypatch.setattr(webhook, "_get_failed_pipeline_jobs", lambda *_args: [{"name": "build_release_arm64"}])
    monkeypatch.setattr(webhook, "_classify_pipeline_failures", lambda jobs: (["build"], jobs))
    monkeypatch.setattr(webhook, "_should_suppress_pipeline_card", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(webhook._requests, "get", lambda *_args, **_kwargs: Response())

    asyncio.run(webhook._notify_feishu_pipeline_failure({
        "project": {"id": 2, "path_with_namespace": "eabot/cook"},
        "object_attributes": {
            "id": 29908,
            "ref": "feature/fix",
            "sha": "agent-fix-sha",
            "status": "failed",
            "source": "push",
        },
    }))

    broker.enqueue_notification.assert_not_awaited()
