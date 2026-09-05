"""Integration test for PRCodeSuggestions.run_repair_pipeline: the Pipeline-v2
orchestration that wires deterministic_fix (Tier-0) + tier1_repair (Tier-1)
together, as called from prepare_prediction_main.
"""
import asyncio
import json
import types

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

CPP_HEAD_FILE = "\n".join([
    "#include <mutex>",
    "",
    "class Foo {",
    "public:",
    "    void bar();",
    "};",
])

CPP_DIFF = (
    "## File: 'src/foo.cpp'\n"
    "__new hunk__\n"
    "3  class Foo {\n"
    "4  public:\n"
    "5 +    void bar();\n"
    "6  };\n"
    "__old hunk__\n"
    "5 -    void old_bar();\n"
)


def _provider(files):
    diff_files = [types.SimpleNamespace(filename=f, head_file=h) for f, h in files]
    provider = types.SimpleNamespace()
    provider.diff_files = diff_files
    provider.get_diff_files = lambda: diff_files
    return provider


class MockAI:
    def __init__(self, response=None):
        self._response = response
        self.calls = 0

    async def chat_completion(self, model, system, user, temperature=0.1, img_path=None):
        self.calls += 1
        return self._response, "stop"


def _make_tool(git_provider, ai_handler):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = git_provider
    tool.ai_handler = ai_handler
    return tool


