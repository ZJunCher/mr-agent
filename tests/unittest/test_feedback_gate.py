# tests/unittest/test_feedback_gate.py
from unittest.mock import MagicMock

import pr_agent.feedback.gate as gate
from pr_agent.config_loader import get_settings


class FakeProvider:
    def __init__(self, head="head1"):
        self._head = head
        self.calls = []

    def get_diff_refs(self):
        return {"head_sha": self._head} if self._head else None

    def set_commit_status(self, sha, state, context, description="", target_url=None):
        self.calls.append((sha, state, context))
        return True


def _enable(monkeypatch, enabled=True, ctx="pr-agent/feedback"):
    get_settings().set("pr_feedback.gate_enabled", enabled)
    get_settings().set("pr_feedback.gate_status_context", ctx)


def test_disabled_blocks_pending_but_allows_success(monkeypatch):
    # When the gate is disabled, locking (pending) is a no-op, but unlocking
    # (success) is still applied so a later /feedback can release a stuck MR.
    _enable(monkeypatch, enabled=False)
    p = FakeProvider(head="abc")
    gate.apply_pending(p)
    assert p.calls == []
    gate.apply_success(p)
    assert p.calls == [("abc", "success", "pr-agent/feedback")]


def test_apply_pending_sets_pending(monkeypatch):
    _enable(monkeypatch)
    p = FakeProvider(head="abc")
    gate.apply_pending(p)
    assert p.calls == [("abc", "pending", "pr-agent/feedback")]


def test_apply_success_sets_success(monkeypatch):
    _enable(monkeypatch)
    p = FakeProvider(head="def")
    gate.apply_success(p)
    assert p.calls == [("def", "success", "pr-agent/feedback")]


def test_restamp_success_when_feedback_exists(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(gate, "has_feedback", lambda project, mr_iid: True)
    p = FakeProvider(head="xyz")
    gate.restamp_on_push(p, "group/proj", "5")
    assert p.calls == [("xyz", "success", "pr-agent/feedback")]


def test_restamp_pending_when_no_feedback(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(gate, "has_feedback", lambda project, mr_iid: False)
    p = FakeProvider(head="xyz")
    gate.restamp_on_push(p, "group/proj", "5")
    assert p.calls == [("xyz", "pending", "pr-agent/feedback")]


def test_guidance_md_nonempty(monkeypatch):
    _enable(monkeypatch)
    assert isinstance(gate.guidance_md(), str)
    assert len(gate.guidance_md()) > 0
