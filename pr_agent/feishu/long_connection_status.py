import threading
import time
from typing import Dict


_status_lock = threading.Lock()
_status = {
    "started": False,
    "app_id": "",
    "retry_seconds": 0,
    "start_time": 0.0,
    "last_heartbeat": 0.0,
    "reconnect_count": 0,
    "event_count": 0,
    "last_event_time": 0.0,
    "last_error": "",
    "last_error_time": 0.0,
}


def _now() -> float:
    return time.time()


def mark_started(app_id: str, retry_seconds: int) -> None:
    with _status_lock:
        _status["started"] = True
        _status["app_id"] = app_id
        _status["retry_seconds"] = retry_seconds
        _status["start_time"] = _now()


def mark_heartbeat() -> None:
    with _status_lock:
        _status["last_heartbeat"] = _now()


def mark_reconnect() -> None:
    with _status_lock:
        _status["reconnect_count"] += 1


def mark_event() -> None:
    with _status_lock:
        _status["event_count"] += 1
        _status["last_event_time"] = _now()


def mark_error(error_msg: str) -> None:
    with _status_lock:
        _status["last_error"] = (error_msg or "")[:1000]
        _status["last_error_time"] = _now()


def snapshot() -> Dict[str, object]:
    with _status_lock:
        return dict(_status)