class TestRunRepairPipeline:
    def test_clean_suggestion_passes_through_unchanged(self):
        tool = _make_tool(_provider([("src/foo.cpp", CPP_HEAD_FILE)]), MockAI())
        suggestions = [{
            "relevant_file": "src/foo.cpp", "existing_code": "    void bar();",
            "improved_code": "    void bar() const;", "structural_issue": "none",
            "relevant_lines_start": 5, "relevant_lines_end": 5,
        }]
        result = asyncio.run(tool.run_repair_pipeline(suggestions, CPP_DIFF))
        assert len(result["resolved"]) == 1 and not result["pending_tier2"]
        assert result["resolved"][0]["resolved_by_stage"] == "reflect_pass"

    def test_existing_mismatch_repaired_without_any_llm_call(self):
        ai = MockAI()
        tool = _make_tool(_provider([("src/foo.cpp", CPP_HEAD_FILE)]), ai)
        suggestions = [{
            "relevant_file": "src/foo.cpp", "existing_code": "    void bar();",
            "improved_code": "    void bar() const;", "structural_issue": "none",
            "relevant_lines_start": 999, "relevant_lines_end": 999,
        }]
        result = asyncio.run(tool.run_repair_pipeline(suggestions, CPP_DIFF))
        assert len(result["resolved"]) == 1 and not result["pending_tier2"]
        assert result["resolved"][0]["resolved_by_stage"] == "deterministic_fix"
        assert ai.calls == 0  # zero-LLM repair, Tier-1 never invoked

    def test_cross_file_companion_not_in_diff_routes_straight_to_tier2(self):
        ai = MockAI()
        tool = _make_tool(_provider([("src/foo.cpp", CPP_HEAD_FILE)]), ai)
        suggestions = [{
            "relevant_file": "src/foo.cpp", "existing_code": "    void bar();",
            "improved_code": "    void bar() const;", "structural_issue": "cross_file",
            "companion_file": "src/foo.hpp", "relevant_lines_start": 5, "relevant_lines_end": 5,
        }]
        result = asyncio.run(tool.run_repair_pipeline(suggestions, CPP_DIFF))
        assert not result["resolved"]
        assert len(result["pending_tier2"]) == 1
        assert ai.calls == 0  # deterministic_fix routes needs_tier2 tasks around Tier-1 entirely

    def test_tier1_repairs_what_deterministic_fix_could_not(self):
        good_fix = json.dumps({
            "fixable": True,
            "primary": {"existing_code": "    void bar();", "improved_code": "    void bar() const;",
                        "relevant_lines_start": 5, "relevant_lines_end": 5},
        })
        ai = MockAI(response=good_fix)
        tool = _make_tool(_provider([("src/foo.cpp", CPP_HEAD_FILE)]), ai)
        suggestions = [{
            "relevant_file": "src/foo.cpp", "existing_code": "totally_wrong_snippet_xyz();",
            "improved_code": "totally_wrong_snippet_fixed();", "structural_issue": "none",
            "relevant_lines_start": 1, "relevant_lines_end": 1,
        }]
        result = asyncio.run(tool.run_repair_pipeline(suggestions, CPP_DIFF))
        assert len(result["resolved"]) == 1 and not result["pending_tier2"]
        assert result["resolved"][0]["resolved_by_stage"] == "tier1_llm"
        assert ai.calls == 1

    def test_tier1_failure_falls_through_to_pending_tier2(self):
        not_fixable = json.dumps({"fixable": False, "reason": "cannot determine"})
        ai = MockAI(response=not_fixable)
        tool = _make_tool(_provider([("src/foo.cpp", CPP_HEAD_FILE)]), ai)
        suggestions = [{
            "relevant_file": "src/foo.cpp", "existing_code": "totally_wrong_snippet_xyz();",
            "improved_code": "totally_wrong_snippet_fixed();", "structural_issue": "none",
            "relevant_lines_start": 1, "relevant_lines_end": 1,
        }]
        result = asyncio.run(tool.run_repair_pipeline(suggestions, CPP_DIFF))
        assert not result["resolved"]
        assert len(result["pending_tier2"]) == 1

    def test_never_raises_with_empty_input(self):
        tool = _make_tool(_provider([]), MockAI())
        result = asyncio.run(tool.run_repair_pipeline([]))
        assert result == {"resolved": [], "pending_tier2": []}

    def test_mr518_tier1_repair_ignores_model_line_numbers(self):
        head_file = "\n".join([
            "#include \"ssm.hpp\"",
            "}  // namespace",
            "",
            "SSM::SSM() {",
            "    auto node = server.node();",
            "    supplemental_.setCallback(on_data);",
            "}",
        ])
        diff = (
            "## File: 'src/ssm.cpp'\n"
            "__new hunk__\n"
            "4  SSM::SSM() {\n"
            "5 +    auto node = server.node();\n"
            "6 +    supplemental_.setCallback(on_data);\n"
            "7  }\n"
            "__old hunk__\n"
            "5 -    auto node = old_server.node();\n"
        )
        repaired = json.dumps({
            "fixable": True,
            "primary": {
                "existing_code": (
                    "SSM::SSM() {\n"
                    "    auto node = server.node();\n"
                    "    supplemental_.setCallback(on_data);\n"
                    "}"
                ),
                "improved_code": (
                    "SSM::SSM() {\n"
                    "    auto node = server.node();\n"
                    "    supplemental_.setCallback(on_data);\n"
                    "    supplemental_time_ = clock.now();\n"
                    "}"
                ),
                "relevant_lines_start": 30,
                "relevant_lines_end": 57,
            },
        })
        tool = _make_tool(_provider([("src/ssm.cpp", head_file)]), MockAI(response=repaired))
        suggestions = [{
            "relevant_file": "src/ssm.cpp",
            "existing_code": "bool suppl_to = now - supplemental_time_ > timeout;",
            "improved_code": "supplemental_time_ = clock.now();",
            "structural_issue": "cross_file",
            "companion_file": "src/ssm.cpp",
            "relevant_lines_start": 198,
            "relevant_lines_end": 206,
        }]

        result = asyncio.run(tool.run_repair_pipeline(suggestions, diff))

        assert len(result["resolved"]) == 1
        assert result["resolved"][0]["relevant_lines_start"] == 4
        assert result["resolved"][0]["relevant_lines_end"] == 7

    def test_tier1_repair_preserves_original_suggestion_order(self):
        repaired = json.dumps({
            "fixable": True,
            "primary": {
                "existing_code": "    void bar();",
                "improved_code": "    void bar() const;",
                "relevant_lines_start": 999,
                "relevant_lines_end": 999,
            },
        })
        tool = _make_tool(_provider([("src/foo.cpp", CPP_HEAD_FILE)]), MockAI(response=repaired))
        suggestions = [
            {
                "relevant_file": "src/foo.cpp",
                "existing_code": "wrong();",
                "improved_code": "fixed();",
                "one_sentence_summary": "first",
                "structural_issue": "none",
                "relevant_lines_start": 1,
                "relevant_lines_end": 1,
            },
            {
                "relevant_file": "src/foo.cpp",
                "existing_code": "class Foo {",
                "improved_code": "class Foo final {",
                "one_sentence_summary": "second",
                "structural_issue": "none",
                "relevant_lines_start": 3,
                "relevant_lines_end": 3,
            },
        ]

        result = asyncio.run(tool.run_repair_pipeline(suggestions, CPP_DIFF))

        assert [s["one_sentence_summary"] for s in result["resolved"]] == ["first", "second"]
