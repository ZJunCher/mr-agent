import asyncio
import ast
import inspect
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import pr_agent.suggestions.prompt_evolution.scheduler as scheduler_module
from pr_agent.suggestions.prompt_evolution.models import EvolutionRunStatus
from pr_agent.suggestions.prompt_evolution.scheduler import (
    PromptEvolutionScheduler,
    heartbeat_is_healthy,
    scheduled_at_for_week,
)

CN = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("now", "due"),
    [
        ("2026-08-17T02:59:59+08:00", False),
        ("2026-08-17T03:00:00+08:00", True),
        ("2026-08-18T12:00:00+08:00", True),
    ],
)
def test_weekly_due_uses_beijing_time_and_catches_up(now, due):
    current = datetime.fromisoformat(now)
    scheduled = scheduled_at_for_week(current, "monday", 3, 0, CN)
    assert (current >= scheduled) is due


class StaticRunner:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    async def run(self, dry_run):
        self.calls += 1
        assert dry_run is False
        return SimpleNamespace(status=self.status)


def _scheduler(runner, *, retry_seconds=900):
    return PromptEvolutionScheduler(
        runner_factory=lambda now: runner,
        timezone=CN,
        weekday="monday",
        hour=3,
        minute=0,
        poll_seconds=60,
        retry_interval_seconds=retry_seconds,
        heartbeat_seconds=30,
        heartbeat_path=Path("/tmp/test-prompt-evolution-heartbeat"),
    )


def test_terminal_run_is_called_only_once_per_iso_week():
    runner = StaticRunner(EvolutionRunStatus.COMPLETED_NO_CHANGE)
    scheduler = _scheduler(runner)
    now = datetime.fromisoformat("2026-08-18T12:00:00+08:00")

    asyncio.run(scheduler.maybe_run(now))
    asyncio.run(scheduler.maybe_run(now + timedelta(hours=1)))

    assert runner.calls == 1


def test_retryable_run_waits_for_retry_interval():
    runner = StaticRunner(EvolutionRunStatus.FAILED_RETRYABLE)
    scheduler = _scheduler(runner, retry_seconds=900)
    now = datetime.fromisoformat("2026-08-18T12:00:00+08:00")

    asyncio.run(scheduler.maybe_run(now))
    asyncio.run(scheduler.maybe_run(now + timedelta(seconds=899)))
    asyncio.run(scheduler.maybe_run(now + timedelta(seconds=900)))

    assert runner.calls == 2


def test_restart_safely_calls_same_weekly_batch_once_again():
    runner = StaticRunner(EvolutionRunStatus.MR_OPEN)
    now = datetime.fromisoformat("2026-08-18T12:00:00+08:00")

    asyncio.run(_scheduler(runner).maybe_run(now))
    asyncio.run(_scheduler(runner).maybe_run(now))

    assert runner.calls == 2


def test_heartbeat_healthcheck_rejects_missing_and_stale_files(tmp_path):
    path = tmp_path / "heartbeat"
    now = datetime(2026, 8, 18, 12, tzinfo=CN).timestamp()
    assert not heartbeat_is_healthy(path, stale_seconds=120, now_timestamp=now)

    path.write_text("ok", encoding="utf-8")
    os.utime(path, (now - 121, now - 121))
    assert not heartbeat_is_healthy(path, stale_seconds=120, now_timestamp=now)

    os.utime(path, (now - 30, now - 30))
    assert heartbeat_is_healthy(path, stale_seconds=120, now_timestamp=now)


def test_scheduler_keeps_production_factory_import_lazy():
    tree = ast.parse(inspect.getsource(scheduler_module))
    top_level_imports = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }

    assert "pr_agent.suggestions.prompt_evolution.factory" not in top_level_imports
