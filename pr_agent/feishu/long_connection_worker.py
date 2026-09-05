import asyncio
import os
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Thread
from typing import Any

from pr_agent.config_loader import get_settings
from pr_agent.feishu.feishu_webhook import handle_feishu_event_payload
from pr_agent.feishu.long_connection_status import mark_error, mark_event, mark_heartbeat, mark_reconnect, mark_started
from pr_agent.log import LoggingFormat, get_logger, setup_logger

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))

FEISHU_CARD_CALLBACK_DEADLINE_SECONDS = 3.0


def normalize_card_action_payload(data: Any) -> dict:
    if isinstance(data, dict):
        event = data.get("event", {}) or {}
        open_id = (event.get("operator") or {}).get("open_id")
        action = event.get("action") or {}
        context = event.get("context") or {}
        value = action.get("value") or {}
        form_value = action.get("form_value") or {}
        action_tag = action.get("tag") or ""
        action_type = action.get("action_type") or ""
        action_name = action.get("name") or ""
        trigger_time = action.get("trigger_time") or ""
        header = data.get("header") or {}
        event_id = header.get("event_id") or ""
        create_time = header.get("create_time") or ""
    else:
        event = getattr(data, "event", None)
        operator = getattr(event, "operator", None)
        open_id = getattr(operator, "open_id", None)
        action = getattr(event, "action", None)
        context = getattr(event, "context", None)
        value = getattr(action, "value", None) or {}
        form_value = getattr(action, "form_value", None) or {}
        action_tag = getattr(action, "tag", None) or ""
        action_type = getattr(action, "action_type", None) or ""
        action_name = getattr(action, "name", None) or ""
        trigger_time = getattr(action, "trigger_time", None) or ""
        header = getattr(data, "header", None)
        event_id = getattr(header, "event_id", None) or ""
        create_time = getattr(header, "create_time", None) or ""
    if isinstance(context, dict):
        open_message_id = context.get("open_message_id") or ""
    else:
        open_message_id = getattr(context, "open_message_id", None) or ""
    return {
        "header": {
            "event_type": "card.action.trigger",
            "event_id": event_id,
            "create_time": str(create_time),
        },
        "event": {
            "operator": {"open_id": open_id},
            "action": {
                "value": value,
                "form_value": form_value,
                "tag": action_tag,
                "action_type": action_type,
                "name": action_name,
                "trigger_time": trigger_time,
            },
            "context": {"open_message_id": open_message_id},
        },
    }


