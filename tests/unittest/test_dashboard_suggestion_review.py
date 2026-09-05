from datetime import datetime, timedelta
from unittest.mock import patch

from pr_agent.feedback.timez import now_cn
from pr_agent.servers.dashboard_routes import (
    _suggestion_filter_dashboard_html,
    collect_suggestion_review_summary,
    collect_suggestion_review_table,
)
from pr_agent.suggestions.review_reporting import (
    collect_creation_review_detail,
    derive_abnormal_reason,
    derive_primary_status,
    historical_evidence_run,
    include_in_operational_summary,
    select_creation_review_run,
)
from pr_agent.suggestions.review_tracking import (
    finish_review_run,
    mark_creation_recovery,
    mark_creation_tracking_started,
    start_review_run,
    update_review_alert_state,
    upsert_mr,
)
from pr_agent.suggestions.store import save_published_suggestion


def test_summary_uses_inventory_as_mr_denominator(tmp_path):
    path = str(tmp_path / "coverage.db")
    old = (now_cn() - timedelta(days=1)).isoformat()
    recent = (now_cn() - timedelta(minutes=2)).isoformat()
    for iid, created_at in (("1", old), ("2", old), ("3", recent)):
        assert upsert_mr({
            "project_id": "7", "project_path": "g/r", "mr_iid": iid,
            "mr_url": f"https://gl/g/r/-/merge_requests/{iid}", "commit_sha": f"sha-{iid}",
            "created_at": created_at, "updated_at": created_at, "discovered_by": "reconcile",
        }, path=path)
    run_id = start_review_run({
        "project_path": "g/r", "mr_iid": "1", "commit_sha": "sha-1", "trigger": "auto_mr_create",
    }, path=path)
    finish_review_run(
        "completed", run_id, path=path, generated_count=3, kept_count=2, filtered_count=1,
        inline_selected_count=2, inline_published_count=2, stage="published",
    )

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
    ):
        result = collect_suggestion_review_summary(days=30)

    assert result["inventory_total"] == 3
    assert result["triggered_total"] == 1
    assert result["published_mr_total"] == 1
    assert result["filtered_mr_total"] == 1
    statuses = {row["mr_iid"]: row["status"] for row in result["review_mr_rows"]}
    assert statuses == {"1": "published", "2": "not_triggered", "3": "waiting"}
    assert next(row for row in result["review_mr_rows"] if row["mr_iid"] == "1")["has_secondary_filter"] is True


def test_review_mr_table_uses_fixed_fifteen_row_pages(tmp_path):
    path = str(tmp_path / "coverage.db")
    created = (now_cn() - timedelta(hours=2)).isoformat()
    for index in range(32):
        assert upsert_mr({
            "project_id": "7", "project_path": "g/r", "mr_iid": str(index),
            "title": f"Review item {index:02d}", "author": "alice",
            "created_at": created, "updated_at": created, "discovered_by": "reconcile",
        }, path=path)

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
    ):
        pages = [
            collect_suggestion_review_table("review_mrs", page=page, days=None)
            for page in (1, 2, 3)
        ]
        clamped = collect_suggestion_review_table("review_mrs", page=99, days=None)
        searched = collect_suggestion_review_table(
            "review_mrs", page=1, days=None, query="item 31", status="not_triggered",
            attention_only=True,
        )

    assert [len(result["rows"]) for result in pages] == [15, 15, 2]
    assert all(result["page_size"] == 15 for result in pages)
    assert all(result["total_rows"] == 32 and result["total_pages"] == 3 for result in pages)
    assert len({row["mr"] for result in pages for row in result["rows"]}) == 32
    assert clamped["page"] == 3
    assert len(clamped["rows"]) == 2
    assert [row["mr_iid"] for row in searched["rows"]] == ["31"]


def test_public_summary_mode_omits_full_table_rows(tmp_path):
    path = str(tmp_path / "coverage.db")
    created = (now_cn() - timedelta(hours=2)).isoformat()
    upsert_mr({
        "project_id": "7", "project_path": "g/r", "mr_iid": "1",
        "created_at": created, "updated_at": created,
    }, path=path)

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
    ):
        result = collect_suggestion_review_summary(days=None, include_rows=False)

    assert result["inventory_total"] == 1
    assert result["project_options"] == ["g/r"]
    for key in ("review_mr_rows", "coverage_project_rows", "project_rows", "mr_rows", "filtered_rows"):
        assert result[key] == []


