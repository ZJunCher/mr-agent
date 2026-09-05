"""Tests for the zero-LLM deterministic repair layer (deterministic_fix.py).

This module evolves inline_gate.py's G1-G4 checks from "detect and block"
into "detect and repair": instead of stopping a structurally-flawed
suggestion from being published, it tries to fix it first.
"""
from pr_agent.suggestions.deterministic_fix import (
    fuzzy_match_existing_code, repair_existing_mismatch,
    validate_repaired_suggestion)


class TestFuzzyMatchExistingCode:
    def test_finds_correct_window_when_line_numbers_are_wrong(self):
        head_file = "\n".join([
            "a", "b", "    auto it = monitors_.find(topic);", "    do_x();", "e",
        ])
        existing = "auto it = monitors_.find(topic);\ndo_x();"
        result = fuzzy_match_existing_code(existing, head_file, threshold=0.85)
        assert result is not None
        matched_text, start, end = result
        assert start == 3 and end == 4
        assert "monitors_.find(topic)" in matched_text

    def test_returns_none_when_nothing_similar_exists(self):
        head_file = "a\nb\nc\nd\ne"
        existing = "totally_unrelated_code_xyz();\nanother_line_abc();"
        assert fuzzy_match_existing_code(existing, head_file, threshold=0.85) is None

    def test_returns_none_on_empty_inputs(self):
        assert fuzzy_match_existing_code("", "some file", 0.85) is None
        assert fuzzy_match_existing_code("some code", "", 0.85) is None


class TestRepairExistingMismatch:
    def test_repairs_wrong_line_numbers(self):
        head_file = "\n".join(["a", "b", "    auto it = monitors_.find(topic);", "    do_x();", "e"])
        sugg = {
            "relevant_file": "a.cpp",
            "existing_code": "auto it = monitors_.find(topic);\ndo_x();",
            "improved_code": "auto it = monitors_.find(topic);\ndo_y();",
            "relevant_lines_start": 99, "relevant_lines_end": 99,
        }
        fixed = repair_existing_mismatch(sugg, head_file, threshold=0.85)
        assert fixed is not None
        assert fixed["relevant_lines_start"] == 3
        assert fixed["relevant_lines_end"] == 4
        # original dict must not be mutated
        assert sugg["relevant_lines_start"] == 99

    def test_returns_none_when_unfixable(self):
        head_file = "a\nb\nc"
        sugg = {"relevant_file": "a.cpp", "existing_code": "nonexistent_call_xyz();",
                 "improved_code": "nonexistent_call_fixed();",
                 "relevant_lines_start": 1, "relevant_lines_end": 1}
        assert repair_existing_mismatch(sugg, head_file, threshold=0.85) is None


class TestValidateRepairedSuggestion:
    def test_valid_suggestion_passes(self):
        head_file = "line1\nline2\nline3\n"
        sugg = {"existing_code": "line2", "improved_code": "line2fixed",
                "relevant_lines_start": 2, "relevant_lines_end": 2}
        assert validate_repaired_suggestion(sugg, head_file) == ""

    def test_wrong_position_fails(self):
        head_file = "line1\nline2\nline3\n"
        sugg = {"existing_code": "line2", "improved_code": "line2fixed",
                "relevant_lines_start": 99, "relevant_lines_end": 99}
        reason = validate_repaired_suggestion(sugg, head_file)
        assert reason != ""
        assert "claimed lines" in reason

    def test_unbalanced_braces_fail(self):
        head_file = "line1\nline2\nline3\n"
        sugg = {"existing_code": "line2", "improved_code": "line2fixed {",
                "relevant_lines_start": 2, "relevant_lines_end": 2}
        reason = validate_repaired_suggestion(sugg, head_file)
        assert reason != ""
        assert "brace" in reason

    def test_never_raises_on_garbage_input(self):
        assert validate_repaired_suggestion({}, "") == "" or isinstance(
            validate_repaired_suggestion({}, ""), str)

