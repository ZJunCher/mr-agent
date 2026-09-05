import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.config_loader import get_settings
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.ingress import QueueIngress
from pr_agent.distributed.models import TriageCardState
from pr_agent.feishu.feishu_client import FeishuClient
from pr_agent.feishu.feishu_git_provider import FeishuGitProvider
from pr_agent.git_providers import git_provider_factory_context
from pr_agent.log import get_logger

router = APIRouter()

_REPAIR_CATEGORY_ORDER = ("format", "clang", "build", "unknown")


def normalize_selected_categories(value: object) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("selected repair categories must be an array")
    normalized = [str(item).strip().lower() for item in value]
    if len(set(normalized)) != len(normalized):
        raise ValueError("selected repair categories contain duplicates")
    invalid = [item for item in normalized if item not in _REPAIR_CATEGORY_ORDER]
    if invalid:
        raise ValueError(f"unsupported repair categories: {','.join(invalid)}")
    selected = set(normalized)
    return tuple(category for category in _REPAIR_CATEGORY_ORDER if category in selected)


def _card_action_age_seconds(data: dict) -> float | None:
    """Return the callback age from Feishu's event header, accepting seconds, milliseconds, or ISO timestamps."""
    raw = str((data.get("header") or {}).get("create_time") or "").strip()
    if not raw:
        return None
    try:
        timestamp = float(raw)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
    except ValueError:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    return max(0.0, datetime.now(timezone.utc).timestamp() - timestamp)


def _card_action_is_stale(data: dict) -> bool:
    age = _card_action_age_seconds(data)
    if age is None:
        return False
    max_age = int(get_settings().get("FEISHU.CARD_CALLBACK_MAX_AGE_SECONDS", 300) or 300)
    if max_age <= 0:
        raise ValueError("feishu.card_callback_max_age_seconds must be positive")
    return age > max_age


def trigger_pr_agent_command(command: str, mr_url: str, sender_id: str):
    """
    Register the Feishu requester for result routing and run the PR-Agent command
    in background. Shared by text-message flow and card-button flow.
    """
    import asyncio

    from pr_agent.feishu.webhook_handler import pending_feishu_requests

    agent_command = command if command.startswith("/") else f"/{command}"

    pending_feishu_requests[mr_url] = sender_id
    get_logger().info(f"Registered pending Feishu request: {mr_url} -> {sender_id}")

    get_settings().set("CONFIG.GIT_PROVIDER", "gitlab")
    get_settings().set("PR_DESCRIPTION.PERSISTENT_COMMENT", False)
    get_settings().set("PR_REVIEW.PERSISTENT_COMMENT", False)
    get_settings().set("PR_CODE_SUGGESTIONS.PERSISTENT_COMMENT", False)

    asyncio.create_task(run_pr_agent(mr_url, agent_command, sender_id))


def _card_action_idempotency_key(data: dict, command: str, mr_url: str, sender_id: str) -> str:
    header = data.get("header") or {}
    event_id = str(header.get("event_id") or "").strip()
    if event_id:
        return f"feishu-card:{event_id}"
    action = (data.get("event") or {}).get("action") or {}
    trigger_time = str(action.get("trigger_time") or "")
    raw = f"{sender_id}\0{command}\0{mr_url}\0{trigger_time}".encode()
    return f"feishu-card:{hashlib.sha256(raw).hexdigest()}"


def _default_queue_ingress() -> QueueIngress:
    from pr_agent.servers.gitlab_webhook import _get_queue_ingress

    return _get_queue_ingress()


