from types import SimpleNamespace

import pytest

from pr_agent.triage.pipeline_coverage import (
    CoverageResult,
    normalize_coverage,
    parse_changed_lines_summary,
    parse_coverage_trace,
    resolve_pipeline_coverage,
)


class FakeGitLabError(Exception):
    def __init__(self, response_code: int):
        super().__init__(f"HTTP {response_code}")
        self.response_code = response_code


class LoadedJob:
    def __init__(self, *, artifact=None, trace=b"", artifact_error=None, trace_error=None):
        self.artifact_value = artifact
        self.trace_value = trace
        self.artifact_error = artifact_error
        self.trace_error = trace_error
        self.artifact_calls = []
        self.trace_calls = 0

    def artifact(self, path):
        self.artifact_calls.append(path)
        if self.artifact_error:
            raise self.artifact_error
        return self.artifact_value

    def trace(self):
        self.trace_calls += 1
        if self.trace_error:
            raise self.trace_error
        return self.trace_value


class JobCollection:
    def __init__(self, values):
        self.values = values
        self.get_calls = []

    def get(self, job_id):
        self.get_calls.append(int(job_id))
        value = self.values[int(job_id)]
        if isinstance(value, Exception):
            raise value
        return value


def _summary_job(job_id: int, *, status: str = "success", name: str = "x86_64_ut_coverage_check"):
    return SimpleNamespace(id=job_id, name=name, status=status)


def _resolve(*, loaded_jobs, summaries, pipeline_coverage=None):
    project = SimpleNamespace(jobs=JobCollection(loaded_jobs))
    pipeline = SimpleNamespace(id=300, coverage=pipeline_coverage)
    jobs = tuple((300, summary) for summary in summaries)
    result = resolve_pipeline_coverage(
        project,
        pipeline,
        jobs,
        ("build", "coverage", "format", "merge_commit"),
    )
    return result, project


def test_artifact_coverage_wins_over_trace_and_pipeline():
    job = LoadedJob(
        artifact="<div>覆盖率</div><strong>63.04%</strong>".encode(),
        trace=b"Coverage: 61.00%",
    )

    result, _ = _resolve(loaded_jobs={107440: job}, summaries=[_summary_job(107440)], pipeline_coverage="70.0")

    assert result == CoverageResult(63.04, "changed_lines", "reported", 107440)
    assert job.trace_calls == 1


def test_artifact_404_falls_back_to_trace():
    job = LoadedJob(artifact_error=FakeGitLabError(404), trace=b"Coverage: 62.50%")

    result, _ = _resolve(loaded_jobs={8: job}, summaries=[_summary_job(8)])

    assert result == CoverageResult(62.5, "changed_lines", "reported", 8)


def test_failed_job_artifact_can_report_coverage():
    job = LoadedJob(artifact="<div>覆盖率</div><strong>63.04%</strong>".encode())

    result, project = _resolve(
        loaded_jobs={8: job},
        summaries=[_summary_job(8, status="failed")],
    )

    assert result == CoverageResult(63.04, "changed_lines", "reported", 8)
    assert project.jobs.get_calls == [8]


def test_failed_job_trace_can_report_coverage_when_artifact_is_missing():
    job = LoadedJob(
        artifact_error=FakeGitLabError(404),
        trace=b"Coverage: 62.50%\nThreshold: 80.0%",
    )

    result, project = _resolve(
        loaded_jobs={8: job},
        summaries=[_summary_job(8, status="failed")],
    )

    assert result == CoverageResult(62.5, "changed_lines", "reported", 8, 80.0)
    assert project.jobs.get_calls == [8]


def test_missing_job_report_falls_back_to_pipeline_coverage():
    job = LoadedJob(artifact_error=FakeGitLabError(404), trace=b"no coverage summary")

    result, _ = _resolve(loaded_jobs={8: job}, summaries=[_summary_job(8)], pipeline_coverage="71.2")

    assert result == CoverageResult(71.2, "gitlab_pipeline", "reported")


@pytest.mark.parametrize(
    ("summaries", "expected_status"),
    [
        ([_summary_job(8)], "report_missing"),
        ([_summary_job(8, status="failed")], "job_failed"),
        ([_summary_job(8, name="build_release_arm64")], "not_configured"),
    ],
)
def test_missing_coverage_has_precise_status(summaries, expected_status):
    loaded_jobs = {8: LoadedJob(artifact_error=FakeGitLabError(404), trace=b"no report")}

    result, project = _resolve(loaded_jobs=loaded_jobs, summaries=summaries)

    assert result == CoverageResult(status=expected_status)
    if expected_status == "not_configured":
        assert project.jobs.get_calls == []
    elif expected_status == "job_failed":
        assert project.jobs.get_calls == [8]


def test_fetch_failures_are_contained():
    result, _ = _resolve(
        loaded_jobs={8: FakeGitLabError(503)},
        summaries=[_summary_job(8)],
    )

    assert result == CoverageResult(status="fetch_failed", job_id=8)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01, 100.01, "bad", True, [], {}])
def test_normalize_coverage_rejects_invalid_values(value):
    assert normalize_coverage(value) is None


def test_multiple_successful_jobs_are_checked_newest_first():
    newest = LoadedJob(artifact_error=FakeGitLabError(404), trace=b"no report")
    older = LoadedJob(artifact_error=FakeGitLabError(404), trace=b"Coverage: 64.0%")

    result, project = _resolve(
        loaded_jobs={10: older, 20: newest},
        summaries=[_summary_job(10), _summary_job(20)],
    )

    assert result == CoverageResult(64.0, "changed_lines", "reported", 10)
    assert project.jobs.get_calls == [20, 10]


def test_changed_lines_and_trace_parsers_preserve_existing_summary_contracts():
    html = """
    <div>总修改行数</div><strong>92</strong>
    <div>已覆盖行数</div><strong>58</strong>
    <div>未覆盖行数</div><strong>34</strong>
    <div>覆盖率</div><strong>63.04%</strong>
    """
    trace = """
    Coverage: 63.04%
    Threshold: 80.0%
    Total changed lines: 92
    Covered changed lines: 58
    """

    assert parse_changed_lines_summary(html) == {
        "total": 92,
        "covered": 58,
        "uncovered": 34,
        "coverage_pct": 63.04,
    }
    assert parse_coverage_trace(trace) == {
        "coverage": 63.04,
        "threshold": 80.0,
        "total_lines": 92,
        "covered_lines": 58,
    }
