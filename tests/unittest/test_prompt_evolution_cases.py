import sqlite3

import pytest

from pr_agent.feedback.store import list_evolution_cases, save_evolution_case
from pr_agent.suggestions.prompt_evolution.cases import (
    EvolutionCaseKind,
    attributable_case_kind,
    build_evolution_case,
)


def _case(**updates) -> dict:
    value = {
        "kind": "false_negative",
        "project": "group/repo",
        "mr_iid": "12",
        "review_id": "review-12",
        "head_sha": "a" * 40,
        "command": "review",
        "description": "Missing null check on the decoded value.",
        "source": "manual",
        "file_path": "src/parser.py",
        "line_start": 20,
        "line_end": 20,
        "created_at": "2026-08-27T00:00:00+08:00",
    }
    value.update(updates)
    return value


def test_case_schema_derives_expected_action_and_stable_identity():
    first = build_evolution_case(_case())
    second = build_evolution_case(_case(description="  missing NULL check on the decoded value. "))

    assert first.expected_action == "emit"
    assert first.case_id == second.case_id
    assert first.case_hash == second.case_hash


def test_bad_fix_requires_suggestion_identity():
    with pytest.raises(ValueError, match="suggestion_id"):
        build_evolution_case(_case(kind="bad_fix", file_path="", line_start=0, line_end=0))

    case = build_evolution_case(_case(
        kind="bad_fix",
        file_path="",
        line_start=0,
        line_end=0,
        suggestion_id="suggestion-1",
    ))
    assert case.expected_action == "revise"


@pytest.mark.parametrize("value", [
    _case(file_path="../secret", line_start=1),
    _case(head_sha="main"),
    _case(line_start=20, line_end=10),
    _case(kind="output_schema_error", source="automatic", error_code="timeout", file_path="", line_start=0),
    _case(kind="parser_error", source="manual", error_code="parser_error", file_path="", line_start=0),
])
def test_case_schema_rejects_unsafe_or_non_attributable_records(value):
    with pytest.raises(ValueError):
        build_evolution_case(value)


def test_attributable_error_mapping_excludes_infrastructure_failures():
    assert attributable_case_kind("parser_error") is EvolutionCaseKind.PARSER_ERROR
    assert attributable_case_kind("incomplete_coverage") is EvolutionCaseKind.INCOMPLETE_COVERAGE
    assert attributable_case_kind("timeout") is None
    assert attributable_case_kind("model_unavailable") is None
    assert attributable_case_kind("gitlab_api_error") is None


def test_case_store_is_idempotent_and_queryable(tmp_path):
    path = str(tmp_path / "feedback.db")

    assert save_evolution_case(_case(), path=path)
    assert save_evolution_case(_case(), path=path)

    rows = list_evolution_cases(path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "false_negative"
    assert rows[0]["expected_action"] == "emit"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM evolution_cases").fetchone()[0] == 1
    conn.close()


def test_store_rejects_invalid_case_without_writing(tmp_path):
    path = str(tmp_path / "feedback.db")

    assert save_evolution_case(_case(file_path="/etc/passwd"), path=path) is False
    assert list_evolution_cases(path) == []
