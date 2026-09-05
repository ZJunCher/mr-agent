import copy
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from threading import Lock, Thread

import redis
import requests as _requests
import uvicorn
from fastapi import APIRouter, FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.distributed.config import load_distributed_settings
from pr_agent.distributed.health import DistributedHealthService
from pr_agent.distributed.ingress import QueueIngress, build_gitlab_event_dedup_key, extract_project_path
from pr_agent.distributed.metrics import DistributedMetrics
from pr_agent.distributed.models import AutoWorkflowDecision
from pr_agent.distributed.redis_client import RedisClientFactory
from pr_agent.feedback import gate
from pr_agent.feishu.feishu_webhook import router as feishu_router
from pr_agent.feishu.long_connection_status import snapshot as feishu_long_connection_snapshot
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.secret_providers import get_secret_provider
from pr_agent.servers.dashboard_routes import router as dashboard_router
from pr_agent.servers.repair_results import configure_repair_results_broker
from pr_agent.servers.repair_results import router as repair_results_router
from pr_agent.suggestions import inline_gate_status, inline_thread_sync
from pr_agent.suggestions.gitlab_mr_sync import (
    capture_webhook_mr,
    start_sync_worker_if_enabled,
    stop_sync_worker,
)
from pr_agent.suggestions.inline_apply_detector import handle_push_event
from pr_agent.suggestions.inline_feedback_collector import handle_note_event as collect_note_feedback
from pr_agent.suggestions.review_tracking import mark_creation_tracking_started
from pr_agent.triage.failure_categories import (
    categorize_failed_job,
    classify_failed_jobs,
    pipeline_repair_item,
)
from pr_agent.triage.format_job_preflight import (
    FORMAT_CI_JOB_CONFIGURATION,
    classify_format_job_trace,
)
from pr_agent.triage.pipeline_freshness import check_pipeline_freshness

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
router = APIRouter()

secret_provider = get_secret_provider() if get_settings().get("CONFIG.SECRET_PROVIDER") else None

_WEBHOOK_DEDUP_TTL_SECONDS = 120
_recent_webhook_events = {}
_recent_webhook_events_lock = Lock()
_feishu_worker_started = False
_feishu_worker_lock = Lock()
_feishu_worker_thread = None
_queue_redis_client = None
_queue_ingress = None

def _read_feishu_setting(setting_key: str, env_key: str) -> str:
    """Read Feishu settings with env var precedence for container deployments."""
    env_val = (os.environ.get(env_key) or "").strip()
    if env_val:
        return env_val
    return str(get_settings().get(f"FEISHU.{setting_key}", "") or "").strip()


def _start_feishu_long_connection_worker_if_needed():
    global _feishu_worker_started, _feishu_worker_thread

    enabled = bool(get_settings().get("FEISHU.LONG_CONNECTION_ENABLED", True))
    auto_start = bool(get_settings().get("FEISHU.LONG_CONNECTION_AUTO_START", True))
    if not enabled or not auto_start:
        get_logger().info("Feishu long-connection worker disabled by settings")
        return

    app_id = _read_feishu_setting("APP_ID", "FEISHU_APP_ID")
    app_secret = _read_feishu_setting("APP_SECRET", "FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        get_logger().warning(
            "Skip starting Feishu long-connection worker: FEISHU.APP_ID/APP_SECRET not configured"
        )
        return

    with _feishu_worker_lock:
        if _feishu_worker_started:
            return

        def _run_worker():
            try:
                from pr_agent.feishu.long_connection_worker import start as start_feishu_long_connection

                start_feishu_long_connection()
            except Exception:
                get_logger().exception("Feishu long-connection worker exited with error")

        _feishu_worker_thread = Thread(target=_run_worker, daemon=True, name="feishu-long-connection-worker")
        _feishu_worker_thread.start()
        _feishu_worker_started = True
        get_logger().info("Feishu long-connection worker started in background thread")


def _get_queue_ingress() -> QueueIngress:
    global _queue_redis_client, _queue_ingress

    if _queue_ingress is None:
        distributed_settings = load_distributed_settings()
        _queue_redis_client = RedisClientFactory(distributed_settings.redis_url).create_async()
        from pr_agent.distributed.broker import RedisBroker

        _queue_ingress = QueueIngress(
            RedisBroker(_queue_redis_client, distributed_settings),
            DistributedMetrics(_queue_redis_client),
        )
    return _queue_ingress


def _build_gitlab_event_dedup_key(request_json: dict, request: Request) -> str:
    """Build a stable key to identify duplicate GitLab webhook deliveries."""
    try:
        return build_gitlab_event_dedup_key(request_json, request.headers)
    except ValueError:
        return ""


def _is_duplicate_gitlab_event(dedup_key: str) -> bool:
    if not dedup_key:
        return False

    now = time.monotonic()
    expire_before = now - _WEBHOOK_DEDUP_TTL_SECONDS

    with _recent_webhook_events_lock:
        stale_keys = [key for key, ts in _recent_webhook_events.items() if ts < expire_before]
        for key in stale_keys:
            del _recent_webhook_events[key]

        if dedup_key in _recent_webhook_events:
            return True

        _recent_webhook_events[dedup_key] = now
        return False


async def handle_request(api_url: str, body: str, log_context: dict, sender_id: str, notify=None):
    log_context["action"] = body
    log_context["event"] = "pull_request" if body == "/review" else "comment"
    log_context["api_url"] = api_url
    log_context["app_name"] = get_settings().get("CONFIG.APP_NAME", "Unknown")

    # 只处理以'/'开头的命令，避免普通评论触发告警或噪声
    try:
        normalized = (body or "").strip()
        if not normalized.startswith("/"):
            return
    except Exception:
        return

    with get_logger().contextualize(**log_context):
        reviewer_user = log_context.get("sender")
        await PRAgent().handle_request(api_url, body, notify, reviewer_user=reviewer_user)

async def _perform_commands_gitlab(commands_conf: str, agent: PRAgent, api_url: str,
                                   log_context: dict, data: dict):
    apply_repo_settings(api_url)
    if commands_conf == "pr_commands" and get_settings().config.disable_auto_feedback:  # auto commands for PR, and auto feedback is disabled
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {api_url=}", **log_context)
        return
    if not should_process_pr_logic(data): # Here we already updated the configurations
        return
    commands = get_settings().get(f"gitlab.{commands_conf}", {})
    get_settings().set("config.is_auto_command", True)
    for command in commands:
        try:
            split_command = command.split(" ")
            command = split_command[0]
            args = split_command[1:]
            other_args = update_settings_from_args(args)
            new_command = ' '.join([command] + other_args)
            get_logger().info(f"Performing command: {new_command}")
            with get_logger().contextualize(**log_context):
                await agent.handle_request(api_url, new_command)
        except Exception as e:
            get_logger().error(f"Failed to perform command {command}: {e}")


def is_bot_user(data) -> bool:
    try:
        conf_section = get_settings().get("gitlab", {})
        conf_ignore = conf_section.get("ignore_bot_user", True)
        sender_name = data.get("user", {}).get("name", "unknown").lower()
        if not conf_ignore:
            return False
        allowlist = conf_section.get("bot_user_allowlist", [])
        try:
            allowlist = [a.lower() for a in allowlist] if isinstance(allowlist, list) else []
        except Exception:
            allowlist = []
        if sender_name in allowlist:
            return False
        bot_indicators = ['codium', 'bot_', 'bot-', '_bot', '-bot']
        if any(indicator in sender_name for indicator in bot_indicators):
            get_logger().info(f"Skipping GitLab bot user: {sender_name}")
            return True
    except Exception as e:
        get_logger().error(f"Failed 'is_bot_user' logic: {e}")
    return False

def is_draft(data) -> bool:
    try:
        if 'draft' in data.get('object_attributes', {}):
            return data['object_attributes']['draft']

        # for gitlab server version before 16
        elif 'Draft:' in data.get('object_attributes', {}).get('title'):
            return True
    except Exception as e:
        get_logger().error(f"Failed 'is_draft' logic: {e}")
    return False

