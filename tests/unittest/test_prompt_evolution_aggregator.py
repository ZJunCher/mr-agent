import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from pr_agent.suggestions.prompt_evolution.aggregator import (
    AggregationThresholds,
    score_cluster,
    select_eligible_candidates,
)
from pr_agent.suggestions.prompt_evolution.models import Evidence, Outcome

NOW = datetime(2026, 8, 14, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


def _evidence(outcome: Outcome, *, project: str = "eabot/cook", mr_iid: str = "1") -> Evidence:
    weight = 0.25 if outcome is Outcome.UNHANDLED else (0.0 if outcome in {Outcome.PENDING, Outcome.INVALID} else 1.0)
    return Evidence(
        suggestion_id=f"{project}:{mr_iid}:{outcome.value}:{uuid.uuid4().hex}",
        project=project,
        mr_iid=mr_iid,
        mr_url=f"https://gitlab.example/{project}/-/merge_requests/{mr_iid}",
        created_at=NOW.isoformat(),
        file_path="src/a.py",
        label="bug",
        summary="Avoid speculative change",
        suggestion_content="Replace the branch without evidence",
        outcome=outcome,
        weight=weight,
        global_prompt_set_hash="global-v1",
        prompt_bundle_hash=f"bundle:{project}:v1",
    )


def _cluster(key: str, evidence: tuple[Evidence, ...]):
    return score_cluster(key, evidence)


def _thresholds() -> AggregationThresholds:
    return AggregationThresholds(
        project_min_negative_weight=3.0,
        project_min_negative_ratio=0.70,
        project_min_mrs=2,
        unhandled_only_min_count=12,
        unhandled_only_min_mrs=3,
        global_min_negative_weight=5.0,
        global_min_negative_ratio=0.70,
        global_min_projects=2,
        global_min_mrs=3,
    )


def test_twelve_unhandled_equal_three_negative_weight():
    cluster = score_cluster("noise", tuple(_evidence(Outcome.UNHANDLED) for _ in range(12)))
    assert cluster.negative_weight == 3.0
    assert cluster.negative_ratio == 1.0


def test_project_candidate_requires_three_mrs_when_only_unhandled():
    evidence = tuple(
        _evidence(Outcome.UNHANDLED, project="eabot/cook", mr_iid=str(index // 4))
        for index in range(12)
    )
    candidates = select_eligible_candidates((_cluster("noise", evidence),), _thresholds(), "global-v1")
    assert len(candidates) == 1
    assert candidates[0].scope.value == "project"


def test_global_candidate_requires_two_projects():
    evidence = tuple(
        [_evidence(Outcome.REJECTED, project="eabot/cook", mr_iid=str(i)) for i in range(3)]
        + [_evidence(Outcome.REJECTED, project="eabot/chogori", mr_iid=str(i)) for i in range(3)]
    )
    candidates = select_eligible_candidates((_cluster("shared-noise", evidence),), _thresholds(), "global-v1")
    assert len(candidates) == 1
    assert candidates[0].scope.value == "global"


def test_threshold_failures_do_not_emit_candidates():
    one_mr = tuple(_evidence(Outcome.REJECTED, mr_iid="1") for _ in range(3))
    eleven_unhandled = tuple(_evidence(Outcome.UNHANDLED, mr_iid=str(index // 4)) for index in range(11))
    low_ratio = tuple(
        [_evidence(Outcome.REJECTED, mr_iid=str(index)) for index in range(3)]
        + [_evidence(Outcome.ACCEPTED, mr_iid=str(index + 10)) for index in range(2)]
    )
    assert select_eligible_candidates((_cluster("one-mr", one_mr),), _thresholds(), "global-v1") == ()
    assert select_eligible_candidates((_cluster("eleven", eleven_unhandled),), _thresholds(), "global-v1") == ()
    assert select_eligible_candidates((_cluster("low-ratio", low_ratio),), _thresholds(), "global-v1") == ()
