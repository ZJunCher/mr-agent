"""Tier-2 refresh adds only successfully published inline suggestions to the
existing top summary comment."""
import asyncio
from unittest.mock import MagicMock

import pr_agent.tools.pr_mr_create as mr_mod
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_mr_create import PRMrCreate


def _tool(data_suggestions, rendered_after_merge="## PR Code Suggestions ✨\nMERGED"):
    tool = MagicMock()
    tool.data = {"code_suggestions": data_suggestions}
    tool.generate_summarized_suggestions = MagicMock(return_value=rendered_after_merge)
    return tool


class TestRefreshImproveTable:
    def test_merges_tier2_one_click_into_the_table_and_edits_the_comment(self):
        inst = PRMrCreate.__new__(PRMrCreate)
        inst.git_provider = MagicMock()
        tool = _tool([{"relevant_file": "a.cpp", "score": 9}])
        original_body = "## Review\n\n___\n\n## PR Code Suggestions ✨\nOriginal Table\n\n___\n\n## Help"
        improve_md = "## PR Code Suggestions ✨\nOriginal Table"
        comment = object()
        tier2_result = {
            "one_click": [{"relevant_file": "b.cpp", "score": 8, "inline_note_url": "http://gl#note_2"}],
            "copy_patch": [],
            "failed": [],
        }

        comment_state = {"body": original_body, "improve_md": improve_md}
        asyncio.run(inst._refresh_improve_table(tool, comment_state, comment, tier2_result))

        tool.generate_summarized_suggestions.assert_called_once()
        merged_arg = tool.generate_summarized_suggestions.call_args[0][0]
        assert merged_arg["code_suggestions"] == [
            {"relevant_file": "a.cpp", "score": 9},
            {"relevant_file": "b.cpp", "score": 8, "inline_note_url": "http://gl#note_2"},
        ]
        inst.git_provider.edit_comment.assert_called_once()
        edited_comment, edited_body = inst.git_provider.edit_comment.call_args[0]
        assert edited_comment is comment
        assert "MERGED" in edited_body
        assert "## Review" in edited_body and "## Help" in edited_body  # other sections untouched

    def test_noop_when_no_one_click_results(self):
        inst = PRMrCreate.__new__(PRMrCreate)
        inst.git_provider = MagicMock()
        tool = _tool([])
        comment_state = {"body": "body", "improve_md": "## PR Code Suggestions ✨\nx"}
        asyncio.run(inst._refresh_improve_table(tool, comment_state, object(),
                                                 {"one_click": [], "copy_patch": [], "failed": []}))
        tool.generate_summarized_suggestions.assert_not_called()
        inst.git_provider.edit_comment.assert_not_called()

    def test_copy_patch_only_results_do_not_enter_the_table(self):
        inst = PRMrCreate.__new__(PRMrCreate)
        inst.git_provider = MagicMock()
        tool = _tool([{"relevant_file": "a.cpp", "score": 9}])
        original_body = "## Review\n\n___\n\n## PR Code Suggestions ✨\nOriginal Table\n\n___\n\n## Help"
        improve_md = "## PR Code Suggestions ✨\nOriginal Table"
        comment = object()
        tier2_result = {"one_click": [], "copy_patch": [{"relevant_file": "c.hpp", "score": 7}], "failed": []}

        comment_state = {"body": original_body, "improve_md": improve_md}
        asyncio.run(inst._refresh_improve_table(tool, comment_state, comment, tier2_result))

        tool.generate_summarized_suggestions.assert_not_called()
        inst.git_provider.edit_comment.assert_not_called()

    def test_noop_when_tool_is_none(self):
        inst = PRMrCreate.__new__(PRMrCreate)
        inst.git_provider = MagicMock()
        comment_state = {"body": "body", "improve_md": "## PR Code Suggestions ✨\nx"}
        asyncio.run(inst._refresh_improve_table(None, comment_state, object(),
                                                 {"one_click": [{"relevant_file": "a"}]}))
        inst.git_provider.edit_comment.assert_not_called()

    def test_noop_when_comment_is_none(self):
        inst = PRMrCreate.__new__(PRMrCreate)
        inst.git_provider = MagicMock()
        tool = _tool([])
        comment_state = {"body": "body", "improve_md": "## PR Code Suggestions ✨\nx"}
        asyncio.run(inst._refresh_improve_table(tool, comment_state, None,
                                                 {"one_click": [{"relevant_file": "a", "inline_note_url": "http://gl#note_1"}]}))
        inst.git_provider.edit_comment.assert_not_called()

    def test_noop_when_improve_section_not_found_verbatim_in_body(self):
        inst = PRMrCreate.__new__(PRMrCreate)
        inst.git_provider = MagicMock()
        tool = _tool([])
        comment_state = {"body": "## Review only, no improve section here", "improve_md": "## PR Code Suggestions ✨\nMissing"}
        asyncio.run(inst._refresh_improve_table(
            tool, comment_state, object(),
            {"one_click": [{"relevant_file": "a", "inline_note_url": "http://gl#note_1"}]}))
        inst.git_provider.edit_comment.assert_not_called()

    def test_never_raises_when_generate_summarized_suggestions_throws(self):
        inst = PRMrCreate.__new__(PRMrCreate)
        inst.git_provider = MagicMock()
        tool = MagicMock()
        tool.data = {"code_suggestions": []}
        tool.generate_summarized_suggestions = MagicMock(side_effect=RuntimeError("boom"))
        comment_state = {"body": "## PR Code Suggestions ✨\nx", "improve_md": "## PR Code Suggestions ✨\nx"}
        asyncio.run(inst._refresh_improve_table(
            tool, comment_state, object(),
            {"one_click": [{"relevant_file": "a", "inline_note_url": "http://gl#note_1"}]}))
        inst.git_provider.edit_comment.assert_not_called()

    def test_never_raises_when_edit_comment_throws(self):
        inst = PRMrCreate.__new__(PRMrCreate)
        inst.git_provider = MagicMock()
        inst.git_provider.edit_comment = MagicMock(side_effect=RuntimeError("boom"))
        tool = _tool([])
        body = "## PR Code Suggestions ✨\nOld"
        comment_state = {"body": body, "improve_md": "## PR Code Suggestions ✨\nOld"}
        asyncio.run(inst._refresh_improve_table(
            tool, comment_state, object(),
            {"one_click": [{"relevant_file": "a", "inline_note_url": "http://gl#note_1"}]}))
        # exception is swallowed, run() must never see it raise


