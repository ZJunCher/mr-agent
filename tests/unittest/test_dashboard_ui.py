from fastapi import FastAPI
from fastapi.testclient import TestClient

import pr_agent.servers.dashboard_routes as dashboard_module
from pr_agent.servers.dashboard_routes import (
    _inline_dashboard_html,
    _repair_memory_dashboard_html,
    _triage_dashboard_html,
    router,
)
from tests.unittest.repair_memory_helpers import sample_memory
from ut_agent.repair_memory.audit import initialize_retrieval_audit
from ut_agent.repair_memory.models import MemoryStatus, RetrievalMode
from ut_agent.repair_memory.store import (
    init_repair_memory_tables,
    list_memory_events,
    load_memory,
    save_memory,
)


def _repair_memory_client(tmp_path, monkeypatch) -> tuple[TestClient, str]:
    db_path = str(tmp_path / "dashboard-memory.db")
    init_repair_memory_tables(db_path)
    monkeypatch.setattr(dashboard_module, "get_feedback_db_path", lambda: db_path)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), db_path


def test_inline_dashboard_preserves_behavior_and_uses_ops_layout():
    html = _inline_dashboard_html()

    assert "fetch('/api/inline/summary')" in html
    assert html.count("pageSize: 10") == 3
    assert "type: 'bar'" in html
    assert "type: 'line'" in html
    for element_id in (
        "loadedAt",
        "mPub",
        "mApp",
        "mPct",
        "mFb",
        "projectChart",
        "trendChart",
        "projectTableBody",
        "mrTableBody",
        "feedbackTableBody",
    ):
        assert f'id="{element_id}"' in html
    for href in ("/dashboard/inline", "/dashboard/triage", "/dashboard/suggestion-filter"):
        assert f'href="{href}"' in html

    assert 'class="ops-shell"' in html
    assert "@media (prefers-reduced-motion: reduce)" in html
    assert ":focus-visible" in html
    assert "overflow-x: auto" in html


def test_triage_dashboard_preserves_behavior_and_uses_text_status_badges():
    html = _triage_dashboard_html()

    assert "fetch('/api/triage/summary?days=30&page=1')" in html
    assert "type: 'bar'" in html
    assert "type: 'line'" in html
    for element_id in (
        "loadedAt",
        "mTotal",
        "mSR",
        "mBlocked",
        "mIters",
        "mDur",
        "catChart",
        "trendChart",
        "runsBody",
        "triagePageInfo",
        "triagePageNumbers",
        "triagePrevPage",
        "triageNextPage",
    ):
        assert f'id="{element_id}"' in html
    assert html.count("createPager({") == 1  # helper definition only; Triage uses server-side pagination
    assert "data.recent_rows.forEach" in html

    assert 'class="ops-shell"' in html
    assert "status-badge" in html
    assert "成功" in html
    assert "失败" in html
    assert "外部依赖阻塞" in html
    assert "r.repair_outcome === 'blocked'" in html
    assert "escapeHtml(r.blocker_summary)" in html
    assert "category-chip" in html
    assert "作者（GitLab）" in html
    assert "作者（飞书）" not in html
    assert "escapeHtml(r.actor)" in html


def test_operations_layout_is_scoped_to_target_dashboards():
    inline_html = _inline_dashboard_html()
    triage_html = _triage_dashboard_html()

    for html in (inline_html, triage_html):
        assert '<body class="ops-dashboard">' in html
        assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in html
        assert "@media (max-width: 1100px)" in html
        assert "@media (max-width: 640px)" in html
        assert 'aria-current="page"' in html


def test_repair_memory_dashboard_has_soft_delete_restore_controls():
    html = _repair_memory_dashboard_html()

    assert "m.status === 'disabled' ? 'enable'" in html
    assert "['active', 'needs_review'].includes(m.status) ? 'disable'" in html
    assert "window.confirm" in html
    assert "window.prompt" in html
    assert "请填写${actionLabel}原因（必填，1-500 字）" in html
    assert "await loadMemories()" in html
    assert "location.reload" not in html
    assert "只影响之后启动的检索" in html
    assert "已经进入运行中 Agent 上下文的经验不会被中途撤回" in html
    assert "失败，请稍后重试" in html
    assert "console.error" not in html
    assert "/app/data" not in html


