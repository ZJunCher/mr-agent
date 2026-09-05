"""Focused tests for verified repair-episode capture.

Covers Task 2 of the UT-Agent repair-memory implementation plan:
- exact-SHA eligibility: only successful final Pipeline + verified action;
- one episode per verified unambiguous action;
- ambiguous changed-file overlap excludes both actions;
- format-only actions are excluded;
- capture requires final-report enablement and project allowlist;
- recording is idempotent and never raises.
"""

from datetime import datetime, timedelta

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before the eager ut_agent package import
from tests.unittest.repair_memory_helpers import (
    count_rows,
    enabled_memory_settings,
    sample_action,
    sample_manifest,
    sample_pipeline_repair_state,
    sample_report_input,
    sample_report_state,
)
from ut_agent.repair_memory.episodes import (
    _diagnostic_fingerprint,
    build_verified_repair_episodes,
    record_verified_repair_episodes,
)
from ut_agent.repair_memory.store import init_repair_memory_tables
from ut_agent.repair_progress import diagnostic_fingerprint


@pytest.fixture
def memory_db(tmp_path) -> str:
    path = str(tmp_path / "repair-memory.db")
    init_repair_memory_tables(path)
    return path


def test_verified_action_becomes_one_episode():
    report_input = sample_report_input(final_pipeline_status="success", final_sha="b" * 40)
    report_state = sample_report_state(input_digest=report_input.digest(), source="model")
    action = sample_action(
        action_id="action-1",
        root_cause_group_id="root-1",
        status="verified",
        validation_pipeline_id=report_input.final_pipeline_id,
        commit_sha=report_input.final_sha,
        changed_files=("tests/request_test.cpp",),
    )

    episodes = build_verified_repair_episodes(report_input, report_state, sample_manifest(), (action,))

    assert len(episodes) == 1
    assert episodes[0].action_identity == "action-1"
    assert episodes[0].project == report_input.project_id
    assert episodes[0].final_sha == report_input.final_sha


def test_verified_episode_gets_beijing_creation_timestamp():
    report_input = sample_report_input()

    episode = build_verified_repair_episodes(
        report_input,
        sample_report_state(input_digest=report_input.digest()),
        sample_manifest(),
        (sample_action(),),
    )[0]

    assert episode.created_at
    assert datetime.fromisoformat(episode.created_at).utcoffset() == timedelta(hours=8)


def test_episode_fingerprint_ignores_complete_iso_timestamp_microseconds():
    first = _diagnostic_fingerprint(
        ("2026-08-17T01:50:44.677496Z src/a.cpp:142:23: error: no member named node_name",),
        ("build_release_arm64",),
    )
    second = _diagnostic_fingerprint(
        ("2026-08-18T10:20:31.123456Z src/a.cpp:301:9: error: no member named node_name",),
        ("clang_tidy_check",),
    )

    assert first == second


def test_episode_fingerprint_prefers_verified_action_root_cause():
    report_input = sample_report_input(
        causal_lines=("component.cpp:142: error: compiler output was truncated",),
    )
    action_root_cause = (
        "2026-08-20T03:45:32.569704Z /builds/eabot/cook/src/component.cpp:142:23: "
        "error: Request has no member named 'node_name'"
    )
    action = sample_action(root_cause=action_root_cause, job_names=("build_release_arm64",))

    episode = build_verified_repair_episodes(
        report_input,
        sample_report_state(input_digest=report_input.digest()),
        sample_manifest(),
        (action,),
    )[0]

    assert episode.diagnostic_fingerprint == diagnostic_fingerprint(
        action_root_cause,
        job_name="build_release_arm64",
    )


@pytest.mark.parametrize("final_status", ["failed", "canceled", "unknown"])
def test_non_successful_final_pipeline_produces_no_episode(final_status):
    report_input = sample_report_input(final_pipeline_status=final_status)
    report_state = sample_report_state(input_digest=report_input.digest(), source="model")
    assert build_verified_repair_episodes(report_input, report_state, sample_manifest(), (sample_action(),)) == ()


