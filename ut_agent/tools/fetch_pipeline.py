"""
fetch_pipeline 工具 - 获取 GitLab 流水线覆盖率和失败 job 日志。

在 UT Agent push 代码后，等待 CI 流水线完成，然后拉取：
1. 整体覆盖率（如果有）
2. 指定 job 的失败日志（build_release_arm64 / x86_64_ut_coverage_check）

=== 设计说明 ===

流水线运行需要时间，本工具采用轮询策略：
- 初始等待 60s（流水线通常需要排队）
- 每 30s 查询一次流水线状态
- 最大等待 20 分钟（可配置）
- 只关注 push 后触发的最新流水线

只关注以下 job:
- build_release_arm64: ARM64 构建
- x86_64_ut_coverage_check: x86 单元测试覆盖率检查
"""
import asyncio
import logging
import time
from typing import Annotated, Optional

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from langgraph.types import interrupt

from pr_agent.triage.pipeline_coverage import parse_coverage_trace
from ut_agent.ci_diagnostics import extract_diagnostic_candidates, is_diagnostic_line
from ut_agent.pipeline_reconciliation import (
    observed_jobs_from_group_jobs,
    reconcile_pipeline_failures,
)
from ut_agent.repair_progress import build_root_cause_groups, extract_causal_lines
from ut_agent.tools.context import get_git_provider
from ut_agent.tools.pipeline_group import (
    PipelineGroup,
    TERMINAL_PIPELINE_STATUSES,
    required_pipeline_job_patterns,
    resolve_pipeline_group,
)

logger = logging.getLogger("ut_agent")

# 关注的 job 名称
TARGET_JOBS = ["build_release_arm64", "x86_64_ut_coverage_check"]

# 轮询参数
INITIAL_WAIT_SECONDS = 60       # 首次等待时间（让流水线开始运行）
POLL_INTERVAL_SECONDS = 30      # 轮询间隔
MAX_WAIT_SECONDS = 20 * 60      # 最大等待时间 20 分钟
JOB_LOG_TAIL_LINES = 80         # 尾部保留行数（总结区）
JOB_LOG_CONTEXT_LINES = 15      # 每个错误段落前后上下文行数

# 日志中的错误/失败模式（用于从全量日志提取关键段落）
# 注意：匹配时使用 re.IGNORECASE，所有 pattern 大小写不敏感。
# 方括号为可选（\[?\s* ... \s*\]?），有无括号均可匹配。
ERROR_PATTERNS = [
    r"\[\s*failed\s*\]",                   # [FAILED]
    r"failed\s+<<<",                       # colcon package failure
    r"\d+\s+packages?\s+failed",           # colcon failure summary
    r"failed.*test.*failed",               # CTest summary: N tests failed
    r"\berror\s*:",                        # error: / Error: / ERROR:（含 gcc/clang 小写 error:）
    r"undefined reference",                # 链接错误
    r"fatal\s+error",                      # fatal error（编译致命错误）
    r"❌.*test.*fail",                     # 自定义脚本失败标记
    r"package had test failures",          # colcon test failures
    r"assert.*fail",                       # assertion 失败（assert failed / ASSERT FAIL 等）
    r"Segmentation fault|SIGSEGV|SIGABRT", # 崩溃信号
    r"SSH 连接检测失败",                    # 依赖仓库 SSH 不可达
    r"无法访问仓库",                        # 依赖仓库克隆失败
]


def _diagnostic_text(job: dict | None) -> str:
    if not isinstance(job, dict):
        return ""
    values = [job.get("diagnostic"), job.get("log_context"), job.get("log_tail")]
    causal_lines = job.get("causal_lines")
    if isinstance(causal_lines, (list, tuple)):
        values.extend(causal_lines)
    elif causal_lines:
        values.append(causal_lines)
    primary = job.get("primary_diagnostic")
    if isinstance(primary, dict):
        values.extend((primary.get("signal"), primary.get("text")))
    candidates = job.get("diagnostic_candidates")
    for candidate in candidates if isinstance(candidates, (list, tuple)) else ():
        if isinstance(candidate, dict):
            values.extend((candidate.get("signal"), candidate.get("text")))
    return "\n".join(str(value) for value in values if value)[:20_000].lower()


