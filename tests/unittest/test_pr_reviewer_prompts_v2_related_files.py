from jinja2 import Environment, StrictUndefined

from pr_agent.config_loader import get_settings

BASE_VARS = {
    "num_max_findings": 3,
    "title": "t", "branch": "b", "description": "", "language": "python",
    "diff": "SAMPLE_DIFF_MARKER", "num_pr_files": 1,
    "require_score": False, "require_tests": False,
    "require_estimate_effort_to_review": False,
    "require_estimate_contribution_time_cost": False,
    "require_can_be_split_review": False, "require_security_review": False,
    "require_todo_scan": False, "question_str": "", "answer_str": "",
    "extra_instructions": "", "commit_messages_str": "",
    "custom_labels": "", "enable_custom_labels": False,
    "is_ai_metadata": False, "related_tickets": [],
    "duplicate_prompt_examples": False, "date": "2026-01-01",
}


def _render(related_files_context: str):
    env = Environment(undefined=StrictUndefined)
    settings = get_settings().pr_review_prompt_v2
    variables = dict(BASE_VARS)
    variables["related_files_context"] = related_files_context
    system = env.from_string(settings.system).render(variables)
    user = env.from_string(settings.user).render(variables)
    return system, user


def test_no_related_files_keeps_old_disclaimer_and_no_new_section():
    system, user = _render("")
    assert "you only see changed code segments (diff hunks in a PR), not the entire codebase" in system
    assert "Related Files" not in system
    assert "Related Files" not in user


def test_with_related_files_adds_explanation_and_user_block():
    system, user = _render("SAMPLE_RELATED_MARKER")
    assert "Related Files" in system
    assert "not part of this PR's diff" in system
    assert "SAMPLE_RELATED_MARKER" in user
    assert "SAMPLE_DIFF_MARKER" in user
    # The two sections must be independently fenced, not sharing one '======' block.
    diff_fence_idx = user.index("SAMPLE_DIFF_MARKER")
    related_fence_idx = user.index("SAMPLE_RELATED_MARKER")
    between = user[diff_fence_idx:related_fence_idx]
    assert between.count("======") >= 2  # diff's closing fence + related's opening fence


def test_key_issues_field_forbids_pointing_into_related_files():
    system, _ = _render("SAMPLE_RELATED_MARKER")
    assert "never to a file that only appears in the Related Files section" in system


def test_speculation_rule_allows_related_files_as_evidence():
    system, _ = _render("SAMPLE_RELATED_MARKER")
    assert "from the diff context or the Related Files section" in system
