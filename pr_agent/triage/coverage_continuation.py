"""Pure policy for the optional coverage continuation after a verified CI repair."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from pr_agent.triage.pipeline_coverage import CoverageResult
from pr_agent.triage.pipeline_repair import PipelineRepairState


@dataclass(frozen=True)
class CoverageContinuationDecision:
    eligible: bool
    code: str
    message: str


_MESSAGES = {
    "eligible": "编译问题已修复，可以根据覆盖率报告补充一次单元测试。",
    "disabled": "覆盖率补测功能未启用。",
    "unsupported_selection": "本次选择不属于可续跑的 Clang 或 Build 修复。",
    "baseline_missing": "缺少已验证的编译修复提交。",
    "non_coverage_failure_remains": "仍有非覆盖率任务失败，暂不补充测试。",
    "coverage_job_missing": "验证流水线没有覆盖率失败任务。",
    "coverage_report_missing": "覆盖率任务未生成可读取的未覆盖行报告。",
    "coverage_value_missing": "覆盖率报告缺少有效覆盖率。",
    "coverage_threshold_missing": "覆盖率报告缺少有效阈值。",
    "coverage_already_sufficient": "覆盖率已经达到阈值。",
    "uncovered_lines_empty": "覆盖率报告没有可用于补测的未覆盖代码行。",
    "attempt_limit_reached": "本次任务已执行过覆盖率补测。",
}


def is_coverage_job(job: dict) -> bool:
    """Return whether one failed job is explicitly a coverage job."""
    return "coverage" in str((job or {}).get("name") or "").lower()


def non_coverage_jobs(failed_jobs: Iterable[dict]) -> tuple[dict, ...]:
    """Keep failures that must still block optional test generation."""
    return tuple(job for job in failed_jobs if not is_coverage_job(job))


def _decision(code: str, eligible: bool = False) -> CoverageContinuationDecision:
    return CoverageContinuationDecision(eligible, code, _MESSAGES[code])


def _finite_percentage(value: object) -> float | None:
    if isinstance(value, bool) or value in {None, ""}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def decide_coverage_continuation(
    *,
    state: PipelineRepairState,
    failed_jobs: Iterable[dict],
    coverage: CoverageResult,
    report_available: bool,
    uncovered_line_count: int,
    enabled: bool,
    max_attempts: int,
) -> CoverageContinuationDecision:
    """Return one deterministic continuation verdict with a stable reason."""
    jobs = tuple(failed_jobs)
    if not enabled or max_attempts <= 0:
        return _decision("disabled")
    selected = {str(category) for category in state.selected_categories}
    if not selected or not selected.issubset({"clang", "build"}):
        return _decision("unsupported_selection")
    if len(state.coverage_baseline_sha) != 40:
        return _decision("baseline_missing")
    if non_coverage_jobs(jobs):
        return _decision("non_coverage_failure_remains")
    if not any(is_coverage_job(job) for job in jobs):
        return _decision("coverage_job_missing")
    if not report_available:
        return _decision("coverage_report_missing")
    value = _finite_percentage(coverage.value)
    if value is None:
        return _decision("coverage_value_missing")
    threshold = _finite_percentage(coverage.threshold)
    if threshold is None:
        return _decision("coverage_threshold_missing")
    if value >= threshold:
        return _decision("coverage_already_sufficient")
    if uncovered_line_count <= 0:
        return _decision("uncovered_lines_empty")
    if state.coverage_attempts >= min(max(0, int(max_attempts)), 1):
        return _decision("attempt_limit_reached")
    return _decision("eligible", eligible=True)
