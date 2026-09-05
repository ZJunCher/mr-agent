from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote


class PipelineFreshnessState(StrEnum):
    CURRENT = "current"
    STALE_HEAD = "stale_head"
    STALE_PIPELINE = "stale_pipeline"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PipelineFreshness:
    state: PipelineFreshnessState
    head_sha: str = ""
    latest_pipeline_id: int = 0
    latest_pipeline_status: str = ""
    reason: str = ""

    @property
    def current(self) -> bool:
        return self.state is PipelineFreshnessState.CURRENT


def _extract_head_sha(value: Mapping[str, Any]) -> str:
    diff_refs = value.get("diff_refs") or {}
    if not isinstance(diff_refs, Mapping):
        diff_refs = {}
    return str(value.get("sha") or diff_refs.get("head_sha") or "")


def _request_json(
    api_get: Callable[..., Any],
    path: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int,
) -> Any:
    for _ in range(max(1, attempts)):
        response = api_get(path, params=params)
        if response is None or not getattr(response, "ok", False):
            continue
        try:
            return response.json()
        except Exception:
            continue
    return None


def check_pipeline_freshness(
    *,
    api_get: Callable[..., Any],
    project_id: str | int,
    mr_iid: int,
    pipeline_id: int,
    pipeline_sha: str,
    ref: str,
    mr_payload: Mapping[str, Any] | None = None,
    attempts: int = 2,
) -> PipelineFreshness:
    encoded_project = quote(str(project_id), safe="")
    head_sha = _extract_head_sha(mr_payload or {})
    if not head_sha:
        mr_response = _request_json(
            api_get,
            f"/api/v4/projects/{encoded_project}/merge_requests/{mr_iid}",
            attempts=attempts,
        )
        if not isinstance(mr_response, Mapping):
            return PipelineFreshness(PipelineFreshnessState.UNKNOWN, reason="mr_lookup_failed")
        head_sha = _extract_head_sha(mr_response)
    if not head_sha:
        return PipelineFreshness(PipelineFreshnessState.UNKNOWN, reason="head_sha_missing")
    if pipeline_sha != head_sha:
        return PipelineFreshness(
            PipelineFreshnessState.STALE_HEAD,
            head_sha=head_sha,
            reason="head_sha_changed",
        )

    pipelines = _request_json(
        api_get,
        f"/api/v4/projects/{encoded_project}/merge_requests/{mr_iid}/pipelines",
        params={"per_page": 100},
        attempts=attempts,
    )
    if not isinstance(pipelines, list):
        return PipelineFreshness(
            PipelineFreshnessState.UNKNOWN,
            head_sha=head_sha,
            reason="pipeline_lookup_failed",
        )
    top_level = [
        item
        for item in pipelines
        if (
            isinstance(item, Mapping)
            and str(item.get("source") or "") != "parent_pipeline"
            and str(item.get("sha") or pipeline_sha) == pipeline_sha
        )
    ]
    if not top_level:
        return PipelineFreshness(
            PipelineFreshnessState.UNKNOWN,
            head_sha=head_sha,
            reason="pipeline_missing",
        )
    latest = max(top_level, key=lambda item: int(item.get("id") or 0))
    latest_id = int(latest.get("id") or 0)
    if latest_id <= 0:
        return PipelineFreshness(
            PipelineFreshnessState.UNKNOWN,
            head_sha=head_sha,
            reason="pipeline_id_missing",
        )
    state = (
        PipelineFreshnessState.CURRENT
        if latest_id == int(pipeline_id)
        else PipelineFreshnessState.STALE_PIPELINE
    )
    return PipelineFreshness(
        state,
        head_sha=head_sha,
        latest_pipeline_id=latest_id,
        latest_pipeline_status=str(latest.get("status") or ""),
        reason="" if state is PipelineFreshnessState.CURRENT else "newer_pipeline_exists",
    )
