"""Tests for Tier-2 fire-and-forget scheduling (tier2_scheduler.py)."""
import asyncio
import types

from pr_agent.config_loader import get_settings
from pr_agent.suggestions import tier2_scheduler


def _provider():
    provider = types.SimpleNamespace()
    provider.comments = []
    provider.publish_comment = lambda body: provider.comments.append(body)
    provider.id_project = "group/cook"
    provider.id_mr = "10"
    provider.pr_url = "https://gitlab.example.com/group/cook/-/merge_requests/10"
    provider.get_diff_files = lambda: []
    provider.diff_files = []
    provider.get_diff_refs = lambda: {"head_sha": "abc123"}
    provider.get_pr_url = lambda: provider.pr_url
    provider.publish_inline_suggestions = lambda payloads: [
        {"suggestion_id": p["suggestion_id"], "discussion_id": f"d-{p['suggestion_id']}",
         "note_id": f"n-{p['suggestion_id']}", "publish_status": "published", "skip_reason": ""}
        for p in payloads
    ]
    return provider


class TestScheduleTier2:
    def test_no_pending_tasks_returns_none_immediately(self):
        get_settings().set("pr_code_suggestions.tier2_enabled", True)
        try:
            assert tier2_scheduler.schedule_tier2(_provider(), []) is None
        finally:
            get_settings().set("pr_code_suggestions.tier2_enabled", False)

    def test_disabled_switch_returns_none_without_running_anything(self, monkeypatch):
        called = {"ran": False}

        async def fake_run_and_publish(*a, **k):
            called["ran"] = True
            return {}

        monkeypatch.setattr(tier2_scheduler, "_run_and_publish", fake_run_and_publish)
        get_settings().set("pr_code_suggestions.tier2_enabled", False)
        result = tier2_scheduler.schedule_tier2(_provider(), [{"relevant_file": "a.cpp"}])
        assert result is None
        assert called["ran"] is False

    def test_enabled_switch_creates_a_background_task(self, monkeypatch):
        async def fake_run_and_publish(*a, **k):
            return {"one_click": 0, "copy_patch": 0, "failed": 0}

        monkeypatch.setattr(tier2_scheduler, "_run_and_publish", fake_run_and_publish)
        get_settings().set("pr_code_suggestions.tier2_enabled", True)

        async def _main():
            task = tier2_scheduler.schedule_tier2(_provider(), [{"relevant_file": "a.cpp"}])
            assert task is not None
            await task

        try:
            asyncio.run(_main())
        finally:
            get_settings().set("pr_code_suggestions.tier2_enabled", False)


