from jinja2 import Environment, StrictUndefined

from pr_agent.config_loader import get_settings

BASE_VARS = {
    "title": "t", "date": "2026-01-01", "language": "python",
    "num_code_suggestions": 3, "focus_only_on_problems": False,
    "diff_no_line_numbers": "SAMPLE_DIFF_MARKER",
    "is_ai_metadata": False, "extra_instructions": "",
    "duplicate_prompt_examples": False,
}


def _render(related_files_context: str):
    env = Environment(undefined=StrictUndefined)
    settings = get_settings().pr_code_suggestions_prompt_python_v2
    variables = dict(BASE_VARS)
    variables["related_files_context"] = related_files_context
    system = env.from_string(settings.system).render(variables)
    user = env.from_string(settings.user).render(variables)
    return system, user


def test_no_related_files_keeps_old_behavior():
    system, user = _render("")
    assert "不要假设 diff 之外的代码缺失就是问题" in system
    assert "Related Files" not in system
    assert "Related Files" not in user


def test_with_related_files_adds_explanation_and_hard_constraint():
    system, user = _render("SAMPLE_RELATED_MARKER")
    assert "Related Files" in system
    assert "不得针对 Related Files" in system
    assert "SAMPLE_RELATED_MARKER" in user
    assert "SAMPLE_DIFF_MARKER" in user
    diff_idx = user.index("SAMPLE_DIFF_MARKER")
    related_idx = user.index("SAMPLE_RELATED_MARKER")
    assert user[diff_idx:related_idx].count("======") >= 2
