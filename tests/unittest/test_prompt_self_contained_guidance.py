"""The self-contained-writing guidance must be present (and render cleanly
through Jinja2, using the same variables PRCodeSuggestions supplies) in all
three /improve prompt variants (C++ default, not-decoupled, Python)."""
from jinja2 import Environment, StrictUndefined

from pr_agent.config_loader import get_settings

_RENDER_VARS = {
    "language": "cpp",
    "focus_only_on_problems": False,
    "num_code_suggestions": 3,
    "extra_instructions": "",
    "is_ai_metadata": False,
    "date": "2026-07-10",
    "duplicate_prompt_examples": False,
}


def _render(system_template: str) -> str:
    return Environment(undefined=StrictUndefined).from_string(system_template).render(_RENDER_VARS)


def test_cpp_default_prompt_has_self_contained_guidance():
    rendered = _render(get_settings().pr_code_suggestions_prompt.system)
    assert "自包含写法" in rendered
    assert "lambda" in rendered.lower()


def test_not_decoupled_prompt_has_self_contained_guidance():
    rendered = _render(get_settings().pr_code_suggestions_prompt_not_decoupled.system)
    assert "自包含写法" in rendered


def test_python_prompt_has_self_contained_guidance():
    rendered = _render(get_settings().pr_code_suggestions_prompt_python.system)
    assert "自包含写法" in rendered
    assert "functools.partial" in rendered