def _classify_failed_job(job_name: str, job: dict | None = None) -> tuple[str, str]:
    name = job_name.lower()
    evidence = _diagnostic_text(job)
    combined = f"{name}\n{evidence}"
    if "format" in name or "clang-format" in evidence:
        return "format", "apply_format_report_tool"
    if "coverage" in combined:
        return "coverage", "fetch_coverage_report_tool"
    if "merge" in name and "check" in name:
        return "merge_check", "generate_code_tool"
    lint_markers = (
        "clang-tidy", "clang_tidy", "cpplint", "eslint", "flake8", "pylint", "ruff",
        "lint", "static-analysis", "static_analysis", "static analysis",
    )
    if any(marker in combined for marker in lint_markers):
        return "lint", "generate_code_tool"
    build_markers = (
        "build", "compile", "cmake", "ninja", "colcon", "undefined reference",
        "fatal error", "compilation terminated", "collect2:", "ld: error",
    )
    cpp_error = any(
        extension in evidence
        for extension in (".c:", ".cc:", ".cpp:", ".cxx:", ".h:", ".hpp:", ".hxx:")
    ) and "error:" in evidence
    if any(marker in combined for marker in build_markers) or cpp_error:
        return "build", "generate_code_tool"
    if "test" in name or any(marker in evidence for marker in ("assertionerror", "segmentation fault", "sigsegv")):
        return "test", "generate_code_tool"
    return "other", "generate_code_tool"


def _build_work_items(failed_jobs: list[dict]) -> list[dict]:
    groups = build_root_cause_groups(failed_jobs)
    group_by_job_id = {
        job_id: group
        for group in groups
        for job_id in group.job_ids
        if job_id is not None
    }
    work_items = []
    for job in failed_jobs:
        kind, required_tool = _classify_failed_job(job["name"], job)
        group = group_by_job_id.get(job.get("job_id"))
        work_item = {
            "job_id": job.get("job_id"),
            "pipeline_id": job.get("pipeline_id"),
            "job_name": job["name"],
            "kind": kind,
            "required_tool": required_tool,
            "root_cause_id": group.root_cause_id if group else "",
            "canonical_job_name": group.canonical_job_name if group else job["name"],
        }
        if isinstance(job.get("preflight_blocker"), dict):
            work_item["preflight_blocker"] = job["preflight_blocker"]
        work_items.append(work_item)
    return work_items


def _scope_failed_jobs(failed_jobs: list[dict], state: dict | None) -> list[dict]:
    """Expose only the repair categories selected by the user as actionable jobs."""
    selected = {str(category) for category in ((state or {}).get("selected_categories") or ()) if str(category)}
    if not selected:
        return list(failed_jobs)

    from pr_agent.triage.failure_categories import categorize_failed_job

    return [job for job in failed_jobs if categorize_failed_job(job).value in selected]


def _with_structured_diagnostics(job: dict, *, trace: str | None = None) -> dict:
    """Attach ordered failure observations without selecting a semantic root cause."""
    result = dict(job)
    log_tail = str(result.get("log_tail") or "")
    identity_key = f"{result.get('pipeline_id', '')}:{result.get('job_id', '')}"
    candidate_set = extract_diagnostic_candidates(
        trace if trace is not None else log_tail,
        identity_key=identity_key,
        limit=12,
    )
    from ut_agent.ci_diagnostics import credible_code_diagnostic, primary_diagnostic

    job_name = str(result.get("name") or result.get("job_name") or "")
    candidates = candidate_set.candidates
    is_clang_job = "clang" in job_name.lower()
    if is_clang_job:
        candidates = tuple(item for item in candidates if credible_code_diagnostic(item, job_name))
        result["evidence_mode"] = (
            "structured_evidence"
            if candidates
            else "raw_log_fallback"
            if str(trace if trace is not None else log_tail).strip()
            else "evidence_unavailable"
        )

    result["diagnostic_candidates"] = [item.to_dict() for item in candidates]
    result["diagnostic_candidate_count"] = len(candidates) if is_clang_job else candidate_set.total_matches
    result["diagnostic_candidates_truncated"] = candidate_set.truncated and bool(candidates)
    retained_identities = {item.diagnostic_identity for item in candidates if item.diagnostic_identity}
    result["diagnostic_identity_count"] = (
        len(retained_identities) if is_clang_job else candidate_set.identity_count
    )
    result["omitted_diagnostic_identity_count"] = (
        0 if is_clang_job else candidate_set.omitted_identity_count
    )
    primary = primary_diagnostic(candidates)
    ranked = ([primary] if primary is not None else []) + [
        item for item in candidates if item is not primary
    ]
    if primary is not None:
        result["primary_diagnostic"] = primary.to_dict()
    result["causal_lines"] = [item.text for item in ranked[:3]]
    if not result["causal_lines"] and result.get("evidence_mode") not in {"raw_log_fallback", "evidence_unavailable"}:
        result["causal_lines"] = extract_causal_lines(log_tail)
    from ut_agent.ci_repair_preflight import build_ci_repair_preflight

    preflight_blocker = build_ci_repair_preflight(
        str(result.get("name") or result.get("job_name") or "unknown"),
        [item.text for item in ranked] or result["causal_lines"],
    )
    if preflight_blocker is not None:
        result["preflight_blocker"] = preflight_blocker
    result["log_context"] = log_tail
    return result


