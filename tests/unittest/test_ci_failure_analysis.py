import sqlite3

from pr_agent.triage.ci_failure_analysis import (
    CapabilityClass,
    FailureFamily,
    aggregate_failure,
    analyze_failed_jobs,
)
from ut_agent.repair_memory.episodes import _diagnostic_fingerprint


def _job(job_id: int = 11, name: str = "build_release") -> dict:
    return {
        "id": job_id,
        "name": name,
        "stage": "build",
        "web_url": f"https://gitlab.example/jobs/{job_id}",
        "pipeline": {"id": 91},
    }


def test_analyze_failed_jobs_extracts_sanitized_stable_fingerprint():
    traces = {
        11: "2026-08-20T09:00:00Z /tmp/a/src/main.cc:18:4: error: undefined reference to Foo token=secret-a",
        12: "2026-08-21T10:11:12Z /work/b/src/main.cc:99:8: error: undefined reference to Foo token=secret-b",
    }

    jobs = analyze_failed_jobs(
        [_job(11, "build_release"), _job(12, "build_debug")],
        lambda job_id: traces[job_id],
        pipeline_id=91,
    )

    assert len(jobs) == 2
    assert jobs[0].family is FailureFamily.BUILD
    assert "secret-a" not in jobs[0].confirmed_reason
    assert "[REDACTED]" in jobs[0].confirmed_reason
    assert jobs[0].trace_line == 1
    assert jobs[0].fingerprint == jobs[1].fingerprint
    assert jobs[0].fingerprint == _diagnostic_fingerprint(
        (jobs[0].confirmed_reason,), (jobs[0].job_name,)
    )
    assert jobs[0].capability is CapabilityClass.CAPABILITY_GAP
    assert jobs[0].capability_basis == "code_failure_without_verified_support"


def test_generic_exit_code_and_missing_trace_remain_unknown():
    jobs = analyze_failed_jobs(
        [_job(11, "misc_job"), _job(12, "another_job")],
        lambda job_id: "Job failed: exit code 1" if job_id == 11 else b"",
        pipeline_id=91,
    )

    assert all(item.confirmed_reason == "" for item in jobs)
    assert all(item.fingerprint == "" for item in jobs)
    assert all(item.family is FailureFamily.UNKNOWN for item in jobs)
    assert all(item.capability is CapabilityClass.UNKNOWN for item in jobs)


def test_job_url_rejects_non_http_schemes():
    job = _job()
    job["web_url"] = "javascript:alert(1)"

    analyzed = analyze_failed_jobs(
        [job], lambda _job_id: "error: compile failed", pipeline_id=91
    )

    assert analyzed[0].job_url == ""


def test_infrastructure_failure_precedes_code_capability():
    jobs = analyze_failed_jobs(
        [_job()],
        lambda _job_id: "fatal: unable to access registry: connection timed out",
        pipeline_id=91,
    )

    assert jobs[0].family is FailureFamily.INFRASTRUCTURE
    assert jobs[0].capability is CapabilityClass.INFRASTRUCTURE
    assert jobs[0].capability_basis == "infrastructure_pattern"


def test_active_memory_with_exact_fingerprint_marks_supported(tmp_path):
    db_path = str(tmp_path / "memory.db")
    first = analyze_failed_jobs(
        [_job()],
        lambda _job_id: "error: undefined reference to WidgetFactory",
        pipeline_id=91,
    )[0]
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE repair_memories (diagnostic_fingerprint TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO repair_memories VALUES (?, 'active')",
        (first.fingerprint,),
    )
    conn.commit()
    conn.close()

    supported = analyze_failed_jobs(
        [_job()],
        lambda _job_id: "error: undefined reference to WidgetFactory",
        pipeline_id=91,
        memory_path=db_path,
    )[0]

    assert supported.capability is CapabilityClass.SUPPORTED
    assert supported.capability_basis == "verified_memory_exact_fingerprint"
    assert supported.capability_confidence == "high"


def test_analysis_respects_job_and_trace_budgets(monkeypatch):
    monkeypatch.setattr(
        "pr_agent.triage.ci_failure_analysis._analysis_settings",
        lambda: {"trace_job_limit": 1, "trace_bytes_limit": 24},
    )
    called = []

    jobs = analyze_failed_jobs(
        [_job(11, "build_release"), _job(12, "build_debug")],
        lambda job_id: called.append(job_id) or ("error: " + "x" * 100),
        pipeline_id=91,
    )

    assert called == [11]
    assert jobs[0].confirmed_reason.startswith("error:")
    assert jobs[1].confirmed_reason == ""


def test_aggregate_failure_counts_unknown_and_picks_primary_reason():
    jobs = analyze_failed_jobs(
        [_job(11, "build"), _job(12, "misc")],
        lambda job_id: "error: compile failed" if job_id == 11 else "",
        pipeline_id=91,
    )

    aggregate = aggregate_failure(jobs)

    assert aggregate.failed_job_count == 2
    assert aggregate.unknown_reason_count == 1
    assert aggregate.primary_reason == "error: compile failed"
    assert aggregate.primary_fingerprint == jobs[0].fingerprint
    assert aggregate.categories == ("build", "unknown")
