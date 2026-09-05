from unittest.mock import MagicMock

import pr_agent.servers.gitlab_webhook as wh
from pr_agent.config_loader import get_settings


def _payload():
    return {
        "object_kind": "merge_request",
        "project": {"id": 42},
        "object_attributes": {
            "action": "update",
            "iid": 5,
            "url": "http://gl/group/proj/-/merge_requests/5",
        },
    }


def test_push_restamps_when_enabled(monkeypatch):
    get_settings().set("pr_feedback.gate_enabled", True)
    fake_gp = MagicMock()
    fake_gp.id_project = "group/proj"
    fake_gp.id_mr = 5
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)
    seen = {}
    monkeypatch.setattr(wh.gate, "restamp_on_push",
                        lambda gp, project, mr_iid: seen.update(project=project, mr_iid=mr_iid))
    wh._handle_feedback_gate_push(_payload())
    assert seen == {"project": "group/proj", "mr_iid": 5}
    # Guard: the numeric payload id must NOT be what is passed to restamp_on_push
    assert seen["project"] != 42, "Bug regression: numeric payload id was used instead of provider id_project"


def test_push_noop_when_disabled(monkeypatch):
    get_settings().set("pr_feedback.gate_enabled", False)
    called = {"n": 0}
    monkeypatch.setattr(wh.gate, "restamp_on_push",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    wh._handle_feedback_gate_push(_payload())
    assert called["n"] == 0
