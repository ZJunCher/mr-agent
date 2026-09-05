import json
from types import SimpleNamespace

from pr_agent.config_loader import get_settings
from ut_agent.tools import fetch_pipeline as pipeline_tools
from ut_agent.tools.pipeline_group import resolve_pipeline_group

get_settings()


class Collection:
    def __init__(self, values=()):
        self.values = list(values)

    def list(self, **_kwargs):
        return list(self.values)


class PipelineCollection(Collection):
    def __init__(self, values):
        super().__init__(values)
        self.by_id = {value.id: value for value in values}

    def get(self, pipeline_id):
        return self.by_id[int(pipeline_id)]


def pipeline(
    pipeline_id,
    status,
    jobs,
    *,
    source="push",
    downstream=(),
    created_at="2026-08-19T06:10:00Z",
):
    bridges = [
        SimpleNamespace(name=f"child-{child.id}", downstream_pipeline={"id": child.id})
        for child in downstream
    ]
    return SimpleNamespace(
        id=pipeline_id,
        sha="abc",
        status=status,
        source=source,
        created_at=created_at,
        coverage=None,
        jobs=Collection(
            SimpleNamespace(id=index, name=name, status=job_status)
            for index, (name, job_status) in enumerate(jobs)
        ),
        bridges=Collection(bridges),
    )


def project(*pipelines, loaded_jobs=()):
    return SimpleNamespace(
        pipelines=PipelineCollection(pipelines),
        jobs=PipelineCollection(loaded_jobs),
    )


def test_dynamic_parent_uses_child_as_validation_pipeline():
    child = pipeline(
        29921,
        "failed",
        [("build_release_arm64", "failed"), ("code_format_check", "failed")],
        source="parent_pipeline",
    )
    root = pipeline(29920, "success", [("generate_joblist", "success")], downstream=[child])

    group = resolve_pipeline_group(
        project(root, child),
        root,
        required_job_patterns=("build", "coverage", "format", "merge_commit"),
        exact_sha="abc",
    )

    assert group.root_pipeline_id == 29920
    assert group.validation_pipeline_id == 29921
    assert group.pipeline_ids == (29920, 29921)
    assert group.status == "failed"
    assert [job.name for _, job in group.jobs] == ["build_release_arm64", "code_format_check"]


def test_child_first_event_still_discovers_root():
    child = pipeline(29921, "running", [("build_release_arm64", "running")], source="parent_pipeline")
    root = pipeline(29920, "success", [("generate_joblist", "success")], downstream=[child])

    group = resolve_pipeline_group(
        project(root, child),
        child,
        required_job_patterns=("build", "coverage", "format", "merge_commit"),
        exact_sha="abc",
    )

    assert group.root_pipeline_id == 29920
    assert group.validation_pipeline_id == 29921
    assert group.status == "running"
    assert group.resolution_source == "bounded_same_sha_fallback"


def test_unrelated_historical_same_sha_pipeline_cannot_win_by_job_score():
    historical = pipeline(
        29065,
        "failed",
        [
            ("build_release_arm64", "success"),
            ("x86_64_ut_coverage_check", "success"),
            ("code_format_check", "failed"),
            ("mr_merge_commit_check", "failed"),
        ],
        created_at="2026-08-04T06:10:00Z",
    )
    child = pipeline(
        33534,
        "failed",
        [
            ("build_release_arm64", "success"),
            ("x86_64_ut_coverage_check", "success"),
            ("code_format_check", "failed"),
        ],
        source="parent_pipeline",
    )
    root = pipeline(
        33530,
        "success",
        [("generate_joblist", "success")],
        downstream=[child],
        created_at="2026-08-19T06:09:58Z",
    )

    group = resolve_pipeline_group(
        project(historical, root, child),
        root,
        required_job_patterns=("build", "coverage", "format", "merge_commit"),
        exact_sha="abc",
    )

    assert group.root_pipeline_id == 33530
    assert group.validation_pipeline_id == 33534
    assert group.pipeline_ids == (33530, 33534)
    assert group.resolution_source == "causal_graph"


def test_parent_without_validation_jobs_is_not_reported_as_success():
    root = pipeline(29920, "success", [("generate_joblist", "success")])

    group = resolve_pipeline_group(
        project(root),
        root,
        required_job_patterns=("build", "coverage", "format", "merge_commit"),
        exact_sha="abc",
    )

    assert group.validation_pipeline_id is None
    assert group.status == "running"
    assert group.coverage_source == ""
    assert group.coverage_status == "validation_pipeline_missing"


def test_validation_pipeline_resolves_changed_lines_coverage():
    validation = pipeline(
        29921,
        "success",
        [("x86_64_ut_coverage_check", "success")],
        source="parent_pipeline",
    )
    loaded_job = SimpleNamespace(
        id=0,
        artifact=lambda _path: "<div>覆盖率</div><strong>63.04%</strong>",
        trace=lambda: b"Coverage: 61.00%",
    )

    group = resolve_pipeline_group(
        project(validation, loaded_jobs=(loaded_job,)),
        validation,
        required_job_patterns=("build", "coverage", "format", "merge_commit"),
        exact_sha="abc",
    )

    assert group.coverage == 63.04
    assert group.coverage_source == "changed_lines"
    assert group.coverage_status == "reported"
    assert pipeline_tools._group_fields(group)["coverage_source"] == "changed_lines"


def test_fetch_logs_reports_validation_child_truth(monkeypatch):
    child = pipeline(
        29921,
        "failed",
        [("build_release_arm64", "failed"), ("code_format_check", "failed")],
        source="parent_pipeline",
    )
    root = pipeline(29920, "success", [("generate_joblist", "success")], downstream=[child])
    fake_project = project(root, child)
    provider = SimpleNamespace(
        id_project="eabot/chogori",
        gl=SimpleNamespace(projects=SimpleNamespace(get=lambda _project_id: fake_project)),
    )
    monkeypatch.setattr(pipeline_tools, "get_git_provider", lambda: provider)
    monkeypatch.setattr(pipeline_tools, "_get_failed_job_diagnostics", lambda *_args: "compiler error")

    result = json.loads(
        pipeline_tools.fetch_pipeline_logs_tool.func(
            pipeline_id=29920,
            commit_sha="abc",
            state={"project_id": "eabot/chogori"},
        )
    )

    assert result["pipeline_id"] == 29921
    assert result["root_pipeline_id"] == 29920
    assert result["validation_pipeline_id"] == 29921
    assert result["pipeline_ids"] == [29920, 29921]
    assert result["pipeline_status"] == "failed"
    assert [job["name"] for job in result["failed_jobs"]] == ["build_release_arm64", "code_format_check"]