def test_summary_excludes_inventory_before_tracking_boundary(tmp_path):
    path = str(tmp_path / "coverage.db")
    for iid, created_at in (
        ("before", "2026-08-06T15:07:59+08:00"),
        ("at", "2026-08-06T15:08:00+08:00"),
        ("after", "2026-08-06T15:08:01+08:00"),
    ):
        assert upsert_mr({
            "project_id": "7", "project_path": "g/r", "mr_iid": iid,
            "created_at": created_at, "updated_at": created_at,
        }, path=path)

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch(
            "pr_agent.servers.dashboard_routes._suggestion_review_cutoff",
            return_value="2026-08-06T15:08:00+08:00",
        ),
    ):
        result = collect_suggestion_review_summary(days=None)

    assert result["inventory_total"] == 2
    assert {row["mr_iid"] for row in result["review_mr_rows"]} == {"at", "after"}


def test_summary_keeps_creation_result_after_head_sha_changes(tmp_path):
    path = str(tmp_path / "coverage.db")
    created = (now_cn() - timedelta(days=1)).isoformat()
    upsert_mr({
        "project_id": "7", "project_path": "g/r", "mr_iid": "1", "commit_sha": "new",
        "created_at": created, "updated_at": created,
    }, path=path)
    run_id = start_review_run({
        "project_path": "g/r", "mr_iid": "1", "commit_sha": "old", "trigger": "auto_mr_create",
    }, path=path)
    finish_review_run("completed", run_id, path=path, generated_count=0)

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
    ):
        result = collect_suggestion_review_summary(days=None)

    assert result["triggered_total"] == 1
    assert result["review_mr_rows"][0]["status"] == "no_suggestions"
    assert result["review_mr_rows"][0]["commit_sha"] == "old"


def test_status_result_precedence():
    base = {"status": "completed", "stage": "validated"}
    assert derive_primary_status({}, {**base, "generated_count": 0}, grace_seconds=900,
                                 run_timeout_seconds=7200) == "no_suggestions"
    assert derive_primary_status({}, {**base, "generated_count": 2}, grace_seconds=900,
                                 run_timeout_seconds=7200) == "unpublished"
    assert derive_primary_status({}, {**base, "inline_published_count": 1}, grace_seconds=900,
                                 run_timeout_seconds=7200) == "published"
    assert derive_primary_status({}, {**base, "inline_fallback_count": 1}, grace_seconds=900,
                                 run_timeout_seconds=7200) == "fallback_published"
    assert derive_primary_status(
        {}, {**base, "inline_fallback_count": 1, "inline_failed_count": 1},
        grace_seconds=900, run_timeout_seconds=7200,
    ) == "publish_failed"
    assert derive_primary_status({}, {**base, "status": "failed"}, grace_seconds=900,
                                 run_timeout_seconds=7200) == "startup_failed"
    assert derive_primary_status(
        {}, {**base, "status": "failed", "improve_started_at": "2026-08-08T12:00:00+08:00"},
        grace_seconds=900, run_timeout_seconds=7200,
    ) == "execution_failed"
    assert derive_primary_status(
        {}, {**base, "status": "failed", "inline_failed_count": 1},
        grace_seconds=900, run_timeout_seconds=7200,
    ) == "publish_failed"
    assert derive_primary_status(
        {}, {**base, "status": "skipped", "error_code": "ignored_label"},
        grace_seconds=900, run_timeout_seconds=7200,
    ) == "skipped"


def test_summary_reports_policy_skip_without_attention(tmp_path):
    path = str(tmp_path / "coverage.db")
    created = (now_cn() - timedelta(hours=1)).isoformat()
    upsert_mr({
        "project_id": "7", "project_path": "g/r", "mr_iid": "skip",
        "created_at": created, "updated_at": created,
    }, path=path)
    run_id = start_review_run({
        "project_path": "g/r", "mr_iid": "skip", "trigger": "auto_mr_create",
        "review_scope": "mr_creation",
    }, path=path)
    finish_review_run(
        "skipped", run_id, path=path, stage="skipped",
        error_code="ignored_label", error_message="Ignored by label rule: no-ai",
    )

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
    ):
        result = collect_suggestion_review_summary(days=None)

    assert result["status_counts"] == {"skipped": 1}
    assert result["attention_total"] == 0
    assert result["review_mr_rows"][0]["reason_code"] == "ignored_label"
    assert result["review_mr_rows"][0]["reason_label"] == "标签规则跳过"


