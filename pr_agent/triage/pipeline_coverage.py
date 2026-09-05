"""Resolve normalized coverage for one selected validation Pipeline."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

ARTIFACT_PATH = "coverage_html/changed_lines.html"

CoverageSource = Literal["", "changed_lines", "gitlab_pipeline"]
CoverageStatus = Literal[
    "reported",
    "validation_pipeline_missing",
    "not_configured",
    "job_failed",
    "report_missing",
    "fetch_failed",
]

_PERCENTAGE_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_COVERAGE_LABELS = {
    "changed_lines": "变更行覆盖率",
    "gitlab_pipeline": "Pipeline 覆盖率",
}
_COVERAGE_UNAVAILABLE_REASONS = {
    "validation_pipeline_missing": "尚未找到验证流水线",
    "not_configured": "流水线未配置覆盖率 Job",
    "job_failed": "覆盖率 Job 失败，未产出结果",
    "report_missing": "覆盖率 Job 通过，但未产出报告",
    "fetch_failed": "覆盖率报告读取失败",
}


@dataclass(frozen=True)
class CoverageResult:
    value: float | None = None
    source: CoverageSource = ""
    status: CoverageStatus = "not_configured"
    job_id: int | None = None
    threshold: float | None = None


def coverage_label(source: str) -> str:
    """Return the user-facing metric name for a fixed coverage source."""
    return _COVERAGE_LABELS.get(str(source or "").strip(), "覆盖率")


def coverage_unavailable_reason(status: str) -> str:
    """Return a stable user-facing reason for missing coverage."""
    return _COVERAGE_UNAVAILABLE_REASONS.get(str(status or "").strip(), "未提供")


def normalize_coverage(value: object) -> float | None:
    """Return a finite percentage from 0 through 100."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or not 0 <= result <= 100:
        return None
    return result


def parse_changed_lines_summary(html: str) -> dict[str, int | float]:
    """Parse the summary fields emitted by changed_lines.html."""
    cleaned = re.sub(r'\sstyle\s*=\s*"[^"]*"', " ", str(html), flags=re.IGNORECASE)
    cleaned = re.sub(r"\sstyle\s*=\s*'[^']*'", " ", cleaned, flags=re.IGNORECASE)
    result: dict[str, int | float] = {}
    count_fields = {
        "total": "总修改行数",
        "covered": "已覆盖行数",
        "uncovered": "未覆盖行数",
    }
    for key, label in count_fields.items():
        match = re.search(rf"{label}[\s\S]{{0,200}}?>\s*(\d+)\s*<", cleaned)
        if match:
            result[key] = int(match.group(1))
    match = re.search(rf"覆盖率[\s\S]{{0,200}}?>\s*({_PERCENTAGE_RE})\s*%", cleaned)
    if match:
        coverage = normalize_coverage(match.group(1))
        if coverage is not None:
            result["coverage_pct"] = coverage
    return result


def parse_coverage_trace(trace: str) -> dict[str, int | float]:
    """Parse the changed-lines summary emitted in a Coverage Job trace."""
    text = str(trace)
    result: dict[str, int | float] = {}
    percentage_fields = {"coverage": "Coverage", "threshold": "Threshold"}
    for key, label in percentage_fields.items():
        match = re.search(rf"{label}:\s*({_PERCENTAGE_RE})%", text, flags=re.IGNORECASE)
        if match:
            value = normalize_coverage(match.group(1))
            if value is not None:
                result[key] = value
    count_fields = {
        "total_lines": "Total changed lines",
        "covered_lines": "Covered changed lines",
    }
    for key, label in count_fields.items():
        match = re.search(rf"{label}:\s*(\d+)", text, flags=re.IGNORECASE)
        if match:
            result[key] = int(match.group(1))
    return result


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _is_not_found(error: Exception) -> bool:
    return str(getattr(error, "response_code", "")) == "404"


def _job_id(job: Any) -> int:
    return int(getattr(job, "id", 0) or 0)


def resolve_pipeline_coverage(
    project: Any,
    pipeline: Any,
    jobs: tuple[tuple[int, Any], ...],
    required_job_patterns: tuple[str, ...],
) -> CoverageResult:
    """Resolve changed-lines coverage before falling back to GitLab Pipeline coverage."""
    coverage_patterns = tuple(
        str(pattern).lower()
        for pattern in required_job_patterns
        if "coverage" in str(pattern).lower()
    )
    coverage_jobs = [
        job
        for _, job in jobs
        if any(pattern in str(getattr(job, "name", "")).lower() for pattern in coverage_patterns)
    ]
    completed_jobs = sorted(
        (
            job
            for job in coverage_jobs
            if str(getattr(job, "status", "")).lower() in {"success", "failed"}
        ),
        key=_job_id,
        reverse=True,
    )
    has_failed_job = any(str(getattr(job, "status", "")).lower() == "failed" for job in coverage_jobs)
    fetch_failed_job_id = None

    for summary_job in completed_jobs:
        job_id = _job_id(summary_job)
        try:
            job = project.jobs.get(job_id)
        except Exception:
            fetch_failed_job_id = fetch_failed_job_id or job_id
            logger.warning("Coverage Job could not be loaded (job_id=%s)", job_id)
            continue

        try:
            trace_summary = parse_coverage_trace(_text(job.trace()))
            trace_coverage = normalize_coverage(trace_summary.get("coverage"))
            trace_threshold = normalize_coverage(trace_summary.get("threshold"))
        except Exception:
            trace_summary = {}
            trace_coverage = None
            trace_threshold = None
            fetch_failed_job_id = fetch_failed_job_id or job_id
            logger.warning("Coverage trace could not be read (job_id=%s)", job_id)

        try:
            artifact = job.artifact(ARTIFACT_PATH)
        except Exception as error:
            artifact = None
            if not _is_not_found(error):
                fetch_failed_job_id = fetch_failed_job_id or job_id
                logger.warning("Coverage Artifact could not be read (job_id=%s)", job_id)
        if artifact is not None:
            try:
                artifact_coverage = normalize_coverage(
                    parse_changed_lines_summary(_text(artifact)).get("coverage_pct")
                )
            except Exception:
                artifact_coverage = None
                fetch_failed_job_id = fetch_failed_job_id or job_id
                logger.warning("Coverage Artifact could not be parsed (job_id=%s)", job_id)
            if artifact_coverage is not None:
                return CoverageResult(
                    artifact_coverage,
                    "changed_lines",
                    "reported",
                    job_id,
                    trace_threshold,
                )
        if trace_coverage is not None:
            return CoverageResult(trace_coverage, "changed_lines", "reported", job_id, trace_threshold)

    raw_pipeline_coverage = getattr(pipeline, "coverage", None)
    if raw_pipeline_coverage is None:
        raw_pipeline_coverage = (getattr(pipeline, "attributes", {}) or {}).get("coverage")
    pipeline_coverage = normalize_coverage(raw_pipeline_coverage)
    if pipeline_coverage is not None:
        return CoverageResult(pipeline_coverage, "gitlab_pipeline", "reported")
    if not coverage_jobs:
        return CoverageResult(status="not_configured")
    if fetch_failed_job_id is not None:
        return CoverageResult(status="fetch_failed", job_id=fetch_failed_job_id)
    if has_failed_job:
        return CoverageResult(status="job_failed")
    return CoverageResult(status="report_missing")