def is_draft_ready(data) -> bool:
    try:
        if 'draft' in data.get('changes', {}):
            # Handle both boolean values and string values for compatibility
            previous = data['changes']['draft']['previous']
            current = data['changes']['draft']['current']

            # Convert to boolean if they're strings
            if isinstance(previous, str):
                previous = previous.lower() == 'true'
            if isinstance(current, str):
                current = current.lower() == 'true'

            if previous is True and current is False:
                return True

        # for gitlab server version before 16
        elif 'title' in data.get('changes', {}):
            if 'Draft:' in data['changes']['title']['previous'] and 'Draft:' not in data['changes']['title']['current']:
                return True
    except Exception as e:
        get_logger().error(f"Failed 'is_draft_ready' logic: {e}")
    return False

def evaluate_pr_logic(data: dict) -> AutoWorkflowDecision:
    try:
        attributes = data.get("object_attributes") or {}
        if not attributes:
            return AutoWorkflowDecision.skip("missing_object_attributes", "Merge request attributes are missing")

        action = attributes.get("action")
        if action and action not in {"open", "opened", "reopened"}:
            get_logger().debug(f"Skipping MR processing for action '{action}'")
            return AutoWorkflowDecision.skip("unsupported_action", f"Unsupported merge request action: {action}")

        title = str(attributes.get("title") or "")
        sender = data.get("user", {}).get("username", "")
        repo_full_name = data.get("project", {}).get("path_with_namespace", "")

        # logic to ignore PRs from specific repositories
        ignore_repos = get_settings().get("CONFIG.IGNORE_REPOSITORIES", [])
        if ignore_repos and repo_full_name:
            if any(re.search(regex, repo_full_name) for regex in ignore_repos):
                get_logger().info(
                    f"Ignoring MR from repository '{repo_full_name}' due to 'config.ignore_repositories' setting"
                )
                return AutoWorkflowDecision.skip(
                    "ignored_repository", f"Repository is ignored: {repo_full_name}",
                )

        # logic to ignore PRs from specific users
        ignore_pr_users = get_settings().get("CONFIG.IGNORE_PR_AUTHORS", [])
        if ignore_pr_users and sender:
            if any(re.search(regex, sender) for regex in ignore_pr_users):
                get_logger().info(f"Ignoring PR from user '{sender}' due to 'config.ignore_pr_authors' settings")
                return AutoWorkflowDecision.skip("ignored_author", f"Author is ignored: {sender}")

        # logic to ignore MRs for titles, labels and source, target branches.
        ignore_mr_title = get_settings().get("CONFIG.IGNORE_PR_TITLE", [])
        ignore_mr_labels = get_settings().get("CONFIG.IGNORE_PR_LABELS", [])
        ignore_mr_source_branches = get_settings().get("CONFIG.IGNORE_PR_SOURCE_BRANCHES", [])
        ignore_mr_target_branches = get_settings().get("CONFIG.IGNORE_PR_TARGET_BRANCHES", [])

        #
        if ignore_mr_source_branches:
            source_branch = str(attributes.get("source_branch") or "")
            if any(re.search(regex, source_branch) for regex in ignore_mr_source_branches):
                get_logger().info(
                    f"Ignoring MR with source branch '{source_branch}' due to "
                    "gitlab.ignore_mr_source_branches settings"
                )
                return AutoWorkflowDecision.skip(
                    "ignored_source_branch", f"Source branch is ignored: {source_branch}",
                )

        if ignore_mr_target_branches:
            target_branch = str(attributes.get("target_branch") or "")
            if any(re.search(regex, target_branch) for regex in ignore_mr_target_branches):
                get_logger().info(
                    f"Ignoring MR with target branch '{target_branch}' due to "
                    "gitlab.ignore_mr_target_branches settings"
                )
                return AutoWorkflowDecision.skip(
                    "ignored_target_branch", f"Target branch is ignored: {target_branch}",
                )

        if ignore_mr_labels:
            labels = [str(label.get("title") or "") for label in attributes.get("labels", [])]
            if any(label in ignore_mr_labels for label in labels):
                labels_str = ", ".join(labels)
                get_logger().info(f"Ignoring MR with labels '{labels_str}' due to gitlab.ignore_mr_labels settings")
                return AutoWorkflowDecision.skip("ignored_label", f"Merge request label is ignored: {labels_str}")

        if ignore_mr_title:
            if any(re.search(regex, title) for regex in ignore_mr_title):
                get_logger().info(f"Ignoring MR with title '{title}' due to gitlab.ignore_mr_title settings")
                return AutoWorkflowDecision.skip("ignored_title", f"Merge request title is ignored: {title}")
    except Exception as exc:
        get_logger().error(f"Failed 'evaluate_pr_logic': {exc}")
        return AutoWorkflowDecision.skip("invalid_event", "Invalid merge request event")
    return AutoWorkflowDecision.allow()


def should_process_pr_logic(data) -> bool:
    return evaluate_pr_logic(data).allowed


def _extract_comparable_secret(secret_value: str) -> str | None:
    """
    Return a secret string to compare against the request token when possible.

    - Plain string secret: compare directly.
    - JSON secret: compare only if one of the known secret fields exists.
    - JSON secret without a comparable field: return None and treat lookup success as validation.
    """
    if not secret_value:
        return None

    try:
        parsed_secret = json.loads(secret_value)
    except Exception:
        return secret_value

    if isinstance(parsed_secret, dict):
        for key in ("shared_secret", "webhook_secret", "secret", "token"):
            value = parsed_secret.get(key)
            if isinstance(value, str) and value:
                return value

    return None


def _normalize_secret(value) -> str:
    """Normalize secret values to avoid false mismatches from surrounding whitespace/quotes."""
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if (normalized.startswith('"') and normalized.endswith('"')) or (
        normalized.startswith("'") and normalized.endswith("'")
    ):
        normalized = normalized[1:-1].strip()
    return normalized


def _extract_handle_request_payload(data: dict) -> tuple[str, str, dict, str]:
    """Build handle_request arguments from a GitLab webhook payload."""
    log_context = {"server_type": "gitlab_app"}
    sender_id = str(data.get("user", {}).get("id", ""))
    sender_username = data.get("user", {}).get("username", "")
    if sender_username:
        log_context["sender"] = sender_username

    object_kind = data.get("object_kind", "")
    object_attributes = data.get("object_attributes", {}) or {}
    merge_request = data.get("merge_request", {}) or {}

    body = ""
    api_url = ""
    if object_kind == "note":
        body = object_attributes.get("note") or object_attributes.get("description") or ""
        api_url = merge_request.get("url") or object_attributes.get("url") or ""
        if isinstance(api_url, str) and "#" in api_url:
            api_url = api_url.split("#", 1)[0]

    return api_url, body, log_context, sender_id


def _extract_mr_url(data: dict) -> str:
    """Extract MR URL from webhook payload for MR events."""
    try:
        object_attributes = data.get("object_attributes", {}) or {}
        merge_request = data.get("merge_request", {}) or {}
        return object_attributes.get("url") or merge_request.get("url") or ""
    except Exception:
        return ""


async def _notify_feishu_mr_author(request_json: dict) -> None:
    """
    On MR open/reopen, map the GitLab author to a Feishu user.
    Currently disabled — no card is pushed on MR creation.
    Triage cards are only sent when a pipeline fails.
    """
    return


def _get_pipeline_payload_parts(request_json: dict) -> tuple[int | None, int | None, str, str]:
    object_attributes = request_json.get("object_attributes", {}) or {}
    project = request_json.get("project", {}) or {}
    project_id = project.get("id") or request_json.get("project_id") or object_attributes.get("project_id")
    pipeline_id = object_attributes.get("id") or request_json.get("id")
    ref = object_attributes.get("ref") or request_json.get("ref") or ""
    status = object_attributes.get("status") or request_json.get("status") or ""
    return project_id, pipeline_id, str(ref or ""), str(status or "")


def _get_pipeline_sha(request_json: dict) -> str:
    """从 pipeline webhook payload 取触发该流水线的 commit SHA。"""
    object_attributes = request_json.get("object_attributes", {}) or {}
    return str(object_attributes.get("sha") or request_json.get("sha") or "")


