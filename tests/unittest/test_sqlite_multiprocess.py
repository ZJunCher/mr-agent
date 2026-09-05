import multiprocessing
import sqlite3
from unittest.mock import Mock

from pr_agent.storage.sqlite import connect_sqlite, run_write_transaction


def _write_triage_rows(path: str, worker: int, count: int) -> None:
    from pr_agent.triage.store import save_triage_run

    for index in range(count):
        task_id = f"worker-{worker}-task-{index}"
        assert save_triage_run(
            {
                "task_id": task_id,
                "pr_url": f"https://gitlab.example/g/r/-/merge_requests/{index}",
                "project": "g/r",
                "mr_iid": str(index),
                "success": 1,
                "final_pipeline_status": "success",
            },
            path=path,
        )


def test_connect_uses_wal_and_busy_timeout(tmp_path):
    conn = connect_sqlite(str(tmp_path / "db.sqlite"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()


def test_locked_write_is_retried(monkeypatch, tmp_path):
    path = str(tmp_path / "retry.sqlite")
    attempts = 0
    sleep = Mock()
    monkeypatch.setattr("pr_agent.storage.sqlite.time.sleep", sleep)

    def flaky_connect(value):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return connect_sqlite(value)

    result = run_write_transaction(
        path,
        lambda conn: conn.execute("CREATE TABLE ready (id INTEGER)") or True,
        connect=flaky_connect,
    )

    assert result is not None
    assert attempts == 2
    sleep.assert_called_once_with(0.05)


def test_same_triage_task_id_is_upserted_once(tmp_path):
    from pr_agent.triage.store import save_triage_run

    path = str(tmp_path / "triage.sqlite")
    record = {
        "task_id": "task-536",
        "pr_url": "https://gitlab.example/eabot/cook/-/merge_requests/536",
        "project": "eabot/cook",
        "mr_iid": "536",
        "success": 1,
        "final_pipeline_status": "success",
        "final_coverage": 60.0,
    }
    assert save_triage_run(record, path=path) is True
    assert save_triage_run({**record, "final_coverage": 63.04}, path=path) is True

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT task_id, final_coverage FROM triage_runs").fetchall()
    finally:
        conn.close()
    assert rows == [("task-536", 63.04)]


def test_same_suggestion_effect_is_stored_once(tmp_path):
    from pr_agent.suggestions.store import save_suggestion_thread

    path = str(tmp_path / "suggestions.sqlite")
    record = {
        "task_id": "task-improve-536",
        "suggestion_id": "S1",
        "review_id": "review-1",
        "project": "eabot/cook",
        "mr_iid": "536",
        "file_path": "src/main.cpp",
        "line_start": 10,
        "publish_status": "skipped",
        "skip_reason": "test",
    }
    assert save_suggestion_thread(record, path=path) is True
    assert save_suggestion_thread({**record, "skip_reason": "updated"}, path=path) is True

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT task_id, skip_reason FROM suggestion_threads").fetchall()
    finally:
        conn.close()
    assert rows == [("task-improve-536", "updated")]


def test_three_processes_can_write_same_database(tmp_path):
    path = str(tmp_path / "multiprocess.sqlite")
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_write_triage_rows, args=(path, worker, 8)) for worker in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    conn = sqlite3.connect(path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM triage_runs").fetchone()[0]
    finally:
        conn.close()
    assert count == 24
