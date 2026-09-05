"""Bounded, dry-run-first reconciliation for missed failed MR Pipelines."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import timedelta
from typing import Callable

import requests

from pr_agent.config_loader import get_settings
from pr_agent.feedback.store import get_db_path
from pr_agent.feedback.timez import now_cn
from pr_agent.triage.ci_failure_analysis import aggregate_failure, analyze_failed_jobs
from pr_agent.triage.ci_failure_store import save_ci_failure


def _iso_after(value: object, cutoff) -> bool:
    if not value:
        return True
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed >= cutoff
    except (TypeError, ValueError):
        return True


def _inventory(path: str, cutoff, project: str, max_mrs: int) -> list[dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mr_inventory'"
        ).fetchone()
        if exists is None:
            return []
        clauses = ["state = 'opened'", "COALESCE(updated_at, '') >= ?"]
        params: list[object] = [cutoff.isoformat()]
        if project:
            clauses.append("project_path = ?")
            params.append(project)
        params.append(max_mrs)
        return [dict(row) for row in conn.execute(
            f"SELECT * FROM mr_inventory WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()]
    finally:
        conn.close()


def _existing(path: str, project_id: str, mr_iid: str, pipeline_id: int) -> bool:
    try:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(
                "SELECT 1 FROM ci_failure_pipelines WHERE project_id = ? AND mr_iid = ? AND pipeline_id = ?",
                (project_id, mr_iid, pipeline_id),
            ).fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _failed_jobs(
    api_get: Callable,
    project_id: str,
    pipeline_id: int,
    visited: set[tuple[str, int]] | None = None,
    depth: int = 0,
) -> list[dict]:
    visited = visited or set()
    pipeline_key = (str(project_id), pipeline_id)
    if pipeline_key in visited or depth > 4:
        return []
    visited.add(pipeline_key)
    jobs = api_get(
        f"/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs",
        {"scope[]": "failed", "per_page": 100},
    ) or []
    output = [dict(job) for job in jobs if str((job or {}).get("status") or "failed") == "failed"]
    bridges = api_get(
        f"/api/v4/projects/{project_id}/pipelines/{pipeline_id}/bridges",
        {"per_page": 100},
    ) or []
    for bridge in bridges:
        downstream = (bridge or {}).get("downstream_pipeline") or {}
        downstream_id = downstream.get("id")
        if downstream_id:
            downstream_project_id = str(downstream.get("project_id") or project_id)
            output.extend(_failed_jobs(api_get, downstream_project_id, int(downstream_id), visited, depth + 1))
    return output


def reconcile_ci_failures(
    api_get: Callable[[str, dict | None], object],
    *,
    path: str,
    days: int = 30,
    project: str = "",
    max_mrs: int = 200,
    max_pipelines: int = 500,
    max_traces: int = 1000,
    apply: bool = False,
) -> dict[str, int | bool]:
    """Discover bounded historical candidates; write only when ``apply`` is true."""
    cutoff = now_cn() - timedelta(days=max(1, days))
    counters: dict[str, int | bool] = {
        "dry_run": not apply,
        "mrs_scanned": 0,
        "pipelines_scanned": 0,
        "traces_fetched": 0,
        "candidates": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
    }
    for mr in _inventory(path, cutoff, project, max(0, max_mrs)):
        if int(counters["pipelines_scanned"]) >= max_pipelines:
            break
        counters["mrs_scanned"] = int(counters["mrs_scanned"]) + 1
        project_id = str(mr.get("project_id") or "")
        mr_iid = str(mr.get("mr_iid") or "")
        try:
            pipelines = api_get(
                f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/pipelines",
                {"per_page": 100},
            ) or []
            for pipeline in pipelines:
                if int(counters["pipelines_scanned"]) >= max_pipelines:
                    break
                if str((pipeline or {}).get("status") or "").lower() != "failed":
                    continue
                if not _iso_after((pipeline or {}).get("created_at"), cutoff):
                    continue
                counters["pipelines_scanned"] = int(counters["pipelines_scanned"]) + 1
                pipeline_id = int((pipeline or {}).get("id") or 0)
                jobs = _failed_jobs(api_get, project_id, pipeline_id)

                def trace_loader(job_id: int, project_key: str = project_id) -> object:
                    if int(counters["traces_fetched"]) >= max_traces:
                        return b""
                    counters["traces_fetched"] = int(counters["traces_fetched"]) + 1
                    return api_get(f"/api/v4/projects/{project_key}/jobs/{job_id}/trace", None)

                analyzed = analyze_failed_jobs(
                    jobs,
                    trace_loader,
                    pipeline_id=pipeline_id,
                    memory_path=path,
                )
                aggregate = aggregate_failure(analyzed)
                counters["candidates"] = int(counters["candidates"]) + 1
                existed = _existing(path, project_id, mr_iid, pipeline_id)
                if not apply:
                    continue
                saved = save_ci_failure(
                    {
                        "detected_at": (pipeline or {}).get("created_at"),
                        "project_id": project_id,
                        "project_path": mr.get("project_path"),
                        "mr_iid": mr_iid,
                        "mr_url": mr.get("mr_url"),
                        "mr_title": mr.get("title"),
                        "mr_author": mr.get("author"),
                        "source_branch": mr.get("source_branch"),
                        "target_branch": mr.get("target_branch"),
                        "pipeline_id": pipeline_id,
                        "pipeline_url": (pipeline or {}).get("web_url"),
                        "pipeline_sha": (pipeline or {}).get("sha"),
                        "pipeline_status": "failed",
                        "notification_state": "not_attempted",
                        "card_id": f"reconcile:{project_id}:{mr_iid}:{pipeline_id}",
                        "source": "reconcile",
                    },
                    analyzed,
                    aggregate=aggregate,
                    path=path,
                )
                if saved is None:
                    counters["errors"] = int(counters["errors"]) + 1
                elif existed:
                    counters["updated"] = int(counters["updated"]) + 1
                else:
                    counters["inserted"] = int(counters["inserted"]) + 1
        except Exception:
            counters["errors"] = int(counters["errors"]) + 1
    return counters


def _production_api_get(path: str, params: dict | None = None) -> object:
    base_url = str(get_settings().get("GITLAB.URL", "") or "").rstrip("/")
    token = str(get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", "") or "")
    if not base_url or not token:
        raise RuntimeError("GitLab URL or token is not configured")
    response = requests.get(
        f"{base_url}{path}",
        headers={"PRIVATE-TOKEN": token},
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    if path.endswith("/trace"):
        return response.content
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--project", default="")
    parser.add_argument("--max-mrs", type=int, default=200)
    parser.add_argument("--max-pipelines", type=int, default=500)
    parser.add_argument("--max-traces", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = reconcile_ci_failures(
        _production_api_get,
        path=get_db_path(),
        days=args.days,
        project=args.project,
        max_mrs=max(0, args.max_mrs),
        max_pipelines=max(0, args.max_pipelines),
        max_traces=max(0, args.max_traces),
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
