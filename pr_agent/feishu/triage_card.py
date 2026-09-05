import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from pr_agent.distributed.models import (
    PostRepairUTStatus,
    RepairCategory,
    RepairItemStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.triage.post_repair_ut import is_post_repair_ut_eligible
from pr_agent.triage.repair_card_mode import RepairCardMode
from pr_agent.triage.repair_details import build_repair_details_url

_STATE_TITLES = {
    TriageCardState.PIPELINE_FAILED: "流水线失败",
    TriageCardState.REPAIR_QUEUED: "已进入修复队列",
    TriageCardState.REPAIR_RUNNING: "正在修复",
    TriageCardState.WAITING_PIPELINE: "等待流水线",
    TriageCardState.REPAIR_SUCCEEDED: "修复成功",
    TriageCardState.REPAIR_PARTIAL: "部分修复成功",
    TriageCardState.REPAIR_BLOCKED: "外部依赖阻塞",
    TriageCardState.REPAIR_MODEL_UNAVAILABLE: "模型服务不可用",
    TriageCardState.REPAIR_FAILED: "修复失败",
    TriageCardState.CANCELING: "正在取消修复",
    TriageCardState.CANCELED: "修复已取消",
    TriageCardState.ROLLBACK_QUEUED: "已进入撤回队列",
    TriageCardState.ROLLBACK_RUNNING: "正在撤回修复",
    TriageCardState.ROLLBACK_SUCCEEDED: "撤回成功",
    TriageCardState.ROLLBACK_FAILED: "撤回失败",
}

_STATE_TEMPLATES = {
    TriageCardState.PIPELINE_FAILED: "blue",
    TriageCardState.REPAIR_QUEUED: "blue",
    TriageCardState.REPAIR_RUNNING: "wathet",
    TriageCardState.WAITING_PIPELINE: "orange",
    TriageCardState.REPAIR_SUCCEEDED: "green",
    TriageCardState.REPAIR_PARTIAL: "orange",
    TriageCardState.REPAIR_BLOCKED: "orange",
    TriageCardState.REPAIR_MODEL_UNAVAILABLE: "orange",
    TriageCardState.REPAIR_FAILED: "red",
    TriageCardState.CANCELING: "orange",
    TriageCardState.CANCELED: "grey",
    TriageCardState.ROLLBACK_QUEUED: "orange",
    TriageCardState.ROLLBACK_RUNNING: "orange",
    TriageCardState.ROLLBACK_SUCCEEDED: "green",
    TriageCardState.ROLLBACK_FAILED: "red",
}

_FAILURE_EXPLANATION_LIMIT = 3
_FAILURE_EXPLANATION_TEXT_LIMIT = 4000

_ALLOWED_TRANSITIONS = {
    TriageCardState.PIPELINE_FAILED: {TriageCardState.REPAIR_QUEUED},
    TriageCardState.REPAIR_QUEUED: {
        TriageCardState.REPAIR_RUNNING,
        TriageCardState.REPAIR_BLOCKED,
        TriageCardState.REPAIR_MODEL_UNAVAILABLE,
        TriageCardState.REPAIR_FAILED,
        TriageCardState.CANCELING,
        TriageCardState.CANCELED,
    },
    TriageCardState.REPAIR_RUNNING: {
        TriageCardState.WAITING_PIPELINE,
        TriageCardState.REPAIR_SUCCEEDED,
        TriageCardState.REPAIR_PARTIAL,
        TriageCardState.REPAIR_BLOCKED,
        TriageCardState.REPAIR_MODEL_UNAVAILABLE,
        TriageCardState.REPAIR_FAILED,
        TriageCardState.CANCELING,
        TriageCardState.CANCELED,
    },
    TriageCardState.WAITING_PIPELINE: {
        TriageCardState.REPAIR_RUNNING,
        TriageCardState.REPAIR_SUCCEEDED,
        TriageCardState.REPAIR_PARTIAL,
        TriageCardState.REPAIR_BLOCKED,
        TriageCardState.REPAIR_MODEL_UNAVAILABLE,
        TriageCardState.REPAIR_FAILED,
        TriageCardState.CANCELING,
        TriageCardState.CANCELED,
    },
    TriageCardState.CANCELING: {TriageCardState.CANCELED, TriageCardState.ROLLBACK_QUEUED},
    TriageCardState.CANCELED: {TriageCardState.REPAIR_QUEUED},
    TriageCardState.REPAIR_SUCCEEDED: {TriageCardState.ROLLBACK_QUEUED},
    TriageCardState.REPAIR_PARTIAL: {TriageCardState.ROLLBACK_QUEUED},
    TriageCardState.REPAIR_BLOCKED: set(),
    TriageCardState.REPAIR_MODEL_UNAVAILABLE: {TriageCardState.REPAIR_QUEUED},
    TriageCardState.REPAIR_FAILED: {TriageCardState.ROLLBACK_QUEUED},
    TriageCardState.ROLLBACK_QUEUED: {TriageCardState.ROLLBACK_RUNNING, TriageCardState.ROLLBACK_FAILED},
    TriageCardState.ROLLBACK_RUNNING: {
        TriageCardState.ROLLBACK_SUCCEEDED,
        TriageCardState.ROLLBACK_FAILED,
    },
    TriageCardState.ROLLBACK_SUCCEEDED: set(),
    TriageCardState.ROLLBACK_FAILED: set(),
}


@dataclass(frozen=True)
class MrIdentity:
    project_id: str
    mr_iid: int
    mr_url: str


def parse_mr_identity(mr_url: str) -> MrIdentity:
    parsed = urlparse(mr_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("not an absolute merge request URL")
    parts = [part for part in parsed.path.split("/") if part]
    separator = next(
        (
            index
            for index in range(len(parts) - 2)
            if parts[index] == "-" and parts[index + 1] == "merge_requests"
        ),
        -1,
    )
    if separator <= 0 or separator + 2 >= len(parts):
        raise ValueError("not a merge request URL")
    try:
        mr_iid = int(parts[separator + 2])
    except ValueError as error:
        raise ValueError("merge request IID must be an integer") from error
    if mr_iid <= 0 or separator + 3 != len(parts):
        raise ValueError("not a merge request URL")
    normalized_path = "/" + "/".join(parts)
    normalized_url = urlunparse((parsed.scheme, parsed.netloc, normalized_path, "", "", ""))
    return MrIdentity("/".join(parts[:separator]), mr_iid, normalized_url)


def build_card_id(project_id: str, mr_iid: int, pipeline_id: int) -> str:
    value = f"{project_id}\0{mr_iid}\0{pipeline_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:32]


def can_transition_triage_card(current: TriageCardState, target: TriageCardState) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]


def triage_card_predecessors(target: TriageCardState) -> set[TriageCardState]:
    return {current for current, targets in _ALLOWED_TRANSITIONS.items() if target in targets}


def triage_card_title(binding: TriageCardBinding, state: TriageCardState) -> str:
    title = _STATE_TITLES[state]
    if binding.rollback_trigger == "auto_failure":
        title = {
            TriageCardState.ROLLBACK_QUEUED: "正在撤回未生效的自动修改",
            TriageCardState.ROLLBACK_RUNNING: "正在撤回未生效的自动修改",
            TriageCardState.ROLLBACK_SUCCEEDED: "修复失败，修改已撤回",
            TriageCardState.ROLLBACK_FAILED: "修复失败，自动撤回未完成",
        }.get(state, title)
    return f"【{binding.project_id} !{binding.mr_iid}】{title}"


def triage_card_template(binding: TriageCardBinding, state: TriageCardState) -> str:
    if binding.rollback_trigger == "auto_failure" and state in {
        TriageCardState.ROLLBACK_SUCCEEDED,
        TriageCardState.ROLLBACK_FAILED,
    }:
        return "red"
    return _STATE_TEMPLATES[state]


def triage_failure_explanations_enabled() -> bool:
    from pr_agent.config_loader import get_settings

    value = get_settings().get("FEISHU.TRIAGE_FAILURE_EXPLANATIONS", True)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _failure_explanation_lines(binding: TriageCardBinding) -> list[str]:
    if not triage_failure_explanations_enabled():
        return []
    output: list[str] = []
    used = 0
    for item in binding.repair_items:
        if item.status is not RepairItemStatus.FAILED:
            continue
        for record in item.failure_explanations[:_FAILURE_EXPLANATION_LIMIT]:
            title = f"- `{record.job_name}`"
            if record.job_url:
                title += f"（[查看 Job 日志]({record.job_url})）"
            lines = [title]
            if record.possible_reason:
                lines.append(f"  - **原因分析**：{record.possible_reason}")
            elif record.confirmed_reason:
                lines.append(f"  - **已确认原因**：{record.confirmed_reason}")
            if record.suggested_action:
                lines.append(f"  - **建议处理**：{record.suggested_action}")
            if len(lines) == 1:
                lines.append("  - 暂未提取到具体根因，请查看 Job 日志")
            block_size = sum(len(line) + 1 for line in lines)
            if used + block_size > _FAILURE_EXPLANATION_TEXT_LIMIT:
                return output
            output.extend(lines)
            used += block_size
    return ["", "---", "**失败说明**", *output] if output else []


def _binding_repair_card_mode(binding: TriageCardBinding) -> RepairCardMode:
    if binding.repair_card_mode:
        return RepairCardMode(binding.repair_card_mode)
    if any(item.category is RepairCategory.PIPELINE for item in binding.repair_items):
        return RepairCardMode.UNIFIED
    return RepairCardMode.LEGACY_ACTIONS


def render_repair_selection_form(binding: TriageCardBinding) -> dict:
    current_pipeline_id = binding.current_pipeline_id or binding.pipeline_id
    current_pipeline_sha = binding.current_pipeline_sha or binding.pipeline_sha
    options = [
        {
            "text": {"tag": "plain_text", "content": item.display_name},
            "value": item.category.value,
            "selected": False,
        }
        for item in binding.repair_items
        if item.status in {RepairItemStatus.PENDING, RepairItemStatus.FAILED}
    ]
    return {
        "tag": "form",
        "name": "pipeline_repair_selection",
        "elements": [
            {
                "tag": "multi_select_static",
                "name": "selected_categories",
                "required": True,
                "placeholder": {"tag": "plain_text", "content": "请选择修复类别"},
                "options": options,
            },
            {
                "tag": "button",
                "name": "submit_pipeline_repair",
                "text": {"tag": "plain_text", "content": "修复所选问题"},
                "type": "primary",
                "action_type": "form_submit",
                "value": {
                    "command": "repair-pipeline",
                    "repair_card_mode": RepairCardMode.MULTI_SELECT.value,
                    "mr_url": binding.mr_url,
                    "card_id": binding.card_id,
                    "pipeline_id": current_pipeline_id,
                    "pipeline_sha": current_pipeline_sha,
                    "revision": binding.revision,
                },
            },
        ],
    }


def _append_repair_details_action(elements: list[dict], task_id: str) -> None:
    url = build_repair_details_url(task_id)
    if not url:
        return
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看修复详情"},
            "type": "default",
            "url": url,
        }],
    })


