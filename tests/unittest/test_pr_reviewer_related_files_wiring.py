from unittest.mock import MagicMock, patch

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


@pytest.fixture(autouse=True)
def _reset_enabled_flag():
    previous = get_settings().get("pr_reviewer.code_graph.enabled", False)
    yield
    get_settings().set("pr_reviewer.code_graph.enabled", previous)


def _make_reviewer():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.pr_url = "https://gitlab.example.com/team/repo/-/merge_requests/1"
    reviewer.git_provider = MagicMock()
    reviewer.git_provider.mr.target_branch = "main"
    reviewer.git_provider.get_git_repo_url.return_value = "https://gitlab.example.com/team/repo.git"
    reviewer.git_provider._prepare_clone_url_with_token.return_value = "https://oauth2:token@gitlab.example.com/team/repo.git"
    reviewer.git_provider.get_files.return_value = ["a.py"]
    diff_file = MagicMock()
    diff_file.filename = "pkg/a.py"
    diff_file.head_file = "VALUE = 1\n"
    reviewer.git_provider.get_diff_files.return_value = [diff_file]
    reviewer.token_handler = MagicMock()
    reviewer.vars = {"diff": ""}
    return reviewer


def test_disabled_leaves_diff_untouched_and_related_files_context_empty():
    get_settings().set("pr_reviewer.code_graph.enabled", False)
    reviewer = _make_reviewer()
    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value="SAMPLE_DIFF"):
        import asyncio
        asyncio.run(reviewer._prepare_prediction_related_files_only())
    assert reviewer.patches_diff == "SAMPLE_DIFF"
    assert reviewer.related_files_context == ""


def test_enabled_populates_related_files_context_without_touching_diff():
    get_settings().set("pr_reviewer.code_graph.enabled", True)
    reviewer = _make_reviewer()
    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value="SAMPLE_DIFF"), \
         patch("pr_agent.tools.pr_reviewer.build_related_files_context", return_value="RELATED_CONTENT") as builder:
        import asyncio
        asyncio.run(reviewer._prepare_prediction_related_files_only())
    assert reviewer.patches_diff == "SAMPLE_DIFF"  # unchanged - no concatenation
    assert reviewer.related_files_context == "RELATED_CONTENT"
    args = builder.call_args.args
    assert args[0][0].relpath == "pkg/a.py"
    assert args[3] == "main"


def test_get_prediction_uses_v2_prompts_and_injects_related_files_context():
    get_settings().set("pr_reviewer.code_graph.enabled", True)
    reviewer = _make_reviewer()
    reviewer.patches_diff = "SAMPLE_DIFF"
    reviewer.related_files_context = "RELATED_CONTENT"
    reviewer.token_handler.count_tokens = MagicMock(return_value=1)
    reviewer.ai_handler = MagicMock()

    async def _fake_chat_completion(**kwargs):
        return kwargs["system"], "stop"

    reviewer.ai_handler.chat_completion = _fake_chat_completion

    with patch("pr_agent.tools.pr_reviewer.get_review_prompt_pairs") as mocked_pairs:
        mocked_pairs.return_value = [("SYSTEM_TEMPLATE {{ related_files_context }}", "USER_TEMPLATE {{ diff }}")]
        import asyncio
        result = asyncio.run(reviewer._get_prediction("gpt-4"))

    assert "RELATED_CONTENT" in result
    _, kwargs = mocked_pairs.call_args
    assert kwargs.get("use_v2") is True
