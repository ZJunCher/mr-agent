import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from pr_agent.feishu.webhook_handler import handle_gitlab_webhook_event

@pytest.mark.asyncio
async def test_feishu_notification_open_mr():
    # Mock data
    data = {
        "object_kind": "merge_request",
        "user": {"username": "user1"},
        "object_attributes": {
            "action": "open",
            "url": "http://gitlab.com/org/repo/merge_requests/1",
            "title": "Test MR"
        }
    }

    settings = MagicMock()

    with patch("pr_agent.feishu.webhook_handler.FeishuClient") as MockClient:
        client_instance = MockClient.return_value
        client_instance.app_id = "fake_id"
        client_instance.app_secret = "fake_secret"
        client_instance.notify_user = AsyncMock()

        await handle_gitlab_webhook_event(data, settings)

        # Verify
        client_instance.notify_user.assert_called_once()
        args = client_instance.notify_user.call_args[0]
        assert args[0] == "user1"
        assert "Test MR" in args[1]

@pytest.mark.asyncio
async def test_feishu_notification_comment_mr():
    # Mock data
    data = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "reviewer"},
        "merge_request": {
            "url": "http://gitlab.com/org/repo/merge_requests/1",
            "title": "Test MR",
            "iid": 1
        },
        "project": {"id": 100}
    }

    settings = MagicMock()
    settings.get.return_value = "https://gitlab.com"
    settings.gitlab.personal_access_token = "fake_token"

    with patch("pr_agent.feishu.webhook_handler.FeishuClient") as MockClient, \
         patch("gitlab.Gitlab") as MockGitlab:

        client_instance = MockClient.return_value
        client_instance.app_id = "fake_id"
        client_instance.app_secret = "fake_secret"
        client_instance.notify_user = AsyncMock()

        # Mock GitLab API
        gl_instance = MockGitlab.return_value
        project = MagicMock()
        mr = MagicMock()
        mr.author = {"username": "author_user"}
        project.mergerequests.get.return_value = mr
        gl_instance.projects.get.return_value = project

        await handle_gitlab_webhook_event(data, settings)

        # Verify
        client_instance.notify_user.assert_called_once()
        args = client_instance.notify_user.call_args[0]
        assert args[0] == "author_user"
        assert "commented on" in args[1]
