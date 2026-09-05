"""Tests for self-reflect YAML schema fallback (Option A defensive parsing).

The self-reflect LLM call is asked to return a top-level `code_suggestions`
key, but reasoning models sometimes drift to a differently-named key (e.g.
`suggestions`) or an entirely different, unmappable schema (e.g. `code_review`
with fields like `file`/`line_range`). Previously any drift silently zeroed
out feedback_by_summary matching, which caused every suggestion's
relevant_lines_start/end to fall back to -1 and be skipped as invalid_lines.
"""

import asyncio

import pytest

from pr_agent.tools.pr_code_suggestions import (
    PRCodeSuggestions,
    _extract_reflect_feedback_list,
)


# ---------------------------------------------------------------------------
# _extract_reflect_feedback_list
# ---------------------------------------------------------------------------

def test_extracts_from_expected_key():
    parsed = {"code_suggestions": [{"suggestion_summary": "a"}]}
    feedback, key = _extract_reflect_feedback_list(parsed)
    assert feedback == [{"suggestion_summary": "a"}]
    assert key == "code_suggestions"


def test_falls_back_to_suggestions_alias():
    parsed = {"suggestions": [{"suggestion_summary": "a"}]}
    feedback, key = _extract_reflect_feedback_list(parsed)
    assert feedback == [{"suggestion_summary": "a"}]
    assert key == "suggestions"


def test_returns_empty_for_unmappable_schema():
    parsed = {"code_review": [{"file": "a.py", "line_range": [1, 2]}]}
    feedback, key = _extract_reflect_feedback_list(parsed)
    assert feedback == []
    assert key is None


def test_returns_empty_for_falsy_input():
    assert _extract_reflect_feedback_list(None) == ([], None)
    assert _extract_reflect_feedback_list({}) == ([], None)
    assert _extract_reflect_feedback_list([]) == ([], None)


def test_prefers_expected_key_over_alias_when_both_present():
    parsed = {
        "code_suggestions": [{"suggestion_summary": "expected"}],
        "suggestions": [{"suggestion_summary": "alias"}],
    }
    feedback, key = _extract_reflect_feedback_list(parsed)
    assert feedback == [{"suggestion_summary": "expected"}]
    assert key == "code_suggestions"


# ---------------------------------------------------------------------------
# analyze_self_reflection_response integration
# ---------------------------------------------------------------------------

class _FakeGitProvider:
    def get_diff_files(self):
        return []


def _make_instance():
    """Bypass __init__ (which requires a live git provider) for a unit test."""
    instance = object.__new__(PRCodeSuggestions)
    instance.git_provider = _FakeGitProvider()
    return instance


def _suggestion(**kw):
    base = {
        "one_sentence_summary": "fix off-by-one",
        "relevant_file": "a.py",
        "existing_code": "old",
        "improved_code": "new",
        "label": "bug",
    }
    base.update(kw)
    return base


def test_alias_key_recovers_relevant_lines():
    instance = _make_instance()
    data = {"code_suggestions": [_suggestion()]}
    response_reflect = (
        "suggestions:\n"
        "  - suggestion_summary: fix off-by-one\n"
        "    relevant_file: a.py\n"
        "    relevant_lines_start: 10\n"
        "    relevant_lines_end: 12\n"
        "    suggestion_score: 8\n"
        "    why: because\n"
    )
    asyncio.run(instance.analyze_self_reflection_response(data, response_reflect))
    suggestion = data["code_suggestions"][0]
    assert suggestion["relevant_lines_start"] == 10
    assert suggestion["relevant_lines_end"] == 12
    assert suggestion["score"] == 8


def test_unmappable_schema_falls_back_to_default_and_does_not_crash():
    instance = _make_instance()
    data = {"code_suggestions": [_suggestion()]}
    response_reflect = (
        "code_review:\n"
        "  - file: a.py\n"
        "    line_range: [1, 2]\n"
        "    severity: Blocker\n"
    )
    # Must not raise even though the schema is completely unmappable.
    asyncio.run(instance.analyze_self_reflection_response(data, response_reflect))
    # code_suggestions_feedback stays empty, so the per-suggestion loop at
    # line ~507 is skipped entirely; relevant_lines_start/end are assigned
    # by the caller's post-processing (not this method), so no key is set here.
    suggestion = data["code_suggestions"][0]
    assert "relevant_lines_start" not in suggestion
