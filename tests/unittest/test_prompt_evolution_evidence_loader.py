from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pr_agent.eval.store import save_review_run
from pr_agent.feedback.store import save_evolution_case, save_feedback, save_project_skill_usage
from pr_agent.suggestions.prompt_evolution.evidence_loader import (
    EvidenceSourceUnavailable,
    SqliteEvidenceLoader,
)
from pr_agent.suggestions.prompt_evolution.models import Outcome


NOW = datetime(2026, 8, 18, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


def _create_source_tables(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE published_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            suggestion_id TEXT,
            review_id TEXT,
            project TEXT,
            mr_iid TEXT,
            mr_url TEXT,
            commit_sha TEXT,
            file_path TEXT,
            line_start INTEGER,
            line_end INTEGER,
            label TEXT,
            one_sentence_summary TEXT,
            suggestion_content TEXT,
            existing_code TEXT,
            improved_code TEXT,
            applied_at TEXT,
            resolved_at TEXT,
            global_prompt_set_hash TEXT,
            project_rules_hash TEXT,
            prompt_bundle_hash TEXT
        );
        CREATE TABLE inline_suggestion_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            project TEXT,
            mr_iid TEXT,
            suggestion_id TEXT,
            comment TEXT
        );
        CREATE TABLE mr_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_path TEXT,
            mr_iid TEXT,
            state TEXT,
            updated_at TEXT
        );
        """
    )
    return conn


def _insert_suggestion(
    conn: sqlite3.Connection,
    suggestion_id: str,
    mr_iid: str,
    *,
    created_at: str = "2026-08-01T09:00:00+08:00",
    applied_at: str | None = None,
    resolved_at: str | None = None,
    global_hash: str = "global-v1",
    rules_hash: str = "rules-v1",
    bundle_hash: str = "bundle-v1",
) -> None:
    conn.execute(
        """
        INSERT INTO published_suggestions (
            created_at, updated_at, suggestion_id, project, mr_iid, mr_url,
            commit_sha, file_path, line_start, line_end, label, one_sentence_summary, suggestion_content,
            existing_code, improved_code,
            applied_at, resolved_at, global_prompt_set_hash,
            project_rules_hash, prompt_bundle_hash
        ) VALUES (?, ?, ?, 'eabot/cook', ?, ?, ?, 'src/a.cpp', 10, 12, 'bug', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            created_at,
            applied_at or resolved_at or created_at,
            suggestion_id,
            mr_iid,
            f"https://gitlab.example/eabot/cook/-/merge_requests/{mr_iid}",
            "a" * 40,
            f"summary-{suggestion_id}",
            f"content-{suggestion_id}",
            "old()",
            "new()",
            applied_at,
            resolved_at,
            global_hash,
            rules_hash,
            bundle_hash,
        ),
    )


def _seed_feedback_db(path) -> None:
    conn = _create_source_tables(path)
    _insert_suggestion(conn, "accepted", "1", applied_at="2026-08-10T10:00:00+08:00")
    _insert_suggestion(conn, "rejected", "2", resolved_at="2026-08-11T10:00:00+08:00")
    _insert_suggestion(conn, "merged", "3", created_at="2026-08-17T10:00:00+08:00")
    _insert_suggestion(conn, "pending", "4", created_at="2026-08-17T11:00:00+08:00")
    _insert_suggestion(conn, "closed", "5")
    _insert_suggestion(
        conn,
        "missing-hashes",
        "6",
        applied_at="2026-08-12T10:00:00+08:00",
        global_hash="",
        rules_hash="",
        bundle_hash="",
    )
    conn.executemany(
        "INSERT INTO mr_inventory (project_path, mr_iid, state, updated_at) "
        "VALUES ('eabot/cook', ?, ?, ?)",
        [
            ("1", "opened", "2026-08-10T10:00:00+08:00"),
            ("2", "opened", "2026-08-11T10:00:00+08:00"),
            ("3", "merged", "2026-08-18T08:00:00+08:00"),
            ("4", "opened", "2026-08-17T11:00:00+08:00"),
            ("5", "closed", "2026-08-12T09:00:00+08:00"),
            ("6", "opened", "2026-08-12T10:00:00+08:00"),
        ],
    )
    conn.execute(
        "INSERT INTO inline_suggestion_feedback "
        "(created_at, project, mr_iid, suggestion_id, comment) "
        "VALUES (?, 'eabot/cook', '2', 'rejected', ?)",
        ("2026-08-11T10:01:00+08:00", "具体拒绝原因"),
    )
    conn.commit()
    conn.close()


