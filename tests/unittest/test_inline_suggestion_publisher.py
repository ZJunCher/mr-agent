import json
import os
import tempfile

from pr_agent.config_loader import get_settings
from pr_agent.suggestions import inline_publisher as ip
from pr_agent.suggestions.review_tracking import (
    activate_review_run,
    get_review_run,
    start_review_run,
    update_review_run,
)
from pr_agent.suggestions.store import (get_published_suggestions,
                                        get_suggestion_threads)


def _sugg(**kw):
    base = {
        "relevant_file": "src/a.go",
        "relevant_lines_start": 10,
        "relevant_lines_end": 12,
        "label": "possible bug",
        "severity": "High",
        "score": 9,
        "one_sentence_summary": "fix off-by-one",
        "suggestion_content": "Why it's wrong: boom\nFix: swap order",
        "existing_code": "old",
        "improved_code": "new_code_line",
    }
    base.update(kw)
    return base


# ---------- project_allowed ----------

def test_project_allowed_empty_allowlist_denies():
    # Defense in depth: an empty allowlist must NOT open the feature to all
    # projects. Inline suggestions are an allowlist-gated rollout.
    assert ip.project_allowed("group/anything", []) is False
    assert ip.project_allowed("group/anything", None) is False


def test_project_allowed_matches_path():
    assert ip.project_allowed("group/cook", ["group/cook"]) is True
    assert ip.project_allowed("group/other", ["group/cook"]) is False


def test_project_allowed_matches_numeric_id_as_string():
    assert ip.project_allowed(123, ["123"]) is True
    assert ip.project_allowed(123, ["456"]) is False


def test_project_allowed_wildcard_opens_all_projects():
    # A literal "*" entry is an explicit global opt-in (rollout to all repos).
    assert ip.project_allowed("group/anything", ["*"]) is True
    assert ip.project_allowed(999, ["*"]) is True
    # "*" mixed with specific entries still opens everything.
    assert ip.project_allowed("group/other", ["group/cook", "*"]) is True


# ---------- self_reflect_allowed ----------

class _Prov:
    def __init__(self, pid):
        self.id_project = pid


def _set_reflect(enabled, allowlist):
    get_settings().set("pr_code_suggestions.inline_suggestions_enabled", enabled)
    get_settings().set("pr_code_suggestions.inline_suggestions_project_allowlist", allowlist)


def test_self_reflect_allowed_true_for_allowlisted_project():
    _set_reflect(True, ["group/cook"])
    assert ip.self_reflect_allowed(_Prov("group/cook")) is True


def test_self_reflect_allowed_false_when_master_switch_off():
    _set_reflect(False, ["group/cook"])
    assert ip.self_reflect_allowed(_Prov("group/cook")) is False


def test_self_reflect_allowed_false_for_non_allowlisted_project():
    _set_reflect(True, ["group/cook"])
    assert ip.self_reflect_allowed(_Prov("group/other")) is False


def test_self_reflect_allowed_false_when_allowlist_empty():
    _set_reflect(True, [])
    assert ip.self_reflect_allowed(_Prov("group/cook")) is False


def test_self_reflect_allowed_false_without_project_id():
    _set_reflect(True, ["group/cook"])
    assert ip.self_reflect_allowed(object()) is False


# ---------- make_suggestion_id ----------

def test_make_suggestion_id_is_zero_padded():
    assert ip.make_suggestion_id(1) == "SUG-001"
    assert ip.make_suggestion_id(23) == "SUG-023"


def test_native_and_fallback_bodies_share_stable_publish_marker():
    marker = ip.make_publish_marker("group/project:10:abcdef12", "SUG-001")
    native_body = f"{ip.render_inline_body(_sugg(), 'SUG-001', True)}\n\n<!-- {marker} -->"
    fallback_body = ip.render_fallback_body(_sugg(), marker, "https://gitlab/line", True)

    assert marker == "pr-agent-suggestion:group/project:10:abcdef12:SUG-001"
    assert f"<!-- {marker} -->" in native_body
    assert f"<!-- {marker} -->" in fallback_body
    assert "### 代码建议（已降级为普通评论）" in fallback_body
    assert "src/a.go:10-12" in fallback_body
    assert "fix off-by-one" in fallback_body
    assert "```diff" in fallback_body
    assert "-old" in fallback_body
    assert "+new_code_line" in fallback_body


# ---------- select_inline_candidates ----------

