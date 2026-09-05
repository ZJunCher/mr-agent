"""Cron-friendly CLI entry point for the weekly /improve Prompt evolution Draft MR.

Invoke once per week from the deployment environment (CronJob / systemd timer
/ scheduled task). Do not add a GitHub/GitLab CI schedule here.

Usage:
    python -m scripts.prompt_evolution_weekly --dry-run
    python -m scripts.prompt_evolution_weekly --publish
"""
from __future__ import annotations

import argparse
import asyncio
import json

from pr_agent.log import get_logger


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Build the weekly /improve Prompt evolution Draft MR")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--publish", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        from pr_agent.suggestions.prompt_evolution.factory import build_runner_from_settings
        result = asyncio.run(build_runner_from_settings().run(dry_run=args.dry_run))
        print(json.dumps({
            "batch_id": result.batch_id,
            "status": result.status.value,
            "mr_url": result.mr_url,
            "base_sha": result.base_sha,
            "error_code": result.error_code,
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        get_logger().exception(f"prompt evolution weekly run failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