async def handle_feishu_card_action(data: dict, queue_ingress: QueueIngress | None = None) -> dict:
    """
    Handle card.action.trigger callback (button click on an interactive card).
    Expected action value: {"command": "review", "mr_url": "https://..."}
    Returns a toast payload for the card callback response.
    """
    try:
        event = data.get("event", {}) or {}
        operator = event.get("operator", {}) or {}
        sender_id = operator.get("open_id")
        action_payload = event.get("action", {}) or {}
        value = action_payload.get("value", {}) or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                value = {}

        command = (value.get("command") or "").strip().lower()
        mr_url = (value.get("mr_url") or "").strip()
        card_id = str(value.get("card_id") or "").strip()
        task_id = str(value.get("task_id") or "").strip()
        repair_task_id = str(value.get("repair_task_id") or "").strip()
        pipeline_id = value.get("pipeline_id")
        open_message_id = str((event.get("context") or {}).get("open_message_id") or "").strip()
        category = str(value.get("category") or "").strip().lower()
        selected_categories: tuple[str, ...] = ()
        multi_select_submit = (
            command == "repair-pipeline"
            and str(value.get("repair_card_mode") or "").strip().lower() == "multi_select"
        )
        if multi_select_submit:
            form_value = action_payload.get("form_value") or {}
            if not isinstance(form_value, dict):
                return {"toast": {"type": "error", "content": "修复类别参数无效，请刷新卡片后重试"}}
            try:
                selected_categories = normalize_selected_categories(form_value.get("selected_categories"))
            except ValueError:
                return {"toast": {"type": "error", "content": "修复类别参数无效，请刷新卡片后重试"}}
            if not selected_categories:
                return {"toast": {"type": "error", "content": "请至少选择一个修复类别"}}
            category = "batch"
        pipeline_sha = str(value.get("pipeline_sha") or "").strip()
        revision_value = value.get("revision")
        revision = int(revision_value) if revision_value not in (None, "") else None
        action_name = (value.get("action") or "").strip().lower()

        # ── 评分按钮回调 ──
        if action_name == "rate":
            score = value.get("score")
            get_logger().info(f"Feishu rating: score={score} mr_url={mr_url} by {sender_id}")
            thanks = {1: "😢", 2: "😕", 3: "😐", 4: "🙂", 5: "🎉"}.get(score, "")
            await FeishuClient().send_message(sender_id, f"{thanks} 感谢评分 {score}/5！")
            return {"toast": {"type": "success", "content": f"已记录评分 {score} 分，谢谢反馈！"}}

        if not command or not mr_url or not sender_id:
            get_logger().warning(f"Feishu card action missing fields: command={command}, mr_url={mr_url}")
            return {"toast": {"type": "error", "content": "无效的按钮参数"}}

        if _card_action_is_stale(data):
            get_logger().warning(
                f"Ignored stale Feishu card action: command={command}, mr_url={mr_url}, sender_id={sender_id}"
            )
            return {"toast": {"type": "error", "content": "该操作已过期，请使用最新流水线卡片"}}

        get_logger().info(f"Feishu card action: {command} {mr_url} by {sender_id}")
        distributed_settings = load_distributed_settings()
        # A Queue-mode Feishu worker must never execute long PR-Agent work in its callback loop.
        # The project allowlist only controls GitLab ingress and full card-correlation rollout.
        if distributed_settings.execution_mode == "queue":
            from pr_agent.distributed.notifications import (
                triage_card_updates_enabled,
                unified_pipeline_repair_enabled,
            )

            ingress = queue_ingress or _default_queue_ingress()
            correlate_card = triage_card_updates_enabled() and bool(card_id and open_message_id)
            if command == "supplement-unit-tests":
                if (
                    not correlate_card
                    or not repair_task_id
                    or not pipeline_id
                    or not pipeline_sha
                    or revision is None
                ):
                    return {"toast": {"type": "error", "content": "补测按钮参数已失效，请使用最新卡片"}}
                try:
                    result = await ingress.enqueue_post_repair_ut(
                        repair_task_id=repair_task_id,
                        mr_url=mr_url,
                        sender_id=sender_id,
                        idempotency_key=_card_action_idempotency_key(data, command, mr_url, sender_id),
                        card_id=card_id,
                        open_message_id=open_message_id,
                        pipeline_id=int(pipeline_id),
                        pipeline_sha=pipeline_sha,
                        revision=revision,
                    )
                except Exception as error:
                    from pr_agent.distributed.broker import RepairAlreadyRunningError, StaleCardActionError

                    if isinstance(error, StaleCardActionError):
                        return {"toast": {"type": "error", "content": "卡片状态已更新，请使用最新按钮"}}
                    if isinstance(error, RepairAlreadyRunningError):
                        return {"toast": {"type": "warning", "content": "当前 MR 已有任务正在处理，请等待完成"}}
                    raise
                content = "已进入单元测试补充队列" if result.created else "补测请求已收到，请勿重复点击"
                return {"toast": {"type": "success", "content": content}}
            if command == "cancel-unit-tests":
                if not correlate_card or not task_id or revision is None:
                    return {"toast": {"type": "error", "content": "取消按钮参数已失效，请使用最新卡片"}}
                try:
                    result = await ingress.cancel_post_repair_ut(
                        task_id=task_id,
                        card_id=card_id,
                        open_message_id=open_message_id,
                        sender_id=sender_id,
                        revision=revision,
                    )
                except Exception as error:
                    from pr_agent.distributed.broker import StaleCardActionError

                    if isinstance(error, StaleCardActionError):
                        return {"toast": {"type": "error", "content": "卡片状态已更新，请使用最新按钮"}}
                    raise
                content = "正在取消补测" if result.accepted else "取消请求已收到，请勿重复点击"
                return {"toast": {"type": "success", "content": content}}
            if command == "rollback-repair":
                if not correlate_card or not repair_task_id or revision is None:
                    return {"toast": {"type": "error", "content": "撤回按钮参数已失效，请使用最新卡片"}}
                try:
                    result = await ingress.rollback_feishu_repair(
                        repair_task_id=repair_task_id,
                        card_id=card_id,
                        open_message_id=open_message_id,
                        sender_id=sender_id,
                        revision=revision,
                    )
                except Exception as error:
                    from pr_agent.distributed.broker import (
                        RepairAlreadyRunningError,
                        RepairRollbackUnavailable,
                        StaleCardActionError,
                        UnauthorizedRepairRollback,
                    )

                    if isinstance(error, UnauthorizedRepairRollback):
                        content = "仅 MR 作者可执行此操作"
                    elif isinstance(error, RepairRollbackUnavailable):
                        content = "本次修复缺少完整提交记录，无法自动撤回"
                    elif isinstance(error, RepairAlreadyRunningError):
                        content = "该 MR 当前有其他任务正在处理"
                    elif isinstance(error, StaleCardActionError):
                        content = "卡片状态已更新，请使用最新按钮"
                    else:
                        raise
                    return {"toast": {"type": "error", "content": content}}
                content = "已进入撤回队列" if result.created else "撤回请求已收到，请勿重复点击"
                return {"toast": {"type": "success", "content": content}}
            if command == "cancel-repair":
                if not correlate_card or not task_id or revision is None:
                    return {"toast": {"type": "error", "content": "取消按钮参数已失效，请使用最新卡片"}}
                try:
                    result = await ingress.cancel_feishu_repair(
                        task_id=task_id,
                        card_id=card_id,
                        open_message_id=open_message_id,
                        sender_id=sender_id,
                        revision=revision,
                    )
                except Exception as error:
                    from pr_agent.distributed.broker import StaleCardActionError

                    if isinstance(error, StaleCardActionError):
                        return {"toast": {"type": "error", "content": "卡片状态已更新，请使用最新按钮"}}
                    raise
                if result.terminal_status == "canceled":
                    return {"toast": {"type": "success", "content": "修复已取消"}}
                content = "正在取消修复" if result.accepted else "取消请求已收到，请勿重复点击"
                return {"toast": {"type": "success", "content": content}}
            repair_commands = {"triage", "fix-format", "fix_format", "repair-pipeline"}
            if (
                correlate_card
                and unified_pipeline_repair_enabled()
                and command == "repair-pipeline"
                and (not category or not pipeline_sha or revision is None)
            ):
                from pr_agent.distributed.broker import StaleCardActionError

                try:
                    category, pipeline_sha, revision = await ingress.resolve_unified_repair_card_action(
                        mr_url=mr_url,
                        card_id=card_id,
                        pipeline_id=int(pipeline_id or 0),
                    )
                except StaleCardActionError:
                    return {"toast": {"type": "error", "content": "卡片状态已更新，请使用最新按钮"}}
            if (
                correlate_card
                and unified_pipeline_repair_enabled()
                and command in repair_commands
                and (command != "repair-pipeline" or category != "pipeline")
            ):
                return {"toast": {"type": "error", "content": "旧卡片已失效，请使用最新流水线卡片"}}
            try:
                result = await ingress.enqueue_feishu_command(
                    command=command,
                    mr_url=mr_url,
                    sender_id=sender_id,
                    idempotency_key=_card_action_idempotency_key(data, command, mr_url, sender_id),
                    event=data,
                    card_id=card_id if correlate_card else "",
                    open_message_id=open_message_id if correlate_card else "",
                    category=category if correlate_card else "",
                    selected_categories=selected_categories if correlate_card else (),
                    pipeline_id=int(pipeline_id) if correlate_card and pipeline_id else None,
                    pipeline_sha=pipeline_sha if correlate_card else "",
                    revision=revision if correlate_card else None,
                )
            except Exception as error:
                from pr_agent.distributed.broker import RepairAlreadyRunningError, StaleCardActionError

                if isinstance(error, StaleCardActionError):
                    return {"toast": {"type": "error", "content": "卡片状态已更新，请使用最新按钮"}}
                if isinstance(error, RepairAlreadyRunningError):
                    return {"toast": {"type": "warning", "content": "当前 MR 已有修复任务，请等待完成"}}
                raise
            if correlate_card:
                await ingress.queue_triage_card_update(
                    result.task_id,
                    TriageCardState.REPAIR_QUEUED,
                    "已进入修复队列",
                )
            if result.recovered:
                content = "任务状态已恢复，已重新进入修复队列"
            elif result.created:
                content = (
                    "已提交所选问题，正在进入修复队列"
                    if multi_select_submit
                    else f"已触发 {command}，结果稍后私聊发送"
                )
            else:
                content = "该操作已收到，请勿重复点击"
            return {"toast": {"type": "success", "content": content}}

        if command != "triage":
            await FeishuClient().send_message(sender_id, f"Received command: /{command} for {mr_url}. Processing...")
        trigger_pr_agent_command(command, mr_url, sender_id)
        return {"toast": {"type": "success", "content": f"已触发 {command}，结果稍后私聊发送"}}
    except Exception as e:
        get_logger().error(f"Error handling Feishu card action: {e}")
        return {"toast": {"type": "error", "content": "处理失败，请查看服务日志"}}


