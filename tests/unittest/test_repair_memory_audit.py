"""Focused tests for task-level repair-memory retrieval audits."""

import sqlite3

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before eager imports
from tests.unittest.repair_memory_helpers import count_rows, sample_memory
from ut_agent.repair_memory.audit import (
    initialize_retrieval_audit,
    list_recent_retrieval_audits,
    load_retrieval_audit,
    query_retrieval_audits,
    record_retrieval_candidate_audits,
    record_retrieval_completion,
    record_retrieval_error,
    record_retrieval_injection,
)
from ut_agent.repair_memory.models import (
    MemoryScope,
    RepairMemoryCandidateAudit,
    RepairQuery,
    RetrievalAuditStatus,
    RetrievalMode,
)
from ut_agent.repair_memory.store import init_repair_memory_tables, save_memory


@pytest.fixture
def memory_db(tmp_path) -> str:
    path = str(tmp_path / "repair-memory.db")
    init_repair_memory_tables(path)
    return path


def _query(*, root_cause_group_id: str = "root-1") -> RepairQuery:
    return RepairQuery(
        project="group/a",
        root_cause_group_id=root_cause_group_id,
        source_pipeline_id=100,
        source_sha="a" * 40,
        failure_category="build",
        job_family="build",
        failure_family="compile_error",
        language="cpp",
        build_system="cmake",
        diagnostic_fingerprint="fingerprint-1",
        causal_tokens=("request", "member"),
    )


def _initialize(path: str, task_id: str = "task-1") -> None:
    assert initialize_retrieval_audit(
        task_id=task_id,
        project="group/a",
        mr_iid=7,
        source_pipeline_id=100,
        source_sha="a" * 40,
        mode=RetrievalMode.INJECT,
        reason_code="repair_session_not_reached",
        path=path,
    )


def test_initializes_one_not_attempted_row_idempotently(memory_db):
    _initialize(memory_db)
    _initialize(memory_db)

    audit = load_retrieval_audit("task-1", memory_db)

    assert audit is not None
    assert audit.status is RetrievalAuditStatus.NOT_ATTEMPTED
    assert audit.reason_code == "repair_session_not_reached"
    assert audit.search_count == 0

    connection = sqlite3.connect(memory_db)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM repair_memory_retrieval_audits WHERE task_id = ?",
            ("task-1",),
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_no_match_records_actual_search_counts(memory_db):
    _initialize(memory_db)

    assert record_retrieval_completion(
        "task-1",
        _query(),
        attempt_id="attempt-1",
        mode=RetrievalMode.INJECT,
        status=RetrievalAuditStatus.NO_MATCH,
        reason_code="below_threshold",
        candidate_count=4,
        passed_threshold_count=0,
        selected_count=0,
        path=memory_db,
    )

    audit = load_retrieval_audit("task-1", memory_db)
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.NO_MATCH
    assert audit.search_count == 1
    assert audit.candidate_count == 4
    assert audit.passed_threshold_count == 0
    assert audit.selected_count == 0
    assert audit.attempted_at


def test_later_recall_overrides_error_and_accumulates_counts(memory_db):
    _initialize(memory_db)
    assert record_retrieval_error(
        "task-1",
        error_code="TimeoutError",
        attempt_id="attempt-error",
        increment_search=True,
        path=memory_db,
    )
    assert record_retrieval_completion(
        "task-1",
        _query(root_cause_group_id="root-2"),
        attempt_id="attempt-recall",
        mode=RetrievalMode.INJECT,
        status=RetrievalAuditStatus.RECALLED,
        reason_code="selected",
        candidate_count=3,
        passed_threshold_count=2,
        selected_count=1,
        path=memory_db,
    )

    audit = load_retrieval_audit("task-1", memory_db)
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.RECALLED
    assert audit.reason_code == "selected"
    assert audit.error_code == ""
    assert audit.search_count == 2
    assert audit.candidate_count == 3
    assert audit.selected_count == 1


def test_no_match_after_recall_does_not_downgrade_or_clear_counts(memory_db):
    _initialize(memory_db)
    assert record_retrieval_completion(
        "task-1", _query(), attempt_id="attempt-recall", mode=RetrievalMode.INJECT,
        status=RetrievalAuditStatus.RECALLED, reason_code="selected",
        candidate_count=3, passed_threshold_count=2, selected_count=1, path=memory_db,
    )
    assert record_retrieval_completion(
        "task-1", _query(root_cause_group_id="root-2"), attempt_id="attempt-empty",
        mode=RetrievalMode.INJECT, status=RetrievalAuditStatus.NO_MATCH,
        reason_code="below_threshold", candidate_count=2, passed_threshold_count=0,
        selected_count=0, path=memory_db,
    )

    audit = load_retrieval_audit("task-1", memory_db)
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.RECALLED
    assert audit.reason_code == "selected"
    assert audit.search_count == 2
    assert audit.candidate_count == 5
    assert audit.selected_count == 1


