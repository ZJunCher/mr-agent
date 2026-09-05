import json
import os
import time
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from pr_agent.config_loader import get_settings
from pr_agent.distributed.models import NotificationEnvelope
from pr_agent.log import get_logger

# Module-level cache shared across FeishuClient instances: email -> (open_id, expire_ts)
_email_open_id_cache: dict = {}
_EMAIL_CACHE_TTL = 3600


@dataclass(frozen=True)
class FeishuSendResult:
    ok: bool
    message_id: str | None
    retryable: bool
    error: str


class FeishuClient:
    def __init__(self):
        self.app_id = get_settings().get("FEISHU.APP_ID", os.environ.get("FEISHU_APP_ID"))
        self.app_secret = get_settings().get("FEISHU.APP_SECRET", os.environ.get("FEISHU_APP_SECRET"))
        self.base_url = "https://open.feishu.cn/open-apis"
        self.token = None
        self.token_expire_time = 0
        self.user_map = {}

    def _load_user_map(self):
        # User map is no longer used, as we now reply directly to the sender via Feishu Open ID
        return {}

    async def get_tenant_access_token(self):
        if not self.app_id or not self.app_secret:
            get_logger().warning("Feishu App ID or Secret not configured")
            return None

        if self.token and time.time() < self.token_expire_time:
            return self.token

        url = f"{self.base_url}/auth/v3/tenant_access_token/internal"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status != 200:
                        get_logger().error(f"Failed to get Feishu token: {response.status}")
                        return None
                    data = await response.json()
                    if data.get("code") != 0:
                        get_logger().error(f"Feishu token error: {data}")
                        return None
                    
                    self.token = data["tenant_access_token"]
                    self.token_expire_time = time.time() + data["expire"] - 60  # Buffer
                    return self.token
            except Exception as e:
                get_logger().error(f"Exception getting Feishu token: {e}")
                return None

    async def send_notification(self, notification: NotificationEnvelope) -> FeishuSendResult:
        token = await self.get_tenant_access_token()
        if not token:
            return FeishuSendResult(False, None, True, "tenant_access_token_unavailable")
        if notification.kind == "card_update":
            return await self.update_notification(notification, token)
        if not notification.receive_id:
            return FeishuSendResult(False, None, False, "receive_id_missing")

        url = f"{self.base_url}/im/v1/messages?receive_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = self._notification_payload(notification)
        payload["uuid"] = notification.notification_id
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    data = await self._response_json(response)
                    code = data.get("code")
                    if response.status == 200 and code == 0:
                        message_id = (data.get("data") or {}).get("message_id")
                        return FeishuSendResult(True, message_id, False, "")
                    retryable = self._retryable_response(response.status, code)
                    if response.status == 401:
                        self.token = None
                    return FeishuSendResult(
                        False,
                        None,
                        retryable,
                        f"{response.status}:{code}:{data.get('msg', '')}",
                    )
        except (aiohttp.ClientError, TimeoutError) as error:
            return FeishuSendResult(False, None, True, str(error))
        except Exception as error:
            return FeishuSendResult(False, None, False, str(error))

    async def update_notification(
        self,
        notification: NotificationEnvelope,
        token: str,
    ) -> FeishuSendResult:
        if not notification.message_id:
            return FeishuSendResult(False, None, False, "message_id_missing")
        message_id = quote(notification.message_id, safe="")
        url = f"{self.base_url}/im/v1/messages/{message_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.patch(
                    url,
                    json={"content": notification.content},
                    headers=headers,
                ) as response:
                    data = await self._response_json(response)
                    code = data.get("code")
                    if response.status == 200 and code == 0:
                        return FeishuSendResult(True, notification.message_id, False, "")
                    retryable = self._retryable_response(response.status, code)
                    if response.status == 401:
                        self.token = None
                    return FeishuSendResult(
                        False,
                        None,
                        retryable,
                        f"{response.status}:{code}:{data.get('msg', '')}",
                    )
        except (aiohttp.ClientError, TimeoutError) as error:
            return FeishuSendResult(False, None, True, str(error))
        except Exception as error:
            return FeishuSendResult(False, None, False, str(error))

    @staticmethod
    def _retryable_response(status_code: int, feishu_code) -> bool:
        return status_code >= 500 or status_code in {401, 408, 409, 425, 429} or feishu_code in {230020, 230049}

    @staticmethod
    async def _response_json(response) -> dict:
        try:
            value = await response.json(content_type=None)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _action_button_value(item: dict, mr_url: str) -> dict:
        value = {
            "command": str(item.get("command") or "").strip(),
            "mr_url": str(item.get("mr_url") or mr_url),
        }
        for key in ("card_id", "pipeline_id", "category", "pipeline_sha", "revision"):
            if item.get(key) not in (None, ""):
                value[key] = item[key]
        return value

    def _notification_payload(self, notification: NotificationEnvelope) -> dict:
        if notification.kind == "text":
            return {
                "receive_id": notification.receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": notification.content}, ensure_ascii=False),
            }

        if notification.kind == "interactive_card":
            card = json.loads(notification.content)
            if not isinstance(card, dict):
                raise ValueError("interactive_card content must be a JSON object")
            return {
                "receive_id": notification.receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            }

        markdown = notification.content
        actions = []
        if notification.kind == "action_card":
            parsed = json.loads(notification.content)
            markdown = str(parsed.get("markdown") or "")
            actions = list(parsed.get("actions") or [])
        elements = self._markdown_elements(markdown)
        if actions:
            buttons = []
            for item in actions:
                command = str(item.get("command") or "").strip()
                if not command:
                    continue
                value = self._action_button_value(item, notification.mr_url)
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": str(item.get("label") or command)},
                        "type": str(item.get("type") or "primary"),
                        "value": value,
                    }
                )
            if buttons:
                elements.append({"tag": "action", "actions": buttons})
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": notification.title},
                "template": notification.header_template,
            },
            "elements": elements,
        }
        return {
            "receive_id": notification.receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }

    @staticmethod
    def _markdown_elements(markdown: str) -> list[dict]:
        max_block = 4000
        elements = []
        for index, start in enumerate(range(0, len(markdown), max_block)):
            if index:
                elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": markdown[start : start + max_block]})
        return elements or [{"tag": "markdown", "content": ""}]

    async def send_message(self, receive_id, content):
        token = await self.get_tenant_access_token()
        if not token:
            return

        url = f"{self.base_url}/im/v1/messages?receive_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        
        # Content needs to be a JSON string inside the JSON payload
        msg_content = json.dumps({"text": content})
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": msg_content
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status != 200:
                        get_logger().error(f"Failed to send Feishu message: {response.status}")
                        return
                    data = await response.json()
                    if data.get("code") != 0:
                        get_logger().error(f"Feishu send message error: {data}")
                    else:
                        get_logger().info(f"Sent Feishu message to {receive_id}")
            except Exception as e:
                get_logger().error(f"Exception sending Feishu message: {e}")

    @staticmethod
    def rating_elements(mr_url: str = "", label: str = "请对本次 PR-Agent 输出评分：") -> list:
        """
        Return card elements for a 1-5 star rating row.
        Button value: {"action": "rate", "score": N, "mr_url": mr_url}
        """
        # score 1-2 -> danger(red), 3 -> default(gray), 4-5 -> primary(blue)
        type_map = {1: "danger", 2: "danger", 3: "default", 4: "primary", 5: "primary"}
        buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": str(score)},
                "type": type_map[score],
                "value": {"action": "rate", "score": score, "mr_url": mr_url},
            }
            for score in range(1, 6)
        ]
        return [
            {"tag": "hr"},
            {"tag": "markdown", "content": f"**{label}**"},
            {"tag": "action", "actions": buttons},
        ]

    async def send_markdown(self, receive_id, markdown_content, title="PR-Agent", header_template="blue",
                            show_rating: bool = False, mr_url: str = ""):
        """发送带 header 的飞书交互卡片（富文本消息）"""
        if not markdown_content:
            get_logger().warning("Empty markdown content, skip")
            return
        token = await self.get_tenant_access_token()
        if not token:
            return
        if not receive_id:
            get_logger().warning("Empty receive_id, skip")
            return

        url = f"{self.base_url}/im/v1/messages?receive_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }

        # 飞书卡片 1.0 结构：带 header + wide_screen_mode，支持 markdown
        # markdown element 单块内容上限约 4000 字符，超出时拆分
        MAX_BLOCK = 4000
        chunks = [markdown_content[i:i + MAX_BLOCK] for i in range(0, len(markdown_content), MAX_BLOCK)]

        elements = []
        for i, chunk in enumerate(chunks):
            if i > 0:
                elements.append({"tag": "hr"})  # 分隔线
            elements.append({"tag": "markdown", "content": chunk})

        if show_rating:
            elements.extend(self.rating_elements(mr_url=mr_url))

        card_content = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": header_template,
            },
            "elements": elements,
        }

        payload = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card_content, ensure_ascii=False)
        }
        get_logger().debug(f"Feishu interactive payload: {payload}")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers) as response:
                    resp_text = await response.text()
                    get_logger().debug(f"Feishu markdown response: {response.status}, {resp_text}")
                    if response.status != 200:
                        get_logger().error(f"Failed to send Feishu markdown: {response.status}, {resp_text}")
                        return
                    data = await response.json()
                    if data.get("code") != 0:
                        error_code = data.get("code")
                        error_msg = data.get("msg")
                        if error_code == 230013:
                            get_logger().error(
                                f"Feishu send markdown error (230013): {error_msg}. "
                                "Hint: This usually means the user is not in the bot's availability scope. "
                                "Please check 'Availability' settings in Feishu Developer Console."
                            )
                        else:
                            get_logger().error(f"Feishu send markdown error: {data}")
                    else:
                        get_logger().info(f"Sent Feishu markdown to {receive_id}")
            except Exception as e:
                get_logger().error(f"Exception sending Feishu markdown: {e}")

    async def get_open_id_by_email(self, email):
        """Resolve a Feishu open_id from an email via contact batch_get_id API (cached)."""
        if not email:
            return None
        cached = _email_open_id_cache.get(email)
        if cached and time.time() < cached[1]:
            return cached[0]

        token = await self.get_tenant_access_token()
        if not token:
            return None

        url = f"{self.base_url}/contact/v3/users/batch_get_id?user_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        payload = {"emails": [email]}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers) as response:
                    data = await response.json()
                    if response.status != 200 or data.get("code") != 0:
                        get_logger().error(f"Feishu batch_get_id error: {response.status}, {data}. "
                                           f"Hint: requires 'contact:user.employee_id:readonly' permission.")
                        return None
                    user_list = data.get("data", {}).get("user_list", [])
                    open_id = None
                    for item in user_list:
                        if item.get("email") == email and item.get("user_id"):
                            open_id = item["user_id"]
                            break
                    if open_id:
                        _email_open_id_cache[email] = (open_id, time.time() + _EMAIL_CACHE_TTL)
                        get_logger().info(f"Resolved Feishu open_id for email {email}")
                    else:
                        get_logger().debug(f"No Feishu user found for email {email}")
                    return open_id
            except Exception as e:
                get_logger().error(f"Exception resolving Feishu open_id by email: {e}")
                return None

    async def resolve_open_id_for_gitlab_user(self, gitlab_username, email=None):
        """
        Map a GitLab user to a Feishu open_id.
        Order: static map (FEISHU.USER_MAP) -> webhook payload email -> <username>@FEISHU.EMAIL_DOMAIN
        """
        user_map = get_settings().get("FEISHU.USER_MAP", {}) or {}
        if gitlab_username:
            open_id = user_map.get(gitlab_username)
            if open_id:
                return open_id

        candidate_emails = []
        if email and "@" in email and "REDACTED" not in email.upper():
            candidate_emails.append(email)
        email_domain = get_settings().get("FEISHU.EMAIL_DOMAIN", "")
        if gitlab_username and email_domain:
            composed = f"{gitlab_username}@{email_domain}"
            if composed not in candidate_emails:
                candidate_emails.append(composed)

        for candidate in candidate_emails:
            open_id = await self.get_open_id_by_email(candidate)
            if open_id:
                return open_id
        return None

    async def get_user_display_name(self, open_id: str) -> str:
        """Resolve one Feishu Open ID to a readable display name without raising."""
        if not open_id:
            return ""
        token = await self.get_tenant_access_token()
        if not token:
            return ""
        user_id = quote(open_id, safe="")
        url = f"{self.base_url}/contact/v3/users/{user_id}?user_id_type=open_id"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    data = await response.json()
                    if response.status != 200 or data.get("code") != 0:
                        get_logger().warning(
                            f"Feishu user lookup failed for Open ID: status={response.status}, code={data.get('code')}"
                        )
                        return ""
                    return str(((data.get("data") or {}).get("user") or {}).get("name") or "").strip()
        except Exception as error:
            get_logger().warning(f"Feishu user lookup failed: {error}")
            return ""

    async def send_action_card(self, receive_id, mr_url, markdown_content="", title="PR-Agent", actions=None):
        """Send an interactive card with optional action buttons for the given MR."""
        if not receive_id or not mr_url:
            get_logger().warning("send_action_card: missing receive_id or mr_url, skip")
            return
        token = await self.get_tenant_access_token()
        if not token:
            return

        if not markdown_content:
            markdown_content = f"MR: [{mr_url}]({mr_url})\n**请按需选择操作。**"

        button_actions = []
        for item in actions or []:
            command = (item.get("command") or "").strip()
            label = (item.get("label") or command or "操作").strip()
            button_type = (item.get("type") or "primary").strip()
            if not command:
                continue
            value = self._action_button_value(item, mr_url)
            button_actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": button_type,
                "value": value,
            })

        elements = [{"tag": "markdown", "content": markdown_content}]
        if button_actions:
            elements.append({"tag": "action", "actions": button_actions})

        card_content = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
            "elements": elements,
        }

        url = f"{self.base_url}/im/v1/messages?receive_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        payload = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card_content, ensure_ascii=False),
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers) as response:
                    data = await response.json()
                    if response.status != 200 or data.get("code") != 0:
                        get_logger().error(f"Feishu send action card error: {response.status}, {data}")
                    else:
                        get_logger().info(f"Sent Feishu action card to {receive_id} for {mr_url}")
            except Exception as e:
                get_logger().error(f"Exception sending Feishu action card: {e}")

    async def notify_user(self, gitlab_username, message):
        feishu_id = self.user_map.get(gitlab_username)
        if not feishu_id:
            get_logger().debug(f"No Feishu mapping found for GitLab user: {gitlab_username}")
            return
        
        await self.send_message(feishu_id, message)

    async def send_markdown_to_user(self, gitlab_username, markdown_content):
        """直接把 Markdown 内容私聊给指定 GitLab 用户"""
        feishu_id = self.user_map.get(gitlab_username)
        if not feishu_id:
            get_logger().debug(f"No Feishu mapping found for GitLab user: {gitlab_username}")
            return
        await self.send_markdown(feishu_id, markdown_content)
