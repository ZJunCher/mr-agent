"""Dependency-free owner repair report page."""

# ruff: noqa: E501 -- Embedded dependency-free HTML/CSS/JS is intentionally kept as a removable page module.

from __future__ import annotations

import json


def render_repair_result_page(task_id: str, signature: str, *, embedded: bool = False) -> str:
    template = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="robots" content="noindex,nofollow">
  <meta name="color-scheme" content="light">
  <title>CI 修复报告 · PR-Agent</title>
  <style>
    :root {
      --page: #f4f6f8;
      --paper: #ffffff;
      --ink: #182230;
      --muted: #667085;
      --subtle: #98a2b3;
      --line: #e4e7ec;
      --line-strong: #d0d5dd;
      --blue: #175cd3;
      --blue-soft: #eff6ff;
      --green: #067647;
      --green-soft: #ecfdf3;
      --red: #b42318;
      --red-soft: #fef3f2;
      --amber: #b54708;
      --amber-soft: #fffaeb;
      --code: #ffffff;
      --code-raised: #f1f5f9;
      --code-line: #d8dee4;
      --code-text: #1f2328;
      --code-muted: #57606a;
      --add: #ecf4ee;
      --delete: #fbe9eb;
      --radius: 12px;
      font-synthesis: none;
    }
    * { box-sizing: border-box; }
    html { min-width: 320px; background: var(--page); scroll-behavior: smooth; }
    body {
      min-height: 100vh;
      margin: 0;
      overflow-x: hidden;
      color: var(--ink);
      background: var(--page);
      font: 15px/1.65 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    button, a, summary { -webkit-tap-highlight-color: transparent; }
    button, .button-link, summary { min-height: 44px; }
    :is(button, a, summary):focus-visible { outline: 3px solid #84adff; outline-offset: 2px; }
    .mono, code, .code-line { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }
    .shell { width: min(100% - 32px, 1160px); margin: 0 auto; padding: 24px 0 72px; }
    .topbar { display: flex; min-height: 46px; align-items: center; justify-content: space-between; gap: 18px; }
    .brand { display: flex; align-items: center; gap: 10px; color: #344054; font-weight: 700; letter-spacing: -.01em; }
    .brand-mark { display: grid; width: 30px; height: 30px; place-items: center; border-radius: 8px; color: white; background: #101828; }
    .brand-mark svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 1.8; }
    .connection { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; white-space: nowrap; }
    .connection-dot { width: 7px; height: 7px; border-radius: 50%; background: #f79009; }
    .connection.live .connection-dot, .connection.settled .connection-dot { background: #12b76a; }
    .connection.offline .connection-dot { background: #f04438; }
    .breadcrumb { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin-top: 26px; color: var(--muted); font-size: 13px; }
    .breadcrumb a { color: var(--blue); text-decoration: none; }
    .breadcrumb a:hover { text-decoration: underline; text-underline-offset: 3px; }
    .page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 28px; padding: 16px 0 22px; }
    .page-title { max-width: 820px; margin: 0; font-size: clamp(24px, 3.6vw, 36px); line-height: 1.24; letter-spacing: -.035em; }
    .page-subtitle { margin: 8px 0 0; color: var(--muted); }
    .button-link { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 7px; padding: 0 14px; border: 1px solid var(--line-strong); border-radius: 8px; color: #344054; background: white; font-weight: 600; text-decoration: none; }
    .button-link:hover { border-color: #98a2b3; background: #f9fafb; }
    .outcome { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 16px; padding: 18px 20px; border: 1px solid #abefc6; border-radius: var(--radius); background: var(--green-soft); }
    .outcome.danger { border-color: #fecdca; background: var(--red-soft); }
    .outcome.warning { border-color: #fedf89; background: var(--amber-soft); }
    .outcome.live { border-color: #b2ddff; background: var(--blue-soft); }
    .outcome-icon { display: grid; width: 36px; height: 36px; place-items: center; border-radius: 50%; color: white; background: var(--green); font-size: 20px; font-weight: 800; }
    .danger .outcome-icon { background: var(--red); }
    .warning .outcome-icon { background: var(--amber); }
    .live .outcome-icon { background: var(--blue); }
    .outcome-title { font-size: 17px; font-weight: 750; letter-spacing: -.01em; }
    .outcome-copy { margin-top: 2px; color: #475467; }
    .outcome-facts { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px 14px; color: #475467; font-size: 12px; }
    .report { margin-top: 18px; overflow: clip; border: 1px solid var(--line); border-radius: var(--radius); background: var(--paper); }
    .report-section { display: grid; grid-template-columns: 190px minmax(0, 1fr); gap: 34px; padding: 30px 32px; }
    .report-section + .report-section { border-top: 1px solid var(--line); }
    .section-kicker { color: var(--subtle); font-size: 11px; font-weight: 750; letter-spacing: .11em; text-transform: uppercase; }
    .section-title { margin: 4px 0 0; font-size: 18px; line-height: 1.35; letter-spacing: -.015em; }
    .section-note { margin: 7px 0 0; color: var(--muted); font-size: 13px; line-height: 1.5; }
    .prose { max-width: 780px; }
    .prose p { margin: 0; }
    .prose p + p { margin-top: 12px; }
    .lead { color: #344054; font-size: 16px; font-weight: 560; line-height: 1.75; }
    .evidence { margin-top: 16px; padding: 13px 15px; border-left: 3px solid #98a2b3; border-radius: 0 7px 7px 0; color: #475467; background: #f9fafb; white-space: pre-wrap; overflow-wrap: anywhere; }
    .source-jobs { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }
    .job-log-link { display: inline-flex; min-height: 28px; align-items: center; padding: 2px 9px; border: 1px solid #b2ddff; border-radius: 999px; color: var(--blue); background: var(--blue-soft); font-size: 11px; line-height: 1.5; text-decoration: none; }
    .job-log-link:hover { border-color: #84adff; text-decoration: underline; text-underline-offset: 2px; }
    .issue + .issue { margin-top: 24px; padding-top: 24px; border-top: 1px dashed var(--line-strong); }
    .issue-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; }
    .badge { display: inline-flex; min-height: 25px; align-items: center; padding: 1px 8px; border: 1px solid #d0d5dd; border-radius: 999px; color: #475467; background: #fff; font-size: 11px; font-weight: 700; }
    .badge.success { border-color: #abefc6; color: var(--green); background: var(--green-soft); }
    .badge.danger { border-color: #fecdca; color: var(--red); background: var(--red-soft); }
    .badge.blue { border-color: #b2ddff; color: var(--blue); background: var(--blue-soft); }
    .empty { padding: 16px; border: 1px dashed var(--line-strong); border-radius: 8px; color: var(--muted); background: #fcfcfd; }
    .diff-section { padding: 0; }
    .diff-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 26px 32px 20px; }
    .diff-summary { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 7px; color: var(--muted); font-size: 13px; }
    .diff-toolbar { display: flex; padding: 3px; border: 1px solid var(--line-strong); border-radius: 8px; background: #f9fafb; }
    .diff-toggle { min-height: 36px; padding: 0 12px; border: 0; border-radius: 6px; color: var(--muted); background: transparent; cursor: pointer; font: inherit; font-size: 12px; font-weight: 650; }
    .diff-toggle.active { color: #101828; background: white; box-shadow: 0 1px 3px rgba(16, 24, 40, .12); }
    .file-nav { display: flex; gap: 8px; padding: 0 32px 18px; overflow-x: auto; scrollbar-width: thin; }
    .file-nav a { flex: 0 0 auto; padding: 6px 10px; border: 1px solid var(--line); border-radius: 7px; color: #344054; background: #fff; font-size: 12px; text-decoration: none; }
    .file-nav a:hover { border-color: #98a2b3; }
    .file-change { border-top: 1px solid var(--line); scroll-margin-top: 12px; }
    .file-change > summary { display: flex; list-style: none; align-items: center; justify-content: space-between; gap: 18px; padding: 14px 18px; cursor: pointer; color: #344054; background: #f9fafb; }
    .file-change > summary::-webkit-details-marker { display: none; }
    .file-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 650; }
    .file-stats { flex: 0 0 auto; color: var(--muted); font-size: 12px; }
    .additions { color: #079455; }
    .deletions { color: #d92d20; }
    .file-explanation { margin: 0; padding: 11px 18px; border-top: 1px solid var(--line); color: #475467; background: #fff; font-size: 13px; }
    .diff-view { min-width: 0; overflow: hidden; color: var(--code-text); background: var(--code); }
    .hunk-header { padding: 8px 14px; border-top: 1px solid var(--code-line); border-bottom: 1px solid var(--code-line); color: #0550ae; background: #f1f8ff; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }
    .unified-row { display: grid; grid-template-columns: 54px 54px 26px minmax(0, 1fr); min-width: 0; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }
    .side-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); min-width: 0; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }
    .code-cell { display: grid; grid-template-columns: 52px 24px minmax(0, 1fr); min-width: 0; min-height: 23px; overflow: hidden; }
    .line-number { padding: 2px 9px; border-right: 1px solid rgba(208, 215, 222, .7); color: var(--code-muted); background: rgba(246, 248, 250, .82); text-align: right; user-select: none; }
    .line-marker { padding: 2px 5px; color: var(--code-muted); text-align: center; user-select: none; }
    .line-content { min-width: 0; padding: 2px 12px 2px 5px; overflow-x: auto; white-space: pre; scrollbar-width: thin; }
    .line-addition, .cell-addition { background: var(--add); }
    .line-deletion, .cell-deletion { background: var(--delete); }
    .cell-empty { border-left: 1px solid var(--code-line); background: #f6f8fa; }
    .truncated { padding: 11px 16px; border-top: 1px solid var(--code-line); color: var(--amber); background: var(--code-raised); font-size: 12px; }
    .validation-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .fact { padding: 13px 14px; border: 1px solid var(--line); border-radius: 8px; background: #fcfcfd; }
    .fact dt { color: var(--muted); font-size: 11px; font-weight: 650; }
    .fact dd { margin: 4px 0 0; color: #344054; font-size: 13px; overflow-wrap: anywhere; }
    .fact a { color: var(--blue); text-decoration: none; }
    .jobs { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }
    .run-log { border-top: 1px solid var(--line); background: #fcfcfd; }
    .run-log > summary { display: flex; list-style: none; align-items: center; justify-content: space-between; padding: 16px 32px; cursor: pointer; color: #475467; font-weight: 650; }
    .run-log > summary::-webkit-details-marker { display: none; }
    .run-log > summary::after { content: "+"; color: var(--muted); font-size: 20px; font-weight: 400; }
    .run-log[open] > summary::after { content: "−"; }
    .timeline-scroll { max-height: 420px; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
    .timeline { margin: 0; padding: 0 32px 22px 52px; list-style: none; }
    .timeline-item { position: relative; padding: 7px 0; color: #475467; }
    .timeline-item::before { position: absolute; top: 16px; left: -20px; width: 6px; height: 6px; border-radius: 50%; background: #98a2b3; content: ""; }
    .timeline-meta { color: var(--subtle); font-size: 11px; }
    .error-banner { display: none; margin-top: 14px; padding: 12px 14px; border: 1px solid #fecdca; border-radius: 8px; color: var(--red); background: var(--red-soft); }
    .error-banner.visible { display: block; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    @media (max-width: 760px) {
      .shell { width: min(100% - 20px, 1160px); padding-top: 12px; }
      .topbar { min-height: 40px; }
      .connection span:last-child { display: none; }
      .breadcrumb { margin-top: 17px; }
      .page-head { display: block; padding-top: 11px; }
      .button-link { margin-top: 15px; }
      .outcome { grid-template-columns: auto minmax(0, 1fr); padding: 15px; }
      .outcome-facts { grid-column: 1 / -1; justify-content: flex-start; padding-left: 52px; }
      .report-section { display: block; padding: 23px 18px; }
      .section-heading { margin-bottom: 18px; }
      .diff-heading { display: block; padding: 22px 18px 16px; }
      .diff-toolbar { width: fit-content; margin-top: 15px; }
      #sideButton { display: none; }
      .file-nav { padding: 0 18px 14px; }
      .validation-grid { grid-template-columns: 1fr; }
      .run-log > summary { padding: 15px 18px; }
      .timeline { padding: 0 18px 18px 38px; }
      .timeline-scroll { max-height: 50vh; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; }
    }
    body.embedded { min-height: 0; background: transparent; }
    body.embedded .shell { width: 100%; padding: 0; }
    body.embedded .topbar { display: none; }
    body.embedded .report { margin-top: 0; box-shadow: none; }
  </style>
</head>
<body class="__BODY_CLASS__">
  <div class="shell">
    <header class="topbar">
      <div class="brand"><span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M7 3v4M17 3v4M5 7h14v10a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3V7Zm4 5h6M9 16h3"/></svg></span><span>PR-Agent · CI Repair</span></div>
      <div id="connection" class="connection"><span class="connection-dot" aria-hidden="true"></span><span id="connectionText">正在连接</span></div>
    </header>
    <main>
      <nav id="breadcrumb" class="breadcrumb" aria-label="当前位置"></nav>
      <div class="page-head">
        <div><h1 id="pageTitle" class="page-title">正在载入 CI 修复报告</h1><p id="pageSubtitle" class="page-subtitle">正在读取修复结论和代码改动。</p></div>
        <a id="mrLink" class="button-link" href="#" target="_blank" rel="noopener noreferrer">在 GitLab 查看 MR</a>
      </div>
      <section id="outcome" class="outcome live" aria-labelledby="outcomeTitle">
        <div id="outcomeIcon" class="outcome-icon" aria-hidden="true">…</div>
        <div><div id="outcomeTitle" class="outcome-title">正在读取修复状态</div><div id="outcomeCopy" class="outcome-copy">请稍候。</div></div>
        <div id="outcomeFacts" class="outcome-facts"></div>
      </section>
      <section id="rollbackStrip" class="outcome warning" hidden aria-label="撤回结果"></section>
      <div id="errorBanner" class="error-banner" role="alert"></div>
      <article class="report">
        <section class="report-section" aria-labelledby="causeTitle">
          <div class="section-heading"><div class="section-kicker">01 · Cause</div><h2 id="causeTitle" class="section-title">失败原因</h2><p class="section-note">流水线证据确认的问题。</p></div>
          <div id="causeList" class="prose"><div class="empty">正在分析失败原因…</div></div>
        </section>
        <section class="report-section" aria-labelledby="solutionTitle">
          <div class="section-heading"><div class="section-kicker">02 · Solution</div><h2 id="solutionTitle" class="section-title">修复方案</h2><p class="section-note">实际采用的修改及其理由。</p></div>
          <div id="solutionList" class="prose"><div class="empty">尚未生成可靠的方案说明。</div></div>
        </section>
        <section class="diff-section" aria-labelledby="changesTitle">
          <div class="diff-heading">
            <div><div class="section-kicker">03 · Changes</div><h2 id="changesTitle" class="section-title">代码改动</h2><div id="diffSummary" class="diff-summary"></div></div>
            <div class="diff-toolbar" role="group" aria-label="代码差异显示方式"><button id="sideButton" class="diff-toggle active" type="button">并排</button><button id="unifiedButton" class="diff-toggle" type="button">统一</button></div>
          </div>
          <nav id="fileNav" class="file-nav" aria-label="修改文件"></nav>
          <div id="diffList"><div class="empty" style="margin:0 32px 26px">等待代码改动…</div></div>
        </section>
        <section class="report-section" aria-labelledby="validationTitle">
          <div class="section-heading"><div class="section-kicker">04 · Verification</div><h2 id="validationTitle" class="section-title">验证结果</h2><p class="section-note">以匹配修复 Commit 的流水线为准。</p></div>
          <div><dl id="validationFacts" class="validation-grid"></dl><div id="remainingJobs" class="jobs"></div></div>
        </section>
        <details id="runLog" class="run-log" open><summary>运行记录</summary><div id="timelineScroll" class="timeline-scroll"><ol id="timeline" class="timeline"><li class="empty">等待运行记录…</li></ol></div></details>
      </article>
      <div id="liveAnnouncement" class="sr-only" aria-live="polite" aria-atomic="true"></div>
    </main>
  </div>
  <script>
  (() => {
    'use strict';
    const taskId = __TASK_ID__;
    const signature = __SIGNATURE__;
    const embedded = __EMBEDDED__;
    const apiBase = `/api/repair-results/${encodeURIComponent(taskId)}`;
    const query = `sig=${encodeURIComponent(signature)}`;
    const categoryNames = { format: 'Format', clang: 'Clang', build: 'Build', unknown: 'Unknown' };
    const phaseNames = { queued: '已进入队列', preparing: '准备工作区', diagnosing: '正在诊断', editing: '正在修改', committing: '正在提交', waiting_pipeline: '等待流水线', validating: '正在验证', triage_running: '正在诊断与修复', triage_waiting: '等待流水线', format_running: '正在修复格式', format_waiting: '等待格式流水线', terminal: '已结束' };
    let snapshot = null;
    let eventSource = null;
    let pollingTimer = null;
    let reconnects = 0;
    let diffMode = window.matchMedia('(max-width: 760px)').matches ? 'unified' : 'side';
    const byId = id => document.getElementById(id);
    const create = (tag, className = '', text = null) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== null) node.textContent = String(text); return node; };
    const safeUrl = value => { try { const url = new URL(String(value)); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; } catch (_) { return ''; } };
    function jobLogHref(job) {
      const base = safeUrl(job && job.job_url); if (!base) return '';
      const url = new URL(base); const traceLine = Number(job.trace_line || 0); url.hash = '';
      if (Number.isSafeInteger(traceLine) && traceLine > 0) url.hash = `L${traceLine}`;
      return url.href;
    }
    function appendSourceJobLinks(parent, sourceJobs) {
      const links = create('div', 'source-jobs');
      (Array.isArray(sourceJobs) ? sourceJobs : []).forEach(job => {
        const href = jobLogHref(job); if (!href) return;
        const traceLine = Number(job.trace_line || 0); const precise = Number.isSafeInteger(traceLine) && traceLine > 0;
        const label = `↗ ${job.job_name || `Job #${job.job_id || '—'}`}${precise ? ` · 定位第 ${traceLine} 行` : ' · 查看日志'}`;
        const link = create('a', 'job-log-link mono', label); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; links.appendChild(link);
      });
      if (links.childElementCount) parent.appendChild(links);
    }
    const shortSha = value => value ? String(value).slice(0, 12) : '—';
    const formatTime = value => { if (!value) return '—'; const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value); return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(date); };
    const projectBase = mrUrl => String(mrUrl || '').split('/-/merge_requests/')[0];
    const repairOutcome = data => data.repair_outcome || (data.final_pipeline && data.final_pipeline.status === 'success' ? 'success' : 'failed');
    const terminalSuccess = data => Boolean(data.terminal && repairOutcome(data) === 'success');
    const terminalPartial = data => Boolean(data.terminal && repairOutcome(data) === 'partial_success');
    const terminalBlocked = data => Boolean(data.terminal && repairOutcome(data) === 'blocked');
    const isSettled = data => Boolean(data && data.terminal && (!data.report || ['not_applicable', 'model_generated', 'fallback'].includes(data.report.status)));
    const hasChanges = data => Boolean((data.final_file_changes || []).length || (data.actions || []).some(action => (action.file_changes || []).length || (action.changed_files || []).length));
    function outcome(data) {
      if (!data.terminal) return { tone: 'live', icon: '…', title: phaseNames[data.phase] || '修复进行中' };
      if (data.status === 'canceled') return { tone: 'warning', icon: '–', title: '修复已取消' };
      if (terminalSuccess(data)) return { tone: 'success', icon: '✓', title: '修复成功' };
      if (terminalPartial(data)) return { tone: 'warning', icon: '!', title: '部分修复成功' };
      if (terminalBlocked(data)) return { tone: 'warning', icon: '!', title: '外部依赖阻塞' };
      if (hasChanges(data)) return { tone: 'danger', icon: '!', title: '已修改但验证未通过' };
      return { tone: 'danger', icon: '×', title: '修复未完成' };
    }
    function categoryBadges(parent, action) {
      (action.categories || []).forEach(category => parent.appendChild(create('span', 'badge blue', categoryNames[category] || category)));
      (action.job_names || []).forEach(job => parent.appendChild(create('span', 'badge mono', job)));
    }
    function renderHeader(data) {
      const mr = data.mr || {};
      byId('pageTitle').textContent = mr.title || `${mr.project || '项目'} !${mr.iid || ''} CI 修复报告`;
      byId('pageSubtitle').textContent = `${mr.project || '未知项目'} · MR !${mr.iid || '—'} · ${mr.source_branch || '未提供分支'}`;
      const link = byId('mrLink'); const href = safeUrl(mr.url); link.href = href || '#'; link.hidden = !href;
      const breadcrumb = byId('breadcrumb'); breadcrumb.replaceChildren(create('span', '', mr.project || '未知项目'), create('span', '', '/'), create('span', '', `MR !${mr.iid || '—'}`), create('span', '', '/'), create('strong', '', 'CI 修复报告'));
    }
    function renderOutcome(data) {
      const state = outcome(data); const box = byId('outcome'); box.className = `outcome ${state.tone === 'success' ? '' : state.tone}`;
      byId('outcomeIcon').textContent = state.icon; byId('outcomeTitle').textContent = state.title;
      const actions = data.actions || []; const report = data.report || {}; const solution = report.solution_summary || actions.find(action => action.solution_summary)?.solution_summary;
      let copy = solution || (['queued', 'generating'].includes(report.status) ? '正在根据最终代码生成修复说明。' : (data.terminal ? (data.error || '本次任务未记录可靠的方案说明。') : '正在确认失败原因并生成安全修复。'));
      byId('outcomeCopy').textContent = copy;
      const facts = byId('outcomeFacts'); facts.replaceChildren();
      const finalResult = data.final_pipeline || {};
      facts.append(
        create('span', 'mono', terminalBlocked(data) ? '修复 Commit 未产生' : `Commit ${shortSha(finalResult.sha)}`),
        create('span', 'mono', finalResult.id ? `验证 Pipeline #${finalResult.id}` : '验证 Pipeline 待生成'),
      );
      const rollback = data.rollback; const strip = byId('rollbackStrip'); strip.replaceChildren(); strip.hidden = !rollback;
      if (rollback) {
        const ok = rollback.status === 'succeeded'; strip.className = `outcome ${ok ? '' : 'warning'}`;
        const icon = create('div', 'outcome-icon', ok ? '↶' : '!'); const body = create('div');
        body.append(create('div', 'outcome-title', ok ? '自动修复已撤回' : '撤回未完成'));
        const detail = ok ? `撤回 Commit ${shortSha(rollback.rollback_commit_sha)}` : (rollback.failure_message || '请人工检查分支状态');
        body.append(create('div', 'outcome-copy', `${rollback.trigger === 'cancel' ? '取消修复' : '修复完成后撤回'} · ${detail}`));
        strip.append(icon, body);
      }
    }
    function dependencyCandidateText(candidate) {
      const files = Object.entries(candidate.file_paths || {}).map(([name, path]) => `${name}: ${path}`);
      const identity = [candidate.branch, candidate.resolved_sha ? shortSha(candidate.resolved_sha) : ''].filter(Boolean).join(' @ ');
      return [identity, ...files].filter(Boolean).join(' · ') || '未提供候选分支详情';
    }
    function appendDependencyFacts(parent, blocker) {
      const evidenceList = Array.isArray(blocker.dependency_evidence) ? blocker.dependency_evidence : [];
      evidenceList.forEach(evidence => {
        const current = evidence.current_branch || {};
        const project = evidence.project_path || '未知依赖项目';
        const branch = evidence.declared_branch || current.branch || '未知分支';
        const sha = evidence.declared_sha || current.resolved_sha;
        const currentItem = create('section', 'issue');
        currentItem.append(create('span', 'badge mono', '当前依赖'), create('p', 'lead mono', `${project} @ ${branch}${sha ? ` · ${shortSha(sha)}` : ''}`));
        parent.appendChild(currentItem);
        (evidence.verified_candidates || []).forEach(candidate => {
          const item = create('section', 'issue');
          item.append(create('span', 'badge success', '已验证候选'), create('p', 'lead mono', dependencyCandidateText(candidate)));
          parent.appendChild(item);
        });
      });
    }
    function renderIssues(data) {
      const actions = data.actions || []; const report = data.report || {}; const causeRoot = byId('causeList'); const solutionRoot = byId('solutionList'); causeRoot.replaceChildren(); solutionRoot.replaceChildren();
      byId('solutionTitle').textContent = terminalBlocked(data) ? '建议处理' : '修复方案';
      if (terminalBlocked(data)) {
        const blocker = data.blocker || {};
        const cause = create('section', 'issue');
        cause.append(create('span', 'badge danger', '外部依赖'), create('p', 'lead', blocker.summary || '当前失败由仓库外部依赖导致。'));
        appendSourceJobLinks(cause, data.source_jobs); causeRoot.appendChild(cause);
        const solution = create('section', 'issue');
        solution.append(create('span', 'badge blue', '建议处理'), create('p', 'lead', blocker.suggested_action || '请维护者确认并修复外部依赖。'));
        solution.appendChild(create('p', '', '当前仓库不能安全补齐上游接口定义；本次未修改当前仓库代码。'));
        solutionRoot.appendChild(solution); appendDependencyFacts(solutionRoot, blocker); return;
      }
      if (['queued', 'generating'].includes(report.status)) { causeRoot.appendChild(create('div', 'empty', '正在根据最终代码核对失败原因。')); solutionRoot.appendChild(create('div', 'empty', '正在根据最终代码生成修复说明。')); return; }
      if (report.root_cause_summary || report.solution_summary) {
        const cause = create('section', 'issue'); const causeHead = create('div', 'issue-head');
        causeHead.appendChild(create('span', 'badge blue', report.source === 'model' ? '模型总结' : 'Diff 兜底')); cause.append(causeHead, create('p', 'lead', report.root_cause_summary || '根因未能可靠确认。'));
        appendSourceJobLinks(cause, data.source_jobs);
        causeRoot.appendChild(cause);
        const solution = create('section', 'issue'); const solutionHead = create('div', 'issue-head');
        solutionHead.appendChild(create('span', report.source === 'model' ? 'badge success' : 'badge', report.source === 'model' ? '已核验' : 'Diff 兜底'));
        solution.append(solutionHead, create('p', 'lead', report.solution_summary || '未生成可靠的方案说明。'));
        if (report.rationale) solution.appendChild(create('p', '', `为什么这样改：${report.rationale}`));
        if (report.failure_reason) solution.appendChild(create('p', '', `说明限制：${report.failure_reason}`));
        solutionRoot.appendChild(solution); return;
      }
      if (!actions.length) { causeRoot.appendChild(create('div', 'empty', '正在收集流水线失败证据。')); solutionRoot.appendChild(create('div', 'empty', '尚未生成可靠的方案说明。')); return; }
      actions.forEach((action, index) => {
        const cause = create('section', 'issue'); const causeHead = create('div', 'issue-head'); categoryBadges(causeHead, action); cause.appendChild(causeHead);
        cause.appendChild(create('p', 'lead', action.root_cause || '根因尚未确认。'));
        if (action.evidence && action.evidence !== action.root_cause) cause.appendChild(create('div', 'evidence mono', action.evidence));
        if (index === 0) appendSourceJobLinks(cause, data.source_jobs);
        causeRoot.appendChild(cause);
        const solution = create('section', 'issue'); const solutionHead = create('div', 'issue-head'); categoryBadges(solutionHead, action); solution.appendChild(solutionHead);
        solution.appendChild(create('p', 'lead', action.solution_summary || '未生成可靠的方案说明。'));
        if (action.rationale) solution.appendChild(create('p', '', `为什么这样改：${action.rationale}`));
        if (action.failure_reason) solution.appendChild(create('p', '', `未完成原因：${action.failure_reason}`));
        solutionRoot.appendChild(solution);
      });
    }
    function allFileChanges(data) {
      if ((data.final_file_changes || []).length) return data.final_file_changes;
      const output = new Map();
      (data.actions || []).forEach(action => (action.file_changes || []).forEach(change => { if (!output.has(change.path)) output.set(change.path, change); }));
      return [...output.values()];
    }
    function captureDiffState() {
      const state = new Map();
      document.querySelectorAll('.file-change').forEach(details => state.set(details.id, { open: details.open, scroll: [...details.querySelectorAll('.line-content')].map(node => node.scrollLeft) }));
      return state;
    }
    function restoreDiffState(state) {
      document.querySelectorAll('.file-change').forEach(details => { const saved = state.get(details.id); if (!saved) return; details.open = saved.open; details.querySelectorAll('.line-content').forEach((node, index) => { node.scrollLeft = saved.scroll[index] || 0; }); });
    }
    function lineCell(line, side) {
      if (!line) return create('div', 'code-cell cell-empty');
      const cell = create('div', `code-cell cell-${line.kind}`); const number = side === 'old' ? line.old_line : line.new_line;
      const marker = line.kind === 'addition' ? '+' : line.kind === 'deletion' ? '−' : ' ';
      cell.append(create('span', 'line-number', number ?? ''), create('span', 'line-marker', marker), create('span', 'line-content', line.content || ''));
      return cell;
    }
    function pairedRows(lines) {
      const rows = []; let removed = [];
      const flush = additions => { const count = Math.max(removed.length, additions.length); for (let i = 0; i < count; i += 1) rows.push([removed[i] || null, additions[i] || null]); removed = []; };
      let additions = [];
      (lines || []).forEach(line => {
        if (line.kind === 'deletion') { if (additions.length) { flush(additions); additions = []; } removed.push(line); }
        else if (line.kind === 'addition') additions.push(line);
        else { if (removed.length || additions.length) { flush(additions); additions = []; } rows.push([line, line]); }
      });
      if (removed.length || additions.length) flush(additions);
      return rows;
    }
    function renderHunk(hunk, mode) {
      const fragment = document.createDocumentFragment(); fragment.appendChild(create('div', 'hunk-header', `@@ -${hunk.old_start || 0} +${hunk.new_start || 0} @@ ${hunk.header || ''}`));
      if (mode === 'unified') {
        (hunk.lines || []).forEach(line => { const row = create('div', `unified-row line-${line.kind}`); row.append(create('span', 'line-number', line.old_line ?? ''), create('span', 'line-number', line.new_line ?? ''), create('span', 'line-marker', line.kind === 'addition' ? '+' : line.kind === 'deletion' ? '−' : ' '), create('span', 'line-content', line.content || '')); fragment.appendChild(row); });
      } else pairedRows(hunk.lines || []).forEach(([oldLine, newLine]) => { const row = create('div', 'side-row'); row.append(lineCell(oldLine, 'old'), lineCell(newLine, 'new')); fragment.appendChild(row); });
      return fragment;
    }
    function renderDiff(data) {
      const previousState = captureDiffState();
      const changes = allFileChanges(data); const root = byId('diffList'); const nav = byId('fileNav'); root.replaceChildren(); nav.replaceChildren();
      const additions = changes.reduce((sum, item) => sum + Number(item.additions || 0), 0); const deletions = changes.reduce((sum, item) => sum + Number(item.deletions || 0), 0);
      const summary = byId('diffSummary'); summary.replaceChildren(create('span', '', `${changes.length} 个文件`), create('span', 'additions', `+${additions}`), create('span', 'deletions', `−${deletions}`));
      byId('sideButton').classList.toggle('active', diffMode === 'side'); byId('unifiedButton').classList.toggle('active', diffMode === 'unified');
      if (!changes.length) { root.appendChild(create('div', 'empty', terminalBlocked(data) ? '本次未修改当前仓库代码。' : data.terminal ? '本次任务没有记录到可展示的代码改动。' : '代码修改完成后将在这里展示真实差异。')); return; }
      const explanations = new Map(((data.report || {}).file_explanations || []).map(item => [item.path, item.summary]));
      changes.forEach((change, index) => {
        const id = `file-${index}`; const navLink = create('a', 'mono', change.path); navLink.href = `#${id}`; nav.appendChild(navLink);
        const details = document.createElement('details'); details.className = 'file-change'; details.id = id; details.open = index < 3;
        const heading = document.createElement('summary'); heading.append(create('span', 'file-name mono', change.path), create('span', 'file-stats', `${change.change_type || 'modified'} · `));
        heading.lastChild.append(create('span', 'additions', `+${change.additions || 0}`), document.createTextNode(' '), create('span', 'deletions', `−${change.deletions || 0}`)); details.appendChild(heading);
        const explanation = explanations.get(change.path) || change.summary; if (explanation) details.appendChild(create('p', 'file-explanation', explanation));
        const view = create('div', 'diff-view');
        if (change.binary) view.appendChild(create('div', 'truncated', '二进制文件已变更，不展示文件内容。'));
        else if (!(change.hunks || []).length) view.appendChild(create('div', 'truncated', '未保存内联差异，请在 GitLab 查看完整改动。'));
        else (change.hunks || []).forEach(hunk => view.appendChild(renderHunk(hunk, diffMode)));
        if (change.truncated) view.appendChild(create('div', 'truncated', `差异过大，已省略 ${change.omitted_lines || 0} 行；请在 GitLab 查看完整改动。`));
        details.appendChild(view); root.appendChild(details);
      });
      restoreDiffState(previousState);
    }
    function addFact(root, label, value, href = '') { const fact = create('div', 'fact'); fact.appendChild(create('dt', '', label)); const dd = create('dd', 'mono'); if (href) { const link = create('a', '', value); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; dd.appendChild(link); } else dd.textContent = value || '—'; fact.appendChild(dd); root.appendChild(fact); }
    function renderValidation(data) {
      const root = byId('validationFacts'); root.replaceChildren(); const mr = data.mr || {}; const source = data.source_pipeline || {}; const finalResult = data.final_pipeline || {}; const base = projectBase(mr.url);
      addFact(root, '原始流水线', source.id ? `#${source.id}` : '—', source.id && base ? `${base}/-/pipelines/${source.id}` : '');
      addFact(root, '原始 Commit', shortSha(source.sha), source.sha && base ? `${base}/-/commit/${encodeURIComponent(source.sha)}` : '');
      addFact(root, '验证流水线', finalResult.id ? `#${finalResult.id}` : '等待生成', finalResult.id && base ? `${base}/-/pipelines/${finalResult.id}` : '');
      addFact(root, '验证 Commit', shortSha(finalResult.sha), finalResult.sha && base ? `${base}/-/commit/${encodeURIComponent(finalResult.sha)}` : '');
      addFact(root, '流水线状态', finalResult.status || (data.terminal ? 'unknown' : '等待验证'));
      const coverageLabels = { changed_lines: '变更行覆盖率', gitlab_pipeline: 'Pipeline 覆盖率' };
      const coverageReasons = { validation_pipeline_missing: '尚未找到验证流水线', not_configured: '流水线未配置覆盖率 Job', job_failed: '覆盖率 Job 失败，未产出结果', report_missing: '覆盖率 Job 通过，但未产出报告', fetch_failed: '覆盖率报告读取失败' };
      const coverageLabel = coverageLabels[finalResult.coverage_source] || '覆盖率';
      const coverageDisplay = finalResult.coverage !== null && finalResult.coverage !== undefined ? `${finalResult.coverage}%` : (coverageReasons[finalResult.coverage_status] || '未提供');
      addFact(root, coverageLabel, coverageDisplay);
      addFact(root, '开始时间', formatTime(data.created_at)); addFact(root, '最后更新', formatTime(data.updated_at));
      const jobs = byId('remainingJobs'); jobs.replaceChildren(); (data.failed_job_names || []).forEach(job => jobs.appendChild(create('span', 'badge danger mono', `仍失败：${job}`)));
      if (!(data.failed_job_names || []).length && terminalSuccess(data)) {
        const verifiedJobs = [...new Set((data.actions || []).flatMap(action => action.job_names || []))];
        verifiedJobs.forEach(job => jobs.appendChild(create('span', 'badge success mono', `已通过：${job}`)));
        if (!verifiedJobs.length) jobs.appendChild(create('span', 'badge success', '最新流水线已通过'));
      }
    }
    function renderTimeline(events) {
      const scroller = byId('timelineScroll'); const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 24; const previousTop = scroller.scrollTop;
      const root = byId('timeline'); root.replaceChildren(); if (!events || !events.length) { root.appendChild(create('li', 'empty', '没有可用的运行记录。')); return; }
      events.forEach(event => { const item = create('li', 'timeline-item'); const count = Number(event.count || 1); item.append(create('div', '', `${event.summary || phaseNames[event.phase] || '状态更新'}${count > 1 ? ` × ${count}` : ''}`), create('div', 'timeline-meta', `${phaseNames[event.phase] || event.phase || '进度'} · ${formatTime(event.occurred_at)}`)); root.appendChild(item); });
      scroller.scrollTop = nearBottom ? scroller.scrollHeight : previousTop;
    }
    function render(data) {
      snapshot = data; renderHeader(data); renderOutcome(data); renderIssues(data); renderDiff(data); renderValidation(data); renderTimeline(data.progress || []);
      const banner = byId('errorBanner'); if (data.error) { banner.textContent = data.error; banner.classList.add('visible'); } else { banner.textContent = ''; banner.classList.remove('visible'); }
      byId('liveAnnouncement').textContent = `修复状态更新：${outcome(data).title}`;
      if (isSettled(data)) stopLiveUpdates();
      notifyEmbeddedHeight();
    }
    function setConnection(state, text) { byId('connection').className = `connection ${state}`; byId('connectionText').textContent = text; }
    async function fetchSnapshot() { const response = await fetch(`${apiBase}?${query}`, { headers: { Accept: 'application/json' }, cache: 'no-store' }); if (!response.ok) throw new Error(response.status === 404 ? '修复详情不存在或链接已失效。' : '暂时无法读取修复详情。'); render(await response.json()); }
    function startPolling() { if (pollingTimer) return; setConnection('offline', '实时连接中断，自动刷新'); pollingTimer = setInterval(() => fetchSnapshot().catch(() => {}), 3000); }
    function stopLiveUpdates() { if (eventSource) { eventSource.close(); eventSource = null; } if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; } setConnection('settled', '报告已更新'); }
    function notifyEmbeddedHeight() {
      if (!embedded || window.parent === window) return;
      const height = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
      window.parent.postMessage({ type: 'repair-detail-height', taskId, height }, window.location.origin);
    }
    function openStream() {
      if (!window.EventSource || isSettled(snapshot)) return;
      eventSource = new EventSource(`${apiBase}/events?${query}`);
      eventSource.addEventListener('snapshot', event => { try { render(JSON.parse(event.data)); } catch (_) {} });
      eventSource.addEventListener('progress', () => { fetchSnapshot().catch(() => {}); });
      eventSource.onopen = () => { reconnects = 0; setConnection('live', '实时更新中'); if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; } };
      eventSource.onerror = () => { reconnects += 1; if (eventSource) eventSource.close(); eventSource = null; if (reconnects >= 2) startPolling(); else setTimeout(openStream, 1500); };
    }
    byId('sideButton').addEventListener('click', () => { if (window.matchMedia('(max-width: 760px)').matches) return; diffMode = 'side'; if (snapshot) renderDiff(snapshot); });
    byId('unifiedButton').addEventListener('click', () => { diffMode = 'unified'; if (snapshot) renderDiff(snapshot); });
    window.matchMedia('(max-width: 760px)').addEventListener('change', event => { if (event.matches && diffMode !== 'unified') { diffMode = 'unified'; if (snapshot) renderDiff(snapshot); } });
    if (embedded && window.ResizeObserver) {
      const resizeObserver = new ResizeObserver(notifyEmbeddedHeight);
      resizeObserver.observe(document.documentElement);
      window.addEventListener('pagehide', () => resizeObserver.disconnect(), { once: true });
    }
    window.addEventListener('pagehide', stopLiveUpdates, { once: true });
    fetchSnapshot().then(() => { if (isSettled(snapshot)) setConnection('settled', '报告已更新'); else openStream(); }).catch(error => { const banner = byId('errorBanner'); banner.textContent = error.message; banner.classList.add('visible'); setConnection('offline', '读取失败'); notifyEmbeddedHeight(); startPolling(); });
  })();
  </script>
</body>
</html>'''
    return (
        template.replace("__TASK_ID__", json.dumps(task_id))
        .replace("__SIGNATURE__", json.dumps(signature))
        .replace("__BODY_CLASS__", "embedded" if embedded else "standalone")
        .replace("__EMBEDDED__", "true" if embedded else "false")
    )