def test_replayed_completion_and_injection_are_idempotent(memory_db):
    _initialize(memory_db)
    for _ in range(2):
        assert record_retrieval_completion(
            "task-1", _query(), attempt_id="attempt-recall", mode=RetrievalMode.INJECT,
            status=RetrievalAuditStatus.RECALLED, reason_code="selected",
            candidate_count=3, passed_threshold_count=2, selected_count=1, path=memory_db,
        )
        assert record_retrieval_injection("task-1", "attempt-recall", 1, memory_db)

    audit = load_retrieval_audit("task-1", memory_db)
    assert audit is not None
    assert audit.search_count == 1
    assert audit.candidate_count == 3
    assert audit.selected_count == 1
    assert audit.injected_count == 1


def test_candidate_audits_are_idempotent_bounded_and_exposed_on_task(memory_db):
    _initialize(memory_db)
    assert record_retrieval_completion(
        "task-1",
        _query(),
        attempt_id="attempt-1",
        mode=RetrievalMode.SHADOW,
        status=RetrievalAuditStatus.NO_MATCH,
        reason_code="below_threshold",
        candidate_count=1,
        passed_threshold_count=0,
        selected_count=0,
        path=memory_db,
    )
    candidate = RepairMemoryCandidateAudit(
        attempt_id="attempt-1",
        task_id="task-1",
        memory_id="mem-1",
        memory_scope=MemoryScope.PROJECT,
        scoring_mode="hybrid",
        semantic_similarity=0.711676,
        total_score=42,
        score={
            "total": 42,
            "semantic_points": 28,
            "effective_min_score": 60,
            "unknown_private_field": "must-not-be-stored",
        },
        decision="rejected",
        rejection_reason="total_below_threshold",
    )

    assert record_retrieval_candidate_audits((candidate,), memory_db)
    assert record_retrieval_candidate_audits((candidate,), memory_db)

    result = query_retrieval_audits(path=memory_db)
    scores = result["audits"][0]["candidate_scores"]
    assert count_rows(memory_db, "repair_memory_retrieval_candidates") == 1
    assert scores == [{
        "attempt_id": "attempt-1",
        "memory_id": "mem-1",
        "problem_pattern": "",
        "memory_scope": "project",
        "scoring_mode": "hybrid",
        "semantic_similarity": pytest.approx(0.711676),
        "total_score": 42,
        "score": {
            "total": 42,
            "semantic_points": 28,
            "effective_min_score": 60,
        },
        "decision": "rejected",
        "rejection_reason": "total_below_threshold",
        "created_at": scores[0]["created_at"],
    }]


def test_error_code_is_bounded_and_exception_message_is_not_stored(memory_db):
    _initialize(memory_db)
    secret_message = "token=should-not-be-stored"

    assert record_retrieval_error(
        "task-1",
        error_code=("RuntimeError" * 20),
        attempt_id="attempt-error",
        increment_search=True,
        path=memory_db,
    )

    audit = load_retrieval_audit("task-1", memory_db)
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.ERROR
    assert len(audit.error_code) == 80
    assert secret_message not in repr(audit)


def test_error_without_prior_initialization_still_creates_audit(memory_db):
    assert record_retrieval_error("task-setup-error", error_code="RuntimeError", path=memory_db)

    audit = load_retrieval_audit("task-setup-error", memory_db)
    assert audit is not None
    assert audit.status is RetrievalAuditStatus.ERROR
    assert audit.search_count == 0


def test_invalid_status_is_rejected_by_value_object():
    with pytest.raises(ValueError):
        RetrievalAuditStatus("unknown")


