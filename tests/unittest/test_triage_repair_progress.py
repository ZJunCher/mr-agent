import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.config_loader  # noqa: F401 - initialize settings before ut_agent package
from ut_agent.ci_diagnostics import extract_diagnostic_candidates, primary_diagnostic
from ut_agent.execution_policy import ToolAttempt
from ut_agent.repair_progress import (
    build_root_cause_groups,
    build_root_cause_progress,
    diagnostic_fingerprint,
    evaluate_hermes_budget,
    extract_causal_lines,
    normalize_diagnostic,
    root_cause_id_for,
)


def _failed_job(name: str, diagnostic: str, job_id: int) -> dict:
    return {
        "job_id": job_id,
        "pipeline_id": 29921,
        "name": name,
        "status": "failed",
        "log_tail": diagnostic,
    }


def _generate_exchange(call_id: str, result: dict, *, root_cause_id: str = "root-1") -> list:
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "id": call_id,
                "name": "generate_code_tool",
                "args": {
                    "job_name": "build_release_arm64",
                    "operation": "repair",
                    "root_cause_id": root_cause_id,
                    "task_description": "repair",
                },
            }],
        ),
        ToolMessage(content=json.dumps(result), tool_call_id=call_id),
    ]


def _root_pipeline(sequence: int, sha: str, *root_ids: str) -> dict:
    return {
        "_sequence": sequence,
        "status": "success",
        "requested_commit_sha": sha,
        "matched_commit_sha": sha,
        "pipeline_id": 34000 + sequence,
        "pipeline_status": "failed" if root_ids else "success",
        "root_cause_groups": [
            {
                "root_cause_id": root_id,
                "canonical_job_name": "build_release_arm64",
                "job_names": ["build_release_arm64"],
            }
            for root_id in root_ids
        ],
    }


def _changed_repair(sequence: int, root_cause_id: str) -> ToolAttempt:
    return ToolAttempt(
        name="generate_code_tool",
        args={"operation": "repair_session", "root_cause_id": root_cause_id},
        result={"status": "changed", "root_cause_id": root_cause_id},
        sequence=sequence,
    )


def _successful_push(sequence: int, sha: str) -> ToolAttempt:
    return ToolAttempt(
        name="commit_and_push_tool",
        args={},
        result={"status": "success", "changed": True, "commit_sha": sha},
        sequence=sequence,
    )


def _valid_blocker(job_name: str = "build_release_arm64") -> dict:
    return {
        "schema_version": 1,
        "outcome": "blocked",
        "job_name": job_name,
        "blocker_type": "ci_environment",
        "root_cause": "CI image lacks the required compiler.",
        "ci_evidence": [{"job_name": job_name, "observation": "compiler executable is missing"}],
        "repository_evidence": [{
            "kind": "build_config",
            "locator": "CMakeLists.txt:1",
            "observation": "the project requires this compiler",
        }],
        "attempted_repairs": ["Checked repository-local alternatives."],
        "why_no_safe_repo_change": "Source changes cannot install the compiler.",
        "suggested_action": "Restore the compiler in the CI image.",
    }


def test_diagnostic_candidates_preserve_primary_failure_before_later_cancellation():
    trace = "\n".join((
        "starting dependency checkout",
        "fatal: declared external reference ref-absent was not found",
        "cleanup started",
        "remote: rpc error: code = Canceled desc = running upload-pack: user canceled the request",
        "ERROR: Job failed: exit code 1",
    ))

    result = extract_diagnostic_candidates(trace, identity_key="job:101", limit=12)

    assert [item.line_number for item in result.candidates] == [2, 4, 5]
    assert "ref-absent" in result.candidates[0].text
    assert "user canceled" in result.candidates[1].text
    assert result.total_matches == 3
    assert result.truncated is False
    assert len({item.candidate_id for item in result.candidates}) == 3


def test_primary_diagnostic_prefers_fatal_branch_error_over_fallback_and_transport_consequence():
    trace = "\n".join((
        "[Build] ci_deps file not found or download failed (HTTP 404), using default deps.yml",
        "fatal: Remote branch joint/e2e/da_mini/830 not found in upstream origin",
        "remote: rpc error: code = Canceled desc = running upload-pack: user canceled the request",
        "ERROR: Job failed: command terminated with exit code 1",
    ))

    result = extract_diagnostic_candidates(trace, identity_key="job:104")
    primary = primary_diagnostic(result.candidates)

    assert [item.line_number for item in result.candidates] == [1, 2, 3, 4]
    assert primary is not None
    assert primary.line_number == 2
    assert "Remote branch" in primary.text
    assert extract_causal_lines(trace)[0] == primary.text


