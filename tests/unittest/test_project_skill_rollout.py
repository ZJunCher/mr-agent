from types import SimpleNamespace

from pr_agent.suggestions.project_prompt_rules import ProjectSkillSession
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


STABLE = """schema_version = 1
name = "stable"
project = "eabot/cook"
[[rules]]
id = "stable"
targets = ["review"]
instruction = "Use stable behavior."
"""

CANARY = """schema_version = 1
name = "canary"
project = "eabot/cook"
[[rules]]
id = "canary"
targets = ["review"]
instruction = "Use approved canary behavior."
"""


class Settings:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class Provider:
    id_project = "eabot/cook"
    id_mr = "12"

    def __init__(self, canary=CANARY):
        self.canary = canary

    def get_pr_target_branch(self):
        return "main"

    def get_pr_target_branch_sha(self):
        return "a" * 40

    def get_diff_refs(self):
        return {"base_sha": "0" * 40, "head_sha": "b" * 40}

    def get_file_content_at_ref(self, path, ref):
        if ref == "a" * 40:
            return STABLE
        if ref == "c" * 40:
            return self.canary
        return None


def _runtime_settings(monkeypatch, *, enabled, percent=100, approved_ref="c" * 40):
    settings = Settings({
        "prompt_evolution.project_skill_canary_enabled": enabled,
        "prompt_evolution.project_skill_canary_percent": percent,
        "prompt_evolution.project_skill_canary_approved_ref": approved_ref,
    })
    monkeypatch.setattr("pr_agent.suggestions.project_prompt_rules.get_settings", lambda: settings)


def test_canary_disabled_uses_target_branch_stable_skill(monkeypatch):
    _runtime_settings(monkeypatch, enabled=False)

    session = ProjectSkillSession.load(Provider(), "eabot/cook")

    assert session.rule_set.target_sha == "a" * 40
    assert session.rule_set.rules[0].id == "stable"


def test_approved_immutable_canary_ref_receives_selected_traffic(monkeypatch):
    _runtime_settings(monkeypatch, enabled=True, percent=100)

    session = ProjectSkillSession.load(Provider(), "eabot/cook")

    assert session.rule_set.target_sha == "c" * 40
    assert session.rule_set.rules[0].id == "canary"


def test_invalid_canary_content_immediately_falls_back_to_stable(monkeypatch):
    _runtime_settings(monkeypatch, enabled=True, percent=100)

    session = ProjectSkillSession.load(Provider(canary="invalid = ["), "eabot/cook")

    assert session.rule_set.target_sha == "a" * 40
    assert session.rule_set.rules[0].id == "stable"


def test_improve_capture_persists_frozen_sha_and_non_code_inputs(monkeypatch):
    records = []
    settings = Settings({
        "eval.enable_capture": True,
        "config.git_provider": "gitlab",
        "config.model": "model",
        "large_mr_review.enabled": True,
        "related_tickets": [],
    })
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.get_settings", lambda: settings)
    monkeypatch.setattr("pr_agent.eval.store.save_review_run", lambda record: records.append(record) or True)
    tool = object.__new__(PRCodeSuggestions)
    tool.git_provider = SimpleNamespace(
        id_project="eabot/cook",
        id_mr="12",
        mr=SimpleNamespace(target_branch="main", source_branch="feature"),
        get_diff_refs=lambda: {
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "start_sha": "a" * 40,
        },
        get_pr_url=lambda: "https://gitlab/eabot/cook/-/merge_requests/12",
        get_file_content_at_ref=lambda path, ref: STABLE,
    )
    tool.pr_url = "url"
    tool.vars = {
        "title": "Frozen title",
        "description": "Frozen description",
        "commit_messages_str": "commit",
        "branch": "feature",
    }
    tool.project_skill_effective = SimpleNamespace(skill_hash="skill", target_sha="a" * 40)

    tool._capture_high_fidelity_snapshot()

    assert len(records) == 1
    assert records[0]["base_sha"] == "a" * 40
    assert records[0]["head_sha"] == "b" * 40
    assert records[0]["input"]["title"] == "Frozen title"
    assert records[0]["extra"] == {
        "capture_source": "improve_generation",
        "project_skill_content": STABLE,
        "project_skill_target_sha": "a" * 40,
    }
