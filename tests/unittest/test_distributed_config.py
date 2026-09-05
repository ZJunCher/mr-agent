import pytest

from pr_agent.config_loader import task_settings_context
from pr_agent.distributed.config import load_distributed_settings


def test_invalid_execution_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "typo")

    with pytest.raises(ValueError, match="inline or queue"):
        load_distributed_settings()


def test_queue_mode_requires_redis_url(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
    monkeypatch.delenv("PR_AGENT_REDIS_URL", raising=False)

    with pytest.raises(ValueError, match="Redis URL"):
        load_distributed_settings(redis_url_override="")


def test_inline_mode_does_not_require_redis(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")
    monkeypatch.delenv("PR_AGENT_REDIS_URL", raising=False)

    settings = load_distributed_settings(redis_url_override="")

    assert settings.execution_mode == "inline"
    assert settings.redis_url == ""
    assert settings.should_queue("eabot/cook") is False


def test_queue_allowlist_limits_adoption(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "queue")
    monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("PR_AGENT_QUEUE_ALLOWLISTED_PROJECTS", "eabot/cook, eabot/map")

    settings = load_distributed_settings()

    assert settings.should_queue("eabot/cook") is True
    assert settings.should_queue("eabot/other") is False


def test_triage_priority_defaults_are_enabled(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")

    settings = load_distributed_settings(redis_url_override="")

    assert settings.triage_priority_over_auto is True
    assert settings.auto_pause_at_command_boundary is True
    assert settings.task_heartbeat_seconds == 15
    assert settings.running_orphan_seconds == 120
    assert settings.assigned_start_seconds == 120
    assert settings.queued_dispatch_seconds == 300
    assert settings.repair_reconcile_seconds == 120
    assert settings.auto_workflow_retry_limit == 1


def test_auto_workflow_retry_limit_allows_zero_and_rejects_negative(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")

    with task_settings_context() as settings:
        settings.set("DISTRIBUTED.AUTO_WORKFLOW_RETRY_LIMIT", 0)
        assert load_distributed_settings(redis_url_override="").auto_workflow_retry_limit == 0

    with task_settings_context() as settings:
        settings.set("DISTRIBUTED.AUTO_WORKFLOW_RETRY_LIMIT", -1)
        with pytest.raises(ValueError, match="auto_workflow_retry_limit must be non-negative"):
            load_distributed_settings(redis_url_override="")


def test_invalid_triage_priority_boolean_fails_closed(monkeypatch):
    monkeypatch.setenv("PR_AGENT_EXECUTION_MODE", "inline")

    with task_settings_context() as settings:
        settings.set("DISTRIBUTED.TRIAGE_PRIORITY_OVER_AUTO", "sometimes")
        with pytest.raises(ValueError, match="triage_priority_over_auto must be a boolean"):
            load_distributed_settings(redis_url_override="")
