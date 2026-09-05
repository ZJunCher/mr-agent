import json
from types import SimpleNamespace

from ut_agent.repair_plan import RepairPlan, RepairVerification, RepairWorkItem
from ut_agent.pipeline_reconciliation import (
    native_exhausted_root_ids,
    native_failed_validation_counts,
    observed_jobs_from_group_jobs,
    reconcile_pipeline_failures,
)


BASE_SHA = "a" * 40
FIRST_SHA = "b" * 40
SECOND_SHA = "c" * 40
FIRST_DIFF = "sha256:" + "1" * 64
SECOND_DIFF = "sha256:" + "2" * 64


def _pipeline(
    pipeline_id: int,
    sha: str,
    roots: dict[str, tuple[str, ...]],
    statuses: dict[str, str],
) -> dict:
    return {
        "status": "success",
        "pipeline_status": "failed",
        "pipeline_id": pipeline_id,
        "requested_commit_sha": sha,
        "matched_commit_sha": sha,
        "root_cause_groups": [
            {
                "root_cause_id": root_id,
                "canonical_diagnostic": f"{root_id} failed",
                "job_names": list(job_names),
            }
            for root_id, job_names in roots.items()
        ],
        "observed_jobs": [
            {
                "pipeline_id": pipeline_id,
                "job_id": index,
                "name": name,
                "status": status,
            }
            for index, (name, status) in enumerate(statuses.items(), start=1)
        ],
    }


def _transition_statuses(result: dict) -> dict[str, str]:
    return {
        transition["root_cause_id"]: transition["status"]
        for transition in result["transitions"]
    }


def _exchange(name: str, call_id: str, result: dict) -> list[dict]:
    return [
        {"tool_calls": [{"name": name, "args": {}, "id": call_id}]},
        {"tool_call_id": call_id, "content": json.dumps(result)},
    ]


def _plan(base_sha: str, root_ids: tuple[str, ...], marker: str, pipeline_id: int) -> RepairPlan:
    work_items = tuple(RepairWorkItem(
        work_item_id=root_id,
        job_names=(f"build-{root_id.lower()}",),
        kind="build",
        required_tool="apply_repo_patch_tool",
        failure_signature=root_id,
        failure_evidence=(f"{root_id} failed",),
    ) for root_id in root_ids)
    return RepairPlan(
        plan_id=marker * 64,
        lineage_id=marker.upper() * 64,
        version=1,
        project_id="group/repo",
        mr_id=1,
        baseline_sha=base_sha,
        source_pipeline_id=pipeline_id,
        source_commit_sha=base_sha,
        source_failure_digest=marker * 64,
        evidence_cursor=0,
        created_at="2026-09-02T00:00:00+00:00",
        revision_reason="test",
        planning_mode="deterministic_fallback",
        work_items=work_items,
    )


def _verification(plan: RepairPlan, diff_digest: str, covered_ids: tuple[str, ...]) -> dict:
    return RepairVerification(
        plan_id=plan.plan_id,
        lineage_id=plan.lineage_id,
        plan_version=plan.version,
        work_item_id=covered_ids[0],
        baseline_sha=plan.baseline_sha,
        diff_digest=diff_digest,
        verdict="pass",
        causal_alignment=True,
        scope_compliant=True,
        evidence_sufficient=True,
        covered_work_item_ids=covered_ids,
        reason="exact Diff passed independent verification",
        created_at="2026-09-02T00:00:00+00:00",
    ).model_dump(mode="json")


def _push(attempt_id: str, sequence: int, base_sha: str, diff_digest: str, commit_sha: str) -> dict:
    return {
        "status": "success",
        "changed": True,
        "attempt_id": attempt_id,
        "attempt_sequence": sequence,
        "base_sha": base_sha,
        "diff_digest": diff_digest,
        "commit_sha": commit_sha,
    }


