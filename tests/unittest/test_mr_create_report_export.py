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
    get_settings().set("pr_feedback.gate_enabled", False)
    return inst


def test_download_link_appended_as_single_bold_line_when_report_export_succeeds(monkeypatch):
    monkeypatch.setattr(
        mr_mod, "build_and_upload_report",
        lambda git_provider, combined_md: (
            "https://gitlab.example.com/api/v4/projects/1/uploads/abc/report_mr1_deadbeef.md",
            "report_mr1_deadbeef.md",
        ),
    )
    inst = _make(monkeypatch, ["## Review\nbody"])
    asyncio.run(inst.run())
    published = inst.git_provider.publish_comment.call_args[0][0]
    # Single bold paragraph line (not a "## " heading) so it renders at
    # normal/smaller font size rather than the larger heading size used by
    # sections like "## PR 评审指南 🔍".
    assert "**下载报告📥 （可供本地AI确认修改）：**" in published
    assert "[report_mr1_deadbeef.md](https://gitlab.example.com/api/v4/projects/1/uploads/abc/report_mr1_deadbeef.md)" in published
    assert "## 📥" not in published


def test_no_download_link_when_report_export_returns_none(monkeypatch):
    monkeypatch.setattr(mr_mod, "build_and_upload_report", lambda git_provider, combined_md: None)
    inst = _make(monkeypatch, ["## Review\nbody"])
    asyncio.run(inst.run())
    published = inst.git_provider.publish_comment.call_args[0][0]
    assert "📥" not in published
    assert "下载报告" not in published


def test_report_export_receives_only_review_and_improve_text(monkeypatch):
    captured = {}

    def fake_export(git_provider, combined_md):
        captured["combined_md"] = combined_md
        return None

    monkeypatch.setattr(mr_mod, "build_and_upload_report", fake_export)
    # 3 sections popped in order by _safe_tool_run: review, improve, help.
    inst = _make(monkeypatch, ["## Review\nreview body", "## PR Code Suggestions ✨\nimprove body", "## PR-Agent 使用指引 🤖\nhelp body"])
    asyncio.run(inst.run())
    assert "review body" in captured["combined_md"]
    assert "improve body" in captured["combined_md"]
    # The help/usage-guide section must NOT be part of what's sent to the
    # report exporter, even though it's still part of the published comment.
    assert "help body" not in captured["combined_md"]
    assert "PR-Agent 使用指引" not in captured["combined_md"]


def test_help_section_still_appears_in_published_comment_even_when_excluded_from_report(monkeypatch):
    monkeypatch.setattr(mr_mod, "build_and_upload_report", lambda git_provider, combined_md: None)
    inst = _make(monkeypatch, ["## Review\nreview body", "## PR Code Suggestions ✨\nimprove body", "## PR-Agent 使用指引 🤖\nhelp body"])
    asyncio.run(inst.run())
    published = inst.git_provider.publish_comment.call_args[0][0]
    assert "help body" in published