def _append_repair_rollback_action(
    elements: list[dict],
    binding: TriageCardBinding,
    state: TriageCardState,
) -> None:
    if (
        state not in {
            TriageCardState.REPAIR_SUCCEEDED,
            TriageCardState.REPAIR_PARTIAL,
            TriageCardState.REPAIR_FAILED,
        }
        or not binding.rollback_repair_task_id
        or binding.rollback_commit_count <= 0
        or binding.rollback_status in {"queued", "validating", "reverting", "committing", "pushing", "succeeded"}
    ):
        return
    from pr_agent.triage.repair_rollback import repair_rollback_enabled

    if not repair_rollback_enabled():
        return
    count = binding.rollback_commit_count
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "撤回修复"},
            "type": "danger",
            "confirm": {
                "title": {"tag": "plain_text", "content": "确认撤回本次自动修复？"},
                "text": {
                    "tag": "plain_text",
                    "content": f"将完整撤回本次自动修复产生的 {count} 个提交，并新建一个撤回提交。",
                },
            },
            "value": {
                "command": "rollback-repair",
                "repair_task_id": binding.rollback_repair_task_id,
                "mr_url": binding.mr_url,
                "card_id": binding.card_id,
                "revision": binding.revision,
            },
        }],
    })


def _append_post_repair_ut_action(
    elements: list[dict],
    binding: TriageCardBinding,
    state: TriageCardState,
) -> None:
    ut_state = binding.post_repair_ut
    if state is TriageCardState.REPAIR_SUCCEEDED and is_post_repair_ut_eligible(binding):
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "补充单元测试"},
                "type": "primary",
                "value": {
                    "command": "supplement-unit-tests",
                    "repair_task_id": binding.task_id,
                    "mr_url": binding.mr_url,
                    "card_id": binding.card_id,
                    "pipeline_id": binding.current_pipeline_id,
                    "pipeline_sha": binding.current_pipeline_sha,
                    "revision": binding.revision,
                },
            }],
        })
        return
    if (
        state is TriageCardState.REPAIR_SUCCEEDED
        and ut_state.task_id
        and ut_state.status in {
            PostRepairUTStatus.QUEUED,
            PostRepairUTStatus.RUNNING,
            PostRepairUTStatus.WAITING_PIPELINE,
        }
    ):
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消补测"},
                "type": "default",
                "value": {
                    "command": "cancel-unit-tests",
                    "task_id": ut_state.task_id,
                    "mr_url": binding.mr_url,
                    "card_id": binding.card_id,
                    "pipeline_id": ut_state.current_pipeline_id or binding.current_pipeline_id,
                    "pipeline_sha": ut_state.current_sha or binding.current_pipeline_sha,
                    "revision": binding.revision,
                },
            }],
        })


