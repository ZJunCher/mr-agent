"""Immediate Pipeline settlement and conservative confidence updates.

When memory retrieval occurs, all selected hints share one stable attempt ID.
When ``UTAgent.resume()`` receives the next Pipeline event, it settles every
pending injected attempt for that task against that exact event before
continuing the graph. A terminal path with no post-repair Pipeline settles
pending attempts as ``no_validation``.

Confidence updates are deliberately conservative:
- a new project memory starts at ``0.60``;
- a new global memory starts at ``0.70``;
- each additional independent supporting episode adds ``0.05``, capped at ``0.80``;
- an injected attempt whose immediate Pipeline succeeds adds ``0.03``;
- an injected attempt whose immediate Pipeline fails subtracts ``0.02``;
- one failure never disables a memory;
- after at least three settled injected attempts, confidence below ``0.45``
  changes status to ``needs_review``.

Confidence is recomputed from persisted evidence and outcome counters, not
incremented blindly. Replays and ``no_validation`` outcomes do not change it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pr_agent.feedback.store import _connect, get_db_path
from pr_agent.log import get_logger
from pr_agent.storage.sqlite import run_write_transaction
from ut_agent.repair_memory.config import load_repair_memory_settings
from ut_agent.repair_memory.models import MemoryStatus, _json_dumps


@dataclass(frozen=True)
class SettlementSummary:
    """Counts from one settlement operation."""

    settled_attempts: int
    settled_hits: int


def _now_iso() -> str:
    from pr_agent.feedback.timez import now_cn_iso

    return now_cn_iso()


def _recompute_confidence(
    memory_id: str,
    *,
    success_delta: int,
    failure_delta: int,
    conn,
) -> tuple[float, MemoryStatus]:
    """Recompute confidence from persisted counters and return (confidence, status).

    Applies the exact project/global base formulas from the design, then applies
    ``+0.03`` per success and ``-0.02`` per settled non-success before clamping
    to ``[0.30, 0.95]``. Marks ``needs_review`` after the configured minimum
    settled attempts when confidence drops below the threshold.
    """
    settings = load_repair_memory_settings()
    row = conn.execute(
        "SELECT scope, support_episode_count, settled_attempts, immediate_successes, "
        "support_project_count FROM repair_memories WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        return (0.0, MemoryStatus.DISABLED)

    scope = str(row[0])
    support_episodes = int(row[1])
    settled = int(row[2]) + success_delta + failure_delta
    successes = int(row[3]) + success_delta

    if scope == "global":
        base = min(
            0.80,
            settings.global_initial_confidence
            + max(0, support_episodes - 2) * settings.support_confidence_increment,
        )
    else:
        base = min(
            0.80,
            settings.project_initial_confidence
            + max(0, support_episodes - 1) * settings.support_confidence_increment,
        )

    confidence = max(
        0.30,
        min(
            0.95,
            base
            + successes * settings.success_confidence_increment
            - (settled - successes) * settings.failure_confidence_decrement,
        ),
    )

    status = MemoryStatus.ACTIVE
    if settled >= settings.needs_review_min_attempts and confidence < settings.needs_review_confidence:
        status = MemoryStatus.NEEDS_REVIEW

    return (confidence, status)


def settle_immediate_pipeline(
    task_id: str,
    pipeline_event: Any,
    path: str | None = None,
) -> SettlementSummary:
    """Settle pending injected attempts for ``task_id`` against one Pipeline event.

    Groups hits by attempt ID, writes immediate Pipeline identity/status, updates
    each distinct memory's settled/success counters once per attempt, recomputes
    confidence, and appends audit events. Replays find no pending rows and perform
    no second update. Never raises.
    """
    db_path = path or get_db_path()
    pipeline_id = int(getattr(pipeline_event, "pipeline_id", 0) or 0)
    pipeline_sha = str(getattr(pipeline_event, "sha", "") or "")
    pipeline_status = str(getattr(pipeline_event, "status", "") or "")
    is_success = pipeline_status.lower() == "success"
    now = _now_iso()

    try:

        def write(conn) -> SettlementSummary:
            conn.row_factory = None
            pending = conn.execute(
                "SELECT attempt_id, memory_id FROM repair_memory_hits "
                "WHERE task_id = ? AND mode = 'inject' AND settled_at IS NULL",
                (task_id,),
            ).fetchall()
            if not pending:
                return SettlementSummary(settled_attempts=0, settled_hits=0)

            attempts: dict[str, list[str]] = {}
            for attempt_id, memory_id in pending:
                attempts.setdefault(attempt_id, []).append(memory_id)

            settled_hits = 0
            for attempt_id, memory_ids in attempts.items():
                outcome = "success" if is_success else "failed"
                conn.execute(
                    "UPDATE repair_memory_hits "
                    "SET immediate_pipeline_id = ?, immediate_pipeline_sha = ?, "
                    "immediate_pipeline_status = ?, outcome = ?, settled_at = ? "
                    "WHERE attempt_id = ? AND settled_at IS NULL",
                    (pipeline_id, pipeline_sha, pipeline_status, outcome, now, attempt_id),
                )
                settled_hits += len(memory_ids)

                # Update each distinct memory once per attempt.
                seen: set[str] = set()
                for memory_id in memory_ids:
                    if memory_id in seen:
                        continue
                    seen.add(memory_id)
                    success_delta = 1 if is_success else 0
                    failure_delta = 0 if is_success else 1
                    confidence, status = _recompute_confidence(
                        memory_id,
                        success_delta=success_delta,
                        failure_delta=failure_delta,
                        conn=conn,
                    )
                    conn.execute(
                        "UPDATE repair_memories "
                        "SET confidence = ?, settled_attempts = settled_attempts + 1, "
                        "immediate_successes = immediate_successes + ?, "
                        "status = ?, updated_at = ? "
                        "WHERE memory_id = ?",
                        (confidence, success_delta, status.value, now, memory_id),
                    )
                    if is_success:
                        event_type = "reinforced"
                    elif status is MemoryStatus.NEEDS_REVIEW:
                        event_type = "needs_review"
                    else:
                        event_type = "settled"
                    conn.execute(
                        "INSERT INTO repair_memory_events "
                        "(memory_id, event_type, reason, metadata_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            memory_id,
                            event_type,
                            f"immediate pipeline {pipeline_status}",
                            _json_dumps({"pipeline_id": pipeline_id, "outcome": outcome}),
                            now,
                        ),
                    )

            return SettlementSummary(settled_attempts=len(attempts), settled_hits=settled_hits)

        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to settle immediate pipeline: {type(error).__name__}")
        return SettlementSummary(settled_attempts=0, settled_hits=0)


def settle_without_validation(
    task_id: str,
    reason: str,
    path: str | None = None,
) -> SettlementSummary:
    """Settle pending injected attempts that never reached a validation Pipeline.

    Uses a fixed reason; does not store arbitrary exception bodies. Never raises.
    """
    db_path = path or get_db_path()
    now = _now_iso()

    try:

        def write(conn) -> SettlementSummary:
            pending = conn.execute(
                "SELECT attempt_id, memory_id FROM repair_memory_hits "
                "WHERE task_id = ? AND mode = 'inject' AND settled_at IS NULL",
                (task_id,),
            ).fetchall()
            if not pending:
                return SettlementSummary(settled_attempts=0, settled_hits=0)

            attempts: set[str] = set()
            for attempt_id, _ in pending:
                attempts.add(attempt_id)
                conn.execute(
                    "UPDATE repair_memory_hits "
                    "SET outcome = 'no_validation', settled_at = ? "
                    "WHERE attempt_id = ? AND settled_at IS NULL",
                    (now, attempt_id),
                )

            return SettlementSummary(settled_attempts=len(attempts), settled_hits=len(pending))

        return run_write_transaction(db_path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to settle without validation: {type(error).__name__}")
        return SettlementSummary(settled_attempts=0, settled_hits=0)


def memory_effectiveness_summary(
    days: int | None = None,
    project: str | None = None,
    path: str | None = None,
) -> dict[str, int | float]:
    """Return the read-only effectiveness summary for the dashboard.

    Counts distinct attempt IDs for attempt metrics. Returns the exact
    zero-valued dictionary on missing tables or query failure.
    """
    db_path = path or get_db_path()
    zero: dict[str, int | float] = {
        "eligible_episodes": 0,
        "active_project_memories": 0,
        "active_global_memories": 0,
        "shadow_attempts": 0,
        "injected_attempts": 0,
        "settled_pipeline_attempts": 0,
        "immediate_successes": 0,
        "immediate_success_rate": 0,
        "no_validation_attempts": 0,
        "needs_review": 0,
    }
    try:
        conn = _connect(db_path)
        try:
            eligible = conn.execute(
                "SELECT COUNT(*) FROM repair_memory_episodes WHERE eligibility_reason = 'eligible'"
            ).fetchone()[0]
            project_memories = conn.execute(
                "SELECT COUNT(*) FROM repair_memories WHERE scope = 'project' AND status = 'active'"
            ).fetchone()[0]
            global_memories = conn.execute(
                "SELECT COUNT(*) FROM repair_memories WHERE scope = 'global' AND status = 'active'"
            ).fetchone()[0]
            shadow = conn.execute(
                "SELECT COUNT(DISTINCT attempt_id) FROM repair_memory_hits WHERE mode = 'shadow'"
            ).fetchone()[0]
            injected = conn.execute(
                "SELECT COUNT(DISTINCT attempt_id) FROM repair_memory_hits WHERE mode = 'inject'"
            ).fetchone()[0]
            settled_pipeline = conn.execute(
                "SELECT COUNT(DISTINCT attempt_id) FROM repair_memory_hits "
                "WHERE mode = 'inject' AND outcome IN ('success', 'failed')"
            ).fetchone()[0]
            successes = conn.execute(
                "SELECT COUNT(DISTINCT attempt_id) FROM repair_memory_hits "
                "WHERE mode = 'inject' AND outcome = 'success'"
            ).fetchone()[0]
            no_validation = conn.execute(
                "SELECT COUNT(DISTINCT attempt_id) FROM repair_memory_hits "
                "WHERE mode = 'inject' AND outcome = 'no_validation'"
            ).fetchone()[0]
            needs_review = conn.execute(
                "SELECT COUNT(*) FROM repair_memories WHERE status = 'needs_review'"
            ).fetchone()[0]

            rate = round(100.0 * successes / settled_pipeline, 1) if settled_pipeline else 0
            return {
                "eligible_episodes": int(eligible),
                "active_project_memories": int(project_memories),
                "active_global_memories": int(global_memories),
                "shadow_attempts": int(shadow),
                "injected_attempts": int(injected),
                "settled_pipeline_attempts": int(settled_pipeline),
                "immediate_successes": int(successes),
                "immediate_success_rate": rate,
                "no_validation_attempts": int(no_validation),
                "needs_review": int(needs_review),
            }
        finally:
            conn.close()
    except Exception as error:
        get_logger().error(f"Failed to build memory effectiveness summary: {type(error).__name__}")
        return dict(zero)
