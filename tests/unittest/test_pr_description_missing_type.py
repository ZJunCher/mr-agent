from unittest.mock import MagicMock

from pr_agent.tools.pr_description import PRDescription


def _make_description(data: dict) -> PRDescription:
    """Build a minimal PRDescription instance, bypassing __init__, with just
    enough state for _prepare_pr_answer() to run."""
    pr = object.__new__(PRDescription)
    pr.data = data
    pr.vars = {"title": "fallback title"}
    pr.user_description = ""
    pr.file_label_dict = {}
    git_provider = MagicMock()
    git_provider.is_supported.return_value = False
    git_provider.pr.title = "fallback title"
    pr.git_provider = git_provider
    return pr


def test_prepare_pr_answer_does_not_crash_when_ai_response_omits_type():
    # Simulates AI model output drift where the 'type' key is absent entirely
    # (enable_pr_type is false by default, so the code tries to drop it).
    pr = _make_description({"title": "fix: something", "description": "desc body"})

    title, pr_body, changes_walkthrough, pr_file_changes = pr._prepare_pr_answer()

    assert title == "fallback title"
    assert "desc body" in pr_body


def test_prepare_pr_answer_still_drops_type_when_present():
    pr = _make_description({"title": "fix: something", "type": ["Bug fix"], "description": "desc body"})

    title, pr_body, changes_walkthrough, pr_file_changes = pr._prepare_pr_answer()

    assert "type" not in pr.data
    assert "desc body" in pr_body