def test_select_keeps_good_suggestion():
    selected, skipped = ip.select_inline_candidates([_sugg()], min_score=7, max_lines=20)
    assert len(selected) == 1
    assert skipped == []


def test_select_skips_low_score():
    selected, skipped = ip.select_inline_candidates([_sugg(score=5)], min_score=7, max_lines=20)
    assert selected == []
    assert skipped[0][1] == "low_score"


def test_select_skips_missing_improved_code():
    selected, skipped = ip.select_inline_candidates([_sugg(improved_code="  ")], min_score=7, max_lines=20)
    assert selected == []
    assert skipped[0][1] == "no_improved_code"


def test_select_skips_missing_existing_code():
    selected, skipped = ip.select_inline_candidates([_sugg(existing_code="")], min_score=7, max_lines=20)
    assert selected == []
    assert skipped[0][1] == "no_existing_code"


def test_select_skips_too_large():
    selected, skipped = ip.select_inline_candidates(
        [_sugg(relevant_lines_start=10, relevant_lines_end=40)], min_score=7, max_lines=20)
    assert selected == []
    assert skipped[0][1] == "too_large"


def test_select_skips_invalid_lines():
    selected, skipped = ip.select_inline_candidates(
        [_sugg(relevant_lines_start=0, relevant_lines_end=0)], min_score=7, max_lines=20)
    assert selected == []
    assert skipped[0][1] == "invalid_lines"


def test_select_skips_missing_file():
    selected, skipped = ip.select_inline_candidates([_sugg(relevant_file="")], min_score=7, max_lines=20)
    assert selected == []
    assert skipped[0][1] == "no_file"


# ---------- render_inline_body ----------

def test_render_body_zh_contains_key_parts():
    body = ip.render_inline_body(_sugg(), "SUG-001", is_zh=True)
    assert "**PR-Agent" in body
    assert "possible bug" in body
    assert "高（9）" in body
    assert "问题：fix off-by-one" in body
    assert "```suggestion" in body
    assert "new_code_line" in body
    assert "<details>" in body
    assert "展开原因与验证" in body
    assert "<!-- pr-agent-suggestion:SUG-001 -->" in body


def test_render_body_en_contains_key_parts():
    body = ip.render_inline_body(_sugg(), "SUG-002", is_zh=False)
    assert "Issue: fix off-by-one" in body
    assert "High (9)" in body
    assert "<!-- pr-agent-suggestion:SUG-002 -->" in body


# ---------- orchestrator ----------

class _FakeGitLab:
    def __init__(self):
        self.id_project = "group/cook"
        self.id_mr = "10"
        self.diff_files = []
        self.published = None
        self.pr_url = "https://gitlab.example.com/group/cook/-/merge_requests/10"

    def get_diff_refs(self):
        return {"base_sha": "b", "head_sha": "h123456", "start_sha": "s"}

    def get_pr_url(self):
        return self.pr_url

    def publish_inline_suggestions(self, payloads):
        self.published = payloads
        return [
            {"suggestion_id": p["suggestion_id"], "discussion_id": "disc-" + p["suggestion_id"],
             "note_id": 1, "publish_status": "published", "skip_reason": ""}
            for p in payloads
        ]


class _FakeGitLabFallback(_FakeGitLab):
    def publish_inline_suggestions(self, payloads):
        self.published = payloads
        return [
            {
                "suggestion_id": payload["suggestion_id"],
                "discussion_id": None,
                "note_id": 999,
                "publish_status": "fallback_published",
                "skip_reason": "native_inline_rejected",
                "provider_error": "position not part of the diff",
                "attempt_count": 2,
                "positions": [
                    {"attempt": 1, "position": {"head_sha": "stale-head"}, "error": "rejected"},
                    {"attempt": 2, "position": {"head_sha": "fresh-head"}, "error": "rejected"},
                ],
            }
            for payload in payloads
        ]


