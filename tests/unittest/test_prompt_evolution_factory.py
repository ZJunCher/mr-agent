from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pr_agent.suggestions.prompt_evolution.evidence_loader import SqliteEvidenceLoader
from pr_agent.suggestions.prompt_evolution.factory import build_runner_from_settings
from pr_agent.suggestions.prompt_evolution.runner import PromptEvolutionUnavailable

NOW = datetime(2026, 8, 18, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
MODELS = (
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-4-8",
    "anthropic/claude-opus-4-6",
)


class FakeSettings:
    def __init__(self, db_path, **overrides):
        cfg = {
            "enabled": True,
            "target_project": "example-group/mr-agent",
            "target_branch": "main",
            "models": MODELS,
            "model_max_attempts": 2,
            "project_skill_optimizer_enabled": True,
            "project_skill_optimizer_gate_mode": "enforce",
            "project_skill_optimizer_edit_budget": 1,
            "project_skill_optimizer_max_edit_budget": 3,
            "project_skill_optimizer_selection_ratio": 0.25,
            "project_skill_optimizer_min_train_mrs": 2,
            "project_skill_optimizer_min_selection_mrs": 1,
            "project_skill_optimizer_min_control_cases": 1,
            "project_skill_optimizer_max_selection_cases": 20,
            "project_skill_optimizer_minimum_score_delta": 0.05,
            "project_skill_optimizer_rejected_buffer_size": 10,
            "project_skill_high_fidelity_enabled": True,
            "project_skill_high_fidelity_gate_mode": "enforce",
            "project_skill_high_fidelity_min_mrs": 1,
            "project_skill_high_fidelity_max_mrs": 10,
            "global_prompt_high_fidelity_enabled": True,
            "global_prompt_high_fidelity_gate_mode": "enforce",
            "global_prompt_high_fidelity_min_mrs": 1,
            "global_prompt_high_fidelity_max_mrs": 10,
            "global_prompt_high_fidelity_minimum_score_delta": 0.05,
            "project_skill_canary_enabled": False,
            "project_skill_canary_percent": 0,
            "project_skill_canary_approved_ref": "",
        }
        cfg.update(overrides)
        self.prompt_evolution = SimpleNamespace(**cfg)
        self.prompt_evolution_cluster_prompt = SimpleNamespace(system="cluster-system", user="cluster-user")
        self.values = {
            "GITLAB.URL": "https://gitlab.example",
            "GITLAB.PERSONAL_ACCESS_TOKEN": "test-token",
            "GITLAB.AUTH_TYPE": "private_token",
            "GITLAB.SSL_VERIFY": True,
            "pr_code_suggestions.inline_suggestions_storage_path": str(db_path),
            "pr_feedback.storage_path": str(db_path),
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeRedisFactory:
    def __init__(self, url):
        self.url = url

    def create_async(self):
        return SimpleNamespace(kind="async-redis")


class FakeProjects:
    def __init__(self, project):
        self.project = project
        self.requested = []

    def get(self, path):
        self.requested.append(path)
        return self.project


class FakeGitLab:
    def __init__(self, project, **kwargs):
        self.kwargs = kwargs
        self.projects = FakeProjects(project)


class FakeHandler:
    def _resolve_endpoint(self, model):
        return "https://relay.example", "relay-key"


class FakeHealthStore:
    def candidate_allowed(self, model, owner):
        return True

    def mark_failed(self, model, owner, failure):
        return None

    def mark_succeeded(self, model, owner):
        return None


def _build(tmp_path, monkeypatch, settings=None):
    monkeypatch.setenv("PR_AGENT_REDIS_URL", "redis://redis:6379/0")
    project = SimpleNamespace(path_with_namespace="example-group/mr-agent")
    gitlab_client = FakeGitLab(project)
    settings = settings or FakeSettings(tmp_path / "feedback.db")
    runner = build_runner_from_settings(
        now=NOW,
        settings=settings,
        redis_factory_cls=FakeRedisFactory,
        gitlab_factory=lambda **kwargs: gitlab_client,
        llm_handler_factory=FakeHandler,
        model_health_store=FakeHealthStore(),
    )
    return runner, gitlab_client, project


def test_factory_wires_real_sources_prompts_and_project(tmp_path, monkeypatch):
    runner, gitlab_client, project = _build(tmp_path, monkeypatch)

    assert isinstance(runner.evidence_loader, SqliteEvidenceLoader)
    assert runner.publisher.project is project
    assert gitlab_client.projects.requested == ["example-group/mr-agent"]
    assert runner.clusterer.system_prefix == "cluster-system"
    assert runner.clusterer.user_template == "cluster-user"
    assert runner.agent.model == MODELS[0]
    assert runner.now == NOW
    assert runner.global_runner.high_fidelity_evaluator is not None
    assert runner.global_runner.behavioral_model == MODELS[0]


@pytest.mark.parametrize(
    ("setting_overrides", "missing_key", "message"),
    [
        ({"target_project": "other/project"}, None, "target_project"),
        ({"target_branch": "main"}, None, "target_branch"),
        ({"models": ()}, None, "models"),
        ({}, "GITLAB.URL", "GITLAB.URL"),
        ({}, "GITLAB.PERSONAL_ACCESS_TOKEN", "GITLAB.PERSONAL_ACCESS_TOKEN"),
    ],
)
def test_factory_fails_closed_on_invalid_production_configuration(
    tmp_path, monkeypatch, setting_overrides, missing_key, message
):
    settings = FakeSettings(tmp_path / "feedback.db", **setting_overrides)
    if missing_key:
        settings.values[missing_key] = ""
    with pytest.raises(PromptEvolutionUnavailable, match=message):
        _build(tmp_path, monkeypatch, settings)


def test_factory_requires_shared_redis(tmp_path, monkeypatch):
    monkeypatch.delenv("PR_AGENT_REDIS_URL", raising=False)
    settings = FakeSettings(tmp_path / "feedback.db")
    with pytest.raises(PromptEvolutionUnavailable, match="PR_AGENT_REDIS_URL"):
        build_runner_from_settings(
            now=NOW,
            settings=settings,
            redis_factory_cls=FakeRedisFactory,
            gitlab_factory=lambda **kwargs: None,
            llm_handler_factory=FakeHandler,
            model_health_store=FakeHealthStore(),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"project_skill_high_fidelity_enabled": False},
        {"project_skill_high_fidelity_gate_mode": "invalid"},
        {"project_skill_high_fidelity_min_mrs": 0},
        {"project_skill_high_fidelity_min_mrs": 3, "project_skill_high_fidelity_max_mrs": 2},
        {"project_skill_canary_percent": 101},
        {"project_skill_canary_enabled": True, "project_skill_canary_percent": 10,
         "project_skill_canary_approved_ref": "main"},
        {"global_prompt_high_fidelity_enabled": False},
        {"global_prompt_high_fidelity_gate_mode": "shadow"},
        {"global_prompt_high_fidelity_min_mrs": 0},
        {"global_prompt_high_fidelity_min_mrs": 3, "global_prompt_high_fidelity_max_mrs": 2},
        {"global_prompt_high_fidelity_minimum_score_delta": 1.1},
    ],
)
def test_factory_rejects_unsafe_high_fidelity_and_canary_configuration(tmp_path, monkeypatch, overrides):
    settings = FakeSettings(tmp_path / "feedback.db", **overrides)

    with pytest.raises(Exception):
        _build(tmp_path, monkeypatch, settings)
