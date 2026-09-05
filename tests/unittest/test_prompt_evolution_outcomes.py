from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pr_agent.suggestions.prompt_evolution.models import Outcome
from pr_agent.suggestions.prompt_evolution.outcomes import classify_outcome

CN = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 14, 12, tzinfo=CN)


def test_apply_wins_over_resolve():
    row = {"applied_at": NOW.isoformat(), "resolved_at": NOW.isoformat(), "created_at": NOW.isoformat()}
    assert classify_outcome(row, {"state": "opened"}, NOW, 14) is Outcome.ACCEPTED


def test_resolve_without_apply_is_rejected():
    row = {"applied_at": None, "resolved_at": NOW.isoformat(), "created_at": NOW.isoformat()}
    assert classify_outcome(row, {"state": "opened"}, NOW, 14) is Outcome.REJECTED


def test_old_open_or_merged_is_unhandled_but_closed_is_invalid():
    row = {"applied_at": None, "resolved_at": None, "created_at": (NOW - timedelta(days=15)).isoformat()}
    assert classify_outcome(row, {"state": "opened"}, NOW, 14) is Outcome.UNHANDLED
    assert classify_outcome(row, {"state": "merged"}, NOW, 14) is Outcome.UNHANDLED
    assert classify_outcome(row, {"state": "closed"}, NOW, 14) is Outcome.INVALID
