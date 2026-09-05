"""Reliable completion snapshot for a GitLab parent/child pipeline group."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ut_agent.tools.pipeline_group import PipelineGroup, resolve_pipeline_group

NONTERMINAL_PIPELINE_STATUSES = {
    "created",
    "pending",
    "preparing",
    "waiting_for_resource",
    "scheduled",
    "running",
}
TERMINAL_PIPELINE_STATUSES = {"success", "failed", "canceled", "skipped"}


def _attribute(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


@dataclass(frozen=True)
class PipelineCompletionSnapshot:
    terminal: bool
    root_pipeline_id: int
    validation_pipeline_id: int
    sha: str
    pipeline_statuses: tuple[tuple[int, str], ...]
    nonterminal_job_names: tuple[str, ...]
    digest: str
    reason: str = ""


def completion_snapshot_from_group(
    group: PipelineGroup,
    required_job_patterns: tuple[str, ...],
) -> PipelineCompletionSnapshot:
    patterns = tuple(str(pattern).lower() for pattern in required_job_patterns if str(pattern).strip())
    pipeline_statuses = tuple(
        sorted(
            (
                int(_attribute(pipeline, "id", 0) or 0),
                str(_attribute(pipeline, "status", "unknown") or "unknown").lower(),
            )
            for pipeline in group.pipelines
        )
    )
    nonterminal_jobs = []
    for _, job in group.jobs:
        name = str(_attribute(job, "name", "") or "")
        status = str(_attribute(job, "status", "unknown") or "unknown").lower()
        if patterns and not any(pattern in name.lower() for pattern in patterns):
            continue
        if status == "manual":
            continue
        if status in NONTERMINAL_PIPELINE_STATUSES or status not in TERMINAL_PIPELINE_STATUSES:
            nonterminal_jobs.append(name or "unknown")
    missing_validation = not group.validation_pipeline_id
    nonterminal_pipelines = [
        pipeline_id
        for pipeline_id, status in pipeline_statuses
        if status in NONTERMINAL_PIPELINE_STATUSES or status not in TERMINAL_PIPELINE_STATUSES
    ]
    terminal = not missing_validation and not nonterminal_pipelines and not nonterminal_jobs
    reason = ""
    if missing_validation:
        reason = "等待验证流水线创建"
    elif nonterminal_pipelines or nonterminal_jobs:
        reason = "仍有流水线或关键 Job 正在运行"
    identity = json.dumps(
        {
            "root": group.root_pipeline_id,
            "validation": group.validation_pipeline_id or 0,
            "sha": group.sha,
            "pipelines": pipeline_statuses,
            "nonterminal_jobs": sorted(nonterminal_jobs),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return PipelineCompletionSnapshot(
        terminal=terminal,
        root_pipeline_id=int(group.root_pipeline_id or 0),
        validation_pipeline_id=int(group.validation_pipeline_id or 0),
        sha=str(group.sha or ""),
        pipeline_statuses=pipeline_statuses,
        nonterminal_job_names=tuple(sorted(nonterminal_jobs)),
        digest=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        reason=reason,
    )


def inspect_pipeline_completion(
    project: Any,
    pipeline: Any,
    *,
    required_job_patterns: tuple[str, ...],
    exact_sha: str,
) -> PipelineCompletionSnapshot:
    group = resolve_pipeline_group(
        project,
        pipeline,
        required_job_patterns=required_job_patterns,
        exact_sha=exact_sha,
    )
    return completion_snapshot_from_group(group, required_job_patterns)
