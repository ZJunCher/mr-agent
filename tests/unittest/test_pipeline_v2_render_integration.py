"""Tests for Pipeline v2 render integration in inline_publisher.py: when
pipeline_v2_enabled is true, the legacy heuristic gate (inline_gate.py) and
LLM self-check/de-conflict (inline_selfcheck.py) are skipped, since
deterministic_fix.py + tier1_repair.py already validated/repaired every
candidate upstream. resolved_by_stage is threaded through to the store.
"""
import asyncio
import types

from pr_agent.config_loader import get_settings
from pr_agent.suggestions import inline_publisher


def _provider(files):
    diff_files = [types.SimpleNamespace(filename=f, head_file=h) for f, h in files]
    provider = types.SimpleNamespace()
    provider.diff_files = diff_files
    provider.get_diff_files = lambda: diff_files
    provider.id_project = "group/cook"
    provider.id_mr = "10"
    provider.pr_url = "https://gitlab.example.com/group/cook/-/merge_requests/10"
    provider.get_diff_refs = lambda: {"head_sha": "abc123def456"}
    provider.get_pr_url = lambda: provider.pr_url
    provider.publish_inline_suggestions = lambda payloads: [
        {"suggestion_id": p["suggestion_id"], "discussion_id": f"disc-{p['suggestion_id']}",
         "note_id": f"note-{p['suggestion_id']}", "publish_status": "published", "skip_reason": ""}
        for p in payloads
    ]
    return provider


def _sugg(**kw):
    base = {
        "relevant_file": "src/foo.cpp", "existing_code": "void bar();", "improved_code": "void bar() const;",
        "relevant_lines_start": 5, "relevant_lines_end": 5, "score": 8, "label": "correctness",
        "one_sentence_summary": "make const", "suggestion_content": "should be const",
    }
    base.update(kw)
    return base


def _set_defaults():
    s = get_settings()
    s.set("pr_code_suggestions.inline_suggestions_enabled", True)
    s.set("pr_code_suggestions.inline_suggestions_on_mr_create", True)
    s.set("pr_code_suggestions.inline_suggestions_project_allowlist", ["*"])
    s.set("pr_code_suggestions.inline_suggestion_min_score", 0)
    s.set("pr_code_suggestions.inline_suggestion_max_lines", 20)
    s.set("pr_code_suggestions.pipeline_v2_enabled", False)


class TestPipelineV2SkipsLegacyGateAndPhase2:
    def test_legacy_path_still_runs_gate_and_phase2_by_default(self, monkeypatch):
        _set_defaults()
        calls = {"gate": 0, "phase2": 0}

        def fake_gate(git_provider, selected):
            calls["gate"] += 1
            return selected, []

        async def fake_phase2(git_provider, selected, ai_handler=None):
            calls["phase2"] += 1
            return selected, []

        monkeypatch.setattr(inline_publisher, "gate_suggestions", fake_gate)
        monkeypatch.setattr(inline_publisher, "run_phase2", fake_phase2)

        provider = _provider([("src/foo.cpp", "void bar();")])
        asyncio.run(inline_publisher.publish_inline_suggestions_async(provider, [_sugg()]))
        assert calls["gate"] == 1 and calls["phase2"] == 1

    def test_pipeline_v2_skips_gate_and_phase2(self, monkeypatch):
        _set_defaults()
        get_settings().set("pr_code_suggestions.pipeline_v2_enabled", True)
        calls = {"gate": 0, "phase2": 0}

        def fake_gate(git_provider, selected):
            calls["gate"] += 1
            return selected, []

        async def fake_phase2(git_provider, selected, ai_handler=None):
            calls["phase2"] += 1
            return selected, []

        monkeypatch.setattr(inline_publisher, "gate_suggestions", fake_gate)
        monkeypatch.setattr(inline_publisher, "run_phase2", fake_phase2)

        provider = _provider([("src/foo.cpp", "void bar();")])
        try:
            summary = asyncio.run(
                inline_publisher.publish_inline_suggestions_async(provider, [_sugg()]))
        finally:
            get_settings().set("pr_code_suggestions.pipeline_v2_enabled", False)
        assert calls["gate"] == 0 and calls["phase2"] == 0
        assert summary["published"] == 1

    def test_resolved_by_stage_is_saved_to_store(self, monkeypatch, tmp_path):
        _set_defaults()
        get_settings().set("pr_code_suggestions.pipeline_v2_enabled", True)
        db_path = str(tmp_path / "s.db")
        provider = _provider([("src/foo.cpp", "void bar();")])
        try:
            asyncio.run(inline_publisher.publish_inline_suggestions_async(
                provider, [_sugg(resolved_by_stage="deterministic_fix")], store_path=db_path))
        finally:
            get_settings().set("pr_code_suggestions.pipeline_v2_enabled", False)

        from pr_agent.suggestions.store import get_published_suggestions
        rows = get_published_suggestions("group/cook", "10", path=db_path)
        assert len(rows) == 1
        assert rows[0]["resolved_by_stage"] == "deterministic_fix"