from unittest.mock import MagicMock, patch

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


@pytest.fixture(autouse=True)
def _reset_enabled_flag():
    previous = get_settings().get("pr_reviewer.code_graph.enabled", False)
    yield
    get_settings().set("pr_reviewer.code_graph.enabled", previous)


def _make_tool():
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.pr_url = "https://gitlab.example.com/team/repo/-/merge_requests/1"
    tool.git_provider = MagicMock()
    tool.git_provider.mr.target_branch = "main"
    tool.git_provider.get_git_repo_url.return_value = "https://gitlab.example.com/team/repo.git"
    tool.git_provider._prepare_clone_url_with_token.return_value = "https://oauth2:token@gitlab.example.com/team/repo.git"
    diff_file = MagicMock()
    diff_file.filename = "pkg/a.py"
    diff_file.head_file = "VALUE = 1\n"
    tool.git_provider.get_diff_files.return_value = [diff_file]
    tool.token_handler = MagicMock()
    return tool


def test_related_files_context_is_empty_when_disabled():
    get_settings().set("pr_reviewer.code_graph.enabled", False)
    tool = _make_tool()
    assert tool._get_related_files_context() == ""


def test_related_files_context_calls_builder_with_expected_arguments():
    get_settings().set("pr_reviewer.code_graph.enabled", True)
    tool = _make_tool()
    with patch(
        "pr_agent.tools.pr_code_suggestions.build_related_files_context", return_value="RELATED_CONTENT"
    ) as builder:
        assert tool._get_related_files_context() == "RELATED_CONTENT"
    args = builder.call_args.args
    assert args[0][0].relpath == "pkg/a.py"
    assert args[0][0].new_content == "VALUE = 1\n"
    assert args[1] == "https://oauth2:token@gitlab.example.com/team/repo.git"
    assert args[2] == "https://gitlab.example.com/team/repo.git"
    assert args[3] == "main"


def test_related_files_context_swallows_exceptions():
    get_settings().set("pr_reviewer.code_graph.enabled", True)
    tool = _make_tool()
    tool.git_provider.get_diff_files.side_effect = RuntimeError("boom")
    assert tool._get_related_files_context() == ""