def test_recent_audits_include_current_hits_and_legacy_unknown(memory_db):
    from pr_agent.triage.store import init_triage_table

    init_triage_table(memory_db)
    _initialize(memory_db, "task-recalled")
    assert record_retrieval_completion(
        "task-recalled", _query(), attempt_id="attempt-recalled", mode=RetrievalMode.INJECT,
        status=RetrievalAuditStatus.RECALLED, reason_code="selected",
        candidate_count=2, passed_threshold_count=1, selected_count=1, path=memory_db,
    )
    assert save_memory(sample_memory("mem-recalled"), memory_db)
    connection = sqlite3.connect(memory_db)
    try:
        with connection:
            connection.execute(
                "INSERT INTO repair_memory_hits "
                "(attempt_id, task_id, root_cause_group_id, current_project, source_pipeline_id, "
                "source_sha, memory_id, memory_scope, rank, score_json, mode, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "attempt-recalled", "task-recalled", "root-1", "group/a", 100,
                    "a" * 40, "mem-recalled", "project", 1, "{}", "inject",
                    "2026-08-19T20:00:00+08:00",
                ),
            )
            for task_id, mr_iid, created_at in (
                ("task-recalled", "7", "2026-08-19T20:10:00+08:00"),
                ("task-legacy", "8", "2026-08-19T20:20:00+08:00"),
            ):
                connection.execute(
                    "INSERT INTO triage_runs "
                    "(created_at, task_id, project, mr_iid, pipeline_id, success, final_pipeline_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (created_at, task_id, "group/a", mr_iid, "100", 1, "success"),
                )
    finally:
        connection.close()

    rows = list_recent_retrieval_audits(limit=20, path=memory_db)
    by_task = {row["task_id"]: row for row in rows}

    assert by_task["task-legacy"]["status"] == "legacy_unknown"
    assert by_task["task-recalled"]["status"] == "recalled"
    assert by_task["task-recalled"]["final_repair_outcome"] == "success"
    assert by_task["task-recalled"]["recalled_memories"] == [{
        "memory_id": "mem-recalled",
        "scope": "project",
        "problem_pattern": sample_memory("mem-recalled").problem_pattern,
    }]
    assert "source_sha" not in by_task["task-recalled"]


def test_recent_audits_project_filter_and_limit_are_bounded(memory_db):
    _initialize(memory_db, "task-a")
    assert initialize_retrieval_audit(
        task_id="task-b", project="group/b", mr_iid=8, source_pipeline_id=101,
        source_sha="b" * 40, mode=RetrievalMode.INJECT,
        reason_code="repair_session_not_reached", path=memory_db,
    )

    rows = list_recent_retrieval_audits(limit=999, project="group/b", path=memory_db)

    assert [row["task_id"] for row in rows] == ["task-b"]


def test_query_retrieval_audits_paginates_audited_and_legacy_tasks(memory_db):
    from pr_agent.triage.store import init_triage_table

    init_triage_table(memory_db)
    for index in range(9):
        task_id = f"task-audit-{index:02d}"
        project = "group/a" if index % 2 == 0 else "group/b"
        assert initialize_retrieval_audit(
            task_id=task_id,
            project=project,
            mr_iid=100 + index,
            source_pipeline_id=200 + index,
            source_sha="a" * 40,
            mode=RetrievalMode.INJECT,
            reason_code="repair_session_not_reached",
            path=memory_db,
        )

    connection = sqlite3.connect(memory_db)
    try:
        for index in range(9):
            task_id = f"task-audit-{index:02d}"
            timestamp = f"2026-08-20T10:{index * 2:02d}:00+08:00"
            connection.execute(
                "UPDATE repair_memory_retrieval_audits SET created_at = ?, updated_at = ? WHERE task_id = ?",
                (timestamp, timestamp, task_id),
            )
        for index in range(8):
            task_id = f"task-legacy-{index:02d}"
            project = "group/a" if index % 2 == 0 else "group/b"
            timestamp = f"2026-08-20T10:{index * 2 + 1:02d}:00+08:00"
            connection.execute(
                "INSERT INTO triage_runs "
                "(created_at, task_id, project, mr_iid, pipeline_id, success, final_pipeline_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (timestamp, task_id, project, str(300 + index), str(400 + index), 0, "failed"),
            )
        connection.commit()
    finally:
        connection.close()

    first = query_retrieval_audits(page=1, page_size=15, path=memory_db)
    second = query_retrieval_audits(page=2, page_size=15, path=memory_db)
    overflow = query_retrieval_audits(page=3, page_size=15, path=memory_db)
    filtered = query_retrieval_audits(page=1, page_size=15, project="group/b", path=memory_db)

    assert first["page"] == 1
    assert first["page_size"] == 15
    assert first["total"] == 17
    assert first["total_pages"] == 2
    assert len(first["audits"]) == 15
    assert len(second["audits"]) == 2
    assert {row["task_id"] for row in first["audits"]}.isdisjoint(
        row["task_id"] for row in second["audits"]
    )
    assert [row["updated_at"] for row in first["audits"]] == sorted(
        (row["updated_at"] for row in first["audits"]), reverse=True
    )
    assert overflow["page"] == 3
    assert overflow["audits"] == []
    assert filtered["total"] == 8