def test_diagnostic_candidate_limit_retains_head_and_tail_in_original_order():
    trace = "\n".join(f"error: failure-{index}" for index in range(20))

    result = extract_diagnostic_candidates(trace, identity_key="job:102", limit=6)

    assert [item.line_number for item in result.candidates] == [1, 2, 3, 4, 19, 20]
    assert result.total_matches == 20
    assert result.truncated is True


def test_candidate_limit_covers_distinct_private_members_before_duplicate_context():
    def private_error(line: int, name: str) -> str:
        return (
            f"tests/handler_test.cpp:{line}:7: error: "
            f"'void Handler::{name}()' is private within this context"
        )

    abnormal = private_error(40, "AbnormalCollecterHandler")
    remote = private_error(81, "RemoteControlHandler")
    version = private_error(122, "HandlePadVersion")
    post = private_error(163, "PostTask")
    trace = "\n".join([
        *([abnormal] * 8),
        remote,
        *([abnormal] * 6),
        version,
        *([abnormal] * 6),
        post,
        *([abnormal] * 8),
    ])

    result = extract_diagnostic_candidates(trace, identity_key="job:111510", limit=12)

    identities = {item.diagnostic_identity for item in result.candidates}
    assert len(identities) == 4
    assert all(any(name in item.text for item in result.candidates) for name in (
        "AbnormalCollecterHandler",
        "RemoteControlHandler",
        "HandlePadVersion",
        "PostTask",
    ))
    assert result.identity_count == 4
    assert result.omitted_identity_count == 0


def test_strong_midlog_fatal_error_survives_candidate_sampling():
    """A fatal compile error buried mid-log must not be dropped by head+tail sampling."""
    noise_head = [f"pkg-{index}/1.0: Not found in local cache, looking in remotes..." for index in range(10)]
    real_error = "/builds/x/src/a.hpp:11:10: fatal error: eabot_msgs/msg/frame.hpp: No such file or directory"
    noise_tail = [f"'{name}': error: build step failed" for name in ("s1", "s2", "s3", "s4", "s5")] + [
        "ERROR: No files to upload",
        "ERROR: Job failed: command terminated with exit code 1",
    ]
    trace = "\n".join((*noise_head, real_error, *noise_tail))

    result = extract_diagnostic_candidates(trace, identity_key="job:105", limit=12)
    primary = primary_diagnostic(result.candidates)

    assert any("fatal error" in item.text for item in result.candidates)
    assert primary is not None
    assert "eabot_msgs/msg/frame.hpp" in primary.text


def test_causal_lines_are_compatibility_view_of_ordered_candidates():
    diagnostic = "\n".join((
        "fatal: declared external reference was not found",
        "remote: rpc error: code = Canceled desc = request canceled",
    ))

    assert extract_causal_lines(diagnostic) == [
        "fatal: declared external reference was not found",
        "remote: rpc error: code = Canceled desc = request canceled",
    ]


def test_diagnostic_candidate_text_remains_sanitized_and_bounded():
    trace = "error: token=secret-token-value " + ("x" * 1500)

    result = extract_diagnostic_candidates(trace, identity_key="job:103")

    assert "secret-token-value" not in result.candidates[0].text
    assert "token=[REDACTED]" in result.candidates[0].text
    assert len(result.candidates[0].text) <= 1000


def test_diagnostic_candidate_strips_timestamp_prefix_but_keeps_trace_line():
    result = extract_diagnostic_candidates(
        "2026-08-22T01:00:00.123456Z 01O src/main.cpp:4:3: error: invalid value"
    )

    assert result.candidates[0].text == "01O src/main.cpp:4:3: error: invalid value"
    assert result.candidates[0].line_number == 1


def test_same_compiler_error_across_build_and_coverage_is_one_group():
    groups = build_root_cause_groups([
        _failed_job(
            "build_release_arm64",
            "/build/arm/src/a.hpp:7:11: fatal error: missing.hpp: No such file or directory",
            1,
        ),
        _failed_job(
            "x86_64_ut_coverage_check",
            "/build/x86/src/a.hpp:91:3: fatal error: missing.hpp: No such file or directory",
            2,
        ),
    ])

    assert len(groups) == 1
    assert groups[0].job_names == ("build_release_arm64", "x86_64_ut_coverage_check")
    assert groups[0].canonical_job_name == "build_release_arm64"


