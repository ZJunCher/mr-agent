"""Tests for the Tier-1 small-model repair retry (tier1_repair.py)."""
import asyncio
import json

from pr_agent.suggestions.tier1_repair import (_companion_suggestion,
                                               repair_task, run_tier1_repair)

CPP_HEAD_FILE = "\n".join([
    "#include <mutex>",
    "",
    "class Foo {",
    "public:",
    "    void bar();",
    "};",
])


def _task(**overrides):
    base = {
        "kind": "single",
        "relevant_file": "src/foo.cpp",
        "structural_issue": "existing_mismatch",
        "fix_note": "existing_code not found above the fuzzy-match threshold",
        "companion_head_file": None,
        "needs_tier2": False,
        "members": [{
            "relevant_file": "src/foo.cpp",
            "existing_code": "    void bar();",
            "improved_code": "    void bar() const;",
            "relevant_lines_start": 999, "relevant_lines_end": 999,
            "one_sentence_summary": "make bar const",
            "suggestion_content": "bar() should be const",
            "label": "correctness",
            "score": 6,
        }],
    }
    base.update(overrides)
    return base


class MockAI:
    def __init__(self, response=None):
        self._response = response
        self.calls = []

    async def chat_completion(self, model, system, user, temperature=0.1, img_path=None):
        self.calls.append({"model": model, "system": system, "user": user})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response, "stop"


GOOD_FIX = json.dumps({
    "fixable": True,
    "primary": {
        "existing_code": "    void bar();",
        "improved_code": "    void bar() const;",
        "relevant_lines_start": 5,
        "relevant_lines_end": 5,
    },
    "reason": "corrected line number",
})

NOT_FIXABLE = json.dumps({"fixable": False, "reason": "cannot determine a safe fix"})


class TestRepairTask:
    def test_returns_primary_dict_on_success(self):
        ai = MockAI(response=GOOD_FIX)
        result = asyncio.run(repair_task(ai, _task(), CPP_HEAD_FILE, model="test-model"))
        assert result is not None
        assert result["primary"]["existing_code"] == "    void bar();"
        assert result["companion"] is None

    def test_returns_none_when_model_says_not_fixable(self):
        ai = MockAI(response=NOT_FIXABLE)
        assert asyncio.run(repair_task(ai, _task(), CPP_HEAD_FILE, model="test-model")) is None

    def test_returns_none_on_llm_exception(self):
        ai = MockAI(response=RuntimeError("boom"))
        assert asyncio.run(repair_task(ai, _task(), CPP_HEAD_FILE, model="test-model")) is None

    def test_returns_none_on_unparseable_response(self):
        ai = MockAI(response="not json at all")
        assert asyncio.run(repair_task(ai, _task(), CPP_HEAD_FILE, model="test-model")) is None


class TestRunTier1Repair:
    def test_successful_repair_marks_resolved_by_stage(self):
        ai = MockAI(response=GOOD_FIX)
        head_map = {"src/foo.cpp": CPP_HEAD_FILE}
        resolved, unresolved = asyncio.run(
            run_tier1_repair(ai, [_task()], head_map, model="test-model", max_retries=2))
        assert len(resolved) == 1 and not unresolved
        assert resolved[0]["resolved_by_stage"] == "tier1_llm"
        assert resolved[0]["relevant_lines_start"] == 5

    def test_repair_failing_validation_is_retried_then_unresolved(self):
        # the model's proposed existing_code does not actually appear in
        # head_file at the claimed lines -> must fail validation every time
        bad_fix = json.dumps({
            "fixable": True,
            "primary": {"existing_code": "totally not in the file", "improved_code": "still not in the file",
                        "relevant_lines_start": 1, "relevant_lines_end": 1},
        })
        ai = MockAI(response=bad_fix)
        head_map = {"src/foo.cpp": CPP_HEAD_FILE}
        resolved, unresolved = asyncio.run(
            run_tier1_repair(ai, [_task()], head_map, model="test-model", max_retries=2))
        assert not resolved and len(unresolved) == 1
        assert "tier1" in unresolved[0]["fix_note"]
        assert len(ai.calls) == 2  # retried up to max_retries

    def test_not_fixable_becomes_unresolved_without_retry_exhaustion_crash(self):
        ai = MockAI(response=NOT_FIXABLE)
        head_map = {"src/foo.cpp": CPP_HEAD_FILE}
        resolved, unresolved = asyncio.run(
            run_tier1_repair(ai, [_task()], head_map, model="test-model", max_retries=1))
        assert not resolved and len(unresolved) == 1

    def test_never_raises_with_empty_task_list(self):
        ai = MockAI(response=GOOD_FIX)
        resolved, unresolved = asyncio.run(run_tier1_repair(ai, [], {}, model="test-model"))
        assert resolved == [] and unresolved == []