def test_reconcile_resolved_persistent_and_introduced_roots():
    previous = _pipeline(
        10,
        BASE_SHA,
        {"A": ("build-a",), "B": ("build-b",)},
        {"build-a": "failed", "build-b": "failed"},
    )
    current = _pipeline(
        11,
        FIRST_SHA,
        {"B": ("build-b",), "C": ("test-c",)},
        {"build-a": "success", "build-b": "failed", "test-c": "failed"},
    )

    result = reconcile_pipeline_failures(previous, current)

    assert _transition_statuses(result) == {
        "A": "resolved",
        "B": "persistent",
        "C": "introduced",
    }
    assert result["previous_pipeline_id"] == 10
    assert result["previous_matched_commit_sha"] == BASE_SHA
    assert result["current_pipeline_id"] == 11
    assert result["current_matched_commit_sha"] == FIRST_SHA


def test_missing_root_is_unknown_when_its_job_did_not_run_successfully():
    previous = _pipeline(10, BASE_SHA, {"A": ("build",)}, {"build": "failed"})
    current = _pipeline(11, FIRST_SHA, {}, {"build": "skipped"})

    assert _transition_statuses(reconcile_pipeline_failures(previous, current)) == {"A": "unknown"}


def test_missing_root_is_superseded_when_same_job_failed_with_a_new_root():
    previous = _pipeline(10, BASE_SHA, {"A": ("build",)}, {"build": "failed"})
    current = _pipeline(11, FIRST_SHA, {"C": ("build",)}, {"build": "failed"})

    assert _transition_statuses(reconcile_pipeline_failures(previous, current)) == {
        "A": "superseded",
        "C": "introduced",
    }


def test_all_previous_roots_are_resolved_only_when_their_jobs_succeed():
    previous = _pipeline(
        10,
        BASE_SHA,
        {"A": ("build-a",), "B": ("test-b",)},
        {"build-a": "failed", "test-b": "failed"},
    )
    current = _pipeline(
        11,
        FIRST_SHA,
        {},
        {"build-a": "success", "test-b": "success"},
    )

    assert _transition_statuses(reconcile_pipeline_failures(previous, current)) == {
        "A": "resolved",
        "B": "resolved",
    }


def test_reconciliation_reports_when_root_transitions_are_truncated():
    roots = {f"root-{index:02d}": (f"job-{index:02d}",) for index in range(25)}
    statuses = {f"job-{index:02d}": "failed" for index in range(25)}

    result = reconcile_pipeline_failures(None, _pipeline(10, BASE_SHA, roots, statuses))

    assert len(result["transitions"]) == 20
    assert result["transitions_truncated"] is True


def test_missing_root_with_truncated_job_names_is_unknown_not_resolved():
    previous = _pipeline(
        10,
        BASE_SHA,
        {"A": tuple(f"job-{index:02d}" for index in range(21))},
        {f"job-{index:02d}": "failed" for index in range(21)},
    )
    current = _pipeline(
        11,
        FIRST_SHA,
        {},
        {
            **{f"job-{index:02d}": "success" for index in range(20)},
            "job-20": "skipped",
        },
    )

    result = reconcile_pipeline_failures(previous, current)

    assert _transition_statuses(result) == {"A": "unknown"}
    assert result["transitions_truncated"] is True


def test_observed_jobs_are_normalized_sorted_and_bounded():
    jobs = [
        (3, SimpleNamespace(id=index, name=f"job-{25 - index:02d}", status="success"))
        for index in range(25)
    ]

    result = observed_jobs_from_group_jobs(reversed(jobs))

    assert len(result) == 25
    assert result == sorted(
        result,
        key=lambda item: (item["pipeline_id"], item["name"], item["job_id"]),
    )
    assert set(result[0]) == {"pipeline_id", "job_id", "name", "status"}


def test_observed_jobs_have_a_hard_upper_bound():
    jobs = [
        (3, SimpleNamespace(id=index, name=f"job-{index:03d}", status="success"))
        for index in range(125)
    ]

    assert len(observed_jobs_from_group_jobs(jobs)) == 100


