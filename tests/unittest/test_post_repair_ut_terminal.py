from pr_agent.distributed.models import PostRepairUTStatus
from pr_agent.triage.post_repair_ut_terminal import classify_post_repair_ut_result


def _result(status="success", coverage=85.0, failed_jobs=None):
    return {
        "result": {
            "final_pipeline_status": status,
            "final_coverage": coverage,
            "pushed_sha": "a" * 40,
            "pipeline_groups": [{
                "validation_pipeline_id": 42,
                "requested_commit_sha": "a" * 40,
                "status": status,
                "failed_jobs": failed_jobs or [],
            }],
        }
    }


def test_classifies_full_success():
    outcome = classify_post_repair_ut_result(_result(), threshold=80)
    assert outcome.status is PostRepairUTStatus.SUCCEEDED
    assert outcome.keeps_commits is True


def test_classifies_green_pipeline_below_threshold_as_partial():
    outcome = classify_post_repair_ut_result(_result(coverage=72.5), threshold=80)
    assert outcome.status is PostRepairUTStatus.PARTIAL
    assert outcome.keeps_commits is True


def test_classifies_missing_coverage_as_unverified():
    outcome = classify_post_repair_ut_result(_result(coverage=None), threshold=80)
    assert outcome.status is PostRepairUTStatus.UNVERIFIED
    assert outcome.keeps_commits is True


def test_classifies_failed_pipeline_as_failed_even_with_coverage():
    outcome = classify_post_repair_ut_result(_result(status="failed", coverage=90), threshold=80)
    assert outcome.status is PostRepairUTStatus.FAILED
    assert outcome.keeps_commits is False
