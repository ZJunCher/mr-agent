"""PRMrCreate reserves the combined summary comment above inline suggestions,
then replaces its pending section with only successfully published inline
suggestions."""
import asyncio
from unittest.mock import MagicMock

import pr_agent.tools.pr_mr_create as mr_mod
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_mr_create import PRMrCreate


class _FakeCodeSuggestionsTool:
    def __init__(self, pr_url, ai_handler=None):
        self.pending_tier2_tasks = []
        self.data = {"code_suggestions": [
            {"relevant_file": "a", "relevant_lines_start": 1, "relevant_lines_end": 1, "score": 9},
        ]}
        self.render_calls = []

    async def generate_suggestions_data(self):
        return self.data

    def generate_summarized_suggestions(self, data):
        # Snapshot (deep-ish copy) so later in-place backfill_note_urls
        # mutations to the ORIGINAL suggestion dicts don't retroactively
        # change what this render call is asserted to have seen.
        snapshot = {"code_suggestions": [dict(s) for s in data["code_suggestions"]]}
        self.render_calls.append(snapshot)
        if not data["code_suggestions"]:
            return "## PR Code Suggestions ✨\nnone"
        has_link = bool(data["code_suggestions"][0].get("inline_note_url"))
        return "## PR Code Suggestions ✨\nx" + (" [linked]" if has_link else "")

    @staticmethod
    def generate_pending_suggestions():
        return "## PR Code Suggestions ✨\npending"


class _EmptyCodeSuggestionsTool(_FakeCodeSuggestionsTool):
    def __init__(self, pr_url, ai_handler=None):
        super().__init__(pr_url, ai_handler=ai_handler)
        self.data = {"code_suggestions": []}

    @staticmethod
    def no_suggestions_markdown():
        return "## PR 代码建议 ✨\n\n未发现可改进建议。"


def _make(monkeypatch):
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
    monkeypatch.setattr(mr_mod, "PRCodeSuggestions", _FakeCodeSuggestionsTool)
    monkeypatch.setattr(mr_mod, "schedule_tier2", lambda *a, **k: None)
    get_settings().set("pr_feedback.gate_enabled", False)
    return inst


def test_mr_create_includes_empty_suggestion_success_section(monkeypatch):
    instance = _make(monkeypatch)
    monkeypatch.setattr(mr_mod, "PRCodeSuggestions", _EmptyCodeSuggestionsTool)
    get_settings().set("config.response_language", "zh-cn")
    get_settings().set("config.publish_output", True)

    asyncio.run(instance.run())

    body = instance.git_provider.publish_comment.call_args.args[0]
    assert body.count("## PR 代码建议 ✨") == 1
    assert "未发现可改进建议。" in body


def test_mr_create_does_not_label_all_filtered_as_no_suggestions(monkeypatch, tmp_path):
    instance = _make(monkeypatch)
    monkeypatch.setattr(mr_mod, "PRCodeSuggestions", _EmptyCodeSuggestionsTool)
    get_settings().set("pr_feedback.storage_path", str(tmp_path / "review.db"))
    get_settings().set("pr_code_suggestions.inline_suggestions_storage_path", "")
    monkeypatch.setattr(
        mr_mod,
        "get_review_run",
        lambda *_args, **_kwargs: {"generated_count": 1, "filtered_count": 1},
    )
    get_settings().set("config.publish_output", True)

    asyncio.run(instance.run())

    published = (
        instance.git_provider.publish_comment.call_args.args[0]
        if instance.git_provider.publish_comment.called
        else ""
    )
    assert "未发现可改进建议。" not in published


def test_main_comment_is_published_before_inline_suggestions(monkeypatch):
    call_order = []

    def fake_publish_comment(body):
        call_order.append("publish_comment")
        return MagicMock()

    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        call_order.append("publish_inline")
        return {"published": 0, "skipped": 0, "failed": 0, "published_locations": []}

    monkeypatch.setattr(mr_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)

    get_settings().set("config.publish_output", True)
    inst = _make(monkeypatch)
    inst.git_provider.publish_comment = fake_publish_comment
    asyncio.run(inst.run())

    assert call_order == ["publish_comment", "publish_inline"]


def test_initial_top_comment_has_no_concrete_suggestion(monkeypatch):
    captured_tool = {}
    real_init = _FakeCodeSuggestionsTool.__init__

    def capturing_init(self, pr_url, ai_handler=None):
        real_init(self, pr_url, ai_handler=ai_handler)
        captured_tool["tool"] = self

    monkeypatch.setattr(_FakeCodeSuggestionsTool, "__init__", capturing_init)

    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {
            "published": 1, "skipped": 0, "failed": 0,
            "published_locations": [{
                "relevant_file": "a", "relevant_lines_start": 1, "relevant_lines_end": 1,
                "note_url": "http://gl/mr/1#note_1",
            }],
        }

    monkeypatch.setattr(mr_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)

    get_settings().set("config.publish_output", True)
    inst = _make(monkeypatch)
    asyncio.run(inst.run())

    initial_body = inst.git_provider.publish_comment.call_args.args[0]
    assert "\nx" not in initial_body