def test_loader_joins_feedback_inventory_and_prompt_attribution(tmp_path):
    path = tmp_path / "feedback.db"
    _seed_feedback_db(path)
    loader = SqliteEvidenceLoader(str(path))

    snapshot = loader.load(
        prior_watermark=None,
        window_days=90,
        unhandled_after_days=14,
        now=NOW,
    )

    by_id = {item.suggestion_id: item for item in snapshot.evidence}
    assert by_id["accepted"].outcome is Outcome.ACCEPTED
    assert by_id["rejected"].outcome is Outcome.REJECTED
    assert by_id["merged"].outcome is Outcome.UNHANDLED
    assert by_id["pending"].outcome is Outcome.PENDING
    assert by_id["closed"].outcome is Outcome.INVALID
    assert by_id["rejected"].feedback == ("具体拒绝原因",)
    assert by_id["rejected"].project_rules_hash == "rules-v1"
    assert by_id["accepted"].commit_sha == "a" * 40
    assert by_id["accepted"].existing_code == "old()"
    assert by_id["accepted"].improved_code == "new()"
    assert by_id["accepted"].line_start == 10
    assert by_id["accepted"].line_end == 12
    assert "missing-hashes" not in by_id
    assert snapshot.has_new_signal is True


def test_loader_marks_published_suggestion_replayable_only_for_matching_frozen_review(tmp_path):
    path = tmp_path / "feedback.db"
    _seed_feedback_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE published_suggestions SET review_id = 'improve-1' WHERE suggestion_id = 'accepted'"
    )
    conn.commit()
    conn.close()
    assert save_review_run({
        "review_id": "improve-1",
        "project": "eabot/cook",
        "mr_iid": "1",
        "base_sha": "b" * 40,
        "head_sha": "a" * 40,
        "input": {"title": "Frozen improve input"},
    }, path=str(path))

    snapshot = SqliteEvidenceLoader(str(path)).load(
        prior_watermark=None,
        window_days=90,
        unhandled_after_days=14,
        now=NOW,
    )
    by_id = {item.suggestion_id: item for item in snapshot.evidence}

    assert by_id["accepted"].review_id == "improve-1"
    assert by_id["accepted"].replayable is True
    assert by_id["rejected"].replayable is False


def test_loader_applies_configured_weights(tmp_path):
    path = tmp_path / "feedback.db"
    _seed_feedback_db(path)
    loader = SqliteEvidenceLoader(
        str(path),
        accepted_weight=2.0,
        rejected_weight=3.0,
        unhandled_weight=0.5,
    )

    snapshot = loader.load(
        prior_watermark=None,
        window_days=90,
        unhandled_after_days=14,
        now=NOW,
    )
    by_id = {item.suggestion_id: item for item in snapshot.evidence}

    assert by_id["accepted"].weight == 2.0
    assert by_id["rejected"].weight == 3.0
    assert by_id["merged"].weight == 0.5


def test_loader_reads_project_skill_provenance_when_columns_exist(tmp_path):
    path = tmp_path / "feedback.db"
    _seed_feedback_db(path)
    conn = sqlite3.connect(path)
    for column in (
        "project_skill_hash",
        "project_skill_manifest_hash",
        "project_skill_target_sha",
        "project_skill_status",
        "project_skill_rule_ids_json",
        "project_skill_reference_hashes_json",
    ):
        conn.execute(f"ALTER TABLE published_suggestions ADD COLUMN {column} TEXT")
    conn.execute(
        """
        UPDATE published_suggestions SET
            project_skill_hash = 'skill-v2',
            project_skill_manifest_hash = 'manifest-v2',
            project_skill_target_sha = 'sha-v2',
            project_skill_status = 'loaded',
            project_skill_rule_ids_json = '["api"]',
            project_skill_reference_hashes_json = '{"references/api.md":"ref-v2"}'
        WHERE suggestion_id = 'accepted'
        """
    )
    conn.commit()
    conn.close()

    snapshot = SqliteEvidenceLoader(str(path)).load(
        prior_watermark=None,
        window_days=90,
        unhandled_after_days=14,
        now=NOW,
    )
    accepted = next(item for item in snapshot.evidence if item.suggestion_id == "accepted")

    assert accepted.project_skill_hash == "skill-v2"
    assert accepted.project_skill_target_sha == "sha-v2"
    assert accepted.project_skill_rule_ids == ("api",)
    assert accepted.project_skill_reference_hashes == (("references/api.md", "ref-v2"),)


