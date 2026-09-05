"""Independent weekly scheduler for Prompt evolution Draft MR generation."""
from __future__ import annotations

import argparse
import asyncio
import inspect
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.suggestions.prompt_evolution.models import EvolutionRunStatus

DEFAULT_HEARTBEAT_PATH = Path("/tmp/pr-agent-prompt-evolution-heartbeat")
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_TERMINAL_STATUSES = {
    EvolutionRunStatus.MR_OPEN,
    EvolutionRunStatus.COMPLETED_NO_CHANGE,
    EvolutionRunStatus.DRY_RUN_VALIDATED,
    EvolutionRunStatus.FAILED_TERMINAL,
    EvolutionRunStatus.SUPERSEDED,
    EvolutionRunStatus.MERGED,
    EvolutionRunStatus.CLOSED,
}


def scheduled_at_for_week(now: datetime, weekday: str, hour: int, minute: int,
                          timezone: ZoneInfo) -> datetime:
    """Return this ISO week's configured due time in the requested timezone."""
    weekday_index = _WEEKDAYS.get(str(weekday).strip().lower())
    if weekday_index is None:
        raise ValueError(f"unsupported weekday: {weekday}")
    local_now = now.astimezone(timezone)
    monday = (local_now - timedelta(days=local_now.weekday())).date()
    due_date = monday + timedelta(days=weekday_index)
    return datetime(
        due_date.year,
        due_date.month,
        due_date.day,
        int(hour),
        int(minute),
        tzinfo=timezone,
    )


def heartbeat_is_healthy(path: Path, *, stale_seconds: int,
                         now_timestamp: float | None = None) -> bool:
    try:
        modified = path.stat().st_mtime
    except OSError:
        return False
    now_timestamp = time.time() if now_timestamp is None else float(now_timestamp)
    return 0 <= now_timestamp - modified <= int(stale_seconds)


class PromptEvolutionScheduler:
    def __init__(self, *, runner_factory, timezone: ZoneInfo, weekday: str,
                 hour: int, minute: int, poll_seconds: int,
                 retry_interval_seconds: int, heartbeat_seconds: int,
                 heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH,
                 clock=None, sleep=asyncio.sleep):
        self.runner_factory = runner_factory
        self.timezone = timezone
        self.weekday = weekday
        self.hour = int(hour)
        self.minute = int(minute)
        self.poll_seconds = int(poll_seconds)
        self.retry_interval_seconds = int(retry_interval_seconds)
        self.heartbeat_seconds = int(heartbeat_seconds)
        self.heartbeat_path = heartbeat_path
        self.clock = clock or (lambda: datetime.now(self.timezone))
        self.sleep = sleep
        self._completed_week: tuple[int, int] | None = None
        self._attempt_week: tuple[int, int] | None = None
        self._next_attempt_at: datetime | None = None

    async def run_forever(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._heartbeat_loop())
            group.create_task(self._schedule_loop())

    async def maybe_run(self, now: datetime):
        local_now = now.astimezone(self.timezone)
        due = scheduled_at_for_week(local_now, self.weekday, self.hour, self.minute, self.timezone)
        week = (local_now.isocalendar().year, local_now.isocalendar().week)
        if local_now < due or self._completed_week == week:
            return None
        if self._attempt_week != week:
            self._attempt_week = week
            self._next_attempt_at = None
        if self._next_attempt_at is not None and local_now < self._next_attempt_at:
            return None

        try:
            runner = self.runner_factory(local_now)
            if inspect.isawaitable(runner):
                runner = await runner
            result = await runner.run(dry_run=False)
        except Exception as exc:
            get_logger().error(f"prompt evolution scheduled run failed: {type(exc).__name__}")
            self._next_attempt_at = local_now + timedelta(seconds=self.retry_interval_seconds)
            return None

        if result.status in _TERMINAL_STATUSES:
            self._completed_week = week
            self._next_attempt_at = None
        else:
            self._next_attempt_at = local_now + timedelta(seconds=self.retry_interval_seconds)
        get_logger().info(f"prompt evolution scheduled run finished: status={result.status.value}")
        return result

    async def _heartbeat_loop(self) -> None:
        while True:
            self._touch_heartbeat()
            await self.sleep(self.heartbeat_seconds)

    async def _schedule_loop(self) -> None:
        while True:
            await self.maybe_run(self.clock().astimezone(self.timezone))
            await self.sleep(self.poll_seconds)

    def _touch_heartbeat(self) -> None:
        self.heartbeat_path.write_text(self.clock().astimezone(self.timezone).isoformat(), encoding="utf-8")


def _scheduler_from_settings() -> PromptEvolutionScheduler:
    from pr_agent.suggestions.prompt_evolution.factory import build_runner_from_settings

    cfg = get_settings().prompt_evolution
    timezone = ZoneInfo(str(cfg.timezone))
    return PromptEvolutionScheduler(
        runner_factory=lambda now: build_runner_from_settings(now=now),
        timezone=timezone,
        weekday=str(cfg.weekday),
        hour=int(cfg.hour),
        minute=int(cfg.minute),
        poll_seconds=int(cfg.poll_seconds),
        retry_interval_seconds=int(cfg.retry_interval_seconds),
        heartbeat_seconds=int(cfg.heartbeat_seconds),
        heartbeat_path=DEFAULT_HEARTBEAT_PATH,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the weekly Prompt evolution scheduler")
    parser.add_argument("--healthcheck", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.healthcheck:
        stale_seconds = int(get_settings().prompt_evolution.heartbeat_stale_seconds)
        return 0 if heartbeat_is_healthy(DEFAULT_HEARTBEAT_PATH, stale_seconds=stale_seconds) else 1
    asyncio.run(_scheduler_from_settings().run_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
