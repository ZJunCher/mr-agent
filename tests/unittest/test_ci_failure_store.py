import sqlite3

import pytest

from pr_agent.triage.ci_failure_analysis import (
    CapabilityClass,
    FailureFamily,
    aggregate_failure,
    analyze_failed_jobs,
)
from pr_agent.triage.ci_failure_store import (
    get_ci_failure,
    init_ci_failure_tables,
    query_ci_failures,
    record_followup_pipeline,
    save_annotation,
    save_ci_failure,
    update_notification_state,
)


def _analysis(job_id: int = 11, reason: str = "error: undefined reference to Widget"):
    jobs = analyze_failed_jobs(
        [{
            "id": job_id,
            "name": "build_release",
            "stage": "build",
            "web_url": f"https://gitlab.example/jobs/{job_id}",
            "pipeline": {"id": 91},
        }],
        lambda _job_id: reason,
        pipeline_id=91,
    )
    return jobs, aggregate_failure(jobs)


def _record(**overrides) -> dict:
    value = {
        "project_id": "23",
        "project_path": "eabot/cook",
        "mr_iid": "551",
        "mr_url": "https://gitlab.example/eabot/cook/-/merge_requests/551",
        "mr_title": "Fix build",
        "mr_author": "alice",
        "source_branch": "fix/build",
        "target_branch": "dev",
        "pipeline_id": 91,
        "pipeline_url": "https://gitlab.example/eabot/cook/-/pipelines/91",
        "pipeline_sha": "a" * 40,
        "pipeline_status": "failed",
        "notification_state": "not_attempted",
        "card_id": "eabot-cook-551-91",
        "source": "webhook",
    }
    value.update(overrides)
    return value


def test_schema_is_additive_and_idempotent(tmp_path):
    path = str(tmp_path / "feedback.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE review_feedback (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    init_ci_failure_tables(path)
    init_ci_failure_tables(path)

    conn = sqlite3.connect(path)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"review_feedback", "ci_failure_pipelines", "ci_failure_jobs", "ci_failure_annotations"} <= names


def test_save_is_idempotent_and_updates_jobs(tmp_path):
    path = str(tmp_path / "feedback.db")
    jobs, aggregate = _analysis()
    failure_id = save_ci_failure(_record(), jobs, aggregate=aggregate, path=path)
    changed_jobs, changed_aggregate = _analysis(reason="fatal: WidgetFactory is missing")
    duplicate_id = save_ci_failure(
        _record(mr_title="Updated title"), changed_jobs, aggregate=changed_aggregate, path=path
    )

    assert duplicate_id == failure_id
    detail = get_ci_failure(failure_id, path=path)
    assert detail["mr_title"] == "Updated title"
    assert len(detail["jobs"]) == 1
    assert detail["jobs"][0]["system_reason"] == "fatal: WidgetFactory is missing"


def test_notification_and_followup_updates(tmp_path):
    path = str(tmp_path / "feedback.db")
    jobs, aggregate = _analysis()
    failure_id = save_ci_failure(_record(), jobs, aggregate=aggregate, path=path)

    assert update_notification_state("eabot-cook-551-91", "delivered", path=path)
    assert record_followup_pipeline(
        "23", "551", 92, "a" * 40, "success", path=path
    ) == 1

    detail = get_ci_failure(failure_id, path=path)
    assert detail["notification_state"] == "delivered"
    assert detail["followup_state"] == "same_sha_success"
    assert detail["followup_pipeline_id"] == 92


def test_annotations_preserve_system_values_and_override_effective_values(tmp_path):
    path = str(tmp_path / "feedback.db")
    jobs, aggregate = _analysis()
    failure_id = save_ci_failure(_record(), jobs, aggregate=aggregate, path=path)
    job_id = get_ci_failure(failure_id, path=path)["jobs"][0]["id"]

    assert save_annotation(
        failure_id,
        job_id=job_id,
        reason="实际是依赖服务不可用",
        capability="infrastructure",
        note="已通过重跑验证",
        path=path,
    )

    job = get_ci_failure(failure_id, path=path)["jobs"][0]
    assert job["system_capability"] == CapabilityClass.CAPABILITY_GAP.value
    assert job["manual_capability"] == CapabilityClass.INFRASTRUCTURE.value
    assert job["effective_capability"] == CapabilityClass.INFRASTRUCTURE.value
    assert job["effective_reason"] == "实际是依赖服务不可用"
    assert job["note"] == "已通过重跑验证"

    with pytest.raises(ValueError, match="capability"):
        save_annotation(failure_id, job_id=job_id, capability="magic", path=path)


def test_query_returns_approved_metrics_and_recurring_patterns(tmp_path):
    path = str(tmp_path / "feedback.db")
    jobs, aggregate = _analysis()
    save_ci_failure(_record(), jobs, aggregate=aggregate, path=path)
    second_jobs, second_aggregate = _analysis(job_id=12)
    save_ci_failure(
        _record(mr_iid="552", pipeline_id=92, card_id="eabot-cook-552-92"),
        second_jobs,
        aggregate=second_aggregate,
        path=path,
    )
    unknown_jobs, unknown_aggregate = _analysis(job_id=13, reason="")
    save_ci_failure(
        _record(project_id="24", project_path="eabot/pad", mr_iid="9", pipeline_id=93, card_id="pad-9-93"),
        unknown_jobs,
        aggregate=unknown_aggregate,
        path=path,
    )

    result = query_ci_failures({"days": None, "page": 1, "page_size": 20}, path=path)

    assert result["metrics"] == {
        "failed_pipelines": 3,
        "failed_jobs": 3,
        "unknown_reason_jobs": 1,
        "recurring_patterns": 1,
    }
    assert result["total"] == 3
    assert len(result["recurring"]) == 1
    assert result["recurring"][0]["occurrences"] == 2
    assert "recovery_rate" not in result["metrics"]


