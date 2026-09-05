"""dashboard_routes suggestion-filter 看板数据采集单测。"""
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def populated_db(tmp_path: Path) -> str:
    db = str(tmp_path / "sf.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE published_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, suggestion_id TEXT, review_id TEXT,
            project TEXT, mr_iid TEXT, mr_url TEXT, mr_author TEXT,
            commit_sha TEXT, file_path TEXT, line_start INTEGER, line_end INTEGER,
            label TEXT, severity TEXT, score INTEGER, one_sentence_summary TEXT,
            suggestion_content TEXT, existing_code TEXT, improved_code TEXT,
            gitlab_discussion_id TEXT, gitlab_note_id TEXT, state TEXT,
            extra_json TEXT, applied_at TEXT, apply_user TEXT,
            resolved_by_stage TEXT, tier2_duration_ms INTEGER
        );
        CREATE TABLE filtered_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, review_id TEXT, project TEXT, mr_iid TEXT,
            mr_url TEXT, mr_author TEXT, commit_sha TEXT, file_path TEXT,
            line_start INTEGER, line_end INTEGER, label TEXT, severity TEXT,
            score INTEGER, one_sentence_summary TEXT, suggestion_content TEXT,
            existing_code TEXT, improved_code TEXT, filter_stage TEXT,
            skip_reason TEXT, judge_model TEXT, extra_json TEXT
        );
    """)
    # 2 published, 3 filtered across 2 MRs
    conn.execute(
        "INSERT INTO published_suggestions (created_at, project, mr_iid, mr_url, mr_author, "
        "file_path, label, score, one_sentence_summary) VALUES "
        "('2026-08-01T10:00:00+08:00','g/r','10','https://gl/g/r/-/merge_requests/10','alice','a.go','bug',8,'s1'),"
        "('2026-08-02T10:00:00+08:00','g/r','10','https://gl/g/r/-/merge_requests/10','alice','b.go','bug',7,'s2')"
    )
    conn.execute(
        "INSERT INTO filtered_suggestions (created_at, project, mr_iid, mr_url, mr_author, "
        "file_path, label, score, one_sentence_summary, suggestion_content, skip_reason, judge_model, filter_stage) VALUES "
        "('2026-08-01T11:00:00+08:00','g/r','10','https://gl/g/r/-/merge_requests/10','alice','c.go','bug',9,'f1','why1','scenario_invalid','opus','scenario_validation'),"
        "('2026-08-01T12:00:00+08:00','g/r','10','https://gl/g/r/-/merge_requests/10','alice','d.go','bug',6,'f2','why2','scenario_missing_prefix','opus','scenario_validation'),"
        "('2026-08-03T10:00:00+08:00','g/r2','11','https://gl/g/r2/-/merge_requests/11','bob','e.go','bug',5,'f3','why3','scenario_extreme','opus','scenario_validation')"
    )
    conn.commit()
    conn.close()
    return db


def test_collect_summary_basic(populated_db):
    from pr_agent.servers.dashboard_routes import collect_suggestion_filter_summary

    with patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=populated_db), \
         patch("pr_agent.servers.dashboard_routes.migrate_inline_schema"):
        result = collect_suggestion_filter_summary(days=None, project=None)

    assert result["pub_total"] == 2
    assert result["filtered_total"] == 3
    assert result["filter_rate"] == round(3 / 5 * 100, 1)
    assert result["mr_count"] == 2  # g/r!10 and g/r2!11
    assert "scenario_invalid" in result["reason_labels"]
    assert "scenario_missing_prefix" in result["reason_labels"]
    assert len(result["filtered_rows"]) == 3
    assert len(result["project_rows"]) == 2
    assert len(result["mr_rows"]) == 2


def test_collect_summary_empty_db(tmp_path: Path):
    from pr_agent.servers.dashboard_routes import collect_suggestion_filter_summary

    db = str(tmp_path / "empty.db")
    with patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=db), \
         patch("pr_agent.servers.dashboard_routes.migrate_inline_schema"):
        result = collect_suggestion_filter_summary(days=None, project=None)

    assert result["pub_total"] == 0
    assert result["filtered_total"] == 0
    assert result["filter_rate"] == 0
    assert result["filtered_rows"] == []


def test_collect_summary_never_raises():
    from pr_agent.servers.dashboard_routes import collect_suggestion_filter_summary

    with patch("pr_agent.servers.dashboard_routes.get_inline_db_path", side_effect=Exception("boom")):
        result = collect_suggestion_filter_summary(days=None, project=None)

    assert result["pub_total"] == 0
    assert result["filtered_total"] == 0


def test_collect_summary_project_filter(populated_db):
    from pr_agent.servers.dashboard_routes import collect_suggestion_filter_summary

    with patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=populated_db), \
         patch("pr_agent.servers.dashboard_routes.migrate_inline_schema"):
        result = collect_suggestion_filter_summary(days=None, project="g/r2")

    assert result["pub_total"] == 0
    assert result["filtered_total"] == 1


@pytest.mark.parametrize(
    ("table", "total"),
    (("filter_projects", 2), ("filter_mrs", 2), ("filtered_suggestions", 3)),
)
def test_filter_tables_return_fixed_page_metadata(populated_db, table, total):
    from pr_agent.servers.dashboard_routes import collect_suggestion_review_table

    with patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=populated_db), \
         patch("pr_agent.servers.dashboard_routes.migrate_inline_schema"), \
         patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None):
        result = collect_suggestion_review_table(table, page=1, days=None)

    assert result["page"] == 1
    assert result["page_size"] == 15
    assert result["total_rows"] == total
    assert result["total_pages"] == 1
    assert len(result["rows"]) == total


def test_project_table_includes_project_with_published_suggestions_only(populated_db):
    from pr_agent.servers.dashboard_routes import collect_suggestion_review_table

    conn = sqlite3.connect(populated_db)
    conn.execute(
        "INSERT INTO published_suggestions (created_at, project, mr_iid, mr_url, mr_author) "
        "VALUES ('2026-08-04T10:00:00+08:00','g/published-only','12',"
        "'https://gl/g/published-only/-/merge_requests/12','carol')"
    )
    conn.commit()
    conn.close()

    with patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=populated_db), \
         patch("pr_agent.servers.dashboard_routes.migrate_inline_schema"), \
         patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None):
        result = collect_suggestion_review_table("filter_projects", page=1, days=None)

    row = next(row for row in result["rows"] if row["project"] == "g/published-only")
    assert row == {"project": "g/published-only", "pub": 1, "filtered": 0, "rate": 0.0, "mr_count": 1}


def test_filter_table_uses_suggestion_review_history_boundary(populated_db):
    from pr_agent.servers.dashboard_routes import collect_suggestion_review_table

    with patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=populated_db), \
         patch("pr_agent.servers.dashboard_routes.migrate_inline_schema"), \
         patch(
             "pr_agent.servers.dashboard_routes._suggestion_review_cutoff",
             return_value="2026-08-02T12:00:00+08:00",
         ):
        result = collect_suggestion_review_table("filter_projects", page=1, days=30)

    assert [row["project"] for row in result["rows"]] == ["g/r2"]