class TestRunAndPublish:
    def test_one_click_results_get_published_as_inline_suggestions(self, monkeypatch, tmp_path):
        async def fake_run_heavy_repair(git_provider, tasks):
            return {"one_click": [{
                "relevant_file": "src/foo.cpp", "existing_code": "a", "improved_code": "b",
                "relevant_lines_start": 1, "relevant_lines_end": 1, "resolved_by_stage": "tier2_heavy",
                "tier2_duration_ms": 1234, "label": "possible bug", "score": 8,
                "one_sentence_summary": "s", "suggestion_content": "c",
            }], "copy_patch": [], "failed": []}

        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "run_heavy_repair", fake_run_heavy_repair)

        get_settings().set("pr_code_suggestions.inline_suggestions_enabled", True)
        get_settings().set("pr_code_suggestions.inline_suggestions_project_allowlist", ["*"])
        get_settings().set("pr_code_suggestions.inline_suggestion_min_score", 0)
        get_settings().set("pr_code_suggestions.inline_suggestion_max_lines", 20)
        # Tier-2 results are already verified (real diff applied + classified);
        # pipeline_v2_enabled=true makes publish_inline_suggestions_async skip
        # the legacy heuristic gate/LLM self-check (see Task 12), matching how
        # this scheduler is only ever invoked when Pipeline v2 is active.
        get_settings().set("pr_code_suggestions.pipeline_v2_enabled", True)

        provider = _provider()
        provider.diff_files = [types.SimpleNamespace(filename="src/foo.cpp", head_file="a")]
        provider.get_diff_files = lambda: provider.diff_files

        db_path = str(tmp_path / "s.db")
        try:
            summary = asyncio.run(tier2_scheduler._run_and_publish(provider, [{"relevant_file": "src/foo.cpp"}],
                                                                    store_path=db_path))
        finally:
            get_settings().set("pr_code_suggestions.pipeline_v2_enabled", False)
        assert summary["one_click"] == 1

        from pr_agent.suggestions.store import get_published_suggestions
        rows = get_published_suggestions("group/cook", "10", path=db_path)
        assert len(rows) == 1
        assert rows[0]["resolved_by_stage"] == "tier2_heavy"
        assert rows[0]["tier2_duration_ms"] == 1234

    def test_one_click_suggestions_get_inline_note_url_backfilled(self, monkeypatch):
        one_click_item = {
            "relevant_file": "a.cpp", "existing_code": "x1", "improved_code": "y1",
            "relevant_lines_start": 1, "relevant_lines_end": 1, "resolved_by_stage": "tier2_heavy",
            "label": "correctness", "score": 8, "one_sentence_summary": "s",
        }

        async def fake_run_heavy_repair(git_provider, tasks):
            return {"one_click": [one_click_item], "copy_patch": [], "failed": []}

        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "run_heavy_repair", fake_run_heavy_repair)

        async def fake_publish_inline(*a, **k):
            return {
                "published": 1, "skipped": 0, "failed": 0,
                "published_locations": [{
                    "relevant_file": "a.cpp", "relevant_lines_start": 1, "relevant_lines_end": 1,
                    "note_url": "http://gl/mr/1#note_9",
                }],
            }

        import pr_agent.suggestions.inline_publisher as ip
        monkeypatch.setattr(ip, "publish_inline_suggestions_async", fake_publish_inline)

        provider = _provider()
        summary = asyncio.run(tier2_scheduler._run_and_publish(provider, [{"relevant_file": "a.cpp"}]))

        assert summary["one_click"] == 1
        assert one_click_item["inline_note_url"] == "http://gl/mr/1#note_9"

    def test_copy_patch_results_get_no_standalone_comment(self, monkeypatch):
        # copy_patch is no longer published as its own comment -- it only
        # reaches the user via the table refresh (on_complete), rendered as
        # a "not one-click appliable" row. See the module docstring.
        async def fake_run_heavy_repair(git_provider, tasks):
            return {"one_click": [], "copy_patch": [{
                "relevant_file": "src/foo.hpp", "existing_code": "a", "improved_code": "b", "note": "needs decl",
            }], "failed": []}

        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "run_heavy_repair", fake_run_heavy_repair)

        provider = _provider()
        summary = asyncio.run(tier2_scheduler._run_and_publish(provider, [{"relevant_file": "src/foo.hpp"}]))
        assert summary["copy_patch"] == 1
        assert not provider.comments

    def test_failed_results_get_a_text_only_fallback_comment(self, monkeypatch):
        async def fake_run_heavy_repair(git_provider, tasks):
            return {"one_click": [], "copy_patch": [], "failed": [("SUG-001", "could not determine fix")]}

        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "run_heavy_repair", fake_run_heavy_repair)

        provider = _provider()
        summary = asyncio.run(tier2_scheduler._run_and_publish(provider, [{"relevant_file": "a.cpp"}]))
        assert summary["failed"] == 1
        assert len(provider.comments) == 1
        assert "SUG-001" in provider.comments[0]
        assert "could not determine fix" in provider.comments[0]

    def test_run_heavy_repair_exception_never_raises(self, monkeypatch):
        async def raising(*a, **k):
            raise RuntimeError("boom")

        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "run_heavy_repair", raising)

        provider = _provider()
        summary = asyncio.run(tier2_scheduler._run_and_publish(provider, [{"relevant_file": "a.cpp"}]))
        assert summary["failed"] == 1
        assert not provider.comments  # no fallback comment when the exception path returns early

    def test_multi_location_group_gets_an_overview_comment_before_the_suggestions(self, monkeypatch):
        async def fake_run_heavy_repair(git_provider, tasks):
            return {"one_click": [
                {"relevant_file": "a.cpp", "existing_code": "x1", "improved_code": "y1",
                 "relevant_lines_start": 1, "relevant_lines_end": 1, "resolved_by_stage": "tier2_heavy",
                 "label": "并发性", "score": 9, "one_sentence_summary": "锁竞争风险", "source_task_id": "SUG-001"},
                {"relevant_file": "a.cpp", "existing_code": "x2", "improved_code": "y2",
                 "relevant_lines_start": 5, "relevant_lines_end": 5, "resolved_by_stage": "tier2_heavy",
                 "label": "并发性", "score": 9, "one_sentence_summary": "锁竞争风险", "source_task_id": "SUG-001"},
            ], "copy_patch": [], "failed": []}

        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "run_heavy_repair", fake_run_heavy_repair)

        async def fake_publish_inline(*a, **k):
            return {"published": 2, "skipped": 0, "failed": 0}

        import pr_agent.suggestions.inline_publisher as ip
        monkeypatch.setattr(ip, "publish_inline_suggestions_async", fake_publish_inline)

        provider = _provider()
        summary = asyncio.run(tier2_scheduler._run_and_publish(provider, [{"relevant_file": "a.cpp"}]))
        assert summary["one_click"] == 2
        assert len(provider.comments) == 1
        overview = provider.comments[0]
        assert "2" in overview
        assert "并发性" in overview
        assert "锁竞争风险" in overview

    def test_single_location_group_gets_no_overview_comment(self, monkeypatch):
        async def fake_run_heavy_repair(git_provider, tasks):
            return {"one_click": [
                {"relevant_file": "a.cpp", "existing_code": "x1", "improved_code": "y1",
                 "relevant_lines_start": 1, "relevant_lines_end": 1, "resolved_by_stage": "tier2_heavy",
                 "label": "correctness", "score": 8, "one_sentence_summary": "s", "source_task_id": "SUG-001"},
            ], "copy_patch": [], "failed": []}

        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "run_heavy_repair", fake_run_heavy_repair)

        async def fake_publish_inline(*a, **k):
            return {"published": 1, "skipped": 0, "failed": 0}

        import pr_agent.suggestions.inline_publisher as ip
        monkeypatch.setattr(ip, "publish_inline_suggestions_async", fake_publish_inline)

        provider = _provider()
        summary = asyncio.run(tier2_scheduler._run_and_publish(provider, [{"relevant_file": "a.cpp"}]))
        assert summary["one_click"] == 1
        assert not provider.comments  # no overview comment for a single-location group

    def test_on_complete_is_awaited_with_the_raw_result(self, monkeypatch):
        async def fake_run_heavy_repair(git_provider, tasks):
            return {"one_click": [], "copy_patch": [], "failed": []}

        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "run_heavy_repair", fake_run_heavy_repair)

        received = {}

        async def fake_on_complete(result):
            received["result"] = result

        provider = _provider()
        asyncio.run(tier2_scheduler._run_and_publish(provider, [{"relevant_file": "a.cpp"}],
                                                      on_complete=fake_on_complete))
        assert received["result"] == {"one_click": [], "copy_patch": [], "failed": []}

    def test_on_complete_exception_never_raises(self, monkeypatch):
        async def fake_run_heavy_repair(git_provider, tasks):
            return {"one_click": [], "copy_patch": [], "failed": []}

        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "run_heavy_repair", fake_run_heavy_repair)

        async def raising_on_complete(result):
            raise RuntimeError("boom")

        provider = _provider()
        summary = asyncio.run(tier2_scheduler._run_and_publish(provider, [{"relevant_file": "a.cpp"}],
                                                                on_complete=raising_on_complete))
        assert summary == {"one_click": 0, "copy_patch": 0, "failed": 0}


class TestRenderHelpers:
    def test_render_text_only_fallback_lists_every_task(self):
        body = tier2_scheduler.render_text_only_fallback([("SUG-001", "r1"), ("SUG-002", "r2")])
        assert "SUG-001" in body and "r1" in body
        assert "SUG-002" in body and "r2" in body

    def test_render_multi_location_overview_includes_count_label_summary(self):
        body = tier2_scheduler.render_multi_location_overview(3, "并发性", "锁竞争风险")
        assert "3" in body
        assert "并发性" in body
        assert "锁竞争风险" in body

    def test_render_multi_location_overview_handles_missing_label_and_summary(self):
        body = tier2_scheduler.render_multi_location_overview(2, "", "")
        assert body  # never empty, falls back to placeholders