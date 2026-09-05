import asyncio

from pr_agent.suggestions import project_prompt_rules
from pr_agent.suggestions.project_prompt_rules import PROJECT_SKILL_MANIFEST_PATH, ProjectSkillSession
from pr_agent.tools import pr_reviewer
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_reviewer import PRReviewer


SKILL = """
schema_version = 1
name = "cook-review"
project = "eabot/cook"

[[rules]]
id = "review-rule"
targets = ["review"]
paths = ["src/**"]
instruction = "Review the Cook ownership invariant."

[[rules]]
id = "improve-rule"
targets = ["improve"]
paths = ["src/**"]
instruction = "Preserve the Cook ownership invariant."
"""


class Provider:
    id_project = "eabot/cook"

    def get_pr_target_branch(self):
        return "main"

    def get_pr_target_branch_sha(self):
        return "target-sha"

    def get_file_content_at_ref(self, path, ref):
        assert ref == "target-sha"
        return SKILL if path == PROJECT_SKILL_MANIFEST_PATH else None


class CaptureAI:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.response, "stop"


class ModeSettings:
    def __init__(self, mode):
        self.mode = mode

    def get(self, key, default=None):
        if key == "project_review_skill.rollout_mode":
            return self.mode
        return default


def _session():
    provider = Provider()
    return provider, ProjectSkillSession.load(provider, provider.id_project)


def test_review_appends_target_branch_skill_to_user_prompt(monkeypatch):
    monkeypatch.setattr(project_prompt_rules, "get_settings", lambda: ModeSettings("review_and_improve"))
    monkeypatch.setattr(
        pr_reviewer,
        "get_review_prompt_pairs",
        lambda *_args, **_kwargs: [("SYSTEM", "USER {{ diff }}")],
    )
    provider, session = _session()
    ai = CaptureAI("review: {}")
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.vars = {}
    reviewer.patches_diff = "DIFF"
    reviewer.related_files_context = ""
    reviewer._changed_files = ("src/a.cc",)
    reviewer.git_provider = provider
    reviewer.project_skill_session = session
    reviewer.ai_handler = ai

    asyncio.run(reviewer._get_prediction("model"))

    assert ai.calls[0]["system"] == "SYSTEM"
    assert "Review the Cook ownership invariant." in ai.calls[0]["user"]
    assert "controlled user context" in ai.calls[0]["user"]


def test_improve_appends_same_session_skill_to_generation_prompt(monkeypatch):
    monkeypatch.setattr(project_prompt_rules, "get_settings", lambda: ModeSettings("review_and_improve"))
    provider, session = _session()
    ai = CaptureAI("code_suggestions: []")
    improve = PRCodeSuggestions.__new__(PRCodeSuggestions)
    improve.vars = {}
    improve.related_files_context = ""
    improve._changed_files = ("src/a.cc",)
    improve._improve_prompt_pairs = (("SYSTEM", "USER {{ diff }}"),)
    improve._improve_prompt_languages = (frozenset({"cpp"}),)
    improve._improve_rule_languages = frozenset({"cpp"})
    improve.project_skill_session = session
    improve.git_provider = provider
    improve.ai_handler = ai

    result = asyncio.run(improve._get_prediction("model", "DIFF", "DIFF"))

    assert result == {"code_suggestions": []}
    assert "Preserve the Cook ownership invariant." in ai.calls[0]["user"]
    assert improve.project_skill_session is session


def test_shadow_mode_loads_but_does_not_inject(monkeypatch):
    monkeypatch.setattr(project_prompt_rules, "get_settings", lambda: ModeSettings("shadow"))
    monkeypatch.setattr(pr_reviewer, "get_review_prompt_pairs", lambda *_args, **_kwargs: [("SYSTEM", "USER")])
    provider, session = _session()
    ai = CaptureAI("review: {}")
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.vars = {}
    reviewer.patches_diff = "DIFF"
    reviewer.related_files_context = ""
    reviewer._changed_files = ("src/a.cc",)
    reviewer.git_provider = provider
    reviewer.project_skill_session = session
    reviewer.ai_handler = ai

    asyncio.run(reviewer._get_prediction("model"))

    assert session.rule_set.rules
    assert "Cook ownership" not in ai.calls[0]["user"]
