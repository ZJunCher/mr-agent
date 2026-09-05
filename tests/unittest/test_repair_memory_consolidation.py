"""Focused tests for repair-memory consolidation and global promotion.

Covers Task 3 of the UT-Agent repair-memory implementation plan:
- strict candidate parsing with controlled taxonomy;
- stable pattern keys ignoring free text;
- one verified episode creates one active project memory;
- additional episodes reinforce without duplicate;
- same pattern in two projects promotes one global memory;
- two episodes from one project do not promote;
- ``other`` taxonomy never promotes;
- global support revalidation when project memory is disabled;
- global de-identification rejects project paths and symbols;
- expired consolidation claim can be recovered;
- retention keeps support evidence and prunes old settled hits.
"""

import asyncio
import json
import sqlite3

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before the eager ut_agent package import
from tests.unittest.repair_memory_helpers import (
    disable_project_memory,
    seed_old_episode,
    seed_old_settled_hit,
    seed_pending_episode,
    seed_project_memory,
    seed_promoted_global_memory,
    seed_two_projects,
    supporting_episodes,
    valid_candidate_payload,
)
from ut_agent.model_failover import LLMCallOutcome
from ut_agent.repair_memory.consolidate import (
    GlobalMemoryLeakError,
    MemoryCandidateValidationError,
    _build_consolidation_input,
    _build_global_promotion_input,
    _parse_model_candidate_response,
    candidate_from_payload,
    parse_memory_candidate,
    pattern_key_for,
    promote_ready_patterns,
    run_consolidation_batch,
    validate_global_candidate,
)
from ut_agent.repair_memory.models import MemoryStatus
from ut_agent.repair_memory.store import (
    claim_pending_episodes,
    init_repair_memory_tables,
    list_attempt_hits,
    list_memories,
    load_episode,
    load_memory,
    prune_expired_memory_data,
    revalidate_global_support,
)


@pytest.fixture
def memory_db(tmp_path) -> str:
    path = str(tmp_path / "repair-memory.db")
    init_repair_memory_tables(path)
    return path


def _fake_llm_outcome(payload: dict):
    """Return one strict submit_repair_memory Tool Calling outcome."""
    return LLMCallOutcome(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "memory-1",
                "type": "function",
                "function": {
                    "name": "submit_repair_memory",
                    "arguments": json.dumps(payload, ensure_ascii=False),
                },
            }],
        },
        "test-model",
        (),
    )


def _fake_text_outcome(text: str, terminal_error: str = ""):
    """Return ordinary assistant text, which the strict boundary must reject."""
    return LLMCallOutcome(
        {"role": "assistant", "content": text},
        "test-model",
        (),
        terminal_error,
    )


def _fake_call(outcome):
    async def _call(*args, **kwargs):
        return outcome

    return _call


def _fake_global_call():
    """Return a callable for global promotion that yields a de-identified candidate."""

    async def _call(*args, **kwargs):
        return _fake_llm_outcome(valid_candidate_payload())

    return _call


def test_candidate_parser_accepts_only_controlled_taxonomy():
    candidate = parse_memory_candidate(json.dumps(valid_candidate_payload()))
    assert candidate.failure_family == "missing_member"
    assert len(pattern_key_for(candidate)) == 24


def test_candidate_parser_rejects_unknown_taxonomy():
    payload = valid_candidate_payload()
    payload["root_cause_class"] = "invented_class"
    with pytest.raises(MemoryCandidateValidationError, match="root_cause_class"):
        parse_memory_candidate(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "english_value"),
    [
        ("problem_pattern", "A request member is absent."),
        ("applicability", ["The compiler reports a missing member."]),
        ("anti_conditions", ["The member exists in the current interface."]),
        ("repair_guidance", "Align the test with the current interface."),
        ("validation_guidance", ["Run the exact-SHA Pipeline."]),
    ],
)
def test_candidate_parser_rejects_english_user_visible_fields(field, english_value):
    payload = valid_candidate_payload()
    payload[field] = english_value

    with pytest.raises(MemoryCandidateValidationError, match=f"content_locale:{field}"):
        parse_memory_candidate(json.dumps(payload))


def test_candidate_parser_accepts_chinese_explanations_with_technical_identifiers():
    candidate = candidate_from_payload(
        valid_candidate_payload(
            problem_pattern="编译器找不到 std::unique_ptr",
            repair_guidance="补充 #include <memory> 后重新编译",
        )
    )

    assert "std::unique_ptr" in candidate.problem_pattern
    assert "#include <memory>" in candidate.repair_guidance