def test_actions_with_distinct_files_become_distinct_episodes():
    report_input = sample_report_input()
    actions = (
        sample_action(action_id="a", root_cause_group_id="root-a", changed_files=("tests/a.cpp",)),
        sample_action(action_id="b", root_cause_group_id="root-b", changed_files=("tests/b.cpp",)),
    )
    episodes = build_verified_repair_episodes(
        report_input,
        sample_report_state(input_digest=report_input.digest()),
        sample_manifest(action_shas=("b" * 40,)),
        actions,
    )
    assert {episode.action_identity for episode in episodes} == {"a", "b"}


def test_overlapping_changed_files_are_not_retrievable_episodes():
    report_input = sample_report_input()
    actions = (
        sample_action(action_id="a", root_cause_group_id="root-a", changed_files=("tests/shared.cpp",)),
        sample_action(action_id="b", root_cause_group_id="root-b", changed_files=("tests/shared.cpp",)),
    )
    assert build_verified_repair_episodes(
        report_input,
        sample_report_state(input_digest=report_input.digest()),
        sample_manifest(action_shas=("b" * 40,)),
        actions,
    ) == ()


def test_format_only_action_is_excluded():
    report_input = sample_report_input()
    action = sample_action(categories=("format",), changed_files=("src/a.cpp",))
    assert build_verified_repair_episodes(
        report_input,
        sample_report_state(input_digest=report_input.digest()),
        sample_manifest(),
        (action,),
    ) == ()


def test_capture_requires_final_report_and_project_allowlist(memory_db, monkeypatch):
    monkeypatch.setattr("ut_agent.repair_memory.episodes.load_repair_memory_settings", enabled_memory_settings)
    monkeypatch.setattr("ut_agent.repair_memory.episodes.final_repair_report_enabled", lambda: False)
    assert record_verified_repair_episodes(
        sample_report_input(),
        sample_report_state(input_digest=sample_report_input().digest()),
        sample_manifest(),
        sample_pipeline_repair_state(),
        path=memory_db,
    ) == ()
    assert count_rows(memory_db, "repair_memory_episodes") == 0


def test_record_is_idempotent_and_never_raises(memory_db, monkeypatch):
    monkeypatch.setattr("ut_agent.repair_memory.episodes.load_repair_memory_settings", enabled_memory_settings)
    monkeypatch.setattr("ut_agent.repair_memory.episodes.final_repair_report_enabled", lambda: True)

    report_input = sample_report_input()
    report_state = sample_report_state(input_digest=report_input.digest())
    first = record_verified_repair_episodes(
        report_input, report_state, sample_manifest(), sample_pipeline_repair_state(), path=memory_db
    )
    second = record_verified_repair_episodes(
        report_input, report_state, sample_manifest(), sample_pipeline_repair_state(), path=memory_db
    )
    assert first == second
    assert len(first) == 1
    assert count_rows(memory_db, "repair_memory_episodes") == 1


def test_unverified_action_produces_no_episode():
    report_input = sample_report_input()
    action = sample_action(status="failed", validation_pipeline_id=101)
    assert build_verified_repair_episodes(
        report_input,
        sample_report_state(input_digest=report_input.digest()),
        sample_manifest(),
        (action,),
    ) == ()


def test_validation_pipeline_mismatch_produces_no_episode():
    report_input = sample_report_input()
    action = sample_action(validation_pipeline_id=999)
    assert build_verified_repair_episodes(
        report_input,
        sample_report_state(input_digest=report_input.digest()),
        sample_manifest(),
        (action,),
    ) == ()


def test_commit_not_in_manifest_produces_no_episode():
    report_input = sample_report_input()
    action = sample_action(commit_sha="x" * 40)
    assert build_verified_repair_episodes(
        report_input,
        sample_report_state(input_digest=report_input.digest()),
        sample_manifest(),
        (action,),
    ) == ()