def test_summary_counts_fallback_delivery_as_success_without_attention(tmp_path):
    path = str(tmp_path / "coverage.db")
    created = (now_cn() - timedelta(hours=1)).isoformat()
    upsert_mr({
        "project_id": "7", "project_path": "g/r", "mr_iid": "fallback",
        "created_at": created, "updated_at": created,
    }, path=path)
    run_id = start_review_run({
        "project_path": "g/r", "mr_iid": "fallback", "trigger": "auto_mr_create",
    }, path=path)
    finish_review_run(
        "completed", run_id, path=path, generated_count=1,
        inline_fallback_count=1, stage="published",
    )

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
    ):
        result = collect_suggestion_review_summary(days=None)

    assert result["status_counts"] == {"fallback_published": 1}
    assert result["published_mr_total"] == 1
    assert result["attention_total"] == 0
    assert result["review_mr_rows"][0]["inline_fallback_count"] == 1


def test_historical_creation_runs_and_evidence_are_recognized():
    mr = {"created_at": "2026-08-07T12:00:00+08:00"}
    boundary = "2026-08-08T12:00:00+08:00"
    calibrated = {"trigger": "historical_auto_mr_create", "started_at": "2026-08-07T12:01:00+08:00"}
    assert select_creation_review_run(mr, [calibrated], boundary) == calibrated
    assert historical_evidence_run(mr, {"published": 1}, boundary)["inline_published_count"] == 1
    assert historical_evidence_run(
        {"created_at": "2026-08-08T12:01:00+08:00"}, {"published": 1}, boundary,
    ) is None


def test_operational_summary_hides_only_unreliable_not_triggered():
    boundary = "2026-08-08T12:00:00+08:00"
    historical = {"created_at": "2026-08-07T12:00:00+08:00"}
    reliable = {"created_at": "2026-08-09T12:00:00+08:00"}

    assert include_in_operational_summary(historical, "not_triggered", boundary) is False
    assert include_in_operational_summary(historical, "published", boundary) is True
    assert include_in_operational_summary(reliable, "not_triggered", boundary) is True
    assert include_in_operational_summary(historical, "not_triggered", None) is True
    assert include_in_operational_summary({}, "not_triggered", boundary) is True


def test_summary_excludes_historical_not_triggered_from_all_aggregates(tmp_path):
    path = str(tmp_path / "coverage.db")
    for iid, created_at in (
        ("historical-empty", "2026-08-07T12:00:00+08:00"),
        ("historical-published", "2026-08-07T12:00:00+08:00"),
        ("reliable-empty", "2026-08-09T12:00:00+08:00"),
    ):
        upsert_mr({
            "project_id": "7",
            "project_path": "g/r",
            "mr_iid": iid,
            "commit_sha": f"sha-{iid}",
            "created_at": created_at,
            "updated_at": created_at,
        }, path=path)
    save_published_suggestion({
        "project": "g/r",
        "mr_iid": "historical-published",
        "commit_sha": "sha-historical-published",
    }, path=path)

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
        patch(
            "pr_agent.servers.dashboard_routes.get_creation_tracking_boundary",
            return_value="2026-08-08T12:00:00+08:00",
        ),
    ):
        result = collect_suggestion_review_summary(days=None)

    assert result["inventory_total"] == 2
    assert result["status_counts"]["published"] == 1
    assert result["status_counts"]["not_triggered"] == 1
    assert {row["mr_iid"] for row in result["review_mr_rows"]} == {
        "historical-published",
        "reliable-empty",
    }
    assert sum(project["total"] for project in result["coverage_project_rows"]) == 2


def test_abnormal_reason_is_short_and_stable():
    reason = derive_abnormal_reason(
        {}, {"error_message": "worker lost and retry limit exceeded"}, "startup_failed",
    )
    assert reason == {"code": "worker_lost", "label": "Worker 丢失", "detail": ""}
    historical = derive_abnormal_reason(
        {"created_at": "2026-08-07T12:00:00+08:00"},
        {"trigger": "historical_auto_mr_create", "error_code": "HistoricalOutputMissing"},
        "startup_failed", reliable_started_at="2026-08-08T12:00:00+08:00",
    )
    assert historical is None


