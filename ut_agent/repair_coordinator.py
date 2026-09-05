"""Pure repair lifecycle facts shared by policy, result extraction, and workers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pr_agent.config_loader import get_settings
from ut_agent.execution_ledger import ExecutionLedger, ToolAttempt, build_execution_ledger

_NONTERMINAL_PIPELINE_STATUSES = {
    "",
    "created",
    "pending",
    "preparing",
    "running",
    "waiting_for_resource",
}
_TERMINAL_PIPELINE_STATUSES = {"success", "failed", "canceled", "skipped"}


class PublicationPhase(StrEnum):
    NONE = "none"
    UNVERIFIED = "unverified"
    NONTERMINAL = "nonterminal"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class TerminalPipelineProof:
    attempt_id: str
    commit_sha: str
    pipeline_id: int
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "commit_sha": self.commit_sha,
            "pipeline_id": self.pipeline_id,
            "status": self.status,
        }


@dataclass(frozen=True)
class RepairCoordinatorSnapshot:
    ledger: ExecutionLedger
    published_attempt_count: int
    latest_push_attempt: ToolAttempt | None
    latest_pushed_sha: str
    latest_attempt_id: str
    publication_phase: PublicationPhase
    latest_exact_pipeline: dict[str, Any] | None
    terminal_proof: TerminalPipelineProof | None

    @property
    def requires_exact_pipeline(self) -> bool:
        return self.publication_phase in {PublicationPhase.UNVERIFIED, PublicationPhase.NONTERMINAL}


def _successful_push_attempts(ledger: ExecutionLedger) -> list[ToolAttempt]:
    pushes = []
    seen = set()
    for attempt in ledger.tool_attempts:
        result = attempt.result or {}
        if (
            attempt.name != "commit_and_push_tool"
            or result.get("status") != "success"
            or result.get("changed") is not True
            or not result.get("commit_sha")
        ):
            continue
        identity = str(result.get("commit_sha") or "")
        if identity in seen:
            continue
        seen.add(identity)
        pushes.append(attempt)
    return pushes


def _pipeline_matches_attempt(pipeline: dict, pushed_sha: str, attempt_id: str) -> bool:
    if str(pipeline.get("status") or "").lower() not in {"success", "running"}:
        return False
    if str(pipeline.get("requested_commit_sha") or "") != pushed_sha:
        return False
    if str(pipeline.get("matched_commit_sha") or "") != pushed_sha:
        return False
    pipeline_attempt_id = str(pipeline.get("attempt_id") or "")
    return not attempt_id or not pipeline_attempt_id or pipeline_attempt_id == attempt_id


def _pipeline_id(pipeline: dict) -> int:
    try:
        return int(pipeline.get("validation_pipeline_id") or pipeline.get("pipeline_id") or 0)
    except (TypeError, ValueError):
        return 0


def build_repair_snapshot(messages: list) -> RepairCoordinatorSnapshot:
    ledger = build_execution_ledger(messages)
    pushes = _successful_push_attempts(ledger)
    if not pushes:
        return RepairCoordinatorSnapshot(
            ledger=ledger,
            published_attempt_count=0,
            latest_push_attempt=None,
            latest_pushed_sha="",
            latest_attempt_id="",
            publication_phase=PublicationPhase.NONE,
            latest_exact_pipeline=None,
            terminal_proof=None,
        )

    latest_push = pushes[-1]
    push_result = latest_push.result or {}
    pushed_sha = str(push_result.get("commit_sha") or "")
    attempt_id = str(push_result.get("attempt_id") or "")
    exact_pipelines = [
        pipeline
        for pipeline in ledger.pipelines
        if int(pipeline.get("_sequence") or 0) > latest_push.sequence
        and _pipeline_matches_attempt(pipeline, pushed_sha, attempt_id)
    ]
    terminal_pipelines = [
        pipeline
        for pipeline in exact_pipelines
        if str(pipeline.get("pipeline_status") or "").lower() in _TERMINAL_PIPELINE_STATUSES
    ]
    pipeline = terminal_pipelines[-1] if terminal_pipelines else (exact_pipelines[-1] if exact_pipelines else None)
    if pipeline is None:
        phase = PublicationPhase.UNVERIFIED
        proof = None
    else:
        status = str(pipeline.get("pipeline_status") or "").lower()
        if status in _TERMINAL_PIPELINE_STATUSES:
            phase = PublicationPhase.TERMINAL
            proof = TerminalPipelineProof(attempt_id, pushed_sha, _pipeline_id(pipeline), status)
        else:
            phase = PublicationPhase.NONTERMINAL
            proof = None

    return RepairCoordinatorSnapshot(
        ledger=ledger,
        published_attempt_count=len(pushes),
        latest_push_attempt=latest_push,
        latest_pushed_sha=pushed_sha,
        latest_attempt_id=attempt_id,
        publication_phase=phase,
        latest_exact_pipeline=pipeline,
        terminal_proof=proof,
    )


def load_max_repair_commits() -> int:
    try:
        value = int(get_settings().get("TRIAGE.MAX_REPAIR_COMMITS", 3))
    except (TypeError, ValueError):
        value = 3
    return max(1, value)


def terminal_guard(snapshot: RepairCoordinatorSnapshot) -> tuple[bool, str]:
    if snapshot.requires_exact_pipeline:
        return False, f"修复提交 {snapshot.latest_pushed_sha} 尚未完成精确流水线验证。"
    return True, ""
