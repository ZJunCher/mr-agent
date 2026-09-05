from fastapi import FastAPI
from fastapi.testclient import TestClient

from pr_agent.servers.dashboard_routes import router
from pr_agent.triage.ci_failure_analysis import aggregate_failure, analyze_failed_jobs
from pr_agent.triage.ci_failure_store import save_ci_failure


def _seed(path: str) -> int:
    jobs = analyze_failed_jobs(
        [{
            "id": 11,
            "name": "build_release",
            "stage": "build",
            "web_url": "https://gitlab.example/jobs/11",
            "pipeline": {"id": 91},
        }],
        lambda _job_id: "error: undefined reference to WidgetFactory",
        pipeline_id=91,
    )
    return save_ci_failure(
        {
            "project_id": "23",
            "project_path": "eabot/cook",
            "mr_iid": "551",
            "mr_url": "https://gitlab.example/eabot/cook/-/merge_requests/551",
            "mr_title": "Fix build",
            "mr_author": "alice",
            "pipeline_id": 91,
            "pipeline_url": "https://gitlab.example/pipelines/91",
            "pipeline_sha": "a" * 40,
            "card_id": "card-1",
        },
        jobs,
        aggregate=aggregate_failure(jobs),
        path=path,
    )


def _client(path: str, monkeypatch) -> TestClient:
    monkeypatch.setattr("pr_agent.servers.ci_failure_dashboard.get_feedback_db_path", lambda: path)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_summary_and_detail_apis(tmp_path, monkeypatch):
    path = str(tmp_path / "feedback.db")
    failure_id = _seed(path)
    client = _client(path, monkeypatch)

    summary = client.get("/api/ci-failures/summary?days=0&page=1&page_size=20").json()
    detail = client.get(f"/api/ci-failures/{failure_id}").json()

    assert summary["metrics"]["failed_pipelines"] == 1
    assert summary["metrics"]["failed_jobs"] == 1
    assert "recovery_rate" not in summary["metrics"]
    assert detail["jobs"][0]["effective_reason"] == "error: undefined reference to WidgetFactory"


def test_summary_filters_and_clamps_page_size(tmp_path, monkeypatch):
    path = str(tmp_path / "feedback.db")
    _seed(path)
    client = _client(path, monkeypatch)

    payload = client.get(
        "/api/ci-failures/summary?days=0&project=eabot/cook&family=build&capability=capability_gap"
        "&page=2&page_size=999&recurring_page=3&recurring_page_size=999"
        "&project_distribution_page=4&project_distribution_page_size=999"
        "&job_distribution_page=5&job_distribution_page_size=999"
    ).json()

    assert payload["total"] == 1
    assert payload["page"] == 2
    assert payload["page_size"] == 100
    assert payload["recurring_page"] == 3
    assert payload["recurring_page_size"] == 100
    assert payload["total_pages"] == 1
    assert payload["recurring_total_pages"] == 0
    assert payload["project_distribution_page"] == 4
    assert payload["project_distribution_page_size"] == 100
    assert payload["job_distribution_page"] == 5
    assert payload["job_distribution_page_size"] == 100


def test_annotation_api_validates_and_preserves_system_value(tmp_path, monkeypatch):
    path = str(tmp_path / "feedback.db")
    failure_id = _seed(path)
    client = _client(path, monkeypatch)
    job_id = client.get(f"/api/ci-failures/{failure_id}").json()["jobs"][0]["id"]

    response = client.post(
        f"/api/ci-failures/{failure_id}/annotations",
        json={
            "job_id": job_id,
            "reason": "实际为依赖服务失败",
            "capability": "infrastructure",
            "note": "人工复核",
        },
    )
    invalid = client.post(
        f"/api/ci-failures/{failure_id}/annotations",
        json={"capability": "magic", "unknown": "field"},
    )

    assert response.status_code == 200
    job = response.json()["failure"]["jobs"][0]
    assert job["system_capability"] == "capability_gap"
    assert job["effective_capability"] == "infrastructure"
    assert invalid.status_code == 422


def test_unknown_failure_returns_404(tmp_path, monkeypatch):
    client = _client(str(tmp_path / "feedback.db"), monkeypatch)
    assert client.get("/api/ci-failures/999").status_code == 404


def test_dashboard_uses_approved_hybrid_layout_and_existing_style():
    from pr_agent.servers.dashboard_routes import _ci_failure_dashboard_html

    html = _ci_failure_dashboard_html()

    for text in (
        "CI 失败分析",
        "失败 Pipeline",
        "失败 Job",
        "未明确原因",
        "重复错误模式",
        "失败趋势",
        "失败类别",
        "高频错误模式",
        "失败 Pipeline 明细",
        "人工修正",
    ):
        assert text in html
    assert "恢复率" not in html
    assert "ops-dashboard" in html
    assert "/api/ci-failures/summary" in html
    assert "/api/ci-failures/" in html
    for marker in (
        '<body class="ops-dashboard ops-dashboard-light">',
        'class="ci-filter-label"',
        'id="recurringPrev"',
        'id="recurringNext"',
        'id="recurringPageInfo"',
        'id="pipelinePrev"',
        'id="pipelineNext"',
        'id="pipelinePageInfo"',
        "let pipelinePage=1, recurringPage=1",
        "p.set('page_size','15')",
        "p.set('recurring_page_size','5')",
        'id="distributionPrev"',
        'id="distributionNext"',
        'id="distributionPageInfo"',
        "let projectDistributionPage=1, jobDistributionPage=1",
        "p.set('project_distribution_page_size','5')",
        "p.set('job_distribution_page_size','5')",
        'aria-live="polite"',
    ):
        assert marker in html
    assert "p.set('page_size','50')" not in html
