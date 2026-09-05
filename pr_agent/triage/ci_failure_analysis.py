"""Deterministic, bounded analysis for failed CI Jobs."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Mapping
from urllib.parse import urlparse

from pr_agent.config_loader import get_settings
from pr_agent.triage.failure_explanations import (
    extract_confirmed_reason_with_line,
    sanitize_failure_text,
    select_latest_failed_jobs,
)
from ut_agent.repair_progress import normalize_diagnostic


class FailureFamily(str, Enum):
    BUILD = "build"
    CLANG = "clang"
    FORMAT = "format"
    TEST = "test"
    COVERAGE = "coverage"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


class CapabilityClass(str, Enum):
    SUPPORTED = "supported"
    CAPABILITY_GAP = "capability_gap"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AnalyzedFailureJob:
    job_id: int
    job_name: str
    stage: str
    job_url: str
    pipeline_id: int
    family: FailureFamily
    confirmed_reason: str
    trace_line: int
    reason_confidence: str
    fingerprint: str
    capability: CapabilityClass
    capability_basis: str
    capability_confidence: str


@dataclass(frozen=True)
class FailureAggregate:
    failed_job_count: int
    unknown_reason_count: int
    categories: tuple[str, ...]
    primary_reason: str
    primary_fingerprint: str


_DEFAULTS = {
    "trace_job_limit": 20,
    "trace_bytes_limit": 131_072,
    "format_keywords": ("code_format_check", "format"),
    "clang_keywords": ("clang", "clang-tidy"),
    "coverage_keywords": ("coverage",),
    "test_keywords": ("test", "pytest", "gtest", "unittest"),
    "build_keywords": ("build", "compile", "cmake", "ninja", "link"),
    "infrastructure_patterns": (
        "connection timed out",
        "connection reset",
        "runner system failure",
        "no space left on device",
        "failed to pull image",
        "service unavailable",
        "temporary failure in name resolution",
    ),
}


def _analysis_settings() -> dict:
    try:
        configured = get_settings().get("ci_failure_dashboard", {}) or {}
        return {**_DEFAULTS, **dict(configured)}
    except Exception:
        return dict(_DEFAULTS)


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _bounded_trace(value: object, byte_limit: int) -> str:
    if isinstance(value, bytes):
        raw = value[:byte_limit]
        return raw.decode("utf-8", errors="replace")
    raw = str(value or "").encode("utf-8")[:byte_limit]
    return raw.decode("utf-8", errors="replace")


def _safe_url(value: object) -> str:
    url = sanitize_failure_text(value, 500)
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _contains_any(value: str, patterns: object) -> bool:
    return any(str(pattern).lower() in value for pattern in (patterns or ()))


def _failure_family(job_name: str, reason: str, settings: Mapping[str, object]) -> FailureFamily:
    lowered_reason = reason.lower()
    if _contains_any(lowered_reason, settings.get("infrastructure_patterns")):
        return FailureFamily.INFRASTRUCTURE
    lowered_name = job_name.lower()
    checks = (
        (FailureFamily.FORMAT, "format_keywords"),
        (FailureFamily.CLANG, "clang_keywords"),
        (FailureFamily.COVERAGE, "coverage_keywords"),
        (FailureFamily.TEST, "test_keywords"),
        (FailureFamily.BUILD, "build_keywords"),
    )
    for family, key in checks:
        if _contains_any(lowered_name, settings.get(key)):
            return family
    if reason and any(marker in lowered_reason for marker in ("error:", "fatal:", "undefined reference")):
        return FailureFamily.BUILD
    return FailureFamily.UNKNOWN


def _fingerprint(reason: str, job_name: str) -> str:
    """Use the exact normalization contract used by Repair Memory episodes."""
    normalized = normalize_diagnostic(reason, job_name=job_name)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _has_active_memory(fingerprint: str, path: str | None) -> bool:
    if not fingerprint or not path:
        return False
    try:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(
                "SELECT 1 FROM repair_memories WHERE diagnostic_fingerprint = ? AND status = 'active' LIMIT 1",
                (fingerprint,),
            ).fetchone() is not None
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return False


def _capability(
    family: FailureFamily,
    reason: str,
    fingerprint: str,
    memory_path: str | None,
) -> tuple[CapabilityClass, str, str]:
    if family is FailureFamily.INFRASTRUCTURE:
        return CapabilityClass.INFRASTRUCTURE, "infrastructure_pattern", "high"
    if _has_active_memory(fingerprint, memory_path):
        return CapabilityClass.SUPPORTED, "verified_memory_exact_fingerprint", "high"
    if reason and family is not FailureFamily.UNKNOWN:
        return CapabilityClass.CAPABILITY_GAP, "code_failure_without_verified_support", "medium"
    return CapabilityClass.UNKNOWN, "insufficient_evidence", "low"


def analyze_failed_jobs(
    failed_jobs: Iterable[dict],
    trace_loader: Callable[[int], object],
    *,
    pipeline_id: int,
    memory_path: str | None = None,
) -> tuple[AnalyzedFailureJob, ...]:
    """Return bounded Job analysis without raising provider or storage failures."""
    settings = _analysis_settings()
    job_limit = max(0, _safe_int(settings.get("trace_job_limit")))
    byte_limit = max(1, _safe_int(settings.get("trace_bytes_limit")))
    output = []
    for index, job in enumerate(select_latest_failed_jobs(failed_jobs)):
        job_id = _safe_int(job.get("id") or job.get("job_id"))
        trace = ""
        if index < job_limit and job_id:
            try:
                trace = _bounded_trace(trace_loader(job_id), byte_limit)
            except Exception:
                trace = ""
        confirmed_reason, trace_line = extract_confirmed_reason_with_line(trace)
        confirmed_reason = sanitize_failure_text(confirmed_reason)
        job_name = str(job.get("name") or "unknown")
        family = _failure_family(job_name, confirmed_reason, settings)
        fingerprint = _fingerprint(confirmed_reason, job_name) if confirmed_reason else ""
        capability, basis, capability_confidence = _capability(
            family,
            confirmed_reason,
            fingerprint,
            memory_path,
        )
        output.append(AnalyzedFailureJob(
            job_id=job_id,
            job_name=sanitize_failure_text(job.get("name") or "unknown", 120),
            stage=sanitize_failure_text(job.get("stage") or "", 80),
            job_url=_safe_url(job.get("web_url")),
            pipeline_id=_safe_int((job.get("pipeline") or {}).get("id") or pipeline_id),
            family=family,
            confirmed_reason=confirmed_reason,
            trace_line=trace_line,
            reason_confidence="confirmed" if confirmed_reason else "unknown",
            fingerprint=fingerprint,
            capability=capability,
            capability_basis=basis,
            capability_confidence=capability_confidence,
        ))
    return tuple(output)


def aggregate_failure(jobs: Iterable[AnalyzedFailureJob]) -> FailureAggregate:
    records = tuple(jobs)
    family_order = {family.value: index for index, family in enumerate(FailureFamily)}
    categories = tuple(sorted({item.family.value for item in records}, key=family_order.get))
    primary = next((item for item in records if item.confirmed_reason), None)
    return FailureAggregate(
        failed_job_count=len(records),
        unknown_reason_count=sum(not item.confirmed_reason for item in records),
        categories=categories,
        primary_reason=primary.confirmed_reason if primary else "",
        primary_fingerprint=primary.fingerprint if primary else "",
    )
