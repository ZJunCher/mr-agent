"""Manual `/improve` reserves its summary comment before inline publication,
then edits that same comment with only successfully published suggestions."""
import asyncio
from unittest.mock import MagicMock

import pr_agent.tools.pr_code_suggestions as pcs_mod
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


def _make_instance(monkeypatch, code_suggestions, generate_summarized_suggestions=None):
    instance = object.__new__(PRCodeSuggestions)
    instance.git_provider = MagicMock()
    instance.git_provider.get_files.return_value = ["a.py"]
    instance.git_provider.is_supported.return_value = True
    instance.progress_response = None
    instance.progress = "## Generating PR code suggestions\n\n"
    instance.pending_tier2_tasks = []
    instance.data = None

    data = {"code_suggestions": code_suggestions}

    async def fake_generate_suggestions_data():
        instance.data = data
        return data

    instance.generate_suggestions_data = fake_generate_suggestions_data

    render_calls = []
    if generate_summarized_suggestions is None:
        def default_render(d):
            render_calls.append([dict(s) for s in d["code_suggestions"]])
            if not d["code_suggestions"]:
                return "## PR Code Suggestions ✨\nnone"
            has_link = bool(d["code_suggestions"]) and bool(d["code_suggestions"][0].get("inline_note_url"))
            return "## PR Code Suggestions ✨\nx" + (" [linked]" if has_link else "")
        instance.generate_summarized_suggestions = default_render
    else:
        instance.generate_summarized_suggestions = generate_summarized_suggestions
    instance._render_calls = render_calls

    get_settings().set("config.publish_output", True)
    get_settings().set("config.publish_output_progress", False)
    get_settings().set("config.is_auto_command", False)
    get_settings().set("pr_code_suggestions.commitable_code_suggestions", False)
    get_settings().set("pr_code_suggestions.demand_code_suggestions_self_review", False)
    get_settings().set("pr_code_suggestions.enable_chat_text", False)
    get_settings().set("pr_code_suggestions.enable_help_text", False)
    get_settings().set("config.output_relevant_configurations", False)
    get_settings().set("pr_code_suggestions.dual_publishing_score_threshold", -1)
    get_settings().set("pr_code_suggestions.persistent_comment", False)
    return instance


def _sugg(**kw):
    base = {
        "relevant_file": "a.py",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
        "score": 9,
    }
    base.update(kw)
    return base


def test_empty_suggestions_publish_exact_chinese_message(monkeypatch):
    instance = _make_instance(monkeypatch, code_suggestions=[])
    get_settings().set("config.response_language", "zh-cn")
    get_settings().set("pr_code_suggestions.publish_output_no_suggestions", True)

    asyncio.run(instance.run())

    instance.git_provider.publish_comment.assert_called_once_with(
        "## PR 代码建议 ✨\n\n未发现可改进建议。"
    )


def test_empty_suggestions_expose_message_when_publish_is_disabled(monkeypatch):
    instance = _make_instance(monkeypatch, code_suggestions=[])
    get_settings().set("config.response_language", "zh-cn")
    get_settings().set("config.publish_output", False)

    asyncio.run(instance.run())

    assert get_settings().data["artifact"] == "## PR 代码建议 ✨\n\n未发现可改进建议。"
    instance.git_provider.publish_comment.assert_not_called()


def test_generation_failure_is_not_reported_as_empty_success(monkeypatch):
    instance = _make_instance(monkeypatch, code_suggestions=[])

    async def fail_generation():
        raise RuntimeError("model failed")

    instance.generate_suggestions_data = fail_generation
    get_settings().data = {}

    asyncio.run(instance.run())

    assert "未发现可改进建议。" not in get_settings().data.get("artifact", "")
    assert "未发现可改进建议。" not in instance.git_provider.publish_comment.call_args.args[0]