def render_triage_card(
    binding: TriageCardBinding,
    state: TriageCardState,
    status_markdown: str,
    *,
    detail_task_id: str = "",
) -> dict:
    task_id = binding.task_id[:12] if binding.task_id else "尚未创建"
    current_pipeline_id = binding.current_pipeline_id or binding.pipeline_id
    current_pipeline_sha = binding.current_pipeline_sha or binding.pipeline_sha
    pipeline_sha = current_pipeline_sha[:12] if current_pipeline_sha else "未知"
    context = [
        f"**MR**: [{binding.mr_title or binding.mr_url}]({binding.mr_url})",
        f"**分支**: `{binding.source_branch or '未知'}`",
        f"**Pipeline**: `{current_pipeline_id}`",
        f"**Commit**: `{pipeline_sha}`",
        f"**任务**: `{task_id}`",
        "",
        "---",
        "**原始失败信息**",
        binding.original_markdown,
    ]
    current_status = status_markdown or binding.status_markdown
    if current_status:
        context.extend(["", "---", "**当前状态**", current_status])
    if binding.repair_items:
        labels = {
            RepairItemStatus.PENDING: "待修复",
            RepairItemStatus.QUEUED: "已进入修复队列",
            RepairItemStatus.RUNNING: "正在修复",
            RepairItemStatus.WAITING_PIPELINE: "等待流水线",
            RepairItemStatus.SUCCEEDED: "已修复",
            RepairItemStatus.BLOCKED: "外部依赖阻塞",
            RepairItemStatus.FAILED: "修复失败，可重试",
            RepairItemStatus.RESOLVED: "已通过",
        }
        context.extend(["", "---", "**修复进度**"])
        for item in binding.repair_items:
            item_status = (
                "模型服务不可用，可重试"
                if state is TriageCardState.REPAIR_MODEL_UNAVAILABLE and item.status is RepairItemStatus.FAILED
                else labels[item.status]
            )
            if binding.active_task_id and item.task_id != binding.active_task_id and item.status in {
                RepairItemStatus.PENDING,
                RepairItemStatus.FAILED,
            }:
                item_status = "等待当前修复完成"
            context.append(f"- **{item.display_name}**：{item_status}")
            if item.status_markdown:
                context.append(f"  - {item.status_markdown}")
        context.extend(_failure_explanation_lines(binding))
    ut_state = binding.post_repair_ut
    if ut_state.status is not PostRepairUTStatus.IDLE:
        labels = {
            PostRepairUTStatus.QUEUED: "已进入队列",
            PostRepairUTStatus.RUNNING: "正在补充",
            PostRepairUTStatus.WAITING_PIPELINE: "等待流水线",
            PostRepairUTStatus.SUCCEEDED: "补测成功",
            PostRepairUTStatus.PARTIAL: "部分完成",
            PostRepairUTStatus.UNVERIFIED: "已补充，覆盖率无法验证",
            PostRepairUTStatus.FAILED: "补测失败，修改已撤回",
            PostRepairUTStatus.CANCELING: "正在取消并撤回",
            PostRepairUTStatus.CANCELED: "已取消并撤回",
            PostRepairUTStatus.ROLLBACK_FAILED: "补测失败，自动撤回未完成",
        }
        context.extend(["", "---", "**单元测试补充**", f"- **状态**：{labels[ut_state.status]}"])
        if ut_state.status_markdown:
            context.append(f"- {ut_state.status_markdown}")
        if ut_state.coverage_before is not None:
            context.append(f"- **补测前覆盖率**：{ut_state.coverage_before:g}%")
        if ut_state.coverage_after is not None:
            context.append(f"- **补测后覆盖率**：{ut_state.coverage_after:g}%")
    repair_mode = _binding_repair_card_mode(binding)
    selectable_items = [
        item
        for item in binding.repair_items
        if item.status in {RepairItemStatus.PENDING, RepairItemStatus.FAILED}
    ]
    if repair_mode is RepairCardMode.MULTI_SELECT and selectable_items and not binding.active_task_id:
        context.extend(["", "---"])
        for item in selectable_items:
            job_names = "、".join(item.failed_job_names) or "未提供"
            context.append(f"**{item.display_name} Jobs**：{job_names}")
        context.extend(["", "**请选择需要自动修复的问题：**"])
    elements: list[dict] = [{"tag": "markdown", "content": "\n".join(context)}]
    if binding.repair_items and not binding.active_task_id:
        if repair_mode is RepairCardMode.MULTI_SELECT:
            if selectable_items:
                elements.append(render_repair_selection_form(binding))
            _append_repair_rollback_action(elements, binding, state)
            _append_post_repair_ut_action(elements, binding, state)
            _append_repair_details_action(elements, detail_task_id)
            return {
                "config": {"wide_screen_mode": True, "update_multi": True},
                "header": {
                    "title": {"tag": "plain_text", "content": triage_card_title(binding, state)},
                    "template": triage_card_template(binding, state),
                },
                "elements": elements,
            }
        actions = []
        for item in binding.repair_items:
            if item.status not in {RepairItemStatus.PENDING, RepairItemStatus.FAILED}:
                continue
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": item.label},
                    "type": item.button_type,
                    "value": {
                        "command": item.command.lstrip("/"),
                        "category": item.category.value,
                        "mr_url": binding.mr_url,
                        "card_id": binding.card_id,
                        "pipeline_id": current_pipeline_id,
                        "pipeline_sha": current_pipeline_sha,
                        "revision": binding.revision,
                    },
                }
            )
        if actions:
            elements.append({"tag": "action", "actions": actions})
    elif binding.active_task_id and state in {
        TriageCardState.REPAIR_QUEUED,
        TriageCardState.REPAIR_RUNNING,
        TriageCardState.WAITING_PIPELINE,
    }:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消修复"},
                        "type": "default",
                        "value": {
                            "command": "cancel-repair",
                            "task_id": binding.active_task_id,
                            "mr_url": binding.mr_url,
                            "card_id": binding.card_id,
                            "pipeline_id": current_pipeline_id,
                            "pipeline_sha": current_pipeline_sha,
                            "revision": binding.revision,
                        },
                    }
                ],
            }
        )
    elif state is TriageCardState.PIPELINE_FAILED and repair_mode is not RepairCardMode.MULTI_SELECT:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "修复编译错误"},
                        "type": "primary",
                        "value": {
                            "command": "triage",
                            "mr_url": binding.mr_url,
                            "card_id": binding.card_id,
                            "pipeline_id": binding.pipeline_id,
                        },
                    }
                ],
            }
        )
    _append_repair_rollback_action(elements, binding, state)
    _append_post_repair_ut_action(elements, binding, state)
    _append_repair_details_action(elements, detail_task_id)
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": triage_card_title(binding, state)},
            "template": triage_card_template(binding, state),
        },
        "elements": elements,
    }
