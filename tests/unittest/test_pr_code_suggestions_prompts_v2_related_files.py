import pytest
from jinja2 import Environment, StrictUndefined

from pr_agent.config_loader import get_settings

BASE_VARS = {
    "title": "t", "date": "2026-01-01", "language": "cpp",
    "num_code_suggestions": 3, "focus_only_on_problems": False,
    "diff_no_line_numbers": "SAMPLE_DIFF_MARKER",
    "is_ai_metadata": False, "extra_instructions": "",
    "duplicate_prompt_examples": False,
}


@pytest.mark.parametrize("section_key", [
    "pr_code_suggestions_prompt_v2",
    "pr_code_suggestions_prompt_not_decoupled_v2",
])
def test_no_related_files_keeps_old_behavior(section_key):
    env = Environment(undefined=StrictUndefined)
    settings = get_settings().get(section_key)
    variables = dict(BASE_VARS)
    variables["related_files_context"] = ""
    system = env.from_string(settings.system).render(variables)
    user = env.from_string(settings.user).render(variables)
    assert "输入仅为 PR diff 片段而非完整工程" in system
    assert "Related Files" not in system
    assert "Related Files" not in user


@pytest.mark.parametrize("section_key", [
    "pr_code_suggestions_prompt_v2",
    "pr_code_suggestions_prompt_not_decoupled_v2",
])
def test_with_related_files_adds_explanation_and_hard_constraint(section_key):
    env = Environment(undefined=StrictUndefined)
    settings = get_settings().get(section_key)
    variables = dict(BASE_VARS)
    variables["related_files_context"] = "SAMPLE_RELATED_MARKER"
    system = env.from_string(settings.system).render(variables)
    user = env.from_string(settings.user).render(variables)
    assert "Related Files" in system
    assert "不得针对 Related Files" in system
    assert "SAMPLE_RELATED_MARKER" in user
    assert "SAMPLE_DIFF_MARKER" in user
    diff_idx = user.index("SAMPLE_DIFF_MARKER")
    related_idx = user.index("SAMPLE_RELATED_MARKER")
    assert user[diff_idx:related_idx].count("======") >= 2