def test_query_paginates_recurring_patterns_independently(tmp_path):
    path = str(tmp_path / "feedback.db")
    for pattern in range(7):
        for occurrence in range(2):
            job_id = 100 + pattern * 2 + occurrence
            jobs, aggregate = _analysis(
                job_id=job_id,
                reason=f"error: distinct failure pattern {pattern}",
            )
            save_ci_failure(
                _record(
                    mr_iid=str(600 + job_id),
                    pipeline_id=200 + job_id,
                    card_id=f"pattern-{pattern}-{occurrence}",
                ),
                jobs,
                aggregate=aggregate,
                path=path,
            )

    first = query_ci_failures(
        {"days": None, "page": 1, "page_size": 15, "recurring_page": 1, "recurring_page_size": 5},
        path=path,
    )
    second = query_ci_failures(
        {"days": None, "page": 1, "page_size": 15, "recurring_page": 2, "recurring_page_size": 5},
        path=path,
    )
    overflow = query_ci_failures(
        {"days": None, "page": 1, "page_size": 15, "recurring_page": 3, "recurring_page_size": 5},
        path=path,
    )

    assert first["recurring_page"] == 1
    assert first["recurring_page_size"] == 5
    assert first["recurring_total"] == 7
    assert first["recurring_total_pages"] == 2
    assert first["total_pages"] == 1
    assert len(first["recurring"]) == 5
    assert len(second["recurring"]) == 2
    assert {row["fingerprint"] for row in first["recurring"]}.isdisjoint(
        row["fingerprint"] for row in second["recurring"]
    )
    assert overflow["recurring_page"] == 3
    assert overflow["recurring"] == []


def test_query_paginates_project_and_job_distributions_independently(tmp_path):
    path = str(tmp_path / "feedback.db")
    for index in range(7):
        job_id = 300 + index
        jobs = analyze_failed_jobs(
            [{
                "id": job_id,
                "name": f"build_variant_{index}",
                "stage": "build",
                "pipeline": {"id": 400 + index},
            }],
            lambda _job_id, value=index: f"error: distribution failure {value}",
            pipeline_id=400 + index,
        )
        save_ci_failure(
            _record(
                project_id=str(100 + index),
                project_path=f"eabot/project-{index}",
                mr_iid=str(700 + index),
                pipeline_id=400 + index,
                card_id=f"distribution-{index}",
            ),
            jobs,
            aggregate=aggregate_failure(jobs),
            path=path,
        )

    first = query_ci_failures({
        "days": None,
        "project_distribution_page": 1,
        "project_distribution_page_size": 5,
        "job_distribution_page": 1,
        "job_distribution_page_size": 5,
    }, path=path)
    second = query_ci_failures({
        "days": None,
        "project_distribution_page": 2,
        "project_distribution_page_size": 5,
        "job_distribution_page": 2,
        "job_distribution_page_size": 5,
    }, path=path)

    assert first["project_distribution_total"] == 7
    assert first["project_distribution_total_pages"] == 2
    assert first["project_distribution_page_size"] == 5
    assert first["job_distribution_total"] == 7
    assert first["job_distribution_total_pages"] == 2
    assert first["job_distribution_page_size"] == 5
    assert len(first["top_projects"]) == len(first["top_jobs"]) == 5
    assert len(second["top_projects"]) == len(second["top_jobs"]) == 2
    assert {row["project_path"] for row in first["top_projects"]}.isdisjoint(
        row["project_path"] for row in second["top_projects"]
    )
    assert {row["job_name"] for row in first["top_jobs"]}.isdisjoint(
        row["job_name"] for row in second["top_jobs"]
    )


def test_query_filters_project_family_and_capability(tmp_path):
    path = str(tmp_path / "feedback.db")
    jobs, aggregate = _analysis()
    save_ci_failure(_record(), jobs, aggregate=aggregate, path=path)
    infra_jobs = analyze_failed_jobs(
        [{"id": 12, "name": "build", "pipeline": {"id": 92}}],
        lambda _job_id: "fatal: connection timed out",
        pipeline_id=92,
    )
    save_ci_failure(
        _record(project_id="24", project_path="eabot/pad", mr_iid="9", pipeline_id=92, card_id="pad-9-92"),
        infra_jobs,
        aggregate=aggregate_failure(infra_jobs),
        path=path,
    )

    result = query_ci_failures(
        {"project": "eabot/pad", "family": FailureFamily.INFRASTRUCTURE.value,
         "capability": CapabilityClass.INFRASTRUCTURE.value, "page": 1, "page_size": 20},
        path=path,
    )

    assert result["total"] == 1
    assert result["rows"][0]["project_path"] == "eabot/pad"
