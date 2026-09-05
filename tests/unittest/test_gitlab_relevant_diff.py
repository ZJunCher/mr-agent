"""Regression tests for GitLabProvider.get_relevant_diff / last_diff.

GitLab's merge_request diff-versions endpoint returns versions newest-first
(confirmed against a real MR: higher `.id` == more recently created). The
old code did `self.mr.diffs.list(get_all=True)[-1]` to compute `last_diff`,
silently picking the OLDEST version instead of the newest. get_relevant_diff
then fell back to that wrong (ancient) diff whenever its match loop found no
hit in mr.changes() for a given file/line -- handing GitLab a stale
base_sha/head_sha combination for `send_inline_comment`'s position, which
GitLab's API rejects with a 500 Internal Server Error, silently dropping
that inline suggestion.
"""
from unittest.mock import MagicMock

from pr_agent.git_providers.gitlab_provider import GitLabProvider


class _FakeDiffVersion:
    def __init__(self, id_, base_sha, head_sha):
        self.id = id_
        self.base_commit_sha = base_sha
        self.head_commit_sha = head_sha
        self.start_commit_sha = head_sha


def _provider_with_diffs(diffs):
    gp = GitLabProvider.__new__(GitLabProvider)
    gp.mr = MagicMock()
    gp.mr.diffs.list.return_value = diffs
    return gp


def test_set_merge_request_picks_the_diff_with_the_highest_id_newest_first_order():
    # Simulate GitLab's real newest-first ordering: index 0 has the highest id.
    diffs = [
        _FakeDiffVersion(17392, "0319f475", "6ecdce10"),
        _FakeDiffVersion(17391, "2a73140e", "dc6300f0"),
    ]
    gp = _provider_with_diffs(diffs)
    gp.id_project = "eabot/prism"
    gp.id_mr = "64"
    gp._get_merge_request = lambda: gp.mr
    gp._parse_merge_request_url = lambda url: ("eabot/prism", "64")
    gp._set_merge_request("http://gl/mr/64")
    assert gp.last_diff.id == 17392
    assert gp.last_diff.base_commit_sha == "0319f475"


def test_set_merge_request_picks_the_diff_with_the_highest_id_oldest_first_order():
    # Even if the API ever returned oldest-first, picking by max(id) must
    # still land on the newest version, not list position.
    diffs = [
        _FakeDiffVersion(17391, "2a73140e", "dc6300f0"),
        _FakeDiffVersion(17392, "0319f475", "6ecdce10"),
    ]
    gp = _provider_with_diffs(diffs)
    gp.id_project = "eabot/prism"
    gp.id_mr = "64"
    gp._get_merge_request = lambda: gp.mr
    gp._parse_merge_request_url = lambda url: ("eabot/prism", "64")
    gp._set_merge_request("http://gl/mr/64")
    assert gp.last_diff.id == 17392
    assert gp.last_diff.base_commit_sha == "0319f475"


def test_get_relevant_diff_returns_last_diff_when_match_found():
    gp = GitLabProvider.__new__(GitLabProvider)
    gp.last_diff = _FakeDiffVersion(17392, "0319f475", "6ecdce10")
    gp.mr = MagicMock()
    gp.mr.changes.return_value = {
        "changes": [{"new_path": "a.py", "diff": "the matching line text"}],
    }
    result = gp.get_relevant_diff("a.py", "the matching line text")
    assert result is gp.last_diff


def test_get_relevant_diff_returns_last_diff_when_no_match_found():
    # This is the exact bug scenario: no textual match for this file/line in
    # mr.changes(), so the function must fall back to the (now-correct)
    # newest diff version instead of an old/wrong one.
    gp = GitLabProvider.__new__(GitLabProvider)
    gp.last_diff = _FakeDiffVersion(17392, "0319f475", "6ecdce10")
    gp.mr = MagicMock()
    gp.mr.changes.return_value = {
        "changes": [{"new_path": "a.py", "diff": "totally different text"}],
    }
    result = gp.get_relevant_diff("a.py", "the matching line text")
    assert result is gp.last_diff


def test_get_relevant_diff_returns_last_diff_when_changes_list_is_empty():
    # An empty changes list (nothing touched this file) is a legitimate,
    # reachable scenario -- unlike `mr.changes()` returning a falsy value
    # outright (which the `if not _changes` guard exists for, but is
    # unreachable here since `_changes['changes']` is always assigned before
    # that check runs). Must still return last_diff, not None or raise.
    gp = GitLabProvider.__new__(GitLabProvider)
    gp.last_diff = _FakeDiffVersion(1, "a", "b")
    gp.mr = MagicMock()
    gp.mr.changes.return_value = {"changes": []}
    result = gp.get_relevant_diff("a.py", "some line")
    assert result is gp.last_diff