def _enable(path, allowlist=None):
    get_settings().set("pr_code_suggestions.inline_suggestions_enabled", True)
    get_settings().set("pr_code_suggestions.inline_suggestions_on_mr_create", True)
    get_settings().set("pr_code_suggestions.inline_suggestion_min_score", 7)
    get_settings().set("pr_code_suggestions.inline_suggestion_max_lines", 20)
    get_settings().set("pr_code_suggestions.inline_suggestions_project_allowlist",
                       ["group/cook"] if allowlist is None else allowlist)
    get_settings().set("pr_code_suggestions.inline_suggestions_storage_path", path)
    # Phase 2 self-check needs an LLM; keep selection/gate/publish tests offline.
    # Dedicated phase-2 integration tests below stub run_phase2 explicitly.
    get_settings().set("pr_code_suggestions.inline_selfcheck_enabled", False)
    get_settings().set("pr_code_suggestions.inline_conflict_check_enabled", False)
    # These tests exercise the legacy heuristic gate / LLM self-check path
    # directly; pipeline_v2_enabled must be explicitly false here regardless
    # of the global default, since Task 12 skips gate/phase2 entirely when
    # it's true.
    get_settings().set("pr_code_suggestions.pipeline_v2_enabled", False)


def test_orchestrator_publishes_and_persists():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        gp = _FakeGitLab()
        summary = ip.publish_inline_suggestions(gp, [_sugg(), _sugg(score=5)])
        assert summary["published"] == 1
        assert summary["skipped"] == 1
        assert summary["published_locations"][0]["suggestion_id"] == "SUG-001"
        # provider received exactly the 1 selected payload with a rendered body
        assert len(gp.published) == 1
        assert "```suggestion" in gp.published[0]["body"]
        pub_rows = get_published_suggestions("group/cook", "10", path=path)
        skip_rows = get_suggestion_threads("group/cook", "10", path=path)
        assert len(pub_rows) == 1
        assert len(skip_rows) == 1
        assert skip_rows[0]["publish_status"] == "skipped"


def test_orchestrator_tracks_fallback_delivery_separately():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "s.db")
        _enable(path)
        run_id = start_review_run({
            "project_path": "group/cook", "mr_iid": "10", "trigger": "auto_mr_create",
        }, path=path)
        update_review_run(run_id, path=path, generated_count=1, improve_started_at="2026-08-18T12:00:00+08:00")
        gp = _FakeGitLabFallback()

        with activate_review_run(run_id):
            summary = ip.publish_inline_suggestions(gp, [_sugg()], store_path=path)

        assert summary["fallback_published"] == 1
        assert summary["published"] == 0
        assert summary["failed"] == 0
        assert summary["published_locations"][0]["note_url"].endswith("#note_999")
        run = get_review_run(run_id, path=path)
        assert run["inline_fallback_count"] == 1
        assert run["inline_failed_count"] == 0
        saved = get_suggestion_threads("group/cook", "10", path=path)[0]
        assert saved["publish_status"] == "fallback_published"
        extra = json.loads(saved["extra_json"])
        assert extra["provider_error"] == "position not part of the diff"
        assert extra["attempt_count"] == 2
        assert [item["position"]["head_sha"] for item in extra["positions"]] == [
            "stale-head", "fresh-head",
        ]


def test_orchestrator_persists_real_mr_url_on_published_and_skipped_rows():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        gp = _FakeGitLab()
        ip.publish_inline_suggestions(gp, [_sugg(), _sugg(score=5)])
        pub_rows = get_published_suggestions("group/cook", "10", path=path)
        skip_rows = get_suggestion_threads("group/cook", "10", path=path)
        assert pub_rows[0]["mr_url"] == "https://gitlab.example.com/group/cook/-/merge_requests/10"
        assert skip_rows[0]["mr_url"] == "https://gitlab.example.com/group/cook/-/merge_requests/10"


def test_orchestrator_noop_when_disabled():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_code_suggestions.inline_suggestions_enabled", False)
        gp = _FakeGitLab()
        summary = ip.publish_inline_suggestions(gp, [_sugg()])
        assert summary["published"] == 0
        assert gp.published is None


def test_orchestrator_noop_when_project_not_allowed():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path, allowlist=["group/other"])
        gp = _FakeGitLab()
        summary = ip.publish_inline_suggestions(gp, [_sugg()])
        assert summary["published"] == 0
        assert gp.published is None


def test_orchestrator_noop_when_allowlist_empty():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path, allowlist=[])
        gp = _FakeGitLab()
        summary = ip.publish_inline_suggestions(gp, [_sugg()])
        assert summary["published"] == 0
        assert gp.published is None


# ---------- inline_feature_enabled: per-entry-point source gating ----------