def test_native_failed_validation_counts_only_verified_covered_persistent_roots():
    plan = _plan(BASE_SHA, ("A", "B"), "1", 10)
    first_pipeline = _pipeline(
        20,
        FIRST_SHA,
        {"A": ("build-a",), "B": ("build-b",)},
        {"build-a": "failed", "build-b": "failed"},
    )
    state = {
        "messages": [
            *_exchange(
                "fetch_pipeline_logs_tool",
                "source",
                _pipeline(10, BASE_SHA, {"A": ("build-a",), "B": ("build-b",)}, {
                    "build-a": "failed", "build-b": "failed",
                }),
            ),
            *_exchange("commit_and_push_tool", "push-1", _push("attempt-1", 1, BASE_SHA, FIRST_DIFF, FIRST_SHA)),
            *_exchange("wait_pipeline_tool", "pipeline-1", first_pipeline),
        ],
        "repair_plans": [plan.model_dump(mode="json")],
        "repair_verifications": [_verification(plan, FIRST_DIFF, ("A",))],
    }

    assert native_failed_validation_counts(state) == {"A": 1}
    assert native_exhausted_root_ids(state, 2) == frozenset()


def test_native_failed_validation_counts_reaches_limit_per_root_without_charging_siblings():
    first_plan = _plan(BASE_SHA, ("A", "B"), "1", 10)
    second_plan = _plan(FIRST_SHA, ("A", "B"), "2", 20)
    first_pipeline = _pipeline(
        20,
        FIRST_SHA,
        {"A": ("build-a",), "B": ("build-b",)},
        {"build-a": "failed", "build-b": "failed"},
    )
    second_pipeline = _pipeline(
        21,
        SECOND_SHA,
        {"A": ("build-a",), "B": ("build-b",)},
        {"build-a": "failed", "build-b": "failed"},
    )
    state = {
        "messages": [
            *_exchange(
                "fetch_pipeline_logs_tool",
                "source",
                _pipeline(10, BASE_SHA, {"A": ("build-a",), "B": ("build-b",)}, {
                    "build-a": "failed", "build-b": "failed",
                }),
            ),
            *_exchange("commit_and_push_tool", "push-1", _push("attempt-1", 1, BASE_SHA, FIRST_DIFF, FIRST_SHA)),
            *_exchange("wait_pipeline_tool", "pipeline-1", first_pipeline),
            *_exchange("commit_and_push_tool", "push-2", _push("attempt-2", 2, FIRST_SHA, SECOND_DIFF, SECOND_SHA)),
            *_exchange("wait_pipeline_tool", "pipeline-2", second_pipeline),
        ],
        "repair_plans": [first_plan.model_dump(mode="json"), second_plan.model_dump(mode="json")],
        "repair_verifications": [
            _verification(first_plan, FIRST_DIFF, ("A",)),
            _verification(second_plan, SECOND_DIFF, ("A",)),
        ],
    }

    assert native_failed_validation_counts(state) == {"A": 2}
    assert native_exhausted_root_ids(state, 2) == frozenset({"A"})


def test_native_attribution_requires_passing_verifier_and_exact_later_pipeline():
    plan = _plan(BASE_SHA, ("A",), "1", 10)
    wrong_sha_pipeline = _pipeline(20, SECOND_SHA, {"A": ("build",)}, {"build": "failed"})
    state = {
        "messages": [
            *_exchange(
                "fetch_pipeline_logs_tool",
                "source",
                _pipeline(10, BASE_SHA, {"A": ("build",)}, {"build": "failed"}),
            ),
            *_exchange("commit_and_push_tool", "push", _push("attempt", 1, BASE_SHA, FIRST_DIFF, FIRST_SHA)),
            *_exchange("wait_pipeline_tool", "pipeline", wrong_sha_pipeline),
        ],
        "repair_plans": [plan.model_dump(mode="json")],
        "repair_verifications": [
            {**_verification(plan, FIRST_DIFF, ("A",)), "verdict": "replan"},
        ],
    }

    assert native_failed_validation_counts(state) == {}


