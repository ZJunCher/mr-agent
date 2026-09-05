"""filtered_suggestions 表存储层单测。"""
import json
import sqlite3
from pathlib import Path

import pytest


def _rec(suggestion_id="SUG-001", project="group/cook", mr_iid="10", **kw):
    base = {
        "created_at": "2026-08-04T10:00:00+08:00",
        "review_id": "run-1",
        "project": project,
        "mr_iid": mr_iid,
        "mr_url": "https://gitlab/group/cook/-/merge_requests/10",
        "mr_author": "alice",
        "commit_sha": "abc123",
        "file_path": "src/a.go",
        "line_start": 10,
        "line_end": 12,
        "label": "possible bug",
        "severity": "High",
        "score": 9,
        "one_sentence_summary": "fix off-by-one",
        "suggestion_content": "why...\nfix...",
        "existing_code": "old",
        "improved_code": "new",
        "filter_stage": "scenario_validation",
        "skip_reason": "scenario_invalid",
        "judge_model": "anthropic/claude-opus-4-8",
    }
    base.update(kw)
    return base


def test_save_filtered_suggestion_persists_row(tmp_path: Path):
    from pr_agent.suggestions.store import save_filtered_suggestion, init_filtered_table

    db = str(tmp_path / "f.db")
    init_filtered_table(db)
    ok = save_filtered_suggestion(_rec(), path=db)
    assert ok is True

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM filtered_suggestions WHERE mr_iid = ?", ("10",)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["skip_reason"] == "scenario_invalid"
    assert row["judge_model"] == "anthropic/claude-opus-4-8"
    assert row["filter_stage"] == "scenario_validation"
    assert row["score"] == 9


def test_save_filtered_suggestion_isolated_by_mr(tmp_path: Path):
    from pr_agent.suggestions.store import save_filtered_suggestion, init_filtered_table

    db = str(tmp_path / "f.db")
    init_filtered_table(db)
    save_filtered_suggestion(_rec(mr_iid="10"), path=db)
    save_filtered_suggestion(_rec(mr_iid="11"), path=db)

    conn = sqlite3.connect(db)
    count = conn.execute(
        "SELECT COUNT(*) FROM filtered_suggestions WHERE mr_iid = ?", ("10",)
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_save_filtered_suggestion_never_raises_on_bad_path():
    from pr_agent.suggestions.store import save_filtered_suggestion

    # parent path goes through a regular file, so the directory cannot be created
    import tempfile
    with tempfile.NamedTemporaryFile() as f:
        bad = f.name + "/nested/f.db"
        ok = save_filtered_suggestion(_rec(), path=bad)
    assert ok is False


def test_save_filtered_suggestion_serializes_extra_json(tmp_path: Path):
    from pr_agent.suggestions.store import save_filtered_suggestion, init_filtered_table

    db = str(tmp_path / "f.db")
    init_filtered_table(db)
    save_filtered_suggestion(_rec(extra={"k": "v"}), path=db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT extra_json FROM filtered_suggestions WHERE mr_iid = ?", ("10",)
    ).fetchone()
    conn.close()
    assert row is not None
    assert json.loads(row["extra_json"]) == {"k": "v"}


def test_init_filtered_table_idempotent(tmp_path: Path):
    from pr_agent.suggestions.store import init_filtered_table

    db = str(tmp_path / "f.db")
    init_filtered_table(db)
    init_filtered_table(db)  # second call must not raise

    conn = sqlite3.connect(db)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='filtered_suggestions'"
    ).fetchall()
    conn.close()
    assert len(tables) == 1
