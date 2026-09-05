from datetime import datetime, timezone
from unittest.mock import patch

from pr_agent.suggestions.gitlab_mr_sync import (
    _sync_window,
    normalize_api_mr,
    normalize_webhook_mr,
    sync_gitlab_mrs_once,
)


class Response:
    def __init__(self, rows, next_page=""):
        self._rows = rows
        self.headers = {"X-Next-Page": next_page}

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_normalize_webhook_and_api_payloads():
    webhook = normalize_webhook_mr({
        "object_kind": "merge_request",
        "project": {"id": 9, "path_with_namespace": "group/repo"},
        "user": {"username": "alice"},
        "object_attributes": {
            "iid": 3, "url": "https://gitlab/group/repo/-/merge_requests/3",
            "title": "MR", "state": "opened", "source_branch": "feat", "target_branch": "main",
            "last_commit": {"id": "abc"},
        },
    })
    assert webhook["project_path"] == "group/repo"
    assert webhook["mr_iid"] == 3
    assert webhook["commit_sha"] == "abc"

    api = normalize_api_mr({
        "project_id": 9, "iid": 3, "web_url": "https://gitlab/group/repo/-/merge_requests/3",
        "author": {"username": "alice"}, "sha": "def",
    })
    assert api["project_path"] == "group/repo"
    assert api["author"] == "alice"


def test_sync_pages_and_advances_cursor_only_after_success():
    session = Session([
        Response([{"project_id": 9, "iid": 1, "web_url": "https://gl/g/r/-/merge_requests/1",
                   "updated_at": "2026-08-07T01:00:00Z"}], "2"),
        Response([{"project_id": 9, "iid": 2, "web_url": "https://gl/g/r/-/merge_requests/2",
                   "updated_at": "2026-08-07T02:00:00Z"}]),
    ])
    saved = []
    state = {"cursor_at": "2026-08-07T00:00:00Z", "last_reconcile_at": "2026-08-07T00:00:00Z"}
    with (
        patch("pr_agent.suggestions.gitlab_mr_sync.claim_sync_lease", return_value=True),
        patch("pr_agent.suggestions.gitlab_mr_sync.get_sync_state", return_value=state),
        patch("pr_agent.suggestions.gitlab_mr_sync.upsert_mr", side_effect=lambda row: saved.append(row) or True),
        patch("pr_agent.suggestions.gitlab_mr_sync.complete_sync") as complete,
        patch("pr_agent.suggestions.gitlab_mr_sync.recover_synced_mrs", return_value={"recovered": 2}) as recover,
        patch("pr_agent.suggestions.gitlab_mr_sync.evaluate_review_alerts", return_value=[{}, {}]),
        patch("pr_agent.suggestions.gitlab_mr_sync.get_settings") as settings,
    ):
        settings.return_value.get.side_effect = lambda key, default=None: {
            "GITLAB.URL": "https://gl", "GITLAB.PERSONAL_ACCESS_TOKEN": "secret",
            "suggestion_review_dashboard.reconcile_interval_seconds": 999999,
            "suggestion_review_dashboard.sync_overlap_seconds": 600,
        }.get(key, default)
        result = sync_gitlab_mrs_once(session, owner="worker")

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert len(session.calls) == 2
    assert {row["mr_iid"] for row in saved} == {1, 2}
    complete.assert_called_once()
    assert complete.call_args.kwargs["cursor_at"] != "2026-08-07T00:00:00Z"
    assert session.calls[0][1]["headers"] == {"Authorization": "Bearer secret"}
    assert result["recovery"] == {"recovered": 2}
    assert result["active_alerts"] == 2
    assert len(recover.call_args.args[0]) == 2


def test_sync_error_preserves_cursor():
    class FailingSession:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("network down")

    with patch("pr_agent.suggestions.gitlab_mr_sync.claim_sync_lease", return_value=True), \
         patch("pr_agent.suggestions.gitlab_mr_sync.get_sync_state", return_value={"cursor_at": "old"}), \
         patch("pr_agent.suggestions.gitlab_mr_sync.complete_sync") as complete, \
         patch("pr_agent.suggestions.gitlab_mr_sync.get_settings") as settings:
        settings.return_value.get.side_effect = lambda key, default=None: {
            "GITLAB.URL": "https://gl", "GITLAB.PERSONAL_ACCESS_TOKEN": "secret",
        }.get(key, default)
        result = sync_gitlab_mrs_once(FailingSession(), owner="worker")

    assert result["status"] == "error"
    assert complete.call_args.kwargs["error"] == "network down"
    assert "cursor_at" not in complete.call_args.kwargs


def test_sync_skips_when_lease_is_busy():
    with patch("pr_agent.suggestions.gitlab_mr_sync.claim_sync_lease", return_value=False):
        assert sync_gitlab_mrs_once(owner="worker") == {"status": "busy", "count": 0}


def test_sync_window_uses_overlap_and_daily_reconciliation():
    current = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
    values = {
        "reconcile_interval_seconds": 86400,
        "sync_overlap_seconds": 600,
        "reconcile_lookback_hours": 48,
    }
    with (
        patch("pr_agent.suggestions.gitlab_mr_sync.now_cn", return_value=current),
        patch("pr_agent.suggestions.gitlab_mr_sync._setting", side_effect=lambda key, default: values[key]),
    ):
        start, reconciled = _sync_window({
            "cursor_at": "2026-08-07T10:00:00+00:00",
            "last_reconcile_at": "2026-08-07T08:00:00+00:00",
        })
        assert start == "2026-08-07T17:50:00+08:00"
        assert not reconciled

        start, reconciled = _sync_window({
            "cursor_at": "2026-08-07T10:00:00+00:00",
            "last_reconcile_at": "2026-08-05T08:00:00+00:00",
        })
        assert start == "2026-08-05T12:00:00+00:00"
        assert reconciled