def test_inline_feature_enabled_default_source_checks_mr_create_switch():
    get_settings().set("pr_code_suggestions.inline_suggestions_enabled", True)
    get_settings().set("pr_code_suggestions.inline_suggestions_on_mr_create", True)
    get_settings().set("pr_code_suggestions.inline_suggestions_on_improve_command", False)
    assert ip.inline_feature_enabled() is True
    assert ip.inline_feature_enabled(source="mr_create") is True
    assert ip.inline_feature_enabled(source="improve_command") is False


def test_inline_feature_enabled_improve_command_source_checks_its_own_switch():
    get_settings().set("pr_code_suggestions.inline_suggestions_enabled", True)
    get_settings().set("pr_code_suggestions.inline_suggestions_on_mr_create", False)
    get_settings().set("pr_code_suggestions.inline_suggestions_on_improve_command", True)
    assert ip.inline_feature_enabled(source="improve_command") is True
    assert ip.inline_feature_enabled(source="mr_create") is False


def test_inline_feature_enabled_false_when_master_switch_off_regardless_of_source():
    get_settings().set("pr_code_suggestions.inline_suggestions_enabled", False)
    get_settings().set("pr_code_suggestions.inline_suggestions_on_mr_create", True)
    get_settings().set("pr_code_suggestions.inline_suggestions_on_improve_command", True)
    assert ip.inline_feature_enabled(source="mr_create") is False
    assert ip.inline_feature_enabled(source="improve_command") is False


def test_orchestrator_noop_when_improve_command_switch_off():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_code_suggestions.inline_suggestions_on_improve_command", False)
        gp = _FakeGitLab()
        summary = ip.publish_inline_suggestions(gp, [_sugg()], source="improve_command")
        assert summary["published"] == 0
        assert gp.published is None


def test_orchestrator_publishes_for_improve_command_source_when_its_switch_is_on():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_code_suggestions.inline_suggestions_on_mr_create", False)
        get_settings().set("pr_code_suggestions.inline_suggestions_on_improve_command", True)
        gp = _FakeGitLab()
        summary = ip.publish_inline_suggestions(gp, [_sugg()], source="improve_command")
        assert summary["published"] == 1


def test_orchestrator_noop_for_unsupported_provider():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)

        class _NoInline:
            id_project = "group/cook"
            id_mr = "10"

        summary = ip.publish_inline_suggestions(_NoInline(), [_sugg()])
        assert summary["published"] == 0


# ---------- collapsed suggestion block ----------

def test_render_body_collapsed_wraps_suggestion_block():
    get_settings().set("pr_code_suggestions.inline_suggestion_collapsed", True)
    body = ip.render_inline_body(_sugg(), "SUG-001", is_zh=True)
    assert "查看修改建议（点击展开）" in body
    # suggestion block must be inside a <details> section
    details_start = body.index("查看修改建议（点击展开）")
    assert body.index("```suggestion") > details_start
    assert "**PR-Agent" in body  # header stays visible
    assert "问题：fix off-by-one" in body  # issue line stays visible


def test_render_body_not_collapsed_when_disabled():
    get_settings().set("pr_code_suggestions.inline_suggestion_collapsed", False)
    body = ip.render_inline_body(_sugg(), "SUG-001", is_zh=True)
    assert "查看修改建议（点击展开）" not in body
    assert "```suggestion" in body


def test_render_body_collapsed_en():
    get_settings().set("pr_code_suggestions.inline_suggestion_collapsed", True)
    body = ip.render_inline_body(_sugg(), "SUG-001", is_zh=False)
    assert "View suggested change (click to expand)" in body


def test_configuration_toml_defaults_to_expanded():
    # Suggestions must be visible at first glance (not hidden behind a click),
    # so the shipped default in configuration.toml must be false. Read the raw
    # file (not get_settings()) so this isn't affected by other tests' mutations.
    toml_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "pr_agent", "settings", "configuration.toml",
    )
    with open(toml_path, encoding="utf-8") as f:
        content = f.read()
    assert "inline_suggestion_collapsed = false" in content


# ---------- gate integration in orchestrator ----------

def test_orchestrator_gate_blocks_and_persists_reason():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_code_suggestions.inline_gate_enabled", True)
        get_settings().set("pr_code_suggestions.inline_gate_check_speculative", True)
        get_settings().set("pr_code_suggestions.inline_gate_speculative_labels", ["performance"])
        gp = _FakeGitLab()
        summary = ip.publish_inline_suggestions(gp, [_sugg(), _sugg(label="performance")])
        assert summary["published"] == 1
        assert summary["skipped"] == 1
        assert len(gp.published) == 1
        rows = get_suggestion_threads("group/cook", "10", path=path)
        reasons = {r["skip_reason"] for r in rows if r["publish_status"] == "skipped"}
        assert "speculative" in reasons


