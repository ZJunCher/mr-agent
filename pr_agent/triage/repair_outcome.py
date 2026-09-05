"""Category-scoped outcome for one CI repair attempt."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Iterable

from pr_agent.distributed.models import RepairCategory
from pr_agent.triage.failure_categories import categorize_failed_job


class RepairOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    BLOCKED = "blocked"
    FAILED = "failed"


class CategoryRepairOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    NOT_SELECTED = "not_selected"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class CategoryRepairResult:
    category: str
    outcome: CategoryRepairOutcome
    selection: str
    source_failed_job_names: tuple[str, ...] = ()
    validation_failed_job_names: tuple[str, ...] = ()
    verified_repaired_job_names: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        if not self.verified_repaired_job_names:
            value.pop("verified_repaired_job_names")
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "CategoryRepairResult":
        return cls(
            category=str(value.get("category") or "unknown"),
            outcome=CategoryRepairOutcome(value.get("outcome") or CategoryRepairOutcome.UNVERIFIED.value),
            selection=str(value.get("selection") or "not_selected"),
            source_failed_job_names=tuple(str(name) for name in value.get("source_failed_job_names") or ()),
            validation_failed_job_names=tuple(
                str(name) for name in value.get("validation_failed_job_names") or ()
            ),
            verified_repaired_job_names=tuple(
                str(name) for name in value.get("verified_repaired_job_names") or ()
            ),
            reason=str(value.get("reason") or ""),
        )


@dataclass(frozen=True)
class RepairVerdict:
    outcome: RepairOutcome
    category_results: tuple[CategoryRepairResult, ...]
    introduced_failure_categories: tuple[str, ...] = ()
    introduced_failed_job_names: tuple[str, ...] = ()


_CATEGORY_ORDER = (
    RepairCategory.FORMAT,
    RepairCategory.CLANG,
    RepairCategory.BUILD,
    RepairCategory.UNKNOWN,
)


def _group_names(job_names: Iterable[str]) -> dict[RepairCategory, tuple[str, ...]]:
    grouped: dict[RepairCategory, list[str]] = {category: [] for category in _CATEGORY_ORDER}
    for raw_name in job_names:
        name = str(raw_name or "").strip()
        if name:
            grouped[categorize_failed_job({"name": name})].append(name)
    return {category: tuple(names) for category, names in grouped.items() if names}


def verified_selected_success_count(results: Iterable[CategoryRepairResult]) -> int:
    """Count selected categories with at least one benefit proven by validation."""
    return sum(
        result.selection == "selected"
        and (
            result.outcome is CategoryRepairOutcome.SUCCEEDED
            or (
                result.outcome is not CategoryRepairOutcome.UNVERIFIED
                and bool(result.verified_repaired_job_names)
            )
        )
        for result in results
    )


def evaluate_repair_outcome(
    *,
    source_failed_job_names: Iterable[str],
    validation_failed_jobs: Iterable[dict],
    selected_categories: Iterable[RepairCategory | str],
    effective_categories: Iterable[RepairCategory | str] = (),
    validation_reliable: bool = True,
    blocked_job_names: Iterable[str] = (),
) -> RepairVerdict:
    """Evaluate only the repair scope selected by the user."""
    source_groups = _group_names(source_failed_job_names)
    validation_names = tuple(
        str((job or {}).get("name") or "").strip()
        for job in validation_failed_jobs
        if str((job or {}).get("name") or "").strip()
    )
    validation_groups = _group_names(validation_names)
    blocked_names = {
        str(name or "").strip()
        for name in blocked_job_names
        if str(name or "").strip()
    }
    selected = {RepairCategory(category) for category in selected_categories}
    effective = {RepairCategory(category) for category in effective_categories} or selected
    introduced_categories = tuple(
        category.value
        for category in _CATEGORY_ORDER
        if category in validation_groups
        and category not in source_groups
        and any(name not in blocked_names for name in validation_groups.get(category, ()))
    )
    introduced_names = tuple(
        name
        for category in _CATEGORY_ORDER
        if category.value in introduced_categories
        for name in validation_groups.get(category, ())
        if name not in blocked_names
    )

    results: list[CategoryRepairResult] = []
    for category in _CATEGORY_ORDER:
        source_names = source_groups.get(category, ())
        validation_category_names = validation_groups.get(category, ())
        blocked_category_names = tuple(name for name in validation_category_names if name in blocked_names)
        nonblocked_category_names = tuple(name for name in validation_category_names if name not in blocked_names)
        validation_name_set = set(validation_category_names)
        verified_repaired_names = (
            tuple(name for name in source_names if name not in validation_name_set)
            if validation_reliable
            else ()
        )
        if category in selected:
            if nonblocked_category_names:
                outcome = CategoryRepairOutcome.FAILED
                reason = "验证流水线仍有该类非阻塞失败任务"
            elif blocked_category_names:
                outcome = CategoryRepairOutcome.BLOCKED
                reason = "当前类别被外部依赖阻塞"
            elif not validation_reliable:
                outcome = CategoryRepairOutcome.UNVERIFIED
                reason = "无法可靠确认验证流水线结果"
            else:
                outcome = CategoryRepairOutcome.SUCCEEDED
                reason = "所选类别的失败任务已全部通过"
            selection = "selected"
        elif category in effective:
            if nonblocked_category_names:
                outcome = CategoryRepairOutcome.FAILED
            elif blocked_category_names:
                outcome = CategoryRepairOutcome.BLOCKED
            elif not validation_reliable:
                outcome = CategoryRepairOutcome.UNVERIFIED
            else:
                outcome = CategoryRepairOutcome.SUCCEEDED
            reason = "系统附带处理的格式清理" if category is RepairCategory.FORMAT else "系统附带处理"
            selection = "effective_cleanup"
        else:
            outcome = CategoryRepairOutcome.NOT_SELECTED
            reason = "本次未选择"
            selection = "not_selected"
        if source_names or validation_category_names or category in selected or category in effective:
            results.append(CategoryRepairResult(
                category=category.value,
                outcome=outcome,
                selection=selection,
                source_failed_job_names=source_names,
                validation_failed_job_names=validation_category_names,
                verified_repaired_job_names=verified_repaired_names,
                reason=reason,
            ))

    selected_results = [result for result in results if result.selection == "selected"]
    verified_benefit = verified_selected_success_count(selected_results)
    fully_succeeded = sum(
        result.outcome is CategoryRepairOutcome.SUCCEEDED
        for result in selected_results
    )
    if introduced_names:
        aggregate = RepairOutcome.FAILED
    elif selected_results and fully_succeeded == len(selected_results):
        aggregate = RepairOutcome.SUCCESS
    elif verified_benefit:
        aggregate = RepairOutcome.PARTIAL_SUCCESS
    elif selected_results and all(result.outcome is CategoryRepairOutcome.BLOCKED for result in selected_results):
        aggregate = RepairOutcome.BLOCKED
    else:
        aggregate = RepairOutcome.FAILED
    return RepairVerdict(aggregate, tuple(results), introduced_categories, introduced_names)
