from dataclasses import dataclass

from pr_agent.triage.pipeline_freshness import (
    PipelineFreshnessState,
    check_pipeline_freshness,
)


@dataclass
class _Response:
    payload: object
    ok: bool = True
    status_code: int = 200

    def json(self):
        return self.payload


def _api(*, mr=None, pipelines=None, pipelines_ok=True):
    calls = []

    def get(path, *, params=None):
        calls.append((path, params))
        if path.endswith("/pipelines"):
            return _Response(pipelines, ok=pipelines_ok, status_code=200 if pipelines_ok else 503)
        return _Response(mr)

    get.calls = calls
    return get


def _scoped_pipeline_api(*, mr_pipelines, project_pipelines):
    calls = []

    def get(path, *, params=None):
        calls.append((path, params))
        if "/merge_requests/" in path and path.endswith("/pipelines"):
            return _Response(mr_pipelines)
        if path.endswith("/pipelines"):
            return _Response(project_pipelines)
        return _Response({"sha": "same-sha"})

    get.calls = calls
    return get


def test_rejects_pipeline_from_old_head_without_listing_pipelines():
    api_get = _api(pipelines=[])

    result = check_pipeline_freshness(
        api_get=api_get,
        project_id="eabot/cook",
        mr_iid=10,
        pipeline_id=100,
        pipeline_sha="old-sha",
        ref="feature/test",
        mr_payload={"diff_refs": {"head_sha": "new-sha"}},
    )

    assert result.state is PipelineFreshnessState.STALE_HEAD
    assert result.head_sha == "new-sha"
    assert api_get.calls == []


def test_rejects_older_retry_for_same_sha_even_when_latest_is_running():
    result = check_pipeline_freshness(
        api_get=_api(
            pipelines=[
                {"id": 102, "sha": "same-sha", "source": "push", "status": "running"},
                {"id": 101, "sha": "same-sha", "source": "push", "status": "failed"},
            ]
        ),
        project_id="eabot/cook",
        mr_iid=10,
        pipeline_id=101,
        pipeline_sha="same-sha",
        ref="feature/test",
        mr_payload={"sha": "same-sha"},
    )

    assert result.state is PipelineFreshnessState.STALE_PIPELINE
    assert result.latest_pipeline_id == 102
    assert result.latest_pipeline_status == "running"


def test_accepts_latest_top_level_pipeline_and_ignores_downstream():
    result = check_pipeline_freshness(
        api_get=_api(
            pipelines=[
                {"id": 103, "source": "parent_pipeline", "status": "failed"},
                {"id": 102, "source": "push", "status": "failed"},
            ]
        ),
        project_id="eabot/cook",
        mr_iid=10,
        pipeline_id=102,
        pipeline_sha="same-sha",
        ref="feature/test",
        mr_payload={"sha": "same-sha"},
    )

    assert result.state is PipelineFreshnessState.CURRENT
    assert result.latest_pipeline_id == 102


def test_uses_mr_pipeline_scope_when_two_mrs_share_branch_and_sha():
    api_get = _scoped_pipeline_api(
        mr_pipelines=[
            {"id": 30786, "sha": "same-sha", "source": "merge_request_event", "status": "failed"},
        ],
        project_pipelines=[
            {"id": 30959, "sha": "same-sha", "source": "merge_request_event", "status": "failed"},
            {"id": 30786, "sha": "same-sha", "source": "merge_request_event", "status": "failed"},
        ],
    )

    result = check_pipeline_freshness(
        api_get=api_get,
        project_id="eabot/cook",
        mr_iid=547,
        pipeline_id=30786,
        pipeline_sha="same-sha",
        ref="shared-branch",
        mr_payload={"sha": "same-sha"},
    )

    assert result.state is PipelineFreshnessState.CURRENT
    assert api_get.calls[0][0].endswith("/merge_requests/547/pipelines")


def test_loads_mr_detail_when_list_payload_has_no_head_sha():
    api_get = _api(
        mr={"diff_refs": {"head_sha": "same-sha"}},
        pipelines=[{"id": 102, "source": "push", "status": "failed"}],
    )

    result = check_pipeline_freshness(
        api_get=api_get,
        project_id="eabot/cook",
        mr_iid=10,
        pipeline_id=102,
        pipeline_sha="same-sha",
        ref="feature/test",
    )

    assert result.state is PipelineFreshnessState.CURRENT
    assert api_get.calls[0][0].endswith("/projects/eabot%2Fcook/merge_requests/10")


def test_pipeline_lookup_failure_is_unknown_not_current():
    result = check_pipeline_freshness(
        api_get=_api(pipelines={"message": "unavailable"}, pipelines_ok=False),
        project_id="eabot/cook",
        mr_iid=10,
        pipeline_id=102,
        pipeline_sha="same-sha",
        ref="feature/test",
        mr_payload={"sha": "same-sha"},
        attempts=1,
    )

    assert result.state is PipelineFreshnessState.UNKNOWN
    assert result.reason == "pipeline_lookup_failed"
