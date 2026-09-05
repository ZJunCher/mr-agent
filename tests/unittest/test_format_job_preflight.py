from pr_agent.triage.format_job_preflight import (
    FORMAT_CI_JOB_CONFIGURATION,
    FORMAT_REPAIRABLE_OR_UNKNOWN,
    classify_format_job_trace,
)


def test_empty_git_revision_is_ci_job_configuration():
    trace = "ERROR: git diff failed: fatal: ambiguous argument '': unknown revision or path not in the working tree."

    result = classify_format_job_trace(trace, job_url="https://gitlab.example/jobs/1")

    assert result.kind == FORMAT_CI_JOB_CONFIGURATION
    assert "基准 Commit 为空" in result.summary
    assert "格式检查尚未开始" in result.summary
    assert result.job_url == "https://gitlab.example/jobs/1"
    assert "ambiguous argument ''" in result.evidence


def test_ansi_prefix_does_not_hide_empty_git_revision():
    trace = (
        "\x1b[0K2026-08-07T08:09:23.478107Z 01O "
        "❌ ERROR: git diff failed: fatal: ambiguous argument '': "
        "unknown revision or path not in the working tree.\x1b[0;m\n"
    )

    result = classify_format_job_trace(trace)

    assert result.kind == FORMAT_CI_JOB_CONFIGURATION
    assert "\x1b" not in result.evidence


def test_artifact_warning_alone_does_not_suppress_repair():
    result = classify_format_job_trace("WARNING: code-format-report.txt: no matching files")

    assert result.kind == FORMAT_REPAIRABLE_OR_UNKNOWN
    assert result.summary == ""


def test_unrelated_git_diff_failure_does_not_suppress_repair():
    result = classify_format_job_trace("ERROR: git diff failed: fatal: bad object abc123")

    assert result.kind == FORMAT_REPAIRABLE_OR_UNKNOWN


def test_evidence_is_bounded():
    trace = "x" * 600 + " ERROR: git diff failed: fatal: ambiguous argument '': unknown revision" + "y" * 600

    result = classify_format_job_trace(trace)

    assert result.kind == FORMAT_CI_JOB_CONFIGURATION
    assert len(result.evidence) <= 500
    assert "ambiguous argument ''" in result.evidence