def _pipeline_facts(result: dict) -> list[str]:
    """Keep bounded canonical CI evidence available after conversation compaction."""
    status = str(result.get("pipeline_status") or result.get("status") or "unknown")
    job_names = [
        str(job.get("name") or job.get("job_name") or "unknown")
        for job in (result.get("failed_jobs") or [])
        if isinstance(job, dict)
    ]
    facts = [f"流水线状态: {status}, 失败 jobs: {', '.join(job_names) or '无'}"]
    seen = set()
    for group in result.get("root_cause_groups") or []:
        if not isinstance(group, dict):
            continue
        root_cause_id = str(group.get("root_cause_id") or "")
        diagnostic = str(group.get("canonical_diagnostic") or "").strip()
        if not diagnostic or (root_cause_id, diagnostic) in seen:
            continue
        seen.add((root_cause_id, diagnostic))
        job_name = str(group.get("canonical_job_name") or "unknown")
        facts.append(f"流水线根因 {root_cause_id or 'unknown'} ({job_name}): {diagnostic}"[:1200])
        if len(facts) >= 10:
            break
    return facts


def _scope_pipeline_result(result: dict, state: dict | None) -> dict:
    selected = (state or {}).get("selected_categories") or ()
    if not selected:
        return result
    scoped = _scope_failed_jobs(list(result.get("failed_jobs") or ()), state)
    result = dict(result)
    result["failed_jobs"] = scoped
    result["work_items"] = _build_work_items(scoped)
    result["root_cause_groups"] = [group.to_dict() for group in build_root_cause_groups(scoped)]
    result["message"] = (
        f"Pipeline #{result.get('pipeline_id')} 状态: {result.get('pipeline_status')}; "
        f"所选范围内失败 jobs: {', '.join(job.get('name', '?') for job in scoped) or '无'}"
    )
    return result


def _resolve_group(project, pipeline, commit_sha: str) -> PipelineGroup:
    return resolve_pipeline_group(
        project,
        pipeline,
        required_job_patterns=required_pipeline_job_patterns(),
        exact_sha=commit_sha,
    )


def _group_fields(group: PipelineGroup) -> dict:
    return {
        "pipeline_id": group.validation_pipeline_id,
        "root_pipeline_id": group.root_pipeline_id,
        "validation_pipeline_id": group.validation_pipeline_id,
        "pipeline_ids": list(group.pipeline_ids),
        "pipeline_status": group.status,
        "coverage": group.coverage,
        "coverage_source": group.coverage_source,
        "coverage_status": group.coverage_status,
        "pipeline_resolution_source": group.resolution_source,
    }


def _exact_pipeline_sha(result: dict) -> str:
    requested_sha = str(result.get("requested_commit_sha") or "")
    matched_sha = str(result.get("matched_commit_sha") or "")
    return requested_sha if requested_sha and requested_sha == matched_sha else ""


def _latest_terminal_pipeline_with_different_sha(result: dict, state: dict | None) -> dict | None:
    """Return the latest comparable Pipeline fact without comparing one SHA to itself."""
    current_sha = _exact_pipeline_sha(result)
    if not current_sha or not state:
        return None

    try:
        from ut_agent.execution_ledger import build_execution_ledger

        pipelines = build_execution_ledger(state.get("messages", [])).pipelines
    except Exception:
        return None

    for previous in reversed(pipelines):
        status = str(previous.get("pipeline_status") or "").lower()
        previous_sha = _exact_pipeline_sha(previous)
        if status in TERMINAL_PIPELINE_STATUSES and previous_sha and previous_sha != current_sha:
            return previous
    return None


def _with_failure_reconciliation(result: dict, state: dict | None) -> dict:
    """Attach bounded cross-SHA failure transitions to a terminal Pipeline result."""
    result = dict(result)
    if (
        not _exact_pipeline_sha(result)
        or str(result.get("pipeline_status") or "").lower() not in TERMINAL_PIPELINE_STATUSES
    ):
        return result
    previous = _latest_terminal_pipeline_with_different_sha(result, state)
    result["failure_reconciliation"] = reconcile_pipeline_failures(previous, result)
    return result


def _attempt_id_for_sha(state: dict | None, sha: str) -> str:
    if not state:
        return ""
    try:
        from ut_agent.execution_policy import build_execution_ledger

        ledger = build_execution_ledger(state.get("messages", []))
        matching = [
            push for push in ledger.pushes
            if push.get("status") == "success" and str(push.get("commit_sha") or "") == sha
        ]
        if matching:
            return str(matching[-1].get("attempt_id") or "")
    except Exception:
        pass
    return ""


