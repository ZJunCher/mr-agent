import asyncio
from unittest.mock import Mock

import pr_agent.servers.gitlab_webhook as webhook
from pr_agent.triage.ci_failure_store import get_ci_failure
from pr_agent.triage.pipeline_freshness import PipelineFreshness, PipelineFreshnessState


class _Settings:
    @staticmethod
    def get(key, default=None):
        values = {
            "CI_FAILURE_DASHBOARD.CAPTURE_ENABLED": True,
            "FEISHU.NOTIFY_ON_PIPELINE_FAILURE": False,
            "GITLAB.URL": "https://gitlab.example",
            "GITLAB.PERSONAL_ACCESS_TOKEN": "",
        }
        return values.get(key, default)


def test_capture_persists_without_notification(tmp_path, monkeypatch):
    db_path = str(tmp_path / "feedback.db")
    monkeypatch.setattr(webhook, "get_settings", lambda: _Settings())
    monkeypatch.setattr("pr_agent.feedback.store.get_db_path", lambda: db_path)
    monkeypatch.setattr("pr_agent.triage.ci_failure_store.get_db_path", lambda: db_path)
    monkeypatch.setattr(
        webhook,
        "_load_ci_job_trace",
        lambda _project_id, _job_id: b"error: undefined reference to SensorFactory",
    )

    failure_id = webhook._capture_ci_failure(
        request_json={"object_attributes": {"ref": "fix", "url": "https://gitlab.example/pipelines/91"}},
        mr={
            "iid": 551,
            "web_url": "https://gitlab.example/eabot/cook/-/merge_requests/551",
            "title": "Fix build",
            "target_branch": "dev",
            "author": {"username": "alice"},
        },
        project_id=23,
        project_path="eabot/cook",
        pipeline_id=91,
        pipeline_sha="a" * 40,
        failed_jobs=[{"id": 11, "name": "build_release", "pipeline": {"id": 91}}],
        card_id="cook-551-91",
    )

    detail = get_ci_failure(failure_id, path=db_path)
    assert detail["project_path"] == "eabot/cook"
    assert detail["notification_state"] == "not_attempted"
    assert detail["jobs"][0]["system_reason"] == "error: undefined reference to SensorFactory"


def test_success_event_only_records_followup(monkeypatch):
    class Response:
        ok = True

        @staticmethod
        def json():
            return [{
                "iid": 551,
                "web_url": "https://gitlab.example/eabot/cook/-/merge_requests/551",
                "title": "Fix build",
                "author": {"username": "alice"},
            }]

    followups = []
    monkeypatch.setattr(webhook, "get_settings", lambda: _Settings())
    monkeypatch.setattr(webhook._requests, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(webhook, "_should_suppress_pipeline_card", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        webhook,
        "check_pipeline_freshness",
        lambda **_kwargs: PipelineFreshness(PipelineFreshnessState.CURRENT),
    )
    monkeypatch.setattr(webhook, "_record_ci_followup", lambda *args: followups.append(args))
    failed_jobs = Mock()
    monkeypatch.setattr(webhook, "_get_failed_pipeline_jobs", failed_jobs)

    asyncio.run(webhook._notify_feishu_pipeline_failure({
        "project": {"id": 23, "path_with_namespace": "eabot/cook"},
        "object_attributes": {
            "id": 92,
            "ref": "fix",
            "sha": "b" * 40,
            "status": "success",
            "source": "merge_request_event",
        },
    }))

    assert followups == [(23, 551, 92, "b" * 40, "success")]
    failed_jobs.assert_not_called()


def test_storage_failure_does_not_escape_capture(monkeypatch):
    monkeypatch.setattr(webhook, "get_settings", lambda: _Settings())
    monkeypatch.setattr("pr_agent.triage.ci_failure_store.save_ci_failure", Mock(side_effect=OSError("disk")))

    assert webhook._capture_ci_failure(
        request_json={"object_attributes": {"ref": "fix"}},
        mr={"iid": 1},
        project_id=23,
        project_path="eabot/cook",
        pipeline_id=91,
        pipeline_sha="a",
        failed_jobs=[],
        card_id="card",
    ) is None