def test_model_response_adapter_accepts_one_full_json_fence():
    payload = json.dumps(valid_candidate_payload())

    candidate = _parse_model_candidate_response(f"```json\n{payload}\n```")

    assert candidate.failure_family == "missing_member"


@pytest.mark.parametrize(
    "response",
    [
        "Here is the result:\n```json\n{}\n```",
        "```json\n{}\n```\nAdditional explanation",
        "```json\n{}\n```\n```json\n{}\n```",
    ],
)
def test_model_response_adapter_rejects_prose_and_multiple_fences(response):
    with pytest.raises(MemoryCandidateValidationError):
        _parse_model_candidate_response(response)


def test_pattern_key_is_stable_and_ignores_free_text():
    first = candidate_from_payload(valid_candidate_payload(problem_pattern="第一种表述"))
    second = candidate_from_payload(valid_candidate_payload(problem_pattern="第二种表述"))
    assert pattern_key_for(first) == pattern_key_for(second)


def test_one_verified_episode_creates_active_project_memory(memory_db):
    seed_pending_episode(memory_db, project="group/a")
    outcome = _fake_llm_outcome(valid_candidate_payload())
    summary = run_consolidation_batch(10, "worker-1", memory_db, llm_call=_fake_call(outcome))

    memories = list_memories(scope="project", scope_key="group/a", path=memory_db)
    assert summary.completed == 1
    assert len(memories) == 1
    assert memories[0].confidence == pytest.approx(0.60)
    assert memories[0].support_project_count == 1
    assert memories[0].content_locale == "zh-CN"


def test_additional_project_episode_reinforces_existing_memory_without_duplicate(memory_db):
    seed_pending_episode(memory_db, project="group/a", episode_id="episode:task-1:action-a")
    seed_pending_episode(memory_db, project="group/a", episode_id="episode:task-2:action-b")
    outcome = _fake_llm_outcome(valid_candidate_payload())
    run_consolidation_batch(10, "worker-1", memory_db, llm_call=_fake_call(outcome))
    memories = list_memories(scope="project", scope_key="group/a", path=memory_db)
    assert len(memories) == 1
    assert memories[0].support_episode_count == 2
    assert memories[0].confidence == pytest.approx(0.65)


def test_consolidation_user_prompt_is_self_contained(memory_db):
    seed_pending_episode(memory_db)
    episode = claim_pending_episodes("worker", limit=1, lease_seconds=60, path=memory_db)[0]

    prompt = _build_consolidation_input(episode)

    assert "Convert the verified repair episode" in prompt
    assert "Call submit_repair_memory exactly once" in prompt
    assert '"schema_version": 1' in prompt
    assert "[VERIFIED_REPAIR_EPISODE]" in prompt


def test_invalid_prose_is_corrected_without_echoing_rejected_text(memory_db):
    seed_pending_episode(memory_db)
    prompts: list[str] = []
    outcomes = iter(
        (
            _fake_text_outcome("REJECTED PROSE"),
            _fake_llm_outcome(valid_candidate_payload()),
        )
    )

    async def call(system, user, **kwargs):
        prompts.append(user)
        return next(outcomes)

    summary = run_consolidation_batch(1, "worker", memory_db, llm_call=call)

    assert summary.completed == 1
    assert len(prompts) == 2
    assert "tool_call_missing" in prompts[1]
    assert "REJECTED PROSE" not in prompts[1]


def test_english_candidate_is_corrected_without_echoing_rejected_text(memory_db):
    seed_pending_episode(memory_db)
    prompts: list[str] = []
    english_payload = valid_candidate_payload(problem_pattern="A request member is absent.")
    outcomes = iter(
        (
            _fake_llm_outcome(english_payload),
            _fake_llm_outcome(valid_candidate_payload()),
        )
    )

    async def call(system, user, **kwargs):
        prompts.append(user)
        return next(outcomes)

    summary = run_consolidation_batch(1, "worker", memory_db, llm_call=call)

    assert summary.completed == 1
    assert len(prompts) == 2
    assert "content_locale:problem_pattern" in prompts[1]
    assert "A request member is absent." not in prompts[1]


def test_full_json_fence_is_not_a_tool_call_and_stops_after_corrections(memory_db):
    seed_pending_episode(memory_db)
    calls = 0

    async def call(*args, **kwargs):
        nonlocal calls
        calls += 1
        payload = json.dumps(valid_candidate_payload())
        return _fake_text_outcome(f"```json\n{payload}\n```")

    summary = run_consolidation_batch(1, "worker", memory_db, llm_call=call)

    assert summary.invalid == 1
    assert calls == 3