def test_abnormal_reason_does_not_guess_historical_webhook_configuration():
    reason = derive_abnormal_reason(
        {"created_at": "2026-08-07T12:01:00+08:00", "discovered_by": "incremental_sync"},
        None, "not_triggered",
        reliable_started_at="2026-08-08T12:00:00+08:00",
    )
    assert reason is None


def test_abnormal_reason_keeps_reliable_stored_recovery_reason():
    reason = derive_abnormal_reason(
        {
            "created_at": "2026-08-08T12:01:00+08:00",
            "creation_reason_code": "recovery_window_expired",
        },
        None, "not_triggered", reliable_started_at="2026-08-08T12:00:00+08:00",
    )
    assert reason == {
        "code": "recovery_window_expired", "label": "发现时已超过补审期限", "detail": "",
    }


def test_unexecuted_mr_detail_keeps_short_reason(tmp_path):
    path = str(tmp_path / "coverage.db")
    created = (now_cn() - timedelta(hours=2)).isoformat()
    upsert_mr({
        "project_id": "7", "project_path": "g/r", "mr_iid": "18",
        "created_at": created, "updated_at": created, "discovered_by": "incremental_sync",
    }, path=path)
    mark_creation_recovery(
        "g/r", "18", "outside_window", "recovery_window_expired", path=path,
    )

    detail = collect_creation_review_detail("g/r", "18", path=path)

    assert detail["detail_state"] == "available_empty"
    assert detail["mr"]["status"] == "not_triggered"
    assert detail["mr"]["reason_label"] == "发现时已超过补审期限"


def test_summary_counts_any_filtered_current_run_once_per_mr(tmp_path):
    path = str(tmp_path / "coverage.db")
    created = (now_cn() - timedelta(hours=1)).isoformat()
    for iid in ("full", "partial"):
        upsert_mr({
            "project_id": "7", "project_path": "g/r", "mr_iid": iid,
            "commit_sha": f"sha-{iid}", "created_at": created, "updated_at": created,
        }, path=path)
        run_id = start_review_run({
            "project_path": "g/r", "mr_iid": iid, "commit_sha": f"sha-{iid}",
            "trigger": "auto_mr_create",
        }, path=path)
        finish_review_run(
            "completed", run_id, path=path, generated_count=2,
            kept_count=0 if iid == "full" else 1, filtered_count=2 if iid == "full" else 1,
            inline_published_count=0 if iid == "full" else 1,
        )

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
    ):
        result = collect_suggestion_review_summary(days=None)

    assert result["filtered_mr_total"] == 2
    assert result["published_mr_total"] == 1
    assert {row["mr_iid"] for row in result["review_mr_rows"] if row["has_secondary_filter"]} == {
        "full", "partial",
    }


def test_summary_does_not_fall_back_to_historical_suggestion_rows(tmp_path):
    path = str(tmp_path / "coverage.db")
    save_published_suggestion({
        "project": "g/history", "mr_iid": "4", "mr_url": "https://gl/g/history/-/merge_requests/4",
    }, path=path)
    with patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path):
        result = collect_suggestion_review_summary(days=None)

    assert result["inventory_total"] == 0
    assert result["review_mr_rows"] == []


def test_reliable_window_excludes_manual_improve_runs(tmp_path):
    path = str(tmp_path / "coverage.db")
    mark_creation_tracking_started(path=path)
    created = (now_cn() + timedelta(seconds=1)).isoformat()
    upsert_mr({
        "project_id": "7", "project_path": "g/r", "mr_iid": "9",
        "created_at": created, "updated_at": created,
    }, path=path)
    run_id = start_review_run({
        "project_path": "g/r", "mr_iid": "9", "trigger": "manual_improve", "task_id": "manual-9",
    }, path=path)
    finish_review_run("completed", run_id, path=path, generated_count=1, inline_published_count=1)

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
    ):
        result = collect_suggestion_review_summary(days=None)

    assert result["triggered_total"] == 0
    assert result["review_mr_rows"][0]["status"] == "waiting"


