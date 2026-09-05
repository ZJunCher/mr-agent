from unittest.mock import MagicMock

from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _provider_with_fake_gl():
    gp = GitLabProvider.__new__(GitLabProvider)  # bypass __init__/network
    gp.id_project = "group/proj"
    gp.id_mr = 7
    gp.gl = MagicMock()
    return gp


def test_set_commit_status_calls_statuses_create():
    gp = _provider_with_fake_gl()
    ok = gp.set_commit_status("abc123", "pending", "pr-agent/feedback",
                              description="please rate", target_url="http://x")
    assert ok is True
    commit = gp.gl.projects.get.return_value.commits.get.return_value
    commit.statuses.create.assert_called_once()
    payload = commit.statuses.create.call_args[0][0]
    assert payload["state"] == "pending"
    assert payload["name"] == "pr-agent/feedback"
    assert payload["description"] == "please rate"
    assert payload["target_url"] == "http://x"


def test_set_commit_status_returns_false_on_error():
    gp = _provider_with_fake_gl()
    gp.gl.projects.get.side_effect = RuntimeError("boom")
    assert gp.set_commit_status("abc123", "success", "pr-agent/feedback") is False