def test_native_attribution_deduplicates_replayed_push_and_pipeline_results():
    plan = _plan(BASE_SHA, ("A",), "1", 10)
    push = _push("attempt-1", 1, BASE_SHA, FIRST_DIFF, FIRST_SHA)
    pipeline = _pipeline(20, FIRST_SHA, {"A": ("build",)}, {"build": "failed"})
    state = {
        "messages": [
            *_exchange(
                "fetch_pipeline_logs_tool",
                "source",
                _pipeline(10, BASE_SHA, {"A": ("build",)}, {"build": "failed"}),
            ),
            *_exchange("commit_and_push_tool", "push-1", push),
            *_exchange("commit_and_push_tool", "push-replay", push),
            *_exchange("wait_pipeline_tool", "pipeline-1", pipeline),
            *_exchange("fetch_pipeline_logs_tool", "pipeline-replay", pipeline),
        ],
        "repair_plans": [plan.model_dump(mode="json")],
        "repair_verifications": [_verification(plan, FIRST_DIFF, ("A",))],
    }

    assert native_failed_validation_counts(state) == {"A": 1}


def test_native_failed_validation_streak_resets_when_root_disappears_then_reappears():
    middle_sha = "d" * 40
    third_sha = "e" * 40
    fourth_sha = "f" * 40
    third_diff = "sha256:" + "3" * 64
    fourth_diff = "sha256:" + "4" * 64
    first_plan = _plan(BASE_SHA, ("A",), "1", 10)
    second_plan = _plan(FIRST_SHA, ("A",), "2", 20)
    third_plan = _plan(middle_sha, ("B",), "3", 21)
    fourth_plan = _plan(third_sha, ("A",), "4", 22)
    state = {
        "messages": [
            *_exchange(
                "fetch_pipeline_logs_tool",
                "source",
                _pipeline(10, BASE_SHA, {"A": ("build-a",)}, {"build-a": "failed"}),
            ),
            *_exchange("commit_and_push_tool", "push-1", _push("attempt-1", 1, BASE_SHA, FIRST_DIFF, FIRST_SHA)),
            *_exchange(
                "wait_pipeline_tool",
                "pipeline-1",
                _pipeline(20, FIRST_SHA, {"A": ("build-a",)}, {"build-a": "failed"}),
            ),
            *_exchange("commit_and_push_tool", "push-2", _push("attempt-2", 2, FIRST_SHA, SECOND_DIFF, middle_sha)),
            *_exchange(
                "wait_pipeline_tool",
                "pipeline-2",
                _pipeline(21, middle_sha, {"B": ("build-b",)}, {"build-a": "success", "build-b": "failed"}),
            ),
            *_exchange("commit_and_push_tool", "push-3", _push("attempt-3", 3, middle_sha, third_diff, third_sha)),
            *_exchange(
                "wait_pipeline_tool",
                "pipeline-3",
                _pipeline(22, third_sha, {"A": ("build-a",)}, {"build-a": "failed", "build-b": "success"}),
            ),
            *_exchange("commit_and_push_tool", "push-4", _push("attempt-4", 4, third_sha, fourth_diff, fourth_sha)),
            *_exchange(
                "wait_pipeline_tool",
                "pipeline-4",
                _pipeline(23, fourth_sha, {"A": ("build-a",)}, {"build-a": "failed"}),
            ),
        ],
        "repair_plans": [
            first_plan.model_dump(mode="json"),
            second_plan.model_dump(mode="json"),
            third_plan.model_dump(mode="json"),
            fourth_plan.model_dump(mode="json"),
        ],
        "repair_verifications": [
            _verification(first_plan, FIRST_DIFF, ("A",)),
            _verification(second_plan, SECOND_DIFF, ("A",)),
            _verification(third_plan, third_diff, ("B",)),
            _verification(fourth_plan, fourth_diff, ("A",)),
        ],
    }

    assert native_failed_validation_counts(state) == {"A": 1}
    assert native_exhausted_root_ids(state, 2) == frozenset()