def test_orchestrator_gate_disabled_publishes_all():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_agent_dummy", None)  # noop
        get_settings().set("pr_code_suggestions.inline_gate_enabled", False)
        gp = _FakeGitLab()
        summary = ip.publish_inline_suggestions(gp, [_sugg(label="performance")])
        assert summary["published"] == 1
        assert summary["skipped"] == 0

# ---------- phase 2 self-check integration in orchestrator ----------

def test_orchestrator_phase2_blocks_and_persists_reason(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_code_suggestions.inline_selfcheck_enabled", True)

        async def fake_phase2(git_provider, suggestions, ai_handler=None):
            # block the second candidate as a self-check failure
            return suggestions[:1], [(suggestions[1], "selfcheck_safe_to_apply")]

        monkeypatch.setattr(ip, "run_phase2", fake_phase2)
        gp = _FakeGitLab()
        summary = ip.publish_inline_suggestions(gp, [_sugg(), _sugg()])
        assert summary["published"] == 1
        assert summary["skipped"] == 1
        assert len(gp.published) == 1
        rows = get_suggestion_threads("group/cook", "10", path=path)
        reasons = {r["skip_reason"] for r in rows if r["publish_status"] == "skipped"}
        assert "selfcheck_safe_to_apply" in reasons


def test_orchestrator_phase2_rewrite_publishes_new_code(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_code_suggestions.inline_conflict_check_enabled", True)

        async def fake_phase2(git_provider, suggestions, ai_handler=None):
            rewritten = dict(suggestions[0])
            rewritten["improved_code"] = "deconflicted_line"
            rewritten["rewritten"] = True
            return [rewritten], []

        monkeypatch.setattr(ip, "run_phase2", fake_phase2)
        gp = _FakeGitLab()
        summary = ip.publish_inline_suggestions(gp, [_sugg()])
        assert summary["published"] == 1
        assert "deconflicted_line" in gp.published[0]["body"]
        published = get_published_suggestions("group/cook", "10", path=path)
        assert published and published[0]["extra_json"] and "rewritten" in published[0]["extra_json"]


# ---------- gate guidance + lock-on-publish ----------

def test_render_body_zh_contains_gate_guidance():
    body = ip.render_inline_body(_sugg(), "SUG-010", is_zh=True)
    assert "解决主题" in body
    assert "应用建议" in body


def test_render_body_en_contains_gate_guidance():
    body = ip.render_inline_body(_sugg(), "SUG-011", is_zh=False)
    assert "Resolve thread" in body or "resolve" in body.lower()
    assert "Apply" in body or "apply" in body.lower()


class _FakeGitLabWithStatus(_FakeGitLab):
    def __init__(self):
        super().__init__()
        self.status_calls = []

    def set_commit_status(self, sha, state, context, description="", target_url=None):
        self.status_calls.append((sha, state, context))
        return True


def test_orchestrator_locks_gate_when_enabled_and_project_allowlisted():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_inline_suggestion_gate.gate_enabled", True)
        get_settings().set("pr_inline_suggestion_gate.gate_project_allowlist", ["group/cook"])
        # Set explicitly rather than relying on the config default, so this
        # test is independent of what other test files/modules may have
        # left in the shared settings singleton.
        get_settings().set("pr_inline_suggestion_gate.gate_status_context", "pr-agent/inline-suggestions（请查看下方建议）")
        gp = _FakeGitLabWithStatus()
        ip.publish_inline_suggestions(gp, [_sugg()])
        assert gp.status_calls == [("h123456", "pending", "pr-agent/inline-suggestions（请查看下方建议）")]


def test_orchestrator_does_not_lock_gate_when_project_not_in_gate_allowlist():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_inline_suggestion_gate.gate_enabled", True)
        get_settings().set("pr_inline_suggestion_gate.gate_project_allowlist", ["group/other"])
        gp = _FakeGitLabWithStatus()
        ip.publish_inline_suggestions(gp, [_sugg()])
        assert gp.status_calls == []


def test_orchestrator_does_not_lock_gate_when_nothing_published():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_inline_suggestion_gate.gate_enabled", True)
        get_settings().set("pr_inline_suggestion_gate.gate_project_allowlist", ["group/cook"])
        gp = _FakeGitLabWithStatus()
        ip.publish_inline_suggestions(gp, [_sugg(score=1)])  # below min_score, nothing published
        assert gp.status_calls == []


def test_orchestrator_does_not_lock_gate_when_gate_disabled():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        get_settings().set("pr_inline_suggestion_gate.gate_enabled", False)
        get_settings().set("pr_inline_suggestion_gate.gate_project_allowlist", ["group/cook"])
        gp = _FakeGitLabWithStatus()
        ip.publish_inline_suggestions(gp, [_sugg()])
        assert gp.status_calls == []


# ---------- backfill_note_urls ----------

def test_backfill_sets_inline_note_url_on_matching_suggestion():
    suggestions = [
        {"relevant_file": "a.py", "relevant_lines_start": 10, "relevant_lines_end": 12, "score": 9},
        {"relevant_file": "b.py", "relevant_lines_start": 5, "relevant_lines_end": 5, "score": 7},
    ]
    published_locations = [
        {"relevant_file": "a.py", "relevant_lines_start": 10, "relevant_lines_end": 12,
         "note_url": "https://gl/mr/1#note_1"},
    ]
    ip.backfill_note_urls(suggestions, published_locations)
    assert suggestions[0]["inline_note_url"] == "https://gl/mr/1#note_1"
    assert "inline_note_url" not in suggestions[1]


def test_backfill_leaves_suggestions_untouched_when_no_locations():
    suggestions = [{"relevant_file": "a.py", "relevant_lines_start": 10, "relevant_lines_end": 12}]
    ip.backfill_note_urls(suggestions, [])
    assert "inline_note_url" not in suggestions[0]


def test_backfill_ignores_locations_missing_note_url():
    suggestions = [{"relevant_file": "a.py", "relevant_lines_start": 10, "relevant_lines_end": 12}]
    published_locations = [
        {"relevant_file": "a.py", "relevant_lines_start": 10, "relevant_lines_end": 12, "note_url": None},
    ]
    ip.backfill_note_urls(suggestions, published_locations)
    assert "inline_note_url" not in suggestions[0]


def test_backfill_matches_only_exact_line_range():
    suggestions = [{"relevant_file": "a.py", "relevant_lines_start": 10, "relevant_lines_end": 12}]
    published_locations = [
        {"relevant_file": "a.py", "relevant_lines_start": 10, "relevant_lines_end": 11,
         "note_url": "https://gl/mr/1#note_1"},
    ]
    ip.backfill_note_urls(suggestions, published_locations)
    assert "inline_note_url" not in suggestions[0]


def test_backfill_returns_only_successful_suggestion_id_when_locations_match():
    suggestions = [
        {"_inline_suggestion_id": "SUG-001", "relevant_file": "a.py",
         "relevant_lines_start": 10, "relevant_lines_end": 12},
        {"_inline_suggestion_id": "SUG-002", "relevant_file": "a.py",
         "relevant_lines_start": 10, "relevant_lines_end": 12},
    ]
    published_locations = [{
        "suggestion_id": "SUG-001",
        "relevant_file": "a.py",
        "relevant_lines_start": 10,
        "relevant_lines_end": 12,
        "note_url": "https://gl/mr/1#note_1",
    }]

    published = ip.backfill_note_urls(suggestions, published_locations)

    assert published == [suggestions[0]]
    assert suggestions[0]["inline_note_url"] == "https://gl/mr/1#note_1"
    assert "inline_note_url" not in suggestions[1]


# ---------- prompt provenance persistence (Task 5) ----------
def test_publisher_persists_prompt_provenance():
    from pr_agent.suggestions.prompt_provenance import PromptProvenance

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        _enable(path)
        gp = _FakeGitLab()
        provenance = PromptProvenance("g-hash", "r-hash", "b-hash", "2026-w34")
        ip.publish_inline_suggestions(gp, [_sugg()], prompt_provenance=provenance)
        rows = get_published_suggestions("group/cook", "10", path=path)
        assert rows
        assert rows[0]["global_prompt_set_hash"] == "g-hash"
        assert rows[0]["project_rules_hash"] == "r-hash"
        assert rows[0]["prompt_bundle_hash"] == "b-hash"
        assert rows[0]["prompt_version"] == "2026-w34"