def test_repair_memory_dashboard_defaults_to_current_memories():
    html = _repair_memory_dashboard_html()

    assert "当前有效经验" in html
    assert '<option value="active">当前有效</option>' in html
    assert '<option value="all">全部状态</option>' in html
    assert "默认仅展示当前有效的中文版本" in html


def test_repair_memory_dashboard_distinguishes_recent_retrieval_states():
    html = _repair_memory_dashboard_html()

    for text in (
        "最近检索记录",
        "未执行检索",
        "已检索，无匹配经验",
        "已召回，已注入 Hermes",
        "已召回，仅影子评估",
        "已召回，未注入 Hermes",
        "检索异常",
        "历史数据未知",
        "查看候选评分",
        "语义相似度不足",
        "总分未达到阈值",
        "已过阈值，未选入",
        "得分构成",
        "/api/repair-memory/retrieval-audits",
    ):
        assert text in html
    assert 'id="retrievalAuditList"' in html
    assert "function loadRetrievalAudits(" in html
    assert '<body class="ops-dashboard ops-dashboard-light">' in html
    assert html.index('id="memoryCardsSection"') < html.index('id="retrievalAuditSection"')
    for contract in (
        "const RETRIEVAL_PAGE_SIZE = 15",
        "let currentRetrievalPage = 1",
        'id="retrievalPagination"',
        'id="retrievalPrev"',
        'id="retrievalNext"',
        'id="retrievalPageInfo"',
        "params.set('page_size', String(RETRIEVAL_PAGE_SIZE))",
    ):
        assert contract in html


def test_repair_memory_dashboard_uses_compact_expandable_paginated_cards():
    html = _repair_memory_dashboard_html()

    for contract in (
        "const MEMORY_PAGE_SIZE = 10",
        "let memoryRows = []",
        "let currentMemoryPage = 1",
        "let expandedMemoryId = ''",
        "function renderMemoryList()",
        "function toggleMemoryDetails(memoryId)",
        'aria-expanded="${expanded}"',
        'id="memPagination"',
        'id="memPageSummary"',
        "currentMemoryPage = 1",
        "expandedMemoryId = ''",
    ):
        assert contract in html
    assert "mem-problem-clamp" in html
    assert "mem-card-details" in html
    assert "mem-page-button" in html
    assert "@media (prefers-reduced-motion: reduce)" in html


def test_repair_memory_api_hides_superseded_memories_by_default(tmp_path, monkeypatch):
    client, db_path = _repair_memory_client(tmp_path, monkeypatch)
    assert save_memory(sample_memory("active-memory", pattern_key="active-pattern"), db_path)
    assert save_memory(
        sample_memory(
            "superseded-memory",
            pattern_key="superseded-pattern",
            status=MemoryStatus.SUPERSEDED,
        ),
        db_path,
    )

    current = client.get("/api/repair-memory/memories")
    history = client.get("/api/repair-memory/memories?status=superseded")
    all_versions = client.get("/api/repair-memory/memories?status=all")

    assert current.status_code == 200
    assert [memory["memory_id"] for memory in current.json()["memories"]] == ["active-memory"]
    assert history.status_code == 200
    assert [memory["memory_id"] for memory in history.json()["memories"]] == ["superseded-memory"]
    assert all_versions.status_code == 200
    assert {memory["memory_id"] for memory in all_versions.json()["memories"]} == {
        "active-memory",
        "superseded-memory",
    }