def test_unknown_absence_does_not_erase_an_existing_failure_streak():
    middle_sha = "d" * 40
    third_sha = "e" * 40
    third_diff = "sha256:" + "3" * 64
    first_plan = _plan(BASE_SHA, ("A",), "1", 10)
    second_plan = _plan(FIRST_SHA, ("A",), "2", 20)
    third_plan = _plan(middle_sha, ("B",), "3", 21)
    state = {
        "messages": [
            *_exchange(
                "fetch_pipeline_logs_tool",
                "source",
                _pipeline(10, BASE_SHA, {"A": ("build-a",)}, {"build-a": "failed"}),
            ),
            *_exchange("commit_and_push_tool", "push-1", _push("attempt-1", 1, BASE_SHA, FIRST_DIFF, FIRST_SHA)),
            *_exchange(
                "wait_pipeline_tool",
                "pipeline-1",
                _pipeline(20, FIRST_SHA, {"A": ("build-a",)}, {"build-a": "failed"}),
            ),
            *_exchange("commit_and_push_tool", "push-2", _push("attempt-2", 2, FIRST_SHA, SECOND_DIFF, middle_sha)),
            *_exchange(
                "wait_pipeline_tool",
                "pipeline-2",
                _pipeline(21, middle_sha, {"B": ("build-b",)}, {"build-a": "skipped", "build-b": "failed"}),
            ),
            *_exchange("commit_and_push_tool", "push-3", _push("attempt-3", 3, middle_sha, third_diff, third_sha)),
            *_exchange(
                "wait_pipeline_tool",
                "pipeline-3",
                _pipeline(22, third_sha, {"A": ("build-a",)}, {"build-a": "failed", "build-b": "success"}),
            ),
        ],
        "repair_plans": [
            first_plan.model_dump(mode="json"),
            second_plan.model_dump(mode="json"),
            third_plan.model_dump(mode="json"),
        ],
        "repair_verifications": [
            _verification(first_plan, FIRST_DIFF, ("A",)),
            _verification(second_plan, SECOND_DIFF, ("A",)),
            _verification(third_plan, third_diff, ("B",)),
        ],
    }

    assert native_failed_validation_counts(state) == {"A": 1}


def test_later_explicit_success_clears_streak_after_an_unknown_absence():
    third_sha = "d" * 40
    fourth_sha = "e" * 40
    fifth_sha = "f" * 40
    third_diff = "sha256:" + "3" * 64
    fourth_diff = "sha256:" + "4" * 64
    fifth_diff = "sha256:" + "5" * 64
    first_plan = _plan(BASE_SHA, ("A",), "1", 10)
    second_plan = _plan(FIRST_SHA, ("A",), "2", 20)
    third_plan = _plan(SECOND_SHA, ("B",), "3", 21)
    fourth_plan = _plan(third_sha, ("B",), "4", 22)
    fifth_plan = _plan(fourth_sha, ("A",), "5", 23)
    steps = [
        (first_plan, "A", BASE_SHA, FIRST_DIFF, FIRST_SHA, {"A": ("job-a",)}, {"job-a": "failed"}),
        (
            second_plan,
            "A",
            FIRST_SHA,
            SECOND_DIFF,
            SECOND_SHA,
            {"B": ("job-b",)},
            {"job-a": "skipped", "job-b": "failed"},
        ),
        (
            third_plan,
            "B",
            SECOND_SHA,
            third_diff,
            third_sha,
            {"B": ("job-b",)},
            {"job-a": "success", "job-b": "failed"},
        ),
        (
            fourth_plan,
            "B",
            third_sha,
            fourth_diff,
            fourth_sha,
            {"A": ("job-a",)},
            {"job-a": "failed", "job-b": "success"},
        ),
        (fifth_plan, "A", fourth_sha, fifth_diff, fifth_sha, {"A": ("job-a",)}, {"job-a": "failed"}),
    ]
    messages = _exchange(
        "fetch_pipeline_logs_tool",
        "source",
        _pipeline(10, BASE_SHA, {"A": ("job-a",)}, {"job-a": "failed"}),
    )
    verifications = []
    for index, (plan, covered, base_sha, diff_digest, commit_sha, roots, statuses) in enumerate(steps, start=1):
        messages += _exchange(
            "commit_and_push_tool",
            f"push-{index}",
            _push(f"attempt-{index}", index, base_sha, diff_digest, commit_sha),
        )
        messages += _exchange(
            "wait_pipeline_tool",
            f"pipeline-{index}",
            _pipeline(19 + index, commit_sha, roots, statuses),
        )
        verifications.append(_verification(plan, diff_digest, (covered,)))

    state = {
        "messages": messages,
        "repair_plans": [step[0].model_dump(mode="json") for step in steps],
        "repair_verifications": verifications,
    }

    assert native_failed_validation_counts(state) == {"A": 1}


