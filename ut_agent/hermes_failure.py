"""Bounded recognition of Hermes provider-control failures."""

from __future__ import annotations

import re
from collections.abc import Iterable

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_REQUEST_ID_RE = re.compile(
    r"(?i)(request[ _-]?id\s*[:=]\s*)[A-Za-z0-9._-]+"
)
_CONTROL_FAILURE_PATTERNS = (
    re.compile(r"(?:^|\s)API Error\s*:", re.IGNORECASE),
    re.compile(r"(?:^|\s)Non-retryable error\b", re.IGNORECASE),
    re.compile(
        r"(?:^|\s)Invalid API response\s*\(attempt\s+\d+/\d+\)\s*:",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|\s)Max retries\s*\(\d+\)\s+exceeded for invalid responses\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|\s)API call failed\s*\(attempt\s+\d+/\d+\)\s*:"
        r".*\[HTTP\s+[45]\d{2}\]",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|\s)API (?:call )?failed after\s+\d+\s+retries\b.*\bHTTP\s+[45]\d{2}\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|\s)Final error\s*:\s*HTTP\s+[45]\d{2}\b", re.IGNORECASE),
)


def _is_control_failure(value: object) -> bool:
    line = _ANSI_RE.sub("", str(value or ""))
    return any(pattern.search(line) for pattern in _CONTROL_FAILURE_PATTERNS)


def _sanitize_control_failure(value: object) -> str:
    line = _ANSI_RE.sub("", str(value or "")).strip()
    return _REQUEST_ID_RE.sub(r"\1[REDACTED]", line)


def extract_hermes_control_failure(
    stdout_lines: Iterable[str],
    stderr_lines: Iterable[str],
    *,
    limit: int = 2000,
) -> str | None:
    """Return bounded Hermes control failures without matching compiler prose."""
    matched = [
        _sanitize_control_failure(line)
        for line in (*tuple(stdout_lines), *tuple(stderr_lines))
        if _is_control_failure(line)
    ]
    text = " | ".join(line for line in matched[-3:] if line)
    return text[:max(0, int(limit))] or None
