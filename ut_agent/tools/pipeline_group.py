"""Resolve GitLab parent/child pipelines into one validation group."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pr_agent.triage.pipeline_coverage import CoverageResult, resolve_pipeline_coverage

TERMINAL_PIPELINE_STATUSES = {"success", "failed", "canceled", "skipped"}
DEFAULT_REQUIRED_JOB_PATTERNS = ("build", "coverage", "format", "merge_commit")
SAME_SHA_PARENT_LOOKBACK = timedelta(hours=1)


@dataclass(frozen=True)
class PipelineGroup:
    root_pipeline_id: int
    validation_pipeline_id: int | None
    pipeline_ids: tuple[int, ...]
    sha: str
    status: str
    jobs: tuple[tuple[int, Any], ...]
    coverage: float | None
    coverage_source: str
    coverage_status: str
    root_pipeline: Any
    validation_pipeline: Any | None
    pipelines: tuple[Any, ...]
    resolution_source: str = "anchor_only"

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_PIPELINE_STATUSES


def _pipeline_id(pipeline: Any) -> int:
    return int(pipeline.id)


def _pipeline_sha(pipeline: Any) -> str:
    return str(getattr(pipeline, "sha", "") or "")


def _created_at(pipeline: Any) -> datetime | None:
    value = str(getattr(pipeline, "created_at", "") or "")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _eligible(pipeline: Any, sha: str, not_before: datetime | None) -> bool:
    if sha and _pipeline_sha(pipeline) != sha:
        return False
    created_at = _created_at(pipeline)
    if not_before is not None and created_at is not None and created_at < not_before:
        return False
    return True


def _load_jobs(pipeline: Any) -> tuple[tuple[int, Any], ...]:
    jobs = pipeline.jobs.list(per_page=100, get_all=True)
    return tuple((_pipeline_id(pipeline), job) for job in jobs)


def _downstream_ids(pipeline: Any) -> list[int]:
    try:
        bridges = pipeline.bridges.list(get_all=True, per_page=100)
    except Exception:
        return []
    result = []
    for bridge in bridges:
        downstream = getattr(bridge, "downstream_pipeline", None)
        pipeline_id = downstream.get("id") if isinstance(downstream, dict) else getattr(downstream, "id", None)
        if pipeline_id:
            result.append(int(pipeline_id))
    return result


def _walk(project: Any, pipeline: Any, pipelines: dict[int, Any], parents: dict[int, int], depth: int = 0) -> None:
    pipeline_id = _pipeline_id(pipeline)
    if pipeline_id in pipelines or depth > 5:
        return
    pipelines[pipeline_id] = pipeline
    for child_id in _downstream_ids(pipeline):
        try:
            child = project.pipelines.get(child_id)
        except Exception:
            continue
        parents[child_id] = pipeline_id
        _walk(project, child, pipelines, parents, depth + 1)


def _same_sha_candidates(project: Any, sha: str) -> tuple[Any, ...]:
    try:
        candidates = project.pipelines.list(sha=sha, order_by="id", sort="asc", per_page=100)
    except Exception:
        return ()
    result = []
    for candidate in candidates:
        try:
            result.append(project.pipelines.get(_pipeline_id(candidate)))
        except Exception:
            continue
    return tuple(result)


def _resolve_causal_graph(
    project: Any,
    pipeline: Any,
    sha: str,
    not_before: datetime | None,
) -> tuple[dict[int, Any], dict[int, int], str]:
    """Resolve the anchor's graph, using same-SHA lookup only to find a real upstream parent."""
    anchor_id = _pipeline_id(pipeline)
    pipelines: dict[int, Any] = {}
    parents: dict[int, int] = {}
    _walk(project, pipeline, pipelines, parents)
    resolution_source = "causal_graph" if len(pipelines) > 1 else "anchor_only"

    anchor_created_at = _created_at(pipeline)
    fallback_not_before = not_before
    if fallback_not_before is None and anchor_created_at is not None:
        fallback_not_before = anchor_created_at - SAME_SHA_PARENT_LOOKBACK

    for candidate in _same_sha_candidates(project, sha):
        candidate_id = _pipeline_id(candidate)
        if candidate_id in pipelines or not _eligible(candidate, sha, fallback_not_before):
            continue
        candidate_graph: dict[int, Any] = {}
        candidate_parents: dict[int, int] = {}
        _walk(project, candidate, candidate_graph, candidate_parents)
        candidate_graph = {
            pipeline_id: value
            for pipeline_id, value in candidate_graph.items()
            if _eligible(value, sha, fallback_not_before)
        }
        if anchor_id not in candidate_graph:
            continue
        pipelines.update(candidate_graph)
        parents.update({
            child_id: parent_id
            for child_id, parent_id in candidate_parents.items()
            if child_id in candidate_graph and parent_id in candidate_graph
        })
        resolution_source = "bounded_same_sha_fallback"
    return pipelines, parents, resolution_source