def test_late_replay_of_an_old_sha_cannot_revive_a_cleared_streak():
    first_plan = _plan(BASE_SHA, ("A",), "1", 10)
    second_plan = _plan(FIRST_SHA, ("A",), "2", 20)
    first_failed = _pipeline(20, FIRST_SHA, {"A": ("job-a",)}, {"job-a": "failed"})
    second_result = _pipeline(
        21,
        SECOND_SHA,
        {"B": ("job-b",)},
        {"job-a": "success", "job-b": "failed"},
    )
    messages = [
        *_exchange(
            "fetch_pipeline_logs_tool",
            "source",
            _pipeline(10, BASE_SHA, {"A": ("job-a",)}, {"job-a": "failed"}),
        ),
        *_exchange(
            "commit_and_push_tool",
            "push-1",
            _push("attempt-1", 1, BASE_SHA, FIRST_DIFF, FIRST_SHA),
        ),
        *_exchange("wait_pipeline_tool", "pipeline-1", first_failed),
        *_exchange(
            "commit_and_push_tool",
            "push-2",
            _push("attempt-2", 2, FIRST_SHA, SECOND_DIFF, SECOND_SHA),
        ),
        *_exchange("wait_pipeline_tool", "pipeline-2", second_result),
        *_exchange("wait_pipeline_tool", "late-replay", first_failed),
    ]
    state = {
        "messages": messages,
        "repair_plans": [first_plan.model_dump(mode="json"), second_plan.model_dump(mode="json")],
        "repair_verifications": [
            _verification(first_plan, FIRST_DIFF, ("A",)),
            _verification(second_plan, SECOND_DIFF, ("A",)),
        ],
    }

    assert native_failed_validation_counts(state) == {}


def test_same_pipeline_retry_supersedes_an_earlier_terminal_observation():
    third_sha = "d" * 40
    third_diff = "sha256:" + "3" * 64
    first_plan = _plan(BASE_SHA, ("A",), "1", 10)
    second_plan = _plan(FIRST_SHA, ("B",), "2", 20)
    third_plan = _plan(SECOND_SHA, ("A",), "3", 30)
    initial_retry_result = _pipeline(
        20,
        FIRST_SHA,
        {"A": ("job-a",), "B": ("job-b",)},
        {"job-a": "failed", "job-b": "failed"},
    )
    completed_retry_result = _pipeline(
        20,
        FIRST_SHA,
        {"B": ("job-b",)},
        {"job-a": "success", "job-b": "failed"},
    )
    later_failure = _pipeline(
        30,
        SECOND_SHA,
        {"A": ("job-a",)},
        {"job-a": "failed", "job-b": "success"},
    )
    repeated_failure = _pipeline(40, third_sha, {"A": ("job-a",)}, {"job-a": "failed"})
    messages = [
        *_exchange(
            "fetch_pipeline_logs_tool",
            "source",
            _pipeline(10, BASE_SHA, {"A": ("job-a",)}, {"job-a": "failed"}),
        ),
        *_exchange(
            "commit_and_push_tool",
            "push-1",
            _push("attempt-1", 1, BASE_SHA, FIRST_DIFF, FIRST_SHA),
        ),
        *_exchange("wait_pipeline_tool", "pipeline-1-initial", initial_retry_result),
        *_exchange("wait_pipeline_tool", "pipeline-1-retry", completed_retry_result),
        *_exchange(
            "commit_and_push_tool",
            "push-2",
            _push("attempt-2", 2, FIRST_SHA, SECOND_DIFF, SECOND_SHA),
        ),
        *_exchange("wait_pipeline_tool", "pipeline-2", later_failure),
        *_exchange(
            "commit_and_push_tool",
            "push-3",
            _push("attempt-3", 3, SECOND_SHA, third_diff, third_sha),
        ),
        *_exchange("wait_pipeline_tool", "pipeline-3", repeated_failure),
    ]
    state = {
        "messages": messages,
        "repair_plans": [
            first_plan.model_dump(mode="json"),
            second_plan.model_dump(mode="json"),
            third_plan.model_dump(mode="json"),
        ],
        "repair_verifications": [
            _verification(first_plan, FIRST_DIFF, ("A",)),
            _verification(second_plan, SECOND_DIFF, ("B",)),
            _verification(third_plan, third_diff, ("A",)),
        ],
    }

    assert native_failed_validation_counts(state) == {"A": 1}


