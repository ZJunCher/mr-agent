"""Tests for the Phase 2 inline self-check (inline_selfcheck.py).

Two layers:
- 2A single self-check: one lightweight LLM call per surviving candidate;
  any failing field blocks the suggestion (fail-closed on error).
- 2B batch de-conflict: when >=2 candidates in the same file collide
  (overlapping/adjacent lines, both add declarations, or share a new
  identifier), an LLM proposes keep/rewrite/drop; rewritten products must
  re-pass Phase1 + 2A before publishing. On LLM error the group falls back
  to keeping only the top-scored suggestion (fail-closed).

Includes the real MR !432 case: two suggestions each adding the same class
member declaration, which stacked into a redeclaration compile error.
"""
import asyncio
import json

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.suggestions.inline_selfcheck import (
    deconflict,
    detect_conflict_groups,
    run_phase2,
    run_selfcheck,
    selfcheck_single,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
CPP_HEAD_FILE = "\n".join(
    [
        "#include <mutex>",              # 1
        "#include <unordered_map>",      # 2
        "",                              # 3
        "class RecordingStatistics {",   # 4
        "public:",                       # 5
        "    void startSession();",      # 6
        "    void stopSession();",       # 7
        "private:",                      # 8
        "    rclcpp::Logger logger_;",   # 9
        "    std::mutex topics_mutex_;", # 10
        "};",                            # 11
    ]
)

ALL_TRUE = json.dumps(
    {
        "complete_fix": True,
        "self_consistent": True,
        "safe_to_apply": True,
        "format_plausible": True,
        "reason": "ok",
    }
)


def _sugg(**kwargs):
    base = {
        "relevant_file": "src/recording_statistics.hpp",
        "existing_code": "    std::mutex topics_mutex_;",
        "improved_code": "    std::mutex topics_mutex_;\n    std::atomic<bool> active_{false};",
        "suggestion_content": "add an active flag",
        "one_sentence_summary": "add active flag",
        "relevant_lines_start": 10,
        "relevant_lines_end": 10,
        "score": 8,
        "label": "possible bug",
    }
    base.update(kwargs)
    return base


class MockAI:
    """Routes to a self-check or de-conflict response by inspecting the system
    prompt. Either response may be an Exception to simulate an LLM failure."""

    def __init__(self, selfcheck=ALL_TRUE, deconflict=None):
        self._selfcheck = selfcheck
        self._deconflict = deconflict
        self.calls = []

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        self.calls.append({"model": model, "system": system, "user": user})
        resp = self._deconflict if "conflict" in (system or "").lower() else self._selfcheck
        if isinstance(resp, Exception):
            raise resp
        return resp, "stop"


def _provider(files):
    """files: list of (filename, head_file)."""
    import types

    diff_files = []
    for filename, head in files:
        f = types.SimpleNamespace(filename=filename, head_file=head)
        diff_files.append(f)
    provider = types.SimpleNamespace()
    provider.diff_files = diff_files
    provider.get_diff_files = lambda: diff_files
    return provider


@pytest.fixture(autouse=True)
def _defaults():
    s = get_settings()
    s.set("pr_code_suggestions.inline_selfcheck_enabled", True)
    s.set("pr_code_suggestions.inline_selfcheck_model", "test-model")
    s.set("pr_code_suggestions.inline_selfcheck_fail_action", "skip")
    s.set("pr_code_suggestions.inline_selfcheck_max_candidates", 5)
    s.set("pr_code_suggestions.inline_conflict_check_enabled", True)
    s.set("pr_code_suggestions.inline_conflict_adjacency_lines", 3)
    s.set("pr_code_suggestions.inline_conflict_fail_action", "keep_top1")
    # keep gate deterministic
    s.set("pr_code_suggestions.inline_gate_enabled", True)


# --------------------------------------------------------------------------- #
# 2A: single self-check
# --------------------------------------------------------------------------- #
class TestSelfcheckSingle:
    def test_all_true_passes(self):
        ai = MockAI(selfcheck=ALL_TRUE)
        reason = asyncio.run(selfcheck_single(ai, _sugg(), CPP_HEAD_FILE))
        assert reason is None
        assert len(ai.calls) == 1

    def test_complete_fix_false_blocks(self):
        resp = json.dumps({"complete_fix": False, "self_consistent": True,
                           "safe_to_apply": True, "format_plausible": True, "reason": "x"})
        reason = asyncio.run(selfcheck_single(MockAI(selfcheck=resp), _sugg(), CPP_HEAD_FILE))
        assert reason == "selfcheck_complete_fix"

    def test_safe_to_apply_false_blocks(self):
        resp = json.dumps({"complete_fix": True, "self_consistent": True,
                           "safe_to_apply": False, "format_plausible": True, "reason": "x"})
        reason = asyncio.run(selfcheck_single(MockAI(selfcheck=resp), _sugg(), CPP_HEAD_FILE))
        assert reason == "selfcheck_safe_to_apply"

    def test_format_plausible_false_blocks(self):
        resp = json.dumps({"complete_fix": True, "self_consistent": True,
                           "safe_to_apply": True, "format_plausible": False, "reason": "x"})
        reason = asyncio.run(selfcheck_single(MockAI(selfcheck=resp), _sugg(), CPP_HEAD_FILE))
        assert reason == "selfcheck_format_plausible"

    def test_json_in_code_fence_is_parsed(self):
        resp = f"```json\n{ALL_TRUE}\n```"
        reason = asyncio.run(selfcheck_single(MockAI(selfcheck=resp), _sugg(), CPP_HEAD_FILE))
        assert reason is None

    def test_unparseable_response_fails_closed(self):
        reason = asyncio.run(selfcheck_single(MockAI(selfcheck="not json at all"), _sugg(), CPP_HEAD_FILE))
        assert reason == "selfcheck_error"

    def test_llm_exception_fails_closed(self):
        reason = asyncio.run(selfcheck_single(MockAI(selfcheck=RuntimeError("boom")), _sugg(), CPP_HEAD_FILE))
        assert reason == "selfcheck_error"

    def test_fail_action_pass_lets_error_through(self):
        get_settings().set("pr_code_suggestions.inline_selfcheck_fail_action", "pass")
        reason = asyncio.run(selfcheck_single(MockAI(selfcheck=RuntimeError("boom")), _sugg(), CPP_HEAD_FILE))
        assert reason is None


class TestRunSelfcheck:
    def test_disabled_passes_all_without_llm(self):
        get_settings().set("pr_code_suggestions.inline_selfcheck_enabled", False)
        ai = MockAI()
        passed, blocked = asyncio.run(run_selfcheck(ai, _provider([]), [_sugg(), _sugg()]))
        assert len(passed) == 2 and blocked == []
        assert ai.calls == []

    def test_blocks_the_failing_one(self):
        good = _sugg(relevant_lines_start=10, relevant_lines_end=10)
        # route both through the same mock -> all fail complete_fix
        resp = json.dumps({"complete_fix": False, "self_consistent": True,
                           "safe_to_apply": True, "format_plausible": True, "reason": "x"})
        provider = _provider([("src/recording_statistics.hpp", CPP_HEAD_FILE)])
        passed, blocked = asyncio.run(run_selfcheck(MockAI(selfcheck=resp), provider, [good]))
        assert passed == []
        assert blocked and blocked[0][1] == "selfcheck_complete_fix"

    def test_max_candidates_caps_llm_calls(self):
        get_settings().set("pr_code_suggestions.inline_selfcheck_max_candidates", 2)
        provider = _provider([("src/recording_statistics.hpp", CPP_HEAD_FILE)])
        ai = MockAI(selfcheck=ALL_TRUE)
        suggs = [_sugg() for _ in range(4)]
        passed, blocked = asyncio.run(run_selfcheck(ai, provider, suggs))
        # only 2 self-checked; the rest pass through unchecked (not blocked)
        assert len(ai.calls) == 2
        assert len(passed) == 4 and blocked == []


# --------------------------------------------------------------------------- #
# 2B: conflict detection (local prefilter, no LLM)
# --------------------------------------------------------------------------- #
class TestConflictDetection:
    def test_mr432_two_member_declarations_conflict(self):
        # Both suggestions add a new member declaration to the same class.
        a = _sugg(relevant_lines_start=9, relevant_lines_end=9,
                  improved_code="    rclcpp::Logger logger_;\n    rclcpp::Node* node_;")
        b = _sugg(relevant_lines_start=10, relevant_lines_end=10,
                  improved_code="    std::mutex topics_mutex_;\n    rclcpp::Node* node_;")
        groups = detect_conflict_groups([a, b], {"src/recording_statistics.hpp": CPP_HEAD_FILE})
        assert groups == [[0, 1]]

    def test_non_overlapping_unrelated_edits_no_conflict(self):
        a = _sugg(relevant_lines_start=6, relevant_lines_end=6,
                  improved_code="    void startSession() noexcept;")
        b = _sugg(relevant_lines_start=30, relevant_lines_end=30,
                  improved_code="    return count_ + 1;")
        groups = detect_conflict_groups([a, b], {"src/recording_statistics.hpp": CPP_HEAD_FILE})
        assert groups == []

    def test_adjacent_lines_within_threshold_conflict(self):
        a = _sugg(relevant_lines_start=10, relevant_lines_end=10, improved_code="    x = 1;")
        b = _sugg(relevant_lines_start=12, relevant_lines_end=12, improved_code="    y = 2;")
        groups = detect_conflict_groups([a, b], {"src/recording_statistics.hpp": CPP_HEAD_FILE})
        assert groups == [[0, 1]]

    def test_different_files_never_conflict(self):
        a = _sugg(relevant_file="a.hpp", relevant_lines_start=10, relevant_lines_end=10,
                  improved_code="    int a_;")
        b = _sugg(relevant_file="b.hpp", relevant_lines_start=10, relevant_lines_end=10,
                  improved_code="    int b_;")
        groups = detect_conflict_groups([a, b], {})
        assert groups == []


# --------------------------------------------------------------------------- #
# 2B: de-conflict orchestration
# --------------------------------------------------------------------------- #
class TestDeconflict:
    def test_no_conflict_signal_skips_llm(self):
        a = _sugg(relevant_lines_start=6, relevant_lines_end=6,
                  improved_code="    void startSession() noexcept;")
        b = _sugg(relevant_lines_start=30, relevant_lines_end=30,
                  improved_code="    return count_ + 1;")
        provider = _provider([("src/recording_statistics.hpp", CPP_HEAD_FILE)])
        ai = MockAI()
        passed, blocked = asyncio.run(deconflict(ai, provider, [a, b]))
        assert len(passed) == 2 and blocked == []
        assert ai.calls == []

    def test_drop_action_blocks_with_reason(self):
        a = _sugg(relevant_lines_start=9, relevant_lines_end=9,
                  improved_code="    rclcpp::Logger logger_;\n    rclcpp::Node* node_;")
        b = _sugg(relevant_lines_start=10, relevant_lines_end=10,
                  improved_code="    std::mutex topics_mutex_;\n    rclcpp::Node* node_;")
        resp = json.dumps({
            "has_conflict": True,
            "resolved": [
                {"id": "S1", "action": "keep"},
                {"id": "S2", "action": "drop", "reason": "duplicate node_ decl"},
            ],
        })
        provider = _provider([("src/recording_statistics.hpp", CPP_HEAD_FILE)])
        passed, blocked = asyncio.run(deconflict(MockAI(deconflict=resp), provider, [a, b]))
        assert len(passed) == 1 and passed[0] is a
        assert len(blocked) == 1 and blocked[0][0] is b and blocked[0][1] == "conflict_dropped"

    def test_rewrite_passes_recheck_and_publishes(self):
        a = _sugg(relevant_lines_start=9, relevant_lines_end=9,
                  improved_code="    rclcpp::Logger logger_;\n    rclcpp::Node* node_;")
        b = _sugg(relevant_lines_start=10, relevant_lines_end=10,
                  improved_code="    std::mutex topics_mutex_;\n    rclcpp::Node* node_;")
        # S2 rewritten to only its unique change (drop the duplicate node_)
        resp = json.dumps({
            "has_conflict": True,
            "resolved": [
                {"id": "S1", "action": "keep"},
                {"id": "S2", "action": "rewrite",
                 "improved_code": "    std::mutex topics_mutex_;",
                 "relevant_lines_start": 10, "relevant_lines_end": 10,
                 "reason": "drop duplicate node_"},
            ],
        })
        provider = _provider([("src/recording_statistics.hpp", CPP_HEAD_FILE)])
        # selfcheck (recheck) returns all-true; deconflict returns resp
        passed, blocked = asyncio.run(deconflict(MockAI(selfcheck=ALL_TRUE, deconflict=resp), provider, [a, b]))
        assert blocked == []
        rewritten = [s for s in passed if s.get("rewritten")]
        assert len(rewritten) == 1
        assert rewritten[0]["improved_code"] == "    std::mutex topics_mutex_;"

    def test_rewrite_failing_recheck_is_blocked(self):
        a = _sugg(relevant_lines_start=9, relevant_lines_end=9,
                  improved_code="    rclcpp::Logger logger_;\n    rclcpp::Node* node_;")
        b = _sugg(relevant_lines_start=10, relevant_lines_end=10,
                  improved_code="    std::mutex topics_mutex_;\n    rclcpp::Node* node_;")
        # rewrite reintroduces a brand-new symbol -> Phase1 gate (new_symbol) rejects it
        resp = json.dumps({
            "has_conflict": True,
            "resolved": [
                {"id": "S1", "action": "keep"},
                {"id": "S2", "action": "rewrite",
                 "improved_code": "    resetEverything(brand_new_symbol_);",
                 "relevant_lines_start": 10, "relevant_lines_end": 10,
                 "reason": "bad"},
            ],
        })
        provider = _provider([("src/recording_statistics.hpp", CPP_HEAD_FILE)])
        passed, blocked = asyncio.run(deconflict(MockAI(selfcheck=ALL_TRUE, deconflict=resp), provider, [a, b]))
        assert passed == [a]
        assert len(blocked) == 1 and blocked[0][0] is b and blocked[0][1] == "conflict_rewrite_failed"

    def test_llm_error_keeps_top_scored_only(self):
        a = _sugg(relevant_lines_start=9, relevant_lines_end=9, score=6,
                  improved_code="    rclcpp::Logger logger_;\n    rclcpp::Node* node_;")
        b = _sugg(relevant_lines_start=10, relevant_lines_end=10, score=9,
                  improved_code="    std::mutex topics_mutex_;\n    rclcpp::Node* node_;")
        provider = _provider([("src/recording_statistics.hpp", CPP_HEAD_FILE)])
        passed, blocked = asyncio.run(deconflict(MockAI(deconflict=RuntimeError("boom")), provider, [a, b]))
        assert passed == [b]  # higher score kept
        assert len(blocked) == 1 and blocked[0][0] is a and blocked[0][1] == "conflict_selfcheck_error"


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #
class TestRunPhase2:
    def test_selfcheck_then_deconflict(self):
        # one candidate fails 2A, the surviving two collide and one is dropped
        bad = _sugg(relevant_lines_start=6, relevant_lines_end=6,
                    improved_code="    void startSession() noexcept;")
        a = _sugg(relevant_lines_start=9, relevant_lines_end=9,
                  improved_code="    rclcpp::Logger logger_;\n    rclcpp::Node* node_;")
        b = _sugg(relevant_lines_start=10, relevant_lines_end=10,
                  improved_code="    std::mutex topics_mutex_;\n    rclcpp::Node* node_;")

        # selfcheck: block the "bad" one (its improved_code has "noexcept"),
        # pass the others. Route via a small stateful mock.
        deconf = json.dumps({
            "has_conflict": True,
            "resolved": [
                {"id": "S1", "action": "keep"},
                {"id": "S2", "action": "drop", "reason": "dup"},
            ],
        })

        class RoutingAI:
            def __init__(self):
                self.calls = []

            async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
                self.calls.append(user)
                if "conflict" in system.lower():
                    return deconf, "stop"
                if "noexcept" in user:
                    return json.dumps({"complete_fix": False, "self_consistent": True,
                                       "safe_to_apply": True, "format_plausible": True,
                                       "reason": "incomplete"}), "stop"
                return ALL_TRUE, "stop"

        provider = _provider([("src/recording_statistics.hpp", CPP_HEAD_FILE)])
        passed, blocked = asyncio.run(run_phase2(provider, [bad, a, b], ai_handler=RoutingAI()))
        reasons = sorted(r for _, r in blocked)
        assert reasons == ["conflict_dropped", "selfcheck_complete_fix"]
        assert passed == [a]
