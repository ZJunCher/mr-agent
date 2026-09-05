from dataclasses import replace
from typing import Iterable

from pr_agent.config_loader import get_settings
from pr_agent.distributed.models import RepairCategory, RepairItem, RepairItemStatus

_CATEGORY_ORDER = (
    RepairCategory.FORMAT,
    RepairCategory.CLANG,
    RepairCategory.BUILD,
    RepairCategory.UNKNOWN,
)

_CATEGORY_DEFAULTS = {
    RepairCategory.PIPELINE: ("流水线", "/repair-pipeline", "修复流水线", "primary"),
    RepairCategory.FORMAT: ("Format", "/fix-format", "修复格式", "primary"),
    RepairCategory.CLANG: ("Clang", "/triage", "修复静态分析问题", "danger"),
    RepairCategory.BUILD: ("Build", "/triage", "修复编译错误", "danger"),
    RepairCategory.UNKNOWN: ("Unknown", "/triage", "自动诊断修复", "danger"),
}

_KEYWORD_DEFAULTS = {
    RepairCategory.FORMAT: ["code_format_check", "format"],
    RepairCategory.CLANG: ["clang", "clang-tidy"],
    RepairCategory.BUILD: ["build", "compile", "cmake", "ninja", "coverage"],
}


def category_metadata(category: RepairCategory) -> tuple[str, str, str, str]:
    return _CATEGORY_DEFAULTS[category]


def pipeline_repair_item(pipeline_id: int, pipeline_sha: str) -> RepairItem:
    display_name, command, label, button_type = category_metadata(RepairCategory.PIPELINE)
    return RepairItem(
        category=RepairCategory.PIPELINE,
        command=command,
        label=label,
        display_name=display_name,
        button_type=button_type,
        status=RepairItemStatus.PENDING,
        pipeline_id=pipeline_id,
        pipeline_sha=pipeline_sha,
    )


def _keywords(category: RepairCategory) -> list[str]:
    defaults = _KEYWORD_DEFAULTS[category]
    values = get_settings().get(f"FEISHU.PIPELINE_{category.value.upper()}_JOB_KEYWORDS", defaults) or defaults
    return [str(value).lower() for value in values if str(value).strip()]


def categorize_failed_job(job: dict) -> RepairCategory:
    name = str((job or {}).get("name") or "").lower()
    for category in _CATEGORY_ORDER[:-1]:
        if any(keyword in name for keyword in _keywords(category)):
            return category
    return RepairCategory.UNKNOWN


def group_failed_jobs(failed_jobs: Iterable[dict]) -> dict[RepairCategory, tuple[dict, ...]]:
    grouped: dict[RepairCategory, list[dict]] = {category: [] for category in _CATEGORY_ORDER}
    for job in failed_jobs:
        grouped[categorize_failed_job(job)].append(job)
    return {category: tuple(grouped[category]) for category in _CATEGORY_ORDER if grouped[category]}


def classify_failed_jobs(failed_jobs: Iterable[dict]) -> list[RepairCategory]:
    return list(group_failed_jobs(failed_jobs))


def collect_failed_jobs(project, pipeline_id: int, visited: set[int] | None = None) -> list[dict]:
    visited = visited or set()
    if pipeline_id in visited or len(visited) > 20:
        return []
    visited.add(pipeline_id)
    pipeline = project.pipelines.get(pipeline_id)
    jobs = []
    for job in pipeline.jobs.list(get_all=True, per_page=100):
        normalized = job if isinstance(job, dict) else dict(getattr(job, "attributes", {}) or {})
        if str(normalized.get("status") or "").lower() == "failed":
            jobs.append(normalized)
    for bridge in pipeline.bridges.list(get_all=True, per_page=100):
        downstream = bridge.get("downstream_pipeline") if isinstance(bridge, dict) else getattr(
            bridge, "downstream_pipeline", None
        )
        downstream_id = downstream.get("id") if isinstance(downstream, dict) else getattr(downstream, "id", None)
        if downstream_id:
            jobs.extend(collect_failed_jobs(project, int(downstream_id), visited))
    return jobs


def repair_items_for_categories(
    categories: Iterable[RepairCategory | str],
    pipeline_id: int,
    pipeline_sha: str,
) -> tuple[RepairItem, ...]:
    normalized = {RepairCategory(category) for category in categories}
    items = []
    for category in _CATEGORY_ORDER:
        if category not in normalized:
            continue
        display_name, command, label, button_type = category_metadata(category)
        items.append(
            RepairItem(
                category=category,
                command=command,
                label=label,
                display_name=display_name,
                button_type=button_type,
                status=RepairItemStatus.PENDING,
                pipeline_id=pipeline_id,
                pipeline_sha=pipeline_sha,
            )
        )
    return tuple(items)


def repair_items_for_failed_jobs(
    failed_jobs: Iterable[dict],
    pipeline_id: int,
    pipeline_sha: str,
) -> tuple[RepairItem, ...]:
    action_jobs = [
        job
        for job in failed_jobs
        if not (
            categorize_failed_job(job) is RepairCategory.FORMAT
            and (job or {}).get("auto_repair_eligible") is False
        )
    ]
    grouped = group_failed_jobs(action_jobs)
    items = []
    for category in _CATEGORY_ORDER:
        jobs = grouped.get(category)
        if not jobs:
            continue
        item = repair_items_for_categories((category,), pipeline_id, pipeline_sha)[0]
        items.append(
            replace(
                item,
                failed_job_names=tuple(str(job.get("name") or "") for job in jobs if str(job.get("name") or "")),
            )
        )
    return tuple(items)