def test_historical_inventory_uses_existing_suggestion_evidence(tmp_path):
    path = str(tmp_path / "coverage.db")
    created = (now_cn() - timedelta(days=1)).isoformat()
    upsert_mr({
        "project_id": "7", "project_path": "g/history", "mr_iid": "4", "commit_sha": "same",
        "created_at": created, "updated_at": created,
    }, path=path)
    save_published_suggestion({
        "project": "g/history", "mr_iid": "4", "commit_sha": "same",
        "mr_url": "https://gl/g/history/-/merge_requests/4",
    }, path=path)
    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
    ):
        result = collect_suggestion_review_summary(days=None)

    assert result["inventory_total"] == 1
    assert result["triggered_total"] == 1
    assert result["published_mr_total"] == 1
    assert result["review_mr_rows"][0]["status"] == "published"


def test_existing_dashboard_url_fetches_new_api_and_keeps_filter_detail():
    html = _suggestion_filter_dashboard_html()
    assert "/api/suggestion-review/summary?days=" in html
    assert "/api/suggestion-review/table/" in html
    assert "page_size" in html
    assert "const PAGE_SIZE = 15" in html
    assert "pageSize: 20" not in html
    assert "pageSize: 10" not in html
    assert "loadDashboard(30)" in html
    assert "row.reason_label" in html
    assert 'id="detailReason"' in html
    assert "降级发布成功" in html
    assert 'id="aggregateAlerts"' in html
    assert html.index('id="aggregateAlerts"') < html.index('id="overviewGrid"')
    assert "container.hidden = alerts.length === 0" in html
    assert "同步补审" in html
    assert "状态 · 简短原因" in html
    for element_id in (
        "overviewGrid", "statusOverview", "mrWorkspace", "mrSearch", "daysFilter",
        "attentionFilter", "filterQualitySection", "filteredDetails",
    ):
        assert f'id="{element_id}"' in html
    assert 'id="mAttention"' not in html
    assert "MR review coverage" not in html
    assert "未执行建议审查" in html
    assert "等待自动审查" in html
    assert "启动失败" in html
    assert "执行失败" in html
    assert "已执行，无可用建议" in html
    assert "有建议，未发布" in html
    assert "发布失败" in html
    assert "已发布" in html
    assert "二次审查过滤" in html
    assert "secondary_filtered" in html
    assert "全部过滤" not in html
    assert "部分过滤" not in html
    assert "审查已过期" not in html
    assert "已完成" not in html
    assert "当前 commit" not in html
    assert "projectFilter" in html
    assert "statusFilter" in html


def test_summary_exposes_persisted_aggregate_alerts(tmp_path):
    path = str(tmp_path / "coverage.db")
    triggered_at = datetime.fromisoformat("2026-08-18T10:10:00+08:00")
    with patch("pr_agent.suggestions.review_tracking.now_cn", return_value=triggered_at):
        transition = update_review_alert_state(
            "model_failures", active=True, count=3, cooldown_seconds=3600, path=path,
        )
    assert transition.should_emit

    with (
        patch("pr_agent.servers.dashboard_routes.get_inline_db_path", return_value=path),
        patch("pr_agent.servers.dashboard_routes._suggestion_review_cutoff", return_value=None),
    ):
        payload = collect_suggestion_review_summary(days=None)

    assert payload["alerts"] == [{
        "key": "model_failures",
        "label": "模型调用连续失败",
        "count": 3,
        "threshold": 3,
        "window_seconds": 1800,
        "first_triggered_at": "2026-08-18T10:10:00+08:00",
    }]


def test_dashboard_contains_accessible_lazy_detail_dialog():
    html = _suggestion_filter_dashboard_html()
    for value in (
        'id="reviewDetailDialog"', 'aria-modal="true"', 'role="dialog"',
        'id="detailTimelineTab"', 'id="detailFilteredTab"',
        'id="detailPublishedTab"', 'id="detailErrorsTab"',
        "/api/suggestion-review/detail?", "查看详情", "trapDialogFocus",
        "restoreDialogFocus", "Escape", "detailRetryButton",
    ):
        assert value in html
    assert "max-width: 1120px" in html
    assert "max-height: 88dvh" in html
    assert "prefers-reduced-motion" in html
    assert "min-height: 44px" in html
    assert "textContent" in html
    assert "escapeHtml" in html