def test_summary_placeholder_is_published_before_inline_and_edited_after(monkeypatch):
    call_order = []

    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        call_order.append(("publish_inline", kwargs.get("source")))
        return {"published": 1, "skipped": 0, "failed": 0, "published_locations": [
            {"relevant_file": "a.py", "relevant_lines_start": 1, "relevant_lines_end": 1,
             "note_url": "https://gl/mr/1#note_1"},
        ]}

    monkeypatch.setattr(pcs_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)

    instance = _make_instance(monkeypatch, [_sugg()])
    monkeypatch.setattr(pcs_mod, "schedule_tier2", lambda *a, **k: None)

    def fake_publish_comment(body):
        call_order.append(("publish_comment", None))
        return MagicMock()

    instance.git_provider.publish_comment = fake_publish_comment
    instance.git_provider.edit_comment = lambda *_args: call_order.append(("edit_comment", None))

    asyncio.run(instance.run())

    assert [c[0] for c in call_order] == ["publish_comment", "publish_inline", "edit_comment"]
    # the improve_command source was used, not mr_create's
    assert call_order[1][1] == "improve_command"
    # the rendered table saw the backfilled inline_note_url
    assert instance._render_calls[0][0].get("inline_note_url") == "https://gl/mr/1#note_1"


def test_fallback_published_note_is_linked_in_manual_improve_summary(monkeypatch):
    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {
            "published": 0, "fallback_published": 1, "skipped": 0, "failed": 0,
            "published_locations": [{
                "relevant_file": "a.py", "relevant_lines_start": 1, "relevant_lines_end": 1,
                "note_url": "https://gl/mr/1#note_999",
            }],
        }

    monkeypatch.setattr(pcs_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)
    monkeypatch.setattr(pcs_mod, "schedule_tier2", lambda *args, **kwargs: None)
    instance = _make_instance(monkeypatch, [_sugg()])
    instance.git_provider.publish_comment = MagicMock(return_value=MagicMock())

    asyncio.run(instance.run())

    assert instance._render_calls[-1][0]["inline_note_url"] == "https://gl/mr/1#note_999"


def test_summary_comment_is_published_once_then_edited(monkeypatch):
    """Regression: calling publish_persistent_comment_with_history (or
    publish_comment) twice for one /improve run would double up the history
    section / publish two comments. Inline suggestions must be resolved
    BEFORE the (single) summary publish, not require a second edit pass."""
    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {"published": 1, "skipped": 0, "failed": 0, "published_locations": [
            {"relevant_file": "a.py", "relevant_lines_start": 1, "relevant_lines_end": 1,
             "note_url": "https://gl/mr/1#note_1"},
        ]}

    monkeypatch.setattr(pcs_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)

    instance = _make_instance(monkeypatch, [_sugg()])
    monkeypatch.setattr(pcs_mod, "schedule_tier2", lambda *a, **k: None)
    published = MagicMock()
    instance.git_provider.publish_comment = MagicMock(return_value=published)
    instance.git_provider.edit_comment = MagicMock()

    asyncio.run(instance.run())

    assert instance.git_provider.publish_comment.call_count == 1
    assert instance.git_provider.edit_comment.call_count == 1


def test_failed_inline_is_removed_from_summary(monkeypatch):
    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {"published": 0, "skipped": 0, "failed": 0, "published_locations": []}

    monkeypatch.setattr(pcs_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)

    instance = _make_instance(monkeypatch, [_sugg()])
    monkeypatch.setattr(pcs_mod, "schedule_tier2", lambda *a, **k: None)
    instance.git_provider.publish_comment = MagicMock(return_value=MagicMock())

    asyncio.run(instance.run())

    assert instance._render_calls[-1] == []
    assert instance.git_provider.publish_comment.call_count == 1
    assert instance.git_provider.edit_comment.call_count == 1