def _collect_all_pipelines(project, pipeline, depth=0, visited=None):
    """Compatibility wrapper returning the exact-SHA pipeline group."""
    del depth, visited
    return list(_resolve_group(project, pipeline, str(getattr(pipeline, "sha", "") or "")).pipelines)


def fetch_pipeline_feedback(
    commit_sha: str,
    project_id: Optional[str] = None,
) -> dict:
    """
    根据 commit SHA 获取对应流水线的覆盖率和失败 job 日志。

    参数:
        commit_sha: 触发流水线的 commit hash
        project_id: GitLab 项目路径/ID（不传则从 ToolContext 获取）

    返回:
        {
            "status": "success" | "timeout" | "error",
            "pipeline_id": int,
            "pipeline_status": str,
            "coverage": float | None,
            "failed_jobs": [
                {
                    "name": str,
                    "status": str,
                    "log_tail": str,  # 最后 N 行日志
                }
            ],
            "message": str,  # 人类可读总结
        }
    """
    git_provider = get_git_provider()
    if not git_provider:
        return {"status": "error", "message": "ERROR: git_provider 未初始化"}

    gl = git_provider.gl
    proj_id = project_id or git_provider.id_project

    try:
        project = gl.projects.get(proj_id)
    except Exception as e:
        return {"status": "error", "message": f"ERROR: 获取项目失败: {e}"}

    logger.info(f"[pipeline] 等待 commit {commit_sha[:8]} 的流水线...")
    print(f"[UT Agent] 等待流水线运行，commit={commit_sha[:8]}...")

    # 初始等待，让流水线有时间被创建
    time.sleep(INITIAL_WAIT_SECONDS)

    start_time = time.time()
    pipeline = None

    while (time.time() - start_time) < MAX_WAIT_SECONDS:
        # 查找与此 commit 关联的流水线
        pipelines = project.pipelines.list(sha=commit_sha, order_by="id", sort="desc", per_page=1)
        if pipelines:
            pipeline = pipelines[0]
            # 刷新状态
            pipeline = project.pipelines.get(pipeline.id)
            status = pipeline.status

            logger.info(f"[pipeline] Pipeline #{pipeline.id} 状态: {status}")
            print(f"[UT Agent] Pipeline #{pipeline.id} 状态: {status}")

            # 终态判断
            if status in ("success", "failed", "canceled", "skipped"):
                break

        # 未完成，继续等待
        time.sleep(POLL_INTERVAL_SECONDS)

    # 超时检查
    if pipeline is None:
        return {
            "status": "timeout",
            "requested_commit_sha": commit_sha,
            "matched_commit_sha": None,
            "pipeline_id": None,
            "pipeline_status": None,
            "coverage": None,
            "failed_jobs": [],
            "message": f"超时 ({MAX_WAIT_SECONDS}s): 未找到 commit {commit_sha[:8]} 对应的流水线",
        }

    # 刷新 pipeline 最终状态
    pipeline = project.pipelines.get(pipeline.id)
    matched_commit_sha = getattr(pipeline, "sha", None)
    if matched_commit_sha != commit_sha:
        return {
            "status": "error",
            "requested_commit_sha": commit_sha,
            "matched_commit_sha": matched_commit_sha,
            "pipeline_id": pipeline.id,
            "pipeline_status": pipeline.status,
            "coverage": None,
            "failed_jobs": [],
            "message": f"ERROR: Pipeline #{pipeline.id} commit SHA 与请求不匹配",
        }

    group = _resolve_group(project, pipeline, commit_sha)
    while not group.terminal and (time.time() - start_time) < MAX_WAIT_SECONDS:
        time.sleep(POLL_INTERVAL_SECONDS)
        root_pipeline = project.pipelines.get(group.root_pipeline_id)
        group = _resolve_group(project, root_pipeline, commit_sha)
    if not group.terminal or group.validation_pipeline is None:
        return {
            "status": "timeout",
            "requested_commit_sha": commit_sha,
            "matched_commit_sha": matched_commit_sha,
            **_group_fields(group),
            "failed_jobs": [],
            "message": (
                f"超时 ({MAX_WAIT_SECONDS}s): validation pipeline "
                f"{group.validation_pipeline_id or '尚未创建'} 仍在运行 ({group.status})"
            ),
        }

    pipeline = group.validation_pipeline

    # GitLab 的 parent-child pipeline 中，父 pipeline 只有 trigger bridges，
    # 真正的 job 在 downstream pipeline 里，必须递归展开。
    all_pipelines = [pipeline]
    if len(all_pipelines) > 1:
        logger.info(f"[pipeline] 共收集到 {len(all_pipelines)} 个相关 pipeline (含 downstream): "
                    f"{[p.id for p in all_pipelines]}")

    # 等待所有 pipeline 的 job 达到终态（pipeline 可能先于部分 job 进入终态）
    JOB_TERMINAL_STATES = {"success", "failed", "canceled", "skipped", "manual"}
    job_wait_start = time.time()
    JOB_WAIT_MAX = 300  # 最多再等 5 分钟让 job 结束
    while (time.time() - job_wait_start) < JOB_WAIT_MAX:
        all_terminal = True
        for p in all_pipelines:
            jobs_list = p.jobs.list(per_page=100, get_all=True)
            for job in jobs_list:
                if job.status not in JOB_TERMINAL_STATES:
                    all_terminal = False
                    logger.info(f"[pipeline] 等待 job 结束: {job.name} (status={job.status}, pipeline=#{p.id})")
                    break
            if not all_terminal:
                break
        if all_terminal:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    # Job 可能晚于 Pipeline 进入终态；重新解析最终 Job 和覆盖率，避免使用等待前的快照。
    root_pipeline = project.pipelines.get(group.root_pipeline_id)
    group = _resolve_group(project, root_pipeline, commit_sha)
    pipeline = group.validation_pipeline or pipeline
    coverage = group.coverage

    # 获取目标 job 的状态和失败日志（聚合所有相关 pipeline）
    failed_jobs = []
    coverage_threshold = None  # 从日志提取的阈值
    ut_coverage_job_id = None  # x86_64_ut_coverage_check 的 job id（无论 success/failed 都记录）
    all_jobs_info = []  # 记录所有 job 用于诊断
    jobs = []
    for p in all_pipelines:
        jobs.extend((p.id, job) for job in p.jobs.list(per_page=100, get_all=True))
    logger.info(f"[pipeline] 获取到 {len(jobs)} 个 job (跨 {len(all_pipelines)} 个 pipeline)")
    for job_pipeline_id, job in jobs:
        all_jobs_info.append(f"{job.name}({job.status})")
        # 模糊匹配目标 job（大小写不敏感子串匹配）
        matched_target = None
        job_name_lower = job.name.lower()
        for target in TARGET_JOBS:
            if target.lower() in job_name_lower:
                matched_target = target
                break

        if matched_target is None:
            # 非目标 job：仅标记名字与状态，不拉日志、不参与失败判定
            if job.status == "failed":
                failed_jobs.append(_with_structured_diagnostics({
                    "job_id": job.id,
                    "pipeline_id": job_pipeline_id,
                    "name": job.name,
                    "status": job.status,
                    "log_tail": "",
                    "is_target": False,
                }))
                logger.info(f"[pipeline] 非目标 job 失败 (仅标记，不参与判定): {job.name}")
            continue

        if job.status == "failed":
            # 获取 job 日志
            log_tail = _get_failed_job_diagnostics(project, job, JOB_LOG_TAIL_LINES)
            failed_jobs.append(_with_structured_diagnostics({
                "job_id": job.id,
                "pipeline_id": job_pipeline_id,
                "name": job.name,
                "status": job.status,
                "log_tail": log_tail,
                "is_target": True,
            }))
            logger.info(f"[pipeline] 目标 job 失败: {job.name} (id={job.id})")
            if "x86_64_ut_coverage_check" in job_name_lower:
                ut_coverage_job_id = job.id
        elif job.status == "success" and "x86_64_ut_coverage_check" in job_name_lower:
            ut_coverage_job_id = job.id
            # job 成功时从日志提取覆盖率数据
            cov_info = _extract_coverage_from_job(project, job.id)
            if cov_info:
                coverage_threshold = cov_info.get("threshold")
                logger.info(f"[pipeline] 覆盖率: {cov_info.get('coverage')}%, 阈值: {coverage_threshold}%")
        elif job.status != "success":
            # canceled / skipped / manual 只证明 Job 未成功执行，不是可修复失败证据。
            # 它们仍会通过 observed_jobs 保留，但不进入 failed_jobs 和 RepairPlan。
            logger.info(f"[pipeline] 目标 job 未成功执行: {job.name} (status={job.status}, id={job.id})")

    # 打印所有 job 信息用于诊断
    logger.info(f"[pipeline] 所有 job: {', '.join(all_jobs_info)}")
    if pipeline.status == "failed" and not failed_jobs:
        logger.warning(f"[pipeline] 流水线失败但未匹配到失败 job! 所有 job 状态: {all_jobs_info}")

    # 构建总结消息
    message = _build_summary(pipeline, coverage, failed_jobs)
    print(f"[UT Agent] Pipeline #{pipeline.id} 完成: {pipeline.status}, 覆盖率={coverage}%")

    root_cause_groups = build_root_cause_groups(failed_jobs)
    observed_jobs = observed_jobs_from_group_jobs(jobs)
    return {
        "status": "success",
        "requested_commit_sha": commit_sha,
        "matched_commit_sha": matched_commit_sha,
        **_group_fields(group),
        "coverage_threshold": coverage_threshold,
        "ut_coverage_job_id": ut_coverage_job_id,
        "failed_jobs": failed_jobs,
        "work_items": _build_work_items(failed_jobs),
        "root_cause_groups": [group.to_dict() for group in root_cause_groups],
        "observed_jobs": observed_jobs,
        "observed_jobs_truncated": len(jobs) > len(observed_jobs),
        "message": message,
    }


