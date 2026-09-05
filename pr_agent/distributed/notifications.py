import asyncio
import hashlib
import json
from dataclasses import replace
from typing import Awaitable, Callable

from pr_agent.distributed.broker import RedisBroker, SyncRedisBroker
from pr_agent.distributed.config import DistributedSettings
from pr_agent.distributed.models import (
    TERMINAL_TRIAGE_CARD_STATES,
    NotificationEnvelope,
    PostRepairUTState,
    PostRepairUTStatus,
    RepairCategory,
    RepairItem,
    RepairItemStatus,
    TaskEnvelope,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.distributed.runtime import get_execution_runtime
from pr_agent.feishu.feishu_client import FeishuClient
from pr_agent.feishu.triage_card import render_triage_card, triage_card_predecessors
from pr_agent.log import get_logger
from pr_agent.triage.model_availability import MODEL_SERVICE_UNAVAILABLE_MESSAGE


def triage_card_updates_enabled() -> bool:
    from pr_agent.config_loader import get_settings

    value = get_settings().get("FEISHU.UPDATE_TRIAGE_CARDS", True)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def multi_action_repair_cards_enabled() -> bool:
    from pr_agent.triage.repair_card_mode import RepairCardMode, repair_card_mode

    return repair_card_mode() in {RepairCardMode.MULTI_SELECT, RepairCardMode.LEGACY_ACTIONS}


def unified_pipeline_repair_enabled() -> bool:
    from pr_agent.triage.repair_card_mode import RepairCardMode, repair_card_mode

    return repair_card_mode() is RepairCardMode.UNIFIED


def triage_card_ttl_seconds() -> int:
    from pr_agent.config_loader import get_settings

    value = int(get_settings().get("FEISHU.TRIAGE_CARD_TTL_SECONDS", 2_592_000))
    if value <= 0:
        raise ValueError("feishu.triage_card_ttl_seconds must be positive")
    return value


async def queue_triage_card_update(
    broker: RedisBroker,
    task_id: str,
    state: TriageCardState,
    status_markdown: str,
) -> bool:
    if not triage_card_updates_enabled():
        return False
    binding = await broker.get_task_triage_card(task_id)
    if binding is None or not binding.open_message_id or not binding.receive_id:
        return False
    predicted = _binding_with_active_item_state(binding, task_id, state, status_markdown)
    notification = build_card_update_notification(predicted, task_id, state, status_markdown)
    return await broker.transition_triage_card_with_notification(
        task_id,
        triage_card_predecessors(state),
        state,
        status_markdown,
        notification,
    )


async def queue_pipeline_repair_progress(
    broker: RedisBroker,
    task_id: str,
    state: TriageCardState,
    status_markdown: str,
    current_pipeline_id: int,
    current_pipeline_sha: str,
    repair_items: tuple[RepairItem, ...] | None = None,
) -> bool:
    if not triage_card_updates_enabled():
        return False
    binding = await broker.get_task_triage_card(task_id)
    if binding is None or not binding.open_message_id or not binding.receive_id:
        return False
    if repair_items is not None:
        binding = replace(binding, repair_items=repair_items)
    predicted = replace(
        _binding_with_active_item_state(binding, task_id, state, status_markdown),
        current_pipeline_id=current_pipeline_id,
        current_pipeline_sha=current_pipeline_sha,
    )
    notification = build_card_update_notification(predicted, task_id, state, status_markdown)
    return await broker.update_repair_progress_with_notification(
        task_id,
        triage_card_predecessors(state) | {state},
        state,
        status_markdown,
        current_pipeline_id,
        current_pipeline_sha,
        notification,
        predicted.repair_items,
    )


async def queue_post_repair_ut_progress(
    broker: RedisBroker,
    task_id: str,
    state: PostRepairUTState,
    *,
    terminal: bool = False,
) -> bool:
    """Update only the UT section while preserving the successful repair card."""
    if not triage_card_updates_enabled():
        return False
    binding = await broker.get_task_triage_card(task_id)
    if binding is None or not binding.open_message_id or not binding.receive_id:
        return False
    predicted = replace(
        binding,
        post_repair_ut=state,
        active_task_id="" if terminal else task_id,
        active_category="" if terminal else "unit_test",
    )
    notification = build_card_update_notification(
        predicted,
        task_id,
        TriageCardState.REPAIR_SUCCEEDED,
        binding.status_markdown,
    )
    return await broker.update_post_repair_ut_with_notification(
        task_id,
        state,
        notification,
        terminal=terminal,
    )


def _binding_with_active_item_state(
    binding: TriageCardBinding,
    task_id: str,
    state: TriageCardState,
    status_markdown: str,
) -> TriageCardBinding:
    from dataclasses import replace

    from pr_agent.distributed.models import RepairItemStatus

    statuses = {
        TriageCardState.REPAIR_QUEUED: RepairItemStatus.QUEUED,
        TriageCardState.REPAIR_RUNNING: RepairItemStatus.RUNNING,
        TriageCardState.WAITING_PIPELINE: RepairItemStatus.WAITING_PIPELINE,
    }
    item_status = statuses.get(state)
    if item_status is None or not binding.repair_items:
        return binding
    items = tuple(
        replace(item, status=item_status, status_markdown=status_markdown)
        if item.task_id == task_id
        else item
        for item in binding.repair_items
    )
    return replace(binding, repair_items=items, state=state, status_markdown=status_markdown)


async def queue_repair_reconciliation(
    broker: RedisBroker,
    task_id: str,
    repair_items: tuple[RepairItem, ...],
    state: TriageCardState,
    status_markdown: str,
    current_pipeline_id: int,
    current_pipeline_sha: str,
    *,
    post_repair_ut_coverage: float | None = None,
    post_repair_ut_coverage_status: str = "",
) -> bool:
    binding = await broker.get_task_triage_card(task_id)
    if binding is None or not binding.open_message_id or not binding.receive_id:
        return False
    post_repair_ut = binding.post_repair_ut
    if state is TriageCardState.REPAIR_SUCCEEDED:
        post_repair_ut = replace(
            post_repair_ut,
            coverage_before=post_repair_ut_coverage,
            coverage_status_before=post_repair_ut_coverage_status,
            baseline_pipeline_id=current_pipeline_id,
            baseline_sha=current_pipeline_sha,
            current_pipeline_id=current_pipeline_id,
            current_sha=current_pipeline_sha,
        )
    predicted = replace(
        binding,
        repair_items=repair_items,
        state=state,
        status_markdown=status_markdown,
        active_task_id="",
        active_category="",
        current_pipeline_id=current_pipeline_id,
        current_pipeline_sha=current_pipeline_sha,
        post_repair_ut=post_repair_ut,
        revision=binding.revision + 1,
    )
    notification = build_card_update_notification(predicted, task_id, state, status_markdown)
    return await broker.reconcile_repair_card_with_notification(
        task_id,
        binding.revision,
        repair_items,
        state,
        status_markdown,
        current_pipeline_id,
        current_pipeline_sha,
        predicted.revision,
        notification,
        predicted.post_repair_ut if state is TriageCardState.REPAIR_SUCCEEDED else None,
    )


def _build_triage_result_envelope(
    task_id: str,
    state: TriageCardState,
    *,
    receive_id: str,
    content: str,
    title: str,
    header_template: str,
    mr_url: str,
) -> NotificationEnvelope:
    if state not in TERMINAL_TRIAGE_CARD_STATES:
        raise ValueError("triage result notifications require a terminal card state")
    notification_id = hashlib.sha256(f"{task_id}\x1fterminal-result".encode("utf-8")).hexdigest()[:32]
    return NotificationEnvelope.new(
        task_id=task_id,
        receive_id=receive_id,
        recipient_email="",
        recipient_username="",
        kind="markdown",
        content=content,
        title=title,
        header_template=header_template,
        mr_url=mr_url,
        notification_id=notification_id,
    )


def build_triage_result_notification(
    binding: TriageCardBinding,
    task_id: str,
    state: TriageCardState,
    status_markdown: str,
) -> NotificationEnvelope:
    card = render_triage_card(binding, state, status_markdown)
    return _build_triage_result_envelope(
        task_id,
        state,
        receive_id=binding.receive_id,
        content=card["elements"][0]["content"],
        title=card["header"]["title"]["content"],
        header_template=card["header"]["template"],
        mr_url=binding.mr_url,
    )


def build_triage_terminal_reminder(
    binding: TriageCardBinding,
    task_id: str,
    state: TriageCardState,
    status_markdown: str,
) -> NotificationEnvelope:
    if state not in TERMINAL_TRIAGE_CARD_STATES:
        raise ValueError("triage terminal reminders require a terminal card state")
    icon, result, detail, header_template = {
        TriageCardState.REPAIR_SUCCEEDED: ("✅", "修复成功", "完整结果", "green"),
        TriageCardState.REPAIR_PARTIAL: ("⚠️", "部分修复成功", "完整结果", "orange"),
        TriageCardState.REPAIR_BLOCKED: (
            "⛔",
            "自动修复被外部依赖阻塞",
            "阻塞原因和人工处理建议",
            "orange",
        ),
        TriageCardState.REPAIR_MODEL_UNAVAILABLE: (
            "⚠️",
            MODEL_SERVICE_UNAVAILABLE_MESSAGE,
            "",
            "orange",
        ),
        TriageCardState.REPAIR_FAILED: ("❌", "修复失败", "失败原因和人工处理建议", "red"),
        TriageCardState.CANCELED: ("⏹️", "修复已取消", "取消状态", "grey"),
    }[state]
    lines = [f"{icon}【{binding.project_id} !{binding.mr_iid}】{result}"]
    lines.append(f"原流水线卡片已更新{detail}。")
    lines.extend(_repair_attempt_identity_lines(binding, task_id))
    target = next((item for item in binding.repair_items if item.task_id == task_id), None)
    if target is not None and target.result_pipeline_sha:
        lines.append(f"Commit: {target.result_pipeline_sha[:12]}")
    if target is not None and target.result_pipeline_id:
        lines.append(f"结果 Pipeline: #{target.result_pipeline_id}")
    lines.append(f"MR: {binding.mr_url}")
    notification_id = hashlib.sha256(
        f"{task_id}\x1fterminal-reminder".encode("utf-8")
    ).hexdigest()[:32]
    return NotificationEnvelope.new(
        task_id=task_id,
        receive_id=binding.receive_id,
        recipient_email="",
        recipient_username="",
        kind="text",
        content="\n".join(lines),
        title="PR-Agent",
        header_template=header_template,
        mr_url=binding.mr_url,
        notification_id=notification_id,
    )


def build_repair_progress_reminder(
    binding: TriageCardBinding,
    task_id: str,
) -> NotificationEnvelope:
    targets = [item for item in binding.repair_items if item.task_id == task_id]
    target = targets[0] if targets else None
    pending = [
        item.display_name
        for item in binding.repair_items
        if item.status in {RepairItemStatus.PENDING, RepairItemStatus.FAILED}
    ]
    target_name = target.display_name if target is not None else "当前项目"
    if len(targets) > 1:
        summary = (
            "所选问题自动修复未完成"
            if any(item.status is RepairItemStatus.FAILED for item in targets)
            else "所选问题已处理完成"
        )
    elif target is not None and target.status is RepairItemStatus.SUCCEEDED:
        summary = f"{target_name} 修复完成"
    elif len(targets) <= 1:
        summary = f"{target_name} 自动修复未完成"
    remaining = "、".join(pending) if pending else "其他检查"
    lines = [f"⚠️【{binding.project_id} !{binding.mr_iid}】{summary}，流水线仍有 {remaining} 失败。"]
    lines.extend(_repair_attempt_identity_lines(binding, task_id))
    lines.extend(["请在原卡继续处理。", f"MR: {binding.mr_url}"])
    content = "\n".join(lines)
    notification_id = hashlib.sha256(
        f"{task_id}\x1frepair-progress\x1f{binding.revision}".encode("utf-8")
    ).hexdigest()[:32]
    return NotificationEnvelope.new(
        task_id=task_id,
        receive_id=binding.receive_id,
        recipient_email="",
        recipient_username="",
        kind="text",
        content=content,
        title="PR-Agent",
        header_template="orange",
        mr_url=binding.mr_url,
        notification_id=notification_id,
    )


def build_post_repair_ut_terminal_reminder(
    binding: TriageCardBinding,
    task_id: str,
) -> NotificationEnvelope:
    state = binding.post_repair_ut
    icon, title, color = {
        PostRepairUTStatus.SUCCEEDED: ("✅", "单元测试补充成功", "green"),
        PostRepairUTStatus.PARTIAL: ("⚠️", "单元测试已补充，覆盖率仍未达标", "orange"),
        PostRepairUTStatus.UNVERIFIED: ("⚠️", "单元测试已补充，覆盖率未确认", "orange"),
        PostRepairUTStatus.CANCELED: ("⏹️", "单元测试补充已取消", "grey"),
        PostRepairUTStatus.ROLLBACK_FAILED: ("❌", "单元测试补充失败，自动撤回未完成", "red"),
        PostRepairUTStatus.FAILED: ("❌", "单元测试补充失败", "red"),
    }[state.status]
    lines = [f"{icon}【{binding.project_id} !{binding.mr_iid}】{title}"]
    if state.outcome_reason:
        lines.append(state.outcome_reason)
    if state.current_pipeline_id:
        lines.append(f"验证 Pipeline: #{state.current_pipeline_id}")
    if state.coverage_after is not None:
        lines.append(f"单元测试覆盖率: {state.coverage_after:.2f}%")
    if state.rollback_commit_sha:
        lines.append(f"撤回 Commit: {state.rollback_commit_sha[:12]}")
    lines.append(f"MR: {binding.mr_url}")
    notification_id = hashlib.sha256(f"post-repair-ut:{task_id}:terminal".encode("utf-8")).hexdigest()[:32]
    return NotificationEnvelope.new(
        task_id=task_id,
        receive_id=binding.receive_id,
        recipient_email="",
        recipient_username="",
        kind="text",
        content="\n".join(lines),
        title="PR-Agent",
        header_template=color,
        mr_url=binding.mr_url,
        notification_id=notification_id,
    )


def _repair_attempt_identity_lines(binding: TriageCardBinding, task_id: str) -> list[str]:
    """Render the stable identity shared by compact repair reminders."""
    lines = [f"任务: {task_id[:12]}"]
    if binding.pipeline_id:
        lines.append(f"原始 Pipeline: #{binding.pipeline_id}")
    return lines


def build_repair_rollback_reminder(
    binding: TriageCardBinding,
    rollback_task_id: str,
    rollback_commit_sha: str,
) -> NotificationEnvelope:
    count = binding.rollback_commit_count
    content = (
        f"✅【{binding.project_id} !{binding.mr_iid}】撤回成功\n"
        f"本次自动修复产生的 {count} 个提交已完整撤回。\n"
        f"撤回 Commit: {rollback_commit_sha[:12]}\n"
        f"MR: {binding.mr_url}"
    )
    notification_id = hashlib.sha256(
        f"{rollback_task_id}\x1frollback-success".encode("utf-8")
    ).hexdigest()[:32]
    return NotificationEnvelope.new(
        task_id=rollback_task_id,
        receive_id=binding.receive_id,
        recipient_email="",
        recipient_username="",
        kind="text",
        content=content,
        title="PR-Agent",
        header_template="green",
        mr_url=binding.mr_url,
        notification_id=notification_id,
    )


def build_auto_failure_rollback_reminder(
    binding: TriageCardBinding,
    rollback_task_id: str,
    *,
    succeeded: bool,
    rollback_commit_sha: str,
    failure_message: str,
) -> NotificationEnvelope:
    if succeeded:
        lines = [
            f"❌【{binding.project_id} !{binding.mr_iid}】修复失败，本次自动修改已撤回",
            f"本次修复未解决任何已选问题，产生的 {binding.rollback_commit_count} 个提交已完整撤回。",
            f"撤回 Commit: {rollback_commit_sha[:12]}",
        ]
    else:
        lines = [
            f"❌【{binding.project_id} !{binding.mr_iid}】修复失败，自动撤回未完成",
            f"原因: {failure_message or '无法确认安全撤回条件'}",
        ]
    lines.append(f"MR: {binding.mr_url}")
    notification_id = hashlib.sha256(
        f"{rollback_task_id}\x1fauto-failure-final".encode("utf-8")
    ).hexdigest()[:32]
    return NotificationEnvelope.new(
        task_id=rollback_task_id,
        receive_id=binding.receive_id,
        recipient_email="",
        recipient_username="",
        kind="text",
        content="\n".join(lines),
        title="PR-Agent",
        header_template="red",
        mr_url=binding.mr_url,
        notification_id=notification_id,
    )


async def queue_triage_terminal_notifications(
    broker: RedisBroker,
    task_id: str,
    state: TriageCardState,
    status_markdown: str,
) -> bool:
    binding = await broker.get_task_triage_card(task_id)
    if binding is None or not binding.receive_id:
        return False
    if binding.open_message_id:
        if await queue_triage_card_update(broker, task_id, state, status_markdown):
            return True
        latest = await broker.get_task_triage_card(task_id)
        if latest is not None and latest.state in TERMINAL_TRIAGE_CARD_STATES:
            return True
    return await broker.enqueue_notification(
        build_triage_result_notification(binding, task_id, state, status_markdown)
    )


async def queue_triage_failure_notification(broker: RedisBroker, task: TaskEnvelope, error: str) -> bool:
    """Publish one terminal Feishu failure even when the original card binding is missing."""
    repair_commands = {"/triage", "/fix-format", "/fix_format", "/repair-pipeline"}
    if task.source != "feishu" or not task.command or task.command.split()[0].lower() not in repair_commands:
        return False
    status_markdown = (
        f"自动修复任务异常终止。\n\n- 任务: `{task.task_id[:12]}`\n- Pipeline: `unknown`"
        f"\n- Coverage: 未提供\n- 原因: {error}"
    )
    binding = await broker.get_task_triage_card(task.task_id)
    if binding is not None and binding.repair_items and binding.active_task_id == task.task_id:
        items = tuple(
            replace(item, status=RepairItemStatus.FAILED, status_markdown=error)
            if item.task_id == task.task_id
            else item
            for item in binding.repair_items
        )
        return await queue_repair_reconciliation(
            broker,
            task.task_id,
            items,
            TriageCardState.PIPELINE_FAILED,
            status_markdown,
            binding.current_pipeline_id,
            binding.current_pipeline_sha,
        )
    if await queue_triage_terminal_notifications(
        broker,
        task.task_id,
        TriageCardState.REPAIR_FAILED,
        status_markdown,
    ):
        return True
    receive_id = str(task.payload.get("sender_id") or "").strip()
    if not receive_id:
        return False
    if task.mr is not None:
        identity = f"{task.mr.project_id} !{task.mr.iid}"
        title = f"【{identity}】修复失败"
    else:
        identity = "MR"
        title = "PR-Agent 修复失败"
    content = f"**MR:** [{identity}]({task.pr_url})\n\n{status_markdown}"
    return await broker.enqueue_notification(
        _build_triage_result_envelope(
            task.task_id,
            TriageCardState.REPAIR_FAILED,
            receive_id=receive_id,
            content=content,
            title=title,
            header_template="red",
            mr_url=task.pr_url,
        )
    )


async def queue_repair_canceled_notification(
    broker: RedisBroker,
    task: TaskEnvelope,
    status_markdown: str,
) -> bool:
    stored = await broker.get_task(task.task_id)
    if stored is None:
        return False
    lease = await broker.get_mr_lease(task.mr) if task.mr is not None and stored.worker_id else None
    if stored.worker_id and (
        lease is None
        or lease.worker_id != stored.worker_id
        or lease.fencing_token != stored.fencing_token
    ):
        return False
    return await broker.finalize_repair_cancel(task, lease, status_markdown)


def build_card_update_notification(
    binding: TriageCardBinding,
    task_id: str,
    state: TriageCardState,
    status_markdown: str,
) -> NotificationEnvelope:
    card = render_triage_card(binding, state, status_markdown, detail_task_id=task_id)
    content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    notification_id = hashlib.sha256(
        f"{task_id}\x1f{state.value}\x1f{content_hash}".encode("utf-8")
    ).hexdigest()[:32]
    original_and_status = card["elements"][0]["content"]
    return NotificationEnvelope.new(
        task_id=task_id,
        receive_id=binding.receive_id,
        recipient_email="",
        recipient_username="",
        kind="card_update",
        content=content,
        title=card["header"]["title"]["content"],
        header_template=card["header"]["template"],
        mr_url=binding.mr_url,
        notification_id=notification_id,
        card_id=binding.card_id,
        message_id=binding.open_message_id,
        fallback_content=f"{card['header']['title']['content']}\n\n{original_and_status}",
        card_state=state.value,
    )


class NotificationConsumer:
    def __init__(
        self,
        broker: RedisBroker,
        client: FeishuClient,
        settings: DistributedSettings,
        *,
        consumer_id: str = "feishu-1",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.broker = broker
        self.client = client
        self.settings = settings
        self.consumer_id = consumer_id
        self.sleep = sleep
        self.stop_event = asyncio.Event()

    @staticmethod
    def _record_pipeline_failure_delivery(
        notification: NotificationEnvelope,
        state: str,
        reason: str = "",
    ) -> None:
        prefix = "pipeline-failure-"
        if not notification.notification_id.startswith(prefix):
            return
        card_id = notification.notification_id[len(prefix):]
        if not card_id:
            return
        try:
            from pr_agent.triage.ci_failure_store import update_notification_state

            update_notification_state(card_id, state, reason)
        except Exception as error:
            get_logger().warning(
                f"Failed to persist Pipeline notification outcome: {type(error).__name__}"
            )

    async def _current_card_binding(
        self,
        notification: NotificationEnvelope,
    ) -> tuple[TriageCardBinding | None, bool]:
        if notification.kind != "card_update" or not notification.card_id:
            return None, False
        binding = await self.broker.get_triage_card(notification.card_id)
        if not isinstance(binding, TriageCardBinding):
            return None, False
        expected = build_card_update_notification(
            binding,
            notification.task_id,
            binding.state,
            binding.status_markdown,
        )
        stale = expected.content != notification.content
        if notification.card_state:
            stale = stale or expected.card_state != notification.card_state
        return binding, stale

    async def _complete_notification(self, notification: NotificationEnvelope, message_id: str) -> None:
        await self.broker.complete_notification(notification.notification_id, message_id)
        if notification.task_id:
            from pr_agent.distributed.lifecycle import LifecycleEvent

            await self.broker.record_lifecycle_event(
                LifecycleEvent.new(
                    notification.task_id,
                    "notification",
                    "end",
                    segment_id=notification.notification_id,
                )
            )

    async def _enqueue_terminal_fallback(
        self,
        notification: NotificationEnvelope,
        binding: TriageCardBinding | None,
    ) -> None:
        if binding is None or binding.state not in TERMINAL_TRIAGE_CARD_STATES:
            return
        fallback = build_triage_result_notification(
            binding,
            notification.task_id,
            binding.state,
            binding.status_markdown,
        )
        await self.broker.enqueue_card_fallback(notification.card_id, fallback)

    async def process(self, notification: NotificationEnvelope) -> None:
        receive_id = notification.receive_id
        if not receive_id:
            receive_id = await self.client.resolve_open_id_for_gitlab_user(
                notification.recipient_username,
                notification.recipient_email,
            )
        if not receive_id:
            await self.broker.dead_letter_notification(notification.notification_id, "recipient_not_found")
            self._record_pipeline_failure_delivery(notification, "recipient_missing", "recipient_not_found")
            return

        resolved = replace(notification, receive_id=receive_id)
        while True:
            binding, stale = await self._current_card_binding(notification)
            if stale:
                await self._complete_notification(notification, notification.message_id)
                return
            result = await self.client.send_notification(resolved)
            if result.ok:
                if notification.card_id and result.message_id:
                    try:
                        await self.broker.record_card_message(
                            notification.card_id,
                            result.message_id,
                            receive_id,
                        )
                    except Exception:
                        get_logger().exception(
                            f"Failed to register delivered Feishu triage card card_id={notification.card_id} "
                            f"message_id={result.message_id}"
                        )
                if binding is not None and binding.state in TERMINAL_TRIAGE_CARD_STATES:
                    await self.broker.enqueue_notification(
                        build_triage_terminal_reminder(
                            binding,
                            notification.task_id,
                            binding.state,
                            binding.status_markdown,
                        )
                    )
                elif (
                    binding is not None
                    and binding.state is TriageCardState.PIPELINE_FAILED
                    and binding.repair_items
                    and not binding.active_task_id
                ):
                    reminder = build_repair_progress_reminder(binding, notification.task_id)
                    await self.broker.enqueue_notification(reminder)
                await self._complete_notification(notification, result.message_id or "")
                self._record_pipeline_failure_delivery(notification, "delivered")
                return
            attempt = await self.broker.fail_notification_attempt(notification.notification_id, result.error)
            if not result.retryable or attempt >= self.settings.notification_retry_limit:
                await self._enqueue_terminal_fallback(notification, binding)
                await self.broker.dead_letter_notification(notification.notification_id, result.error)
                self._record_pipeline_failure_delivery(notification, "failed", "delivery_failed")
                return
            await self.sleep(min(60, 2**attempt))

    async def run(self) -> None:
        while not self.stop_event.is_set():
            delivery = await self.broker.read_notification(self.consumer_id, block_ms=1000)
            if delivery is None:
                continue
            message_id, notification = delivery
            try:
                await self.process(notification)
            except Exception:
                get_logger().exception(
                    f"Feishu notification processing failed without ack notification_id={notification.notification_id}"
                )
                continue
            await self.broker.ack_notification(message_id)


class QueuedNotificationSink:
    def __init__(self, broker: SyncRedisBroker, task_id: str | None = None):
        self.broker = broker
        runtime = get_execution_runtime()
        self.task_id = task_id or (runtime.task_id if runtime else "")

    def _notification_id(self, *parts: str) -> str:
        value = "\x1f".join((self.task_id, *parts))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    def publish_markdown(
        self,
        *,
        receive_id: str,
        content: str,
        title: str,
        header_template: str,
        mr_url: str,
    ) -> bool:
        return self.broker.enqueue_notification(
            NotificationEnvelope.new(
                task_id=self.task_id,
                receive_id=receive_id,
                recipient_email="",
                recipient_username="",
                kind="markdown",
                content=content,
                title=title,
                header_template=header_template,
                mr_url=mr_url,
                notification_id=self._notification_id("markdown", receive_id, title, content, mr_url),
            )
        )

    def publish_text(self, *, receive_id: str, content: str, mr_url: str = "") -> bool:
        return self.broker.enqueue_notification(
            NotificationEnvelope.new(
                task_id=self.task_id,
                receive_id=receive_id,
                recipient_email="",
                recipient_username="",
                kind="text",
                content=content,
                title="PR-Agent",
                header_template="blue",
                mr_url=mr_url,
                notification_id=self._notification_id("text", receive_id, content, mr_url),
            )
        )

    def publish_card_update(self, *, state: TriageCardState, status_markdown: str) -> bool:
        if not self.task_id or not triage_card_updates_enabled():
            return False
        binding = self.broker.get_task_triage_card(self.task_id)
        if binding is None or not binding.open_message_id or not binding.receive_id:
            return False
        terminal_states = TERMINAL_TRIAGE_CARD_STATES
        if binding.state in terminal_states:
            return True
        predicted = _binding_with_active_item_state(binding, self.task_id, state, status_markdown)
        notification = build_card_update_notification(predicted, self.task_id, state, status_markdown)
        changed = self.broker.transition_triage_card_with_notification(
            self.task_id,
            triage_card_predecessors(state),
            state,
            status_markdown,
            notification,
        )
        if changed:
            return True
        latest = self.broker.get_task_triage_card(self.task_id)
        return latest is not None and latest.state in terminal_states

    def publish_triage_result(
        self,
        *,
        state: TriageCardState,
        status_markdown: str,
        receive_id: str = "",
        content: str = "",
        title: str = "",
        header_template: str = "",
        mr_url: str = "",
        details: dict | None = None,
    ) -> bool:
        if not self.task_id:
            return False
        binding = self.broker.get_task_triage_card(self.task_id)
        if binding is not None and binding.receive_id:
            if binding.repair_items and binding.active_task_id == self.task_id:
                return self._publish_multi_action_result(binding, state, status_markdown, details or {})
            if binding.open_message_id:
                if self.publish_card_update(state=state, status_markdown=status_markdown):
                    return True
                latest = self.broker.get_task_triage_card(self.task_id)
                if latest is not None and latest.state in TERMINAL_TRIAGE_CARD_STATES:
                    return True
            notification = build_triage_result_notification(binding, self.task_id, state, status_markdown)
        else:
            if not all((receive_id, content, title, header_template, mr_url)):
                return False
            notification = _build_triage_result_envelope(
                self.task_id,
                state,
                receive_id=receive_id,
                content=content,
                title=title,
                header_template=header_template,
                mr_url=mr_url,
            )
        self.broker.enqueue_notification(notification)
        return True

    def _publish_multi_action_result(
        self,
        binding: TriageCardBinding,
        state: TriageCardState,
        status_markdown: str,
        details: dict,
    ) -> bool:
        from pr_agent.triage.failure_categories import classify_failed_jobs, reconcile_repair_items

        target = RepairCategory(binding.active_category)
        pipeline_groups = details.get("pipeline_groups") or []
        last_group = pipeline_groups[-1] if pipeline_groups and isinstance(pipeline_groups[-1], dict) else {}
        failed_jobs = last_group.get("failed_jobs") or []
        normalized_jobs = [job if isinstance(job, dict) else {"name": str(job)} for job in failed_jobs]
        failed_categories = classify_failed_jobs(normalized_jobs)
        final_pipeline_status = str(details.get("final_pipeline_status") or "").lower()
        terminal_failed = state is TriageCardState.REPAIR_FAILED or final_pipeline_status not in {"", "success"}
        if terminal_failed and not failed_categories:
            failed_categories = [target]
        pipeline_id = int(
            last_group.get("validation_pipeline_id")
            or last_group.get("root_pipeline_id")
            or binding.current_pipeline_id
        )
        push_attempts = details.get("push_attempts") or []
        latest_attempt = push_attempts[-1] if push_attempts and isinstance(push_attempts[-1], dict) else {}
        pipeline_sha = str(
            latest_attempt.get("commit_sha")
            or details.get("pushed_sha")
            or binding.current_pipeline_sha
        )
        items = reconcile_repair_items(
            binding.repair_items,
            target,
            failed_categories,
            pipeline_id,
            pipeline_sha,
            str(details.get("error") or ""),
        )
        if state is TriageCardState.REPAIR_BLOCKED:
            blocker_summary = str(details.get("blocker_summary") or status_markdown).strip()
            items = tuple(
                replace(item, status=RepairItemStatus.BLOCKED, status_markdown=blocker_summary)
                if item.category is target
                else item
                for item in items
            )
            card_state = TriageCardState.REPAIR_BLOCKED
        else:
            card_state = TriageCardState.REPAIR_SUCCEEDED if not failed_categories else TriageCardState.PIPELINE_FAILED
        predicted = replace(
            binding,
            repair_items=items,
            state=card_state,
            status_markdown=status_markdown,
            active_task_id="",
            active_category="",
            revision=binding.revision + 1,
            current_pipeline_id=pipeline_id,
            current_pipeline_sha=pipeline_sha,
        )
        notification = build_card_update_notification(predicted, self.task_id, card_state, status_markdown)
        return self.broker.reconcile_repair_card_with_notification(
            self.task_id,
            binding.revision,
            items,
            card_state,
            status_markdown,
            pipeline_id,
            pipeline_sha,
            predicted.revision,
            notification,
        )


class DirectFeishuNotificationSink:
    def __init__(self, client: FeishuClient | None = None):
        self.client = client or FeishuClient()

    def publish_markdown(
        self,
        *,
        receive_id: str,
        content: str,
        title: str,
        header_template: str,
        mr_url: str,
    ) -> None:
        self._submit(
            self.client.send_markdown(
                receive_id,
                content,
                title=title,
                header_template=header_template,
                show_rating=False,
                mr_url=mr_url,
            )
        )

    def publish_text(self, *, receive_id: str, content: str, mr_url: str = "") -> None:
        self._submit(self.client.send_message(receive_id, content))

    @staticmethod
    def _submit(coroutine) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coroutine)
        else:
            loop.create_task(coroutine)


def action_card_content(markdown: str, actions: list[dict]) -> str:
    return json.dumps({"markdown": markdown, "actions": actions}, ensure_ascii=False, separators=(",", ":"))