from pr_agent.suggestions.deterministic_fix import split_new_dependency


class TestSplitNewDependency:
    def test_splits_cpp_include_into_two_suggestions(self):
        head_file = "#include <mutex>\n\nclass Foo {\npublic:\n    void bar();\n};\n"
        sugg = {
            "relevant_file": "src/foo.cpp",
            "existing_code": "    void bar();",
            "improved_code": "#include <optional>\n    std::optional<int> bar();",
            "one_sentence_summary": "use optional",
            "suggestion_content": "...",
            "label": "correctness",
            "score": 8,
            "relevant_lines_start": 5, "relevant_lines_end": 5,
        }
        result = split_new_dependency(sugg, head_file)
        assert result is not None
        a, b = result
        assert a["existing_code"] == "#include <mutex>"
        assert a["improved_code"] == "#include <mutex>\n#include <optional>"
        assert a["relevant_lines_start"] == a["relevant_lines_end"] == 1
        assert b["existing_code"] == "    void bar();"
        assert b["improved_code"] == "    std::optional<int> bar();"
        # original dict must not be mutated
        assert sugg["improved_code"] == "#include <optional>\n    std::optional<int> bar();"

    def test_anchors_to_line_1_when_no_existing_include(self):
        head_file = "class Foo {\npublic:\n    void bar();\n};\n"
        sugg = {"relevant_file": "src/foo.cpp", "existing_code": "    void bar();",
                "improved_code": "#include <optional>\n    std::optional<int> bar();",
                "relevant_lines_start": 3, "relevant_lines_end": 3}
        a, b = split_new_dependency(sugg, head_file)
        assert a["relevant_lines_start"] == 1
        assert a["existing_code"] == "class Foo {"

    def test_returns_none_when_no_dependency_line_present(self):
        head_file = "#include <mutex>\n\nclass Foo {\n    void bar();\n};\n"
        sugg = {"relevant_file": "src/foo.cpp", "existing_code": "    void bar();",
                "improved_code": "    std::optional<int> bar();",
                "relevant_lines_start": 4, "relevant_lines_end": 4}
        assert split_new_dependency(sugg, head_file) is None

    def test_splits_python_import(self):
        head_file = "import os\nimport sys\n\n\ndef foo():\n    pass\n"
        sugg = {"relevant_file": "src/foo.py", "existing_code": "def foo():\n    pass",
                "improved_code": "import re\ndef foo():\n    return re.compile('x')",
                "relevant_lines_start": 5, "relevant_lines_end": 6}
        a, b = split_new_dependency(sugg, head_file)
        assert a["existing_code"] == "import sys"
        assert a["improved_code"] == "import sys\nimport re"
        assert a["relevant_lines_start"] == 2

from pr_agent.suggestions.deterministic_fix import (detect_conflict_groups,
                                                    prepare_cross_file_context,
                                                    run_deterministic_fix)


class TestPrepareCrossFileContext:
    def test_companion_in_diff_returns_its_head_file(self):
        head_map = {"a.hpp": "class Foo {\n    void bar();\n};\n", "a.cpp": "void Foo::bar() {}\n"}
        sugg = {"companion_file": "a.hpp"}
        result = prepare_cross_file_context(sugg, set(head_map.keys()), head_map)
        assert result == {"needs_tier2": False, "companion_head_file": head_map["a.hpp"]}

    def test_companion_not_in_diff_needs_tier2(self):
        result = prepare_cross_file_context({"companion_file": "a.hpp"}, {"a.cpp"}, {"a.cpp": "x"})
        assert result == {"needs_tier2": True, "companion_head_file": None}

    def test_missing_companion_needs_tier2(self):
        result = prepare_cross_file_context({"companion_file": ""}, {"a.cpp"}, {"a.cpp": "x"})
        assert result["needs_tier2"] is True