def _extract_coverage_from_job(project, job_id: int) -> Optional[dict]:
    """从 x86_64_ut_coverage_check job 日志中提取覆盖率和阈值。

    匹配格式:
        Coverage: 0.00%
        Threshold: 80.0%
        Total changed lines: 60
        Covered changed lines: 0
    """
    try:
        job = project.jobs.get(job_id)
        trace = job.trace()
        if isinstance(trace, bytes):
            trace = trace.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[pipeline] 获取 job {job_id} 日志失败: {e}")
        return None

    result = parse_coverage_trace(trace)
    return result if result else None


def _get_job_log_tail(project, job_id: int, tail_lines: int) -> str:
    """获取 job 日志：提取错误相关段落 + 尾部总结。

    策略：
    1. 全量获取日志
    2. 用正则匹配错误行，提取每个错误行前后 context 行
    3. 拼接去重的错误段落 + 日志尾部（总结区）
    4. 总长度上限 500 行，避免 token 爆炸
    """
    import re

    try:
        job = project.jobs.get(job_id)
        trace = job.trace()
        if isinstance(trace, bytes):
            trace = trace.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[pipeline] 获取 job {job_id} 日志失败: {e}")
        return f"[获取日志失败: {e}]"

    lines = trace.splitlines()
    total = len(lines)

    if total == 0:
        return "[日志为空]"

    # 找出所有匹配错误的行号
    error_line_indices = [index for index, line in enumerate(lines) if is_diagnostic_line(line)]

    # 提取错误段落（每个错误行 ± context）
    context = JOB_LOG_CONTEXT_LINES
    error_segments = []
    used_ranges = []  # 避免重叠

    for idx in error_line_indices:
        start = max(0, idx - context)
        end = min(total, idx + context + 1)

        # 如果和上一个范围重叠，合并
        if used_ranges and start <= used_ranges[-1][1]:
            used_ranges[-1] = (used_ranges[-1][0], end)
        else:
            used_ranges.append((start, end))

    for start, end in used_ranges:
        segment = lines[start:end]
        header = f"--- [日志 L{start+1}-L{end}] ---"
        error_segments.append(header + "\n" + "\n".join(segment))

    # 尾部总结（最后 tail_lines 行）
    tail_start = max(0, total - tail_lines)
    # 避免和已提取的错误段落重复
    if used_ranges and tail_start < used_ranges[-1][1]:
        tail_start = used_ranges[-1][1]
    tail_section = lines[tail_start:] if tail_start < total else []

    # 拼接结果
    parts = []
    if error_segments:
        parts.append(f"=== 错误段落 ({len(used_ranges)} 处) ===")
        parts.extend(error_segments)
    if tail_section:
        parts.append(f"\n=== 日志尾部 (L{tail_start+1}-L{total}) ===")
        parts.append("\n".join(tail_section))

    result = "\n".join(parts)

    # 上限 500 行
    result_lines = result.splitlines()
    if len(result_lines) > 500:
        result = "\n".join(result_lines[:500]) + "\n\n... [截断，共 {} 行]".format(len(result_lines))

    return result


