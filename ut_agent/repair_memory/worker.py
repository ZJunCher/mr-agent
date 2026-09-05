"""Dedicated automatic consolidator for verified CI repair experience.

The worker is deliberately separate from CI Agent workers. It performs bounded
best-effort batches against the shared SQLite database and never participates in
the success or failure decision of a live repair task.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from pr_agent.feedback.store import get_db_path
from pr_agent.log import get_logger
from ut_agent.llm import call_tool_llm_outcome
from ut_agent.repair_memory.config import load_repair_memory_settings
from ut_agent.repair_memory.consolidate import (
    migrate_legacy_memories,
    promote_ready_patterns,
    run_consolidation_batch,
)
from ut_agent.repair_memory.embedding import HttpEmbeddingClient
from ut_agent.repair_memory.embedding_store import run_embedding_batch
from ut_agent.repair_memory.store import init_repair_memory_tables, prune_expired_memory_data


@dataclass(frozen=True)
class WorkerCycleSummary:
    """Bounded counters emitted for one automatic worker cycle."""

    claimed: int = 0
    completed: int = 0
    failed: int = 0
    invalid: int = 0
    promoted: int = 0
    skipped: int = 0
    legacy_selected: int = 0
    legacy_migrated: int = 0
    legacy_marked_for_review: int = 0
    legacy_failed: int = 0
    embeddings_selected: int = 0
    embeddings_indexed: int = 0
    embeddings_failed: int = 0
    deleted_episodes: int = 0
    deleted_hits: int = 0


def run_cycle(owner: str, path: str | None = None) -> WorkerCycleSummary:
    """Run consolidation, optional promotion, and retention once.

    Each phase is fail-open and logs only bounded counters or exception classes.
    Episode content and model prompts are never written to the worker log.
    """
    settings = load_repair_memory_settings()
    db_path = path or get_db_path()
    claimed = completed = failed = invalid = 0
    promoted = skipped = 0
    legacy_selected = legacy_migrated = legacy_marked_for_review = legacy_failed = 0
    embeddings_selected = embeddings_indexed = embeddings_failed = 0
    deleted_episodes = deleted_hits = 0

    try:
        batch = run_consolidation_batch(
            settings.consolidation_batch_size,
            owner,
            db_path,
            llm_call=call_tool_llm_outcome,
            lease_seconds=settings.consolidation_lease_seconds,
        )
        claimed = batch.claimed
        completed = batch.completed
        failed = batch.failed
        invalid = batch.invalid
    except Exception as error:
        failed = 1
        get_logger().error(
            f"Repair memory consolidation cycle failed: error_type={type(error).__name__}"
        )

    try:
        legacy = migrate_legacy_memories(
            limit=min(5, settings.consolidation_batch_size),
            owner=owner,
            llm_call=call_tool_llm_outcome,
            path=db_path,
        )
        legacy_selected = legacy.selected
        legacy_migrated = legacy.migrated
        legacy_marked_for_review = legacy.marked_for_review
        legacy_failed = legacy.failed
    except Exception as error:
        legacy_failed = 1
        get_logger().error(
            f"Repair memory legacy migration failed: error_type={type(error).__name__}"
        )

    if settings.promotion_enabled:
        try:
            promotion = asyncio.run(
                promote_ready_patterns(db_path, llm_call=call_tool_llm_outcome)
            )
            promoted = promotion.promoted
            skipped = promotion.skipped
        except Exception as error:
            get_logger().error(
                f"Repair memory promotion cycle failed: error_type={type(error).__name__}"
            )

    try:
        embedding = run_embedding_batch(
            client=HttpEmbeddingClient(settings.embedding_service_url),
            settings=settings,
            now=datetime.now(timezone.utc),
            path=db_path,
        )
        embeddings_selected = embedding.selected
        embeddings_indexed = embedding.indexed
        embeddings_failed = embedding.failed
    except Exception as error:
        embeddings_failed = 1
        get_logger().error(
            f"Repair memory embedding cycle failed: error_type={type(error).__name__}"
        )

    try:
        pruned = prune_expired_memory_data(
            datetime.now(timezone.utc).isoformat(),
            episode_retention_days=settings.episode_retention_days,
            hit_retention_days=settings.hit_retention_days,
            path=db_path,
        )
        deleted_episodes = pruned.deleted_episodes
        deleted_hits = pruned.deleted_hits
    except Exception as error:
        get_logger().error(
            f"Repair memory retention cycle failed: error_type={type(error).__name__}"
        )

    summary = WorkerCycleSummary(
        claimed=claimed,
        completed=completed,
        failed=failed,
        invalid=invalid,
        promoted=promoted,
        skipped=skipped,
        legacy_selected=legacy_selected,
        legacy_migrated=legacy_migrated,
        legacy_marked_for_review=legacy_marked_for_review,
        legacy_failed=legacy_failed,
        embeddings_selected=embeddings_selected,
        embeddings_indexed=embeddings_indexed,
        embeddings_failed=embeddings_failed,
        deleted_episodes=deleted_episodes,
        deleted_hits=deleted_hits,
    )
    get_logger().info(
        "Repair memory cycle: "
        f"claimed={summary.claimed} completed={summary.completed} "
        f"failed={summary.failed} invalid={summary.invalid} "
        f"promoted={summary.promoted} skipped={summary.skipped} "
        f"legacy_selected={summary.legacy_selected} legacy_migrated={summary.legacy_migrated} "
        f"legacy_review={summary.legacy_marked_for_review} legacy_failed={summary.legacy_failed} "
        f"embeddings_selected={summary.embeddings_selected} "
        f"embeddings_indexed={summary.embeddings_indexed} "
        f"embeddings_failed={summary.embeddings_failed} "
        f"deleted_episodes={summary.deleted_episodes} deleted_hits={summary.deleted_hits}"
    )
    return summary


def _owner() -> str:
    return f"repair-memory:{socket.gethostname()}:{os.getpid()}"


def run_forever(
    *,
    stop_event: threading.Event | None = None,
    install_signal_handlers: bool = True,
) -> None:
    """Run one cycle immediately and continue at the configured interval."""
    stop = stop_event or threading.Event()

    if install_signal_handlers:
        def request_stop(_signum, _frame) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    settings = load_repair_memory_settings()
    db_path = get_db_path()
    init_repair_memory_tables(db_path)
    owner = _owner()
    get_logger().info(
        f"Repair memory worker started: poll_seconds={settings.consolidation_poll_seconds}"
    )

    while not stop.is_set():
        try:
            run_cycle(owner, db_path)
        except Exception as error:
            get_logger().error(
                f"Repair memory worker cycle aborted: error_type={type(error).__name__}"
            )
        stop.wait(settings.consolidation_poll_seconds)

    get_logger().info("Repair memory worker stopped")


if __name__ == "__main__":
    run_forever()
