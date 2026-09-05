"""Append auditable historical creation-review failures from approved Redis evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pr_agent.suggestions.review_tracking import (
    finish_review_run,
    get_review_run_for_task,
    record_review_event,
    start_review_run,
)


def _error_code(message: str) -> str:
    return "worker_lost" if "worker lost" in message.lower() else "historical_task_failed"


def calibrate(records: list[dict], *, path: str, dry_run: bool = True) -> dict:
    """Append failed historical runs without updating or deleting existing evidence."""
    result = {"inserted": 0, "skipped": 0, "invalid": 0, "would_insert": 0}
    for record in records:
        task_id = str(record.get("task_id") or "")
        project_path = str(record.get("project_path") or "")
        mr_iid = str(record.get("mr_iid") or "")
        if not task_id or not project_path or not mr_iid:
            result["invalid"] += 1
            continue
        if get_review_run_for_task(task_id, path=path):
            result["skipped"] += 1
            continue
        if dry_run:
            result["would_insert"] += 1
            continue
        message = str(record.get("error") or "Historical automatic task failed")[:1000]
        code = _error_code(message)
        run_id = start_review_run({
            "project_path": project_path,
            "mr_iid": mr_iid,
            "commit_sha": str(record.get("commit_sha") or ""),
            "task_id": task_id,
            "trigger": "historical_auto_mr_create",
            "review_scope": "mr_creation",
            "stage": "startup_failed",
        }, path=path)
        if not run_id:
            result["invalid"] += 1
            continue
        finish_review_run(
            "failed", run_id, path=path, stage="startup_failed",
            error_code=code, error_message=message,
        )
        record_review_event(
            run_id, "historical_task_failed", "startup_failed", status="failed",
            error_code=code, error_message=message,
            details={"source": "approved_redis_evidence", "task_id": task_id}, path=path,
        )
        result["inserted"] += 1
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Approved JSON array of bounded Redis task evidence")
    parser.add_argument("--db", required=True, help="Suggestion review SQLite database")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    input_path = Path(args.input)
    records = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("input must be a JSON array")
    result = calibrate(records, path=args.db, dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