def test_different_missing_headers_remain_separate_root_causes():
    groups = build_root_cause_groups([
        _failed_job("build_release_arm64", "fatal error: one.hpp: No such file", 1),
        _failed_job("x86_64_ut_coverage_check", "fatal error: two.hpp: No such file", 2),
    ])

    assert len(groups) == 2


def test_missing_member_names_remain_distinct_root_causes():
    first = root_cause_id_for(
        "build_release_arm64",
        "src/handler.cpp:42: error: Request has no member named ‘node_name’",
    )
    second = root_cause_id_for(
        "build_release_arm64",
        "src/handler.cpp:42: error: Request has no member named 'target'",
    )

    assert first != second


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-17T01:50:44Z",
        "2026-08-17T01:50:44.677496Z",
        "2026-08-17T09:50:44+08:00",
        "2026-08-17T095044+0800",
        "2026-08-17 01:50:44.123456",
        "01:50:44.999999",
    ],
)
def test_iso_timestamps_are_fully_removed_from_diagnostic_identity(timestamp):
    normalized = normalize_diagnostic(
        f"{timestamp} /builds/eabot/cook/src/a.cpp:142:23: error: no member named 'node_name'"
    )

    assert normalized == "<time> src/a.cpp: error: no member named 'node_name'"


def test_timestamp_path_and_location_changes_keep_same_root_cause():
    first = root_cause_id_for(
        "build_release_arm64",
        "2026-08-17T01:50:44.677496Z /builds/eabot/cook/src/a.cpp:142:23: "
        "error: no member named 'node_name'",
    )
    second = root_cause_id_for(
        "clang_tidy_check",
        "2026-08-18T10:20:31.123456+08:00 /runner/tmp/src/a.cpp:301:9: "
        "error: no member named 'node_name'",
    )

    assert first == second


def test_diagnostic_fingerprint_is_stable_and_distinguishes_member_names():
    first = diagnostic_fingerprint(
        "2026-08-17T01:50:44.677496Z /builds/eabot/cook/src/a.cpp:142:23: "
        "error: no member named 'node_name'",
        job_name="build_release_arm64",
    )
    second = diagnostic_fingerprint(
        "2026-08-18T10:20:31.123456+08:00 /runner/tmp/src/a.cpp:301:9: "
        "error: no member named 'node_name'",
        job_name="clang_tidy_check",
    )
    different = diagnostic_fingerprint(
        "2026-08-18T10:20:31.123456+08:00 /runner/tmp/src/a.cpp:301:9: "
        "error: no member named 'target'",
        job_name="clang_tidy_check",
    )

    assert len(first) == 32
    assert first == second
    assert first != different


def test_timestamped_same_error_across_build_and_clang_is_one_group():
    groups = build_root_cause_groups([
        _failed_job(
            "build_release_arm64",
            "2026-08-17T01:50:44.677496Z /build/arm/src/a.cpp:7:11: "
            "error: no member named 'node_name'",
            1,
        ),
        _failed_job(
            "clang_tidy_check",
            "2026-08-18T10:20:31.123456Z /build/x86/src/a.cpp:91:3: "
            "error: no member named 'node_name'",
            2,
        ),
    ])

    assert len(groups) == 1
    assert groups[0].job_names == ("build_release_arm64", "clang_tidy_check")


def test_third_identical_no_progress_repair_is_rejected():
    result = {
        "status": "repair_no_changes",
        "operation": "repair",
        "job_name": "build_release_arm64",
        "root_cause_id": "root-1",
        "progress_fingerprint": "same-progress",
        "changed_files": [],
        "diagnostic": "same diagnosis",
    }
    messages = _generate_exchange("repair-1", result) + _generate_exchange("repair-2", result)

    decision = evaluate_hermes_budget(
        {"messages": messages},
        "generate_code_tool",
        {
            "job_name": "build_release_arm64",
            "operation": "repair",
            "root_cause_id": "root-1",
        },
    )

    assert decision.allowed is False
    assert decision.reason_code == "no_progress_limit"