def test_persistent_comment_path_publishes_summary_exactly_once(monkeypatch):
    """With persistent_comment=true (the default), publishing must still
    happen exactly once per run -- publish_persistent_comment_with_history
    must not be invoked twice (which would incorrectly fold the pre-link
    draft into the "previous suggestions" history section)."""
    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {"published": 1, "skipped": 0, "failed": 0, "published_locations": [
            {"relevant_file": "a.py", "relevant_lines_start": 1, "relevant_lines_end": 1,
             "note_url": "https://gl/mr/1#note_1"},
        ]}

    monkeypatch.setattr(pcs_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)

    instance = _make_instance(monkeypatch, [_sugg()])
    monkeypatch.setattr(pcs_mod, "schedule_tier2", lambda *a, **k: None)
    get_settings().set("pr_code_suggestions.persistent_comment", True)

    calls = []

    def fake_publish_with_history(*args, **kwargs):
        calls.append((args, kwargs))
        return MagicMock(), args[1]

    monkeypatch.setattr(PRCodeSuggestions, "publish_persistent_comment_with_history",
                       staticmethod(fake_publish_with_history))

    asyncio.run(instance.run())

    assert len(calls) == 1


def test_tier2_scheduled_with_improve_command_source(monkeypatch):
    scheduled = {}

    def fake_schedule_tier2(git_provider, pending_tasks, **kwargs):
        scheduled["source"] = kwargs.get("source")
        scheduled["pending_tasks"] = pending_tasks
        return None

    monkeypatch.setattr(pcs_mod, "schedule_tier2", fake_schedule_tier2)

    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {"published": 0, "skipped": 0, "failed": 0, "published_locations": []}

    monkeypatch.setattr(pcs_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)

    instance = _make_instance(monkeypatch, [_sugg()])
    instance.pending_tier2_tasks = [{"task": "x"}]
    instance.git_provider.publish_comment = MagicMock(return_value=MagicMock())

    asyncio.run(instance.run())

    assert scheduled["source"] == "improve_command"
    assert scheduled["pending_tasks"] == [{"task": "x"}]


def test_progress_placeholder_is_replaced_by_top_summary_comment(monkeypatch):
    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {"published": 0, "skipped": 0, "failed": 0, "published_locations": []}

    monkeypatch.setattr(pcs_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)
    monkeypatch.setattr(pcs_mod, "schedule_tier2", lambda *a, **k: None)

    instance = _make_instance(monkeypatch, [_sugg()])
    placeholder = MagicMock()
    instance.progress_response = placeholder
    instance.git_provider.remove_comment = MagicMock()
    instance.git_provider.edit_comment = MagicMock()
    fresh_comment = MagicMock()
    instance.git_provider.publish_comment = MagicMock(return_value=fresh_comment)

    asyncio.run(instance.run())

    instance.git_provider.remove_comment.assert_called_once_with(placeholder)
    instance.git_provider.edit_comment.assert_called_once()
    instance.git_provider.publish_comment.assert_called_once()


def test_persistent_comment_path_passes_publish_as_new_comment(monkeypatch):
    """publish_persistent_comment_with_history must be called with
    publish_as_new_comment=True for manual /improve, so it never edits the
    placeholder or a previous run's summary comment in place."""
    async def fake_publish_inline(git_provider, code_suggestions, **kwargs):
        return {"published": 0, "skipped": 0, "failed": 0, "published_locations": []}

    monkeypatch.setattr(pcs_mod.inline_publisher, "publish_inline_suggestions_async", fake_publish_inline)
    monkeypatch.setattr(pcs_mod, "schedule_tier2", lambda *a, **k: None)

    instance = _make_instance(monkeypatch, [_sugg()])
    get_settings().set("pr_code_suggestions.persistent_comment", True)

    calls = []

    def fake_publish_with_history(*args, **kwargs):
        calls.append(kwargs)
        return MagicMock(), args[1]

    monkeypatch.setattr(PRCodeSuggestions, "publish_persistent_comment_with_history",
                       staticmethod(fake_publish_with_history))

    asyncio.run(instance.run())

    assert len(calls) == 1
    assert calls[0].get("publish_as_new_comment") is True
