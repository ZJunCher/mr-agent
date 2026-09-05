from pr_agent.servers.repair_results_page import render_repair_result_page


def _html() -> str:
    return render_repair_result_page("task-12345678", "safe-signature")


def test_run_log_is_open_and_bounded():
    html = _html()
    assert '<details id="runLog" class="run-log" open>' in html
    assert 'id="timelineScroll" class="timeline-scroll"' in html
    assert "max-height: 420px" in html
    assert "overflow-y: auto" in html
    assert "max-height: 50vh" in html


def test_side_by_side_diff_owns_each_pane_overflow():
    html = _html()
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" in html
    assert ".code-cell {" in html and "min-width: 0" in html
    assert ".line-content { min-width: 0" in html


def test_page_uses_selected_repair_outcome_instead_of_whole_pipeline_status():
    html = _html()

    assert "data.repair_outcome ||" in html
    assert "repairOutcome(data) === 'success'" in html
    assert "repairOutcome(data) === 'partial_success'" in html
    assert "部分修复成功" in html
    assert "overflow-x: auto" in html
    assert "minmax(440px" not in html


def test_page_renders_dependency_blocker_without_failure_or_fake_changes():
    html = _html()

    assert "const terminalBlocked" in html
    assert "外部依赖阻塞" in html
    assert "建议处理" in html
    assert "当前依赖" in html
    assert "已验证候选" in html
    assert "本次未修改当前仓库代码" in html
    assert "当前仓库不能安全补齐上游接口定义" in html
    assert "验证 Pipeline" in html
    assert "if (terminalBlocked(data))" in html


def test_page_prefers_final_diff_and_waits_for_report_settlement():
    html = _html()
    assert "if ((data.final_file_changes || []).length) return data.final_file_changes" in html
    assert "const isSettled" in html
    assert "model_generated" in html
    assert "正在根据最终代码生成修复说明" in html
    assert "if (isSettled(data)) stopLiveUpdates()" in html


def test_page_uses_gitlab_light_diff_tokens_and_mobile_unified_mode():
    html = _html()
    assert "--add: #ecf4ee" in html
    assert "--delete: #fbe9eb" in html
    assert '"Liberation Mono"' in html
    assert "#sideButton { display: none; }" in html
    assert "diffMode = 'unified'" in html


def test_page_labels_coverage_by_its_actual_source():
    html = _html()

    assert "changed_lines: '变更行覆盖率'" in html
    assert "gitlab_pipeline: 'Pipeline 覆盖率'" in html
    assert "coverageReasons[finalResult.coverage_status]" in html
    assert "'代码覆盖率'" not in html


def test_page_preserves_diff_and_timeline_scroll_positions():
    html = _html()
    assert "captureDiffState" in html
    assert "restoreDiffState" in html
    assert "const nearBottom" in html
    assert "scroller.scrollTop = nearBottom ? scroller.scrollHeight : previousTop" in html


def test_page_renders_safe_gitlab_job_log_links():
    html = _html()

    assert "function jobLogHref" in html
    assert "data.source_jobs" in html
    assert "url.hash = `L${traceLine}`" in html
    assert "· 定位第 ${traceLine} 行" in html
    assert "· 查看日志" in html
    assert "link.target = '_blank'" in html
    assert "link.rel = 'noopener noreferrer'" in html
    assert "job-log-link" in html
    assert "innerHTML" not in html