def test_consolidation_stops_after_two_correction_attempts(memory_db):
    seed_pending_episode(memory_db)
    calls = 0

    async def call(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _fake_text_outcome("not json")

    summary = run_consolidation_batch(1, "worker", memory_db, llm_call=call)

    assert calls == 3
    assert summary.invalid == 1


def test_incident_shaped_episodes_consolidate_when_user_prompt_contains_task(memory_db):
    first = seed_pending_episode(memory_db, episode_id="episode:cook-549")
    second = seed_pending_episode(memory_db, episode_id="episode:cook-551")
    connection = sqlite3.connect(memory_db)
    try:
        with connection:
            connection.execute(
                "UPDATE repair_memory_episodes SET root_cause = ?, solution_summary = ? WHERE episode_id = ?",
                (
                    "A request interface no longer declares node_name.",
                    "Align the handler with the current generated request interface.",
                    first.episode_id,
                ),
            )
            connection.execute(
                "UPDATE repair_memory_episodes SET root_cause = ?, solution_summary = ? WHERE episode_id = ?",
                (
                    "A removed package is still declared as a build dependency.",
                    "Remove the obsolete dependency declarations from build metadata.",
                    second.episode_id,
                ),
            )
    finally:
        connection.close()

    async def task_aware_call(system, user, **kwargs):
        if "Convert the verified repair episode" not in user or "Call submit_repair_memory exactly once" not in user:
            return _fake_text_outcome("Please tell me what to do with this metadata.")
        return _fake_llm_outcome(valid_candidate_payload())

    summary = run_consolidation_batch(2, "worker", memory_db, llm_call=task_aware_call)

    assert summary.completed == 2
    assert summary.invalid == 0


def test_final_validation_field_is_persisted(memory_db):
    seed_pending_episode(memory_db)
    outcome = _fake_llm_outcome(valid_candidate_payload(root_cause_class="invented"))

    summary = run_consolidation_batch(1, "worker", memory_db, llm_call=_fake_call(outcome))

    connection = sqlite3.connect(memory_db)
    try:
        row = connection.execute(
            "SELECT consolidation_status, last_error_code FROM repair_memory_episodes"
        ).fetchone()
    finally:
        connection.close()
    assert summary.invalid == 1
    assert row == ("invalid", "invalid_candidate:schema:root_cause_class:literal_error")


def test_same_pattern_in_two_projects_promotes_one_global_memory(memory_db):
    seed_project_memory(memory_db, project="group/a", pattern_key="pattern-1", episode_id="episode:task-a:action-a")
    seed_project_memory(memory_db, project="group/b", pattern_key="pattern-1", episode_id="episode:task-b:action-b")

    summary = asyncio.run(promote_ready_patterns(memory_db, llm_call=_fake_global_call()))

    global_memories = list_memories(scope="global", scope_key="*", path=memory_db)
    assert summary.promoted == 1
    assert len(global_memories) == 1
    assert global_memories[0].support_project_count == 2
    assert global_memories[0].confidence == pytest.approx(0.70)


def test_global_promotion_user_prompt_is_self_contained():
    candidate = candidate_from_payload(valid_candidate_payload())

    prompt = _build_global_promotion_input(candidate)

    assert "Generalize the project Repair Memory" in prompt
    assert "Call submit_repair_memory exactly once" in prompt
    assert "[PROJECT_REPAIR_MEMORY]" in prompt
    assert '"schema_version": 1' in prompt


def test_two_episodes_from_one_project_do_not_promote(memory_db):
    seed_project_memory(memory_db, project="group/a", pattern_key="pattern-1", episode_id="episode:task-a:action-a")
    seed_project_memory(memory_db, project="group/a", pattern_key="pattern-1", episode_id="episode:task-b:action-b")
    assert asyncio.run(promote_ready_patterns(memory_db, llm_call=_fake_global_call())).promoted == 0


def test_other_taxonomy_never_promotes(memory_db):
    seed_two_projects(memory_db, pattern_key="pattern-other", failure_family="other")
    assert asyncio.run(promote_ready_patterns(memory_db, llm_call=_fake_global_call())).promoted == 0


def test_global_memory_needs_review_when_active_project_support_drops_below_two(memory_db):
    global_memory = seed_promoted_global_memory(memory_db, projects=("group/a", "group/b"))
    disable_project_memory(memory_db, project="group/b", pattern_key=global_memory.pattern_key)
    assert revalidate_global_support(global_memory.pattern_key, memory_db) is True
    assert load_memory(global_memory.memory_id, memory_db).status is MemoryStatus.NEEDS_REVIEW


def test_global_candidate_rejects_project_path_and_symbol(memory_db):
    seed_two_projects(memory_db, pattern_key="pattern-1", changed_file="cook/src/private.cpp")
    candidate = candidate_from_payload(
        valid_candidate_payload(problem_pattern="问题涉及 cook/src/private.cpp 中的 RemoteControl_Request")
    )
    with pytest.raises(GlobalMemoryLeakError):
        validate_global_candidate(candidate, supporting_episodes(memory_db, "pattern-1"))


def test_expired_consolidation_claim_can_be_recovered(memory_db):
    episode = seed_pending_episode(memory_db, project="group/a")
    first = claim_pending_episodes("worker-a", limit=10, lease_seconds=1, now=100.0, path=memory_db)
    blocked = claim_pending_episodes("worker-b", limit=10, lease_seconds=1, now=100.5, path=memory_db)
    recovered = claim_pending_episodes("worker-b", limit=10, lease_seconds=1, now=102.0, path=memory_db)
    assert [item.episode_id for item in first] == [episode.episode_id]
    assert blocked == ()
    assert [item.episode_id for item in recovered] == [episode.episode_id]


def test_retention_keeps_support_evidence_and_prunes_old_settled_hits(memory_db):
    referenced = seed_old_episode(memory_db, episode_id="episode:referenced", linked_to_memory=True)
    unreferenced = seed_old_episode(memory_db, episode_id="episode:unreferenced", linked_to_memory=False)
    seed_old_settled_hit(memory_db, attempt_id="old-attempt")

    summary = prune_expired_memory_data(
        "2026-08-15T00:00:00+00:00", episode_retention_days=365, hit_retention_days=365, path=memory_db
    )

    assert load_episode(referenced.episode_id, memory_db) is not None
    assert load_episode(unreferenced.episode_id, memory_db) is None
    assert list_attempt_hits("old-attempt", memory_db) == ()
    assert summary.deleted_episodes == 1
    assert summary.deleted_hits == 1


@pytest.mark.parametrize("created_at", ["", "not-a-timestamp"])
def test_retention_preserves_episode_with_unparseable_timestamp(memory_db, created_at):
    episode = seed_pending_episode(memory_db, episode_id=f"episode:{created_at or 'empty'}")
    connection = sqlite3.connect(memory_db)
    try:
        with connection:
            connection.execute(
                "UPDATE repair_memory_episodes SET created_at = ? WHERE episode_id = ?",
                (created_at, episode.episode_id),
            )
    finally:
        connection.close()

    summary = prune_expired_memory_data(
        "2026-08-18T00:00:00+08:00",
        episode_retention_days=1,
        hit_retention_days=365,
        path=memory_db,
    )

    assert load_episode(episode.episode_id, memory_db) is not None
    assert summary.deleted_episodes == 0


def test_retention_compares_episode_offsets_as_datetimes(memory_db):
    episode = seed_pending_episode(memory_db, episode_id="episode:offset")
    connection = sqlite3.connect(memory_db)
    try:
        with connection:
            connection.execute(
                "UPDATE repair_memory_episodes SET created_at = ? WHERE episode_id = ?",
                ("2026-08-17T23:30:00-08:00", episode.episode_id),
            )
    finally:
        connection.close()

    summary = prune_expired_memory_data(
        "2026-08-19T00:00:00+08:00",
        episode_retention_days=1,
        hit_retention_days=365,
        path=memory_db,
    )

    assert load_episode(episode.episode_id, memory_db) is not None
    assert summary.deleted_episodes == 0


@pytest.mark.parametrize("settled_at", ["", "not-a-timestamp"])
def test_retention_preserves_settled_hit_with_unparseable_timestamp(memory_db, settled_at):
    seed_old_settled_hit(memory_db, attempt_id="malformed-hit")
    connection = sqlite3.connect(memory_db)
    try:
        with connection:
            connection.execute(
                "UPDATE repair_memory_hits SET settled_at = ? WHERE attempt_id = ?",
                (settled_at, "malformed-hit"),
            )
    finally:
        connection.close()

    summary = prune_expired_memory_data(
        "2026-08-18T00:00:00+08:00",
        episode_retention_days=365,
        hit_retention_days=1,
        path=memory_db,
    )

    assert list_attempt_hits("malformed-hit", memory_db)
    assert summary.deleted_hits == 0