class TestCompanionSuggestion:
    """_companion_suggestion hand-builds its dict field-by-field (unlike
    _apply_primary, which copies task["members"][0] wholesale) -- it must
    still carry `severity` through, or the /improve summary table's Impact
    column silently renders "Unspecified"/"未标注" for every companion-file
    edit even when the original suggestion had a real severity."""

    def test_severity_is_carried_over_from_the_original_member(self):
        task = _task(members=[{
            "relevant_file": "src/foo.cpp",
            "existing_code": "    void bar();",
            "improved_code": "    void bar() const;",
            "one_sentence_summary": "make bar const",
            "suggestion_content": "bar() should be const",
            "label": "correctness",
            "score": 8,
            "severity": "High",
            "companion_file": "src/foo.h",
        }])
        companion = _companion_suggestion(task, {
            "file": "src/foo.h",
            "existing_code": "void bar();",
            "improved_code": "void bar() const;",
        })
        assert companion is not None
        assert companion["severity"] == "High"

    def test_missing_severity_on_original_defaults_to_empty_not_absent_key(self):
        task = _task()  # default member has no "severity" key at all
        companion = _companion_suggestion(task, {
            "file": "src/foo.h",
            "existing_code": "void bar();",
            "improved_code": "void bar() const;",
        })
        assert companion is not None
        assert companion.get("severity", "") == ""


# --------------------------------------------------------------------------- #
# Integration: tier1 resolution + apply_final_normalization post-process
# --------------------------------------------------------------------------- #
# Diff where line 5 of CPP_HEAD_FILE is in a new-side hunk (bar() was added)
_CPP_DIFF_LINE5_IN_HUNK = (
    "## File: 'src/foo.cpp'\n"
    "__new hunk__\n"
    "3  class Foo {\n"
    "4  public:\n"
    "5 +    void bar();\n"
    "6  };\n"
    "__old hunk__\n"
    " class Foo {\n"
    " public:\n"
    "-    void old_bar();\n"
    " };\n"
)

# Diff where only line 1 (#include) changed; line 5 (bar()) is NOT in any hunk
_CPP_DIFF_LINE5_NOT_IN_HUNK = (
    "## File: 'src/foo.cpp'\n"
    "__new hunk__\n"
    "1 +#include <mutex>\n"
    "__old hunk__\n"
    "-#include <old_mutex>\n"
)


class TestTier1WithFinalNormalization:
    """Integration: tier1 resolves a suggestion, then apply_final_normalization
    either accepts it (existing_code is in a new-side hunk) or rejects it."""

    def test_tier1_resolved_passes_when_existing_code_in_hunk(self):
        """After a successful tier1 repair, apply_final_normalization accepts the
        suggestion because existing_code ('    void bar();') sits on line 5 which
        IS covered by a new-side diff hunk."""
        ai = MockAI(response=GOOD_FIX)
        head_map = {"src/foo.cpp": CPP_HEAD_FILE}
        resolved, _ = asyncio.run(
            run_tier1_repair(ai, [_task()], head_map, model="test-model", max_retries=1))
        assert len(resolved) == 1

        from pr_agent.suggestions.deterministic_fix import \
            apply_final_normalization
        final, rejected = apply_final_normalization(resolved, head_map, _CPP_DIFF_LINE5_IN_HUNK)
        assert len(final) == 1 and not rejected
        assert final[0]["relevant_lines_start"] == 5

    def test_tier1_resolved_rejected_when_existing_code_outside_any_hunk(self):
        """After a successful tier1 repair, apply_final_normalization rejects the
        suggestion because '    void bar();' at line 5 is NOT covered by any
        new-side diff hunk (only line 1 was changed in this diff)."""
        ai = MockAI(response=GOOD_FIX)
        head_map = {"src/foo.cpp": CPP_HEAD_FILE}
        resolved, _ = asyncio.run(
            run_tier1_repair(ai, [_task()], head_map, model="test-model", max_retries=1))
        assert len(resolved) == 1

        from pr_agent.suggestions.deterministic_fix import \
            apply_final_normalization
        final, rejected = apply_final_normalization(resolved, head_map, _CPP_DIFF_LINE5_NOT_IN_HUNK)
        assert not final and len(rejected) == 1