def test_repair_memory_recent_retrieval_audit_api(tmp_path, monkeypatch):
    client, db_path = _repair_memory_client(tmp_path, monkeypatch)
    assert initialize_retrieval_audit(
        task_id="task-dashboard", project="group/a", mr_iid=7,
        source_pipeline_id=100, source_sha="a" * 40,
        mode=RetrievalMode.INJECT, reason_code="repair_session_not_reached", path=db_path,
    )

    response = client.get("/api/repair-memory/retrieval-audits?page=1&page_size=15")

    assert response.status_code == 200
    payload = response.json()
    assert payload["audits"][0]["task_id"] == "task-dashboard"
    assert payload["audits"][0]["status"] == "not_attempted"
    assert payload["audits"][0]["candidate_scores"] == []
    assert payload["page"] == 1
    assert payload["page_size"] == 15
    assert payload["total"] == 1
    assert payload["total_pages"] == 1
    assert client.get("/api/repair-memory/retrieval-audits?page_size=999").status_code == 422


def test_dashboard_can_disable_and_restore_memory_idempotently(tmp_path, monkeypatch):
    client, db_path = _repair_memory_client(tmp_path, monkeypatch)
    assert save_memory(sample_memory("mem-dashboard"), db_path)

    disabled = client.post(
        "/api/repair-memory/memories/mem-dashboard/disable",
        json={"reason": "  人工确认该经验会误导修复  "},
    )
    assert disabled.status_code == 200
    assert disabled.json() == {"memory_id": "mem-dashboard", "status": "disabled", "changed": True}
    assert load_memory("mem-dashboard", db_path).status is MemoryStatus.DISABLED
    events = list_memory_events("mem-dashboard", db_path)
    assert len(events) == 1
    assert events[0].reason == "人工确认该经验会误导修复"
    assert events[0].metadata["source"] == "dashboard"
    assert events[0].metadata["previous_status"] == "active"
    assert events[0].metadata["new_status"] == "disabled"
    assert events[0].metadata["changed_at"] == events[0].created_at

    repeated = client.post(
        "/api/repair-memory/memories/mem-dashboard/disable",
        json={"reason": "重复点击"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["changed"] is False
    assert len(list_memory_events("mem-dashboard", db_path)) == 1

    restored = client.post(
        "/api/repair-memory/memories/mem-dashboard/enable",
        json={"reason": "复核后确认可以恢复"},
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert restored.json()["changed"] is True
    assert load_memory("mem-dashboard", db_path).status is MemoryStatus.ACTIVE
    assert len(list_memory_events("mem-dashboard", db_path)) == 2

    repeated_restore = client.post(
        "/api/repair-memory/memories/mem-dashboard/enable",
        json={"reason": "重复点击"},
    )
    assert repeated_restore.status_code == 200
    assert repeated_restore.json()["changed"] is False
    assert len(list_memory_events("mem-dashboard", db_path)) == 2


def test_dashboard_memory_status_validation_and_conflicts(tmp_path, monkeypatch):
    client, db_path = _repair_memory_client(tmp_path, monkeypatch)
    assert save_memory(
        sample_memory("needs-review", pattern_key="needs-review", status=MemoryStatus.NEEDS_REVIEW), db_path
    )
    assert save_memory(
        sample_memory(
            "needs-review-enable",
            pattern_key="needs-review-enable",
            status=MemoryStatus.NEEDS_REVIEW,
        ),
        db_path,
    )
    assert save_memory(
        sample_memory("superseded", pattern_key="superseded", status=MemoryStatus.SUPERSEDED), db_path
    )

    allowed = client.post(
        "/api/repair-memory/memories/needs-review/disable",
        json={"reason": "人工删除待复核经验"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "disabled"

    assert client.post(
        "/api/repair-memory/memories/superseded/disable", json={"reason": "非法转换"}
    ).status_code == 409
    assert client.post(
        "/api/repair-memory/memories/missing/disable", json={"reason": "不存在"}
    ).status_code == 404
    assert client.post(
        "/api/repair-memory/memories/needs-review-enable/enable", json={"reason": "状态不允许"}
    ).status_code == 409
    assert client.post(
        "/api/repair-memory/memories/needs-review/disable", json={"reason": "   "}
    ).status_code == 422
    assert client.post(
        "/api/repair-memory/memories/needs-review/disable", json={"reason": "x" * 501}
    ).status_code == 422
