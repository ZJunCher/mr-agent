"""Tests for the Phase 1 inline suggestion heuristic gate (inline_gate.py).

Each gate check (G1-G5) has positive (blocked) and negative (passes) cases,
including the real MR !430 case: a suggestion referencing a class member
(frame_monitors_mutex_) that does not exist in the target file.
"""
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.suggestions.inline_gate import (
    check_cross_file,
    check_incomplete_patch,
    check_new_dependency,
    check_new_symbol,
    check_speculative,
    gate_suggestions,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
CPP_HEAD_FILE = """\
#include <mutex>
#include <unordered_map>

class FrameMonitor {
public:
    void recordMessage(const std::string& topic) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = monitors_.find(topic);
        if (it == monitors_.end()) {
            monitors_.emplace(topic, MonitorState{});
        }
        monitors_[topic].count++;
    }

private:
    std::mutex mutex_;
    std::unordered_map<std::string, MonitorState> monitors_;
};
"""


def _sugg(**overrides):
    base = {
        "relevant_file": "src/frame_monitor.cpp",
        "label": "possible issue",
        "score": 8,
        "suggestion_content": "修复空指针问题",
        "one_sentence_summary": "判空修复",
        "existing_code": "auto it = monitors_.find(topic);\nif (it == monitors_.end()) {\n    monitors_.emplace(topic, MonitorState{});\n}",
        "improved_code": "auto it = monitors_.find(topic);\nif (it == monitors_.end()) {\n    monitors_.try_emplace(topic);\n}",
        "relevant_lines_start": 8,
        "relevant_lines_end": 11,
    }
    base.update(overrides)
    return base


def _set_defaults():
    s = get_settings()
    s.set("pr_code_suggestions.inline_gate_enabled", True)
    s.set("pr_code_suggestions.inline_gate_check_new_symbol", True)
    s.set("pr_code_suggestions.inline_gate_check_new_dependency", True)
    s.set("pr_code_suggestions.inline_gate_check_cross_file", True)
    s.set("pr_code_suggestions.inline_gate_check_incomplete", True)
    s.set("pr_code_suggestions.inline_gate_check_speculative", True)
    s.set("pr_code_suggestions.inline_gate_speculative_labels", ["performance"])


@pytest.fixture(autouse=True)
def defaults():
    _set_defaults()


# --------------------------------------------------------------------------- #
# G1: new symbol
# --------------------------------------------------------------------------- #
class TestNewSymbol:
    def test_mr430_new_member_variable_blocked(self):
        # Real MR !430 case: suggestion uses frame_monitors_mutex_ which the
        # class does not have.
        sugg = _sugg(
            improved_code=(
                "std::lock_guard<std::mutex> lock(frame_monitors_mutex_);\n"
                "auto it = monitors_.find(topic);"
            ),
        )
        assert check_new_symbol(sugg, CPP_HEAD_FILE) == "new_symbol"

    def test_existing_member_passes(self):
        sugg = _sugg(
            improved_code=(
                "std::lock_guard<std::mutex> lock(mutex_);\n"
                "monitors_.try_emplace(topic);"
            ),
        )
        assert check_new_symbol(sugg, CPP_HEAD_FILE) is None

    def test_locally_declared_symbol_passes(self):
        # Symbol declared inside improved_code itself is fine.
        sugg = _sugg(
            improved_code=(
                "auto new_count = monitors_[topic].count + 1;\n"
                "monitors_[topic].count = new_count;"
            ),
        )
        assert check_new_symbol(sugg, CPP_HEAD_FILE) is None

    def test_new_function_call_blocked(self):
        sugg = _sugg(improved_code="resetMonitorState(topic);")
        assert check_new_symbol(sugg, CPP_HEAD_FILE) == "new_symbol"

    def test_std_and_keywords_ignored(self):
        sugg = _sugg(
            improved_code=(
                "if (topic.empty()) {\n"
                "    return;\n"
                "}\n"
                "std::lock_guard<std::mutex> lock(mutex_);"
            ),
        )
        assert check_new_symbol(sugg, CPP_HEAD_FILE) is None

    def test_no_head_file_passes(self):
        # Without file content we cannot judge: don't block (avoid false kill).
        sugg = _sugg(improved_code="whatever_symbol_(x);")
        assert check_new_symbol(sugg, "") is None

    def test_python_self_attribute_blocked(self):
        head = "class A:\n    def __init__(self):\n        self.count = 0\n"
        sugg = _sugg(
            relevant_file="a.py",
            existing_code="self.count += 1",
            improved_code="with self._lock:\n    self.count += 1",
        )
        assert check_new_symbol(sugg, head) == "new_symbol"


# --------------------------------------------------------------------------- #
# G2: new dependency
# --------------------------------------------------------------------------- #
class TestNewDependency:
    def test_new_include_blocked(self):
        sugg = _sugg(
            improved_code="#include <shared_mutex>\nstd::shared_lock lk(mutex_);",
        )
        assert check_new_dependency(sugg, CPP_HEAD_FILE) == "new_dependency"

    def test_existing_include_passes(self):
        sugg = _sugg(
            improved_code="#include <mutex>\nstd::lock_guard<std::mutex> lock(mutex_);",
        )
        assert check_new_dependency(sugg, CPP_HEAD_FILE) is None

    def test_python_new_import_blocked(self):
        head = "import os\n\ndef f():\n    return os.getpid()\n"
        sugg = _sugg(
            relevant_file="a.py",
            improved_code="import threading\nlock = threading.Lock()",
        )
        assert check_new_dependency(sugg, head) == "new_dependency"

    def test_no_dependency_passes(self):
        sugg = _sugg()
        assert check_new_dependency(sugg, CPP_HEAD_FILE) is None


# --------------------------------------------------------------------------- #
# G3: cross file
# --------------------------------------------------------------------------- #
class TestCrossFile:
    def test_mention_other_file_with_modify_verb_blocked(self):
        sugg = _sugg(
            suggestion_content="需要同时修改 frame_monitor.hpp 中的成员声明，然后在此处加锁",
        )
        assert check_cross_file(sugg) == "cross_file"

    def test_plain_mention_without_modify_passes(self):
        sugg = _sugg(
            suggestion_content="该函数在 frame_monitor.hpp 中声明，此处存在越界风险",
        )
        assert check_cross_file(sugg) is None

    def test_same_file_mention_passes(self):
        sugg = _sugg(
            suggestion_content="修改 frame_monitor.cpp 中这一段即可",
        )
        assert check_cross_file(sugg) is None

    def test_english_modify_other_file_blocked(self):
        sugg = _sugg(
            suggestion_content="You should also update utils/helper.py to add the new parameter",
        )
        assert check_cross_file(sugg) == "cross_file"


# --------------------------------------------------------------------------- #
# G4: incomplete patch
# --------------------------------------------------------------------------- #
class TestIncompletePatch:
    def test_ellipsis_marker_blocked(self):
        sugg = _sugg(improved_code="foo();\n// ... 其余代码不变\nbar();")
        assert check_incomplete_patch(sugg, CPP_HEAD_FILE) == "incomplete_patch"

    def test_unbalanced_braces_blocked(self):
        # existing balanced, improved opens a brace it never closes
        sugg = _sugg(
            existing_code="monitors_[topic].count++;",
            improved_code="if (ok) {\n    monitors_[topic].count++;",
        )
        assert check_incomplete_patch(sugg, CPP_HEAD_FILE) == "incomplete_patch"

    def test_same_brace_delta_passes(self):
        # both existing and improved open one brace: replacement keeps structure
        sugg = _sugg(
            existing_code="if (it == monitors_.end()) {",
            improved_code="if (it == monitors_.end() && !topic.empty()) {",
        )
        assert check_incomplete_patch(sugg, CPP_HEAD_FILE) is None

    def test_existing_code_not_in_file_blocked(self):
        sugg = _sugg(existing_code="this line never existed in the file")
        assert check_incomplete_patch(sugg, CPP_HEAD_FILE) == "existing_mismatch"

    def test_existing_code_matches_file_passes(self):
        sugg = _sugg(
            existing_code="auto it = monitors_.find(topic);",
            improved_code="const auto it = monitors_.find(topic);",
        )
        assert check_incomplete_patch(sugg, CPP_HEAD_FILE) is None

    def test_no_head_file_skips_mismatch_check(self):
        sugg = _sugg(existing_code="anything")
        assert check_incomplete_patch(sugg, "") is None


# --------------------------------------------------------------------------- #
# G5: speculative
# --------------------------------------------------------------------------- #
class TestSpeculative:
    def test_performance_label_blocked(self):
        sugg = _sugg(label="performance", suggestion_content="高频回调下锁竞争可能成为瓶颈")
        assert check_speculative(sugg) == "speculative"

    def test_chinese_performance_label_blocked(self):
        sugg = _sugg(label="实时性与性能", suggestion_content="锁竞争瓶颈")
        assert check_speculative(sugg) == "speculative"

    def test_bug_label_passes(self):
        sugg = _sugg(label="possible bug", suggestion_content="解引用空指针")
        assert check_speculative(sugg) is None

    def test_switch_off_allows_performance(self):
        get_settings().set("pr_code_suggestions.inline_gate_check_speculative", False)
        sugg = _sugg(label="performance")
        assert check_speculative(sugg) is None


# --------------------------------------------------------------------------- #
# orchestrator: gate_suggestions
# --------------------------------------------------------------------------- #
def _provider_with_head(head_file, filename="src/frame_monitor.cpp"):
    provider = MagicMock()
    f = MagicMock()
    f.filename = filename
    f.head_file = head_file
    provider.get_diff_files.return_value = [f]
    provider.diff_files = [f]
    return provider


class TestGateSuggestions:
    def test_clean_suggestion_passes(self):
        provider = _provider_with_head(CPP_HEAD_FILE)
        passed, blocked = gate_suggestions(provider, [_sugg()])
        assert len(passed) == 1
        assert blocked == []

    def test_bad_suggestion_blocked_with_reason(self):
        provider = _provider_with_head(CPP_HEAD_FILE)
        bad = _sugg(improved_code="std::lock_guard<std::mutex> lock(frame_monitors_mutex_);")
        passed, blocked = gate_suggestions(provider, [bad])
        assert passed == []
        assert len(blocked) == 1
        assert blocked[0][1] == "new_symbol"

    def test_mixed_suggestions_split(self):
        provider = _provider_with_head(CPP_HEAD_FILE)
        good = _sugg()
        bad = _sugg(label="performance")
        passed, blocked = gate_suggestions(provider, [good, bad])
        assert len(passed) == 1
        assert len(blocked) == 1
        assert blocked[0][1] == "speculative"

    def test_gate_disabled_passes_all(self):
        get_settings().set("pr_code_suggestions.inline_gate_enabled", False)
        provider = _provider_with_head(CPP_HEAD_FILE)
        bad = _sugg(label="performance")
        passed, blocked = gate_suggestions(provider, [bad])
        assert len(passed) == 1
        assert blocked == []

    def test_provider_error_fails_open(self):
        # If we cannot read diff files at all, don't block everything.
        provider = MagicMock()
        provider.get_diff_files.side_effect = RuntimeError("boom")
        provider.diff_files = None
        passed, blocked = gate_suggestions(provider, [_sugg()])
        assert len(passed) == 1
        assert blocked == []

    def test_file_not_in_diff_uses_no_head_file(self):
        # Suggestion for a file we can't find content for: G1/G4 mismatch skip,
        # other checks still apply.
        provider = _provider_with_head(CPP_HEAD_FILE, filename="other.cpp")
        sugg = _sugg(label="performance")
        passed, blocked = gate_suggestions(provider, [sugg])
        assert blocked and blocked[0][1] == "speculative"
