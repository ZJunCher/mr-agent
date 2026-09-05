from dataclasses import replace

from pr_agent.distributed.models import (
    PostRepairUTState,
    PostRepairUTStatus,
    RepairCategory,
    RepairItemStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.triage.failure_categories import repair_items_for_categories
from pr_agent.triage.post_repair_ut import is_post_repair_ut_eligible


def _binding(coverage: float | None = None) -> TriageCardBinding:
    item = replace(
        repair_items_for_categories([RepairCategory.BUILD], 11, "a" * 40)[0],
        task_id="repair-1",
        status=RepairItemStatus.SUCCEEDED,
    )
    return replace(
        TriageCardBinding.new(
            card_id="card-1",
            task_id="repair-1",
            open_message_id="om-1",
            receive_id="ou-1",
            mr_url="https://gitlab/eabot/cook/-/merge_requests/1",
            project_id="eabot/cook",
            mr_iid=1,
            mr_title="test",
            source_branch="feature/test",
            pipeline_id=10,
            pipeline_sha="0" * 40,
            original_markdown="failed",
            repair_items=(item,),
        ),
        state=TriageCardState.REPAIR_SUCCEEDED,
        current_pipeline_id=11,
        current_pipeline_sha="a" * 40,
        post_repair_ut=PostRepairUTState(coverage_before=coverage),
    )


def test_eligible_after_success_with_missing_coverage():
    assert is_post_repair_ut_eligible(_binding(), require_enabled=False)


def test_eligible_after_success_with_low_coverage():
    assert is_post_repair_ut_eligible(_binding(79.99), require_enabled=False)


def test_ineligible_when_known_coverage_meets_threshold():
    assert not is_post_repair_ut_eligible(_binding(80.0), require_enabled=False)


def test_ineligible_before_terminal_success():
    assert not is_post_repair_ut_eligible(
        replace(_binding(), state=TriageCardState.REPAIR_FAILED), require_enabled=False
    )


def test_ineligible_after_ut_has_started():
    binding = replace(
        _binding(),
        post_repair_ut=PostRepairUTState(status=PostRepairUTStatus.QUEUED, task_id="ut-1"),
    )
    assert not is_post_repair_ut_eligible(binding, require_enabled=False)