def test_streak_clears_even_when_public_union_and_job_list_exceed_twenty_items():
    old_roots = {
        **{f"a-old-{index:02d}": (f"old-job-{index:02d}",) for index in range(19)},
        "zz-root": ("zz-job",),
    }
    new_roots = {f"m-new-{index:02d}": (f"new-job-{index:02d}",) for index in range(20)}
    first_plan = _plan(BASE_SHA, ("zz-root",), "1", 10)
    second_plan = _plan(FIRST_SHA, tuple(old_roots), "2", 20)
    first_statuses = {name: "failed" for names in old_roots.values() for name in names}
    second_statuses = {
        **{name: "failed" for names in new_roots.values() for name in names},
        "zz-job": "success",
    }
    state = {
        "messages": [
            *_exchange(
                "fetch_pipeline_logs_tool",
                "source",
                _pipeline(10, BASE_SHA, {"zz-root": ("zz-job",)}, {"zz-job": "failed"}),
            ),
            *_exchange("commit_and_push_tool", "push-1", _push("attempt-1", 1, BASE_SHA, FIRST_DIFF, FIRST_SHA)),
            *_exchange(
                "wait_pipeline_tool",
                "pipeline-1",
                _pipeline(20, FIRST_SHA, old_roots, first_statuses),
            ),
            *_exchange("commit_and_push_tool", "push-2", _push("attempt-2", 2, FIRST_SHA, SECOND_DIFF, SECOND_SHA)),
            *_exchange(
                "wait_pipeline_tool",
                "pipeline-2",
                _pipeline(21, SECOND_SHA, new_roots, second_statuses),
            ),
        ],
        "repair_plans": [
            first_plan.model_dump(mode="json"),
            second_plan.model_dump(mode="json"),
        ],
        "repair_verifications": [
            _verification(first_plan, FIRST_DIFF, ("zz-root",)),
            _verification(second_plan, SECOND_DIFF, ("zz-root",)),
        ],
    }

    public = reconcile_pipeline_failures(
        _pipeline(20, FIRST_SHA, old_roots, first_statuses),
        _pipeline(21, SECOND_SHA, new_roots, second_statuses),
    )
    assert len(public["transitions"]) == 20
    assert any(item["root_cause_id"] == "zz-root" for item in public["transitions"])
    assert native_failed_validation_counts(state) == {}


def test_native_attribution_rejects_malformed_unbound_verification():
    pipeline = _pipeline(20, FIRST_SHA, {"A": ("build",)}, {"build": "failed"})
    state = {
        "messages": [
            *_exchange(
                "fetch_pipeline_logs_tool",
                "source",
                _pipeline(10, BASE_SHA, {"A": ("build",)}, {"build": "failed"}),
            ),
            *_exchange("commit_and_push_tool", "push", _push("attempt", 1, BASE_SHA, FIRST_DIFF, FIRST_SHA)),
            *_exchange("wait_pipeline_tool", "pipeline", pipeline),
        ],
        "repair_plans": [],
        "repair_verifications": [{
            "baseline_sha": BASE_SHA,
            "diff_digest": FIRST_DIFF,
            "verdict": "pass",
            "causal_alignment": True,
            "scope_compliant": True,
            "evidence_sufficient": True,
            "covered_work_item_ids": ["A"],
        }],
    }

    assert native_failed_validation_counts(state) == {}