def _ci_failure_capture_enabled() -> bool:
    value = get_settings().get("CI_FAILURE_DASHBOARD.CAPTURE_ENABLED", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _load_ci_job_trace(project_id: int, job_id: int) -> bytes:
    response = _gitlab_api_get(f"/api/v4/projects/{project_id}/jobs/{job_id}/trace")
    if response is None or not response.ok:
        return b""
    content = getattr(response, "content", None)
    if content is not None:
        return bytes(content)
    return str(getattr(response, "text", "") or "").encode("utf-8")


def _capture_ci_failure(
    *,
    request_json: dict,
    mr: dict,
    project_id: int,
    project_path: str,
    pipeline_id: int,
    pipeline_sha: str,
    failed_jobs: list[dict],
    card_id: str,
) -> int | None:
    """Persist bounded failure evidence without affecting notification delivery."""
    if not _ci_failure_capture_enabled():
        return None
    try:
        from pr_agent.feedback.store import get_db_path
        from pr_agent.triage.ci_failure_analysis import aggregate_failure, analyze_failed_jobs
        from pr_agent.triage.ci_failure_store import save_ci_failure

        jobs = analyze_failed_jobs(
            failed_jobs,
            lambda job_id: _load_ci_job_trace(project_id, job_id),
            pipeline_id=pipeline_id,
            memory_path=get_db_path(),
        )
        aggregate = aggregate_failure(jobs)
        attributes = request_json.get("object_attributes", {}) or {}
        author = mr.get("author") or {}
        return save_ci_failure(
            {
                "project_id": str(project_id),
                "project_path": project_path,
                "mr_iid": str(mr.get("iid") or ""),
                "mr_url": str(mr.get("web_url") or mr.get("url") or ""),
                "mr_title": str(mr.get("title") or ""),
                "mr_author": str(author.get("username") or ""),
                "source_branch": str(attributes.get("ref") or ""),
                "target_branch": str(mr.get("target_branch") or ""),
                "pipeline_id": pipeline_id,
                "pipeline_url": str(attributes.get("url") or ""),
                "pipeline_sha": pipeline_sha,
                "pipeline_status": "failed",
                "notification_state": "not_attempted",
                "card_id": card_id,
                "source": "webhook",
            },
            jobs,
            aggregate=aggregate,
        )
    except Exception as error:
        get_logger().error(f"Failed to capture CI failure: {type(error).__name__}")
        return None


def _record_ci_followup(
    project_id: int,
    mr_iid: int,
    pipeline_id: int,
    pipeline_sha: str,
    status: str,
) -> None:
    if not _ci_failure_capture_enabled():
        return
    try:
        from pr_agent.triage.ci_failure_store import record_followup_pipeline

        record_followup_pipeline(str(project_id), str(mr_iid), pipeline_id, pipeline_sha, status)
    except Exception as error:
        get_logger().error(f"Failed to enrich CI failure followup: {type(error).__name__}")


_UT_AGENT_COMMIT_PREFIX = "[UT Agent]"
_UT_AGENT_AUTHOR_EMAIL = "ut-agent@noreply.local"
_PR_AGENT_AUTHOR_EMAIL = "pr-agent@noreply.local"
_ROLLBACK_MARKER_RE = re.compile(
    r"\[pr-agent-rollback:(?P<repair_task_id>[A-Za-z0-9_-]{1,128}):"
    r"(?P<rollback_task_id>[A-Za-z0-9_-]{1,128})\]"
)


@dataclass(frozen=True)
class PipelineCommitOwnership:
    kind: str
    repair_task_id: str
    rollback_task_id: str


def _pipeline_commit_ownership(commit: dict) -> PipelineCommitOwnership | None:
    if str(commit.get("author_email") or "") != _PR_AGENT_AUTHOR_EMAIL:
        return None
    match = _ROLLBACK_MARKER_RE.search(str(commit.get("message") or ""))
    if match is None:
        return None
    return PipelineCommitOwnership(
        kind="repair_rollback",
        repair_task_id=match.group("repair_task_id"),
        rollback_task_id=match.group("rollback_task_id"),
    )


def _is_initial_rollback_pipeline(
    pipeline_sha: str,
    project_id,
    pipeline_id: int | None,
    pipeline_source: str,
) -> bool:
    """Return whether this is the first Pipeline created for a rollback SHA and source."""
    if not pipeline_id or not pipeline_source:
        return False
    response = _gitlab_api_get(
        f"/api/v4/projects/{project_id}/pipelines",
        params={
            "sha": pipeline_sha,
            "source": pipeline_source,
            "order_by": "id",
            "sort": "asc",
            "per_page": 1,
        },
    )
    if response is None or not getattr(response, "ok", False):
        return False
    pipelines = response.json()
    if not isinstance(pipelines, list) or not pipelines or not isinstance(pipelines[0], dict):
        return False
    earliest = pipelines[0]
    return (
        int(earliest.get("id") or 0) == int(pipeline_id)
        and str(earliest.get("source") or "") == pipeline_source
    )


def _should_suppress_pipeline_card(
    pipeline_sha: str,
    project_id,
    mr_iid: int,
    project_path: str = "",
    *,
    pipeline_id: int | None = None,
    pipeline_source: str = "",
) -> bool:
    """压制任务自有修复 Commit 在流程内产生的重复流水线失败卡片。

    普通 UT Agent 修复 Commit 仅当两个条件同时满足才压制：
      1. 触发该流水线的 commit 是 UT Agent 推送的（message 前缀或作者邮箱匹配）；
      2. 该 MR 的修复锁正被持有（Agent 仍在抢救中）。
    带合法任务标记的 PR-Agent 撤回 Commit 仅压制同 SHA、同来源的首个 Pipeline；原任务已由撤回 Worker 更新。
    用户后续重跑同一 SHA 会产生新的 Pipeline ID，必须恢复通知。
    用户手动 commit、UT Agent 未在修、格式 bot commit 等一律照发。
    取不到 commit 等异常时宁可照发，不误压。

    注意：修复锁的 workspace_key 用项目路径（path_with_namespace，如 eabot/cook）
    计算 sha256，与 UT Agent 侧 git_provider.id_project 一致。若错用数字 project_id，
    sha256 不同会永远查不到锁。因此查锁优先用 project_path，缺失时才退回数字 id。
    """
    try:
        if not pipeline_sha:
            return False
        resp = _gitlab_api_get(f"/api/v4/projects/{project_id}/repository/commits/{pipeline_sha}")
        if resp is None or not getattr(resp, "ok", False):
            return False
        commit = resp.json() or {}
        message = str(commit.get("message") or "")
        author_email = str(commit.get("author_email") or "")
        ownership = _pipeline_commit_ownership(commit)
        if ownership is not None:
            suppress = _is_initial_rollback_pipeline(
                pipeline_sha,
                project_id,
                pipeline_id,
                pipeline_source,
            )
            get_logger().info(
                "[pipeline_failure_notification] ROLLBACK_PIPELINE: "
                f"decision={'suppress_initial' if suppress else 'notify_rerun_or_unknown'} "
                f"repair_task_id={ownership.repair_task_id} rollback_task_id={ownership.rollback_task_id} "
                f"pipeline_id={pipeline_id or 0} pipeline_source={pipeline_source or 'unknown'} "
                f"pipeline_sha={pipeline_sha[:12]} mr_iid={mr_iid}"
            )
            return suppress
        is_ut_agent_commit = message.startswith(_UT_AGENT_COMMIT_PREFIX) or author_email == _UT_AGENT_AUTHOR_EMAIL
        if not is_ut_agent_commit:
            return False
        # 锁 key 必须与 UT Agent 侧一致：优先用项目路径，退回数字 id
        lock_project_id = project_path or str(project_id)
        from pr_agent.distributed.models import MrKey
        from pr_agent.distributed.runtime import get_execution_runtime

        runtime = get_execution_runtime()
        if runtime is not None and runtime.mode == "queue":
            return runtime.sync_broker.is_mr_triage_active_sync(
                MrKey(project_id=lock_project_id, iid=mr_iid)
            )
        # 延迟 import，避免 webhook 模块加载期与 ut_agent 形成循环依赖
        from ut_agent.agent import is_mr_being_fixed
        return bool(is_mr_being_fixed(lock_project_id, mr_iid))
    except Exception as e:
        get_logger().warning(f"[pipeline_failure_notification] suppress check failed, default to send: {e}")
        return False


def _get_pipeline_failure_keywords(kind: str, default: list[str]) -> list[str]:
    values = get_settings().get(f"FEISHU.PIPELINE_{kind.upper()}_JOB_KEYWORDS", default) or default
    return [str(v).lower() for v in values if str(v).strip()]


def _gitlab_api_get(path: str, *, params: dict | None = None, timeout: float = 15):
    try:
        base_url = get_settings().get("GITLAB.URL", "").rstrip("/")
        token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", "")
        headers = {"PRIVATE-TOKEN": token} if token else {}
        resp = _requests.get(
            f"{base_url}{path}",
            headers=headers,
            timeout=timeout,
            params=params,
        )
        return resp
    except Exception as e:
        get_logger().warning(f"GitLab API request failed for path={path}: {e}")
        return None


def _collect_failed_pipeline_jobs_recursive(
    project_id: int,
    pipeline_id: int,
    visited: set[tuple[int, int]] | None = None,
    depth: int = 0,
) -> list[dict]:
    if visited is None:
        visited = set()
    pipeline_key = (project_id, pipeline_id)
    if pipeline_key in visited or depth > 4:
        return []

    visited.add(pipeline_key)
    failed_jobs = []

    jobs_resp = _gitlab_api_get(
        f"/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs",
        params={"per_page": 100, "scope[]": "failed"},
    )
    if jobs_resp is None:
        return failed_jobs
    if not jobs_resp.ok:
        get_logger().warning(
            f"Failed to list pipeline jobs for project={project_id}, pipeline={pipeline_id}: {jobs_resp.status_code}"
        )
    else:
        jobs_data = jobs_resp.json()
        if isinstance(jobs_data, list):
            failed_jobs.extend(jobs_data)

    bridges_resp = _gitlab_api_get(
        f"/api/v4/projects/{project_id}/pipelines/{pipeline_id}/bridges",
        params={"per_page": 100},
    )
    if bridges_resp is None:
        return failed_jobs
    if not bridges_resp.ok:
        get_logger().warning(
            f"Failed to list pipeline bridges for project={project_id}, pipeline={pipeline_id}: {bridges_resp.status_code}"
        )
        return failed_jobs

    bridges_data = bridges_resp.json()
    if not isinstance(bridges_data, list):
        return failed_jobs

    for bridge in bridges_data:
        downstream = (bridge or {}).get("downstream_pipeline") or {}
        downstream_id = downstream.get("id")
        if not downstream_id:
            continue
        downstream_project_id = int(downstream.get("project_id") or project_id)
        failed_jobs.extend(
            _collect_failed_pipeline_jobs_recursive(
                downstream_project_id,
                int(downstream_id),
                visited=visited,
                depth=depth + 1,
            )
        )

    return failed_jobs


def _get_failed_pipeline_jobs(project_id: int, pipeline_id: int) -> list[dict]:
    try:
        return _collect_failed_pipeline_jobs_recursive(project_id, pipeline_id)
    except Exception as e:
        get_logger().warning(f"Failed to fetch failed pipeline jobs recursively: {e}")
        return []


def _annotate_format_job_dispositions(project_id: int, failed_jobs: list[dict]) -> list[dict]:
    """Attach deterministic Format Job preflight results without mutating API payloads."""
    from pr_agent.distributed.models import RepairCategory

    annotated = [dict(job or {}) for job in failed_jobs]
    for job in annotated:
        if categorize_failed_job(job) is not RepairCategory.FORMAT:
            continue
        job_id = int(job.get("id") or 0)
        if not job_id:
            continue
        response = _gitlab_api_get(f"/api/v4/projects/{project_id}/jobs/{job_id}/trace")
        if response is None or not response.ok:
            status_code = getattr(response, "status_code", "unavailable")
            get_logger().warning(
                f"Format Job preflight trace unavailable: job_id={job_id}, status={status_code}"
            )
            continue
        disposition = classify_format_job_trace(
            str(getattr(response, "text", "") or ""),
            job_url=str(job.get("web_url") or ""),
        )
        job["format_job_disposition"] = disposition.to_dict()
        if disposition.kind == FORMAT_CI_JOB_CONFIGURATION:
            job["auto_repair_eligible"] = False
        get_logger().info(f"Format Job preflight: job_id={job_id}, disposition={disposition.kind}")
    return annotated


def _classify_pipeline_failures(failed_jobs: list[dict]) -> tuple[list[str], list[dict]]:
    return [category.value for category in classify_failed_jobs(failed_jobs)], failed_jobs


def _build_pipeline_failure_card(
    *,
    project_id: str,
    mr_iid: int,
    mr_title: str,
    mr_author_username: str = "",
    mr_url: str,
    source_branch: str,
    pipeline_id: int,
    pipeline_sha: str,
    categories: list[str],
    failed_jobs: list[dict],
):
    from pr_agent.distributed.models import TriageCardBinding
    from pr_agent.feishu.triage_card import build_card_id, parse_mr_identity

    identity = parse_mr_identity(mr_url)
    resolved_project_id = project_id or identity.project_id
    resolved_mr_iid = mr_iid or identity.mr_iid
    if resolved_project_id != identity.project_id or resolved_mr_iid != identity.mr_iid:
        raise ValueError("pipeline failure card MR identity mismatch")
    reason_lines = []
    header_title = f"【{resolved_project_id} !{resolved_mr_iid}】流水线失败"

    blocked_format_jobs = [
        job
        for job in failed_jobs
        if categorize_failed_job(job).value == "format" and (job or {}).get("auto_repair_eligible") is False
    ]
    repairable_format_jobs = [
        job
        for job in failed_jobs
        if categorize_failed_job(job).value == "format" and (job or {}).get("auto_repair_eligible") is not False
    ]
    if repairable_format_jobs:
        reason_lines.append("- 检测到 **format** 相关流水线失败，可自动修复代码格式。")
    for job in blocked_format_jobs:
        disposition = (job or {}).get("format_job_disposition") or {}
        summary = str(disposition.get("summary") or "Format Job 自身执行失败，格式检查尚未开始。")
        job_url = str(disposition.get("job_url") or (job or {}).get("web_url") or "")
        suffix = f" [查看 Job 日志]({job_url})" if job_url else ""
        reason_lines.append(f"- **Format Job 自身执行失败：** {summary}{suffix}")
    if "clang" in categories:
        reason_lines.append("- 检测到 **clang** 相关流水线失败，可尝试自动修复静态分析问题。")
    if "build" in categories:
        reason_lines.append("- 检测到 **build** 相关流水线失败，可尝试自动修复编译错误。")
    if "unknown" in categories:
        reason_lines.append("- 检测到未归类的流水线失败，可尝试自动诊断修复。")

    if failed_jobs:
        preview_jobs = ", ".join(str((job or {}).get("name") or "") for job in failed_jobs[:5])
        reason_lines.append(f"- 失败 jobs: {preview_jobs}")

    markdown_content = (
        f"**MR:** [{mr_title}]({identity.mr_url})\n"
        f"**分支:** `{source_branch or '未知'}`\n"
        f"**Pipeline:** `{pipeline_id}`\n"
        f"**Commit:** `{pipeline_sha[:12] or '未知'}`\n"
        "**流水线结果:** 失败\n"
        + "\n".join(reason_lines)
    )
    card_id = build_card_id(resolved_project_id, resolved_mr_iid, pipeline_id)
    from pr_agent.triage.failure_categories import repair_items_for_failed_jobs
    from pr_agent.triage.repair_card_mode import RepairCardMode, repair_card_mode

    card_mode = repair_card_mode()
    if card_mode is RepairCardMode.UNIFIED:
        repair_items = (pipeline_repair_item(pipeline_id, pipeline_sha),)
    elif card_mode is RepairCardMode.MULTI_SELECT:
        repair_items = repair_items_for_failed_jobs(failed_jobs, pipeline_id, pipeline_sha)
    else:
        repair_items = repair_items_for_failed_jobs(failed_jobs, pipeline_id, pipeline_sha)
    correlated_actions = [
        {
            "command": item.command.lstrip("/"),
            "label": item.label,
            "type": item.button_type,
            "category": item.category.value,
            "mr_url": identity.mr_url,
            "card_id": card_id,
            "pipeline_id": pipeline_id,
            "pipeline_sha": pipeline_sha,
            "revision": 0,
        }
        for item in repair_items
    ]
    binding = TriageCardBinding.new(
        card_id=card_id,
        task_id="",
        open_message_id="",
        receive_id="",
        mr_url=identity.mr_url,
        project_id=resolved_project_id,
        mr_iid=resolved_mr_iid,
        mr_title=mr_title,
        source_branch=source_branch,
        pipeline_id=pipeline_id,
        pipeline_sha=pipeline_sha,
        original_markdown=markdown_content,
        repair_items=repair_items,
        failed_job_names=tuple(str((job or {}).get("name") or "") for job in failed_jobs),
        repair_card_mode=card_mode.value,
        mr_author_username=mr_author_username,
    )
    return markdown_content, correlated_actions, header_title, binding


async def _notify_feishu_pipeline_failure(request_json: dict) -> None:
    """On GitLab pipeline failure, push a targeted Feishu card to the MR author."""
    try:
        from pr_agent.distributed.runtime import get_execution_runtime

        execution_runtime = get_execution_runtime()
        queue_mode = execution_runtime is not None and execution_runtime.mode == "queue"
        notify_enabled = bool(get_settings().get("FEISHU.NOTIFY_ON_PIPELINE_FAILURE", True))
        get_logger().info(f"[pipeline_failure_notification] START: notify_enabled={notify_enabled}")

        pipeline_source = str((request_json.get("object_attributes", {}) or {}).get("source") or "")
        if pipeline_source == "parent_pipeline":
            get_logger().info("[pipeline_failure_notification] SKIP: Downstream pipeline is covered by its parent")
            return

        project_id, pipeline_id, ref, status = _get_pipeline_payload_parts(request_json)
        get_logger().info(
            f"[pipeline_failure_notification] Parsed pipeline: project_id={project_id}, "
            f"pipeline_id={pipeline_id}, ref={ref}, status={status}"
        )
        if not project_id or not pipeline_id or not ref or status.lower() not in {"failed", "success"}:
            get_logger().info("[pipeline_failure_notification] SKIP: Invalid payload or unsupported status")
            return

        base_url = get_settings().get("GITLAB.URL", "").rstrip("/")
        token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", "")
        headers = {"PRIVATE-TOKEN": token} if token else {}
        get_logger().info(f"[pipeline_failure_notification] Querying MRs: base_url={base_url}, ref={ref}")
        mr_resp = _requests.get(
            f"{base_url}/api/v4/projects/{project_id}/merge_requests",
            headers=headers,
            timeout=15,
            params={"source_branch": ref, "state": "opened", "per_page": 20},
        )
        if not mr_resp.ok:
            get_logger().warning(
                f"[pipeline_failure_notification] SKIP: Failed to resolve MRs for failed pipeline project={project_id}, ref={ref}: {mr_resp.status_code}"
            )
            return
        merge_requests = mr_resp.json() if isinstance(mr_resp.json(), list) else []
        if not merge_requests:
            get_logger().info(f"[pipeline_failure_notification] SKIP: No opened MR found for failed pipeline project={project_id}, ref={ref}")
            return

        get_logger().info(f"[pipeline_failure_notification] Found {len(merge_requests)} opened MR(s), resolving author Feishu users...")
        client = None
        if not queue_mode:
            from pr_agent.feishu.feishu_client import FeishuClient

            client = FeishuClient()
        pipeline_sha = _get_pipeline_sha(request_json)
        failed_jobs = None
        categories = None
        # 项目路径（eabot/cook），与 UT Agent 侧修复锁 key 一致；payload.project 自带
        project_path = str((request_json.get("project", {}) or {}).get("path_with_namespace") or "")
        for mr in merge_requests:
            mr_url = str(mr.get("web_url") or mr.get("url") or "")
            if not mr_url:
                continue
            mr_iid = int(mr.get("iid") or 0)
            # UT Agent 正在修复该 MR 且本次失败由其自身 commit 触发时，压制卡片
            title = str(mr.get("title") or "")
            if (
                status.lower() == "failed"
                and mr_iid
                and _should_suppress_pipeline_card(
                    pipeline_sha,
                    project_id,
                    mr_iid,
                    project_path,
                    pipeline_id=int(pipeline_id),
                    pipeline_source=pipeline_source,
                )
            ):
                get_logger().info(
                    f"[pipeline_failure_notification] SUPPRESS: Pipeline for MR !{mr_iid} is already covered "
                    f"by its repair task, skip standalone notification for sha={pipeline_sha[:8]}"
                )
                continue
            freshness = check_pipeline_freshness(
                api_get=_gitlab_api_get,
                project_id=project_id,
                mr_iid=mr_iid,
                pipeline_id=int(pipeline_id),
                pipeline_sha=pipeline_sha,
                ref=ref,
            )
            if not freshness.current:
                get_logger().info(
                    "[pipeline_failure_notification] SUPPRESS_STALE: "
                    f"project={project_path or project_id} mr=!{mr_iid} event_pipeline={pipeline_id} "
                    f"event_sha={pipeline_sha[:12]} head_sha={freshness.head_sha[:12]} "
                    f"latest_pipeline={freshness.latest_pipeline_id} "
                    f"latest_status={freshness.latest_pipeline_status} reason={freshness.reason}"
                )
                continue
            _record_ci_followup(int(project_id), mr_iid, int(pipeline_id), pipeline_sha, status.lower())
            if status.lower() == "success":
                continue
            if failed_jobs is None or categories is None:
                failed_jobs = _get_failed_pipeline_jobs(int(project_id), int(pipeline_id))
                failed_jobs = _annotate_format_job_dispositions(int(project_id), failed_jobs)
                categories, failed_jobs = _classify_pipeline_failures(failed_jobs)
                get_logger().info(
                    f"[pipeline_failure_notification] Classified failures: categories={categories}, "
                    f"failed_jobs_count={len(failed_jobs)}"
                )
            author = mr.get("author") or {}
            gitlab_username = str(author.get("username") or "")
            email = str(author.get("email") or "")
            get_logger().info(f"[pipeline_failure_notification] Resolving MR owner: gitlab_username={gitlab_username}, email={email}")
            markdown_content, actions, header_title, binding = _build_pipeline_failure_card(
                project_id=project_path,
                mr_iid=mr_iid,
                mr_title=title,
                mr_author_username=gitlab_username,
                mr_url=mr_url,
                source_branch=ref,
                pipeline_id=int(pipeline_id),
                pipeline_sha=pipeline_sha,
                categories=categories,
                failed_jobs=failed_jobs,
            )
            _capture_ci_failure(
                request_json=request_json,
                mr=mr,
                project_id=int(project_id),
                project_path=project_path,
                pipeline_id=int(pipeline_id),
                pipeline_sha=pipeline_sha,
                failed_jobs=failed_jobs,
                card_id=binding.card_id,
            )
            if not notify_enabled:
                get_logger().info("[pipeline_failure_notification] SKIP: FEISHU.NOTIFY_ON_PIPELINE_FAILURE is False")
                continue
            app_id = _read_feishu_setting("APP_ID", "FEISHU_APP_ID")
            if not app_id and not queue_mode:
                get_logger().info("[pipeline_failure_notification] SKIP: Feishu APP_ID not configured")
                continue
            from pr_agent.distributed.notifications import (
                multi_action_repair_cards_enabled,
                triage_card_ttl_seconds,
                triage_card_updates_enabled,
            )

            update_cards = queue_mode and triage_card_updates_enabled() and multi_action_repair_cards_enabled()
            delivery_actions = actions
            if not update_cards:
                delivery_actions = [
                    {key: value for key, value in action.items() if key not in {"card_id", "pipeline_id"}}
                    for action in actions
                ]
            if queue_mode:
                if not gitlab_username and not email:
                    get_logger().warning("Skip pipeline failure card: MR author identity is missing")
                    try:
                        from pr_agent.triage.ci_failure_store import update_notification_state

                        update_notification_state(binding.card_id, "recipient_missing", "identity_missing")
                    except Exception:
                        pass
                    continue
                from pr_agent.distributed.models import NotificationEnvelope
                from pr_agent.distributed.notifications import action_card_content
                from pr_agent.feishu.triage_card import render_triage_card
                from pr_agent.triage.repair_card_mode import RepairCardMode

                card_id = ""
                if update_cards:
                    ttl_seconds = triage_card_ttl_seconds()
                    created_card = await execution_runtime.broker.save_triage_card(binding, ttl_seconds=ttl_seconds)
                    if not created_card:
                        get_logger().info(
                            f"[pipeline_failure_notification] SKIP: card already exists card_id={binding.card_id}"
                        )
                        continue
                    card_id = binding.card_id
                notification_kind = "action_card"
                notification_content = action_card_content(markdown_content, delivery_actions)
                if binding.repair_card_mode == RepairCardMode.MULTI_SELECT.value and update_cards:
                    notification_kind = "interactive_card"
                    notification_content = json.dumps(
                        render_triage_card(binding, binding.state, binding.status_markdown),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                queued = await execution_runtime.broker.enqueue_notification(
                    NotificationEnvelope.new(
                        task_id=execution_runtime.task_id,
                        receive_id="",
                        recipient_email=email,
                        recipient_username=gitlab_username,
                        kind=notification_kind,
                        content=notification_content,
                        title=header_title,
                        header_template="blue",
                        mr_url=binding.mr_url,
                        notification_id=f"pipeline-failure-{binding.card_id}",
                        card_id=card_id,
                    )
                )
                try:
                    from pr_agent.triage.ci_failure_store import update_notification_state

                    update_notification_state(
                        binding.card_id,
                        "queued" if queued is not False else "failed",
                        "" if queued is not False else "enqueue_failed",
                    )
                except Exception:
                    pass
                get_logger().info(
                    f"[pipeline_failure_notification] QUEUED: Feishu card for {gitlab_username}, {mr_url}"
                )
                continue
            open_id = await client.resolve_open_id_for_gitlab_user(gitlab_username, email)
            if not open_id:
                get_logger().info(f"[pipeline_failure_notification] SKIP: No Feishu user resolved for GitLab user '{gitlab_username}'")
                try:
                    from pr_agent.triage.ci_failure_store import update_notification_state

                    update_notification_state(binding.card_id, "recipient_missing", "recipient_not_found")
                except Exception:
                    pass
                continue

            get_logger().info(f"[pipeline_failure_notification] Found Feishu user/open_id: {open_id}, sending card...")
            if binding.repair_card_mode == "multi_select":
                from pr_agent.distributed.models import NotificationEnvelope
                from pr_agent.feishu.triage_card import render_triage_card

                await client.send_notification(
                    NotificationEnvelope.new(
                        task_id="",
                        receive_id=open_id,
                        recipient_email="",
                        recipient_username="",
                        kind="interactive_card",
                        content=json.dumps(
                            render_triage_card(binding, binding.state, binding.status_markdown),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        title=header_title,
                        header_template="blue",
                        mr_url=binding.mr_url,
                    )
                )
            else:
                await client.send_action_card(
                    open_id,
                    binding.mr_url,
                    markdown_content,
                    title=header_title,
                    actions=delivery_actions,
                )
            try:
                from pr_agent.triage.ci_failure_store import update_notification_state

                update_notification_state(binding.card_id, "delivered")
            except Exception:
                pass
            get_logger().info(
                f"[pipeline_failure_notification] SUCCESS: Pushed Feishu pipeline failure card to {gitlab_username} ({open_id}) for {mr_url}"
            )
    except Exception as e:
        import traceback
        get_logger().error(f"[pipeline_failure_notification] ERROR: {e}\n{traceback.format_exc()}")


def _handle_feedback_gate_push(request_json: dict) -> None:
    """On MR push (action=update), re-stamp the feedback commit status.

    Does NOT re-run review. If the MR already has feedback, mark the new head
    commit success; otherwise keep it pending. No-op when the gate is disabled.
    Never raises.
    """
    try:
        if not gate.is_enabled():
            return
        mr_url = _extract_mr_url(request_json)
        if not mr_url:
            get_logger().warning("feedback-gate push: missing mr_url in payload; skipping.")
            return
        git_provider = get_git_provider_with_context(mr_url)
        project = git_provider.id_project
        mr_iid = git_provider.id_mr
        if project is None or mr_iid is None:
            get_logger().warning("feedback-gate push: provider returned None id_project or id_mr; skipping.")
            return
        gate.restamp_on_push(git_provider, project, mr_iid)
    except Exception as e:
        get_logger().warning(f"feedback-gate push handling failed: {e}")


def _handle_title_issue_link_refresh(request_json: dict) -> None:
    """On MR title edit, patch just the "需求链接" line in the already-published
    description to the newly-extracted Feishu issue ID -- WITHOUT touching any
    other part of the description and WITHOUT re-running /describe or calling
    any LLM.

    Only replaces the ID when the current value in the description is either
    the placeholder or the OLD title's extracted ID (i.e. it still looks
    auto-generated, not hand-edited). Never raises.
    """
    try:
        changes = request_json.get("changes") or {}
        if "title" not in changes:
            return

        from pr_agent.tools.pr_description import PRDescription

        old_title = (changes.get("title") or {}).get("previous") or ""
        new_title = (changes.get("title") or {}).get("current") or ""
        old_id = PRDescription._extract_issue_id_from_title(old_title)
        new_id = PRDescription._extract_issue_id_from_title(new_title)
        if old_id == new_id:
            return

        mr_url = _extract_mr_url(request_json)
        if not mr_url:
            get_logger().warning("title issue-link refresh: missing mr_url in payload; skipping.")
            return
        git_provider = get_git_provider_with_context(mr_url)
        description = git_provider.mr.description or ""

        placeholder = "[在此填入问题ID]"
        issue_line_re = re.compile(
            r"### 需求链接：https://project\.feishu\.cn/eabot/issue/detail/(.+)"
        )
        match = issue_line_re.search(description)
        if not match:
            get_logger().debug("title issue-link refresh: no 需求链接 line found in description; skipping.")
            return

        current_value = match.group(1).strip()
        safe_to_replace = current_value in (placeholder, old_id)
        if not safe_to_replace:
            get_logger().debug(
                f"title issue-link refresh: current value '{current_value}' does not match "
                f"placeholder or old id '{old_id}' -- looks hand-edited, skipping."
            )
            return

        new_value = new_id if new_id else placeholder
        updated_description = (
            description[:match.start(1)] + new_value + description[match.end(1):]
        )
        git_provider.mr.description = updated_description
        git_provider.mr.save()
        get_logger().info(
            f"title issue-link refresh: updated 需求链接 from '{current_value}' to '{new_value}' "
            f"following title change."
        )
    except Exception as e:
        get_logger().warning(f"title issue-link refresh failed: {e}")




def _make_fetch_applied_fn(project_id: int):
    """Return a function that queries GitLab for applied suggestion discussion IDs."""
    import requests as _requests

    def _applied_ids_for_mr(base_url, headers, pid, mr_iid) -> list:
        ids = []
        disc_resp = _requests.get(
            f"{base_url}/api/v4/projects/{pid}/merge_requests/{mr_iid}/discussions",
            headers=headers, timeout=10, params={"per_page": 100},
        )
        if not disc_resp.ok:
            return ids
        for disc in disc_resp.json():
            for note in disc.get("notes") or []:
                for sg in note.get("suggestions") or []:
                    if sg.get("applied"):
                        ids.append(disc["id"])
        return ids

    def fetch_applied(pid: int, commit_sha: str, ref: str = "") -> list:
        try:
            token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", "")
            base_url = get_settings().get("GITLAB.URL", "").rstrip("/")
            headers = {"PRIVATE-TOKEN": token}
            mr_iids = []

            # Primary: resolve the MR from the push branch (immediately consistent,
            # unlike commits/{sha}/merge_requests which lags right after the push).
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            if branch:
                br_resp = _requests.get(
                    f"{base_url}/api/v4/projects/{pid}/merge_requests",
                    headers=headers, timeout=10,
                    params={"source_branch": branch, "state": "all", "per_page": 20},
                )
                if br_resp.ok and isinstance(br_resp.json(), list):
                    mr_iids = [mr.get("iid") for mr in br_resp.json() if mr.get("iid")]

            # Fallback: MRs associated with the commit sha.
            if not mr_iids:
                mrs_resp = _requests.get(
                    f"{base_url}/api/v4/projects/{pid}/repository/commits/{commit_sha}/merge_requests",
                    headers=headers, timeout=10,
                )
                mrs = mrs_resp.json() if mrs_resp.ok else []
                if isinstance(mrs, list):
                    mr_iids = [mr.get("iid") for mr in mrs if mr.get("iid")]

            applied_ids = []
            for mr_iid in mr_iids:
                applied_ids.extend(_applied_ids_for_mr(base_url, headers, pid, mr_iid))
            return applied_ids
        except Exception as exc:
            get_logger().warning(f"_make_fetch_applied_fn: {exc}")
            return []

    return fetch_applied


def _handle_inline_apply_push(request_json: dict) -> None:
    """On any push, detect applied inline suggestions and record them. Never raises."""
    try:
        project_id = (request_json.get("project") or {}).get("id")
        if not project_id:
            return
        fetch_fn = _make_fetch_applied_fn(project_id)
        # Retry to absorb GitLab's eventual consistency right after an apply push.
        handle_push_event(
            request_json,
            fetch_applied_fn=fetch_fn,
            max_attempts=5,
            retry_delay=2.0,
        )
        _sync_inline_gate_for_push(request_json, project_id)
    except Exception as e:
        get_logger().warning(f"_handle_inline_apply_push failed: {e}")


def _fetch_mr_discussions(project, mr_iid) -> list:
    """Return the raw GitLab Discussions API list for project/mr_iid.

    `project` may be a numeric id or a URL-encoded-able path (matches
    git_provider.id_project). Never raises; returns [] on failure.
    """
    from urllib.parse import quote
    try:
        token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", "")
        base_url = get_settings().get("GITLAB.URL", "").rstrip("/")
        headers = {"PRIVATE-TOKEN": token}
        encoded = quote(str(project), safe="")
        resp = _requests.get(
            f"{base_url}/api/v4/projects/{encoded}/merge_requests/{mr_iid}/discussions",
            headers=headers, timeout=10, params={"per_page": 100},
        )
        if not resp.ok:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        get_logger().warning(f"_fetch_mr_discussions: {exc}")
        return []


def _resolve_mr_iids_for_push(project_id, ref: str) -> list:
    """Resolve which MR(s) a push's source branch belongs to. Never raises."""
    try:
        token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", "")
        base_url = get_settings().get("GITLAB.URL", "").rstrip("/")
        headers = {"PRIVATE-TOKEN": token}
        branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        if not branch:
            return []
        resp = _requests.get(
            f"{base_url}/api/v4/projects/{project_id}/merge_requests",
            headers=headers, timeout=10,
            params={"source_branch": branch, "state": "all", "per_page": 20},
        )
        if resp.ok and isinstance(resp.json(), list):
            return [mr.get("iid") for mr in resp.json() if mr.get("iid")]
    except Exception as exc:
        get_logger().warning(f"_resolve_mr_iids_for_push: {exc}")
    return []


def _sync_inline_gate_for_push(request_json: dict, project_id) -> None:
    """On any push, sync the inline-suggestion gate for every MR whose
    source branch matches this push. Never raises."""
    try:
        ref = request_json.get("ref") or ""
        mr_iids = _resolve_mr_iids_for_push(project_id, ref)
        if not mr_iids:
            return
        project_path = (request_json.get("project") or {}).get("path_with_namespace") or ""
        gitlab_url = get_settings().get("GITLAB.URL", "").rstrip("/")
        if not project_path or not gitlab_url:
            return
        if not inline_gate_status.is_enabled(project_path):
            return
        for mr_iid in mr_iids:
            try:
                mr_url = f"{gitlab_url}/{project_path}/-/merge_requests/{mr_iid}"
                git_provider = get_git_provider_with_context(mr_url)
                inline_thread_sync.sync_mr_threads(
                    git_provider, git_provider.id_project, git_provider.id_mr, _fetch_mr_discussions,
                )
            except Exception as e:
                get_logger().warning(f"inline-gate push sync failed for mr_iid={mr_iid}: {e}")
    except Exception as e:
        get_logger().warning(f"_sync_inline_gate_for_push failed: {e}")


def _handle_inline_gate_push(request_json: dict) -> None:
    """On merge_request action=update, sync the inline-suggestion gate.

    Covers both "author pushed new code" and "GitLab's own 'all threads
    resolved' aggregate webhook" cases. Never raises.
    """
    try:
        mr_url = _extract_mr_url(request_json)
        if not mr_url:
            get_logger().warning("inline-gate push: missing mr_url in payload; skipping.")
            return
        git_provider = get_git_provider_with_context(mr_url)
        project = git_provider.id_project
        mr_iid = git_provider.id_mr
        if project is None or mr_iid is None:
            get_logger().warning("inline-gate push: provider returned None id_project or id_mr; skipping.")
            return
        if not inline_gate_status.is_enabled(project):
            return
        inline_thread_sync.sync_mr_threads(git_provider, project, mr_iid, _fetch_mr_discussions)
    except Exception as e:
        get_logger().warning(f"inline-gate push handling failed: {e}")


def _handle_inline_gate_note(request_json: dict) -> None:
    """On any MR note (comment), sync the inline-suggestion gate — this note
    might be unrelated, or it might be the trigger that lets us discover a
    "Resolve thread" that GitLab didn't emit its own webhook for. Never raises.
    """
    try:
        mr_url = _extract_mr_url(request_json)
        if not mr_url:
            return
        git_provider = get_git_provider_with_context(mr_url)
        project = git_provider.id_project
        mr_iid = git_provider.id_mr
        if project is None or mr_iid is None:
            return
        if not inline_gate_status.is_enabled(project):
            return
        inline_thread_sync.sync_mr_threads(git_provider, project, mr_iid, _fetch_mr_discussions)
    except Exception as e:
        get_logger().warning(f"inline-gate note handling failed: {e}")

@router.post("/webhook")
async def gitlab_webhook(background_tasks: BackgroundTasks, request: Request):
    start_time = datetime.now()
    log_context = {"server_type": "gitlab_app"}
    request_json = None
    try:
        request_json = await request.json()
    except Exception as e:
        get_logger().error(f"Failed to parse JSON body: {e}")
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"message": "Invalid JSON"})

    # Check if this is a Feishu request
    # 1. Feishu URL Verification
    if request_json.get("type") == "url_verification" and "challenge" in request_json:
        get_logger().info("Received Feishu URL verification challenge")
        return JSONResponse(content={"challenge": request_json.get("challenge")})

    # 2. Feishu Event Callback
    if request_json.get("header", {}).get("event_type") == "im.message.receive_v1":
        get_logger().info("Received Feishu message event")
        # Delegate to feishu_webhook handler logic
        from pr_agent.feishu.feishu_webhook import handle_feishu_webhook
        return await handle_feishu_webhook(request)

    get_logger().debug(request_json)
    context["settings"] = copy.deepcopy(global_settings)
    context["git_provider"] = {}

    async def validate_secret():
        """Validate webhook secret based on configuration."""
        get_logger().debug("Received a GitLab webhook")
        request_token = _normalize_secret(request.headers.get("X-Gitlab-Token", ""))
        configured_shared_secret = _normalize_secret(get_settings().get("GITLAB.SHARED_SECRET", ""))
        validation_mode = str(get_settings().get("GITLAB.WEBHOOK_SECRET_VALIDATION_MODE", "strict")).strip().lower()
        allow_on_validation_failure = validation_mode in {"optional", "allow", "permissive"}

        if validation_mode in {"off", "disabled", "none"}:
            get_logger().warning("GitLab webhook secret validation is disabled by configuration")
            return True, None

        validation_error = None

        if secret_provider:
            if not request_token:
                validation_error = "missing X-Gitlab-Token header"
            else:
                secret_value = secret_provider.get_secret(request_token)
                if not secret_value:
                    validation_error = "token did not resolve in secret provider"
                else:
                    comparable_secret = _extract_comparable_secret(secret_value)
                    comparable_secret = _normalize_secret(comparable_secret)
                    if comparable_secret and not hmac.compare_digest(comparable_secret, request_token):
                        validation_error = "mismatched secret value"
        elif configured_shared_secret:
            if not request_token:
                validation_error = "missing X-Gitlab-Token header"
            elif not hmac.compare_digest(configured_shared_secret, request_token):
                validation_error = "invalid shared secret"

        if validation_error:
            if allow_on_validation_failure:
                get_logger().warning(f"Webhook secret validation failed but allowed by mode '{validation_mode}': {validation_error}")
                return True, None
            else:
                get_logger().error(f"Failed to validate secret: {validation_error}")
                return False, JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"message": "Unauthorized"})

        return True, None

    object_kind = str(request_json.get("object_kind") or "")
    if object_kind in {"push", "pipeline", "merge_request", "note"}:
        distributed_settings = load_distributed_settings()
        project_path = extract_project_path(request_json)
        if distributed_settings.should_queue(project_path):
            is_valid, error_response = await validate_secret()
            if not is_valid:
                return error_response
            if object_kind == "merge_request":
                background_tasks.add_task(capture_webhook_mr, request_json)
            try:
                result = await _get_queue_ingress().enqueue_gitlab_event(request_json, request.headers)
            except (redis.ConnectionError, redis.TimeoutError):
                get_logger().exception("Redis queue unavailable while accepting GitLab webhook")
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    headers={"Retry-After": "5"},
                    content={"message": "Queue unavailable; retry this webhook"},
                )
            message = (
                "Recovered and queued"
                if result.recovered
                else "Queued"
                if result.created
                else "Duplicate event ignored"
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"message": message, "task_id": result.task_id},
            )

    # push events: detect applied inline suggestions
    if request_json.get("object_kind") == "push":
        is_valid, error_response = await validate_secret()
        if not is_valid:
            return error_response
        background_tasks.add_task(_handle_inline_apply_push, request_json)
        return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "Received"})

    if request_json.get("object_kind") == "pipeline":
        is_valid, error_response = await validate_secret()
        if not is_valid:
            return error_response

        dedup_key = _build_gitlab_event_dedup_key(request_json, request)
        if _is_duplicate_gitlab_event(dedup_key):
            get_logger().info(f"Skipping duplicate GitLab pipeline webhook event: {dedup_key}")
            return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "Duplicate event ignored"})

        background_tasks.add_task(_notify_feishu_pipeline_failure, request_json)
        return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "Received"})

    if request_json.get("object_kind") in ("merge_request", "note"):
        is_valid, error_response = await validate_secret()
        if not is_valid:
            return error_response

        if request_json.get("object_kind") == "merge_request":
            background_tasks.add_task(capture_webhook_mr, request_json)

        dedup_key = _build_gitlab_event_dedup_key(request_json, request)
        if _is_duplicate_gitlab_event(dedup_key):
            get_logger().info(f"Skipping duplicate GitLab webhook event: {dedup_key}")
            return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "Duplicate event ignored"})

        object_kind = request_json.get("object_kind", "")
        if object_kind == "merge_request":
            action = request_json.get("object_attributes", {}).get("action") or "opened"
            if action in {"open", "opened", "reopened"}:
                mr_url = _extract_mr_url(request_json)
                if mr_url:
                    log_context = {"server_type": "gitlab_app"}
                    background_tasks.add_task(_perform_commands_gitlab, "pr_commands", PRAgent(), mr_url, log_context, request_json)
            elif action == "update":
                background_tasks.add_task(_handle_feedback_gate_push, request_json)
                background_tasks.add_task(_handle_inline_gate_push, request_json)
                background_tasks.add_task(_handle_title_issue_link_refresh, request_json)
            else:
                get_logger().debug(f"Skipping merge_request auto-commands for action '{action}'")
        elif object_kind == "note":
            note_action = request_json.get("object_attributes", {}).get("action")
            if note_action and note_action != "create":
                get_logger().debug(f"Skipping note command processing for action '{note_action}'")
                return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "Note action ignored"})
            # Collect user feedback on suggestion discussions (before command dispatch)
            bot_username = get_settings().get("GITLAB.BOT_USERNAME", "")
            background_tasks.add_task(collect_note_feedback, request_json, bot_username)
            background_tasks.add_task(_handle_inline_gate_note, request_json)
            api_url, body, req_log_context, sender_id = _extract_handle_request_payload(request_json)
            background_tasks.add_task(handle_request, api_url, body, req_log_context, sender_id)

    return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "Received"})