def bind_auto_format_cleanup(
    previous_items: Iterable[RepairItem],
    *,
    task_id: str,
    failed_jobs: Iterable[dict],
    pipeline_id: int,
    pipeline_sha: str,
) -> tuple[RepairItem, ...]:
    previous = {item.category: item for item in previous_items}
    format_jobs = [job for job in failed_jobs if categorize_failed_job(job) is RepairCategory.FORMAT]
    if not format_jobs:
        format_jobs = [{"name": "code_format_check"}]
    format_item = previous.get(RepairCategory.FORMAT)
    if format_item is None:
        format_item = repair_items_for_failed_jobs(format_jobs, pipeline_id, pipeline_sha)[0]
    previous[RepairCategory.FORMAT] = replace(
        format_item,
        task_id=task_id,
        status=RepairItemStatus.RUNNING,
        pipeline_id=pipeline_id,
        pipeline_sha=pipeline_sha,
        status_markdown="检测到格式问题，正在自动修复",
        failed_job_names=tuple(str(job.get("name") or "") for job in format_jobs),
    )
    return tuple(previous[category] for category in _CATEGORY_ORDER if category in previous)


def reconcile_batch_repair_items(
    previous_items: Iterable[RepairItem],
    selected_categories: Iterable[RepairCategory | str],
    effective_categories: Iterable[RepairCategory | str],
    failed_jobs: Iterable[dict],
    pipeline_id: int,
    pipeline_sha: str,
    error: str = "",
    failure_explanations=(),
    category_results=(),
    *,
    result_pipeline_id: int | None = None,
    result_pipeline_sha: str | None = None,
) -> tuple[RepairItem, ...]:
    public_result_pipeline_id = pipeline_id if result_pipeline_id is None else result_pipeline_id
    public_result_pipeline_sha = pipeline_sha if result_pipeline_sha is None else result_pipeline_sha
    previous = {item.category: item for item in previous_items if item.category is not RepairCategory.PIPELINE}
    selected = {RepairCategory(category) for category in selected_categories}
    effective = {RepairCategory(category) for category in effective_categories} or selected
    explanations_by_job = {record.job_name: record for record in failure_explanations}
    results_by_category = {
        str(result.category): result
        for result in category_results or ()
        if getattr(result, "category", "")
    }
    latest_items = {
        item.category: item for item in repair_items_for_failed_jobs(failed_jobs, pipeline_id, pipeline_sha)
    }
    categories = set(previous) | set(latest_items) | selected | effective
    output = []
    for category in _CATEGORY_ORDER:
        if category not in categories:
            continue
        item = previous.get(category) or latest_items.get(category)
        if item is None:
            item = repair_items_for_categories((category,), pipeline_id, pipeline_sha)[0]
        latest_item = latest_items.get(category)
        category_result = results_by_category.get(category.value)
        if getattr(getattr(category_result, "outcome", None), "value", "") == "blocked":
            status = RepairItemStatus.BLOCKED
            status_markdown = "当前类别被外部依赖阻塞"
            failed_job_names = latest_item.failed_job_names if latest_item is not None else item.failed_job_names
            explanations = tuple(
                explanations_by_job[name]
                for name in failed_job_names
                if name in explanations_by_job
            )
        elif latest_item is not None:
            status = RepairItemStatus.FAILED if category in effective else RepairItemStatus.PENDING
            status_markdown = (error or "流水线仍有该类失败任务") if category in effective else ""
            failed_job_names = latest_item.failed_job_names
            explanations = tuple(
                explanations_by_job[name]
                for name in failed_job_names
                if name in explanations_by_job
            )
        elif category in effective:
            status = RepairItemStatus.SUCCEEDED
            status_markdown = "最新流水线已通过"
            failed_job_names = item.failed_job_names
            explanations = ()
        else:
            status = RepairItemStatus.RESOLVED
            status_markdown = "已随本次修复通过"
            failed_job_names = item.failed_job_names
            explanations = ()
        output.append(
            replace(
                item,
                status=status,
                pipeline_id=pipeline_id,
                pipeline_sha=pipeline_sha,
                result_pipeline_id=public_result_pipeline_id,
                result_pipeline_sha=public_result_pipeline_sha,
                status_markdown=status_markdown,
                failed_job_names=failed_job_names,
                failure_explanations=explanations,
            )
        )
    return tuple(output)


def reconcile_repair_items(
    previous_items: Iterable[RepairItem],
    target_category: RepairCategory | str,
    failed_categories: Iterable[RepairCategory | str],
    pipeline_id: int,
    pipeline_sha: str,
    target_error: str = "",
) -> tuple[RepairItem, ...]:
    target = RepairCategory(target_category)
    remaining = {RepairCategory(category) for category in failed_categories}
    by_category = {item.category: item for item in previous_items}
    categories = set(by_category) | remaining
    output = []
    for category in _CATEGORY_ORDER:
        if category not in categories:
            continue
        item = by_category.get(category)
        if item is None:
            item = repair_items_for_categories([category], pipeline_id, pipeline_sha)[0]
        if category in remaining:
            status = RepairItemStatus.FAILED if category is target else RepairItemStatus.PENDING
        else:
            status = RepairItemStatus.SUCCEEDED if category is target else RepairItemStatus.RESOLVED
        output.append(
            replace(
                item,
                status=status,
                result_pipeline_id=pipeline_id,
                result_pipeline_sha=pipeline_sha,
                status_markdown=target_error if category is target else "",
            )
        )
    return tuple(output)
