"""Operator CLI for repair memory.

Supports:
- ``list``: list memories by project, scope, pattern, status, and confidence;
- ``show``: show content, supporting evidence, hit outcomes, and audit events;
- ``disable`` / ``enable``: disable or re-enable a memory with a required reason;
- ``needs-review``: mark a memory for review;
- ``supersede``: supersede a memory with a corrected version;
- ``consolidate``: run a bounded consolidation batch;
- ``promote --dry-run``: dry-run global promotion report.

Every mutation requires ``--reason`` and appends an audit event. ``supersede``
creates a new memory version and marks the old row ``superseded``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any, Sequence

from ut_agent.llm import call_tool_llm_outcome
from ut_agent.repair_memory.config import load_repair_memory_settings
from ut_agent.repair_memory.consolidate import (
    migrate_legacy_memories,
    parse_memory_candidate,
    promote_ready_patterns,
    run_consolidation_batch,
)
from ut_agent.repair_memory.embedding import HttpEmbeddingClient
from ut_agent.repair_memory.embedding_store import embedding_status_summary, run_embedding_batch
from ut_agent.repair_memory.models import MemoryStatus
from ut_agent.repair_memory.outcomes import memory_effectiveness_summary
from ut_agent.repair_memory.store import (
    list_memories,
    list_memory_events,
    load_memory,
    revalidate_global_support,
    save_memory,
    update_memory_status,
)


def _print_json(value: Any) -> None:
    """Print bounded structured JSON, omitting project-private episode text."""
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _cmd_list(args: argparse.Namespace, path: str) -> int:
    memories = list_memories(
        scope=args.scope,
        scope_key=args.scope_key,
        pattern_key=args.pattern_key,
        status=args.status,
        path=path,
    )
    _print_json(
        [
            {
                "memory_id": m.memory_id,
                "scope": m.scope.value,
                "scope_key": m.scope_key,
                "pattern_key": m.pattern_key,
                "pattern_version": m.pattern_version,
                "language": m.language,
                "failure_family": m.failure_family,
                "confidence": m.confidence,
                "support_episode_count": m.support_episode_count,
                "support_project_count": m.support_project_count,
                "settled_attempts": m.settled_attempts,
                "immediate_successes": m.immediate_successes,
                "status": m.status.value,
            }
            for m in memories
        ]
    )
    return 0


def _cmd_show(args: argparse.Namespace, path: str) -> int:
    memory = load_memory(args.memory_id, path=path)
    if memory is None:
        print(f"memory not found: {args.memory_id}", file=sys.stderr)
        return 1
    events = list_memory_events(args.memory_id, path=path)
    _print_json(
        {
            "memory": {
                "memory_id": memory.memory_id,
                "scope": memory.scope.value,
                "scope_key": memory.scope_key,
                "pattern_key": memory.pattern_key,
                "pattern_version": memory.pattern_version,
                "language": memory.language,
                "build_system": memory.build_system,
                "failure_family": memory.failure_family,
                "root_cause_class": memory.root_cause_class,
                "repair_action_class": memory.repair_action_class,
                "problem_pattern": memory.problem_pattern,
                "applicability": list(memory.applicability),
                "anti_conditions": list(memory.anti_conditions),
                "repair_guidance": memory.repair_guidance,
                "validation_guidance": list(memory.validation_guidance),
                "confidence": memory.confidence,
                "support_episode_count": memory.support_episode_count,
                "support_project_count": memory.support_project_count,
                "settled_attempts": memory.settled_attempts,
                "immediate_successes": memory.immediate_successes,
                "status": memory.status.value,
                "supersedes_id": memory.supersedes_id,
            },
            "events": [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "reason": e.reason,
                    "created_at": e.created_at,
                }
                for e in events
            ],
        }
    )
    return 0


def _cmd_disable(args: argparse.Namespace, path: str) -> int:
    memory = load_memory(args.memory_id, path=path)
    if memory is None:
        print(f"memory not found: {args.memory_id}", file=sys.stderr)
        return 1
    if not update_memory_status(args.memory_id, MemoryStatus.DISABLED, args.reason, path=path):
        print("disable failed", file=sys.stderr)
        return 1
    revalidate_global_support(memory.pattern_key, path=path)
    return 0


def _cmd_enable(args: argparse.Namespace, path: str) -> int:
    memory = load_memory(args.memory_id, path=path)
    if memory is None:
        print(f"memory not found: {args.memory_id}", file=sys.stderr)
        return 1
    if memory.status is MemoryStatus.SUPERSEDED:
        print("superseded memories cannot be re-enabled", file=sys.stderr)
        return 1
    if not update_memory_status(args.memory_id, MemoryStatus.ACTIVE, args.reason, path=path):
        print("enable failed", file=sys.stderr)
        return 1
    return 0


def _cmd_needs_review(args: argparse.Namespace, path: str) -> int:
    memory = load_memory(args.memory_id, path=path)
    if memory is None:
        print(f"memory not found: {args.memory_id}", file=sys.stderr)
        return 1
    if not update_memory_status(args.memory_id, MemoryStatus.NEEDS_REVIEW, args.reason, path=path):
        print("needs-review failed", file=sys.stderr)
        return 1
    return 0


def _cmd_supersede(args: argparse.Namespace, path: str) -> int:
    memory = load_memory(args.memory_id, path=path)
    if memory is None:
        print(f"memory not found: {args.memory_id}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(args.from_json.read_text(encoding="utf-8"))
        candidate = parse_memory_candidate(json.dumps(payload, ensure_ascii=False))
    except Exception as error:
        print(f"invalid correction file: {type(error).__name__}", file=sys.stderr)
        return 1

    from pr_agent.feedback.timez import now_cn_iso

    now = now_cn_iso()
    new_pattern_key = memory.pattern_key
    new_memory = type(memory)(
        memory_id=f"{memory.memory_id}:v{memory.pattern_version + 1}",
        scope=memory.scope,
        scope_key=memory.scope_key,
        pattern_key=new_pattern_key,
        pattern_version=memory.pattern_version + 1,
        language=candidate.language,
        build_system=candidate.build_system,
        failure_family=candidate.failure_family,
        root_cause_class=candidate.root_cause_class,
        repair_action_class=candidate.repair_action_class,
        diagnostic_fingerprint=memory.diagnostic_fingerprint,
        causal_tokens=memory.causal_tokens,
        problem_pattern=candidate.problem_pattern,
        applicability=candidate.applicability,
        anti_conditions=candidate.anti_conditions,
        repair_guidance=candidate.repair_guidance,
        validation_guidance=candidate.validation_guidance,
        confidence=memory.confidence,
        support_episode_count=memory.support_episode_count,
        support_project_count=memory.support_project_count,
        settled_attempts=memory.settled_attempts,
        immediate_successes=memory.immediate_successes,
        status=MemoryStatus.ACTIVE,
        content_locale="zh-CN",
        supersedes_id=memory.memory_id,
        manual_reason=args.reason,
        created_at=now,
        updated_at=now,
        last_reinforced_at=now,
    )
    if not save_memory(new_memory, path=path):
        print("supersede failed to save new version", file=sys.stderr)
        return 1
    if not update_memory_status(args.memory_id, MemoryStatus.SUPERSEDED, args.reason, path=path):
        print("supersede failed to mark old version", file=sys.stderr)
        return 1
    revalidate_global_support(new_pattern_key, path=path)
    return 0


def _cmd_consolidate(args: argparse.Namespace, path: str) -> int:
    summary = run_consolidation_batch(
        args.limit,
        "cli-worker",
        path,
        llm_call=call_tool_llm_outcome,
    )
    print(
        f"consolidated: claimed={summary.claimed} completed={summary.completed} "
        f"failed={summary.failed} invalid={summary.invalid}"
    )
    if summary.failed or summary.invalid:
        return 1
    return 0


def _cmd_promote(args: argparse.Namespace, path: str) -> int:
    import asyncio

    summary = asyncio.run(
        promote_ready_patterns(
            path,
            dry_run=args.dry_run,
            llm_call=call_tool_llm_outcome,
        )
    )
    print(f"promoted: {summary.promoted} skipped: {summary.skipped}")
    return 0


def _cmd_migrate_legacy(args: argparse.Namespace, path: str) -> int:
    summary = migrate_legacy_memories(
        limit=args.limit,
        owner="cli-migration",
        llm_call=call_tool_llm_outcome,
        path=path,
    )
    print(
        f"legacy: selected={summary.selected} migrated={summary.migrated} "
        f"marked_for_review={summary.marked_for_review} failed={summary.failed}"
    )
    return 1 if summary.failed else 0


def _cmd_effectiveness(args: argparse.Namespace, path: str) -> int:
    summary = memory_effectiveness_summary(days=args.days, project=args.project, path=path)
    _print_json(summary)
    return 0


def _cmd_embeddings(args: argparse.Namespace, path: str) -> int:
    settings = load_repair_memory_settings()
    if args.embedding_command == "status":
        _print_json(
            asdict(
                embedding_status_summary(
                    settings=settings,
                    now=datetime.now(timezone.utc),
                    path=path,
                )
            )
        )
        return 0

    client = HttpEmbeddingClient(settings.embedding_service_url)
    remaining = max(1, args.limit)
    selected = indexed = skipped_unchanged = failed = 0
    while remaining > 0:
        batch_settings = replace(
            settings,
            embedding_batch_size=min(settings.embedding_batch_size, remaining),
        )
        summary = run_embedding_batch(
            client=client,
            settings=batch_settings,
            now=datetime.now(timezone.utc),
            path=path,
        )
        selected += summary.selected
        indexed += summary.indexed
        skipped_unchanged += summary.skipped_unchanged
        failed += summary.failed
        if summary.selected == 0:
            break
        remaining -= summary.selected
    print(
        f"embeddings: selected={selected} indexed={indexed} "
        f"skipped_unchanged={skipped_unchanged} failed={failed}"
    )
    return 1 if failed else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ut_agent.repair_memory.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list memories")
    p_list.add_argument("--scope", default="")
    p_list.add_argument("--scope-key", default="")
    p_list.add_argument("--pattern-key", default="")
    p_list.add_argument("--status", default="")

    p_show = sub.add_parser("show", help="show one memory")
    p_show.add_argument("memory_id")

    p_disable = sub.add_parser("disable", help="disable a memory")
    p_disable.add_argument("memory_id")
    p_disable.add_argument("--reason", required=True)

    p_enable = sub.add_parser("enable", help="re-enable a memory")
    p_enable.add_argument("memory_id")
    p_enable.add_argument("--reason", required=True)

    p_review = sub.add_parser("needs-review", help="mark a memory for review")
    p_review.add_argument("memory_id")
    p_review.add_argument("--reason", required=True)

    p_supersede = sub.add_parser("supersede", help="supersede a memory with a corrected version")
    p_supersede.add_argument("memory_id")
    p_supersede.add_argument("--from-json", required=True, type=__import__("pathlib").Path)
    p_supersede.add_argument("--reason", required=True)

    p_consolidate = sub.add_parser("consolidate", help="run a consolidation batch")
    p_consolidate.add_argument("--limit", type=int, default=50)

    p_promote = sub.add_parser("promote", help="promote global patterns")
    p_promote.add_argument("--dry-run", action="store_true")

    p_migrate = sub.add_parser("migrate-legacy", help="regenerate legacy memories in Chinese")
    p_migrate.add_argument("--limit", type=int, default=100)

    p_effect = sub.add_parser("effectiveness", help="show memory effectiveness summary")
    p_effect.add_argument("--days", type=int, default=None)
    p_effect.add_argument("--project", default=None)

    p_embeddings = sub.add_parser("embeddings", help="manage repair-memory embeddings")
    embedding_sub = p_embeddings.add_subparsers(dest="embedding_command", required=True)
    p_backfill = embedding_sub.add_parser("backfill", help="index active memories")
    p_backfill.add_argument("--limit", type=int, default=500)
    embedding_sub.add_parser("status", help="show active-memory embedding status")

    return parser


def cli_main(argv: Sequence[str], *, path: str | None = None) -> int:
    """Run one CLI command. Returns 0 on success, 1 on store/validation failure."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_code:
        return int(exit_code.code) if exit_code.code is not None else 2

    db_path = path or __import__("pr_agent.feedback.store", fromlist=["get_db_path"]).get_db_path()

    handlers = {
        "list": _cmd_list,
        "show": _cmd_show,
        "disable": _cmd_disable,
        "enable": _cmd_enable,
        "needs-review": _cmd_needs_review,
        "supersede": _cmd_supersede,
        "consolidate": _cmd_consolidate,
        "promote": _cmd_promote,
        "migrate-legacy": _cmd_migrate_legacy,
        "effectiveness": _cmd_effectiveness,
        "embeddings": _cmd_embeddings,
    }
    handler = handlers.get(args.command)
    if handler is None:
        print(f"unknown command: {args.command}", file=sys.stderr)
        return 2
    return handler(args, db_path)


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
