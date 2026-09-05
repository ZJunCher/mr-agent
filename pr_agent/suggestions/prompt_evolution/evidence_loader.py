"""Read a frozen Prompt evolution evidence snapshot from the shared SQLite store."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from pr_agent.suggestions.prompt_evolution.models import SourceSnapshot
from pr_agent.suggestions.prompt_evolution.outcomes import build_evidence

_PUBLISHED_SQL = """
SELECT
    created_at,
    updated_at,
    suggestion_id,
    {review_id_column},
    project,
    mr_iid,
    mr_url,
    file_path,
    label,
    one_sentence_summary,
    suggestion_content,
    {replay_columns},
    applied_at,
    resolved_at,
    global_prompt_set_hash,
    project_rules_hash,
    prompt_bundle_hash,
    {project_skill_columns}
FROM published_suggestions
WHERE created_at >= ?
  AND TRIM(COALESCE(global_prompt_set_hash, '')) != ''
  AND TRIM(COALESCE(project_rules_hash, '')) != ''
  AND TRIM(COALESCE(prompt_bundle_hash, '')) != ''
ORDER BY created_at, id
"""

_MR_SQL = """
SELECT project_path AS project, mr_iid, state, updated_at
FROM mr_inventory
ORDER BY project_path, mr_iid
"""

_FEEDBACK_SQL = """
SELECT project, mr_iid, suggestion_id, comment AS body, created_at
FROM inline_suggestion_feedback
ORDER BY created_at, id
"""


def _evolution_case_rows(conn: sqlite3.Connection, cutoff: datetime) -> tuple[list[dict], dict]:
    """Convert versioned structured cases into the existing Evidence input shape."""
    required = {"evolution_cases", "project_skill_usages"}
    if any(not _table_exists(conn, table) for table in required):
        return [], {}
    if _table_exists(conn, "review_runs"):
        review_columns = (
            "rr.pr_url, rr.base_sha, rr.head_sha AS replay_head_sha, rr.input_json"
        )
        review_join = (
            "LEFT JOIN review_runs AS rr "
            "ON rr.review_id = ec.review_id "
            "AND rr.project = ec.project "
            "AND rr.mr_iid = ec.mr_iid "
            "AND rr.head_sha = ec.head_sha"
        )
    else:
        review_columns = (
            "NULL AS pr_url, NULL AS base_sha, NULL AS replay_head_sha, NULL AS input_json"
        )
        review_join = ""
    rows = conn.execute(
        f"""
        SELECT
            ec.*, {review_columns},
            su.target_sha, su.skill_hash, su.manifest_hash, su.load_status,
            su.selected_rule_ids_json, su.reference_hashes_json,
            su.global_prompt_set_hash AS usage_global_hash,
            su.prompt_bundle_hash AS usage_bundle_hash
        FROM evolution_cases AS ec
        {review_join}
        LEFT JOIN project_skill_usages AS su
          ON su.review_id = ec.review_id AND su.command = ec.command
        WHERE ec.created_at >= ?
        ORDER BY ec.created_at, ec.id
        """,
        (cutoff.isoformat(),),
    ).fetchall()
    evidence_rows = []
    feedback = {}
    for raw in rows:
        row = dict(raw)
        global_hash = str(row.get("global_prompt_set_hash") or row.get("usage_global_hash") or "")
        bundle_hash = str(row.get("prompt_bundle_hash") or row.get("usage_bundle_hash") or "")
        skill_hash = str(row.get("project_skill_hash") or row.get("skill_hash") or "")
        if not global_hash or not bundle_hash:
            continue
        case_id = str(row.get("case_id") or "")
        try:
            frozen_input = json.loads(row.get("input_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            frozen_input = {}
        converted = {
            "created_at": row.get("created_at"),
            "updated_at": row.get("created_at"),
            "suggestion_id": case_id,
            "project": row.get("project"),
            "mr_iid": row.get("mr_iid"),
            "mr_url": row.get("pr_url"),
            "file_path": row.get("file_path"),
            "line_start": row.get("line_start"),
            "line_end": row.get("line_end"),
            "label": row.get("kind"),
            "one_sentence_summary": row.get("description"),
            "suggestion_content": row.get("description"),
            # Every structured case represents a baseline failure and must drive candidate generation.
            # ``expected_action`` carries the desired replay behavior independently.
            "applied_at": None,
            "resolved_at": row.get("created_at"),
            "global_prompt_set_hash": global_hash,
            "project_rules_hash": skill_hash or "no-project-skill",
            "prompt_bundle_hash": bundle_hash,
            "project_skill_hash": skill_hash,
            "project_skill_manifest_hash": row.get("manifest_hash"),
            "project_skill_target_sha": row.get("target_sha"),
            "project_skill_status": row.get("load_status"),
            "project_skill_rule_ids_json": row.get("selected_rule_ids_json") or "[]",
            "project_skill_reference_hashes_json": row.get("reference_hashes_json") or "{}",
            "commit_sha": row.get("head_sha"),
            "case_kind": row.get("kind"),
            "expected_action": row.get("expected_action"),
            "review_id": row.get("review_id"),
            "replayable": bool(row.get("base_sha") and row.get("replay_head_sha") and frozen_input),
        }
        evidence_rows.append(converted)
        feedback[(str(row.get("project") or ""), str(row.get("mr_iid") or ""), case_id)] = [{
            "body": row.get("description"),
            "created_at": row.get("created_at"),
        }]
    return evidence_rows, feedback


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _attach_replay_identities(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Mark suggestion evidence replayable only when its frozen review identity matches exactly."""
    if not rows or not _table_exists(conn, "review_runs"):
        return
    review_ids = tuple(sorted({str(row.get("review_id") or "") for row in rows if row.get("review_id")}))
    if not review_ids:
        return
    placeholders = ",".join("?" for _ in review_ids)
    records = conn.execute(
        f"SELECT review_id, project, mr_iid, head_sha, base_sha, input_json FROM review_runs "
        f"WHERE review_id IN ({placeholders})",
        review_ids,
    ).fetchall()
    by_id = {str(record["review_id"]): dict(record) for record in records}
    for row in rows:
        review_id = str(row.get("review_id") or "")
        record = by_id.get(review_id)
        if record is None:
            continue
        try:
            frozen_input = json.loads(record.get("input_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            frozen_input = {}
        row["replayable"] = bool(
            frozen_input
            and record.get("base_sha")
            and str(record.get("project") or "") == str(row.get("project") or "")
            and str(record.get("mr_iid") or "") == str(row.get("mr_iid") or "")
            and str(record.get("head_sha") or "") == str(row.get("commit_sha") or "")
        )


def _review_skill_rows(conn: sqlite3.Connection, cutoff: datetime) -> tuple[list[dict], dict]:
    """Convert explicit `/review` ratings into Skill-versioned evolution evidence."""
    if not _table_exists(conn, "review_feedback") or not _table_exists(conn, "project_skill_usages"):
        return [], {}
    usage_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(project_skill_usages)").fetchall()
    }
    if not {"global_prompt_set_hash", "prompt_bundle_hash"}.issubset(usage_columns):
        return [], {}
    rows = conn.execute(
        """
        SELECT
            rf.created_at, rf.pr_url, rf.project, rf.mr_iid, rf.score, rf.comment, rf.review_id,
            su.skill_hash, su.manifest_hash, su.target_sha, su.load_status,
            su.selected_rule_ids_json, su.matched_files_json, su.reference_hashes_json,
            su.global_prompt_set_hash, su.prompt_bundle_hash
        FROM review_feedback AS rf
        JOIN project_skill_usages AS su ON su.review_id = rf.review_id AND su.command = 'review'
        WHERE rf.created_at >= ? AND su.load_status = 'loaded'
          AND TRIM(COALESCE(su.skill_hash, '')) != ''
          AND TRIM(COALESCE(su.global_prompt_set_hash, '')) != ''
          AND TRIM(COALESCE(su.prompt_bundle_hash, '')) != ''
        ORDER BY rf.created_at, rf.id
        """,
        (cutoff.isoformat(),),
    ).fetchall()
    evidence_rows = []
    feedback_by_key = {}
    for raw in rows:
        row = dict(raw)
        try:
            rule_ids = json.loads(row.get("selected_rule_ids_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            rule_ids = []
        if not rule_ids:
            continue
        try:
            matched_files = json.loads(row.get("matched_files_json") or "{}")
            first_file = next(
                (str(path) for paths in matched_files.values() for path in paths if path),
                "",
            )
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            first_file = ""
        score = int(row.get("score") or 0)
        suggestion_id = f"review:{row.get('review_id') or ''}"
        converted = {
            "created_at": row.get("created_at"),
            "updated_at": row.get("created_at"),
            "suggestion_id": suggestion_id,
            "project": row.get("project"),
            "mr_iid": row.get("mr_iid"),
            "mr_url": row.get("pr_url"),
            "file_path": first_file,
            "label": "review_feedback",
            "one_sentence_summary": f"Review feedback score {score}",
            "suggestion_content": str(row.get("comment") or "")[:500],
            "applied_at": row.get("created_at") if score >= 4 else None,
            "resolved_at": row.get("created_at") if score <= 2 else None,
            "global_prompt_set_hash": row.get("global_prompt_set_hash"),
            "project_rules_hash": row.get("skill_hash"),
            "prompt_bundle_hash": row.get("prompt_bundle_hash"),
            "project_skill_hash": row.get("skill_hash"),
            "project_skill_manifest_hash": row.get("manifest_hash"),
            "project_skill_target_sha": row.get("target_sha"),
            "project_skill_status": row.get("load_status"),
            "project_skill_rule_ids_json": row.get("selected_rule_ids_json"),
            "project_skill_reference_hashes_json": row.get("reference_hashes_json"),
        }
        evidence_rows.append(converted)
        if row.get("comment"):
            feedback_by_key[(
                str(row.get("project") or ""),
                str(row.get("mr_iid") or ""),
                suggestion_id,
            )] = [{"body": row.get("comment"), "created_at": row.get("created_at")}]
    return evidence_rows, feedback_by_key


class EvidenceSourceUnavailable(RuntimeError):
    """The shared feedback database cannot provide a trustworthy snapshot."""


class SqliteEvidenceLoader:
    """Join suggestion outcomes, MR state, and discussion feedback read-only."""

    def __init__(
        self,
        path: str,
        *,
        accepted_weight: float = 1.0,
        rejected_weight: float = 1.0,
        unhandled_weight: float = 0.25,
    ) -> None:
        self.path = str(path)
        self.accepted_weight = float(accepted_weight)
        self.rejected_weight = float(rejected_weight)
        self.unhandled_weight = float(unhandled_weight)

    @contextmanager
    def _snapshot_connection(self) -> Iterator[sqlite3.Connection]:
        resolved = str(Path(self.path).expanduser().resolve())
        uri = f"file:{quote(resolved, safe='/')}?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("BEGIN")
            yield conn
        except sqlite3.Error as exc:
            raise EvidenceSourceUnavailable(
                f"required evidence tables unavailable: {type(exc).__name__}"
            ) from exc
        finally:
            if conn is not None:
                try:
                    conn.rollback()
                finally:
                    conn.close()

    def load(
        self,
        *,
        prior_watermark: str | None,
        window_days: int,
        unhandled_after_days: int,
        now: datetime,
    ) -> SourceSnapshot:
        cutoff = now - timedelta(days=int(window_days))
        with self._snapshot_connection() as conn:
            published_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(published_suggestions)").fetchall()
            }
            optional_names = (
                "commit_sha",
                "existing_code",
                "improved_code",
                "line_start",
                "line_end",
                "project_skill_hash",
                "project_skill_manifest_hash",
                "project_skill_target_sha",
                "project_skill_status",
                "project_skill_rule_ids_json",
                "project_skill_reference_hashes_json",
            )
            def optional_column(name: str) -> str:
                if name in published_columns:
                    return name
                return f"0 AS {name}" if name in {"line_start", "line_end"} else f"'' AS {name}"

            published_rows = [
                dict(row)
                for row in conn.execute(
                    _PUBLISHED_SQL.format(
                        review_id_column=(
                            "review_id" if "review_id" in published_columns else "'' AS review_id"
                        ),
                        replay_columns=",\n    ".join(optional_column(name) for name in optional_names[:5]),
                        project_skill_columns=",\n    ".join(
                            optional_column(name) for name in optional_names[5:]
                        ),
                    ),
                    (cutoff.isoformat(),),
                ).fetchall()
            ]
            _attach_replay_identities(conn, published_rows)
            mr_inventory = {
                (str(row["project"] or ""), str(row["mr_iid"] or "")): dict(row)
                for row in conn.execute(_MR_SQL).fetchall()
            }
            feedback_by_key: dict[tuple[str, str, str], list[dict]] = {}
            for row in conn.execute(_FEEDBACK_SQL).fetchall():
                key = (
                    str(row["project"] or ""),
                    str(row["mr_iid"] or ""),
                    str(row["suggestion_id"] or ""),
                )
                feedback_by_key.setdefault(key, []).append(dict(row))
            review_rows, review_feedback = _review_skill_rows(conn, cutoff)
            published_rows.extend(review_rows)
            for key, rows in review_feedback.items():
                feedback_by_key.setdefault(key, []).extend(rows)
            case_rows, case_feedback = _evolution_case_rows(conn, cutoff)
            published_rows.extend(case_rows)
            for key, rows in case_feedback.items():
                feedback_by_key.setdefault(key, []).extend(rows)

        return build_evidence(
            published_rows,
            mr_inventory,
            feedback_by_key,
            now=now,
            window_days=int(window_days),
            unhandled_after_days=int(unhandled_after_days),
            prior_watermark=prior_watermark,
            accepted_weight=self.accepted_weight,
            rejected_weight=self.rejected_weight,
            unhandled_weight=self.unhandled_weight,
        )
