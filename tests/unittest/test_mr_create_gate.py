import asyncio
from unittest.mock import MagicMock

import pr_agent.tools.pr_mr_create as mr_mod
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_mr_create import PRMrCreate


def _make(monkeypatch, sections):
    inst = PRMrCreate.__new__(PRMrCreate)
    inst.git_provider = MagicMock()
    inst.git_provider.pr_url = "http://gl/mr/1"
    inst.ai_handler_cls = MagicMock()
    inst.args = []
    inst.llm_feedback = []

    async def fake_safe(_tool_name, _factory):
        return sections.pop(0) if sections else ""

    monkeypatch.setattr(inst, "_safe_tool_run", fake_safe)
    get_settings().set("config.publish_output", True)
    return inst


def test_gate_enabled_appends_guidance_and_pending(monkeypatch):
    get_settings().set("pr_feedback.gate_enabled", True)
    pending = {"n": 0}
    monkeypatch.setattr(mr_mod.gate, "apply_pending", lambda gp: pending.__setitem__("n", pending["n"] + 1))
    inst = _make(monkeypatch, ["## Review\nbody"])
    asyncio.run(inst.run())
    published = inst.git_provider.publish_comment.call_args[0][0]
    assert "评分" in published or "Please rate" in published
    assert pending["n"] == 1


def test_gate_disabled_no_guidance(monkeypatch):
    get_settings().set("pr_feedback.gate_enabled", False)
    calls = {"n": 0}
    monkeypatch.setattr(mr_mod.gate, "apply_pending", lambda gp: calls.__setitem__("n", calls["n"] + 1))
    inst = _make(monkeypatch, ["## Review\nbody"])
    asyncio.run(inst.run())
    published = inst.git_provider.publish_comment.call_args[0][0]
    assert "评分" not in published and "Please rate" not in published
    assert calls["n"] == 0
