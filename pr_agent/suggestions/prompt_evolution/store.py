"""SQLite-backed durable store for weekly Prompt evolution runs.

All writes go through ``pr_agent.storage.sqlite.run_write_transaction`` so a
storage problem can never break the weekly runner; every JSON blob uses
``ensure_ascii=False`` and ``sort_keys=True`` for stable hashing and replay.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from typing import Any, Iterable

from pr_agent.feedback.timez import now_cn_iso
from pr_agent.storage.sqlite import run_write_transaction
from pr_agent.suggestions.prompt_evolution.models import (
    CandidateScope,
    EligibleCandidate,
    Evidence,
    EvolutionRun,
    EvolutionRunStatus,
    HighFidelityEvaluationReport,
    Outcome,
    PromptChangeKind,
    PromptEvaluationBatch,
    PromptFileChange,
    PromptProposal,
    SkillOptimizationBatch,
    SkillOptimizationReport,
    ValidationReport,
    WeightedCluster,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_evolution_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL UNIQUE,
    target_project TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    global_prompt_set_hash TEXT NOT NULL,
    target_prompt_set_hash TEXT NOT NULL,
    source_watermark TEXT,
    fencing_token INTEGER NOT NULL,
    status TEXT NOT NULL,
    branch_name TEXT,
    commit_sha TEXT,
    mr_iid TEXT,
    mr_url TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS prompt_evolution_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    project TEXT,
    cluster_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    source_prompt_hash TEXT NOT NULL,
    positive_weight REAL NOT NULL,
    negative_weight REAL NOT NULL,
    negative_ratio REAL NOT NULL,
    candidate_json TEXT NOT NULL,
    proposal_json TEXT,
    validation_json TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_evolution_evidence (
    candidate_id TEXT NOT NULL,
    suggestion_id TEXT NOT NULL,
    project TEXT NOT NULL,
    mr_iid TEXT NOT NULL,
    mr_url TEXT NOT NULL,
    source_created_at TEXT NOT NULL,
    file_path TEXT NOT NULL,
    label TEXT NOT NULL,
    summary TEXT NOT NULL,
    suggestion_content TEXT NOT NULL,
    captured_outcome TEXT NOT NULL,
    captured_weight REAL NOT NULL,
    global_prompt_set_hash TEXT NOT NULL,
    prompt_bundle_hash TEXT NOT NULL,
    project_rules_hash TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    existing_code TEXT NOT NULL DEFAULT '',
    improved_code TEXT NOT NULL DEFAULT '',
    line_start INTEGER NOT NULL DEFAULT 0,
    line_end INTEGER NOT NULL DEFAULT 0,
    feedback_json TEXT NOT NULL,
    PRIMARY KEY(candidate_id, suggestion_id)
);
CREATE TABLE IF NOT EXISTS prompt_evolution_source_evidence (
    run_id TEXT NOT NULL,
    suggestion_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(run_id, suggestion_id)
);
CREATE TABLE IF NOT EXISTS prompt_evolution_meta (
    meta_key TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_skill_optimization_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    project TEXT NOT NULL,
    base_skill_hash TEXT NOT NULL,
    candidate_skill_hash TEXT NOT NULL,
    split_hash TEXT NOT NULL,
    training_ids_json TEXT NOT NULL,
    selection_ids_json TEXT NOT NULL,
    control_ids_json TEXT NOT NULL,
    edit_budget INTEGER NOT NULL,
    edit_count INTEGER NOT NULL,
    edit_signature TEXT NOT NULL,
    replay_model TEXT NOT NULL,
    baseline_scores_json TEXT NOT NULL,
    candidate_scores_json TEXT NOT NULL,
    action TEXT NOT NULL,
    errors_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    high_fidelity_json TEXT NOT NULL DEFAULT '{}',
    execution_mode TEXT NOT NULL DEFAULT 'fragment',
    proposal_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_skill_optimization_rejected
    ON project_skill_optimization_steps (project, base_skill_hash, action, id);
CREATE TABLE IF NOT EXISTS global_prompt_evaluation_batches (
    run_id TEXT PRIMARY KEY,
    base_prompt_hash TEXT NOT NULL,
    split_hash TEXT NOT NULL,
    training_ids_json TEXT NOT NULL,
    selection_ids_json TEXT NOT NULL,
    control_ids_json TEXT NOT NULL,
    report_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_EMPTY_MANIFEST_ID = "__empty__"
_ALLOWED_UPDATE_FIELDS = frozenset({
    "source_watermark", "branch_name", "commit_sha", "mr_iid", "mr_url",
    "error_code", "error_message",
})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stored_candidate_id(run_id: str, candidate_id: str) -> str:
    return hashlib.sha256(f"{run_id}\n{candidate_id}".encode("utf-8")).hexdigest()


def _evidence_to_json(evidence: Evidence) -> str:
    return _canonical_json({
        "suggestion_id": evidence.suggestion_id,
        "project": evidence.project,
        "mr_iid": evidence.mr_iid,
        "mr_url": evidence.mr_url,
        "created_at": evidence.created_at,
        "file_path": evidence.file_path,
        "label": evidence.label,
        "summary": evidence.summary,
        "suggestion_content": evidence.suggestion_content,
        "outcome": str(evidence.outcome),
        "weight": evidence.weight,
        "global_prompt_set_hash": evidence.global_prompt_set_hash,
        "prompt_bundle_hash": evidence.prompt_bundle_hash,
        "project_rules_hash": evidence.project_rules_hash,
        "project_skill_hash": evidence.project_skill_hash,
        "project_skill_manifest_hash": evidence.project_skill_manifest_hash,
        "project_skill_target_sha": evidence.project_skill_target_sha,
        "project_skill_status": evidence.project_skill_status,
        "project_skill_rule_ids": list(evidence.project_skill_rule_ids),
        "project_skill_reference_hashes": dict(evidence.project_skill_reference_hashes),
        "feedback": list(evidence.feedback),
        "existing_code": evidence.existing_code,
        "improved_code": evidence.improved_code,
        "commit_sha": evidence.commit_sha,
        "line_start": evidence.line_start,
        "line_end": evidence.line_end,
        "case_kind": evidence.case_kind,
        "expected_action": evidence.expected_action,
        "review_id": evidence.review_id,
        "replayable": evidence.replayable,
    })


def _evidence_from_json(raw: str) -> Evidence:
    data = json.loads(raw)
    return Evidence(
        suggestion_id=data["suggestion_id"],
        project=data["project"],
        mr_iid=data["mr_iid"],
        mr_url=data["mr_url"],
        created_at=data["created_at"],
        file_path=data["file_path"],
        label=data["label"],
        summary=data["summary"],
        suggestion_content=data["suggestion_content"],
        outcome=Outcome(data["outcome"]),
        weight=float(data["weight"]),
        global_prompt_set_hash=data["global_prompt_set_hash"],
        prompt_bundle_hash=data["prompt_bundle_hash"],
        project_rules_hash=data.get("project_rules_hash", ""),
        project_skill_hash=data.get("project_skill_hash", ""),
        project_skill_manifest_hash=data.get("project_skill_manifest_hash", ""),
        project_skill_target_sha=data.get("project_skill_target_sha", ""),
        project_skill_status=data.get("project_skill_status", ""),
        project_skill_rule_ids=tuple(data.get("project_skill_rule_ids") or ()),
        project_skill_reference_hashes=tuple(
            (str(path), str(content_hash))
            for path, content_hash in (data.get("project_skill_reference_hashes") or {}).items()
        ),
        feedback=tuple(data.get("feedback") or ()),
        existing_code=str(data.get("existing_code") or ""),
        improved_code=str(data.get("improved_code") or ""),
        commit_sha=str(data.get("commit_sha") or ""),
        line_start=int(data.get("line_start") or 0),
        line_end=int(data.get("line_end") or 0),
        case_kind=str(data.get("case_kind") or ""),
        expected_action=str(data.get("expected_action") or ""),
        review_id=str(data.get("review_id") or ""),
        replayable=bool(data.get("replayable", False)),
    )


def _candidate_to_json(candidate: EligibleCandidate) -> str:
    cluster = candidate.cluster
    return _canonical_json({
        "candidate_id": candidate.candidate_id,
        "scope": str(candidate.scope),
        "project": candidate.project,
        "source_prompt_hash": candidate.source_prompt_hash,
        "cluster": {
            "cluster_key": cluster.cluster_key,
            "evidence": [_evidence_to_json(e) for e in cluster.evidence],
            "positive_weight": cluster.positive_weight,
            "negative_weight": cluster.negative_weight,
            "negative_ratio": cluster.negative_ratio,
        },
    })


def _candidate_from_json(raw: str) -> EligibleCandidate:
    data = json.loads(raw)
    cluster_data = data["cluster"]
    evidence = tuple(_evidence_from_json(e) for e in cluster_data["evidence"])
    cluster = WeightedCluster(
        cluster_key=cluster_data["cluster_key"],
        evidence=evidence,
        positive_weight=float(cluster_data["positive_weight"]),
        negative_weight=float(cluster_data["negative_weight"]),
        negative_ratio=float(cluster_data["negative_ratio"]),
    )
    return EligibleCandidate(
        candidate_id=data["candidate_id"],
        scope=CandidateScope(data["scope"]),
        project=data.get("project"),
        source_prompt_hash=data["source_prompt_hash"],
        cluster=cluster,
    )


def _proposal_to_json(proposal: PromptProposal) -> str:
    return _canonical_json({
        "rationale": proposal.rationale,
        "change_kind": str(proposal.change_kind),
        "evidence_ids": list(proposal.evidence_ids),
        "changes": [
            {
                "path": c.path,
                "family": c.family,
                "expected_base_sha256": c.expected_base_sha256,
                "content": c.content,
                "evidence_ids": list(c.evidence_ids),
            }
            for c in proposal.changes
        ],
    })


def _proposal_from_json(raw: str) -> PromptProposal:
    data = json.loads(raw)
    return PromptProposal(
        rationale=data["rationale"],
        change_kind=PromptChangeKind(data["change_kind"]),
        evidence_ids=tuple(data.get("evidence_ids") or ()),
        changes=tuple(
            PromptFileChange(
                path=c["path"],
                family=c["family"],
                expected_base_sha256=c["expected_base_sha256"],
                content=c["content"],
                evidence_ids=tuple(c.get("evidence_ids") or ()),
            )
            for c in (data.get("changes") or ())
        ),
    )


def _validation_to_json(report: ValidationReport) -> str:
    return _canonical_json({
        "passed": report.passed,
        "errors": list(report.errors),
        "checks": list(report.checks),
    })


def _validation_from_json(raw: str) -> ValidationReport:
    data = json.loads(raw)
    return ValidationReport(
        passed=bool(data["passed"]),
        errors=tuple(data.get("errors") or ()),
        checks=tuple(data.get("checks") or ()),
    )


def _optimization_report_to_json(report: SkillOptimizationReport) -> str:
    return _canonical_json({
        "passed": report.passed,
        "action": report.action,
        "errors": list(report.errors),
        "checks": list(report.checks),
        "split_hash": report.split_hash,
        "replay_model": report.replay_model,
        "baseline_score": report.baseline_score,
        "candidate_score": report.candidate_score,
        "baseline_accepted_score": report.baseline_accepted_score,
        "candidate_accepted_score": report.candidate_accepted_score,
        "baseline_rejected_score": report.baseline_rejected_score,
        "candidate_rejected_score": report.candidate_rejected_score,
        "accepted_control_regressions": list(report.accepted_control_regressions),
        "rejected_target_regressions": list(report.rejected_target_regressions),
        "edit_budget": report.edit_budget,
        "edit_count": report.edit_count,
        "edit_signature": report.edit_signature,
    })


def _optimization_report_from_json(raw: str) -> SkillOptimizationReport:
    data = json.loads(raw)
    return SkillOptimizationReport(
        passed=bool(data["passed"]),
        action=str(data["action"]),
        errors=tuple(data.get("errors") or ()),
        checks=tuple(data.get("checks") or ()),
        split_hash=str(data["split_hash"]),
        replay_model=str(data["replay_model"]),
        baseline_score=str(data["baseline_score"]),
        candidate_score=str(data["candidate_score"]),
        baseline_accepted_score=str(data["baseline_accepted_score"]),
        candidate_accepted_score=str(data["candidate_accepted_score"]),
        baseline_rejected_score=str(data["baseline_rejected_score"]),
        candidate_rejected_score=str(data["candidate_rejected_score"]),
        accepted_control_regressions=tuple(data.get("accepted_control_regressions") or ()),
        rejected_target_regressions=tuple(data.get("rejected_target_regressions") or ()),
        edit_budget=int(data["edit_budget"]),
        edit_count=int(data["edit_count"]),
        edit_signature=str(data["edit_signature"]),
    )


def _high_fidelity_report_to_json(report: HighFidelityEvaluationReport) -> str:
    return _canonical_json(asdict(report))


def _row_to_run(row: sqlite3.Row) -> EvolutionRun:
    return EvolutionRun(
        run_id=row["run_id"],
        batch_id=row["batch_id"],
        status=EvolutionRunStatus(row["status"]),
        target_project=row["target_project"],
        target_branch=row["target_branch"],
        base_sha=row["base_sha"],
        global_prompt_set_hash=row["global_prompt_set_hash"],
        target_prompt_set_hash=row["target_prompt_set_hash"],
        source_watermark=row["source_watermark"] or "",
        branch_name=row["branch_name"] or "",
        commit_sha=row["commit_sha"] or "",
        mr_iid=row["mr_iid"] or "",
        mr_url=row["mr_url"] or "",
        error_code=row["error_code"] or "",
        error_message=row["error_message"] or "",
    )


class PromptEvolutionStore:
    """Durable store for weekly Prompt evolution runs, candidates, and evidence.

    Never raises out of public read methods (returns empty/None); write
    methods raise only on programmer error (unsupported fields) and otherwise
    log and swallow so the weekly runner can fail closed without crashing.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        # Ensure tables exist before any read/write so callers never hit
        # "no such table" on a fresh database file.
        self.migrate()

    def migrate(self) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            conn.executescript(_SCHEMA)
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(prompt_evolution_evidence)").fetchall()
            }
            if "project_rules_hash" not in columns:
                conn.execute(
                    "ALTER TABLE prompt_evolution_evidence "
                    "ADD COLUMN project_rules_hash TEXT NOT NULL DEFAULT ''"
                )
            replay_columns = {
                "commit_sha": "TEXT NOT NULL DEFAULT ''",
                "existing_code": "TEXT NOT NULL DEFAULT ''",
                "improved_code": "TEXT NOT NULL DEFAULT ''",
                "line_start": "INTEGER NOT NULL DEFAULT 0",
                "line_end": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in replay_columns.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE prompt_evolution_evidence ADD COLUMN {name} {declaration}"
                    )
            optimization_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(project_skill_optimization_steps)").fetchall()
            }
            if "high_fidelity_json" not in optimization_columns:
                conn.execute(
                    "ALTER TABLE project_skill_optimization_steps "
                    "ADD COLUMN high_fidelity_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "execution_mode" not in optimization_columns:
                conn.execute(
                    "ALTER TABLE project_skill_optimization_steps "
                    "ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'fragment'"
                )
        run_write_transaction(self._path, _op)

    def table_names(self) -> set[str]:
        def _op(conn: sqlite3.Connection) -> set[str]:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return {r[0] for r in rows}
        return run_write_transaction(self._path, _op)

    def start_run(self, batch_id: str, target_project: str, target_branch: str,
                  base_sha: str, global_prompt_set_hash: str,
                  target_prompt_set_hash: str, fencing_token: int) -> EvolutionRun:
        existing = self.get_run_by_batch(batch_id)
        if existing is not None:
            return existing
        run_id = hashlib.sha256(f"{batch_id}\n{target_project}\n{target_branch}".encode("utf-8")).hexdigest()
        now = now_cn_iso()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO prompt_evolution_runs "
                "(run_id, batch_id, target_project, target_branch, base_sha, "
                "global_prompt_set_hash, target_prompt_set_hash, fencing_token, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, batch_id, target_project, target_branch, base_sha,
                 global_prompt_set_hash, target_prompt_set_hash, fencing_token,
                 str(EvolutionRunStatus.CREATED), now, now),
            )
        run_write_transaction(self._path, _op)
        rehydrated = self.get_run_by_batch(batch_id)
        assert rehydrated is not None  # just inserted
        return rehydrated

    def update_run(self, run_id: str, status: EvolutionRunStatus, **fields: Any) -> None:
        bad = set(fields) - _ALLOWED_UPDATE_FIELDS
        if bad:
            raise ValueError(f"unsupported update fields: {sorted(bad)}")
        if status not in {EvolutionRunStatus.FAILED_RETRYABLE, EvolutionRunStatus.FAILED_TERMINAL}:
            fields.setdefault("error_code", "")
            fields.setdefault("error_message", "")
        now = now_cn_iso()
        assignments = [f"{name} = ?" for name in fields]
        values = list(fields.values())
        assignments.append("status = ?")
        values.append(str(status))
        assignments.append("updated_at = ?")
        values.append(now)
        values.append(run_id)
        sql = (
            "UPDATE prompt_evolution_runs SET "
            + ", ".join(assignments)
            + " WHERE run_id = ?"
        )

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(sql, values)
        run_write_transaction(self._path, _op)

    def get_run_by_batch(self, batch_id: str) -> EvolutionRun | None:
        def _op(conn: sqlite3.Connection) -> EvolutionRun | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM prompt_evolution_runs WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            return _row_to_run(row) if row else None
        return run_write_transaction(self._path, _op)

    def save_source_snapshot(self, run_id: str, evidence: Iterable[Evidence]) -> None:
        rows = list(evidence)
        if not rows:
            payload = [_canonical_json({"manifest": "empty"})]
            suggestion_ids: list[str] = [_EMPTY_MANIFEST_ID]
        else:
            payload = [_evidence_to_json(e) for e in rows]
            suggestion_ids = [e.suggestion_id for e in rows]

        def _op(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "INSERT OR REPLACE INTO prompt_evolution_source_evidence "
                "(run_id, suggestion_id, evidence_json) VALUES (?, ?, ?)",
                [(run_id, sid, body) for sid, body in zip(suggestion_ids, payload)],
            )
        run_write_transaction(self._path, _op)

    def get_source_snapshot(self, run_id: str) -> tuple[Evidence, ...]:
        def _op(conn: sqlite3.Connection) -> tuple[Evidence, ...]:
            rows = conn.execute(
                "SELECT suggestion_id, evidence_json FROM prompt_evolution_source_evidence "
                "WHERE run_id = ? ORDER BY suggestion_id",
                (run_id,),
            ).fetchall()
            result: list[Evidence] = []
            for sid, body in rows:
                if sid == _EMPTY_MANIFEST_ID:
                    continue
                result.append(_evidence_from_json(body))
            return tuple(result)
        return run_write_transaction(self._path, _op)

    def save_candidate(self, run_id: str, candidate: EligibleCandidate,
                       fingerprint: str, status: str = "snapshotted") -> str:
        stored_id = _stored_candidate_id(run_id, candidate.candidate_id)
        now = now_cn_iso()
        cluster = candidate.cluster
        candidate_json = _candidate_to_json(candidate)

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO prompt_evolution_candidates "
                "(candidate_id, run_id, scope, project, cluster_key, fingerprint, "
                "source_prompt_hash, positive_weight, negative_weight, negative_ratio, "
                "candidate_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stored_id, run_id, str(candidate.scope), candidate.project,
                 cluster.cluster_key, fingerprint, candidate.source_prompt_hash,
                 cluster.positive_weight, cluster.negative_weight, cluster.negative_ratio,
                 candidate_json, status, now, now),
            )
        run_write_transaction(self._path, _op)
        return stored_id

    def save_evidence_snapshot(self, stored_candidate_id: str, evidence: Iterable[Evidence]) -> None:
        rows = list(evidence)

        def _op(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "INSERT OR IGNORE INTO prompt_evolution_evidence "
                "(candidate_id, suggestion_id, project, mr_iid, mr_url, source_created_at, "
                "file_path, label, summary, suggestion_content, captured_outcome, "
                "captured_weight, global_prompt_set_hash, prompt_bundle_hash, project_rules_hash, "
                "commit_sha, existing_code, improved_code, line_start, line_end, feedback_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (stored_candidate_id, e.suggestion_id, e.project, e.mr_iid, e.mr_url,
                     e.created_at, e.file_path, e.label, e.summary, e.suggestion_content,
                     str(e.outcome), float(e.weight), e.global_prompt_set_hash,
                     e.prompt_bundle_hash, e.project_rules_hash, e.commit_sha, e.existing_code,
                     e.improved_code, e.line_start, e.line_end, _canonical_json(list(e.feedback)))
                    for e in rows
                ],
            )
        run_write_transaction(self._path, _op)

    def save_proposal(self, run_id: str, proposal: PromptProposal) -> None:
        body = _proposal_to_json(proposal)

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE prompt_evolution_candidates SET proposal_json = ?, updated_at = ? "
                "WHERE run_id = ?",
                (body, now_cn_iso(), run_id),
            )
        run_write_transaction(self._path, _op)

    def save_validation(self, run_id: str, report: ValidationReport) -> None:
        body = _validation_to_json(report)

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE prompt_evolution_candidates SET validation_json = ?, updated_at = ? "
                "WHERE run_id = ?",
                (body, now_cn_iso(), run_id),
            )
        run_write_transaction(self._path, _op)

    def get_candidates_for_run(self, run_id: str) -> tuple[EligibleCandidate, ...]:
        def _op(conn: sqlite3.Connection) -> tuple[EligibleCandidate, ...]:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT candidate_id, candidate_json FROM prompt_evolution_candidates "
                "WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            return tuple(_candidate_from_json(r["candidate_json"]) for r in rows)
        return run_write_transaction(self._path, _op)

    def get_proposal(self, run_id: str) -> PromptProposal | None:
        def _op(conn: sqlite3.Connection) -> PromptProposal | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT proposal_json FROM prompt_evolution_candidates "
                "WHERE run_id = ? AND proposal_json IS NOT NULL LIMIT 1",
                (run_id,),
            ).fetchone()
            return _proposal_from_json(row["proposal_json"]) if row else None
        return run_write_transaction(self._path, _op)

    def get_validation(self, run_id: str) -> ValidationReport | None:
        def _op(conn: sqlite3.Connection) -> ValidationReport | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT validation_json FROM prompt_evolution_candidates "
                "WHERE run_id = ? AND validation_json IS NOT NULL LIMIT 1",
                (run_id,),
            ).fetchone()
            return _validation_from_json(row["validation_json"]) if row else None
        return run_write_transaction(self._path, _op)

    def save_optimization_step(
        self,
        run_id: str,
        project: str,
        base_skill_hash: str,
        candidate_skill_hash: str,
        batch: SkillOptimizationBatch,
        report: SkillOptimizationReport,
        proposal_hash: str,
        high_fidelity_report=None,
        execution_mode: str = "fragment",
    ) -> str:
        training_ids = tuple(sorted(
            item.suggestion_id
            for candidate in batch.training_candidates
            for item in candidate.cluster.evidence
        ))
        selection_ids = tuple(batch.selection_ids)
        control_ids = tuple(batch.control_ids)
        step_id = hashlib.sha256(
            (
                f"{run_id}\n{project}\n{base_skill_hash}\n{candidate_skill_hash}\n"
                f"{batch.split_hash}\n{report.edit_signature}\n{proposal_hash}"
            ).encode("utf-8")
        ).hexdigest()
        baseline_scores = _canonical_json({
            "overall": report.baseline_score,
            "accepted": report.baseline_accepted_score,
            "rejected": report.baseline_rejected_score,
        })
        candidate_scores = _canonical_json({
            "overall": report.candidate_score,
            "accepted": report.candidate_accepted_score,
            "rejected": report.candidate_rejected_score,
        })
        now = now_cn_iso()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO project_skill_optimization_steps "
                "(step_id, run_id, project, base_skill_hash, candidate_skill_hash, split_hash, "
                "training_ids_json, selection_ids_json, control_ids_json, edit_budget, edit_count, "
                "edit_signature, replay_model, baseline_scores_json, candidate_scores_json, action, "
                "errors_json, report_json, high_fidelity_json, execution_mode, proposal_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    step_id,
                    run_id,
                    project,
                    base_skill_hash,
                    candidate_skill_hash,
                    batch.split_hash,
                    _canonical_json(training_ids),
                    _canonical_json(selection_ids),
                    _canonical_json(control_ids),
                    report.edit_budget,
                    report.edit_count,
                    report.edit_signature,
                    report.replay_model,
                    baseline_scores,
                    candidate_scores,
                    report.action,
                    _canonical_json(report.errors),
                    _optimization_report_to_json(report),
                    _canonical_json(asdict(high_fidelity_report)) if high_fidelity_report is not None else "{}",
                    str(execution_mode),
                    proposal_hash,
                    now,
                ),
            )

        run_write_transaction(self._path, _op)
        return step_id

    def save_prompt_evaluation_batch(self, run_id: str, batch: PromptEvaluationBatch) -> None:
        training_ids = tuple(sorted(
            item.suggestion_id
            for candidate in batch.training_candidates
            for item in candidate.cluster.evidence
        ))
        now = now_cn_iso()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO global_prompt_evaluation_batches "
                "(run_id, base_prompt_hash, split_hash, training_ids_json, selection_ids_json, "
                "control_ids_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET base_prompt_hash=excluded.base_prompt_hash, "
                "split_hash=excluded.split_hash, training_ids_json=excluded.training_ids_json, "
                "selection_ids_json=excluded.selection_ids_json, control_ids_json=excluded.control_ids_json, "
                "report_json='{}', updated_at=excluded.updated_at",
                (
                    run_id,
                    batch.base_prompt_hash,
                    batch.split_hash,
                    _canonical_json(training_ids),
                    _canonical_json(batch.selection_ids),
                    _canonical_json(batch.control_ids),
                    now,
                    now,
                ),
            )

        run_write_transaction(self._path, _op)

    def save_prompt_behavioral_report(self, run_id: str, report: HighFidelityEvaluationReport) -> None:
        body = _high_fidelity_report_to_json(report)

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE global_prompt_evaluation_batches SET report_json = ?, updated_at = ? WHERE run_id = ?",
                (body, now_cn_iso(), run_id),
            )

        run_write_transaction(self._path, _op)

    def get_prompt_evaluation_audit(self, run_id: str) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM global_prompt_evaluation_batches WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "run_id": row["run_id"],
                "base_prompt_hash": row["base_prompt_hash"],
                "split_hash": row["split_hash"],
                "training_ids": tuple(json.loads(row["training_ids_json"])),
                "selection_ids": tuple(json.loads(row["selection_ids_json"])),
                "control_ids": tuple(json.loads(row["control_ids_json"])),
                "report": json.loads(row["report_json"] or "{}"),
            }

        return run_write_transaction(self._path, _op)

    def get_optimization_step(self, step_id: str) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM project_skill_optimization_steps WHERE step_id = ?",
                (step_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "step_id": row["step_id"],
                "run_id": row["run_id"],
                "project": row["project"],
                "base_skill_hash": row["base_skill_hash"],
                "candidate_skill_hash": row["candidate_skill_hash"],
                "training_ids": tuple(json.loads(row["training_ids_json"])),
                "selection_ids": tuple(json.loads(row["selection_ids_json"])),
                "control_ids": tuple(json.loads(row["control_ids_json"])),
                "proposal_hash": row["proposal_hash"],
                "report": _optimization_report_from_json(row["report_json"]),
                "high_fidelity": json.loads(row["high_fidelity_json"] or "{}"),
                "execution_mode": row["execution_mode"],
            }

        return run_write_transaction(self._path, _op)

    def get_rejected_edit_buffer(
        self,
        project: str,
        base_skill_hash: str,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        bounded_limit = max(0, min(int(limit), 100))
        if bounded_limit == 0:
            return ()

        def _op(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT edit_signature, errors_json, baseline_scores_json, "
                "candidate_scores_json, created_at "
                "FROM project_skill_optimization_steps "
                "WHERE project = ? AND base_skill_hash = ? AND action = 'reject' "
                "ORDER BY id DESC LIMIT ?",
                (project, base_skill_hash, bounded_limit),
            ).fetchall()
            return tuple({
                "edit_signature": row["edit_signature"],
                "errors": tuple(json.loads(row["errors_json"])),
                "baseline_score": str(json.loads(row["baseline_scores_json"])["overall"]),
                "candidate_score": str(json.loads(row["candidate_scores_json"])["overall"]),
                "created_at": row["created_at"],
            } for row in rows)

        return run_write_transaction(self._path, _op)

    def get_watermark(self) -> str | None:
        def _op(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT meta_value FROM prompt_evolution_meta WHERE meta_key = 'source_watermark'"
            ).fetchone()
            return row[0] if row else None
        return run_write_transaction(self._path, _op)

    def set_watermark(self, value: str) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO prompt_evolution_meta (meta_key, meta_value) "
                "VALUES ('source_watermark', ?)",
                (value,),
            )
        run_write_transaction(self._path, _op)

    def candidate_is_suppressed(self, fingerprint: str, source_prompt_hash: str,
                                negative_weight: float, now: str,
                                cooldown_days: int) -> bool:
        """Suppress open/merged candidates for the same source hash; suppress
        closed/terminal candidates until cooldown expires unless negative weight
        grew by at least 1.0."""
        def _op(conn: sqlite3.Connection) -> bool:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT c.source_prompt_hash, c.negative_weight, r.status, r.mr_iid, "
                "r.updated_at FROM prompt_evolution_candidates c "
                "JOIN prompt_evolution_runs r ON c.run_id = r.run_id "
                "WHERE c.fingerprint = ?",
                (fingerprint,),
            ).fetchall()
            for row in rows:
                if row["source_prompt_hash"] != source_prompt_hash:
                    continue
                status_str = row["status"]
                if status_str in (str(EvolutionRunStatus.MR_OPEN), str(EvolutionRunStatus.MERGED)):
                    return True
                if status_str in (str(EvolutionRunStatus.CLOSED),
                                  str(EvolutionRunStatus.FAILED_TERMINAL)):
                    from datetime import datetime, timezone
                    try:
                        closed_at = datetime.fromisoformat(row["updated_at"])
                        now_dt = datetime.fromisoformat(now)
                        if closed_at.tzinfo is None:
                            closed_at = closed_at.replace(tzinfo=timezone.utc)
                        if now_dt.tzinfo is None:
                            now_dt = now_dt.replace(tzinfo=timezone.utc)
                        age_days = (now_dt - closed_at).total_seconds() / 86400
                    except Exception:
                        age_days = 0.0
                    if age_days < cooldown_days:
                        prior_negative = float(row["negative_weight"] or 0.0)
                        if negative_weight - prior_negative >= 1.0:
                            return False
                        return True
            return False
        return run_write_transaction(self._path, _op)

    def mark_mr_state(self, mr_iid: str, state: str, updated_at: str,
                      target_project: str | None = None) -> None:
        """Map GitLab ``merged``/``closed`` state to the owning run and
        candidates; never reopen a merged candidate."""
        state_map = {
            "merged": str(EvolutionRunStatus.MERGED),
            "closed": str(EvolutionRunStatus.CLOSED),
        }
        new_status = state_map.get(state)
        if new_status is None:
            return

        def _op(conn: sqlite3.Connection) -> None:
            conn.row_factory = sqlite3.Row
            if target_project:
                rows = conn.execute(
                    "SELECT run_id, status FROM prompt_evolution_runs "
                    "WHERE mr_iid = ? AND target_project = ?",
                    (mr_iid, target_project),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id, status FROM prompt_evolution_runs WHERE mr_iid = ?",
                    (mr_iid,),
                ).fetchall()
            for row in rows:
                if row["status"] == str(EvolutionRunStatus.MERGED):
                    continue  # never reopen a merged candidate
                conn.execute(
                    "UPDATE prompt_evolution_runs SET status = ?, updated_at = ? "
                    "WHERE run_id = ?",
                    (new_status, updated_at, row["run_id"]),
                )
        run_write_transaction(self._path, _op)

    def list_reconcilable_mrs(self) -> list[EvolutionRun]:
        def _op(conn: sqlite3.Connection) -> list[EvolutionRun]:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM prompt_evolution_runs "
                "WHERE status = ? AND mr_iid IS NOT NULL AND mr_iid != '' "
                "ORDER BY id",
                (str(EvolutionRunStatus.MR_OPEN),),
            ).fetchall()
            return [_row_to_run(r) for r in rows]
        return run_write_transaction(self._path, _op)