def _get_failed_job_diagnostics(project, job, tail_lines: int) -> str:
    log_tail = _get_job_log_tail(project, job.id, tail_lines)
    if "code_format_check" not in job.name.lower():
        return log_tail

    try:
        report = project.jobs.get(job.id).artifact("code-format-report.txt")
        if isinstance(report, bytes):
            report = report.decode("utf-8", errors="replace")
        return f"=== code-format-report.txt ===\n{report}\n\n{log_tail}"
    except Exception as e:
        logger.warning(f"[pipeline] 获取 job {job.id} 格式检查报告失败: {e}")
        return log_tail


def _build_summary(pipeline, coverage: Optional[float], failed_jobs: list[dict]) -> str:
    """构建人类可读的流水线反馈摘要。"""
    parts = [f"Pipeline #{pipeline.id} 状态: {pipeline.status}"]

    if coverage is not None:
        parts.append(f"覆盖率: {coverage}%")
    else:
        parts.append("覆盖率: 无数据")

    if failed_jobs:
        target_failed = [fj for fj in failed_jobs if fj.get("is_target", True)]
        non_target_failed = [fj for fj in failed_jobs if not fj.get("is_target", True)]
        if target_failed:
            parts.append(f"目标 job 失败 ({len(target_failed)}):")
            for fj in target_failed:
                parts.append(f"  - {fj['name']}: {fj['status']}")
        if non_target_failed:
            parts.append(f"非目标 job 失败 ({len(non_target_failed)}, 仅标记不参与判定):")
            for fj in non_target_failed:
                parts.append(f"  - {fj['name']}: {fj['status']}")
    elif pipeline.status == "failed":
        parts.append("流水线失败，但未匹配到具体失败 job（可能 job 名称不在监控列表中）")
    else:
        parts.append("目标 job 全部通过")

    return "\n".join(parts)


