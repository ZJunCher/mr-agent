import sqlite3

from scripts.reconcile_ci_failures import _failed_jobs, reconcile_ci_failures


def _inventory(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE mr_inventory (
            project_id TEXT, project_path TEXT, mr_iid TEXT, mr_url TEXT, title TEXT, author TEXT,
            source_branch TEXT, target_branch TEXT, state TEXT, updated_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO mr_inventory VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("23", "eabot/cook", "551", "https://gitlab.example/mr/551", "Fix build", "alice",
         "fix/build", "dev", "opened", "2026-08-20T09:00:00+08:00"),
    )
    conn.commit()
    conn.close()


def _api(path: str, _params=None):
    if path.endswith("/merge_requests/551/pipelines"):
        return [{
            "id": 91,
            "status": "failed",
            "sha": "a" * 40,
            "web_url": "https://gitlab.example/pipelines/91",
            "created_at": "2026-08-20T09:10:00+08:00",
        }]
    if path.endswith("/pipelines/91/jobs"):
        return [{
            "id": 11,
            "name": "build_release",
            "stage": "build",
            "status": "failed",
            "web_url": "https://gitlab.example/jobs/11",
            "pipeline": {"id": 91},
        }]
    if path.endswith("/pipelines/91/bridges"):
        return []
    if path.endswith("/jobs/11/trace"):
        return b"error: undefined reference to SensorFactory"
    raise AssertionError(path)


def test_reconcile_defaults_to_dry_run_without_writes(tmp_path):
    path = str(tmp_path / "feedback.db")
    _inventory(path)

    result = reconcile_ci_failures(
        _api,
        path=path,
        days=30,
        max_mrs=10,
        max_pipelines=10,
        max_traces=10,
        apply=False,
    )

    assert result["candidates"] == 1
    assert result["inserted"] == 0
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ci_failure_pipelines'"
    ).fetchone() is None
    conn.close()


def test_reconcile_apply_persists_with_webhook_unique_key(tmp_path):
    path = str(tmp_path / "feedback.db")
    _inventory(path)

    first = reconcile_ci_failures(_api, path=path, apply=True)
    second = reconcile_ci_failures(_api, path=path, apply=True)

    assert first["inserted"] == 1
    assert second["updated"] == 1
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT project_id, mr_iid, pipeline_id, source FROM ci_failure_pipelines"
    ).fetchone()
    conn.close()
    assert row == ("23", "551", 91, "reconcile")


def test_reconcile_honors_pipeline_and_trace_budgets(tmp_path):
    path = str(tmp_path / "feedback.db")
    _inventory(path)
    trace_calls = []

    def api(path_value: str, params=None):
        if path_value.endswith("/jobs/11/trace"):
            trace_calls.append(path_value)
        return _api(path_value, params)

    result = reconcile_ci_failures(
        api,
        path=path,
        max_mrs=1,
        max_pipelines=1,
        max_traces=0,
        apply=False,
    )

    assert result["pipelines_scanned"] == 1
    assert result["traces_fetched"] == 0
    assert trace_calls == []


def test_failed_jobs_uses_downstream_project_id():
    calls = []

    def api(path: str, _params=None):
        calls.append(path)
        if path == "/api/v4/projects/23/pipelines/91/jobs":
            return []
        if path == "/api/v4/projects/23/pipelines/91/bridges":
            return [{"downstream_pipeline": {"id": 92, "project_id": 42}}]
        if path == "/api/v4/projects/42/pipelines/92/jobs":
            return [{"id": 12, "name": "child_build", "status": "failed"}]
        if path == "/api/v4/projects/42/pipelines/92/bridges":
            return []
        raise AssertionError(path)

    assert [job["name"] for job in _failed_jobs(api, "23", 91)] == ["child_build"]
    assert "/api/v4/projects/42/pipelines/92/jobs" in calls