def test_progress_change_resets_consecutive_no_progress_counter():
    first = {
        "status": "repair_no_changes",
        "root_cause_id": "root-1",
        "progress_fingerprint": "first",
    }
    second = {
        "status": "changed",
        "root_cause_id": "root-1",
        "progress_fingerprint": "second",
        "changed_files": ["src/a.cpp"],
    }
    messages = _generate_exchange("repair-1", first) + _generate_exchange("repair-2", second)

    decision = evaluate_hermes_budget(
        {"messages": messages},
        "generate_code_tool",
        {"job_name": "build_release_arm64", "operation": "repair", "root_cause_id": "root-1"},
    )

    assert decision.allowed is True


def test_root_cause_progress_counts_only_exact_validation_after_changed_repair():
    pipelines = [
        _root_pipeline(2, "source-sha", "root-a", "root-b"),
        _root_pipeline(8, "fix-sha", "root-a", "root-b", "root-c"),
    ]
    attempts = [_changed_repair(4, "root-a"), _successful_push(6, "fix-sha")]

    progress = build_root_cause_progress(pipelines, attempts, no_progress_limit=2)

    assert progress["root-a"].failed_validations == 1
    assert progress["root-a"].state == "attempted"
    assert progress["root-b"].failed_validations == 0
    assert progress["root-b"].state == "unattempted"
    assert progress["root-c"].failed_validations == 0
    assert progress["root-c"].state == "unattempted"


def test_root_cause_progress_deduplicates_parent_and_downstream_for_same_sha():
    pipelines = [
        _root_pipeline(2, "source-sha", "root-a"),
        _root_pipeline(8, "fix-sha", "root-a"),
        _root_pipeline(9, "fix-sha", "root-a"),
    ]
    attempts = [_changed_repair(4, "root-a"), _successful_push(6, "fix-sha")]

    progress = build_root_cause_progress(pipelines, attempts, no_progress_limit=2)

    assert progress["root-a"].failed_validations == 1
    assert progress["root-a"].state == "attempted"


def test_root_cause_progress_requires_successful_push_and_matching_sha():
    failed_push = ToolAttempt(
        name="commit_and_push_tool",
        args={},
        result={"status": "error", "changed": True, "commit_sha": "fix-sha"},
        sequence=6,
    )
    mismatched = build_root_cause_progress(
        [_root_pipeline(8, "different-sha", "root-a")],
        [_changed_repair(4, "root-a"), _successful_push(6, "fix-sha")],
    )
    failed = build_root_cause_progress(
        [_root_pipeline(8, "fix-sha", "root-a")],
        [_changed_repair(4, "root-a"), failed_push],
    )

    assert mismatched["root-a"].failed_validations == 0
    assert failed["root-a"].failed_validations == 0


def test_root_cause_progress_exhausts_only_twice_repaired_root():
    pipelines = [
        _root_pipeline(2, "source-sha", "root-a", "root-b"),
        _root_pipeline(8, "fix-sha-1", "root-a", "root-b"),
        _root_pipeline(14, "fix-sha-2", "root-a", "root-b"),
    ]
    attempts = [
        _changed_repair(4, "root-a"),
        _successful_push(6, "fix-sha-1"),
        _changed_repair(10, "root-a"),
        _successful_push(12, "fix-sha-2"),
    ]

    progress = build_root_cause_progress(pipelines, attempts, no_progress_limit=2)

    assert progress["root-a"].failed_validations == 2
    assert progress["root-a"].state == "repeat_exhausted"
    assert progress["root-a"].repeat_exhausted is True
    assert progress["root-b"].state == "unattempted"


def test_root_cause_progress_accepts_only_validated_blocker():
    valid = ToolAttempt(
        name="generate_code_tool",
        args={"operation": "repair_session", "root_cause_id": "root-a"},
        result={
            "status": "blocked",
            "root_cause_id": "root-a",
            "job_name": "build_release_arm64",
            "blocker": _valid_blocker(),
        },
        sequence=4,
    )
    invalid = ToolAttempt(
        name="generate_code_tool",
        args={"operation": "repair_session", "root_cause_id": "root-b"},
        result={
            "status": "blocked",
            "root_cause_id": "root-b",
            "job_name": "build_release_arm64",
            "blocker": {"outcome": "blocked"},
        },
        sequence=5,
    )

    progress = build_root_cause_progress(
        [_root_pipeline(2, "source-sha", "root-a", "root-b")],
        [valid, invalid],
    )

    assert progress["root-a"].state == "blocked"
    assert progress["root-b"].state == "unattempted"