class TestDetectConflictGroups:
    def test_two_suggestions_adding_same_declaration_conflict(self):
        head_file = "\n".join(["class Foo {", "public:", "    void bar();", "private:", "};"])
        head_map = {"a.hpp": head_file}
        s1 = {"relevant_file": "a.hpp", "improved_code": "private:\n    int x_;",
              "relevant_lines_start": 4, "relevant_lines_end": 4}
        s2 = {"relevant_file": "a.hpp", "improved_code": "private:\n    int x_;",
              "relevant_lines_start": 4, "relevant_lines_end": 4}
        groups = detect_conflict_groups([s1, s2], head_map)
        assert groups == [[0, 1]]

    def test_split_halves_of_the_same_origin_never_conflict(self):
        # Regression test: two suggestions produced by splitting ONE original
        # suggestion (e.g. split_new_dependency) sit close together (often
        # within the default adjacency window) but must never be flagged as
        # conflicting with each other.
        head_map = {"a.cpp": "#include <mutex>\n\nclass Foo {\n    void bar();\n};\n"}
        a = {"relevant_file": "a.cpp", "improved_code": "#include <mutex>\n#include <optional>",
             "relevant_lines_start": 1, "relevant_lines_end": 1, "_origin_id": 0}
        b = {"relevant_file": "a.cpp", "improved_code": "    std::optional<int> bar();",
             "relevant_lines_start": 4, "relevant_lines_end": 4, "_origin_id": 0}
        assert detect_conflict_groups([a, b], head_map) == []