class TestRunWiresOnCompleteIntoScheduleTier2:
    def test_schedule_tier2_receives_a_callable_on_complete_that_refreshes_the_table(self, monkeypatch):
        class FakeCodeSuggestionsTool:
            def __init__(self, pr_url, ai_handler=None):
                self.pending_tier2_tasks = [{"relevant_file": "b.cpp"}]
                self.data = {"code_suggestions": [{
                    "relevant_file": "a.cpp", "relevant_lines_start": 1, "relevant_lines_end": 1, "score": 9,
                }]}

            async def generate_suggestions_data(self):
                return self.data

            def generate_summarized_suggestions(self, data):
                return f"## PR Code Suggestions ✨\nMerged ({len(data['code_suggestions'])} suggestions)"

            @staticmethod
            def generate_pending_suggestions():
                return "## PR Code Suggestions ✨\npending"

        monkeypatch.setattr(mr_mod, "PRCodeSuggestions", FakeCodeSuggestionsTool)

        async def fake_publish(git_provider, code_suggestions, **kwargs):
            return {
                "published": 1,
                "skipped": 0,
                "failed": 0,
                "published_locations": [{
                    "relevant_file": "a.cpp",
                    "relevant_lines_start": 1,
                    "relevant_lines_end": 1,
                    "note_url": "http://gl#note_1",
                }],
            }

        monkeypatch.setattr(mr_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish)

        captured = {}

        def fake_schedule_tier2(git_provider, pending_tasks, store_path=None, on_complete=None):
            captured["pending_tasks"] = pending_tasks
            captured["on_complete"] = on_complete
            return None

        monkeypatch.setattr(mr_mod, "schedule_tier2", fake_schedule_tier2)

        inst = PRMrCreate.__new__(PRMrCreate)
        inst.git_provider = MagicMock()
        inst.git_provider.pr_url = "http://gl/mr/1"
        inst.ai_handler_cls = MagicMock()
        inst.args = []
        inst.llm_feedback = []

        async def fake_safe(tool_name, factory):
            if tool_name == "improve":
                await factory()
                artifact = (getattr(get_settings(), "data", {}) or {}).get("artifact")
                return artifact or ""
            return ""

        monkeypatch.setattr(inst, "_safe_tool_run", fake_safe)
        get_settings().set("config.publish_output", True)
        get_settings().set("pr_feedback.gate_enabled", False)

        asyncio.run(inst.run())

        assert captured["pending_tasks"] == [{"relevant_file": "b.cpp"}]
        assert callable(captured["on_complete"])

        # Simulate Tier-2 completing later: invoking on_complete must edit
        # the already-published comment with the merged table.
        tier2_result = {
            "one_click": [{"relevant_file": "b.cpp", "score": 8, "inline_note_url": "http://gl#note_2"}],
            "copy_patch": [],
            "failed": [],
        }
        asyncio.run(captured["on_complete"](tier2_result))
        assert inst.git_provider.edit_comment.call_count == 2
        _, edited_body = inst.git_provider.edit_comment.call_args_list[-1].args
        assert "Merged (2 suggestions)" in edited_body