def handle_ask_line(body, data):
    try:
        line_range_ = data['object_attributes']['position']['line_range']
        # if line_range_['start']['type'] == 'new':
        start_line = line_range_['start']['new_line']
        end_line = line_range_['end']['new_line']
        # else:
        #     start_line = line_range_['start']['old_line']
        #     end_line = line_range_['end']['old_line']
        question = body.replace('/ask', '').strip()
        path = data['object_attributes']['position']['new_path']
        side = 'RIGHT'  # if line_range_['start']['type'] == 'new' else 'LEFT'
        comment_id = data['object_attributes']["discussion_id"]
        get_logger().info("Handling line ")
        body = f"/ask_line --line_start={start_line} --line_end={end_line} --side={side} --file_name={path} --comment_id={comment_id} {question}"
    except Exception as e:
        get_logger().error(f"Failed to handle ask line comment: {e}")
    return body


@router.get("/")
async def root():
    return {"status": "ok"}


@router.get("/health/ready")
async def distributed_readiness():
    distributed_settings = load_distributed_settings()
    if distributed_settings.execution_mode != "queue":
        return {"status": "ok", "mode": "inline"}
    service = DistributedHealthService(_get_queue_ingress().broker, distributed_settings)
    return await service.readiness()


@router.get("/health/distributed")
async def distributed_health():
    distributed_settings = load_distributed_settings()
    if distributed_settings.execution_mode != "queue":
        return {"status": "ok", "mode": "inline"}
    service = DistributedHealthService(_get_queue_ingress().broker, distributed_settings)
    return await service.snapshot()


