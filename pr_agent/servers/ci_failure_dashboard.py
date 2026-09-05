# ruff: noqa: E501
"""API helpers and server-rendered UI for durable CI failure analysis."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pr_agent.feedback.store import get_db_path as get_feedback_db_path
from pr_agent.triage.ci_failure_store import get_ci_failure, query_ci_failures, save_annotation


class CiFailureAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int | None = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=300)
    capability: Literal["", "supported", "capability_gap", "infrastructure", "unknown"] = ""
    note: str = Field(default="", max_length=1000)

    @field_validator("reason", "note", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> str:
        return str(value or "").strip()


def collect_ci_failure_summary(
    *,
    days: int | None,
    project: str = "",
    family: str = "",
    capability: str = "",
    fingerprint: str = "",
    query: str = "",
    page: int = 1,
    page_size: int = 20,
    recurring_page: int = 1,
    recurring_page_size: int = 5,
    project_distribution_page: int = 1,
    project_distribution_page_size: int = 5,
    job_distribution_page: int = 1,
    job_distribution_page_size: int = 5,
) -> dict:
    return query_ci_failures(
        {
            "days": days,
            "project": project,
            "family": family,
            "capability": capability,
            "fingerprint": fingerprint,
            "q": query,
            "page": max(1, page),
            "page_size": min(100, max(1, page_size)),
            "recurring_page": max(1, recurring_page),
            "recurring_page_size": min(100, max(1, recurring_page_size)),
            "project_distribution_page": max(1, project_distribution_page),
            "project_distribution_page_size": min(100, max(1, project_distribution_page_size)),
            "job_distribution_page": max(1, job_distribution_page),
            "job_distribution_page_size": min(100, max(1, job_distribution_page_size)),
        },
        path=get_feedback_db_path(),
    )


def collect_ci_failure_detail(failure_id: int) -> dict | None:
    return get_ci_failure(failure_id, path=get_feedback_db_path())


def annotate_ci_failure(failure_id: int, request: CiFailureAnnotationRequest) -> dict | None:
    changed = save_annotation(
        failure_id,
        job_id=request.job_id,
        reason=request.reason,
        capability=request.capability,
        note=request.note,
        path=get_feedback_db_path(),
    )
    return collect_ci_failure_detail(failure_id) if changed else None


def render_ci_failure_dashboard(base_css: str, operations_css: str, nav_html: str, js_helpers: str) -> str:
    """Render the approved hybrid analysis layout using the shared operations theme."""
    template = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>CI 失败分析看板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>__BASE_CSS____OPERATIONS_CSS__
.ci-filter { display:flex; gap:12px; flex-wrap:wrap; align-items:end; margin:14px 0 16px; padding:14px;
  border:1px solid var(--ops-border); border-radius:14px; background:var(--ops-surface); }
.ci-filter-label { display:grid; gap:6px; color:var(--ops-muted); font-size:12px; font-weight:650; }
.ci-filter-label.grow { flex:1 1 220px; }.ci-filter-label input { width:100%; }
.ci-filter input,.ci-filter select,.ci-annotation input,.ci-annotation select,.ci-annotation textarea {
  border:1px solid var(--ops-border); border-radius:8px; background:#0c1626; color:var(--ops-text); padding:9px 11px;
}
.ci-filter button,.ci-annotation button { border:0; border-radius:8px; background:var(--ops-blue); color:white;
  min-height:44px; padding:9px 16px; cursor:pointer; font-weight:700; }
.ci-pattern { padding:11px 0; border-bottom:1px solid var(--ops-border); }
.ci-pattern:last-child { border-bottom:0; }.ci-pattern strong { display:block; color:var(--ops-text); }
.ci-pattern span { color:var(--ops-muted); font-size:12px; }.ci-table-row { cursor:pointer; }
.ci-detail { margin-top:18px; }.ci-detail[hidden] { display:none; }.ci-job { margin-top:10px; padding:14px;
  border:1px solid var(--ops-border); border-radius:10px; background:rgba(8,15,27,.38); }
.ci-job-head { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }.ci-job-reason { margin-top:8px; line-height:1.6; }
.ci-annotation { display:grid; grid-template-columns:180px 1fr 180px auto; gap:8px; margin-top:14px; }
.ci-annotation textarea { grid-column:1/4; min-height:72px; resize:vertical; }.ci-annotation button { grid-column:4; grid-row:1/3; }
.ci-switch { display:flex; gap:8px; }.ci-switch button { border:1px solid var(--ops-border); border-radius:7px;
  background:transparent; color:var(--ops-muted); padding:5px 9px; cursor:pointer; }.ci-switch button.active { color:white; background:#1b2b45; }
.ci-pagination { display:flex; justify-content:center; align-items:center; gap:10px; flex-wrap:wrap; margin-top:14px; }
.ci-pagination[hidden] { display:none; }.ci-pagination button { border:1px solid var(--ops-border); border-radius:7px;
  min-height:44px; background:#101d31; color:var(--ops-text); padding:7px 14px; cursor:pointer; }.ci-pagination button:disabled {
  cursor:not-allowed; opacity:.42; }.ci-page-info { color:var(--ops-muted); font-size:13px; min-width:150px; text-align:center; }
.ops-dashboard-light .ci-filter input,.ops-dashboard-light .ci-filter select,.ops-dashboard-light .ci-annotation input,
.ops-dashboard-light .ci-annotation select,.ops-dashboard-light .ci-annotation textarea,.ops-dashboard-light .ci-pagination button {
  color:var(--ops-text); border-color:var(--ops-border-strong); background:#fff; }
.ops-dashboard-light .ci-switch button { min-height:44px; color:#475569; background:#fff; }
.ops-dashboard-light .ci-switch button.active { color:#fff; border-color:#1e40af; background:#1e40af; }
@media(max-width:800px){.ci-annotation{grid-template-columns:1fr}.ci-annotation textarea,.ci-annotation button{grid-column:1;grid-row:auto}}
</style>
</head>
<body class="ops-dashboard ops-dashboard-light"><main class="ops-shell">
__NAV__
<section class="ops-hero"><div><div class="ops-eyebrow">Continuous Integration</div><h1>CI 失败分析</h1>
<p>汇总开放 MR 的失败 Pipeline、关键原因和 UT-Agent 能力判断。</p></div>
<div class="ops-live" aria-live="polite"><span class="ops-live-dot"></span><div>数据状态<strong id="loadedAt">正在加载...</strong></div></div></section>
<form class="ci-filter" id="filters">
  <label class="ci-filter-label"><span>时间范围</span><select id="days"><option value="7">最近 7 天</option><option value="30" selected>最近 30 天</option><option value="90">最近 90 天</option><option value="0">全部</option></select></label>
  <label class="ci-filter-label grow"><span>项目</span><input id="project" placeholder="例如 eabot/cook" /></label>
  <label class="ci-filter-label"><span>失败类别</span><select id="family"><option value="">全部类别</option><option value="build">Build</option><option value="clang">Clang</option><option value="format">Format</option><option value="test">Test</option><option value="coverage">Coverage</option><option value="infrastructure">环境/基础设施</option><option value="unknown">未知</option></select></label>
  <label class="ci-filter-label"><span>能力判断</span><select id="capability"><option value="">全部能力判断</option><option value="supported">当前支持</option><option value="capability_gap">能力不足</option><option value="infrastructure">环境问题</option><option value="unknown">无法判断</option></select></label>
  <label class="ci-filter-label grow"><span>关键词</span><input id="query" placeholder="搜索项目、MR 标题或原因" /></label><button type="submit">查询</button>
</form>
<section class="ops-metrics">
  <div class="metric-card" style="--metric-accent:#fb7185"><div class="metric-label">失败 Pipeline</div><div class="metric-value" id="mPipelines">--</div></div>
  <div class="metric-card" style="--metric-accent:#fbbf24"><div class="metric-label">失败 Job</div><div class="metric-value" id="mJobs">--</div></div>
  <div class="metric-card" style="--metric-accent:#a78bfa"><div class="metric-label">未明确原因</div><div class="metric-value" id="mUnknown">--</div></div>
  <div class="metric-card" style="--metric-accent:#60a5fa"><div class="metric-label">重复错误模式</div><div class="metric-value" id="mRecurring">--</div></div>
</section>
<section class="ops-grid">
  <div class="ops-card"><div class="section-head"><div><h2 class="section-title">失败趋势</h2><div class="section-subtitle">每日失败 Pipeline 数量</div></div><span class="section-kicker">DAILY</span></div><div class="chart-wrap"><canvas id="trendChart"></canvas></div></div>
  <div class="ops-card"><div class="section-head"><div><h2 class="section-title">失败类别</h2><div class="section-subtitle">按失败 Job 的确定性分类统计</div></div><span class="section-kicker">CATEGORY</span></div><div class="chart-wrap"><canvas id="categoryChart"></canvas></div></div>
</section>
<section class="ops-grid">
  <div class="ops-card"><div class="section-head"><div><h2 class="section-title">高频错误模式</h2><div class="section-subtitle">仅聚合具有明确原因的稳定指纹</div></div><span class="section-kicker">RECURRING</span></div><div id="patterns"></div><nav class="ci-pagination" id="recurringPagination" aria-label="高频错误模式分页" hidden><button type="button" id="recurringPrev">上一页</button><span class="ci-page-info" id="recurringPageInfo"></span><button type="button" id="recurringNext">下一页</button></nav></div>
  <div class="ops-card"><div class="section-head"><div><h2 class="section-title">高频分布</h2><div class="section-subtitle">快速定位问题集中区域</div></div><div class="ci-switch"><button type="button" id="showProjects" class="active" aria-pressed="true">项目</button><button type="button" id="showJobs" aria-pressed="false">Job</button></div></div><div id="topList"></div><nav class="ci-pagination" id="distributionPagination" aria-label="高频分布分页" hidden><button type="button" id="distributionPrev">上一页</button><span class="ci-page-info" id="distributionPageInfo"></span><button type="button" id="distributionNext">下一页</button></nav></div>
</section>
<section class="ops-card"><div class="section-head"><div><h2 class="section-title">失败 Pipeline 明细</h2><div class="section-subtitle">点击一行展开失败 Job 和人工修正</div></div><span class="section-kicker">PIPELINES</span></div>
<div class="table-wrap"><table class="ops-table wide"><thead><tr><th>时间</th><th>项目 / MR</th><th>Pipeline</th><th>失败类别</th><th>主失败原因</th><th>能力判断</th><th>UT-Agent</th><th>通知</th></tr></thead><tbody id="failureRows"></tbody></table></div>
<nav class="ci-pagination" id="pipelinePagination" aria-label="失败 Pipeline 分页" hidden><button type="button" id="pipelinePrev">上一页</button><span class="ci-page-info" id="pipelinePageInfo"></span><button type="button" id="pipelineNext">下一页</button></nav></section>
<section class="ops-card ci-detail" id="detail" hidden><div class="section-head"><div><h2 class="section-title" id="detailTitle">Pipeline 详情</h2><div class="section-subtitle" id="detailMeta"></div></div><span class="section-kicker">EVIDENCE</span></div><div id="jobs"></div>
<form class="ci-annotation" id="annotation"><select id="annotationJob"></select><input id="annotationReason" maxlength="300" placeholder="人工修正原因" /><select id="annotationCapability"><option value="">保留系统判断</option><option value="supported">当前支持</option><option value="capability_gap">能力不足</option><option value="infrastructure">环境问题</option><option value="unknown">无法判断</option></select><textarea id="annotationNote" maxlength="1000" placeholder="分析备注"></textarea><button type="submit">保存人工修正</button></form></section>
</main><script>
__JS_HELPERS__
let trendChart, categoryChart, latestData, selectedFailureId, loadSequence=0;
let pipelinePage=1, recurringPage=1;
let projectDistributionPage=1, jobDistributionPage=1;
let activeDistribution='project';
const capabilityNames={supported:'当前支持',capability_gap:'能力不足',infrastructure:'环境问题',unknown:'无法判断'};
const notificationNames={not_attempted:'未通知',queued:'待发送',delivered:'已送达',recipient_missing:'未找到收件人',failed:'发送失败'};
function params(){const p=new URLSearchParams();['days','project','family','capability'].forEach(id=>p.set(id,document.getElementById(id).value));p.set('q',document.getElementById('query').value);p.set('page',String(pipelinePage));p.set('page_size','15');p.set('recurring_page',String(recurringPage));p.set('recurring_page_size','5');p.set('project_distribution_page',String(projectDistributionPage));p.set('project_distribution_page_size','5');p.set('job_distribution_page',String(jobDistributionPage));p.set('job_distribution_page_size','5');return p;}
function renderTop(rows,key){document.getElementById('topList').innerHTML=(rows||[]).map(r=>`<div class="ci-pattern"><strong>${escapeHtml(r[key])}</strong><span>${r.count} 次</span></div>`).join('')||'<span class="muted">暂无数据</span>';}
function renderCharts(data){if(trendChart)trendChart.destroy();if(categoryChart)categoryChart.destroy();const grid='rgba(148,163,184,.22)',text='#64748b';trendChart=new Chart(document.getElementById('trendChart'),{type:'line',data:{labels:data.trend.map(x=>x.day),datasets:[{label:'失败 Pipeline',data:data.trend.map(x=>x.count),borderColor:'#dc2626',backgroundColor:'rgba(220,38,38,.08)',fill:true,tension:.35}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{color:grid},ticks:{color:text}},y:{beginAtZero:true,grid:{color:grid},ticks:{color:text,precision:0}}}}});categoryChart=new Chart(document.getElementById('categoryChart'),{type:'bar',data:{labels:data.categories.map(x=>x.family),datasets:[{data:data.categories.map(x=>x.count),backgroundColor:'#3b82f6',borderRadius:5}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{display:false},ticks:{color:text}},y:{beginAtZero:true,grid:{color:grid},ticks:{color:text,precision:0}}}}});}
function renderPager(id,page,totalPages,total){const pager=document.getElementById(id+'Pagination');pager.hidden=total===0;document.getElementById(id+'PageInfo').textContent=total===0?'':`第 ${page}/${totalPages} 页，共 ${total} 条`;document.getElementById(id+'Prev').disabled=total===0||page<=1;document.getElementById(id+'Next').disabled=total===0||page>=totalPages;}
function renderPipelinePagination(data){renderPager('pipeline',data.page,data.total_pages,data.total);}
function renderRecurringPagination(data){renderPager('recurring',data.recurring_page,data.recurring_total_pages,data.recurring_total);}
function renderDistribution(data){if(activeDistribution==='project'){renderTop(data.top_projects,'project_path');renderPager('distribution',data.project_distribution_page,data.project_distribution_total_pages,data.project_distribution_total);}else{renderTop(data.top_jobs,'job_name');renderPager('distribution',data.job_distribution_page,data.job_distribution_total_pages,data.job_distribution_total);}}
function setPaginationLoading(loading){if(!loading)return;document.querySelectorAll('.ci-pagination button').forEach(button=>{button.disabled=true;});}
async function loadData(){const sequence=++loadSequence;setPaginationLoading(true);try{const response=await fetch('/api/ci-failures/summary?'+params());if(!response.ok)throw new Error('load failed');const data=await response.json();if(sequence!==loadSequence)return;if(data.total_pages>0&&pipelinePage>data.total_pages){pipelinePage=data.total_pages;return loadData();}if(data.recurring_total_pages>0&&recurringPage>data.recurring_total_pages){recurringPage=data.recurring_total_pages;return loadData();}if(data.project_distribution_total_pages>0&&projectDistributionPage>data.project_distribution_total_pages){projectDistributionPage=data.project_distribution_total_pages;return loadData();}if(data.job_distribution_total_pages>0&&jobDistributionPage>data.job_distribution_total_pages){jobDistributionPage=data.job_distribution_total_pages;return loadData();}if(data.total_pages===0)pipelinePage=1;if(data.recurring_total_pages===0)recurringPage=1;if(data.project_distribution_total_pages===0)projectDistributionPage=1;if(data.job_distribution_total_pages===0)jobDistributionPage=1;latestData=data;document.getElementById('loadedAt').textContent=new Date().toLocaleString('zh-CN');document.getElementById('mPipelines').textContent=data.metrics.failed_pipelines;document.getElementById('mJobs').textContent=data.metrics.failed_jobs;document.getElementById('mUnknown').textContent=data.metrics.unknown_reason_jobs;document.getElementById('mRecurring').textContent=data.metrics.recurring_patterns;renderCharts(data);document.getElementById('patterns').innerHTML=data.recurring.map(r=>`<div class="ci-pattern"><strong>${escapeHtml(r.reason||'未知原因')}</strong><span>${r.occurrences} 次 · ${r.project_count} 个项目 · 最近 ${escapeHtml(r.last_seen||'')}</span></div>`).join('')||'<span class="muted">暂无重复模式</span>';renderRecurringPagination(data);renderDistribution(data);document.getElementById('failureRows').innerHTML=data.rows.map(r=>`<tr class="ci-table-row" data-id="${r.id}"><td class="mono">${escapeHtml(r.detected_at)}</td><td>${escapeHtml(r.project_path)} <span class="mono">!${escapeHtml(r.mr_iid)}</span></td><td><a href="${escapeHtml(r.pipeline_url||r.mr_url)}" target="_blank" rel="noreferrer">#${r.pipeline_id}</a></td><td>${(r.categories||[]).map(x=>`<span class="category-chip">${escapeHtml(x)}</span>`).join('')}</td><td>${escapeHtml(r.primary_reason||'未提取到明确原因')}</td><td>${escapeHtml(capabilityNames[r.capability]||'无法判断')}</td><td>${r.triage_count?'已进入修复':'未修复'}</td><td>${escapeHtml(notificationNames[r.notification_state]||r.notification_state)}</td></tr>`).join('')||'<tr><td colspan="8" class="muted">暂无失败 Pipeline</td></tr>';document.querySelectorAll('.ci-table-row').forEach(row=>row.addEventListener('click',()=>loadDetail(row.dataset.id)));renderPipelinePagination(data);}catch(error){if(sequence===loadSequence){document.getElementById('loadedAt').textContent='加载失败，请重试';if(latestData){renderRecurringPagination(latestData);renderDistribution(latestData);renderPipelinePagination(latestData);}}}}
async function loadDetail(id){const response=await fetch('/api/ci-failures/'+id);if(!response.ok)return;const data=await response.json();selectedFailureId=data.id;document.getElementById('detail').hidden=false;document.getElementById('detailTitle').textContent=`${data.project_path} !${data.mr_iid} · Pipeline #${data.pipeline_id}`;document.getElementById('detailMeta').textContent=`Commit ${data.pipeline_sha||'未知'} · ${data.failed_job_count} 个失败 Job`;document.getElementById('jobs').innerHTML=data.jobs.map(j=>`<div class="ci-job"><div class="ci-job-head"><strong>${escapeHtml(j.job_name)}</strong><span class="category-chip">${escapeHtml(j.family)}</span><span class="status-badge">${escapeHtml(capabilityNames[j.effective_capability]||'无法判断')}</span>${j.job_url?`<a href="${escapeHtml(j.job_url)}" target="_blank" rel="noreferrer">查看 Job</a>`:''}</div><div class="ci-job-reason">${escapeHtml(j.effective_reason||'未提取到明确原因')}${j.trace_line?` <span class="muted">日志第 ${j.trace_line} 行</span>`:''}</div>${j.note?`<div class="muted">人工备注：${escapeHtml(j.note)}</div>`:''}</div>`).join('');document.getElementById('annotationJob').innerHTML=data.jobs.map(j=>`<option value="${j.id}">${escapeHtml(j.job_name)}</option>`).join('');document.getElementById('detail').scrollIntoView({behavior:'smooth',block:'start'});}
document.getElementById('filters').addEventListener('submit',event=>{event.preventDefault();pipelinePage=1;recurringPage=1;projectDistributionPage=1;jobDistributionPage=1;loadData();});
document.getElementById('pipelinePrev').addEventListener('click',()=>{if(pipelinePage>1){pipelinePage-=1;loadData();}});
document.getElementById('pipelineNext').addEventListener('click',()=>{if(latestData&&pipelinePage<latestData.total_pages){pipelinePage+=1;loadData();}});
document.getElementById('recurringPrev').addEventListener('click',()=>{if(recurringPage>1){recurringPage-=1;loadData();}});
document.getElementById('recurringNext').addEventListener('click',()=>{if(latestData&&recurringPage<latestData.recurring_total_pages){recurringPage+=1;loadData();}});
document.getElementById('distributionPrev').addEventListener('click',()=>{if(activeDistribution==='project'&&projectDistributionPage>1)projectDistributionPage-=1;else if(activeDistribution==='job'&&jobDistributionPage>1)jobDistributionPage-=1;else return;loadData();});
document.getElementById('distributionNext').addEventListener('click',()=>{if(!latestData)return;if(activeDistribution==='project'&&projectDistributionPage<latestData.project_distribution_total_pages)projectDistributionPage+=1;else if(activeDistribution==='job'&&jobDistributionPage<latestData.job_distribution_total_pages)jobDistributionPage+=1;else return;loadData();});
document.getElementById('showProjects').addEventListener('click',event=>{event.preventDefault();activeDistribution='project';event.currentTarget.classList.add('active');event.currentTarget.setAttribute('aria-pressed','true');document.getElementById('showJobs').classList.remove('active');document.getElementById('showJobs').setAttribute('aria-pressed','false');if(latestData)renderDistribution(latestData);});
document.getElementById('showJobs').addEventListener('click',event=>{event.preventDefault();activeDistribution='job';event.currentTarget.classList.add('active');event.currentTarget.setAttribute('aria-pressed','true');document.getElementById('showProjects').classList.remove('active');document.getElementById('showProjects').setAttribute('aria-pressed','false');if(latestData)renderDistribution(latestData);});
document.getElementById('annotation').addEventListener('submit',async event=>{event.preventDefault();if(!selectedFailureId)return;const response=await fetch('/api/ci-failures/'+selectedFailureId+'/annotations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:Number(document.getElementById('annotationJob').value),reason:document.getElementById('annotationReason').value,capability:document.getElementById('annotationCapability').value,note:document.getElementById('annotationNote').value})});if(response.ok){await loadDetail(selectedFailureId);await loadData();}});loadData();
</script></body></html>"""
    return (
        template.replace("__BASE_CSS__", base_css)
        .replace("__OPERATIONS_CSS__", operations_css)
        .replace("__NAV__", nav_html)
        .replace("__JS_HELPERS__", js_helpers)
    )
