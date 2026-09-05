"""Bounded, evidence-backed explanations for terminal CI failures."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Iterable
from urllib.parse import urlparse

from pr_agent.feedback.timez import now_cn_iso

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_SECRET_RE = re.compile(r"(?i)\b(token|password|authorization)\b\s*[:=]\s*\S+")
_CONFIDENCE_VALUES = frozenset({"confirmed", "inferred", "unknown"})


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def sanitize_failure_text(value: object, limit: int = 300) -> str:
    """Remove control data and secret-shaped values, then enforce a hard bound."""
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", str(value or ""))
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _ANSI_RE.sub("", text).replace("\x00", "")
    return " ".join(text.split())[:max(0, limit)]


def _safe_job_url(value: object) -> str:
    url = sanitize_failure_text(value, 300)
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


@dataclass(frozen=True)
class FailureExplanation:
    job_name: str
    job_url: str = ""
    confirmed_reason: str = ""
    possible_reason: str = ""
    suggested_action: str = ""
    pipeline_id: int = 0
    job_id: int = 0
    trace_line: int = 0
    confidence: str = "unknown"
    observed_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(_sanitize_record(self))

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "FailureExplanation":
        return _sanitize_record(cls(
            job_name=str(value.get("job_name") or ""),
            job_url=str(value.get("job_url") or ""),
            confirmed_reason=str(value.get("confirmed_reason") or ""),
            possible_reason=str(value.get("possible_reason") or ""),
            suggested_action=str(value.get("suggested_action") or ""),
            pipeline_id=_safe_int(value.get("pipeline_id")),
            job_id=_safe_int(value.get("job_id")),
            trace_line=_safe_int(value.get("trace_line")),
            confidence=str(value.get("confidence") or "unknown"),
            observed_at=str(value.get("observed_at") or ""),
        ))


def _sanitize_record(record: FailureExplanation) -> FailureExplanation:
    return replace(
        record,
        job_name=sanitize_failure_text(record.job_name, 120) or "unknown",
        job_url=_safe_job_url(record.job_url),
        confirmed_reason=sanitize_failure_text(record.confirmed_reason),
        possible_reason=sanitize_failure_text(record.possible_reason),
        suggested_action=sanitize_failure_text(record.suggested_action, 200),
        pipeline_id=_safe_int(record.pipeline_id),
        job_id=_safe_int(record.job_id),
        trace_line=_safe_int(record.trace_line),
        confidence=record.confidence if record.confidence in _CONFIDENCE_VALUES else "unknown",
        observed_at=sanitize_failure_text(record.observed_at, 64),
    )


def extract_confirmed_reason_with_line(trace: str) -> tuple[str, int]:
    """Return the highest-priority causal line and its 1-based trace line."""
    from ut_agent.ci_diagnostics import extract_diagnostic_candidates, primary_diagnostic

    candidates = extract_diagnostic_candidates(str(trace or ""))
    selected = primary_diagnostic(candidates.candidates)
    if selected is None:
        return "", 0
    return sanitize_failure_text(selected.text), selected.line_number


def extract_confirmed_reason(trace: str) -> str:
    """Return the highest-priority causal line, excluding generic runner noise."""
    return extract_confirmed_reason_with_line(trace)[0]


def source_job_records(
    values: Iterable[FailureExplanation | Mapping[str, object]],
    limit: int = 40,
) -> tuple[dict[str, object], ...]:
    """Return bounded, sanitized Job navigation records, newest ID per name."""
    latest: dict[str, FailureExplanation] = {}
    order: list[str] = []
    for value in values:
        record = value if isinstance(value, FailureExplanation) else FailureExplanation.from_dict(dict(value))
        record = _sanitize_record(record)
        if record.job_name not in latest:
            order.append(record.job_name)
        previous = latest.get(record.job_name)
        if previous is None or record.job_id >= previous.job_id:
            latest[record.job_name] = record
    return tuple({
        "job_name": latest[name].job_name,
        "job_id": latest[name].job_id,
        "job_url": latest[name].job_url,
        "trace_line": latest[name].trace_line,
    } for name in order[:max(0, limit)])


def merge_failure_explanations(
    confirmed: Iterable[FailureExplanation],
    inferred: Iterable[FailureExplanation],
) -> tuple[FailureExplanation, ...]:
    """Merge records by exact sanitized Job name, with direct CI evidence taking priority."""
    inferred_records = tuple(_sanitize_record(item) for item in inferred)
    inferred_by_job = {item.job_name: item for item in inferred_records}
    merged = []
    seen = set()
    for raw_direct in confirmed:
        direct = _sanitize_record(raw_direct)
        possible = inferred_by_job.get(direct.job_name)
        merged.append(_sanitize_record(replace(
            direct,
            possible_reason=possible.possible_reason if possible else direct.possible_reason,
            suggested_action=possible.suggested_action if possible else direct.suggested_action,
        )))
        seen.add(direct.job_name)
    for possible in inferred_records:
        if possible.job_name not in seen:
            merged.append(possible)
    return tuple(merged)


def _decode_trace(trace: object) -> str:
    if isinstance(trace, bytes):
        return trace.decode("utf-8", errors="replace")
    return str(trace or "")


def select_latest_failed_jobs(failed_jobs: Iterable[dict]) -> tuple[dict, ...]:
    """Keep the highest GitLab Job ID for each exact Job name."""
    latest: dict[str, dict] = {}
    for raw_job in failed_jobs:
        job = raw_job or {}
        name = str(job.get("name") or "unknown")
        job_id = _safe_int(job.get("id") or job.get("job_id"))
        previous = latest.get(name)
        previous_id = _safe_int((previous or {}).get("id") or (previous or {}).get("job_id"))
        if previous is None or job_id > previous_id:
            latest[name] = job
    return tuple(latest.values())


def collect_gitlab_failure_explanations(
    project,
    failed_jobs: Iterable[dict],
    pipeline_id: int,
) -> tuple[FailureExplanation, ...]:
    """Fetch bounded traces for current failed Jobs without exposing provider failures."""
    records = []
    for job in select_latest_failed_jobs(failed_jobs):
        job_id = _safe_int(job.get("id") or job.get("job_id"))
        try:
            trace = project.jobs.get(job_id).trace() if job_id else b""
        except Exception:
            trace = b""
        confirmed_reason, trace_line = extract_confirmed_reason_with_line(_decode_trace(trace))
        records.append(_sanitize_record(FailureExplanation(
            job_name=str(job.get("name") or "unknown"),
            job_url=str(job.get("web_url") or ""),
            confirmed_reason=confirmed_reason,
            pipeline_id=_safe_int((job.get("pipeline") or {}).get("id") or pipeline_id),
            job_id=job_id,
            trace_line=trace_line,
            confidence="confirmed" if confirmed_reason else "unknown",
            observed_at=now_cn_iso(),
        )))
    return tuple(records)
