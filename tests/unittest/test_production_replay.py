import asyncio
from types import SimpleNamespace

import pytest

from pr_agent.eval.production_replay import ProductionReplayRequest, run_production_replay


SKILL = """schema_version = 1
name = "cook"
project = "eabot/cook"

[[rules]]
id = "evidence"
targets = ["review", "improve"]
instruction = "Require direct evidence."
"""


class Settings:
    def __init__(self):
        self.values = {
            "config.publish_output": True,
            "config.publish_output_progress": True,
            "eval.enable_capture": True,
            "config.temperature": 0.2,
            "config.max_model_tokens": 32000,
            "pr_reviewer.code_graph.enabled": False,
            "large_mr_review.enabled": True,
            "large_mr_review.max_chunks": 20,
        }
        self.config = SimpleNamespace(model="model", temperature=0.2, max_model_tokens=32000)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def unset(self, key):
        self.values.pop(key, None)


class Provider:
    def __init__(self, pr_url, base_sha=None, head_sha=None, input_snapshot=None):
        self.pr_url = pr_url
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.input_snapshot = input_snapshot
        self.id_project = "eabot/cook"
        self.id_mr = "12"
        self.files = [SimpleNamespace(filename="src/a.py", patch="@@ -1 +1 @@\n-old\n+new")]

    def get_diff_files(self):
        return self.files

    def get_files(self):
        return ["src/a.py"]

    def get_file_content_at_ref(self, path, ref):
        return None

    def get_pr_target_branch(self):
        return "main"

    def get_pr_target_branch_sha(self):
        return "c" * 40


class Reviewer:
    instances = []

    def __init__(self, pr_url, *, git_provider, project_skill_session):
        self.provider = git_provider
        self.session = project_skill_session
        self.project_skill_effective = project_skill_session.effective("review", files=git_provider.get_files())
        self.review_chunk_plan = SimpleNamespace(plan_hash="review-plan")
        self.review_coverage = SimpleNamespace(status="complete")
        self.related_files_context = "related"
        self.prediction = "review:\n  key_issues_to_review: []\n  security_concerns: 'No'"
        self.vars = {"title": "frozen"}
        self.called = False
        self.__class__.instances.append(self)

    async def _prepare_prediction(self, model):
        self.called = True

    def _prepare_pr_review(self):
        return "review output"


class Improve:
    instances = []

    def __init__(self, pr_url, *, git_provider, project_skill_session):
        self.provider = git_provider
        self.session = project_skill_session
        self.project_skill_effective = project_skill_session.effective("improve", files=git_provider.get_files())
        self.review_chunk_plan = SimpleNamespace(plan_hash="improve-plan")
        self.review_coverage = SimpleNamespace(status="complete")
        self.related_files_context = "related"
        self._improve_prompt_pairs = (("system", "user"),)
        self.called = False
        self.__class__.instances.append(self)

    async def generate_suggestions_data(self):
        self.called = True
        return {
            "code_suggestions": [{
                "relevant_file": "src/a.py",
                "relevant_lines_start": 1,
                "relevant_lines_end": 1,
                "label": "正确性",
                "one_sentence_summary": "修复问题",
                "suggestion_content": "Trigger: test",
            }],
        }


def _request(command="review", skill=SKILL):
    return ProductionReplayRequest(
        project="eabot/cook",
        mr_iid="12",
        pr_url="https://gitlab/eabot/cook/-/merge_requests/12",
        base_sha="a" * 40,
        head_sha="b" * 40,
        target_sha="c" * 40,
        input_snapshot={"title": "frozen"},
        skill_content=skill,
        command=command,
        model="model",
        captured_at="2026-08-27T12:00:00+08:00",
    )


def _run(request, settings):
    return asyncio.run(run_production_replay(
        request,
        settings=settings,
        provider_factory=Provider,
        reviewer_factory=Reviewer,
        improve_factory=Improve,
    ))


def test_review_replay_uses_production_generation_without_publishing_and_restores_settings():
    settings = Settings()

    result = _run(_request("review"), settings)

    instance = Reviewer.instances[-1]
    assert instance.called
    assert instance.provider.base_sha == "a" * 40
    assert instance.provider.head_sha == "b" * 40
    assert instance.session.rule_set.manifest_hash
    assert result.status == "ok"
    assert result.output == "review output"
    assert result.coverage_status == "complete"
    assert result.condition.chunk_plan_hash == "review-plan"
    assert settings.get("config.publish_output") is True
    assert settings.get("config.publish_output_progress") is True
    assert settings.get("eval.enable_capture") is True


def test_improve_replay_uses_real_suggestion_generation_and_normalizes_items():
    settings = Settings()

    result = _run(_request("improve"), settings)

    assert Improve.instances[-1].called
    assert result.status == "ok"
    assert result.condition.command == "improve"
    assert result.condition.chunk_plan_hash == "improve-plan"
    assert result.normalized_items[0].file_path == "src/a.py"
    assert result.normalized_items[0].line_start == 1


def test_invalid_skill_is_rejected_before_tool_execution():
    settings = Settings()

    result = _run(_request("review", skill="not toml = ["), settings)

    assert result.status == "error"
    assert result.error_code == "invalid_project_skill"


class ExplodingReviewer(Reviewer):
    async def _prepare_prediction(self, model):
        raise RuntimeError("boom")


def test_replay_restores_settings_after_exception():
    settings = Settings()

    result = asyncio.run(run_production_replay(
        _request("review"),
        settings=settings,
        provider_factory=Provider,
        reviewer_factory=ExplodingReviewer,
        improve_factory=Improve,
    ))

    assert result.status == "error"
    assert result.error_code == "replay_execution_failed"
    assert settings.get("config.publish_output") is True
    assert settings.get("eval.enable_capture") is True


def test_project_skill_session_from_content_rejects_project_mismatch():
    from pr_agent.suggestions.project_prompt_rules import ProjectSkillSession

    with pytest.raises(ValueError, match="schema/project"):
        ProjectSkillSession.from_content(Provider("url"), "other/project", SKILL, "c" * 40)