def required_pipeline_job_patterns() -> tuple[str, ...]:
    """Return configured validation Job patterns with stable defaults."""
    try:
        from pr_agent.config_loader import get_settings

        value = get_settings().get("triage.pipeline_required_job_patterns", DEFAULT_REQUIRED_JOB_PATTERNS)
        return tuple(str(pattern).lower() for pattern in value if str(pattern).strip())
    except Exception:
        return DEFAULT_REQUIRED_JOB_PATTERNS


def resolve_pipeline_group(
    project: Any,
    pipeline: Any,
    *,
    required_job_patterns: tuple[str, ...],
    exact_sha: str | None = None,
    not_before: datetime | None = None,
) -> PipelineGroup:
    sha = exact_sha or _pipeline_sha(pipeline)
    pipelines, parents, resolution_source = _resolve_causal_graph(project, pipeline, sha, not_before)
    pipelines = {
        pipeline_id: candidate
        for pipeline_id, candidate in pipelines.items()
        if _eligible(candidate, sha, not_before)
    }
    if not pipelines:
        pipelines = {_pipeline_id(pipeline): pipeline}

    job_map: dict[int, tuple[tuple[int, Any], ...]] = {}
    scores: dict[int, int] = {}
    normalized_patterns = tuple(pattern.lower() for pattern in required_job_patterns if pattern)
    for pipeline_id, candidate in pipelines.items():
        try:
            jobs = _load_jobs(candidate)
        except Exception:
            jobs = ()
        job_map[pipeline_id] = jobs
        names = [str(getattr(job, "name", "")).lower() for _, job in jobs]
        scores[pipeline_id] = sum(any(pattern in name for name in names) for pattern in normalized_patterns)

    validation_id = max(scores, key=lambda pipeline_id: (scores[pipeline_id], pipeline_id)) if scores else None
    if validation_id is not None and scores[validation_id] == 0:
        validation_id = None
    current_id = _pipeline_id(pipeline)
    if validation_id is not None and scores.get(current_id, 0) == scores[validation_id]:
        validation_id = current_id

    root_id = validation_id or current_id
    while root_id in parents and parents[root_id] in pipelines:
        root_id = parents[root_id]
    if root_id not in pipelines:
        root_id = min(pipelines)
    root_pipeline = pipelines[root_id]
    validation_pipeline = pipelines.get(validation_id) if validation_id is not None else None
    validation_jobs = job_map.get(validation_id, ()) if validation_id is not None else ()
    if validation_pipeline is None:
        status = "running"
        coverage_result = CoverageResult(status="validation_pipeline_missing")
    else:
        status = str(getattr(validation_pipeline, "status", "") or "unknown").lower()
        coverage_result = resolve_pipeline_coverage(
            project,
            validation_pipeline,
            validation_jobs,
            normalized_patterns,
        )

    ordered_ids = tuple(sorted(pipelines))
    return PipelineGroup(
        root_pipeline_id=root_id,
        validation_pipeline_id=validation_id,
        pipeline_ids=ordered_ids,
        sha=sha,
        status=status,
        jobs=validation_jobs,
        coverage=coverage_result.value,
        coverage_source=coverage_result.source,
        coverage_status=coverage_result.status,
        root_pipeline=root_pipeline,
        validation_pipeline=validation_pipeline,
        pipelines=tuple(pipelines[pipeline_id] for pipeline_id in ordered_ids),
        resolution_source=resolution_source,
    )
