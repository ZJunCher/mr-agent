from pr_agent.triage.failure_explanations import (
    FailureExplanation,
    collect_gitlab_failure_explanations,
    extract_confirmed_reason,
    extract_confirmed_reason_with_line,
    merge_failure_explanations,
    sanitize_failure_text,
    select_latest_failed_jobs,
    source_job_records,
)


def test_extracts_actionable_mr_title_error():
    trace = """2026-08-11T08:35:01Z MR的标题: m-7058840375 [ctrl] common reconstruction
2026-08-11T08:35:03Z ❌ ERROR: 关联的是技术需求，但当前节点不是代码合入，请将节点设置为代码合入
2026-08-11T08:35:03Z ERROR: Job failed: command terminated with exit code 1"""

    assert extract_confirmed_reason(trace) == (
        "❌ ERROR: 关联的是技术需求，但当前节点不是代码合入，请将节点设置为代码合入"
    )


def test_extractor_strips_ansi_and_ignores_generic_runner_failure():
    trace = "\x1b[31merror: missing required header sdk/header.h\x1b[0m\nERROR: Job failed: exit code 1"

    assert extract_confirmed_reason(trace) == "error: missing required header sdk/header.h"
    assert extract_confirmed_reason("ERROR: Job failed: exit code 1") == ""


def test_extracts_reason_with_one_based_trace_line_without_breaking_legacy_api():
    trace = "preparing\nerror: missing required header sdk/header.h\nERROR: Job failed: exit code 1"

    assert extract_confirmed_reason_with_line(trace) == (
        "error: missing required header sdk/header.h",
        2,
    )
    assert extract_confirmed_reason(trace) == "error: missing required header sdk/header.h"


def test_empty_actionable_trace_has_no_location():
    assert extract_confirmed_reason_with_line("ERROR: Job failed: exit code 1") == ("", 0)


def test_include_stack_error_identifier_does_not_outrank_fatal_missing_header():
    trace = (
        "2026-08-22T01:00:00Z 01O from /usr/include/c++/13/system_error:43,\n"
        "2026-08-22T01:00:01Z 01O test.cpp:8:10: "
        "fatal error: gmock/gmock.h: No such file or directory\n"
        "2026-08-22T01:00:02Z 00O ERROR: Job failed: exit code 1\n"
    )

    assert extract_confirmed_reason_with_line(trace) == (
        "01O test.cpp:8:10: fatal error: gmock/gmock.h: No such file or directory",
        2,
    )


def test_failure_explanation_matches_canonical_primary_diagnostic():
    from ut_agent.ci_diagnostics import extract_diagnostic_candidates, primary_diagnostic

    trace = (
        "2026-08-22T01:00:00Z 01O from /usr/include/c++/13/system_error:43,\n"
        "2026-08-22T01:00:01Z 01O test.cpp:8:10: "
        "fatal error: sdk/api.hpp: No such file or directory\n"
    )
    candidates = extract_diagnostic_candidates(trace)
    primary = primary_diagnostic(candidates.candidates)

    assert primary is not None
    assert extract_confirmed_reason_with_line(trace) == (primary.text, primary.line_number)


def test_sanitizer_redacts_secrets_and_bounds_text():
    value = "authorization: bearer-secret token=abc password=hunter2 " + ("x" * 500)

    sanitized = sanitize_failure_text(value)

    assert "bearer-secret" not in sanitized
    assert "hunter2" not in sanitized
    assert "token=[REDACTED]" in sanitized
    assert len(sanitized) == 300


def test_merge_keeps_confirmed_and_sanitizes_correlated_inference():
    confirmed = [FailureExplanation(job_name="mr_title_check", confirmed_reason="标题检查失败")]
    inferred = [FailureExplanation(
        job_name="mr_title_check",
        possible_reason="需求节点不适用于代码合入 token=secret",
        suggested_action="更换处于代码合入节点的需求 ID",
        confidence="inferred",
    )]

    merged = merge_failure_explanations(confirmed, inferred)

    assert merged[0].confirmed_reason == "标题检查失败"
    assert merged[0].possible_reason == "需求节点不适用于代码合入 token=[REDACTED]"
    assert merged[0].suggested_action == "更换处于代码合入节点的需求 ID"


def test_select_latest_failed_jobs_keeps_highest_job_id_per_name():
    jobs = [
        {"id": 10, "name": "build"},
        {"id": 12, "name": "build"},
        {"id": 11, "name": "clang"},
    ]

    selected = select_latest_failed_jobs(jobs)

    assert {(job["name"], job["id"]) for job in selected} == {("build", 12), ("clang", 11)}


def test_collect_failure_explanations_keeps_unknown_record_when_trace_fails():
    class Job:
        def trace(self):
            raise RuntimeError("provider body must not escape")

    class Jobs:
        def get(self, _job_id):
            return Job()

    class Project:
        jobs = Jobs()

    records = collect_gitlab_failure_explanations(
        Project(),
        [{
            "id": 99853,
            "name": "mr_title_check",
            "web_url": "https://gitlab.example/eabot/control/-/jobs/99853",
            "pipeline": {"id": 31089},
        }],
        31089,
    )

    assert len(records) == 1
    assert records[0].job_name == "mr_title_check"
    assert records[0].confirmed_reason == ""
    assert records[0].confidence == "unknown"
    assert records[0].job_url.endswith("/jobs/99853")
    assert records[0].trace_line == 0


def test_collect_failure_explanations_keeps_confirmed_trace_line():
    class Job:
        def trace(self):
            return b"preparing\nerror: missing required header sdk/header.h\ncleanup"

    class Jobs:
        def get(self, _job_id):
            return Job()

    class Project:
        jobs = Jobs()

    records = collect_gitlab_failure_explanations(
        Project(),
        [{
            "id": 99854,
            "name": "build",
            "web_url": "https://gitlab.example/eabot/control/-/jobs/99854",
            "pipeline": {"id": 31090},
        }],
        31090,
    )

    assert records[0].confirmed_reason == "error: missing required header sdk/header.h"
    assert records[0].trace_line == 2
    assert records[0].to_dict()["trace_line"] == 2


def test_source_job_records_keep_latest_safe_job_per_name():
    records = source_job_records([
        FailureExplanation(
            job_name="build",
            job_id=10,
            job_url="javascript:alert(1)",
            trace_line=-7,
        ),
        FailureExplanation(
            job_name="build",
            job_id=12,
            job_url="https://gitlab.example/eabot/cook/-/jobs/12",
            trace_line=27,
        ),
    ])

    assert records == ({
        "job_name": "build",
        "job_id": 12,
        "job_url": "https://gitlab.example/eabot/cook/-/jobs/12",
        "trace_line": 27,
    },)


def test_failure_explanation_defaults_missing_trace_line_to_zero():
    assert FailureExplanation.from_dict({"job_name": "legacy"}).trace_line == 0


def test_failure_explanation_rejects_non_http_job_link_on_deserialize():
    record = FailureExplanation.from_dict({
        "job_name": "build",
        "job_url": "javascript:alert(1)",
        "confirmed_reason": "fatal: failed",
    })

    assert record.job_url == ""