@router.get("/health/feishu")
async def feishu_long_connection_health():
    distributed_settings = load_distributed_settings()
    if distributed_settings.execution_mode == "queue":
        heartbeat = await _get_queue_ingress().broker.get_service_heartbeat("feishu")
        return {
            "status": "ok" if heartbeat["alive"] else "unavailable",
            "mode": "queue",
            "worker": heartbeat,
        }

    status = feishu_long_connection_snapshot()
    thread_alive = bool(_feishu_worker_thread and _feishu_worker_thread.is_alive())
    enabled = bool(get_settings().get("FEISHU.LONG_CONNECTION_ENABLED", True))
    auto_start = bool(get_settings().get("FEISHU.LONG_CONNECTION_AUTO_START", True))

    return {
        "status": "ok",
        "enabled": enabled,
        "auto_start": auto_start,
        "worker_started_flag": _feishu_worker_started,
        "worker_thread_alive": thread_alive,
        "worker": status,
    }

gitlab_url = get_settings().get("GITLAB.URL", None)
if not gitlab_url:
    raise ValueError("GITLAB.URL is not set")
get_settings().config.git_provider = "gitlab"
middleware = [Middleware(RawContextMiddleware)]
app = FastAPI(middleware=middleware)


@app.on_event("startup")
async def _startup_events():
    mark_creation_tracking_started()
    distributed_settings = load_distributed_settings()
    if distributed_settings.execution_mode == "queue":
        await _get_queue_ingress().broker.redis.ping()
    else:
        _start_feishu_long_connection_worker_if_needed()
    start_sync_worker_if_enabled()


@app.on_event("shutdown")
async def _shutdown_events():
    global _queue_ingress, _queue_redis_client

    stop_sync_worker()
    if _queue_redis_client is not None:
        await _queue_redis_client.aclose()
        _queue_redis_client = None
        _queue_ingress = None


app.include_router(router)
app.include_router(feishu_router)
app.include_router(dashboard_router)
configure_repair_results_broker(lambda: _get_queue_ingress().broker)
app.include_router(repair_results_router)


def start():
    uvicorn.run(app, host="0.0.0.0", port=3000)


if __name__ == '__main__':
    start()
