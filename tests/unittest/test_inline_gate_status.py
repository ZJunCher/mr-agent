from unittest.mock import MagicMock

import pr_agent.suggestions.inline_gate_status as gate
from pr_agent.config_loader import get_settings


class FakeProvider:
    def __init__(self, head="head1"):
        self._head = head
        self.calls = []

    def get_diff_refs(self):
        return {"head_sha": self._head} if self._head else None

    def set_commit_status(self, sha, state, context, description="", target_url=None):
        self.calls.append((sha, state, context, description))
        return True


def _enable(enabled=True, allowlist=None, ctx="pr-agent/inline-suggestions"):
    get_settings().set("pr_inline_suggestion_gate.gate_enabled", enabled)
    get_settings().set("pr_inline_suggestion_gate.gate_status_context", ctx)
    get_settings().set(
        "pr_inline_suggestion_gate.gate_project_allowlist",
        ["cook", "chogori"] if allowlist is None else allowlist,
    )


def test_is_enabled_true_without_project_id():
    _enable(enabled=True)
    assert gate.is_enabled() is True


def test_is_enabled_false_when_master_switch_off():
    _enable(enabled=False)
    assert gate.is_enabled("cook") is False


def test_is_enabled_true_for_allowlisted_project():
    _enable(enabled=True)
    assert gate.is_enabled("cook") is True


def test_is_enabled_false_for_non_allowlisted_project():
    _enable(enabled=True)
    assert gate.is_enabled("other-project") is False


def test_is_enabled_true_for_full_path_matching_by_basename():
    # git_provider.id_project is always the full "namespace/repo" path (e.g.
    # "eabot/cook"), while the allowlist is configured with bare repo names
    # for readability. Basename matching must bridge that gap.
    _enable(enabled=True)
    assert gate.is_enabled("eabot/cook") is True
    assert gate.is_enabled("eabot/chogori") is True


def test_is_enabled_false_for_full_path_not_matching_any_allowlisted_repo():
    _enable(enabled=True)
    assert gate.is_enabled("eabot/some-other-repo") is False


def test_is_enabled_true_for_exact_full_path_configured():
    _enable(enabled=True, allowlist=["eabot/cook"])
    assert gate.is_enabled("eabot/cook") is True
    assert gate.is_enabled("otherorg/cook") is False


def test_disabled_blocks_pending_but_allows_success():
    _enable(enabled=False)
    p = FakeProvider(head="abc")
    gate.apply_pending(p, project_id="cook")
    assert p.calls == []
    gate.apply_success(p, project_id="cook")
    assert p.calls == [("abc", "success", "pr-agent/inline-suggestions", gate._description("success"))]


def test_apply_pending_sets_pending_for_allowlisted_project():
    _enable(enabled=True)
    p = FakeProvider(head="abc")
    gate.apply_pending(p, project_id="cook")
    assert p.calls == [("abc", "pending", "pr-agent/inline-suggestions", gate._description("pending"))]


def test_apply_pending_noop_for_non_allowlisted_project():
    _enable(enabled=True)
    p = FakeProvider(head="abc")
    gate.apply_pending(p, project_id="some-other-repo")
    assert p.calls == []


def test_recompute_success_when_all_threads_satisfied(monkeypatch):
    _enable(enabled=True)
    monkeypatch.setattr(gate, "all_threads_satisfied", lambda project, mr_iid, path=None: True)
    p = FakeProvider(head="xyz")
    gate.recompute(p, "cook", "5")
    assert p.calls == [("xyz", "success", "pr-agent/inline-suggestions", gate._description("success"))]


def test_recompute_pending_when_not_all_threads_satisfied(monkeypatch):
    _enable(enabled=True)
    monkeypatch.setattr(gate, "all_threads_satisfied", lambda project, mr_iid, path=None: False)
    p = FakeProvider(head="xyz")
    gate.recompute(p, "cook", "5")
    assert p.calls == [("xyz", "pending", "pr-agent/inline-suggestions", gate._description("pending"))]


def test_status_context_default_nudges_reviewer_to_suggestions_below():
    # Fallback constant baked into status_context() itself (used when the key
    # is entirely absent, e.g. a fresh settings load with no explicit
    # override) must nudge reviewers to scroll down to the suggestions.
    import inspect
    assert "请查看下方建议" in inspect.getsource(gate.status_context)


def test_description_zh_guides_reviewer_to_suggestions_below():
    get_settings().set("config.response_language", "zh-CN")
    assert "下方建议" in gate._description("pending")
    assert "可以合并" in gate._description("success")


def test_description_en_fallback():
    get_settings().set("config.response_language", "en-US")
    assert "below" in gate._description("pending").lower()
    assert "merge" in gate._description("success").lower()