class TestRunDeterministicFix:
    def test_clean_suggestion_passes_through_as_resolved(self):
        head_map = {"a.cpp": "line1\nline2\nline3\n"}
        sugg = {"relevant_file": "a.cpp", "existing_code": "line2", "improved_code": "line2fixed",
                "structural_issue": "none", "relevant_lines_start": 2, "relevant_lines_end": 2}
        resolved, tasks = run_deterministic_fix(head_map, [sugg])
        assert len(resolved) == 1 and not tasks
        assert resolved[0]["resolved_by_stage"] == "reflect_pass"
        assert "_origin_id" not in resolved[0]  # internal bookkeeping key must not leak

    def test_existing_mismatch_gets_fuzzy_repaired(self):
        head_file = "\n".join(["a", "b", "    auto it = monitors_.find(topic);", "    do_x();", "e"])
        head_map = {"a.cpp": head_file}
        sugg = {"relevant_file": "a.cpp", "existing_code": "auto it = monitors_.find(topic);\ndo_x();",
                "improved_code": "auto it = monitors_.find(topic);\ndo_y();",
                "structural_issue": "none", "relevant_lines_start": 99, "relevant_lines_end": 99}
        resolved, tasks = run_deterministic_fix(head_map, [sugg])
        assert len(resolved) == 1 and not tasks
        assert resolved[0]["relevant_lines_start"] == 3
        assert resolved[0]["resolved_by_stage"] == "deterministic_fix"

    def test_unfixable_existing_mismatch_becomes_a_task(self):
        head_map = {"a.cpp": "a\nb\nc\nd\ne"}
        sugg = {"relevant_file": "a.cpp", "existing_code": "totally_unrelated_code_xyz();",
                "improved_code": "totally_unrelated_code_fixed();", "structural_issue": "none",
                "relevant_lines_start": 1, "relevant_lines_end": 1}
        resolved, tasks = run_deterministic_fix(head_map, [sugg])
        assert not resolved and len(tasks) == 1
        assert tasks[0]["structural_issue"] == "existing_mismatch"
        assert tasks[0]["kind"] == "single"
        assert "_origin_id" not in tasks[0]["members"][0]

    def test_new_dependency_split_both_halves_resolved(self):
        head_file = "#include <mutex>\n\nclass Foo {\n    void bar();\n};\n"
        head_map = {"a.cpp": head_file}
        sugg = {"relevant_file": "a.cpp", "existing_code": "    void bar();",
                "improved_code": "#include <optional>\n    std::optional<int> bar();",
                "structural_issue": "new_dependency", "relevant_lines_start": 4, "relevant_lines_end": 4}
        resolved, tasks = run_deterministic_fix(head_map, [sugg])
        assert len(resolved) == 2 and not tasks
        assert all(s["resolved_by_stage"] == "deterministic_fix" for s in resolved)

    def test_cross_file_companion_in_diff_becomes_task_with_context(self):
        head_map = {"a.hpp": "class Foo {\n    void bar();\n};\n", "a.cpp": "void Foo::bar() {}\n"}
        sugg = {"relevant_file": "a.cpp", "existing_code": "void Foo::bar() {}",
                "improved_code": "void Foo::bar() { log(); }", "structural_issue": "cross_file",
                "companion_file": "a.hpp", "relevant_lines_start": 1, "relevant_lines_end": 1}
        resolved, tasks = run_deterministic_fix(head_map, [sugg])
        assert not resolved and len(tasks) == 1
        assert tasks[0]["needs_tier2"] is False
        assert tasks[0]["companion_head_file"] == head_map["a.hpp"]

    def test_cross_file_companion_not_in_diff_needs_tier2(self):
        head_map = {"a.cpp": "void Foo::bar() {}\n"}
        sugg = {"relevant_file": "a.cpp", "existing_code": "void Foo::bar() {}",
                "improved_code": "void Foo::bar() { log(); }", "structural_issue": "cross_file",
                "companion_file": "a.hpp", "relevant_lines_start": 1, "relevant_lines_end": 1}
        resolved, tasks = run_deterministic_fix(head_map, [sugg])
        assert not resolved and len(tasks) == 1
        assert tasks[0]["needs_tier2"] is True
        assert tasks[0]["companion_head_file"] is None

    def test_incomplete_patch_becomes_task(self):
        head_map = {"a.cpp": "line1\nline2\n"}
        sugg = {"relevant_file": "a.cpp", "existing_code": "line1", "improved_code": "line1\n...",
                "structural_issue": "incomplete_patch", "relevant_lines_start": 1, "relevant_lines_end": 1}
        resolved, tasks = run_deterministic_fix(head_map, [sugg])
        assert not resolved and len(tasks) == 1 and tasks[0]["structural_issue"] == "incomplete_patch"

    def test_conflicting_pair_becomes_a_merged_task(self):
        head_file = "\n".join(["class Foo {", "public:", "    void bar();", "private:", "};"])
        head_map = {"a.hpp": head_file}
        s1 = {"relevant_file": "a.hpp", "existing_code": "private:", "improved_code": "private:\n    int x_;",
              "structural_issue": "none", "relevant_lines_start": 4, "relevant_lines_end": 4}
        s2 = {"relevant_file": "a.hpp", "existing_code": "private:", "improved_code": "private:\n    int x_;",
              "structural_issue": "none", "relevant_lines_start": 4, "relevant_lines_end": 4}
        resolved, tasks = run_deterministic_fix(head_map, [s1, s2])
        assert not resolved
        assert len(tasks) == 1 and tasks[0]["kind"] == "merged" and len(tasks[0]["members"]) == 2

    def test_never_raises_on_a_malformed_suggestion(self):
        head_map = {"a.cpp": "line1\n"}
        # missing every expected key
        resolved, tasks = run_deterministic_fix(head_map, [{}])
        assert isinstance(resolved, list) and isinstance(tasks, list)


# --------------------------------------------------------------------------- #
# __new hunk__ diff fixtures for normalize_final_position / apply_final_normalization
# --------------------------------------------------------------------------- #
# 7-line file: blank at line 3, snippet at lines 4-6, diff change at line 5.
# Mirrors MR !518: model claims start=3 (blank), actual start is 4.
_MR518_HEAD = "\n".join([
    "context_1",     # 1
    "context_2",     # 2
    "",              # 3 - blank (model erroneously claims this as start)
    "snippet_start", # 4 - actual first line of existing_code
    "snippet_mid",   # 5 - the line that was changed in this PR
    "snippet_end",   # 6
    "context_7",     # 7
])

