import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.distributed.models import NotificationEnvelope, TriageCardState
from pr_agent.feishu.feishu_client import FeishuClient, FeishuSendResult
from pr_agent.feishu.triage_card import can_transition_triage_card


def _update_notification(message_id: str = "om_538") -> NotificationEnvelope:
    return NotificationEnvelope.new(
        task_id="task-538",
        receive_id="ou_owner",
        recipient_email="",
        recipient_username="",
        kind="card_update",
        content=json.dumps({"header": {"title": {"content": "【eabot/cook !538】修复成功"}}}),
        title="【eabot/cook !538】修复成功",
        header_template="green",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
        card_id="card-538",
        message_id=message_id,
    )


def _patch_session(monkeypatch, *, status: int, payload: dict):
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.patch.return_value = response
    monkeypatch.setattr("pr_agent.feishu.feishu_client.aiohttp.ClientSession", lambda: session)
    return session


def test_card_update_uses_patch_and_original_message_id(monkeypatch):
    async def run_test():
        session = _patch_session(monkeypatch, status=200, payload={"code": 0})
        client = FeishuClient()
        client.get_tenant_access_token = AsyncMock(return_value="tenant-token")

        result = await client.send_notification(_update_notification("om/538"))

        assert session.patch.call_args.args[0].endswith("/im/v1/messages/om%2F538")
        assert session.patch.call_args.kwargs["json"]["content"]
        assert result == FeishuSendResult(True, "om/538", False, "")

    asyncio.run(run_test())


def test_direct_action_card_keeps_repair_validation_fields(monkeypatch):
    async def run_test():
        response = MagicMock()
        response.status = 200
        response.json = AsyncMock(return_value={"code": 0})
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = response
        monkeypatch.setattr("pr_agent.feishu.feishu_client.aiohttp.ClientSession", lambda: session)
        client = FeishuClient()
        client.get_tenant_access_token = AsyncMock(return_value="tenant-token")

        await client.send_action_card(
            "ou_owner",
            "https://gitlab.example/eabot/cook/-/merge_requests/538",
            actions=[
                {
                    "command": "repair-pipeline",
                    "label": "修复流水线",
                    "card_id": "card-538",
                    "pipeline_id": 29415,
                    "category": "pipeline",
                    "pipeline_sha": "abc123",
                    "revision": 4,
                    "secret": "must-not-leak",
                }
            ],
        )

        payload = session.post.call_args.kwargs["json"]
        card = json.loads(payload["content"])
        value = card["elements"][-1]["actions"][0]["value"]
        assert value == {
            "command": "repair-pipeline",
            "mr_url": "https://gitlab.example/eabot/cook/-/merge_requests/538",
            "card_id": "card-538",
            "pipeline_id": 29415,
            "category": "pipeline",
            "pipeline_sha": "abc123",
            "revision": 4,
        }

    asyncio.run(run_test())


def test_interactive_card_notification_uses_complete_card_json():
    notification = NotificationEnvelope.new(
        task_id="pipeline-event-1",
        receive_id="ou_owner",
        recipient_email="",
        recipient_username="",
        kind="interactive_card",
        content=json.dumps({"config": {"update_multi": True}, "elements": []}),
        title="【eabot/cook !538】流水线失败",
        header_template="blue",
        mr_url="https://gitlab.example/eabot/cook/-/merge_requests/538",
    )

    payload = FeishuClient()._notification_payload(notification)

    assert payload["msg_type"] == "interactive"
    assert json.loads(payload["content"]) == {"config": {"update_multi": True}, "elements": []}


def test_waiting_pipeline_can_return_to_running_analysis():
    assert can_transition_triage_card(TriageCardState.WAITING_PIPELINE, TriageCardState.REPAIR_RUNNING)


@pytest.mark.parametrize("code", [230011, 230072, 230075, 230110])
def test_deleted_or_uneditable_card_is_permanent(monkeypatch, code):
    async def run_test():
        _patch_session(monkeypatch, status=400, payload={"code": code, "msg": "uneditable"})
        client = FeishuClient()
        client.get_tenant_access_token = AsyncMock(return_value="tenant-token")

        result = await client.send_notification(_update_notification())

        assert result.ok is False
        assert result.retryable is False
        assert f":{code}:" in result.error

    asyncio.run(run_test())


@pytest.mark.parametrize(("status", "code"), [(500, 0), (429, 0), (401, 0), (400, 230020)])
def test_transient_card_update_failure_is_retryable(monkeypatch, status, code):
    async def run_test():
        _patch_session(monkeypatch, status=status, payload={"code": code, "msg": "retry"})
        client = FeishuClient()
        client.get_tenant_access_token = AsyncMock(return_value="tenant-token")

        result = await client.send_notification(_update_notification())

        assert result.retryable is True

    asyncio.run(run_test())


def test_non_json_server_error_remains_retryable(monkeypatch):
    async def run_test():
        session = _patch_session(monkeypatch, status=502, payload={})
        session.patch.return_value.json.side_effect = ValueError("not json")
        client = FeishuClient()
        client.get_tenant_access_token = AsyncMock(return_value="tenant-token")

        result = await client.send_notification(_update_notification())

        assert result.retryable is True

    asyncio.run(run_test())