async def async_fetch_pipeline_feedback(
    commit_sha: str,
    project_id: Optional[str] = None,
) -> dict:
    """异步版本 - 在 asyncio 事件循环中运行轮询（避免阻塞）。"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_pipeline_feedback, commit_sha, project_id)


@tool
def fetch_pipeline_logs_tool(
    pipeline_id: int = None,
    commit_sha: str = None,
    job_name: str = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """查询 GitLab 流水线状态和失败 job 日志摘要（非阻塞）。

    查一次当前状态，不等待完成。如果流水线还在运行，返回 "running" 状态。
    用于快速了解流水线失败原因，决定下一步行动。

    参数:
        pipeline_id: 流水线 ID（可选，与 commit_sha 二选一）
        commit_sha: commit hash（可选）
        job_name: 精确查询一个失败 job 的名称（可选）

    返回: 失败 job 的名称、阶段和日志摘要（每个 job 最多 30 行关键错误）。
    """
    import json

    git_provider = get_git_provider()
    if not git_provider:
        return json.dumps({"status": "error", "message": "ERROR: git_provider 未初始化"}, ensure_ascii=False)

    gl = git_provider.gl
    proj_id = state.get("project_id") or git_provider.id_project if state else git_provider.id_project

    try:
        project = gl.projects.get(proj_id)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"ERROR: 获取项目失败: {e}"}, ensure_ascii=False)

    # 查找 pipeline
    pipeline = None
    try:
        if pipeline_id:
            pipeline = project.pipelines.get(pipeline_id)
        elif commit_sha:
            pipelines = project.pipelines.list(sha=commit_sha, order_by="id", sort="desc", per_page=1)
            if pipelines:
                pipeline = project.pipelines.get(pipelines[0].id)
        elif state and state.get("pipeline_id"):
            pipeline = project.pipelines.get(state["pipeline_id"])
    except Exception as e:
        return json.dumps({"status": "error", "message": f"ERROR: 获取流水线失败: {e}"}, ensure_ascii=False)

    if not pipeline:
        return json.dumps({"status": "error", "message": "ERROR: 未找到流水线"}, ensure_ascii=False)

    requested_commit_sha = commit_sha or str(getattr(pipeline, "sha", "") or "")
    matched_commit_sha = getattr(pipeline, "sha", None)
    if requested_commit_sha and matched_commit_sha != requested_commit_sha:
        return json.dumps({
            "status": "error",
            "requested_commit_sha": requested_commit_sha,
            "matched_commit_sha": matched_commit_sha,
            "pipeline_id": pipeline.id,
            "pipeline_status": pipeline.status,
            "message": f"ERROR: Pipeline #{pipeline.id} commit SHA 与请求不匹配",
        }, ensure_ascii=False)

    group = _resolve_group(project, pipeline, requested_commit_sha)
    if not group.terminal or group.validation_pipeline is None:
        return json.dumps({
            "status": "running",
            "requested_commit_sha": requested_commit_sha,
            "matched_commit_sha": matched_commit_sha,
            **_group_fields(group),
            "message": (
                f"Validation pipeline {group.validation_pipeline_id or '尚未创建'} "
                f"正在运行 ({group.status})"
            ),
        }, ensure_ascii=False)

    pipeline = group.validation_pipeline

    # 收集失败 job 的日志摘要
    failed_jobs = []
    try:
        all_failed_jobs = []
        for job_pipeline_id, job in group.jobs:
            if job.status == "failed":
                diagnostic = (
                    _get_failed_job_diagnostics(project, job, 30)
                    if job_name is None or job.name == job_name
                    else ""
                )
                all_failed_jobs.append(_with_structured_diagnostics({
                    "job_id": job.id,
                    "pipeline_id": job_pipeline_id,
                    "name": job.name,
                    "status": job.status,
                    "log_tail": diagnostic,
                }))
                if job_name is None or job.name == job_name:
                    failed_jobs.append(dict(all_failed_jobs[-1]))
    except Exception as e:
        logger.warning(f"[fetch_pipeline_logs] 获取 job 列表失败: {e}")
        all_failed_jobs = []

    scoped_jobs = _scope_failed_jobs(all_failed_jobs, state)
    failed_jobs = [job for job in scoped_jobs if job_name is None or job.get("name") == job_name]
    observed_jobs = observed_jobs_from_group_jobs(group.jobs)
    result = {
        "status": "success",
        "requested_commit_sha": requested_commit_sha,
        "matched_commit_sha": matched_commit_sha,
        **_group_fields(group),
        "failed_jobs": failed_jobs,
        "work_items": _build_work_items(scoped_jobs),
        "root_cause_groups": [group.to_dict() for group in build_root_cause_groups(scoped_jobs)],
        "observed_jobs": observed_jobs,
        "observed_jobs_truncated": len(group.jobs) > len(observed_jobs),
        "message": _build_summary(pipeline, group.coverage, failed_jobs),
    }
    result = _with_failure_reconciliation(result, state)
    result["_facts"] = _pipeline_facts(result)
    return json.dumps(result, ensure_ascii=False)


@tool
def wait_pipeline_tool(
    commit_sha: str = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """等待 CI 流水线完成并获取覆盖率和失败 job 日志。

    queue 模式持久化等待点并由 terminal webhook 恢复；inline 模式保留轮询。
    返回结构化的流水线结果（覆盖率、失败 job 名称和完整错误日志）。

    参数:
        commit_sha: commit hash（可选，不传则从 state 获取）

    返回: JSON 格式的流水线反馈结果。
    """
    import json

    sha = commit_sha or (state.get("commit_sha", "") if state else "")
    if not sha:
        return json.dumps({"status": "error", "message": "ERROR: 无 commit_sha"}, ensure_ascii=False)

    from pr_agent.distributed.runtime import get_execution_runtime

    runtime = get_execution_runtime()
    if runtime is not None and runtime.mode == "queue":
        git_provider = get_git_provider()
        project_id = str(
            (state.get("project_id") if state else "")
            or (getattr(git_provider, "id_project", "") if git_provider else "")
        )
        attempt_id = _attempt_id_for_sha(state, sha)
        validation_pipeline_id = None
        while True:
            cached_event = runtime.register_pipeline_wait_sync(
                project_id=project_id,
                commit_sha=sha,
                attempt_id=attempt_id,
                pipeline_id=validation_pipeline_id,
            )
            event = cached_event.to_dict() if cached_event is not None else interrupt(
                {
                    "kind": "pipeline",
                    "project_id": project_id,
                    "commit_sha": sha,
                    "attempt_id": attempt_id,
                    "pipeline_id": validation_pipeline_id,
                }
            )
            event_pipeline_id = int(event["pipeline_id"])
            identity_mismatch = (
                str(event.get("project_id") or "") != project_id
                or str(event.get("sha") or "") != sha
                or (validation_pipeline_id is not None and event_pipeline_id != validation_pipeline_id)
            )
            if identity_mismatch:
                return json.dumps(
                    {
                        "status": "error",
                        "message": "ERROR: Pipeline resume identity mismatch",
                        "requested_commit_sha": sha,
                        "matched_commit_sha": event.get("sha"),
                    },
                    ensure_ascii=False,
                )
            encoded = fetch_pipeline_logs_tool.func(
                pipeline_id=event_pipeline_id,
                commit_sha=sha,
                state=state,
            )
            result = json.loads(encoded)
            result["attempt_id"] = attempt_id
            discovered_validation_id = result.get("validation_pipeline_id")
            if result.get("status") != "running" or not discovered_validation_id:
                return json.dumps(result, ensure_ascii=False)
            discovered_validation_id = int(discovered_validation_id)
            if discovered_validation_id == validation_pipeline_id:
                return json.dumps(result, ensure_ascii=False)
            validation_pipeline_id = discovered_validation_id

    result = _scope_pipeline_result(fetch_pipeline_feedback(sha), state)
    result["attempt_id"] = _attempt_id_for_sha(state, sha)
    matched_sha = result.get("matched_commit_sha") or result.get("pipeline_sha")
    result["requested_commit_sha"] = sha
    result["matched_commit_sha"] = matched_sha
    if result.get("pipeline_id") and matched_sha != sha:
        result["status"] = "error"
        result["message"] = (
            f"ERROR: Pipeline #{result['pipeline_id']} commit SHA 与请求不匹配: "
            f"requested={sha}, matched={matched_sha}"
        )
    result = _with_failure_reconciliation(result, state)
    result["_facts"] = _pipeline_facts(result)
    return json.dumps(result, ensure_ascii=False)