def test_loader_converts_review_ratings_into_skill_versioned_evidence(tmp_path):
    path = tmp_path / "feedback.db"
    conn = _create_source_tables(path)
    conn.commit()
    conn.close()
    usage = {
        "review_id": "review-low",
        "command": "review",
        "project": "eabot/cook",
        "mr_iid": "8",
        "target_branch": "main",
        "target_sha": "sha-8",
        "skill_hash": "skill-8",
        "manifest_hash": "manifest-8",
        "load_status": "loaded",
        "selected_rule_ids": ["api"],
        "matched_files": {"api": ["src/api.py"]},
        "reference_hashes": {"references/api.md": "ref-8"},
        "global_prompt_set_hash": "global-v1",
        "prompt_bundle_hash": "review-bundle-8",
    }
    assert save_project_skill_usage(usage, path=str(path))
    assert save_feedback({
        "created_at": "2026-08-18T10:00:00+08:00",
        "pr_url": "https://gitlab/eabot/cook/-/merge_requests/8",
        "project": "eabot/cook",
        "mr_iid": "8",
        "score": 1,
        "comment": "This project rule caused a false positive.",
        "review_id": "review-low",
    }, path=str(path))

    snapshot = SqliteEvidenceLoader(str(path)).load(
        prior_watermark=None,
        window_days=90,
        unhandled_after_days=14,
        now=NOW,
    )
    review = next(item for item in snapshot.evidence if item.suggestion_id == "review:review-low")

    assert review.outcome is Outcome.REJECTED
    assert review.project_skill_hash == "skill-8"
    assert review.project_skill_manifest_hash == "manifest-8"
    assert review.project_skill_rule_ids == ("api",)
    assert review.file_path == "src/api.py"
    assert review.feedback == ("This project rule caused a false positive.",)
    assert review.commit_sha == ""
    assert review.existing_code == ""
    assert review.improved_code == ""
    assert review.line_start == 0
    assert review.line_end == 0


def test_loader_converts_replayable_false_negative_case_into_emit_evidence(tmp_path):
    path = tmp_path / "feedback.db"
    conn = _create_source_tables(path)
    conn.commit()
    conn.close()
    assert save_review_run({
        "review_id": "review-missed",
        "created_at": "2026-08-17T10:00:00+08:00",
        "pr_url": "https://gitlab/eabot/cook/-/merge_requests/9",
        "project": "eabot/cook",
        "mr_iid": "9",
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
        "model": "model",
        "input": {"title": "Fix parser"},
    }, path=str(path))
    assert save_project_skill_usage({
        "review_id": "review-missed",
        "command": "review",
        "project": "eabot/cook",
        "mr_iid": "9",
        "target_branch": "main",
        "target_sha": "d" * 40,
        "skill_hash": "skill-v1",
        "manifest_hash": "manifest-v1",
        "load_status": "loaded",
        "selected_rule_ids": ["api"],
        "global_prompt_set_hash": "global-v1",
        "prompt_bundle_hash": "bundle-v1",
    }, path=str(path))
    assert save_evolution_case({
        "kind": "false_negative",
        "project": "eabot/cook",
        "mr_iid": "9",
        "review_id": "review-missed",
        "head_sha": "c" * 40,
        "command": "review",
        "description": "The review missed a null dereference.",
        "source": "manual",
        "file_path": "src/parser.cpp",
        "line_start": 25,
        "line_end": 25,
        "created_at": "2026-08-17T10:01:00+08:00",
    }, path=str(path))

    snapshot = SqliteEvidenceLoader(str(path)).load(
        prior_watermark=None,
        window_days=90,
        unhandled_after_days=14,
        now=NOW,
    )
    case = next(item for item in snapshot.evidence if item.case_kind == "false_negative")

    assert case.outcome is Outcome.REJECTED
    assert case.expected_action == "emit"
    assert case.replayable is True
    assert case.review_id == "review-missed"
    assert case.project_skill_hash == "skill-v1"
    assert case.file_path == "src/parser.cpp"
    assert case.line_start == 25


def test_loader_keeps_nonreplayable_case_as_training_evidence(tmp_path):
    path = tmp_path / "feedback.db"
    conn = _create_source_tables(path)
    conn.commit()
    conn.close()
    assert save_project_skill_usage({
        "review_id": "review-training-only",
        "command": "review",
        "project": "eabot/cook",
        "mr_iid": "10",
        "target_branch": "main",
        "target_sha": "d" * 40,
        "skill_hash": "skill-v1",
        "manifest_hash": "manifest-v1",
        "load_status": "loaded",
        "selected_rule_ids": ["api"],
        "global_prompt_set_hash": "global-v1",
        "prompt_bundle_hash": "bundle-v1",
    }, path=str(path))
    assert save_evolution_case({
        "kind": "false_negative",
        "project": "eabot/cook",
        "mr_iid": "10",
        "review_id": "review-training-only",
        "head_sha": "c" * 40,
        "command": "review",
        "description": "The review missed a bounds check.",
        "source": "manual",
        "file_path": "src/parser.cpp",
        "line_start": 30,
        "line_end": 30,
        "created_at": "2026-08-17T10:01:00+08:00",
    }, path=str(path))

    snapshot = SqliteEvidenceLoader(str(path)).load(
        prior_watermark=None,
        window_days=90,
        unhandled_after_days=14,
        now=NOW,
    )
    case = next(item for item in snapshot.evidence if item.review_id == "review-training-only")

    assert case.replayable is False
    assert case.expected_action == "emit"


def test_loader_fails_closed_when_source_tables_are_missing(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()

    with pytest.raises(EvidenceSourceUnavailable, match="required evidence tables unavailable"):
        SqliteEvidenceLoader(str(path)).load(
            prior_watermark=None,
            window_days=90,
            unhandled_after_days=14,
            now=NOW,
        )