class FeishuLongConnectionWorker:
    """Consume Feishu events via long connection and reuse webhook business logic."""

    def __init__(self, *, loop: asyncio.AbstractEventLoop | None = None, queue_ingress=None):
        self.app_id = self._read_feishu_setting("APP_ID", "FEISHU_APP_ID")
        self.app_secret = self._read_feishu_setting("APP_SECRET", "FEISHU_APP_SECRET")
        self.retry_seconds = int(get_settings().get("FEISHU.LONG_CONNECTION_RETRY_SECONDS", 5) or 5)
        self.callback_timeout_seconds = float(
            get_settings().get("FEISHU.CARD_CALLBACK_TIMEOUT_SECONDS", 2.5) or 2.5
        )
        if not 0 < self.callback_timeout_seconds < FEISHU_CARD_CALLBACK_DEADLINE_SECONDS:
            raise ValueError("feishu.card_callback_timeout_seconds must be greater than 0 and less than 3")
        self.loop = loop
        self.queue_ingress = queue_ingress

    @staticmethod
    def _read_feishu_setting(setting_key: str, env_key: str) -> str:
        """Read Feishu settings with env var precedence for container deployments."""
        env_val = (os.environ.get(env_key) or "").strip()
        if env_val:
            return env_val
        return str(get_settings().get(f"FEISHU.{setting_key}", "") or "").strip()

    @staticmethod
    def _normalize_event_payload(event: Any) -> dict:
        """
        Best-effort payload normalization for different SDK callback payload shapes.
        """
        if isinstance(event, dict):
            return event

        for attr in ("to_dict", "as_dict"):
            fn = getattr(event, attr, None)
            if callable(fn):
                data = fn()
                if isinstance(data, dict):
                    return data

        for attr in ("raw", "raw_event", "event", "body"):
            data = getattr(event, attr, None)
            if isinstance(data, dict):
                return data

        return {}

    @staticmethod
    def _extract_sdk_payload(payload: dict) -> dict:
        """
        Normalize SDK envelope to the callback payload expected by our business logic.
        """
        if not payload:
            return {}

        # Some SDK callbacks wrap data under "event"
        event_data = payload.get("event")
        if isinstance(event_data, dict) and "header" in event_data:
            return event_data

        # Some SDK callbacks use "data"
        data = payload.get("data")
        if isinstance(data, dict) and "header" in data:
            return data

        return payload

    def _run_async(self, coro, *, wait: bool = False):
        if self.loop is not None:
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            if wait:
                try:
                    return future.result(timeout=self.callback_timeout_seconds)
                except FutureTimeoutError as error:
                    future.cancel()
                    raise RuntimeError("Feishu callback queue operation timed out") from error
            return future
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                future = loop.create_task(coro)
                return future
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    def _on_message_event(self, event: Any):
        payload = self._normalize_event_payload(event)
        callback_payload = self._extract_sdk_payload(payload)
        if not callback_payload:
            get_logger().warning("Received empty payload from Feishu long connection callback")
            return

        mark_event()
        self._run_async(handle_feishu_event_payload(callback_payload, queue_ingress=self.queue_ingress))

    def _on_card_action(self, data: Any):
        """Handle card.action.trigger callback (interactive card button clicked)."""
        try:
            payload = normalize_card_action_payload(data)
            from pr_agent.feishu.feishu_webhook import handle_feishu_card_action
            response_payload = self._run_async(
                handle_feishu_card_action(payload, queue_ingress=self.queue_ingress), wait=True
            )
        except Exception as e:
            get_logger().error(f"Error handling Feishu card action from long connection: {e}")
            response_payload = {"toast": {"type": "error", "content": "处理失败，请稍后重试"}}

        # Best-effort card callback response (toast shown to the user who clicked)
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
            return P2CardActionTriggerResponse(response_payload)
        except Exception:
            return None

    def _build_dispatcher(self, lark_module):
        builder_cls = getattr(lark_module, "EventDispatcherHandler", None)
        if builder_cls is None:
            raise RuntimeError("Unsupported lark-oapi SDK: EventDispatcherHandler not found")

        builder = builder_cls.builder(self.app_id, self.app_secret)
        register = getattr(builder, "register_p2_im_message_receive_v1", None)
        if not callable(register):
            raise RuntimeError("Unsupported lark-oapi SDK: register_p2_im_message_receive_v1 not found")
        builder = register(self._on_message_event)

        register_card = getattr(builder, "register_p2_card_action_trigger", None)
        if callable(register_card):
            builder = register_card(self._on_card_action)
            get_logger().info("Registered Feishu card.action.trigger handler for interactive card buttons")
        else:
            get_logger().warning("lark-oapi SDK lacks register_p2_card_action_trigger; card buttons disabled")

        return builder.build()

    def _build_ws_client(self, lark_module, event_handler):
        ws_module = getattr(lark_module, "ws", None)
        if ws_module is None:
            raise RuntimeError("Unsupported lark-oapi SDK: ws module not found")

        client_cls = getattr(ws_module, "Client", None)
        if client_cls is None:
            raise RuntimeError("Unsupported lark-oapi SDK: ws.Client not found")

        log_level = getattr(lark_module, "LogLevel", None)
        sdk_log_level = getattr(log_level, "INFO", None) if log_level else None

        kwargs = {"event_handler": event_handler}
        if sdk_log_level is not None:
            kwargs["log_level"] = sdk_log_level

        return client_cls(self.app_id, self.app_secret, **kwargs)

    @staticmethod
    def _health_check_loop(app_id: str, interval: int = 10):
        """Periodic health check logger to monitor worker liveness."""
        while True:
            mark_heartbeat()
            get_logger().info(f"Feishu long-connection worker heartbeat (app_id={app_id}, interval={interval}s)")
            time.sleep(interval)

    def start(self):
        if not self.app_id or not self.app_secret:
            raise ValueError("FEISHU.APP_ID / FEISHU.APP_SECRET is required for long connection worker")

        try:
            import lark_oapi as lark
        except Exception as e:
            raise RuntimeError(
                "Missing dependency lark-oapi. Install it before starting long connection worker."
            ) from e

        event_handler = self._build_dispatcher(lark)

        get_logger().info(
            f"Starting Feishu long-connection worker for app_id={self.app_id}, retry_seconds={self.retry_seconds}"
        )
        get_logger().info(
            "Long connection is bound to this specific Feishu app by app_id/app_secret credentials"
        )
        mark_started(self.app_id, self.retry_seconds)

        # Start health check in background thread (immediately, then every 10 seconds)
        health_thread = Thread(
            target=self._health_check_loop,
            args=(self.app_id,),
            daemon=True,
            name="feishu-health-check",
        )
        health_thread.start()

        while True:
            try:
                mark_reconnect()
                ws_client = self._build_ws_client(lark, event_handler)
                ws_client.start()
                get_logger().warning("Feishu long-connection ws client exited, restarting")
            except Exception as e:
                mark_error(str(e))
                get_logger().exception("Feishu long-connection ws client failed")
            time.sleep(self.retry_seconds)


def start():
    FeishuLongConnectionWorker().start()


if __name__ == "__main__":
    start()