def test_comment_is_edited_in_place_once_inline_links_are_known(monkeypatch):
    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {
            "published": 1, "skipped": 0, "failed": 0,
            "published_locations": [{
                "relevant_file": "a", "relevant_lines_start": 1, "relevant_lines_end": 1,
                "note_url": "http://gl/mr/1#note_1",
            }],
        }

    monkeypatch.setattr(mr_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)

    get_settings().set("config.publish_output", True)
    inst = _make(monkeypatch)
    asyncio.run(inst.run())

    inst.git_provider.edit_comment.assert_called_once()
    _, edited_body = inst.git_provider.edit_comment.call_args[0]
    assert "[linked]" in edited_body


def test_fallback_published_note_is_linked_in_mr_create_summary(monkeypatch):
    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {
            "published": 0, "fallback_published": 1, "skipped": 0, "failed": 0,
            "published_locations": [{
                "relevant_file": "a", "relevant_lines_start": 1, "relevant_lines_end": 1,
                "note_url": "http://gl/mr/1#note_999",
            }],
        }

    monkeypatch.setattr(mr_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)
    get_settings().set("config.publish_output", True)
    instance = _make(monkeypatch)

    asyncio.run(instance.run())

    _, edited_body = instance.git_provider.edit_comment.call_args.args
    assert "[linked]" in edited_body


def test_comment_not_edited_when_nothing_published_inline(monkeypatch):
    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {"published": 0, "skipped": 1, "failed": 0, "published_locations": []}

    monkeypatch.setattr(mr_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)

    get_settings().set("config.publish_output", True)
    inst = _make(monkeypatch)
    asyncio.run(inst.run())

    inst.git_provider.edit_comment.assert_called_once()
    _, edited_body = inst.git_provider.edit_comment.call_args.args
    assert "\nx" not in edited_body


def test_final_table_contains_only_successfully_published_suggestions(monkeypatch):
    captured_tool = {}
    real_init = _FakeCodeSuggestionsTool.__init__

    def capturing_init(self, pr_url, ai_handler=None):
        real_init(self, pr_url, ai_handler=ai_handler)
        self.data["code_suggestions"].append(
            {"relevant_file": "b", "relevant_lines_start": 2, "relevant_lines_end": 2, "score": 9})
        captured_tool["tool"] = self

    monkeypatch.setattr(_FakeCodeSuggestionsTool, "__init__", capturing_init)

    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        code_suggestions[0]["_inline_suggestion_id"] = "SUG-001"
        code_suggestions[1]["_inline_suggestion_id"] = "SUG-002"
        return {
            "published": 1, "skipped": 0, "failed": 1,
            "published_locations": [{
                "suggestion_id": "SUG-001",
                "relevant_file": "a",
                "relevant_lines_start": 1,
                "relevant_lines_end": 1,
                "note_url": "http://gl/mr/1#note_1",
            }],
        }

    monkeypatch.setattr(mr_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)
    get_settings().set("config.publish_output", True)
    inst = _make(monkeypatch)

    asyncio.run(inst.run())

    final_suggestions = captured_tool["tool"].render_calls[-1]["code_suggestions"]
    assert len(final_suggestions) == 1
    assert final_suggestions[0]["relevant_file"] == "a"
    assert final_suggestions[0]["inline_note_url"] == "http://gl/mr/1#note_1"


def test_inline_publish_skipped_when_publish_output_is_false(monkeypatch):
    calls = {"n": 0}

    async def fake_publish(git_provider, code_suggestions, **kwargs):
        calls["n"] += 1
        return {"published": 0, "skipped": 0, "failed": 0, "published_locations": []}

    monkeypatch.setattr(mr_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish)

    get_settings().set("config.publish_output", False)
    inst = _make(monkeypatch)
    asyncio.run(inst.run())

    assert calls["n"] == 0


def test_mr_create_inline_failure_does_not_break_publish(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("inline exploded")

    monkeypatch.setattr(mr_mod.inline_publisher, "publish_inline_suggestions_async", boom)

    get_settings().set("config.publish_output", True)
    inst = _make(monkeypatch)
    # should not raise even though inline publishing throws
    asyncio.run(inst.run())
    inst.git_provider.publish_comment.assert_called()