_MR518_DIFF = (
    "## File: 'src/mr518.cpp'\n"
    "@@ ... @@\n"
    "__new hunk__\n"
    "2  context_2\n"
    "3  \n"
    "4  snippet_start\n"
    "5 +snippet_mid\n"
    "6  snippet_end\n"
    "7  context_7\n"
    "__old hunk__\n"
    " context_2\n"
    " \n"
    " snippet_start\n"
    "-old_snippet_mid\n"
    " snippet_end\n"
    " context_7\n"
)


from pr_agent.suggestions.deterministic_fix import (apply_final_normalization,
                                                    normalize_final_position)


class TestNormalizeFinalPosition:
    """normalize_final_position: exact unique match in head_file + new-side hunk overlap."""

    def test_mr518_blank_line_start_corrected(self):
        """MR!518: model claims relevant_lines_start=3 (blank), actual snippet starts at line 4.
        The diff hunk includes lines 4-6 so the suggestion should be accepted with start=4."""
        sugg = {
            "relevant_file": "src/mr518.cpp",
            "existing_code": "snippet_start\nsnippet_mid\nsnippet_end",
            "improved_code": "snippet_start\nbetter_mid\nsnippet_end",
            "relevant_lines_start": 3,  # wrong - model said line 3 (blank)
            "relevant_lines_end": 6,
        }
        fixed, reason = normalize_final_position(sugg, _MR518_HEAD, _MR518_DIFF)
        assert reason == "", f"unexpected rejection: {reason}"
        assert fixed is not None
        assert fixed["relevant_lines_start"] == 4
        assert fixed["relevant_lines_end"] == 6
        assert sugg["relevant_lines_start"] == 3  # original must not be mutated

    def test_no_match_in_head_file_rejected(self):
        """existing_code not found anywhere in head_file → reject."""
        sugg = {
            "relevant_file": "src/mr518.cpp",
            "existing_code": "totally_absent_symbol_xyz();",
            "improved_code": "something_else();",
            "relevant_lines_start": 4, "relevant_lines_end": 4,
        }
        fixed, reason = normalize_final_position(sugg, _MR518_HEAD, _MR518_DIFF)
        assert fixed is None
        assert reason != ""

    def test_duplicate_match_in_head_file_rejected(self):
        """existing_code appears at two distinct locations in head_file → ambiguous → reject."""
        head_dup = "foo();\nbar();\nfoo();\nbaz();\n"
        diff_dup = (
            "## File: 'src/dup.cpp'\n"
            "__new hunk__\n"
            "1 +foo();\n"
            "2  bar();\n"
            "3 +foo();\n"
            "4  baz();\n"
        )
        sugg = {
            "relevant_file": "src/dup.cpp",
            "existing_code": "foo();",
            "improved_code": "foo_v2();",
            "relevant_lines_start": 1, "relevant_lines_end": 1,
        }
        fixed, reason = normalize_final_position(sugg, head_dup, diff_dup)
        assert fixed is None
        assert reason != ""

    def test_not_in_new_side_hunk_rejected(self):
        """existing_code found uniquely in head_file but outside all new-side diff hunks → reject."""
        head_file = "line_1\nline_2\nline_3\n"
        # diff changes only line_3; line_2 is not in any new-side hunk range
        diff = (
            "## File: 'src/a.cpp'\n"
            "__new hunk__\n"
            "3 +line_3\n"
            "__old hunk__\n"
            "-old_line_3\n"
        )
        sugg = {
            "relevant_file": "src/a.cpp",
            "existing_code": "line_2",
            "improved_code": "line_2_fixed",
            "relevant_lines_start": 2, "relevant_lines_end": 2,
        }
        fixed, reason = normalize_final_position(sugg, head_file, diff)
        assert fixed is None
        assert reason != ""

    def test_rejects_when_no_existing_code(self):
        sugg = {
            "relevant_file": "src/a.cpp", "existing_code": "",
            "improved_code": "new_code();", "relevant_lines_start": 1, "relevant_lines_end": 1,
        }
        fixed, reason = normalize_final_position(sugg, "line_1\n", _MR518_DIFF)
        assert fixed is None
        assert "existing_code" in reason

    def test_rejects_when_no_head_file(self):
        sugg = {
            "relevant_file": "src/a.cpp", "existing_code": "foo();",
            "improved_code": "bar();", "relevant_lines_start": 1, "relevant_lines_end": 1,
        }
        fixed, reason = normalize_final_position(sugg, "", _MR518_DIFF)
        assert fixed is None
        assert "head_file" in reason

    def test_rejects_when_no_diff(self):
        head_file = "line_1\nline_2\nline_3\n"
        sugg = {
            "relevant_file": "src/a.cpp", "existing_code": "line_2",
            "improved_code": "line_2_fixed",
            "relevant_lines_start": 99, "relevant_lines_end": 99,
        }
        fixed, reason = normalize_final_position(sugg, head_file, "")
        assert fixed is None
        assert "diff" in reason

    def test_never_raises_on_garbage_input(self):
        result = normalize_final_position({}, "", "")
        assert isinstance(result, tuple) and len(result) == 2