async def handle_feishu_event_payload(data: dict, queue_ingress: QueueIngress | None = None):
    """
    Handle Feishu callback payload directly, so webhook and long-connection
    consumers can share exactly the same business logic.
    """
    try:
        # 1. URL Verification Challenge
        # Feishu sends a challenge when you configure the webhook URL
        if data.get("type") == "url_verification":
            return {"challenge": data.get("challenge")}

        # 2. Handle Event
        # V2.0 event structure
        header = data.get("header", {})
        event_type = header.get("event_type")

        if event_type == "card.action.trigger":
            return await handle_feishu_card_action(data, queue_ingress=queue_ingress)

        if event_type == "im.message.receive_v1":
            event = data.get("event", {})
            message = event.get("message", {})
            sender = event.get("sender", {})
            sender_id = sender.get("sender_id", {}).get("open_id")
            chat_type = message.get("chat_type")
            msg_type = message.get("message_type")
            content_str = message.get("content", "{}")

            # Only handle private text messages
            if chat_type == "p2p" and msg_type == "text":
                try:
                    content_json = json.loads(content_str)
                    text = content_json.get("text", "").strip()
                    if not text:
                        return {"code": 0}

                    # 文字交互链路暂未开放：固定回复
                    # （MR 操作目前仅通过 MR 创建时推送的卡片按钮触发）
                    await FeishuClient().send_message(sender_id, "当前功能正在制作，请耐心等待")

                except Exception as e:
                    get_logger().error(f"Error handling Feishu message: {e}")

        return {"code": 0}

    except Exception as e:
        get_logger().error(f"Feishu webhook error: {e}")
        return {"code": 0}


@router.post("/webhook/feishu")
async def handle_feishu_webhook(request: Request):
    """
    Handle Feishu event callbacks (e.g. user messages)
    """
    # request.json() might have been consumed already if called from gitlab_webhook
    # But Starlette's Request.json() caches the result, so it's safe to call again.
    data = await request.json()
    response_payload = await handle_feishu_event_payload(data)
    return JSONResponse(content=response_payload)

async def run_pr_agent(url, command, sender_id):
    try:
        get_logger().info(f"Starting PR-Agent for {url} with command {command}, sender {sender_id}")

        # Define a function to generate our proxy provider
        def get_proxy_provider(pr_url=None):
            # Call original function to get the base provider (GitLab)
            from pr_agent.git_providers.gitlab_provider import GitLabProvider
            original = GitLabProvider(pr_url)
            return FeishuGitProvider(original, sender_id, mr_url=url)

        with git_provider_factory_context(get_proxy_provider):
            await PRAgent().handle_request(url, command)

    except Exception as e:
        get_logger().error(f"Error running PR-Agent from Feishu webhook: {e}")
