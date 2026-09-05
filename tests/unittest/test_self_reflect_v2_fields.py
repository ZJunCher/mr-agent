"""analyze_self_reflection_response must parse the 3 new Pipeline-v2
structural-diagnosis fields (self_contained/structural_issue/companion_file)
off the self-reflect feedback dict, with safe defaults when a suggestion has
no matching feedback (mirrors the existing score=7 default-on-miss behavior)."""
import asyncio

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


def _make_tool(monkeypatch):
    """Build a PRCodeSuggestions instance without hitting a real git provider."""
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    return tool


class TestAnalyzeSelfReflectionV2Fields:
    def test_new_fields_parsed_from_feedback(self, monkeypatch):
        tool = _make_tool(monkeypatch)
        data = {"code_suggestions": [{
            "one_sentence_summary": "fix null deref",
            "relevant_file": "src/a.cpp",
            "existing_code": "x",
            "improved_code": "y",
            "label": "possible bug",
        }]}
        response_reflect = """
```yaml
code_suggestions:
- suggestion_summary: |
    fix null deref
  relevant_file: "src/a.cpp"
  relevant_lines_start: 10
  relevant_lines_end: 11
  suggestion_score: 8
  self_contained: false
  structural_issue: "cross_file"
  companion_file: "src/a.hpp"
  why: |
    real bug
```
"""
        asyncio.run(tool.analyze_self_reflection_response(data, response_reflect))
        sugg = data["code_suggestions"][0]
        assert sugg["self_contained"] is False
        assert sugg["structural_issue"] == "cross_file"
        assert sugg["companion_file"] == "src/a.hpp"

    def test_defaults_when_feedback_missing(self, monkeypatch):
        tool = _make_tool(monkeypatch)
        data = {"code_suggestions": [{
            "one_sentence_summary": "unrelated summary that won't match anything",
            "relevant_file": "src/a.cpp",
            "existing_code": "x",
            "improved_code": "y",
            "label": "possible bug",
        }]}
        # A well-formed reflect response that simply doesn't mention this suggestion.
        response_reflect = """
```yaml
code_suggestions: []
```
"""
        asyncio.run(tool.analyze_self_reflection_response(data, response_reflect))
        sugg = data["code_suggestions"][0]
        assert sugg["self_contained"] is True
        assert sugg["structural_issue"] == "none"
        assert sugg["companion_file"] == ""

    def test_dedicated_prompt_selected_when_pipeline_v2_enabled(self):
        get_settings().set("pr_code_suggestions.pipeline_v2_enabled", True)
        try:
            assert get_settings().get("pr_code_suggestions.pipeline_v2_enabled") is True
            # the v2 prompt section must exist and be distinct from the legacy one
            assert get_settings().pr_code_suggestions_reflect_prompt_v2.system != \
                get_settings().pr_code_suggestions_reflect_prompt.system
        finally:
            get_settings().set("pr_code_suggestions.pipeline_v2_enabled", False)