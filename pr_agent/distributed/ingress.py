import asyncio
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlparse

from pr_agent.config_loader import get_settings
from pr_agent.distributed.broker import CancelRequestResult, EnqueueResult, RedisBroker, StaleCardActionError
from pr_agent.distributed.models import (
    AutoWorkflowDecision,
    MrKey,
    PipelineEvent,
    RepairCategory,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.suggestions.review_tracking import (
    ensure_creation_review,
    finish_review_run,
    record_review_event,
    update_review_run,
)


def extract_project_path(payload: dict[str, Any]) -> str:
    project = payload.get("project") or {}
    object_attributes = payload.get("object_attributes") or {}
    target = object_attributes.get("target") or {}
    return str(
        project.get("path_with_namespace")
        or target.get("path_with_namespace")
        or payload.get("project_path")
        or project.get("id")
        or payload.get("project_id")
        or ""
    )


def extract_mr_key(payload: dict[str, Any]) -> MrKey | None:
    project_id = extract_project_path(payload)
    object_attributes = payload.get("object_attributes") or {}
    merge_request = payload.get("merge_request") or {}
    iid = object_attributes.get("iid") or merge_request.get("iid")
    if not project_id or not iid:
        return None
    return MrKey(project_id=project_id, iid=int(iid))


def extract_mr_url(payload: dict[str, Any]) -> str:
    object_attributes = payload.get("object_attributes") or {}
    merge_request = payload.get("merge_request") or {}
    value = str(object_attributes.get("url") or merge_request.get("url") or "")
    return value.split("#", 1)[0]


def extract_creation_head_sha(payload: dict[str, Any]) -> str:
    attributes = payload.get("object_attributes") or {}
    last_commit = attributes.get("last_commit") or {}
    return str(
        last_commit.get("id")
        or last_commit.get("sha")
        or attributes.get("last_commit_id")
        or attributes.get("sha")
        or ""
    )


def build_creation_idempotency_key(project_path: str, mr_iid: str | int, commit_sha: str) -> str:
    """Return the stable identity shared by webhook admission and sync recovery."""
    del commit_sha  # The initial SHA stays in the task payload for audit; MR creation itself is the identity.
    return f"mr-create:{quote(str(project_path), safe='')}:{int(mr_iid)}"


def build_creation_task(
    record: dict[str, Any],
    *,
    source: str,
    event: dict[str, Any] | None = None,
) -> TaskEnvelope:
    """Build the automatic workflow task for an MR's initial revision."""
    project_path = str(record.get("project_path") or record.get("project") or "")
    mr_iid = int(record.get("mr_iid") or record.get("iid"))
    commit_sha = str(record.get("commit_sha") or record.get("sha") or "")
    return TaskEnvelope.new(
        kind=TaskKind.AUTO_WORKFLOW,
        source=source,
        mr=MrKey(project_id=project_path, iid=mr_iid),
        pr_url=str(record.get("mr_url") or record.get("web_url") or ""),
        command="/auto",
        payload={
            "commands": list(get_settings().get("gitlab.pr_commands", [])),
            "event": event or {},
            "initial_commit_sha": commit_sha,
            "project_id": str(record.get("project_id") or ""),
        },
        idempotency_key=build_creation_idempotency_key(project_path, mr_iid, commit_sha),
    )


def build_gitlab_event_dedup_key(payload: dict[str, Any], headers: Mapping[str, str]) -> str:
    object_kind = payload.get("object_kind", "")
    object_attributes = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    project_id = project.get("id") or payload.get("project_id") or extract_project_path(payload)

    if object_kind == "note":
        note_id = object_attributes.get("id") or ""
        note_action = object_attributes.get("action") or ""
        mr_iid = (payload.get("merge_request") or {}).get("iid") or ""
        if note_id:
            return f"note:{project_id}:{mr_iid}:{note_id}:{note_action}"
    elif object_kind == "merge_request":
        mr_id = object_attributes.get("id") or object_attributes.get("iid") or ""
        mr_action = object_attributes.get("action") or ""
        updated_at = object_attributes.get("updated_at") or ""
        if mr_id:
            return f"mr:{project_id}:{mr_id}:{mr_action}:{updated_at}"
    elif object_kind == "pipeline":
        pipeline_id = object_attributes.get("id") or payload.get("id") or ""
        pipeline_status = object_attributes.get("status") or payload.get("status") or ""
        pipeline_ref = object_attributes.get("ref") or payload.get("ref") or ""
        if pipeline_id:
            return f"pipeline:{project_id}:{pipeline_id}:{pipeline_status}:{pipeline_ref}"

    event_uuid = str(headers.get("X-Gitlab-Event-UUID") or headers.get("x-gitlab-event-uuid") or "").strip()
    if event_uuid:
        return f"uuid:{event_uuid}"
    raise ValueError("GitLab webhook does not contain a stable idempotency key")


class QueueIngress:
    def __init__(self, broker: RedisBroker, metrics=None, pipeline_freshness_checker=None):
        self.broker = broker
        self.metrics = metrics
        self.pipeline_freshness_checker = pipeline_freshness_checker or self._check_pipeline_freshness

    async def enqueue_creation_task(
        self,
        task: TaskEnvelope,
        *,
        webhook_id: str,
        tracking_path: str | None = None,
    ) -> EnqueueResult:
        """Persist and admit one idempotent MR-creation review task."""
        path_kwargs = {"path": tracking_path} if tracking_path else {}
        review_run_id = ensure_creation_review({
            "project_id": str(task.payload.get("project_id") or ""),
            "project_path": task.mr.project_id if task.mr else "",
            "mr_iid": str(task.mr.iid) if task.mr else "",
            "mr_url": task.pr_url,
            "commit_sha": str(task.payload.get("initial_commit_sha") or ""),
            "task_id": task.task_id,
            "webhook_id": webhook_id,
        }, **path_kwargs)
        try:
            result = await self.broker.enqueue_task(task)
        except Exception as exc:
            if review_run_id:
                finish_review_run(
                    "failed", review_run_id, stage="queue_failed",
                    error_code=type(exc).__name__, error_message=str(exc),
                    unpublished_reason="queue_admission_failed",
                    **path_kwargs,
                )
                record_review_event(
                    review_run_id, "queue_failed", "queue_failed", status="failed",
                    error_code=type(exc).__name__, error_message=str(exc),
                    details={"reason_code": "queue_admission_failed"},
                    **path_kwargs,
                )
            raise
        if review_run_id:
            update_review_run(
                review_run_id, stage="queued", status="running", error_code=None,
                error_message=None, completed_at=None, unpublished_reason=None, **path_kwargs,
            )
            record_review_event(review_run_id, "workflow_queued", "queued", **path_kwargs)
        return result

    async def enqueue_gitlab_event(
        self, payload: dict[str, Any], headers: Mapping[str, str]
    ) -> EnqueueResult:
        started_at = time.monotonic()
        idempotency_key = build_gitlab_event_dedup_key(payload, headers)
        object_kind = str(payload.get("object_kind") or "")
        object_attributes = payload.get("object_attributes") or {}
        mr = extract_mr_key(payload)
        pr_url = extract_mr_url(payload)

        if object_kind == "merge_request" and str(object_attributes.get("action") or "opened") in {
            "open",
            "opened",
            "reopened",
        }:
            task = build_creation_task({
                "project_id": str((payload.get("project") or {}).get("id") or ""),
                "project_path": mr.project_id if mr else extract_project_path(payload),
                "mr_iid": str(mr.iid) if mr else "",
                "mr_url": pr_url,
                "commit_sha": extract_creation_head_sha(payload),
            }, source="gitlab", event=payload)
            result = await self.enqueue_creation_task(task, webhook_id=task.idempotency_key)
            await self._record_ingress_metrics("gitlab", result, started_at)
            return result
        elif object_kind == "note" and self._note_command(payload).startswith("/"):
            task = TaskEnvelope.new(
                kind=TaskKind.PR_COMMAND,
                source="gitlab",
                mr=mr,
                pr_url=pr_url,
                command=self._note_command(payload),
                payload={"event": payload, "reviewer_user": (payload.get("user") or {}).get("username")},
                idempotency_key=idempotency_key,
            )
        else:
            task = TaskEnvelope.new(
                kind=TaskKind.GITLAB_EVENT,
                source="gitlab",
                mr=mr,
                pr_url=pr_url,
                command="",
                payload={"event": payload},
                idempotency_key=idempotency_key,
            )
        try:
            result = await self.broker.enqueue_task(task)
        except Exception:
            raise
        await self._record_ingress_metrics("gitlab", result, started_at)
        if object_kind == "pipeline":
            event = PipelineEvent.from_gitlab_payload(payload)
            if event.sha:
                await self.broker.publish_pipeline_event(event)
        return result

    async def enqueue_feishu_command(
        self,
        *,
        command: str,
        mr_url: str,
        sender_id: str,
        idempotency_key: str,
        event: dict[str, Any] | None = None,
        card_id: str = "",
        open_message_id: str = "",
        category: str = "",
        selected_categories: tuple[str, ...] = (),
        pipeline_id: int | None = None,
        pipeline_sha: str = "",
        revision: int | None = None,
    ) -> EnqueueResult:
        from pr_agent.feishu.triage_card import parse_mr_identity

        identity = parse_mr_identity(mr_url)
        mr = MrKey(identity.project_id, identity.mr_iid)
        normalized_command = command if command.startswith("/") else f"/{command}"
        if (
            normalized_command.lower() == "/repair-pipeline"
            and card_id
            and open_message_id
            and pipeline_id is not None
        ):
            if selected_categories:
                await self.validate_repair_selection_freshness(card_id, mr, pipeline_id, selected_categories)
            else:
                await self.validate_unified_repair_freshness(card_id, mr, pipeline_id)
        task = TaskEnvelope.new(
            kind=TaskKind.PR_COMMAND,
            source="feishu",
            mr=mr,
            pr_url=identity.mr_url,
            command=normalized_command,
            payload={
                "sender_id": sender_id,
                "event": event or {},
                "repair_category": category,
                "selected_categories": list(selected_categories),
                "card_revision": revision,
                "source_pipeline_id": pipeline_id,
                "source_pipeline_sha": pipeline_sha,
            },
            idempotency_key=idempotency_key,
        )
        started_at = time.monotonic()
        if card_id and open_message_id:
            from pr_agent.distributed.notifications import triage_card_ttl_seconds

            ttl_seconds = triage_card_ttl_seconds()
            result = await self.broker.enqueue_task_with_card(
                task,
                card_id,
                open_message_id,
                ttl_seconds,
                sender_id=sender_id,
                category=category,
                selected_categories=selected_categories,
                pipeline_id=pipeline_id,
                pipeline_sha=pipeline_sha,
                revision=revision,
            )
        else:
            result = await self.broker.enqueue_task(task)
        await self._record_ingress_metrics("feishu", result, started_at)
        return result

    async def enqueue_post_repair_ut(
        self,
        *,
        repair_task_id: str,
        mr_url: str,
        sender_id: str,
        idempotency_key: str,
        card_id: str,
        open_message_id: str,
        pipeline_id: int,
        pipeline_sha: str,
        revision: int,
    ) -> EnqueueResult:
        from pr_agent.distributed.notifications import triage_card_ttl_seconds
        from pr_agent.feishu.triage_card import parse_mr_identity
        from pr_agent.triage.post_repair_ut import (
            is_post_repair_ut_eligible,
            post_repair_ut_coverage_threshold,
        )

        identity = parse_mr_identity(mr_url)
        mr = MrKey(identity.project_id, identity.mr_iid)
        binding = await self.broker.get_triage_card(card_id)
        original = await self.broker.get_task(repair_task_id)
        coverage_before = original.pipeline_repair_state.final_coverage if original is not None else None
        coverage_status = original.pipeline_repair_state.final_coverage_status if original is not None else ""
        eligible_binding = (
            replace(
                binding,
                post_repair_ut=replace(
                    binding.post_repair_ut,
                    coverage_before=coverage_before,
                    coverage_status_before=coverage_status,
                ),
            )
            if binding is not None
            else None
        )
        if (
            binding is None
            or original is None
            or original.mr != mr
            or original.envelope.source != "feishu"
            or original.status is not TaskStatus.COMPLETED
            or original.pipeline_repair_state.final_pipeline_status != "success"
            or binding.task_id != repair_task_id
            or binding.current_pipeline_id != pipeline_id
            or binding.current_pipeline_sha != pipeline_sha
            or binding.revision != revision
            or eligible_binding is None
            or not is_post_repair_ut_eligible(eligible_binding)
        ):
            raise StaleCardActionError("post-repair UT action is stale or ineligible")
        freshness = await self.pipeline_freshness_checker(binding)
        if not freshness.current:
            raise StaleCardActionError(
                f"post-repair UT card is no longer current: {freshness.state.value}:{freshness.reason}"
            )
        task = TaskEnvelope.new(
            kind=TaskKind.POST_REPAIR_UT,
            source="feishu",
            mr=mr,
            pr_url=identity.mr_url,
            command="/ut",
            payload={
                "sender_id": sender_id,
                "origin_repair_task_id": repair_task_id,
                "baseline_pipeline_id": pipeline_id,
                "baseline_sha": pipeline_sha,
                "coverage_before": coverage_before,
                "coverage_status_before": coverage_status,
                "card_revision": revision,
            },
            idempotency_key=f"post-repair-ut:{card_id}:{repair_task_id}:{pipeline_sha}",
        )
        return await self.broker.admit_post_repair_ut(
            task,
            repair_task_id=repair_task_id,
            card_id=card_id,
            open_message_id=open_message_id,
            sender_id=sender_id,
            pipeline_id=pipeline_id,
            pipeline_sha=pipeline_sha,
            revision=revision,
            ttl_seconds=triage_card_ttl_seconds(),
            coverage_threshold=post_repair_ut_coverage_threshold(),
        )

    async def validate_repair_selection_freshness(
        self,
        card_id: str,
        mr: MrKey,
        pipeline_id: int,
        selected_categories: tuple[str, ...],
    ) -> TriageCardBinding:
        binding = await self.broker.resolve_repair_card_selection(card_id, mr, pipeline_id, selected_categories)
        freshness = await self.pipeline_freshness_checker(binding)
        if not freshness.current:
            raise StaleCardActionError(
                f"repair card is no longer current: {freshness.state.value}:{freshness.reason}"
            )
        return binding

    async def validate_unified_repair_freshness(
        self,
        card_id: str,
        mr: MrKey,
        pipeline_id: int,
    ) -> TriageCardBinding:
        binding = await self.broker.resolve_unified_repair_card(card_id, mr, pipeline_id)
        freshness = await self.pipeline_freshness_checker(binding)
        if not freshness.current:
            raise StaleCardActionError(
                f"repair card is no longer current: {freshness.state.value}:{freshness.reason}"
            )
        return binding

    @staticmethod
    async def _check_pipeline_freshness(binding: TriageCardBinding):
        from pr_agent.servers.gitlab_webhook import _gitlab_api_get
        from pr_agent.triage.pipeline_freshness import check_pipeline_freshness

        def api_get(path: str, *, params: dict | None = None):
            return _gitlab_api_get(path, params=params, timeout=1.0)

        return await asyncio.to_thread(
            check_pipeline_freshness,
            api_get=api_get,
            project_id=binding.project_id,
            mr_iid=binding.mr_iid,
            pipeline_id=binding.current_pipeline_id,
            pipeline_sha=binding.current_pipeline_sha,
            ref=binding.source_branch,
            attempts=1,
        )

    async def queue_triage_card_update(
        self,
        task_id: str,
        state: TriageCardState,
        status_markdown: str,
    ) -> bool:
        from pr_agent.distributed.notifications import queue_triage_card_update

        return await queue_triage_card_update(self.broker, task_id, state, status_markdown)

    async def resolve_unified_repair_card_action(
        self,
        *,
        mr_url: str,
        card_id: str,
        pipeline_id: int,
    ) -> tuple[str, str, int]:
        from pr_agent.feishu.triage_card import parse_mr_identity

        if not card_id or pipeline_id <= 0:
            raise StaleCardActionError("repair card identity is incomplete")
        identity = parse_mr_identity(mr_url)
        binding = await self.broker.resolve_unified_repair_card(
            card_id,
            MrKey(identity.project_id, identity.mr_iid),
            pipeline_id,
        )
        return RepairCategory.PIPELINE.value, binding.current_pipeline_sha, binding.revision

    async def cancel_feishu_repair(
        self,
        *,
        task_id: str,
        card_id: str,
        open_message_id: str,
        sender_id: str,
        revision: int,
    ) -> CancelRequestResult:
        result = await self.broker.request_repair_cancel(
            task_id,
            card_id,
            open_message_id,
            sender_id,
            revision,
        )
        if not result.accepted or result.terminal_status not in {
            TaskStatus.QUEUED.value,
            TaskStatus.ASSIGNED.value,
            TaskStatus.WAITING_PIPELINE.value,
        }:
            return result
        stored = await self.broker.get_task(task_id)
        if stored is None:
            return result
        lease = await self.broker.get_mr_lease(stored.mr) if stored.mr and stored.worker_id else None
        if stored.worker_id and (
            lease is None
            or lease.worker_id != stored.worker_id
            or lease.fencing_token != stored.fencing_token
        ):
            return result
        rollback = await self.broker.finalize_cancel_or_enqueue_rollback(stored.envelope, lease)
        finalized = rollback is None
        if finalized and stored.envelope.command.strip().split(maxsplit=1)[0].lower() == "/repair-pipeline":
            from pr_agent.triage.terminal import persist_repair_terminal

            await persist_repair_terminal(self.broker, task_id, error="用户取消修复")
        return CancelRequestResult(task_id, result.accepted, TaskStatus.CANCELED.value if finalized else "")

    async def cancel_post_repair_ut(
        self,
        *,
        task_id: str,
        card_id: str,
        open_message_id: str,
        sender_id: str,
        revision: int,
    ) -> CancelRequestResult:
        """Request cancellation without changing the successful repair result."""
        return await self.broker.request_post_repair_ut_cancel(
            task_id,
            card_id,
            open_message_id,
            sender_id,
            revision,
        )

    async def rollback_feishu_repair(
        self,
        *,
        repair_task_id: str,
        card_id: str,
        open_message_id: str,
        sender_id: str,
        revision: int,
    ):
        return await self.broker.request_repair_rollback(
            repair_task_id,
            card_id,
            open_message_id,
            sender_id,
            revision,
            trigger="post_repair",
        )

    async def _record_ingress_metrics(self, source: str, result: EnqueueResult, started_at: float) -> None:
        if self.metrics is None:
            return
        try:
            await self.metrics.observe_ms(
                "webhook_enqueue_ms",
                (time.monotonic() - started_at) * 1000,
                labels={"source": source},
            )
            if not result.created and not result.recovered:
                await self.metrics.increment("dedup_rejected", labels={"source": source})
        except Exception:
            pass

    @staticmethod
    def _note_command(payload: dict[str, Any]) -> str:
        object_attributes = payload.get("object_attributes") or {}
        return str(object_attributes.get("note") or object_attributes.get("description") or "").strip()

    @staticmethod
    def _mr_from_url(mr_url: str) -> MrKey:
        parts = [part for part in urlparse(mr_url).path.split("/") if part]
        try:
            separator = parts.index("-")
            if parts[separator + 1] != "merge_requests":
                raise ValueError
            return MrKey(project_id="/".join(parts[:separator]), iid=int(parts[separator + 2]))
        except (ValueError, IndexError) as error:
            raise ValueError(f"invalid GitLab MR URL: {mr_url}") from error


class GitLabEventJobs:
    """Run existing non-command GitLab webhook jobs inside an Agent worker."""

    async def prepare_auto_workflow(self, task: TaskEnvelope) -> AutoWorkflowDecision:
        from pr_agent.git_providers.utils import apply_repo_settings
        from pr_agent.servers import gitlab_webhook

        await asyncio.to_thread(apply_repo_settings, task.pr_url)
        if get_settings().config.disable_auto_feedback:
            return AutoWorkflowDecision.skip("auto_feedback_disabled", "Automatic feedback is disabled")
        return gitlab_webhook.evaluate_pr_logic(task.payload["event"])

    async def before_command(self, task: TaskEnvelope) -> None:
        event = task.payload.get("event")
        if not isinstance(event, dict) or event.get("object_kind") != "note":
            return
        from pr_agent.servers import gitlab_webhook

        bot_username = get_settings().get("GITLAB.BOT_USERNAME", "")
        await asyncio.to_thread(gitlab_webhook.collect_note_feedback, event, bot_username)
        await asyncio.to_thread(gitlab_webhook._handle_inline_gate_note, event)

    async def execute(self, task: TaskEnvelope) -> None:
        from pr_agent.servers import gitlab_webhook

        payload = task.payload["event"]
        object_kind = payload.get("object_kind")
        if object_kind == "push":
            await asyncio.to_thread(gitlab_webhook._handle_inline_apply_push, payload)
        elif object_kind == "pipeline":
            await gitlab_webhook._notify_feishu_pipeline_failure(payload)
        elif object_kind == "merge_request":
            await self._execute_merge_request_jobs(gitlab_webhook, payload)
        elif object_kind == "note":
            bot_username = get_settings().get("GITLAB.BOT_USERNAME", "")
            await asyncio.to_thread(gitlab_webhook.collect_note_feedback, payload, bot_username)
            await asyncio.to_thread(gitlab_webhook._handle_inline_gate_note, payload)

    @staticmethod
    async def _execute_merge_request_jobs(gitlab_webhook, payload: dict[str, Any]) -> None:
        action = str((payload.get("object_attributes") or {}).get("action") or "opened")
        if action == "update":
            await asyncio.to_thread(gitlab_webhook._handle_feedback_gate_push, payload)
            await asyncio.to_thread(gitlab_webhook._handle_inline_gate_push, payload)
            await asyncio.to_thread(gitlab_webhook._handle_title_issue_link_refresh, payload)