class TestApplyFinalNormalization:
    """apply_final_normalization: batch normalization over resolved suggestions."""

    def test_suggestion_in_hunk_passes_with_corrected_lines(self):
        head_map = {"src/mr518.cpp": _MR518_HEAD}
        sugg = {
            "relevant_file": "src/mr518.cpp",
            "existing_code": "snippet_start\nsnippet_mid\nsnippet_end",
            "improved_code": "snippet_start\nbetter_mid\nsnippet_end",
            "relevant_lines_start": 3, "relevant_lines_end": 6,  # model wrong start
        }
        still_resolved, rejected = apply_final_normalization([sugg], head_map, _MR518_DIFF)
        assert len(still_resolved) == 1 and not rejected
        assert still_resolved[0]["relevant_lines_start"] == 4

    def test_suggestion_outside_hunk_rejected(self):
        head_file = "line_1\nline_2\nline_3\n"
        head_map = {"src/a.cpp": head_file}
        diff = "## File: 'src/a.cpp'\n__new hunk__\n3 +line_3\n__old hunk__\n-old_3\n"
        sugg = {
            "relevant_file": "src/a.cpp",
            "existing_code": "line_2",
            "improved_code": "line_2_fixed",
            "relevant_lines_start": 2, "relevant_lines_end": 2,
        }
        still_resolved, rejected = apply_final_normalization([sugg], head_map, diff)
        assert not still_resolved and len(rejected) == 1

    def test_empty_diff_rejects_all(self):
        head_map = {"src/a.cpp": "line_1\nline_2\n"}
        sugg = {
            "relevant_file": "src/a.cpp", "existing_code": "line_2",
            "improved_code": "line_2_new", "relevant_lines_start": 2, "relevant_lines_end": 2,
        }
        still_resolved, rejected = apply_final_normalization([sugg], head_map, "")
        assert not still_resolved
        assert rejected == [sugg]

    def test_unexpected_error_rejects_suggestion(self, monkeypatch):
        import pr_agent.suggestions.deterministic_fix as deterministic_fix

        sugg = {
            "relevant_file": "src/a.cpp", "existing_code": "line_2",
            "improved_code": "line_2_new", "relevant_lines_start": 2, "relevant_lines_end": 2,
        }
        monkeypatch.setattr(
            deterministic_fix, "normalize_final_position", lambda *_args, **_kwargs: 1 / 0)

        still_resolved, rejected = apply_final_normalization(
            [sugg], {"src/a.cpp": "line_1\nline_2\n"}, _MR518_DIFF)

        assert not still_resolved
        assert rejected == [sugg]
