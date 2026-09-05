"""Deduplicated aggregate alerts for automatic suggestion-review failures."""

from __future__ import annotations

from datetime import datetime, timedelta

from pr_agent.config_loader import get_settings
from pr_agent.feedback.timez import now_cn
from pr_agent.log import get_logger
from pr_agent.suggestions.review_tracking import (
    count_review_alert_signals,
    list_active_review_alerts,
    update_review_alert_state,
)

ALERT_CONFIG = {
    "model_failures": "model_failure_alert_count",
    "startup_retry_exhausted": "startup_retry_exhausted_alert_count",
    "publish_fallbacks": "publish_fallback_alert_count",
}
DEFAULT_THRESHOLDS = {
    "model_failures": 3,
    "startup_retry_exhausted": 2,
    "publish_fallbacks": 3,
}
DEFAULT_WINDOW_SECONDS = 1800
DEFAULT_COOLDOWN_SECONDS = 3600


def _setting(name: str, default):
    return get_settings().get(f"suggestion_review_dashboard.{name}", default)


def _validated_int(name: str, default: int, minimum: int) -> int:
    try:
        value = int(_setting(name, default))
        if value < minimum:
            raise ValueError
        return value
    except (TypeError, ValueError):
        get_logger().error(
            f"Invalid suggestion review alert setting {name}; using default {default}"
        )
        return default


def get_review_alert_configuration() -> tuple[int, int, dict[str, int]]:
    window_seconds = _validated_int("alert_window_seconds", DEFAULT_WINDOW_SECONDS, 60)
    cooldown_seconds = _validated_int("alert_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS, 60)
    thresholds = {
        key: _validated_int(setting, DEFAULT_THRESHOLDS[key], 1)
        for key, setting in ALERT_CONFIG.items()
    }
    return window_seconds, cooldown_seconds, thresholds


def active_review_alerts_payload(path: str | None = None) -> list[dict]:
    if not bool(_setting("alerts_enabled", True)):
        return []
    window_seconds, _, thresholds = get_review_alert_configuration()
    return [
        {
            "key": str(row.get("alert_key") or ""),
            "count": int(row.get("last_count") or 0),
            "threshold": thresholds.get(str(row.get("alert_key") or ""), 1),
            "window_seconds": window_seconds,
            "first_triggered_at": str(row.get("first_triggered_at") or ""),
        }
        for row in list_active_review_alerts(path=path)
        if str(row.get("alert_key") or "") in ALERT_CONFIG
    ]


def evaluate_review_alerts(*, now: datetime | None = None, path: str | None = None) -> list[dict]:
    """Evaluate and persist aggregate alerts. Never raises."""
    if not bool(_setting("alerts_enabled", True)):
        return []
    try:
        current = now or now_cn()
        window_seconds, cooldown_seconds, thresholds = get_review_alert_configuration()
        since = (current - timedelta(seconds=window_seconds)).isoformat()
        signals = count_review_alert_signals(since, path=path)
        for alert_key, threshold in thresholds.items():
            count = int(signals.get(alert_key) or 0)
            transition = update_review_alert_state(
                alert_key,
                active=count >= threshold,
                count=count,
                cooldown_seconds=cooldown_seconds,
                path=path,
            )
            artifact = {
                "event": "suggestion_review_aggregate_alert",
                "alert_key": alert_key,
                "count": count,
                "threshold": threshold,
                "window_seconds": window_seconds,
            }
            if transition.should_emit:
                get_logger().warning(
                    f"Suggestion review aggregate alert active: {alert_key}={count}",
                    artifact={**artifact, "state": "active"},
                )
            elif transition.resolved:
                get_logger().info(
                    f"Suggestion review aggregate alert resolved: {alert_key}",
                    artifact={**artifact, "state": "resolved"},
                )
        return active_review_alerts_payload(path=path)
    except Exception as exc:
        get_logger().error(f"Suggestion review aggregate alert evaluation failed: {exc}")
        return []
