"""HTML renderer for the suggestion-review operations dashboard."""

from __future__ import annotations


def render_suggestion_review_dashboard(base_css: str, js_helpers: str, nav_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>建议审查覆盖率</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>{base_css}
:root {{
  --ops-bg: #0a0f1c; --ops-surface: #111827; --ops-surface-2: #151f2f;
  --ops-border: #263449; --ops-text: #f1f5f9; --ops-muted: #94a3b8;
  --ops-blue: #60a5fa; --ops-green: #34d399; --ops-cyan: #22d3ee;
  --ops-amber: #fbbf24; --ops-red: #fb7185; --ops-purple: #a78bfa;
}}
body {{ background: var(--ops-bg); color: var(--ops-text); }}
.skip-link {{ position: fixed; top: 8px; left: 8px; z-index: 20; padding: 10px 12px; border-radius: 8px; background: #dbeafe; color: #172554; transform: translateY(-140%); }}
.skip-link:focus {{ transform: translateY(0); }}
button, a, select, input, summary {{ touch-action: manipulation; }}
.container {{ max-width: 1480px; padding: 20px 24px 48px; }}
.nav-bar {{ display: flex; gap: 6px; padding: 0 0 14px; overflow-x: auto; }}
.nav-tab {{
  min-height: 44px; display: inline-flex; align-items: center; flex: 0 0 auto; padding: 8px 13px;
  border-radius: 8px; font-size: 13px; font-weight: 600;
  color: var(--ops-muted); border: 1px solid transparent; background: transparent;
  transition: color .16s ease, background .16s ease, border-color .16s ease;
}}
.nav-tab:hover {{ color: var(--ops-text); background: #111827; }}
.nav-tab.active {{ color: #dbeafe; background: #172554; border-color: #1e40af; }}
.ops-header {{
  display: flex; align-items: center; justify-content: space-between; gap: 20px;
  padding: 18px 20px; border: 1px solid var(--ops-border); border-radius: 14px; background: var(--ops-surface);
}}
.ops-header h1 {{ margin: 0; font-size: clamp(22px, 2vw, 28px); letter-spacing: -.025em; }}
.ops-header p {{ margin: 6px 0 0; color: var(--ops-muted); font-size: 14px; }}
.sync-panel {{ display: flex; align-items: center; gap: 12px; flex: 0 0 auto; }}
.sync-copy {{ text-align: right; font-size: 12px; color: var(--ops-muted); line-height: 1.45; }}
.sync-copy strong {{ color: var(--ops-text); font-size: 13px; }}
.sync-dot {{ width: 9px; height: 9px; border-radius: 50%; background: var(--ops-green); box-shadow: 0 0 0 4px rgba(52,211,153,.12); }}
.sync-dot.error {{ background: var(--ops-red); box-shadow: 0 0 0 4px rgba(251,113,133,.12); }}
.icon-btn, .toolbar-btn {{
  min-height: 44px; border: 1px solid var(--ops-border); background: var(--ops-surface-2);
  color: var(--ops-text); border-radius: 9px; cursor: pointer; font-weight: 600;
  transition: background .16s ease, border-color .16s ease, transform .16s ease;
}}
.icon-btn {{ width: 44px; display: grid; place-items: center; }}
.icon-btn svg {{ width: 18px; height: 18px; }}
.toolbar-btn {{ padding: 0 14px; }}
.icon-btn:hover, .toolbar-btn:hover {{ background: #1e293b; border-color: #3b82f6; }}
.icon-btn:active, .toolbar-btn:active {{ transform: translateY(1px); }}
.overview-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }}
.kpi-card {{ padding: 16px 18px; border: 1px solid var(--ops-border); border-radius: 12px; background: var(--ops-surface); }}
.kpi-top {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
.kpi-label {{ color: var(--ops-muted); font-size: 13px; font-weight: 600; }}
.kpi-index {{ font: 600 11px ui-monospace, SFMono-Regular, Menlo, monospace; color: #64748b; }}
.kpi-row {{ display: flex; align-items: baseline; gap: 9px; margin-top: 8px; }}
.kpi-value {{ font: 700 30px/1 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: -.04em; }}
.kpi-rate {{ color: var(--ops-blue); font: 600 13px ui-monospace, SFMono-Regular, Menlo, monospace; }}
.kpi-note {{ margin-top: 9px; color: #64748b; font-size: 12px; }}
.ops-section {{ margin-top: 14px; padding: 18px; border: 1px solid var(--ops-border); border-radius: 12px; background: var(--ops-surface); }}
.section-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }}
.section-title {{ margin: 0; font-size: 17px; }}
.section-subtitle {{ margin: 5px 0 0; color: var(--ops-muted); font-size: 13px; }}
.attention-count {{ color: var(--ops-amber); font: 700 14px ui-monospace, SFMono-Regular, Menlo, monospace; }}
.status-bar {{ display: flex; height: 12px; margin-top: 16px; overflow: hidden; border-radius: 999px; background: #1e293b; }}
.status-segment {{ min-width: 2px; height: 100%; }}
.status-chip-list {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }}
.status-chip {{
  min-height: 44px; display: inline-flex; align-items: center; gap: 7px; padding: 0 11px;
  border: 1px solid var(--ops-border); border-radius: 999px; background: #0f172a;
  color: var(--ops-muted); cursor: pointer; font-size: 12px;
}}
.status-chip:hover, .status-chip[aria-pressed="true"] {{ color: var(--ops-text); border-color: currentColor; background: #172033; }}
.status-swatch {{ width: 8px; height: 8px; border-radius: 50%; background: var(--status-color); }}
.workspace-head {{ align-items: center; }}
.result-count {{ color: var(--ops-muted); font-size: 13px; white-space: nowrap; }}
.toolbar {{ display: grid; grid-template-columns: minmax(220px, 1.5fr) repeat(3, minmax(150px, .7fr)) auto auto; gap: 10px; margin-top: 16px; }}
.field {{ position: relative; }}
.field label {{ display: block; margin: 0 0 6px 2px; color: var(--ops-muted); font-size: 11px; font-weight: 600; }}
.field input, .field select {{
  width: 100%; min-height: 44px; padding: 0 12px; border: 1px solid var(--ops-border);
  border-radius: 9px; background: #0d1524; color: var(--ops-text); font-size: 14px;
}}
.field input::placeholder {{ color: #64748b; }}
.attention-toggle {{
  min-height: 44px; display: inline-flex; align-items: center; gap: 8px; padding: 0 12px;
  border: 1px solid var(--ops-border); border-radius: 9px; background: #0d1524;
  color: var(--ops-muted); cursor: pointer; white-space: nowrap; font-size: 13px;
}}
.attention-toggle:has(input:checked) {{ color: #fef3c7; border-color: #b45309; background: #2b1d0b; }}
.attention-toggle input {{ width: 16px; height: 16px; accent-color: var(--ops-amber); }}
.attention-toggle, .toolbar > .toolbar-btn {{ margin-top: 20px; }}
.table-wrap {{ margin-top: 14px; border: 1px solid var(--ops-border); border-radius: 10px; overflow: auto; background: #0d1524; }}
.table-wrap table {{ min-width: 920px; margin: 0; }}
th, td {{ padding: 10px 12px; border-color: #1f2b3d; font-size: 13px; vertical-align: middle; }}
th {{ position: sticky; top: 0; z-index: 2; background: #131d2c; color: #9fb0c6; font-size: 11px; letter-spacing: .04em; text-transform: uppercase; }}
tbody tr {{ transition: background .14s ease; }}
tbody tr:hover {{ background: rgba(59,130,246,.06); }}
.mr-cell {{ min-width: 205px; }}
.mr-project {{ max-width: 260px; overflow: hidden; text-overflow: ellipsis; color: #cbd5e1; }}
.mr-iid {{ margin-top: 3px; color: var(--ops-blue); font: 600 12px ui-monospace, SFMono-Regular, Menlo, monospace; }}
.title-cell {{ min-width: 300px; max-width: 480px; }}
.mr-title {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ops-text); }}
.mr-meta {{ margin-top: 4px; color: #718198; font-size: 12px; }}
.count-group {{ white-space: nowrap; font: 600 12px ui-monospace, SFMono-Regular, Menlo, monospace; }}
.count-group span + span::before {{ content: "/"; margin: 0 6px; color: #475569; }}
.status-badge {{
  display: inline-flex; align-items: center; gap: 6px; min-height: 26px; padding: 0 9px;
  border-radius: 999px; border: 1px solid color-mix(in srgb, var(--status-color) 45%, transparent);
  background: color-mix(in srgb, var(--status-color) 10%, transparent); color: var(--status-color);
  white-space: nowrap; font-size: 12px; font-weight: 700;
}}
.status-badge::before {{ content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }}
.status-stack {{ display: flex; flex-wrap: wrap; gap: 5px; }}
.status-reason {{ width: 100%; margin-top: 2px; color: #cbd5e1; font-size: 12px; line-height: 1.35; }}
.detail-reason {{ margin-top: 10px; padding: 10px 12px; border: 1px solid #7c2d12; border-radius: 8px; background: #2b160d; color: #fed7aa; font-size: 13px; }}
.open-link {{ color: #93c5fd; font-weight: 600; white-space: nowrap; }}
.open-link:hover {{ text-decoration: underline; }}
.pager {{ margin-top: 12px; }}
.pager .btn {{ min-height: 44px; border-radius: 8px; }}
.quality-section {{ margin-top: 28px; padding-top: 24px; border-top: 1px solid var(--ops-border); }}
.quality-heading {{ display: flex; align-items: end; justify-content: space-between; gap: 16px; margin-bottom: 12px; }}
.quality-heading h2 {{ margin: 0; font-size: 20px; }}
.quality-heading p {{ margin: 5px 0 0; color: var(--ops-muted); font-size: 13px; }}
.quality-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
.quality-card {{ padding: 13px 15px; border: 1px solid var(--ops-border); border-radius: 10px; background: var(--ops-surface); }}
.quality-card .metric-value {{ margin-top: 6px; font-size: 24px; }}
.charts-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }}
.chart-panel {{ min-width: 0; padding: 16px; border: 1px solid var(--ops-border); border-radius: 10px; background: var(--ops-surface); }}
.chart-wrap {{ height: 260px; margin-top: 12px; }}
.analysis-tabs {{ display: flex; gap: 6px; margin-top: 14px; }}
.tab-btn {{ min-height: 44px; padding: 0 13px; border: 1px solid var(--ops-border); border-radius: 8px; background: transparent; color: var(--ops-muted); cursor: pointer; }}
.tab-btn.active {{ color: #dbeafe; border-color: #1e40af; background: #172554; }}
.analysis-panel[hidden] {{ display: none; }}
.detail-disclosure {{ margin-top: 12px; border: 1px solid var(--ops-border); border-radius: 10px; background: var(--ops-surface); }}
.detail-disclosure > summary {{ min-height: 52px; display: flex; align-items: center; justify-content: space-between; padding: 0 16px; cursor: pointer; font-weight: 700; }}
.detail-disclosure > summary::after {{ content: "展开"; color: var(--ops-muted); font-size: 12px; font-weight: 500; }}
.detail-disclosure[open] > summary::after {{ content: "收起"; }}
.detail-body {{ padding: 0 16px 16px; }}
.suggestion-content {{ white-space: pre-wrap; word-break: break-word; color: #cbd5e1; line-height: 1.55; font-size: 12px; }}
.empty-state {{ padding: 26px; text-align: center; color: var(--ops-muted); }}
.review-row {{ cursor: pointer; }}
.review-row:focus-visible {{ outline: 3px solid rgba(96,165,250,.55); outline-offset: -3px; }}
.row-actions {{ display: flex; align-items: center; gap: 10px; white-space: nowrap; }}
.detail-action {{
  min-height: 44px; padding: 0 12px; border: 1px solid #31527d; border-radius: 8px;
  background: #13243a; color: #bfdbfe; cursor: pointer; font-weight: 700;
}}
.detail-dialog {{ position: fixed; inset: 0; z-index: 100; display: grid; place-items: center; }}
.detail-dialog[hidden] {{ display: none; }}
.detail-scrim {{ position: absolute; inset: 0; background: rgba(2,6,23,.55); opacity: 1; }}
.detail-panel {{
  position: relative; width: calc(100vw - 32px); max-width: 1120px; max-height: 88dvh;
  display: flex; flex-direction: column; overflow: hidden; border: 1px solid #334155;
  border-radius: 14px; background: #0b1220; box-shadow: 0 28px 80px rgba(0,0,0,.55);
  transform: translateY(0); transition: opacity .2s ease, transform .2s ease;
}}
.detail-panel:focus {{ outline: none; }}
.detail-header {{ flex: 0 0 auto; padding: 18px 20px 14px; border-bottom: 1px solid var(--ops-border); background: #101827; }}
.detail-title-row {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }}
.detail-title-row h2 {{ margin: 0; font-size: 20px; }}
.detail-subtitle {{ margin-top: 5px; color: var(--ops-muted); font-size: 13px; overflow-wrap: anywhere; }}
.detail-close {{
  flex: 0 0 auto; width: 44px; min-height: 44px; border: 1px solid var(--ops-border); border-radius: 9px;
  background: #172033; color: var(--ops-text); cursor: pointer; font-size: 20px;
}}
.detail-summary {{ display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 14px; color: #a9b8cb; font-size: 12px; }}
.detail-counts {{ display: flex; flex-wrap: wrap; gap: 7px; margin-top: 12px; }}
.detail-count {{ padding: 5px 8px; border: 1px solid var(--ops-border); border-radius: 7px; background: #0d1524; color: #cbd5e1; font: 600 12px ui-monospace, SFMono-Regular, Menlo, monospace; }}
.detail-tabs {{ flex: 0 0 auto; display: flex; gap: 4px; padding: 8px 14px; overflow-x: auto; scrollbar-width: none; border-bottom: 1px solid var(--ops-border); background: #0e1726; }}
.detail-tabs::-webkit-scrollbar {{ display: none; }}
.detail-tab {{ min-height: 44px; padding: 0 13px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: var(--ops-muted); cursor: pointer; white-space: nowrap; font-weight: 700; }}
.detail-tab[aria-selected="true"] {{ color: #dbeafe; border-color: #31527d; background: #172554; }}
.detail-scroll-region {{ min-height: 220px; overflow-y: auto; overscroll-behavior: contain; padding: 18px 20px 24px; }}
.detail-list {{ display: grid; gap: 12px; }}
.timeline-item, .suggestion-card, .error-card {{ padding: 14px 16px; border: 1px solid var(--ops-border); border-radius: 10px; background: #0f1827; }}
.timeline-item {{ display: grid; grid-template-columns: 170px minmax(0,1fr); gap: 14px; }}
.timeline-time, .suggestion-meta {{ color: var(--ops-muted); font: 500 12px ui-monospace, SFMono-Regular, Menlo, monospace; }}
.suggestion-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
.suggestion-path {{ color: #bfdbfe; font: 650 13px ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }}
.suggestion-copy, .suggestion-code {{ max-width: 75ch; overflow-wrap: anywhere; white-space: pre-wrap; line-height: 1.6; }}
.suggestion-copy {{ margin-top: 12px; color: #d5deea; }}
.suggestion-card details {{ margin-top: 12px; }}
.suggestion-card summary {{ min-height: 44px; display: flex; align-items: center; color: #93c5fd; cursor: pointer; }}
.suggestion-code {{ margin: 0; padding: 12px; overflow-x: auto; border-radius: 8px; background: #07101d; color: #cbd5e1; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }}
.disposition-row {{ margin-top: 12px; color: #a9b8cb; font-size: 13px; }}
.detail-retry {{ min-height: 44px; margin-top: 12px; padding: 0 14px; border: 1px solid #31527d; border-radius: 8px; background: #172554; color: #dbeafe; cursor: pointer; }}
.aggregate-alerts {{ display: grid; gap: 8px; margin-top: 14px; }}
.aggregate-alert {{ display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 12px 16px; border: 1px solid #92400e; border-radius: 10px; background: #2a170b; color: #fde68a; }}
.aggregate-alert strong {{ color: #fef3c7; }}
.aggregate-alert span {{ color: #fbbf24; font: 600 12px ui-monospace, SFMono-Regular, Menlo, monospace; }}
.detail-skeleton {{ height: 74px; border-radius: 10px; background: linear-gradient(90deg,#111c2c,#1b2a3f,#111c2c); background-size: 200% 100%; animation: detail-pulse 1.2s ease-in-out infinite; }}
@keyframes detail-pulse {{ to {{ background-position: -200% 0; }} }}
button:focus-visible, input:focus-visible, select:focus-visible, a:focus-visible, summary:focus-visible {{ outline: 3px solid rgba(96,165,250,.55); outline-offset: 2px; }}
@media (max-width: 1050px) {{
  .overview-grid, .quality-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .toolbar {{ grid-template-columns: 1fr 1fr 1fr; }}
  .toolbar .search-field {{ grid-column: span 2; }}
}}
@media (max-width: 720px) {{
  .container {{ padding: 14px 12px 32px; }}
  .ops-header {{ align-items: flex-start; padding: 16px; }}
  .sync-copy {{ display: none; }}
  .overview-grid {{ gap: 8px; }}
  .kpi-card {{ padding: 14px; }}
  .kpi-value {{ font-size: 25px; }}
  .ops-section {{ padding: 14px; }}
  .toolbar {{ grid-template-columns: 1fr 1fr; }}
  .toolbar .search-field {{ grid-column: 1 / -1; }}
  .field input, .field select {{ font-size: 16px; }}
  .charts-grid {{ grid-template-columns: minmax(0, 1fr); }}
  .quality-heading {{ align-items: flex-start; flex-direction: column; }}
  .detail-panel {{ width: 100vw; max-width: none; height: 100dvh; max-height: 100dvh; border-radius: 0; }}
  .detail-header {{ padding: 14px 14px 12px; }}
  .detail-scroll-region {{ padding: 14px; }}
  .timeline-item {{ grid-template-columns: 1fr; gap: 6px; }}
}}
@media (max-width: 430px) {{
  .overview-grid, .quality-grid {{ grid-template-columns: 1fr 1fr; }}
  .kpi-note {{ display: none; }}
  .toolbar {{ grid-template-columns: 1fr; }}
  .toolbar .search-field {{ grid-column: auto; }}
  .attention-toggle, .toolbar-btn {{ width: 100%; justify-content: center; }}
}}
@media (prefers-reduced-motion: reduce) {{
  * {{ scroll-behavior: auto !important; transition: none !important; }}
  .detail-skeleton {{ animation: none; }}
}}
</style>
</head>
<body>
<a class="skip-link" href="#opsMain">跳到主要内容</a>
<div class="container">
{nav_html}
<main id="opsMain">
  <header class="ops-header">
    <div>
      <h1>建议审查覆盖率</h1>
      <p>保留 2026-08-06 15:08 起的历史记录；可靠窗口只统计创建 MR 时自动执行的建议审查。</p>
    </div>
    <div class="sync-panel">
      <span id="syncDot" class="sync-dot" aria-hidden="true"></span>
      <div class="sync-copy" aria-live="polite"><strong id="syncState">正在加载</strong><br /><span id="syncTime">—</span></div>
      <button id="refreshButton" class="icon-btn" type="button" aria-label="刷新看板" title="刷新看板">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.34 5.66"/><path d="M20 4v7h-7"/></svg>
      </button>
    </div>
  </header>

  <section id="aggregateAlerts" class="aggregate-alerts" aria-label="建议审查聚合告警" hidden></section>

  <section id="overviewGrid" class="overview-grid" aria-label="核心指标">
    <article class="kpi-card"><div class="kpi-top"><span class="kpi-label">MR 总数</span><span class="kpi-index">01</span></div><div class="kpi-row"><span id="mInventory" class="kpi-value">—</span></div><div class="kpi-note">GitLab token 可见范围</div></article>
    <article class="kpi-card"><div class="kpi-top"><span class="kpi-label">创建审查有记录</span><span class="kpi-index">02</span></div><div class="kpi-row"><span id="mTriggered" class="kpi-value">—</span><span id="mTriggeredRate" class="kpi-rate">—</span></div><div class="kpi-note">创建 MR 时产生自动工作流记录</div></article>
    <article class="kpi-card"><div class="kpi-top"><span class="kpi-label">已得到明确结果</span><span class="kpi-index">03</span></div><div class="kpi-row"><span id="mCompleted" class="kpi-value">—</span><span id="mCompletedRate" class="kpi-rate">—</span></div><div class="kpi-note">执行成功或已有明确失败原因</div></article>
    <article class="kpi-card"><div class="kpi-top"><span class="kpi-label">已发布建议</span><span class="kpi-index">04</span></div><div class="kpi-row"><span id="mPublishedMR" class="kpi-value">—</span><span id="mPublishedRate" class="kpi-rate">—</span></div><div class="kpi-note">至少成功发布一条建议的 MR</div></article>
  </section>

  <section id="statusOverview" class="ops-section" aria-labelledby="statusTitle">
    <div class="section-head">
      <div><h2 id="statusTitle" class="section-title">状态与结果</h2><p class="section-subtitle">二次审查过滤与已发布可以重叠；点击任一项可筛选 MR。</p></div>
      <div><span class="muted">需关注 </span><span id="attentionCount" class="attention-count">—</span></div>
    </div>
    <div id="statusBar" class="status-bar" role="img" aria-label="MR 状态分布"></div>
    <div id="statusChips" class="status-chip-list" aria-label="按状态筛选"></div>
  </section>

  <section id="mrWorkspace" class="ops-section" aria-labelledby="workspaceTitle">
    <div class="section-head workspace-head">
      <div><h2 id="workspaceTitle" class="section-title">MR 工作台</h2><p class="section-subtitle">优先查看未执行、启动失败、执行失败或发布失败的创建审查。</p></div>
      <div id="resultCount" class="result-count" aria-live="polite">—</div>
    </div>
    <div class="toolbar" role="search">
      <div class="field search-field"><label for="mrSearch">搜索 MR</label><input id="mrSearch" type="search" placeholder="搜索项目、MR、标题或作者" /></div>
      <div class="field"><label for="daysFilter">时间范围</label><select id="daysFilter"><option value="7">最近 7 天</option><option value="30" selected>最近 30 天</option><option value="90">最近 90 天</option></select></div>
      <div class="field"><label for="projectFilter">项目</label><select id="projectFilter"><option value="">全部项目</option></select></div>
      <div class="field"><label for="statusFilter">状态</label><select id="statusFilter"><option value="">全部状态</option></select></div>
      <label id="attentionFilter" class="attention-toggle"><input id="attentionOnly" type="checkbox" />只看需关注</label>
      <button id="clearFilters" class="toolbar-btn" type="button">清空筛选</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>项目 / MR</th><th>标题 / 作者</th><th>状态 · 简短原因</th><th>生成 / 过滤 / 发布</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody id="reviewMrTableBody"><tr><td colspan="6" class="empty-state">正在加载…</td></tr></tbody>
      </table>
    </div>
    <div class="pager"><div id="reviewMrPageInfo" class="muted">—</div><div class="pager-actions"><button id="reviewMrPrevPage" class="btn" type="button">上一页</button><button id="reviewMrNextPage" class="btn" type="button">下一页</button></div></div>
  </section>

  <section id="filterQualitySection" class="quality-section" aria-labelledby="qualityTitle">
    <div class="quality-heading"><div><h2 id="qualityTitle">二次审查过滤质量</h2><p>展示过滤率、原因趋势和建议明细。</p></div><span class="muted">场景校验口径</span></div>
    <div class="quality-grid">
      <article class="quality-card"><div class="metric-label">已发布建议</div><div class="metric-value" id="mPub">—</div></article>
      <article class="quality-card"><div class="metric-label">二次审查过滤建议</div><div class="metric-value" id="mFiltered">—</div></article>
      <article class="quality-card"><div class="metric-label">过滤率</div><div class="metric-value" id="mRate">—</div></article>
      <article class="quality-card"><div class="metric-label">涉及 MR</div><div class="metric-value" id="mMR">—</div></article>
    </div>
    <div class="charts-grid">
      <article class="chart-panel"><h3 class="section-title">过滤原因</h3><p class="section-subtitle">各类二次过滤原因的建议数量。</p><div class="chart-wrap"><canvas id="reasonChart" role="img" aria-label="各类二次过滤原因的建议数量横向柱状图">图表加载后展示过滤原因数量。</canvas></div></article>
      <article class="chart-panel"><h3 class="section-title">每周过滤率</h3><p class="section-subtitle">过滤建议占已发布与已过滤建议总数的比例。</p><div class="chart-wrap"><canvas id="trendChart" role="img" aria-label="每周二次过滤率趋势折线图">图表加载后展示每周过滤率趋势。</canvas></div></article>
    </div>
    <div class="analysis-tabs" role="tablist" aria-label="过滤分析维度">
      <button id="projectAnalysisTab" class="tab-btn active" type="button" role="tab" aria-controls="projectAnalysisPanel" aria-selected="true">按项目</button>
      <button id="mrAnalysisTab" class="tab-btn" type="button" role="tab" aria-controls="mrAnalysisPanel" aria-selected="false">按 MR</button>
    </div>
    <div id="projectAnalysisPanel" class="analysis-panel ops-section">
      <div class="table-wrap"><table><thead><tr><th>项目</th><th>已发布</th><th>已过滤</th><th>过滤率</th><th>MR 数</th></tr></thead><tbody id="projectTableBody"></tbody></table></div>
      <div class="pager"><div id="projectPageInfo" class="muted">—</div><div class="pager-actions"><button id="projectPrevPage" class="btn" type="button">上一页</button><button id="projectNextPage" class="btn" type="button">下一页</button></div></div>
    </div>
    <div id="mrAnalysisPanel" class="analysis-panel ops-section" hidden>
      <div class="table-wrap"><table><thead><tr><th>MR</th><th>已发布</th><th>已过滤</th><th>过滤率</th><th>时间</th><th>作者</th><th>操作</th></tr></thead><tbody id="mrTableBody"></tbody></table></div>
      <div class="pager"><div id="mrPageInfo" class="muted">—</div><div class="pager-actions"><button id="mrPrevPage" class="btn" type="button">上一页</button><button id="mrNextPage" class="btn" type="button">下一页</button></div></div>
    </div>
    <details id="filteredDetails" class="detail-disclosure">
      <summary>被过滤建议明细 <span id="filteredDetailCount" class="muted"></span></summary>
      <div class="detail-body"><div class="table-wrap"><table><thead><tr><th>时间</th><th>项目 / MR</th><th>文件</th><th>标签</th><th>分数</th><th>原因</th><th>操作</th></tr></thead><tbody id="filteredTableBody"></tbody></table></div>
      <div class="pager"><div id="filteredPageInfo" class="muted">—</div><div class="pager-actions"><button id="filteredPrevPage" class="btn" type="button">上一页</button><button id="filteredNextPage" class="btn" type="button">下一页</button></div></div></div>
    </details>
  </section>
</main>
</div>

<div id="reviewDetailDialog" class="detail-dialog" role="dialog" aria-modal="true"
     aria-labelledby="detailDialogTitle" hidden>
  <div class="detail-scrim" data-close-dialog aria-hidden="true"></div>
  <section class="detail-panel" tabindex="-1">
    <header class="detail-header">
      <div class="detail-title-row">
        <div><h2 id="detailDialogTitle">创建审查详情</h2><div id="detailDialogSubtitle" class="detail-subtitle">—</div></div>
        <button id="detailCloseButton" class="detail-close" type="button" aria-label="关闭审查详情">×</button>
      </div>
      <div id="detailSummary" class="detail-summary"></div>
      <div id="detailReason" class="detail-reason" hidden></div>
      <div id="detailCounts" class="detail-counts"></div>
    </header>
    <div class="detail-tabs" role="tablist" aria-label="审查详情">
      <button id="detailTimelineTab" class="detail-tab" type="button" role="tab" data-detail-tab="timeline" aria-controls="detailScrollRegion">执行过程</button>
      <button id="detailFilteredTab" class="detail-tab" type="button" role="tab" data-detail-tab="filtered" aria-controls="detailScrollRegion">被过滤建议</button>
      <button id="detailPublishedTab" class="detail-tab" type="button" role="tab" data-detail-tab="published" aria-controls="detailScrollRegion">已发布建议</button>
      <button id="detailErrorsTab" class="detail-tab" type="button" role="tab" data-detail-tab="errors" aria-controls="detailScrollRegion">失败与异常</button>
    </div>
    <div id="detailScrollRegion" class="detail-scroll-region" role="tabpanel" tabindex="0"></div>
  </section>
</div>

<script>
{js_helpers}
const statusMeta = {{
  waiting: {{ label: '等待自动审查', color: '#60a5fa' }},
  not_triggered: {{ label: '未执行建议审查', color: '#fbbf24' }},
  startup_failed: {{ label: '启动失败', color: '#f97316' }},
  no_suggestions: {{ label: '已执行，无可用建议', color: '#94a3b8' }}, published: {{ label: '已发布', color: '#34d399' }},
  fallback_published: {{ label: '降级发布成功', color: '#22d3ee' }},
  unpublished: {{ label: '有建议，未发布', color: '#f59e0b' }},
  secondary_filtered: {{ label: '二次审查过滤', color: '#a78bfa' }},
  publish_failed: {{ label: '发布失败', color: '#fb7185' }}, execution_failed: {{ label: '执行失败', color: '#f43f5e' }},
}};
const stageMeta = {{
  event_received: '收到创建事件', queued: '自动工作流已排队', workflow_started: '自动工作流启动',
  improve_started: '开始生成建议', generating: '生成建议', scenario_validation: '场景校验', validated: '二次审查完成',
  publishing: '发布中', published: '已发布', publish_failed: '发布失败', skipped: '已跳过',
  startup_failed: '启动失败', execution_failed: '执行失败', historical: '历史关联',
}};
const formatPageInfo = (page, totalPages, rowCount) => `第 ${{page}} / ${{totalPages}} 页（共 ${{rowCount}} 条）`;
const PAGE_SIZE = 15;
let dashboardData = null;
let reviewPager = null;
let reasonChart = null;
let trendChart = null;
let reviewRowsByKey = new Map();
let currentDays = 30;
const detailState = {{ row: null, data: null, origin: null, activeTab: 'timeline', bodyOverflow: '' }};

function ratio(value, total) {{ return total ? `${{(value / total * 100).toFixed(1)}}%` : '0%'; }}
function statusLabel(status) {{ return (statusMeta[status] || {{ label: status || '未知' }}).label; }}
function statusColor(status) {{ return (statusMeta[status] || {{ color: '#94a3b8' }}).color; }}
function stageLabel(stage) {{ return stageMeta[stage] || ''; }}
function reviewKey(row) {{ return `${{row.project || ''}}!${{row.mr_iid || ''}}`; }}

function renderAggregateAlerts(data) {{
  const container = document.getElementById('aggregateAlerts');
  const alerts = data.alerts || [];
  container.hidden = alerts.length === 0;
  container.innerHTML = alerts.map(alert => `
    <div class="aggregate-alert" role="alert">
      <strong>${{escapeHtml(alert.label || alert.key)}}</strong>
      <span>${{Number(alert.count || 0)}} / ${{Number(alert.threshold || 0)}} · ${{Math.round(Number(alert.window_seconds || 0) / 60)}} 分钟</span>
    </div>`).join('');
}}

function createRemotePager({{
  table, tbodyId, pageInfoId, prevBtnId, nextBtnId, emptyColspan, emptyText, renderRow,
  getParams = () => ({{}}), onRows = () => {{}},
}}) {{
  const tbody = document.getElementById(tbodyId);
  const pageInfo = document.getElementById(pageInfoId);
  const previous = document.getElementById(prevBtnId);
  const next = document.getElementById(nextBtnId);
  let page = 1;
  let totalPages = 1;
  let requestToken = 0;

  const load = async (requestedPage = 1) => {{
    const token = ++requestToken;
    previous.disabled = true; next.disabled = true;
    tbody.innerHTML = `<tr><td colspan="${{emptyColspan}}" class="muted">正在加载…</td></tr>`;
    const params = new URLSearchParams({{ page: String(requestedPage), days: String(currentDays) }});
    Object.entries(getParams()).forEach(([key, value]) => {{
      if (value !== '' && value !== null && value !== undefined && value !== false) params.set(key, String(value));
    }});
    try {{
      const response = await fetch(`/api/suggestion-review/table/${{table}}?${{params.toString()}}`);
      if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
      const data = await response.json();
      if (token !== requestToken) return;
      if (data.page_size !== PAGE_SIZE) throw new Error('分页大小不一致');
      page = data.page || 1; totalPages = data.total_pages || 1;
      const rows = data.rows || [];
      tbody.innerHTML = rows.length
        ? rows.map(renderRow).join('')
        : `<tr><td colspan="${{emptyColspan}}" class="muted">${{escapeHtml(emptyText)}}</td></tr>`;
      pageInfo.textContent = formatPageInfo(page, totalPages, data.total_rows || 0);
      previous.disabled = page <= 1; next.disabled = page >= totalPages;
      onRows(rows, data);
    }} catch (error) {{
      if (token !== requestToken) return;
      tbody.innerHTML = `<tr><td colspan="${{emptyColspan}}" class="muted">加载失败：${{escapeHtml(error.message || error)}}</td></tr>`;
      pageInfo.textContent = '加载失败';
    }}
  }};
  previous.onclick = () => load(page - 1);
  next.onclick = () => load(page + 1);
  return {{ load, refresh: () => load(page) }};
}}

function renderStatusOverview(data) {{
  const total = Math.max(1, data.inventory_total || 0);
  const counts = data.status_counts || {{}};
  document.getElementById('statusBar').innerHTML = Object.entries(counts).map(([status, count]) =>
    `<span class="status-segment" style="width:${{count / total * 100}}%;background:${{statusColor(status)}}" title="${{escapeHtml(statusLabel(status))}}：${{count}}"></span>`
  ).join('');
  const filterEntries = [...Object.entries(counts), ['secondary_filtered', data.filtered_mr_total || 0]];
  document.getElementById('statusChips').innerHTML = filterEntries
    .sort((a, b) => b[1] - a[1]).map(([status, count]) =>
      `<button class="status-chip" type="button" data-status="${{escapeHtml(status)}}" aria-pressed="false" style="--status-color:${{statusColor(status)}}"><span class="status-swatch"></span><span>${{escapeHtml(statusLabel(status))}}</span><strong>${{count}}</strong></button>`
    ).join('');
  document.querySelectorAll('.status-chip').forEach(button => {{
    button.onclick = () => {{
      const selected = button.dataset.status;
      const filter = document.getElementById('statusFilter');
      filter.value = filter.value === selected ? '' : selected;
      document.getElementById('attentionOnly').checked = false;
      applyReviewFilters();
      const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      document.getElementById('mrWorkspace').scrollIntoView({{ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' }});
    }};
  }});
}}

function populateFilters(data) {{
  const projects = data.project_options || [];
  document.getElementById('projectFilter').innerHTML = '<option value="">全部项目</option>'
    + projects.map(project => `<option value="${{escapeHtml(project)}}">${{escapeHtml(project)}}</option>`).join('');
  document.getElementById('statusFilter').innerHTML = '<option value="">全部状态</option>'
    + [...Object.keys(data.status_counts || {{}}), 'secondary_filtered']
      .sort((a, b) => statusLabel(a).localeCompare(statusLabel(b), 'zh-CN'))
      .map(status => `<option value="${{escapeHtml(status)}}">${{escapeHtml(statusLabel(status))}}</option>`).join('');
}}

function applyReviewFilters() {{
  if (!dashboardData || !reviewPager) return;
  const status = document.getElementById('statusFilter').value;
  reviewPager.load(1);
  document.querySelectorAll('.status-chip').forEach(button => {{
    button.setAttribute('aria-pressed', String(Boolean(status) && button.dataset.status === status));
  }});
}}

function renderReviewWorkspace(data) {{
  reviewPager = createRemotePager({{
    table: 'review_mrs', tbodyId: 'reviewMrTableBody', pageInfoId: 'reviewMrPageInfo',
    prevBtnId: 'reviewMrPrevPage', nextBtnId: 'reviewMrNextPage', emptyColspan: 6,
    emptyText: '没有符合当前筛选条件的 MR',
    getParams: () => ({{
      query: document.getElementById('mrSearch').value.trim(),
      project: document.getElementById('projectFilter').value,
      status: document.getElementById('statusFilter').value,
      attention_only: document.getElementById('attentionOnly').checked,
    }}),
    onRows: (rows, paging) => {{
      reviewRowsByKey = new Map(rows.map(row => [reviewKey(row), row]));
      document.getElementById('resultCount').textContent = `共 ${{paging.total_rows || 0}} 个 MR`;
    }},
    renderRow: row => `
      <tr class="review-row" tabindex="0" data-review-key="${{escapeHtml(reviewKey(row))}}" aria-label="查看 ${{escapeHtml(row.project)}} MR !${{escapeHtml(row.mr_iid)}} 的创建审查详情"><td class="mr-cell"><div class="mr-project" title="${{escapeHtml(row.project)}}">${{escapeHtml(row.project || '未知项目')}}</div><div class="mr-iid">!${{escapeHtml(row.mr_iid)}}</div></td>
      <td class="title-cell"><div class="mr-title" title="${{escapeHtml(row.title)}}">${{escapeHtml(row.title || '无标题')}}</div><div class="mr-meta">${{escapeHtml(row.owner || '未知作者')}}${{stageLabel(row.stage) ? ` · ${{escapeHtml(stageLabel(row.stage))}}` : ''}}</div></td>
      <td><div class="status-stack"><span class="status-badge" style="--status-color:${{statusColor(row.status)}}">${{escapeHtml(statusLabel(row.status))}}</span>${{row.has_secondary_filter ? `<span class="status-badge" style="--status-color:${{statusColor('secondary_filtered')}}">二次审查过滤</span>` : ''}}${{row.recovery_source === 'sync' ? '<span class="status-badge" style="--status-color:#22d3ee">同步补审</span>' : ''}}${{row.reason_label ? `<div class="status-reason">${{escapeHtml(row.reason_label)}}</div>` : ''}}</div></td>
      <td class="count-group" aria-label="生成 ${{row.generated_count}}，过滤 ${{row.filtered_count}}，发布 ${{row.inline_published_count + row.inline_fallback_count}}"><span>${{row.generated_count}}</span><span>${{row.filtered_count}}</span><span>${{row.inline_published_count + row.inline_fallback_count}}</span></td>
      <td class="muted">${{escapeHtml(row.ts)}}</td><td><div class="row-actions"><button class="detail-action" type="button">查看详情</button>${{row.link ? `<a class="open-link" href="${{escapeHtml(row.link)}}" target="_blank" rel="noreferrer">打开 MR ↗</a>` : ''}}</div></td></tr>`,
  }});
  bindReviewRowInteractions();
  populateFilters(data);
  let searchTimer = null;
  document.getElementById('mrSearch').oninput = () => {{
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(applyReviewFilters, 250);
  }};
  document.getElementById('projectFilter').onchange = applyReviewFilters;
  document.getElementById('statusFilter').onchange = applyReviewFilters;
  document.getElementById('attentionOnly').onchange = applyReviewFilters;
  document.getElementById('clearFilters').onclick = () => {{
    document.getElementById('mrSearch').value = '';
    document.getElementById('projectFilter').value = '';
    document.getElementById('statusFilter').value = '';
    document.getElementById('attentionOnly').checked = false;
    applyReviewFilters();
  }};
  applyReviewFilters();
}}

function bindReviewRowInteractions() {{
  const tbody = document.getElementById('reviewMrTableBody');
  tbody.onclick = event => {{
    const rowElement = event.target.closest('.review-row');
    if (!rowElement) return;
    if (event.target.closest('a, button, input, select, summary')) {{
      if (!event.target.closest('.detail-action')) return;
    }}
    const row = reviewRowsByKey.get(rowElement.dataset.reviewKey);
    if (row) openReviewDetail(row, event.target.closest('.detail-action') || rowElement);
  }};
  tbody.onkeydown = event => {{
    if (!['Enter', ' '].includes(event.key) || event.target.closest('a, button')) return;
    const rowElement = event.target.closest('.review-row');
    const row = rowElement ? reviewRowsByKey.get(rowElement.dataset.reviewKey) : null;
    if (row) {{ event.preventDefault(); openReviewDetail(row, rowElement); }}
  }};
}}

function defaultDetailTab(row) {{
  if (row.has_secondary_filter) return 'filtered';
  if (['startup_failed', 'execution_failed', 'publish_failed'].includes(row.status)) return 'errors';
  if (['published', 'fallback_published'].includes(row.status)) return 'published';
  return 'timeline';
}}

function setDetailTab(tabName, focusTab = false) {{
  detailState.activeTab = tabName;
  document.querySelectorAll('[data-detail-tab]').forEach(tab => {{
    const active = tab.dataset.detailTab === tabName;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active && focusTab) tab.focus();
  }});
  renderActiveDetailTab();
}}

function renderDetailLoading() {{
  document.getElementById('detailScrollRegion').innerHTML = '<div class="detail-list" aria-label="正在加载详情"><div class="detail-skeleton"></div><div class="detail-skeleton"></div><div class="detail-skeleton"></div></div>';
}}

function detailLineLabel(item) {{
  if (item.line_start == null) return '';
  return item.line_end && item.line_end !== item.line_start ? `:${{item.line_start}}-${{item.line_end}}` : `:${{item.line_start}}`;
}}

function renderSuggestionCards(items, emptyText) {{
  if (!items.length) return `<div class="empty-state">${{escapeHtml(emptyText)}}</div>`;
  return `<div class="detail-list">${{items.map(item => `
    <article class="suggestion-card">
      <div class="suggestion-head"><div><div class="suggestion-path">${{escapeHtml(item.file_path || '未知文件')}}${{escapeHtml(detailLineLabel(item))}}</div><div class="suggestion-meta">${{escapeHtml(item.label || item.severity || '代码建议')}}${{item.score == null ? '' : ` · 分数 ${{item.score}}`}}</div></div><span class="status-badge" style="--status-color:${{item.disposition === 'published' ? '#34d399' : item.disposition === 'fallback_published' ? '#22d3ee' : '#a78bfa'}}">${{item.disposition === 'published' ? '已发布' : item.disposition === 'fallback_published' ? '降级发布成功' : '已过滤'}}</span></div>
      ${{item.summary ? `<div class="suggestion-copy">${{escapeHtml(item.summary)}}</div>` : ''}}
      <div class="suggestion-copy">${{escapeHtml(item.suggestion || '建议正文未保存')}}</div>
      ${{item.existing_code ? `<details><summary>查看原代码</summary><pre class="suggestion-code">${{escapeHtml(item.existing_code)}}</pre></details>` : ''}}
      ${{item.improved_code ? `<details><summary>查看建议代码 / 补丁</summary><pre class="suggestion-code">${{escapeHtml(item.improved_code)}}</pre></details>` : ''}}
      <div class="disposition-row"><strong>处理结果：</strong>${{escapeHtml(item.reason || (item.disposition === 'published' ? '已成功发布到 GitLab' : item.disposition === 'fallback_published' ? '已降级为普通 MR 评论' : '过滤原因未记录'))}}${{item.discussion_url ? ` · <a class="open-link" href="${{escapeHtml(item.discussion_url)}}" target="_blank" rel="noreferrer">查看讨论 ↗</a>` : ''}}</div>
    </article>`).join('')}}</div>`;
}}

function renderTimeline(items) {{
  if (!items.length) return '<div class="empty-state">本次创建审查暂无可追溯的执行事件</div>';
  return `<div class="detail-list">${{items.map(item => `
    <article class="timeline-item"><div class="timeline-time">${{escapeHtml(item.created_at || '时间未记录')}}</div><div><strong>${{escapeHtml(stageLabel(item.stage) || item.stage || item.event_key)}}</strong><div class="detail-subtitle">${{escapeHtml(item.status || '')}}${{item.error_message ? ` · ${{escapeHtml(item.error_message)}}` : ''}}</div>${{item.details && Object.keys(item.details).length ? `<details><summary>查看事件数据</summary><pre class="suggestion-code">${{escapeHtml(JSON.stringify(item.details, null, 2))}}</pre></details>` : ''}}</div></article>`).join('')}}</div>`;
}}

function renderErrors(items) {{
  if (!items.length) return '<div class="empty-state">本次创建审查没有记录失败或异常</div>';
  return `<div class="detail-list">${{items.map(item => `
    <article class="error-card"><div class="suggestion-head"><strong>${{escapeHtml(stageLabel(item.stage) || item.stage || '异常')}}</strong><span class="timeline-time">${{escapeHtml(item.created_at || '')}}</span></div><div class="suggestion-copy">${{escapeHtml(item.message || '未记录异常详情')}}</div><div class="suggestion-meta">${{escapeHtml(item.error_code || '')}}${{item.file_path ? ` · ${{escapeHtml(item.file_path)}}` : ''}}</div></article>`).join('')}}</div>`;
}}

function renderDetailHeader(data) {{
  const mr = data.mr || {{}}; const counts = data.counts || {{}};
  document.getElementById('detailDialogTitle').textContent = `${{mr.project || detailState.row.project}} !${{mr.mr_iid || detailState.row.mr_iid}}`;
  document.getElementById('detailDialogSubtitle').textContent = mr.title || detailState.row.title || '创建审查详情';
  document.getElementById('detailSummary').innerHTML = `
    <span>作者：${{escapeHtml(mr.author || detailState.row.owner || '未知')}}</span>
    <span>创建：${{escapeHtml(mr.created_at || detailState.row.ts || '—')}}</span>
    <span>初始 SHA：${{escapeHtml(mr.initial_commit_sha || detailState.row.commit_sha || '—')}}</span>
    <span class="status-badge" style="--status-color:${{statusColor(mr.status || detailState.row.status)}}">${{escapeHtml(statusLabel(mr.status || detailState.row.status))}}</span>
    ${{mr.has_secondary_filter ? `<span class="status-badge" style="--status-color:${{statusColor('secondary_filtered')}}">二次审查过滤</span>` : ''}}
    ${{mr.link ? `<a class="open-link" href="${{escapeHtml(mr.link)}}" target="_blank" rel="noreferrer">打开 MR ↗</a>` : ''}}`;
  document.getElementById('detailCounts').innerHTML = [
    ['生成', counts.generated], ['过滤', counts.filtered], ['跳过', counts.skipped],
    ['发布', Number(counts.published || 0) + Number(counts.fallback_published || 0)], ['失败', counts.failed],
  ].map(([label, value]) => `<span class="detail-count">${{label}} ${{Number(value || 0)}}</span>`).join('');
  const reason = document.getElementById('detailReason');
  if (mr.reason_label) {{
    reason.hidden = false;
    reason.textContent = `${{mr.reason_label}}${{mr.reason_code ? ` · ${{mr.reason_code}}` : ''}}`;
  }} else {{
    reason.hidden = true; reason.textContent = '';
  }}
}}

function renderActiveDetailTab() {{
  const region = document.getElementById('detailScrollRegion');
  const data = detailState.data;
  if (!data) {{ renderDetailLoading(); return; }}
  if (data.detail_state === 'unavailable') {{
    region.innerHTML = '<div class="empty-state">该 MR 暂无可追溯的创建审查详情</div>';
    return;
  }}
  if (detailState.activeTab === 'filtered') {{
    region.innerHTML = renderSuggestionCards(data.filtered_suggestions || [], '本次创建审查没有被二次审查过滤的建议');
  }} else if (detailState.activeTab === 'published') {{
    region.innerHTML = renderSuggestionCards(data.published_suggestions || [], '本次创建审查没有已发布建议');
  }} else if (detailState.activeTab === 'errors') {{
    region.innerHTML = renderErrors(data.errors || []);
  }} else {{
    region.innerHTML = renderTimeline(data.timeline || []);
  }}
  region.scrollTop = 0;
}}

async function loadReviewDetail() {{
  const row = detailState.row;
  if (!row) return;
  detailState.data = null; renderDetailLoading();
  try {{
    const query = new URLSearchParams({{ project: row.project, mr_iid: row.mr_iid }});
    const response = await fetch(`/api/suggestion-review/detail?${{query.toString()}}`);
    if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
    const data = await response.json();
    if (detailState.row !== row) return;
    detailState.data = data; renderDetailHeader(data); renderActiveDetailTab();
  }} catch (error) {{
    if (detailState.row !== row) return;
    document.getElementById('detailScrollRegion').innerHTML = `<div class="empty-state">详情加载失败：${{escapeHtml(error.message || String(error))}}<br /><button id="detailRetryButton" class="detail-retry" type="button">重新加载</button></div>`;
    document.getElementById('detailRetryButton').onclick = loadReviewDetail;
  }}
}}

function openReviewDetail(row, triggerElement) {{
  const dialog = document.getElementById('reviewDetailDialog');
  detailState.row = row; detailState.origin = triggerElement; detailState.data = null;
  detailState.bodyOverflow = document.body.style.overflow;
  document.body.style.overflow = 'hidden'; dialog.hidden = false;
  document.getElementById('detailDialogTitle').textContent = `${{row.project}} !${{row.mr_iid}}`;
  document.getElementById('detailDialogSubtitle').textContent = row.title || '创建审查详情';
  document.getElementById('detailSummary').innerHTML = '';
  document.getElementById('detailReason').hidden = true;
  document.getElementById('detailReason').textContent = '';
  document.getElementById('detailCounts').innerHTML = '';
  setDetailTab(defaultDetailTab(row));
  dialog.querySelector('.detail-panel').focus();
  loadReviewDetail();
}}

function restoreDialogFocus() {{
  if (detailState.origin && detailState.origin.isConnected) detailState.origin.focus();
  detailState.origin = null;
}}

function closeReviewDetail() {{
  const dialog = document.getElementById('reviewDetailDialog');
  if (dialog.hidden) return;
  dialog.hidden = true; document.body.style.overflow = detailState.bodyOverflow;
  detailState.row = null; detailState.data = null; restoreDialogFocus();
}}

function trapDialogFocus(event) {{
  if (event.key !== 'Tab') return;
  const panel = document.querySelector('#reviewDetailDialog .detail-panel');
  const focusable = [...panel.querySelectorAll('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')]
    .filter(item => !item.hidden && item.offsetParent !== null);
  if (!focusable.length) {{ event.preventDefault(); panel.focus(); return; }}
  const first = focusable[0]; const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {{ event.preventDefault(); last.focus(); }}
  else if (!event.shiftKey && document.activeElement === last) {{ event.preventDefault(); first.focus(); }}
}}

document.querySelectorAll('[data-detail-tab]').forEach((tab, index, tabs) => {{
  tab.onclick = () => setDetailTab(tab.dataset.detailTab);
  tab.onkeydown = event => {{
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const offset = event.key === 'ArrowRight' ? 1 : -1;
    const target = tabs[(index + offset + tabs.length) % tabs.length];
    setDetailTab(target.dataset.detailTab, true);
  }};
}});
document.getElementById('detailCloseButton').onclick = closeReviewDetail;
document.querySelector('[data-close-dialog]').onclick = closeReviewDetail;
document.getElementById('reviewDetailDialog').addEventListener('keydown', trapDialogFocus);
document.addEventListener('keydown', event => {{
  if (event.key === 'Escape' && !document.getElementById('reviewDetailDialog').hidden) closeReviewDetail();
}});

function renderQuality(data) {{
  document.getElementById('mPub').textContent = data.pub_total;
  document.getElementById('mFiltered').textContent = data.filtered_total;
  document.getElementById('mRate').textContent = `${{data.filter_rate}}%`;
  document.getElementById('mMR').textContent = data.mr_count;
  document.getElementById('filteredDetailCount').textContent = `${{data.filtered_total || 0}} 条`;
  if (reasonChart) reasonChart.destroy();
  if (trendChart) trendChart.destroy();
  const chartText = '#94a3b8'; const chartGrid = 'rgba(148,163,184,.12)';
  reasonChart = new Chart(document.getElementById('reasonChart'), {{
    type: 'bar', data: {{ labels: data.reason_labels, datasets: [{{ data: data.reason_values, backgroundColor: '#a78bfa', borderRadius: 5 }}] }},
    options: {{ responsive: true, maintainAspectRatio: false, indexAxis: 'y', plugins: {{ legend: {{ display: false }} }},
      scales: {{ x: {{ grid: {{ color: chartGrid }}, ticks: {{ color: chartText }} }}, y: {{ grid: {{ display: false }}, ticks: {{ color: chartText }} }} }} }}
  }});
  trendChart = new Chart(document.getElementById('trendChart'), {{
    type: 'line', data: {{ labels: data.week_labels, datasets: [{{ label: '过滤率 %', data: data.week_values, borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,.12)', fill: true, tension: .3, pointRadius: 3 }}] }},
    options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ labels: {{ color: chartText }} }} }},
      scales: {{ x: {{ grid: {{ display: false }}, ticks: {{ color: chartText }} }}, y: {{ min: 0, max: 100, grid: {{ color: chartGrid }}, ticks: {{ color: chartText }} }} }} }}
  }});
  createRemotePager({{
    table: 'filter_projects', tbodyId: 'projectTableBody', pageInfoId: 'projectPageInfo',
    prevBtnId: 'projectPrevPage', nextBtnId: 'projectNextPage', emptyColspan: 5, emptyText: '暂无项目数据',
    renderRow: row => `<tr><td>${{escapeHtml(row.project)}}</td><td>${{row.pub}}</td><td>${{row.filtered}}</td><td>${{row.rate}}%</td><td>${{row.mr_count}}</td></tr>`,
  }}).load(1);
  createRemotePager({{
    table: 'filter_mrs', tbodyId: 'mrTableBody', pageInfoId: 'mrPageInfo',
    prevBtnId: 'mrPrevPage', nextBtnId: 'mrNextPage', emptyColspan: 7, emptyText: '暂无 MR 数据',
    renderRow: row => `<tr><td>${{escapeHtml(row.mr)}}</td><td>${{row.pub}}</td><td>${{row.filtered}}</td><td>${{row.rate}}%</td><td>${{escapeHtml(row.ts)}}</td><td>${{escapeHtml(row.owner || '—')}}</td><td>${{row.link ? `<a class="open-link" href="${{escapeHtml(row.link)}}" target="_blank" rel="noreferrer">打开 MR ↗</a>` : '—'}}</td></tr>`,
  }}).load(1);
  createRemotePager({{
    table: 'filtered_suggestions', tbodyId: 'filteredTableBody', pageInfoId: 'filteredPageInfo',
    prevBtnId: 'filteredPrevPage', nextBtnId: 'filteredNextPage', emptyColspan: 7, emptyText: '暂无被过滤建议',
    renderRow: row => `<tr><td>${{escapeHtml(row.ts)}}</td><td><div>${{escapeHtml(row.project)}}</div><div class="mr-iid">${{escapeHtml(row.mr)}}</div></td><td>${{escapeHtml(row.file)}}</td><td>${{escapeHtml(row.label || '—')}}</td><td>${{row.score ?? '—'}}</td><td><span class="status-badge" style="--status-color:#a78bfa">${{escapeHtml(row.reason)}}</span></td><td>${{row.link ? `<a class="open-link" href="${{escapeHtml(row.link)}}" target="_blank" rel="noreferrer">打开 MR ↗</a>` : '—'}}<details><summary class="muted">查看建议</summary><div class="suggestion-content">${{escapeHtml(row.content)}}</div></details></td></tr>`,
  }}).load(1);
}}

function configureAnalysisTabs() {{
  const projectTab = document.getElementById('projectAnalysisTab');
  const mrTab = document.getElementById('mrAnalysisTab');
  const select = target => {{
    const projectActive = target === 'project';
    projectTab.classList.toggle('active', projectActive); mrTab.classList.toggle('active', !projectActive);
    projectTab.setAttribute('aria-selected', String(projectActive)); mrTab.setAttribute('aria-selected', String(!projectActive));
    document.getElementById('projectAnalysisPanel').hidden = !projectActive;
    document.getElementById('mrAnalysisPanel').hidden = projectActive;
  }};
  projectTab.onclick = () => select('project'); mrTab.onclick = () => select('mr');
}}

async function loadDashboard(days) {{
  currentDays = Number(days) || 30;
  const refresh = document.getElementById('refreshButton');
  refresh.disabled = true; refresh.setAttribute('aria-busy', 'true');
  document.getElementById('syncState').textContent = '正在加载';
  try {{
    const response = await fetch(`/api/suggestion-review/summary?days=${{days}}`);
    if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
    const data = await response.json(); dashboardData = data;
    const total = data.inventory_total || 0;
    document.getElementById('mInventory').textContent = total;
    document.getElementById('mTriggered').textContent = data.triggered_total;
    document.getElementById('mTriggeredRate').textContent = ratio(data.triggered_total, total);
    document.getElementById('mCompleted').textContent = data.completed_total;
    document.getElementById('mCompletedRate').textContent = ratio(data.completed_total, total);
    document.getElementById('mPublishedMR').textContent = data.published_mr_total;
    document.getElementById('mPublishedRate').textContent = ratio(data.published_mr_total, total);
    document.getElementById('attentionCount').textContent = data.attention_total;
    const sync = data.sync || {{}}; const hasError = Boolean(sync.last_error);
    document.getElementById('syncDot').classList.toggle('error', hasError);
    document.getElementById('syncState').textContent = hasError ? '同步异常' : '同步正常';
    document.getElementById('syncTime').textContent = hasError ? sync.last_error : `最后同步 ${{sync.last_success_at || '等待首次同步'}}`;
    renderAggregateAlerts(data); renderStatusOverview(data); renderReviewWorkspace(data); renderQuality(data);
  }} catch (error) {{
    document.getElementById('syncDot').classList.add('error');
    document.getElementById('syncState').textContent = '加载失败';
    document.getElementById('syncTime').textContent = String(error.message || error);
  }} finally {{ refresh.disabled = false; refresh.removeAttribute('aria-busy'); }}
}}

configureAnalysisTabs();
document.getElementById('daysFilter').onchange = event => loadDashboard(event.target.value);
document.getElementById('refreshButton').onclick = () => loadDashboard(document.getElementById('daysFilter').value);
loadDashboard(30);
</script>
</body>
</html>
"""
