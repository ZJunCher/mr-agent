"""dashboard_routes triage 看板单测。"""
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def populated_db(tmp_path: Path) -> str:
    db = str(tmp_path / "triage_dashboard.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE triage_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            pr_url TEXT, project TEXT, mr_iid TEXT, mr_author TEXT,
            source_branch TEXT, target_branch TEXT, commit_sha TEXT, pipeline_id TEXT,
            trigger_type TEXT, failed_job_names TEXT, failure_categories TEXT,
            success INTEGER, finish_reason TEXT, iterations INTEGER, max_iterations INTEGER,
            pushed_sha TEXT, final_pipeline_status TEXT, failure_signatures TEXT,
            fix_duration_ms INTEGER, model TEXT, error TEXT, extra_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE mr_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            project_path TEXT NOT NULL,
            mr_iid TEXT NOT NULL,
            author TEXT,
            UNIQUE(project_path, mr_iid)
        )
    """)
    rows = [
        ("2026-08-01T10:00:00+08:00", "g/r", "1", "build", 1, 5, 120000),
        ("2026-08-02T11:00:00+08:00", "g/r", "2", "format", 0, 30, 600000),
        ("2026-08-03T12:00:00+08:00", "g/r2", "3", "build", 1, 8, 200000),
    ]
    for ts, proj, iid, cat, succ, iters, dur in rows:
        conn.execute(
            "INSERT INTO triage_runs (created_at, pr_url, project, mr_iid, mr_author, "
            "failure_categories, success, iterations, fix_duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, f"https://gitlab/{proj}/-/merge_requests/{iid}", proj, iid, "alice",
             json.dumps([cat]), succ, iters, dur),
        )
    conn.commit()
    conn.close()
    return db


def test_collect_triage_summary(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=None, project=None)

    assert result["total"] == 3
    assert result["success_rate"] == round(2 / 3 * 100, 1)
    assert "build" in result["cat_labels"]
    assert len(result["recent_rows"]) == 3
    assert result["recent_rows"][0]["success"] in (0, 1)
    assert result["recent_rows"][0]["actor"] == "alice"


def test_collect_triage_summary_paginates_all_recent_rows(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    for index in range(4, 33):
        conn.execute(
            "INSERT INTO triage_runs (created_at, pr_url, project, mr_iid, mr_author, "
            "failure_categories, success, iterations, fix_duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"2026-08-04T12:{index - 4:02d}:00+08:00",
                f"https://gitlab/g/r/-/merge_requests/{index}",
                "g/r",
                str(index),
                "alice",
                json.dumps(["build"]),
                index % 2,
                index,
                index * 1000,
            ),
        )
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        first = collect_triage_summary(days=None, page=1)
        second = collect_triage_summary(days=None, page=2)
        last = collect_triage_summary(days=None, page=3)
        overflow = collect_triage_summary(days=None, page=99)

    assert len(first["recent_rows"]) == 15
    assert len(second["recent_rows"]) == 15
    assert len(last["recent_rows"]) == 2
    assert first["recent_total"] == 32
    assert first["recent_total_pages"] == 3
    assert first["recent_page_size"] == 15
    assert first["recent_page"] == 1
    assert second["recent_page"] == 2
    assert overflow["recent_page"] == 3
    assert overflow["recent_rows"] == last["recent_rows"]
    assert first["total"] == second["total"] == last["total"] == 32
    assert set(row["mr_iid"] for row in first["recent_rows"]).isdisjoint(
        row["mr_iid"] for row in second["recent_rows"]
    )


def test_post_repair_ut_is_visible_but_excluded_from_repair_aggregates(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    conn.execute("ALTER TABLE triage_runs ADD COLUMN repair_outcome TEXT")
    conn.execute("ALTER TABLE triage_runs ADD COLUMN category_results TEXT")
    conn.execute(
        "INSERT INTO triage_runs (created_at, pr_url, project, mr_iid, mr_author, trigger_type, "
        "failure_categories, success, repair_outcome, iterations, fix_duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-08-04T12:00:00+08:00",
            "https://gitlab/g/r/-/merge_requests/9",
            "g/r",
            "9",
            "alice",
            "post_repair_ut",
            json.dumps(["unit_test"]),
            1,
            "partial",
            6,
            1000,
        ),
    )
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=None)

    assert result["total"] == 3
    assert result["success_rate"] == round(2 / 3 * 100, 1)
    assert result["recent_rows"][0]["trigger_type"] == "post_repair_ut"
    assert result["recent_rows"][0]["cats"] == ["unit_test"]
    assert "unit_test" not in result["cat_labels"]


def test_dashboard_exposes_signed_same_origin_embedded_repair_detail(populated_db, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from pr_agent.servers.dashboard_routes import collect_triage_summary
    from pr_agent.triage.repair_details import verify_repair_details_signature

    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_ENABLED", "true")
    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_SIGNING_SECRET", "test-secret-with-enough-entropy")
    conn = sqlite3.connect(populated_db)
    conn.execute("ALTER TABLE triage_runs ADD COLUMN task_id TEXT")
    conn.execute("UPDATE triage_runs SET task_id = ? WHERE mr_iid = ?", ("task-12345678", "3"))
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        row = collect_triage_summary(days=None)["recent_rows"][0]

    parsed = urlparse(row["detail_url"])
    query = parse_qs(parsed.query)
    assert row["task_id"] == "task-12345678"
    assert row["detail_available"] is True
    assert parsed.path == "/repair-results/task-12345678"
    assert query["embed"] == ["1"]
    assert verify_repair_details_signature(row["task_id"], query["sig"][0])
    assert row["detail_unavailable_reason"] == ""


def test_dashboard_marks_legacy_row_without_inventing_details(populated_db, monkeypatch):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_ENABLED", "true")
    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_SIGNING_SECRET", "test-secret-with-enough-entropy")

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        row = collect_triage_summary(days=None)["recent_rows"][0]

    assert row["task_id"] == ""
    assert row["detail_available"] is False
    assert row["detail_url"] == ""
    assert row["detail_unavailable_reason"] == "该记录生成时未保存修复详情。"


def test_dashboard_exposes_coverage_status_from_extra_json(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    conn.execute("ALTER TABLE triage_runs ADD COLUMN final_coverage REAL")
    conn.execute(
        "UPDATE triage_runs SET extra_json = ?, final_coverage = NULL WHERE mr_iid = '3'",
        (json.dumps({"coverage_source": "", "coverage_status": "job_failed"}),),
    )
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        row = collect_triage_summary(days=None, project=None)["recent_rows"][0]

    assert row["coverage_source"] == ""
    assert row["coverage_status"] == "job_failed"


def test_dashboard_distinguishes_partial_repair_from_pipeline_status(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    conn.execute("ALTER TABLE triage_runs ADD COLUMN repair_outcome TEXT")
    conn.execute("ALTER TABLE triage_runs ADD COLUMN category_results TEXT")
    conn.execute(
        "UPDATE triage_runs SET repair_outcome = 'partial_success', final_pipeline_status = 'failed', success = 0 "
        "WHERE mr_iid = '3'"
    )
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=None, project=None)

    assert result["partial_count"] == 1
    assert result["recent_rows"][0]["repair_outcome"] == "partial_success"
    assert result["recent_rows"][0]["pipeline_status"] == "failed"
    assert result["recent_rows"][0]["success"] == 0


def test_dashboard_excludes_blockers_from_success_rate_and_reports_them_separately(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    conn.execute("ALTER TABLE triage_runs ADD COLUMN repair_outcome TEXT")
    conn.execute("ALTER TABLE triage_runs ADD COLUMN category_results TEXT")
    conn.execute("UPDATE triage_runs SET repair_outcome = 'success' WHERE mr_iid = '1'")
    conn.execute("UPDATE triage_runs SET repair_outcome = 'failed' WHERE mr_iid = '2'")
    conn.execute(
        "UPDATE triage_runs SET repair_outcome = 'partial_success', success = 0 WHERE mr_iid = '3'"
    )
    conn.execute(
        "UPDATE triage_runs SET created_at = '2026-08-04T09:00:00+08:00' WHERE mr_iid = '1'"
    )
    conn.execute(
        "UPDATE triage_runs SET created_at = '2026-08-04T10:00:00+08:00' WHERE mr_iid = '2'"
    )
    conn.execute(
        "UPDATE triage_runs SET created_at = '2026-08-04T11:00:00+08:00' WHERE mr_iid = '3'"
    )
    conn.execute(
        "INSERT INTO triage_runs (created_at, pr_url, project, mr_iid, mr_author, failure_categories, success, "
        "repair_outcome, category_results, iterations, fix_duration_ms, final_pipeline_status, extra_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-08-04T12:00:00+08:00",
            "https://gitlab/g/r2/-/merge_requests/4",
            "g/r2",
            "4",
            "bob",
            json.dumps(["build"]),
            0,
            "blocked",
            json.dumps([{"category": "build", "outcome": "blocked", "selection": "selected"}]),
            4,
            228400,
            "failed",
            json.dumps({
                "blocker_type": "external_dependency",
                "blocker_summary": "当前声明分支缺少接口。" + "x" * 600,
                "blocker_suggested_action": "请维护者确认候选依赖分支。",
            }),
        ),
    )
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=None, project=None)

    assert result["total"] == 4
    assert result["blocked_count"] == 1
    assert result["success_rate"] == 33.3
    build_index = result["cat_labels"].index("build")
    assert result["cat_values"][build_index] == 3
    assert result["cat_sr"][build_index] == 50.0
    assert result["week_values"] == [33.3]
    assert result["recent_rows"][0]["repair_outcome"] == "blocked"
    assert result["recent_rows"][0]["pipeline_status"] == "failed"
    assert result["recent_rows"][0]["blocker_type"] == "external_dependency"
    assert len(result["recent_rows"][0]["blocker_summary"]) == 500
    assert result["recent_rows"][0]["blocker_suggested_action"] == "请维护者确认候选依赖分支。"


def test_dashboard_all_blocked_interval_has_zero_rate_without_division_error(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    conn.execute("ALTER TABLE triage_runs ADD COLUMN repair_outcome TEXT")
    conn.execute("ALTER TABLE triage_runs ADD COLUMN category_results TEXT")
    conn.execute("UPDATE triage_runs SET repair_outcome = 'blocked', success = 0")
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=None, project=None)

    assert result["total"] == 3
    assert result["blocked_count"] == 3
    assert result["success_rate"] == 0
    assert all(value == 0 for value in result["cat_sr"])
    assert all(value == 0 for value in result["week_values"])


def test_collect_triage_summary_empty_db(tmp_path):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    db = str(tmp_path / "empty.db")
    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=db):
        result = collect_triage_summary(days=None, project=None)

    assert result["total"] == 0
    assert result["recent_rows"] == []
    assert result["recent_page"] == 1
    assert result["recent_page_size"] == 15
    assert result["recent_total"] == 0
    assert result["recent_total_pages"] == 0


def test_dashboard_ignores_legacy_feishu_actor(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    conn.execute("ALTER TABLE triage_runs ADD COLUMN feishu_user_name TEXT")
    conn.execute("UPDATE triage_runs SET feishu_user_name = ? WHERE mr_iid = ?", ("赵军", "3"))
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=None, project=None)

    assert result["recent_rows"][0]["actor"] == "alice"


def test_dashboard_prefers_run_author_then_inventory(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    conn.execute(
        "INSERT INTO mr_inventory (project_id, project_path, mr_iid, author) VALUES (?, ?, ?, ?)",
        ("235", "g/r2", "3", "inventory.user"),
    )
    conn.execute("UPDATE triage_runs SET mr_author = ? WHERE mr_iid = ?", ("run.user", "3"))
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=None, project=None)

    assert result["recent_rows"][0]["actor"] == "run.user"


def test_dashboard_uses_inventory_for_historical_outer_repair(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    conn.execute("UPDATE triage_runs SET mr_author = NULL WHERE mr_iid = ?", ("3",))
    conn.execute(
        "INSERT INTO mr_inventory (project_id, project_path, mr_iid, author) VALUES (?, ?, ?, ?)",
        ("235", "g/r2", "3", "xiaoyu.li"),
    )
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=None, project=None)

    assert result["recent_rows"][0]["actor"] == "xiaoyu.li"


def test_dashboard_without_inventory_uses_run_author(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    conn.execute("DROP TABLE mr_inventory")
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=None, project=None)

    assert result["recent_rows"][0]["actor"] == "alice"


def test_dashboard_treats_unknown_as_missing_and_uses_inventory(populated_db):
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    conn = sqlite3.connect(populated_db)
    conn.execute("UPDATE triage_runs SET mr_author = 'unknown' WHERE mr_iid = '3'")
    conn.execute(
        "INSERT INTO mr_inventory (project_id, project_path, mr_iid, author) VALUES (?, ?, ?, ?)",
        ("235", "g/r2", "3", "xiaoyu.li"),
    )
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=None, project=None)

    assert result["recent_rows"][0]["actor"] == "xiaoyu.li"


def test_collect_triage_summary_never_raises():
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", side_effect=Exception("boom")):
        result = collect_triage_summary(days=None, project=None)

    assert result["total"] == 0  # 不抛异常，返回空 payload


def test_collect_triage_summary_with_days_filter(populated_db):
    """days 过滤不能因 json_each 的 SQL 语法报错导致 total=0。"""
    from pr_agent.servers.dashboard_routes import collect_triage_summary

    with patch("pr_agent.servers.dashboard_routes.get_feedback_db_path", return_value=populated_db):
        result = collect_triage_summary(days=30, project=None)

    assert result["total"] == 3  # days=30 应该能查到全部 3 条记录
    assert len(result["cat_labels"]) > 0  # json_each 查询不报错


def test_triage_dashboard_maps_coverage_absence_reasons():
    from pr_agent.servers.dashboard_routes import _triage_dashboard_html

    html = _triage_dashboard_html()

    for text in (
        "未配置覆盖率任务",
        "覆盖率任务失败，未生成报告",
        "覆盖率报告缺失",
        "覆盖率读取失败",
        "未找到验证流水线",
        "未提供",
    ):
        assert text in html


def test_triage_dashboard_contains_lazy_single_row_repair_disclosure():
    from pr_agent.servers.dashboard_routes import _triage_dashboard_html

    html = _triage_dashboard_html()

    assert "toggleRepairDetail" in html
    assert "closeRepairDetail" in html
    assert "triage-detail-row" in html
    assert "colSpan = 11" in html
    assert 'iframe.loading = "lazy"' in html
    assert "detail_available" in html
    assert "detail_unavailable_reason" in html
    assert "repair-detail-height" in html
    assert "aria-expanded" in html
    assert html.index("iframe.onload =") < html.index("iframe.src = row.detail_url")


def test_triage_dashboard_contains_server_side_recent_runs_pager():
    from pr_agent.servers.dashboard_routes import _triage_dashboard_html

    html = _triage_dashboard_html()

    assert 'id="triagePageInfo"' in html
    assert 'id="triagePageNumbers"' in html
    assert 'id="triagePrevPage"' in html
    assert 'id="triageNextPage"' in html
    assert "loadRecentPage" in html
    assert "renderRecentRows" in html
    assert "renderRecentPager" in html
    assert "page=${encodeURIComponent(page)}" in html
    assert "closeRepairDetail()" in html
    assert "recentRequestId" in html


def test_triage_dashboard_keeps_gitlab_link_outside_row_toggle():
    from pr_agent.servers.dashboard_routes import _triage_dashboard_html

    html = _triage_dashboard_html()

    assert "event.stopPropagation()" in html
    assert "查看" in html
    assert "keydown" in html
    assert "event.key === 'Enter'" in html
    assert "event.key === ' '" in html
