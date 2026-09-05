"""Pure policy for offering unit-test supplementation after a successful repair."""

from math import isfinite

from pr_agent.config_loader import get_settings
from pr_agent.distributed.models import (
    PostRepairUTStatus,
    RepairItemStatus,
    TriageCardBinding,
    TriageCardState,
)


def post_repair_ut_enabled() -> bool:
    value = get_settings().get("FEISHU.POST_REPAIR_UT_ENABLED", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def post_repair_ut_coverage_threshold() -> float:
    value = float(get_settings().get("FEISHU.POST_REPAIR_UT_COVERAGE_THRESHOLD", 80.0) or 80.0)
    if not isfinite(value) or value < 0 or value > 100:
        raise ValueError("feishu.post_repair_ut_coverage_threshold must be between 0 and 100")
    return value


def is_post_repair_ut_eligible(binding: TriageCardBinding, *, require_enabled: bool = True) -> bool:
    """Return whether a repaired card may start one independent UT task."""
    if require_enabled and not post_repair_ut_enabled():
        return False
    state = binding.post_repair_ut
    if binding.state is not TriageCardState.REPAIR_SUCCEEDED or binding.active_task_id:
        return False
    if not binding.task_id or not binding.current_pipeline_id or not binding.current_pipeline_sha:
        return False
    if state.status is not PostRepairUTStatus.IDLE:
        return False
    selected = [item for item in binding.repair_items if item.task_id == binding.task_id]
    if not selected:
        selected = list(binding.repair_items)
    completed_statuses = {RepairItemStatus.SUCCEEDED, RepairItemStatus.RESOLVED}
    if not selected or any(item.status not in completed_statuses for item in selected):
        return False
    coverage = state.coverage_before
    return coverage is None or coverage < post_repair_ut_coverage_threshold()
