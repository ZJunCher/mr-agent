"""Terminal policy for Feishu-triggered unit-test supplementation."""

from dataclasses import dataclass
from typing import Any

from pr_agent.distributed.models import PostRepairUTStatus
from pr_agent.triage.post_repair_ut import post_repair_ut_coverage_threshold


@dataclass(frozen=True)
class PostRepairUTOutcome:
    status: PostRepairUTStatus
    pipeline_id: int = 0
    commit_sha: str = ""
    coverage: float | None = None
    reason: str = ""

    @property
    def keeps_commits(self) -> bool:
        return self.status in {
            PostRepairUTStatus.SUCCEEDED,
            PostRepairUTStatus.PARTIAL,
            PostRepairUTStatus.UNVERIFIED,
        }


def classify_post_repair_ut_result(
    execution_result: dict[str, Any] | None,
    *,
    threshold: float | None = None,
) -> PostRepairUTOutcome:
    """Classify only the UT Agent's structured, exact-SHA validation result."""
    payload = execution_result if isinstance(execution_result, dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    groups = result.get("pipeline_groups") if isinstance(result.get("pipeline_groups"), list) else []
    final_group = groups[-1] if groups and isinstance(groups[-1], dict) else {}
    pipeline_id = int(final_group.get("validation_pipeline_id") or final_group.get("root_pipeline_id") or 0)
    commit_sha = str(result.get("pushed_sha") or final_group.get("requested_commit_sha") or "")
    status = str(result.get("final_pipeline_status") or final_group.get("status") or "unknown").lower()
    failed_jobs = final_group.get("failed_jobs") if isinstance(final_group.get("failed_jobs"), list) else []
    coverage = _coverage(result.get("final_coverage"))
    if status != "success" or failed_jobs:
        reason = str(result.get("error") or result.get("finish_reason") or "验证流水线未通过")
        return PostRepairUTOutcome(PostRepairUTStatus.FAILED, pipeline_id, commit_sha, coverage, reason)
    if coverage is None:
        return PostRepairUTOutcome(
            PostRepairUTStatus.UNVERIFIED,
            pipeline_id,
            commit_sha,
            None,
            "验证流水线已通过，但未取得可靠的单元测试覆盖率",
        )
    target = post_repair_ut_coverage_threshold() if threshold is None else threshold
    if coverage >= target:
        return PostRepairUTOutcome(
            PostRepairUTStatus.SUCCEEDED,
            pipeline_id,
            commit_sha,
            coverage,
            f"验证流水线已通过，单元测试覆盖率达到 {coverage:.2f}%",
        )
    return PostRepairUTOutcome(
        PostRepairUTStatus.PARTIAL,
        pipeline_id,
        commit_sha,
        coverage,
        f"测试代码有效且流水线已通过，单元测试覆盖率为 {coverage:.2f}%，尚未达到 {target:.2f}%",
    )


def _coverage(value: object) -> float | None:
    try:
        coverage = float(value)
    except (TypeError, ValueError):
        return None
    return coverage if 0 <= coverage <= 100 else None
