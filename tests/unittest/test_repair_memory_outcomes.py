"""Focused tests for repair-memory outcome settlement and confidence updates.

Covers Task 5 of the UT-Agent repair-memory implementation plan:
- one Pipeline event settles all hints as one successful attempt;
- replayed Pipeline event is idempotent;
- single failure does not disable a memory;
- low confidence after three settled attempts moves to needs_review;
- no-validation attempts are excluded from the primary rate;
- memory effectiveness summary counts distinct attempts.
"""

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before the eager ut_agent package import
from tests.unittest.repair_memory_helpers import (
    sample_memory,
)
from ut_agent.repair_memory.models import MemoryStatus
from ut_agent.repair_memory.outcomes import (
    memory_effectiveness_summary,
    settle_immediate_pipeline,
    settle_without_validation,
)
from ut_agent.repair_memory.store import (
    init_repair_memory_tables,
    load_memory,
)


@pytest.fixture
def memory_db(tmp_path) -> str:
    path = str(tmp_path / "repair-memory.db")
    init_repair_memory_tables(path)
    return path


def _seed_injected_attempt(
    db_path: str,
    *,
    attempt_id: str = "attempt-1",
    memory_ids: tuple[str, ...] = ("mem-1",),
    task_id: str = "task-1",
    root_cause_group_id: str = "root-1",
    confidence: float = 0.60,
    settled_attempts: int = 0,
    immediate_successes: int = 0,
) -> None:
    """Seed one injected attempt with one hit per memory."""
    from pr_agent.feedback.store import _connect
    from ut_agent.repair_memory.models import _json_dumps

    for _rank, mid in enumerate(memory_ids, start=1):
        memory = sample_memory(
            memory_id=mid,
            confidence=confidence,
            pattern_key=f"pattern-{mid}",
            settled_attempts=settled_attempts,
            immediate_successes=immediate_successes,
        )
        from ut_agent.repair_memory.store import save_memory

        save_memory(memory, db_path)
    conn = _connect(db_path)
    try:
        with conn:
            for rank, mid in enumerate(memory_ids, start=1):
                conn.execute(
                    "INSERT OR IGNORE INTO repair_memory_hits "
                    "(attempt_id, task_id, root_cause_group_id, current_project, "
                    "source_pipeline_id, source_sha, memory_id, memory_scope, rank, "
                    "score_json, mode, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        attempt_id, task_id, root_cause_group_id, "group/a",
                        100, "a" * 40, mid, "project", rank,
                        _json_dumps({"total": 90}), "inject",
                        "2026-08-15T00:00:00+00:00",
                    ),
                )
    finally:
        conn.close()


def _successful_pipeline_event():
    from pr_agent.distributed.models import PipelineEvent

    return PipelineEvent.new(
        project_id="group/a",
        pipeline_id=200,
        sha="b" * 40,
        status="success",
        ref="feature/a",
    )


def _failed_pipeline_event():
    from pr_agent.distributed.models import PipelineEvent

    return PipelineEvent.new(
        project_id="group/a",
        pipeline_id=200,
        sha="b" * 40,
        status="failed",
        ref="feature/a",
    )


def test_one_pipeline_event_settles_all_hints_as_one_successful_attempt(memory_db):
    _seed_injected_attempt(memory_db, attempt_id="attempt-1", memory_ids=("mem-1", "mem-2"))

    summary = settle_immediate_pipeline("task-1", _successful_pipeline_event(), memory_db)

    assert summary.settled_attempts == 1
    assert summary.settled_hits == 2
    assert load_memory("mem-1", memory_db).confidence == pytest.approx(0.63)
    assert load_memory("mem-2", memory_db).confidence == pytest.approx(0.63)


def test_replayed_pipeline_event_is_idempotent(memory_db):
    _seed_injected_attempt(memory_db, attempt_id="attempt-1", memory_ids=("mem-1",))
    event = _successful_pipeline_event()
    settle_immediate_pipeline("task-1", event, memory_db)
    settle_immediate_pipeline("task-1", event, memory_db)
    assert load_memory("mem-1", memory_db).confidence == pytest.approx(0.63)


def test_single_failure_does_not_disable_memory(memory_db):
    _seed_injected_attempt(memory_db, attempt_id="attempt-1", memory_ids=("mem-1",), confidence=0.60)
    settle_immediate_pipeline("task-1", _failed_pipeline_event(), memory_db)
    memory = load_memory("mem-1", memory_db)
    assert memory.confidence == pytest.approx(0.58)
    assert memory.status is MemoryStatus.ACTIVE


def test_low_confidence_after_three_settled_attempts_needs_review(memory_db):
    # Start with 7 prior failures so one more failure yields settled=8,
    # confidence = 0.60 - 8*0.02 = 0.44 < 0.45.
    _seed_injected_attempt(
        memory_db,
        attempt_id="attempt-8",
        memory_ids=("mem-1",),
        task_id="task-8",
        confidence=0.46,
        settled_attempts=7,
    )
    settle_immediate_pipeline("task-8", _failed_pipeline_event(), memory_db)
    assert load_memory("mem-1", memory_db).status is MemoryStatus.NEEDS_REVIEW


def test_no_validation_is_excluded_from_primary_rate(memory_db):
    _seed_injected_attempt(memory_db, attempt_id="attempt-1", memory_ids=("mem-1",))
    settle_without_validation("task-1", "repair produced no pushed commit", memory_db)
    summary = memory_effectiveness_summary(days=None, project=None, path=memory_db)
    assert summary["injected_attempts"] == 1
    assert summary["settled_pipeline_attempts"] == 0
    assert summary["no_validation_attempts"] == 1
    assert summary["immediate_success_rate"] == 0


def test_effectiveness_summary_counts_distinct_successful_attempts(memory_db):
    _seed_injected_attempt(memory_db, attempt_id="attempt-1", memory_ids=("mem-1",))
    settle_immediate_pipeline("task-1", _successful_pipeline_event(), memory_db)
    summary = memory_effectiveness_summary(days=None, project=None, path=memory_db)
    assert summary["injected_attempts"] == 1
    assert summary["settled_pipeline_attempts"] == 1
    assert summary["immediate_successes"] == 1
    assert summary["immediate_success_rate"] == 100.0
