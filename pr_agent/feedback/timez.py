"""Beijing-time (UTC+8) helpers for storing and displaying timestamps.

The whole project standardises on Beijing time (China Standard Time, a fixed
``UTC+8`` offset with no DST) for every timestamp a human ever sees:

- New timestamps are *stored* in Beijing time with an explicit ``+08:00`` offset
  (e.g. ``2026-06-24T18:59:47.123456+08:00``) so reading the SQLite files
  directly already shows Beijing time.
- For *display*, any stored value — legacy UTC rows or new Beijing rows — is
  converted to Beijing time, so old and new data render consistently.

The module depends only on the standard library and imports nothing from the
package, so it is safe to import from anywhere without risking a cycle.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Union

# China Standard Time: fixed +08:00, no daylight saving.
BEIJING_TZ = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    """Current time as a timezone-aware datetime in Beijing time."""
    return datetime.now(BEIJING_TZ)


def now_cn_iso() -> str:
    """Current Beijing-time ISO-8601 string, e.g. ``2026-06-24T18:59:47+08:00``."""
    return now_cn().isoformat()


def to_cn(value: Union[str, datetime, None]) -> Optional[datetime]:
    """Convert an ISO timestamp into a Beijing-time datetime.

    Accepts ISO strings with any offset (a trailing ``Z`` is honoured) or a
    datetime. Naive values are assumed to be UTC (matching the project's
    historical storage). Returns ``None`` when the value is empty or unparsable.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_TZ)


def to_cn_display(value: Union[str, datetime, None], fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format any stored timestamp as Beijing local time for display.

    Falls back to a trimmed raw string when the value cannot be parsed so the
    caller never has to guard against ``None``.
    """
    dt = to_cn(value)
    if dt is None:
        return str(value or "")[:16].replace("T", " ")
    return dt.strftime(fmt)
