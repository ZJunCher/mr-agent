from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pr_agent.suggestions import review_alerts


def configure(monkeypatch):
    values = {
        "suggestion_review_dashboard.alerts_enabled": True,
        "suggestion_review_dashboard.alert_window_seconds": 1800,
        "suggestion_review_dashboard.alert_cooldown_seconds": 3600,
        "suggestion_review_dashboard.model_failure_alert_count": 3,
        "suggestion_review_dashboard.startup_retry_exhausted_alert_count": 2,
        "suggestion_review_dashboard.publish_fallback_alert_count": 3,
    }
    settings = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(review_alerts, "get_settings", lambda: settings)


def test_alert_activation_cooldown_reminder_and_resolution(tmp_path, monkeypatch):
    configure(monkeypatch)
    path = str(tmp_path / "alerts.db")
    logger = Mock()
    monkeypatch.setattr(review_alerts, "get_logger", lambda: logger)
    signals = {"model_failures": 3, "startup_retry_exhausted": 0, "publish_fallbacks": 0}
    monkeypatch.setattr(review_alerts, "count_review_alert_signals", lambda *_args, **_kwargs: dict(signals))
    first_time = datetime.fromisoformat("2026-08-18T10:00:00+08:00")

    with patch("pr_agent.suggestions.review_tracking.now_cn", return_value=first_time):
        first = review_alerts.evaluate_review_alerts(now=first_time, path=path)
        duplicate = review_alerts.evaluate_review_alerts(now=first_time, path=path)

    assert first[0]["key"] == "model_failures"
    assert duplicate[0]["count"] == 3
    logger.warning.assert_called_once()
    assert logger.warning.call_args.kwargs["artifact"] == {
        "event": "suggestion_review_aggregate_alert",
        "alert_key": "model_failures",
        "count": 3,
        "threshold": 3,
        "window_seconds": 1800,
        "state": "active",
    }

    reminder_time = datetime.fromisoformat("2026-08-18T11:01:00+08:00")
    with patch("pr_agent.suggestions.review_tracking.now_cn", return_value=reminder_time):
        review_alerts.evaluate_review_alerts(now=reminder_time, path=path)
    assert logger.warning.call_count == 2

    signals["model_failures"] = 0
    with patch("pr_agent.suggestions.review_tracking.now_cn", return_value=reminder_time):
        active = review_alerts.evaluate_review_alerts(now=reminder_time, path=path)
    assert active == []
    assert logger.info.call_args.kwargs["artifact"]["state"] == "resolved"


def test_below_threshold_does_not_emit(tmp_path, monkeypatch):
    configure(monkeypatch)
    logger = Mock()
    monkeypatch.setattr(review_alerts, "get_logger", lambda: logger)
    monkeypatch.setattr(
        review_alerts,
        "count_review_alert_signals",
        lambda *_args, **_kwargs: {
            "model_failures": 2, "startup_retry_exhausted": 1, "publish_fallbacks": 2,
        },
    )

    result = review_alerts.evaluate_review_alerts(
        now=datetime.fromisoformat("2026-08-18T10:00:00+08:00"),
        path=str(tmp_path / "alerts.db"),
    )

    assert result == []
    logger.warning.assert_not_called()
