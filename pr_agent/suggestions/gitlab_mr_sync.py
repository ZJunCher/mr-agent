"""Low-cost GitLab MR inventory collection for the suggestion dashboard."""

from __future__ import annotations

import os
import threading
import uuid
from datetime import timedelta
from urllib.parse import urlparse

import requests

from pr_agent.config_loader import get_settings
from pr_agent.feedback.timez import now_cn, to_cn
from pr_agent.log import get_logger
from pr_agent.suggestions.creation_review_recovery import recover_synced_mrs
from pr_agent.suggestions.review_alerts import evaluate_review_alerts
from pr_agent.suggestions.review_tracking import (
    claim_sync_lease,
    complete_sync,
    get_sync_state,
    upsert_mr,
)

SYNC_NAME = "gitlab_mr_inventory"
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def _project_path_from_url(url: str) -> str:
    try:
        path = urlparse(url).path.strip("/")
        return path.split("/-/merge_requests/", 1)[0]
    except Exception:
        return ""


def normalize_api_mr(mr: dict, discovered_by: str = "incremental_sync") -> dict:
    author = mr.get("author") or {}
    full_reference = str((mr.get("references") or {}).get("full") or "")
    project_path = full_reference.rsplit("!", 1)[0] or _project_path_from_url(str(mr.get("web_url") or ""))
    return {
        "project_id": mr.get("project_id") or mr.get("target_project_id") or project_path,
        "project_path": project_path,
        "mr_iid": mr.get("iid"),
        "mr_url": mr.get("web_url"),
        "title": mr.get("title"),
        "author": author.get("username") or author.get("name"),
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
        "state": mr.get("state"),
        "draft": bool(mr.get("draft") or mr.get("work_in_progress")),
        "commit_sha": mr.get("sha") or (mr.get("diff_refs") or {}).get("head_sha"),
        "created_at": mr.get("created_at"),
        "updated_at": mr.get("updated_at"),
        "discovered_by": discovered_by,
    }


def normalize_webhook_mr(payload: dict) -> dict:
    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    user = payload.get("user") or {}
    last_commit = attrs.get("last_commit") or {}
    return {
        "project_id": project.get("id") or payload.get("project_id") or attrs.get("target_project_id"),
        "project_path": (
            project.get("path_with_namespace")
            or project.get("web_url", "").split("//")[-1].split("/", 1)[-1]
        ),
        "mr_iid": attrs.get("iid") or attrs.get("id"),
        "mr_url": attrs.get("url"),
        "title": attrs.get("title"),
        "author": user.get("username") or user.get("name"),
        "source_branch": attrs.get("source_branch"),
        "target_branch": attrs.get("target_branch"),
        "state": attrs.get("state"),
        "draft": bool(attrs.get("draft") or attrs.get("work_in_progress")),
        "commit_sha": last_commit.get("id") or attrs.get("last_commit", {}).get("id"),
        "created_at": attrs.get("created_at"),
        "updated_at": attrs.get("updated_at"),
        "discovered_by": "webhook",
    }


def capture_webhook_mr(payload: dict) -> bool:
    if payload.get("object_kind") != "merge_request":
        return False
    return upsert_mr(normalize_webhook_mr(payload))


def _setting(name: str, default):
    return get_settings().get(f"suggestion_review_dashboard.{name}", default)


def _sync_window(state: dict) -> tuple[str, bool]:
    now = now_cn()
    reconcile_seconds = int(_setting("reconcile_interval_seconds", 86400))
    last_reconcile = to_cn(state.get("last_reconcile_at"))
    reconciliation_due = not last_reconcile or (now - last_reconcile).total_seconds() >= reconcile_seconds
    if not state.get("cursor_at"):
        return (now - timedelta(days=int(_setting("initial_lookback_days", 30)))).isoformat(), True
    if reconciliation_due:
        return (now - timedelta(hours=int(_setting("reconcile_lookback_hours", 48)))).isoformat(), True
    cursor = to_cn(state.get("cursor_at")) or now
    overlap = int(_setting("sync_overlap_seconds", 600))
    return (cursor - timedelta(seconds=overlap)).isoformat(), False


def sync_gitlab_mrs_once(session=requests, *, owner: str | None = None) -> dict:
    """Run one lease-protected global metadata scan. Never raises."""
    owner = owner or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    lease_seconds = int(_setting("sync_lease_seconds", 180))
    if not claim_sync_lease(SYNC_NAME, owner, lease_seconds=lease_seconds):
        return {"status": "busy", "count": 0}

    state = get_sync_state(SYNC_NAME)
    updated_after, reconciled = _sync_window(state)
    next_cursor = now_cn().isoformat()
    count = 0
    synced_records: list[dict] = []
    try:
        base_url = str(get_settings().get("GITLAB.URL", "")).rstrip("/")
        token = str(get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", ""))
        if not base_url or not token:
            raise RuntimeError("GitLab URL or personal access token is not configured")
        auth_method = str(get_settings().get("GITLAB.AUTH_TYPE", "oauth_token") or "oauth_token").lower()
        headers = {"Authorization": f"Bearer {token}"} if auth_method == "oauth_token" else {"PRIVATE-TOKEN": token}
        page = 1
        while page:
            response = session.get(
                f"{base_url}/api/v4/merge_requests",
                headers=headers,
                params={
                    "scope": "all", "state": "all", "updated_after": updated_after,
                    "order_by": "updated_at", "sort": "asc", "per_page": 100, "page": page,
                },
                timeout=15,
                verify=get_settings().get("GITLAB.SSL_VERIFY", True),
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise RuntimeError("GitLab merge requests response is not a list")
            source = "reconcile" if reconciled else "incremental_sync"
            for mr in rows:
                record = normalize_api_mr(mr, discovered_by=source)
                if upsert_mr(record):
                    count += 1
                    synced_records.append(record)
            next_page = str(response.headers.get("X-Next-Page") or "").strip()
            page = int(next_page) if next_page else 0
        try:
            recovery = recover_synced_mrs(synced_records)
        except Exception as exc:
            get_logger().warning(f"GitLab MR creation-review recovery batch failed: {exc}")
            recovery = {"failed": len(synced_records)}
        try:
            alerts = evaluate_review_alerts()
        except Exception as exc:
            get_logger().warning(f"Suggestion review alert evaluation failed: {exc}")
            alerts = []
        complete_sync(SYNC_NAME, owner, cursor_at=next_cursor, reconciled=reconciled)
        return {
            "status": "ok", "count": count, "cursor_at": next_cursor,
            "reconciled": reconciled, "recovery": recovery, "active_alerts": len(alerts),
        }
    except Exception as exc:
        complete_sync(SYNC_NAME, owner, error=str(exc))
        get_logger().warning(f"GitLab MR inventory sync failed: {exc}")
        return {"status": "error", "count": count, "error": str(exc)[:300]}


def _worker_loop() -> None:
    while not _stop_event.is_set():
        sync_gitlab_mrs_once()
        interval = max(60, int(_setting("sync_interval_seconds", 600)))
        _stop_event.wait(interval)


def start_sync_worker_if_enabled() -> bool:
    global _worker_thread
    if not bool(_setting("enabled", True)) or not bool(_setting("sync_enabled", True)):
        return False
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return True
        _stop_event.clear()
        _worker_thread = threading.Thread(target=_worker_loop, name="gitlab-mr-inventory-sync", daemon=True)
        _worker_thread.start()
        return True


def stop_sync_worker() -> None:
    _stop_event.set()
