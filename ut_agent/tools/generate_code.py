"""generate_code 工具 - 委托 Hermes CLI 在仓库中生成或修改代码。

这是两层 Agent 架构的核心：
- 外层 Agent（litellm + Claude）负责推理、规划、构造 task_description
- 内层 Agent（Hermes CLI）负责编码执行（读文件、搜索、编辑）

工具内部保留：
1. prompt 构造（通用编码规范 + 语言专项规范）
2. Hermes CLI 调用（含超时诊断）
3. 安全网检查（CMakeLists.txt 引用验证）
4. Git working tree 检查（找出新增/修改的文件）

"""
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time as _time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Callable, Literal

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from pr_agent.log import get_logger
from ut_agent.blocker_evidence import parse_blocker_record
from ut_agent.config import API_KEY, BASE_URL, HERMES_API_MODE, MODEL, MODEL_CANDIDATES
from ut_agent.model_failover import ModelFailure, build_model_health_store, classify_model_failure, ordered_candidates
from ut_agent.prompt import load_prompt
from ut_agent.tools.context import get_repo_dir

logger = get_logger()

CODING_AGENT_TIMEOUT = 600  # 10 分钟超时
HERMES_RETRY_DELAY_SECONDS = 2
PIPELINE_OPERATIONS = {"investigate", "repair", "verify_blocker", "coverage_enhancement"}
MUTATING_PIPELINE_OPERATIONS = {"repair", "coverage_enhancement"}
_MODEL_HEALTH_STORE = build_model_health_store()
_DIRECT_FAILOVER_CODES = {"quota_exceeded", "model_unavailable"}
_OWNER_PROGRESS_RULES = (
    (re.compile(r"\b(search|grep|find|rg)\b", re.IGNORECASE), "正在检索与错误相关的代码"),
    (re.compile(r"\b(read|cat|sed|open)\b", re.IGNORECASE), "正在读取相关源码和配置"),
    (re.compile(r"\b(edit|write|patch|replace|apply)\b", re.IGNORECASE), "正在应用代码修复"),
)


class HermesFailureKind(str, Enum):
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    EXECUTION_BUDGET_EXHAUSTED = "execution_budget_exhausted"
    SEARCH_LOOP = "search_loop"
    INVESTIGATION_NO_RESULT = "investigation_no_result"
    REPAIR_NO_CHANGES = "repair_no_changes"
    PARTIAL_CHANGES = "partial_changes"
    REQUEST_INVALID = "request_invalid"


@dataclass(frozen=True)
class HermesRunOutcome:
    changed_files: tuple[str, ...]
    diagnostic: str
    failure_kind: HermesFailureKind | None = None
    provider_failure: ModelFailure | None = None


def _owner_progress_summary(line: str) -> str:
    """Map untrusted Hermes output to a fixed owner-safe progress phrase."""
    return next((summary for pattern, summary in _OWNER_PROGRESS_RULES if pattern.search(line)), "")


def _provider_error_outcome(error: BaseException | str, *, diagnostic: str = "") -> HermesRunOutcome:
    """Classify direct relay/API evidence without treating local execution deadlines as provider failures."""
    failure = classify_model_failure(error)
    if failure.switchable:
        kind = (
            HermesFailureKind.PROVIDER_TIMEOUT
            if failure.code in {"connection_error", "rate_limited"}
            else HermesFailureKind.PROVIDER_UNAVAILABLE
        )
        return HermesRunOutcome((), diagnostic or str(error), kind, failure)
    return HermesRunOutcome((), diagnostic or str(error), HermesFailureKind.REQUEST_INVALID)


def _hermes_timeout_outcome(
    stdout_lines: list[str],
    stderr_lines: list[str],
    elapsed: float,
) -> HermesRunOutcome:
    """Classify the outer Hermes process deadline from observed execution behavior."""
    diagnostic = _redact_hermes_text("\n".join(stdout_lines[-20:]))
    api_error = _extract_hermes_api_error(stdout_lines, stderr_lines)
    if api_error:
        return _provider_error_outcome(api_error, diagnostic=diagnostic)
    search_count = sum(1 for line in stdout_lines if "search" in line.lower() or "read" in line.lower())
    kind = HermesFailureKind.SEARCH_LOOP if search_count > 20 else HermesFailureKind.EXECUTION_BUDGET_EXHAUSTED
    return HermesRunOutcome((), diagnostic, kind)


def _redact_hermes_text(text: str) -> str:
    """隐藏 Hermes 输出中意外回显的 API key。"""
    return text.replace(API_KEY, "[REDACTED]") if API_KEY else text


def _extract_hermes_api_error(stdout_lines: list[str], stderr_lines: list[str]) -> str | None:
    """提取 Hermes 自身报告的 API 失败，避免把普通诊断文本误判为调用失败。"""
    from ut_agent.hermes_failure import extract_hermes_control_failure

    error = extract_hermes_control_failure(stdout_lines, stderr_lines)
    return _redact_hermes_text(error) if error else None


def _is_transient_hermes_error(error: str) -> bool:
    text = error.lower()
    if re.search(r"\b(?:429|5\d{2})\b", text):
        return True
    return any(marker in text for marker in (
        "超时",
        "timeout",
        "timed out",
        "connection",
        "temporarily unavailable",
        "rate limit",
        "too many requests",
    ))


def _model_call_owner(state: dict | None) -> str:
    try:
        from pr_agent.distributed.runtime import get_execution_runtime

        runtime = get_execution_runtime()
        if runtime is not None and runtime.task_id:
            return runtime.task_id
    except Exception:
        pass
    mr_id = state.get("mr_id", 0) if state else 0
    return f"hermes:{os.getpid()}:{threading.get_ident()}:{mr_id}"


def _pipeline_evidence_for(
    state: dict | None,
    root_cause_id: str,
    job_name: str,
) -> tuple[str, str]:
    """Resolve exact CI evidence without relying on the outer model to copy it."""
    if not state:
        return "", ""

    try:
        from ut_agent.execution_policy import build_execution_ledger

        pipelines = build_execution_ledger(state.get("messages", [])).pipelines
    except Exception as exc:
        logger.warning(f"[generate_code] 无法解析流水线证据: {exc}")
        return "", ""

    for pipeline in reversed(pipelines):
        if pipeline.get("pipeline_status") != "failed":
            continue

        groups = [group for group in pipeline.get("root_cause_groups") or [] if isinstance(group, dict)]
        group = next((item for item in groups if str(item.get("root_cause_id") or "") == root_cause_id), None)
        if group is None:
            group = next((
                item for item in groups
                if job_name == str(item.get("canonical_job_name") or "")
                or job_name in {str(name) for name in item.get("job_names") or []}
            ), None)

        resolved_root_cause_id = str((group or {}).get("root_cause_id") or root_cause_id)
        group_job_ids = {job_id for job_id in (group or {}).get("job_ids") or [] if job_id is not None}
        group_job_names = {str(name) for name in (group or {}).get("job_names") or []}
        matching_jobs = [
            item for item in pipeline.get("failed_jobs") or []
            if isinstance(item, dict) and (
                (group_job_ids and item.get("job_id") in group_job_ids)
                or (group_job_names and str(item.get("name") or "") in group_job_names)
                or (not group and str(item.get("name") or "") == job_name)
            )
        ]

        from pr_agent.triage.failure_explanations import sanitize_failure_text

        diagnostic_candidates = []
        seen_candidate_ids = set()
        diagnostic_candidate_count = 0
        diagnostic_candidates_truncated = False
        legacy_causal_lines = []
        canonical = str((group or {}).get("canonical_diagnostic") or "").strip()
        for item in matching_jobs:
            item_candidates = []
            for candidate in item.get("diagnostic_candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                candidate_id = sanitize_failure_text(candidate.get("candidate_id"), 40)
                text = sanitize_failure_text(candidate.get("text"), 1000)
                signal = sanitize_failure_text(candidate.get("signal"), 24)
                try:
                    line_number = max(0, int(candidate.get("line_number") or 0))
                except (TypeError, ValueError):
                    line_number = 0
                if not candidate_id or not text or candidate_id in seen_candidate_ids:
                    continue
                seen_candidate_ids.add(candidate_id)
                item_candidates.append({
                    "candidate_id": candidate_id,
                    "line_number": line_number,
                    "signal": signal or "observation",
                    "text": text,
                })
            diagnostic_candidates.extend(item_candidates)
            try:
                item_candidate_count = max(0, int(item.get("diagnostic_candidate_count") or 0))
            except (TypeError, ValueError):
                item_candidate_count = 0
            diagnostic_candidate_count += max(len(item_candidates), item_candidate_count)
            diagnostic_candidates_truncated = (
                diagnostic_candidates_truncated or bool(item.get("diagnostic_candidates_truncated"))
            )
            legacy_causal_lines.extend(
                str(line).strip()
                for line in item.get("causal_lines") or []
                if str(line).strip()
            )
        diagnostic_candidates = diagnostic_candidates[:12]
        if diagnostic_candidate_count > len(diagnostic_candidates):
            diagnostic_candidates_truncated = True
        causal_lines = [] if diagnostic_candidates else ([canonical] if canonical else []) + legacy_causal_lines
        causal_lines = [sanitize_failure_text(line, 1000) for line in dict.fromkeys(causal_lines)]
        causal_lines = [line for line in causal_lines if line][:5]
        if not causal_lines:
            causal_lines = [item["text"] for item in diagnostic_candidates[:5]]
        if not causal_lines:
            continue

        pipeline_id = pipeline.get("pipeline_id") or pipeline.get("validation_pipeline_id") or "unknown"
        failed_names = sorted({str(item.get("name") or "unknown") for item in matching_jobs})
        if diagnostic_candidates:
            evidence_lines = "\n".join(
                f"- [{item['candidate_id']}] L{item['line_number']} {item['signal']}: {item['text']}"
                for item in diagnostic_candidates
            )
        else:
            evidence_lines = "\n".join(
                f"- [legacy-{index}] L0 observation: {line}" for index, line in enumerate(causal_lines, 1)
            )
        evidence = (
            "## 当前 CI 失败证据候选（系统自动注入）\n"
            "这些候选不是已确认根因；必须结合候选顺序、当前源码和必要的脱敏原始日志独立判断。\n"
            f"root_cause_id（仅调度标识）: {resolved_root_cause_id or 'unknown'}\n"
            f"Pipeline: #{pipeline_id}\n"
            f"失败 jobs: {', '.join(failed_names) or job_name}\n"
            f"候选总数: {max(diagnostic_candidate_count, len(causal_lines))}; 已截断: "
            f"{'是' if diagnostic_candidates_truncated else '否'}\n"
            "按原始日志顺序的候选：\n"
            f"{evidence_lines}\n\n"
            "执行边界：\n"
            "- 不要搜索 CI 日志文件或流水线缓存；先调查上述候选，信息不足、候选冲突或列表已截断时，"
            "只能读取本任务的脱敏原始日志。\n"
            "- 只检查错误指向的源码、当前 MR diff，以及与该符号直接相关的接口或依赖定义。\n"
            "- 不得用宽泛的全仓库或全文件系统搜索替代针对上述错误的调查或修复。"
        )
        from ut_agent.dependency_evidence import dependency_evidence_from_messages

        dependency_snapshots = dependency_evidence_from_messages(
            state.get("messages", []),
            resolved_root_cause_id,
        )
        if dependency_snapshots:
            interface_blocks = []
            provider_blocks = []
            for snapshot in dependency_snapshots:
                if snapshot.get("evidence_kind") == "discovered_provider":
                    provider_blocks.append(
                        f"missing_package: {snapshot['package_name']}\n"
                        f"module: {snapshot['module']}\n"
                        f"verified_project: {snapshot['project_path']}\n"
                        f"repository_url: {snapshot['repository_url']}\n"
                        f"default_branch: {snapshot['declared_branch']}\n"
                        f"resolved_sha: {snapshot['resolved_sha']}\n"
                        f"package_manifest: {snapshot['file_path']}\n"
                        f"dependency_manifest: {snapshot['dependency_manifest_path']}\n"
                        f"package_xml_sha256: {snapshot['content_sha256']}"
                    )
                else:
                    interface_blocks.append(
                        f"project: {snapshot['project_path']}\n"
                        f"declared_branch: {snapshot['declared_branch']}\n"
                        f"resolved_sha: {snapshot['resolved_sha']}\n"
                        f"path: {snapshot['file_path']}\n"
                        f"content_sha256: {snapshot['content_sha256']}\n"
                        f"content:\n{snapshot['content']}"
                    )
            if interface_blocks:
                evidence += (
                    "\n\n## 当前声明依赖接口（只读快照）\n"
                    + "\n\n".join(interface_blocks)
                    + "\n\n字段约束：只能使用上述当前接口 content 中真实声明的字段；"
                    "未声明的字段不得猜测或替换。"
                )
            if provider_blocks:
                evidence += (
                    "\n\n## 缺失包的唯一提供仓库（只读核验结果）\n"
                    + "\n\n".join(provider_blocks)
                    + "\n\n修改边界：系统已只读核验上述缺失包、仓库、默认分支和 package.xml 的唯一对应关系。"
                    "如果补充依赖清单是解决当前错误的最小修改，可只在给出的 dependency_manifest 中增加该 "
                    "repository_url 和 default_branch；不得替换仓库、猜测分支或添加其他依赖。"
                )
        return resolved_root_cause_id, evidence

    return "", ""


_BLOCKED_PIPELINE_COMMANDS = (
    "apt",
    "apt-cache",
    "apt-get",
    "apk",
    "brew",
    "dnf",
    "dpkg",
    "pacman",
    "pip",
    "pip3",
    "sudo",
    "yum",
)


@contextmanager
def _hermes_runtime(model: str = MODEL, *, guard_system_mutations: bool = False):
    """用当前 UT 模型配置创建隔离的 Hermes 0.19 运行环境。"""
    model = model.split("/", 1)[-1]
    base_url = BASE_URL.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"

    with tempfile.TemporaryDirectory(prefix="pr-agent-hermes-") as home:
        hermes_dir = Path(home) / ".hermes"
        hermes_dir.mkdir()
        config = "\n".join([
            "model:",
            f"  default: {json.dumps(model)}",
            "  provider: relay",
            "custom_providers:",
            "  - name: relay",
            f"    base_url: {json.dumps(base_url)}",
            "    key_env: HERMES_RELAY_API_KEY",
            f"    api_mode: {HERMES_API_MODE}",
            "",
        ])
        (hermes_dir / "config.yaml").write_text(config, encoding="utf-8")
        env = os.environ.copy()
        if guard_system_mutations:
            command_guard = Path(home) / "blocked-system-commands"
            command_guard.mkdir()
            guard_script = "#!/bin/sh\necho 'system package mutation is disabled for CI repair' >&2\nexit 126\n"
            for command in _BLOCKED_PIPELINE_COMMANDS:
                command_path = command_guard / command
                command_path.write_text(guard_script, encoding="utf-8")
                command_path.chmod(0o755)
            env["PATH"] = f"{command_guard}{os.pathsep}{env.get('PATH', '')}"
        env.update({"HOME": home, "HERMES_RELAY_API_KEY": API_KEY})
        yield env


@contextmanager
def _hide_git_metadata(repo_dir: str, enabled: bool):
    """Temporarily remove root Git metadata from a Hermes pipeline task.

    A prompt prohibition is not a security boundary: a coding model can still run
    ``git log`` or ``git show`` and recover a reverted fix from the current commit
    message.  The outer Agent retains Git access before and after this context for
    change detection, commit, and push; Hermes receives only the current files.
    """
    git_path = Path(repo_dir) / ".git"
    if not enabled or not git_path.exists():
        yield
        return

    with tempfile.TemporaryDirectory(prefix="pr-agent-hidden-git-") as hidden_root:
        hidden_path = Path(hidden_root) / "metadata"
        shutil.move(str(git_path), str(hidden_path))
        try:
            yield
        finally:
            if git_path.exists():
                unexpected_path = Path(hidden_root) / "unexpected-metadata"
                shutil.move(str(git_path), str(unexpected_path))
                logger.warning("[generate_code] Hermes created unexpected .git metadata; discarded it")
            shutil.move(str(hidden_path), str(git_path))


@tool
def generate_code_tool(
    job_name: str,
    task_description: str,
    operation: Literal["investigate", "repair", "verify_blocker", "coverage_enhancement"] | None = None,
    root_cause_id: str = "",
    languages: list[str] = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """在仓库中生成或修改代码（单元测试、修复代码等）。

    通过 Hermes CLI 在克隆的仓库中执行编码任务。Hermes 能自主读取仓库文件、
    搜索代码、编辑文件。工具内部包含安全网检查（CMakeLists.txt 引用验证）。

    参数:
        job_name: 当前处理的失败 job 精确名称。一次调用只能处理一个 job。
        task_description: 编码任务描述。要足够具体，让 Hermes 知道改什么文件、
            怎么改。例如:
            - "为 src/modules/acc/foo.cpp 生成 GTest 单元测试，覆盖所有公开函数"
            - "修复 src/bar.cpp 第 42 行的编译错误：缺少 #include <common/types.h>"
            - "为 src/foo.cpp L42-50 的错误处理分支补充测试用例"
        operation: 流水线失败场景的必填操作：investigate（只读调查）、repair（实际修复）
            或 verify_blocker（验证仓库外阻塞）。非流水线单测生成场景可省略。
        root_cause_id: fetch_pipeline_logs_tool 返回的根因组 ID；同一根因的多个 job 共用该 ID。
        languages: 涉及的语言（如 ["cpp"] 或 ["python"]），影响 prompt 模板选择

    返回: 生成的文件路径列表，或错误描述。
    """
    mr_id = state.get("mr_id", 0) if state else 0
    repo_dir = get_repo_dir(mr_id)
    if not repo_dir:
        return json.dumps({
            "status": "error",
            "operation": operation,
            "job_name": job_name,
            "changed_files": [],
            "diagnostic": "",
            "message": f"MR !{mr_id} 仓库未克隆，请先调用 clone_source_branch_tool。",
        }, ensure_ascii=False)

    from ut_agent.workspace import validate_state_workspace

    workspace_validation = validate_state_workspace(state, repo_dir, allow_dirty=True)
    if not workspace_validation.ok:
        return json.dumps({
            "status": "blocked",
            "operation": operation,
            "job_name": job_name,
            "changed_files": [],
            "diagnostic": "",
            "error_code": workspace_validation.error_code,
            "retryable": False,
            "message": workspace_validation.message,
        }, ensure_ascii=False)

    iteration = state.get("iteration", 1) if state else 1
    trigger_type = state.get("trigger_type", "") if state else ""
    resolved_root_cause_id, pipeline_evidence = _pipeline_evidence_for(state, root_cause_id, job_name)
    effective_root_cause_id = resolved_root_cause_id or root_cause_id
    dependency_sources = []
    dependency_snapshots = []
    if trigger_type == "pipeline_failed":
        from ut_agent.dependency_evidence import dependency_evidence_from_messages

        dependency_snapshots = dependency_evidence_from_messages(
            state.get("messages", []), effective_root_cause_id
        )
        dependency_sources = [
            item["content"]
            for item in dependency_snapshots
            if item.get("content")
        ]
    hermes_started: float | None = None
    selected_model = ""
    attempted_models: list[str] = []
    model_failover_count = 0
    model_failure_code = ""
    failure_kind = ""

    def build_result(*args, **kwargs) -> str:
        from ut_agent.repair_progress import root_cause_id_for

        duration_ms = int((_time.monotonic() - hermes_started) * 1000) if hermes_started is not None else 0
        kwargs["root_cause_id"] = effective_root_cause_id or root_cause_id_for(
            job_name,
            str(args[5] if len(args) > 5 else ""),
        )
        kwargs["hermes_duration_ms"] = duration_ms
        kwargs["model"] = selected_model
        kwargs["attempted_models"] = attempted_models
        kwargs["model_failover_count"] = model_failover_count
        kwargs["model_failure_code"] = model_failure_code
        kwargs["failure_kind"] = failure_kind
        return _generate_result(*args, **kwargs)

    if trigger_type == "pipeline_failed":
        if operation not in PIPELINE_OPERATIONS:
            return build_result(
                "incomplete",
                operation,
                job_name,
                repo_dir,
                [],
                "",
                "流水线修复必须指定 operation=investigate、repair 或 verify_blocker。",
                validation_error="缺少有效的流水线修复 operation",
            )
        prompt_name = {
            "investigate": "generate_investigate_system",
            "repair": "generate_fix_system",
            "verify_blocker": "verify_blocker_system",
            "coverage_enhancement": "generate_patch_system",
        }[operation]
        full_system = load_prompt(prompt_name)
        user_template = load_prompt(
            "generate_patch_user" if operation == "coverage_enhancement" else "generate_fix_user"
        )
        result_operation = operation
    else:
        # 构造 prompt：通用编码规范 + 语言专项规范 + task_description
        full_system = load_prompt("generate_patch_system")
        lang_set = set(languages or ["cpp", "python"])
        lang_sections = []
        if lang_set & {"cpp", "c", "cc", "cxx", "h", "hpp"}:
            lang_sections.append(load_prompt("generate_patch_cpp"))
        if lang_set & {"python", "py"}:
            lang_sections.append(load_prompt("generate_patch_python"))
        if lang_sections:
            full_system += "\n\n" + "\n\n".join(lang_sections)

        user_template = load_prompt("generate_patch_user")
        result_operation = "generate"

    user_prompt = user_template.format(
        task_description=(
            f"{task_description}\n\n{pipeline_evidence}"
            if trigger_type == "pipeline_failed" and pipeline_evidence
            else task_description
        ),
        repo_dir=repo_dir,
        mr_id=mr_id,
        iteration=iteration,
    )

    full_prompt = f"{full_system}\n\n---\n\n失败 job: {job_name}\n\n{user_prompt}"

    # 仅在工作区没有产生部分修改时重试或切换模型。
    initial_changes = set(_get_changed_files(repo_dir))
    new_files = []
    diagnostic = ""
    error = None
    provider_failure: ModelFailure | None = None
    hermes_started = _time.monotonic()
    owner = _model_call_owner(state)
    candidates = ordered_candidates(MODEL_CANDIDATES, state.get("active_model") if state else None)
    try:
        from pr_agent.distributed.runtime import get_execution_runtime

        execution_runtime = get_execution_runtime()
    except Exception:
        execution_runtime = None
    category_value = ""
    if trigger_type == "pipeline_failed":
        try:
            from pr_agent.triage.failure_categories import categorize_failed_job

            category_value = categorize_failed_job({"name": job_name}).value
        except Exception:
            category_value = "unknown"

    def report_owner_progress(summary: str, *, phase: str = "diagnosing", changed_files_count: int = 0) -> None:
        if execution_runtime is None or not summary:
            return
        metadata = {"root_cause_group_id": effective_root_cause_id}
        if changed_files_count:
            metadata["changed_files_count"] = changed_files_count
        execution_runtime.record_repair_progress_sync(
            phase,
            summary,
            categories=(category_value,) if category_value else (),
            job_names=(job_name,),
            metadata=metadata,
        )

    if trigger_type == "pipeline_failed":
        report_owner_progress("正在诊断失败原因并检查相关代码")
    stop_model_loop = False
    for candidate_index, candidate in enumerate(candidates):
        if not _MODEL_HEALTH_STORE.candidate_allowed(candidate, owner):
            attempted_models.append(candidate)
            model_failure_code = "cooldown"
            continue
        for same_model_attempt in range(2):
            if candidate not in attempted_models:
                attempted_models.append(candidate)
            selected_model = candidate
            segment_id = (
                f"{effective_root_cause_id or job_name}:{iteration}:{candidate_index + 1}:{same_model_attempt + 1}"
            )
            metadata = {"model": candidate, "model_attempt": same_model_attempt + 1}
            if execution_runtime is not None:
                execution_runtime.record_lifecycle_sync(
                    "hermes", "start", segment_id=segment_id, metadata=metadata
                )
            try:
                outcome = _run_hermes(
                    repo_dir,
                    full_prompt,
                    model=candidate,
                    hide_git_metadata=trigger_type == "pipeline_failed",
                    progress_callback=(
                        lambda summary: report_owner_progress(
                            summary,
                            phase="editing" if "应用" in summary else "diagnosing",
                        )
                    ) if trigger_type == "pipeline_failed" else None,
                )
            finally:
                if execution_runtime is not None:
                    execution_runtime.record_lifecycle_sync(
                        "hermes", "end", segment_id=segment_id, metadata=metadata
                    )
            new_files = list(outcome.changed_files)
            diagnostic = outcome.diagnostic
            provider_failure = outcome.provider_failure
            failure_kind = outcome.failure_kind.value if outcome.failure_kind is not None else ""
            error = failure_kind or None
            current_changes = set(_get_changed_files(repo_dir))
            partial_changes = sorted(current_changes - initial_changes)
            if partial_changes:
                report_owner_progress(
                    "已生成代码修改，正在执行安全检查",
                    phase="editing",
                    changed_files_count=len(partial_changes),
                )
            if outcome.failure_kind is None:
                _MODEL_HEALTH_STORE.mark_succeeded(candidate, owner)
                stop_model_loop = True
                break
            if partial_changes:
                if trigger_type == "pipeline_failed" and operation == "repair":
                    from ut_agent.dependency_evidence import validate_discovered_provider_changes
                    from ut_agent.repair_safety import validate_member_substitutions

                    safe, safety_reason = validate_discovered_provider_changes(
                        repo_dir, dependency_snapshots, partial_changes
                    )
                    if not safe:
                        return build_result(
                            "unsafe_changes",
                            result_operation,
                            job_name,
                            repo_dir,
                            partial_changes,
                            diagnostic,
                            safety_reason + " 请调用 discard_workspace_tool 丢弃该修改。",
                            validation_error=safety_reason,
                        )
                    safe, safety_reason = validate_member_substitutions(repo_dir, dependency_sources)
                    if not safe:
                        return build_result(
                            "unsafe_changes",
                            result_operation,
                            job_name,
                            repo_dir,
                            partial_changes,
                            diagnostic,
                            safety_reason + " 请调用 discard_workspace_tool 丢弃该修改。",
                            validation_error=safety_reason,
                        )
                return build_result(
                    "partial_changes",
                    result_operation,
                    job_name,
                    repo_dir,
                    partial_changes,
                    diagnostic,
                    f"Hermes 执行失败且已产生部分修改，未自动切换模型: {error}",
                )

            if provider_failure is None:
                stop_model_loop = True
                break
            failure = provider_failure
            model_failure_code = failure.code
            if not failure.switchable:
                stop_model_loop = True
                break
            should_retry_same_model = (
                same_model_attempt == 0
                and failure.code not in _DIRECT_FAILOVER_CODES
                and _is_transient_hermes_error(failure.reason)
            )
            if should_retry_same_model:
                logger.warning(
                    f"[generate_code] Hermes 瞬时失败且工作区未变化，使用 {candidate} 重试一次"
                )
                _time.sleep(HERMES_RETRY_DELAY_SECONDS)
                continue
            _MODEL_HEALTH_STORE.mark_failed(candidate, owner, failure)
            model_failover_count += 1
            break
        if stop_model_loop:
            break

    if not selected_model and attempted_models:
        error = "all configured model routes are in shared cooldown"
        model_failure_code = "cooldown"
        failure_kind = HermesFailureKind.PROVIDER_UNAVAILABLE.value

    if new_files and trigger_type == "pipeline_failed" and operation not in MUTATING_PIPELINE_OPERATIONS:
        return build_result(
            "unexpected_changes",
            result_operation,
            job_name,
            repo_dir,
            new_files,
            diagnostic,
            "只读操作产生了意外仓库修改，必须先显式丢弃。",
        )
    if new_files:
        report_owner_progress(
            "代码修改已完成，正在准备提交",
            phase="editing",
            changed_files_count=len(new_files),
        )
        if trigger_type == "pipeline_failed" and operation == "repair":
            from ut_agent.dependency_evidence import validate_discovered_provider_changes
            from ut_agent.repair_safety import validate_member_substitutions

            safe, safety_reason = validate_discovered_provider_changes(repo_dir, dependency_snapshots, new_files)
            if not safe:
                return build_result(
                    "unsafe_changes",
                    result_operation,
                    job_name,
                    repo_dir,
                    new_files,
                    diagnostic,
                    safety_reason + " 请调用 discard_workspace_tool 丢弃该修改。",
                    validation_error=safety_reason,
                )
            safe, safety_reason = validate_member_substitutions(repo_dir, dependency_sources)
            if not safe:
                return build_result(
                    "unsafe_changes",
                    result_operation,
                    job_name,
                    repo_dir,
                    new_files,
                    diagnostic,
                    safety_reason + " 请调用 discard_workspace_tool 丢弃该修改。",
                    validation_error=safety_reason,
                )
        return build_result(
            "changed",
            result_operation,
            job_name,
            repo_dir,
            new_files,
            diagnostic,
            f"Hermes 已修改 {len(new_files)} 个文件。",
        )
    if error:
        if failure_kind in {
            HermesFailureKind.SEARCH_LOOP.value,
            HermesFailureKind.EXECUTION_BUDGET_EXHAUSTED.value,
        }:
            status = "investigation_timeout" if operation == "investigate" else "repair_timeout"
            timeout_label = "搜索调查达到执行上限" if failure_kind == "search_loop" else "Hermes 执行达到时间上限"
            return build_result(
                status,
                result_operation,
                job_name,
                repo_dir,
                [],
                diagnostic,
                f"{timeout_label}，未切换模型。",
            )
        if provider_failure is not None and provider_failure.switchable or model_failure_code == "cooldown":
            models = "、".join(model.split("/", 1)[-1] for model in attempted_models)
            message = f"模型服务暂时不可用；已尝试模型：{models}；原因：{model_failure_code}。"
        else:
            message = f"Hermes CLI 执行失败: {diagnostic or error}"
        return build_result(
            "coding_infra_error",
            result_operation,
            job_name,
            repo_dir,
            [],
            diagnostic,
            message,
        )
    if trigger_type != "pipeline_failed":
        return build_result(
            "no_changes",
            result_operation,
            job_name,
            repo_dir,
            [],
            diagnostic,
            "Hermes 已完成诊断，但未修改文件。",
        )
    if operation == "investigate":
        failure_kind = HermesFailureKind.INVESTIGATION_NO_RESULT.value
        return build_result(
            "investigated",
            result_operation,
            job_name,
            repo_dir,
            [],
            diagnostic,
            "Hermes 已完成调查，尚未尝试修复。",
        )
    if operation == "repair":
        failure_kind = HermesFailureKind.REPAIR_NO_CHANGES.value
        return build_result(
            "repair_no_changes",
            result_operation,
            job_name,
            repo_dir,
            [],
            diagnostic,
            "Hermes 已尝试修复，但未产生仓库修改。",
        )

    if operation == "coverage_enhancement":
        return build_result(
            "no_changes",
            result_operation,
            job_name,
            repo_dir,
            [],
            diagnostic,
            "覆盖率补测未产生仓库修改。",
        )
    blocker, validation_error = parse_blocker_record(diagnostic, job_name)
    if validation_error:
        return build_result(
            "incomplete",
            result_operation,
            job_name,
            repo_dir,
            [],
            diagnostic,
            "阻塞证据不完整。",
            validation_error=validation_error,
        )
    return build_result(
        "blocked",
        result_operation,
        job_name,
        repo_dir,
        [],
        diagnostic,
        "已验证仓库内无安全修复路径。",
        blocker=blocker,
    )


def _generate_result(
    status: str,
    operation: str | None,
    job_name: str,
    repo_dir: str,
    changed_files: list[str],
    diagnostic: str,
    message: str,
    *,
    blocker: dict | None = None,
    validation_error: str = "",
    root_cause_id: str = "",
    hermes_duration_ms: int = 0,
    model: str = "",
    attempted_models: list[str] | None = None,
    model_failover_count: int = 0,
    model_failure_code: str = "",
    failure_kind: str = "",
) -> str:
    from ut_agent.repair_progress import build_progress_fingerprint, diagnostic_digest, workspace_diff_digest

    relative_files = sorted(
        os.path.relpath(path, repo_dir) if os.path.isabs(path) else path
        for path in changed_files
    )
    diff_digest = workspace_diff_digest(repo_dir)
    evidence_digest = diagnostic_digest(diagnostic, job_name=job_name)
    result = {
        "status": status,
        "operation": operation,
        "job_name": job_name,
        "changed_files": relative_files,
        "diagnostic": diagnostic or "",
        "root_cause_id": root_cause_id,
        "evidence_digest": evidence_digest,
        "diff_digest": diff_digest,
        "progress_fingerprint": build_progress_fingerprint(
            operation=operation or "",
            root_cause_id=root_cause_id,
            diagnostic=diagnostic,
            job_name=job_name,
            changed_files=relative_files,
            diff_digest=diff_digest,
        ),
        "hermes_duration_ms": hermes_duration_ms,
        "model": model,
        "attempted_models": attempted_models or [],
        "model_failover_count": model_failover_count,
        "model_failure_code": model_failure_code,
        "failure_kind": failure_kind,
        "message": message,
    }
    if operation == "repair" and relative_files:
        try:
            from ut_agent.repair_report import capture_repair_diff, parse_repair_report

            repair_report = parse_repair_report(diagnostic, relative_files)
            if repair_report is not None:
                result["repair_report"] = repair_report.to_dict()
            if os.path.isdir(os.path.join(repo_dir, ".git")):
                result["file_changes"] = list(capture_repair_diff(repo_dir, relative_files))
        except Exception as exc:
            logger.warning(f"[generate_code] 无法生成结构化修复报告: {exc}")
    if blocker is not None:
        result["blocker"] = blocker
    if validation_error:
        result["validation_error"] = validation_error
    return json.dumps(result, ensure_ascii=False)


def _snapshot_files(repo_dir: str) -> dict[str, float]:
    """遍历 repo 目录，记录所有文件的 mtime。"""
    snapshot = {}
    if not os.path.isdir(repo_dir):
        return snapshot
    for root, _dirs, files in os.walk(repo_dir):
        if "/.git" in root or "\\.git" in root:
            continue
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                snapshot[fpath] = os.path.getmtime(fpath)
            except OSError:
                pass
    return snapshot


def _get_changed_files(repo_dir: str) -> list[str]:
    """返回 Git working tree 中所有已跟踪和未跟踪变更。"""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        logger.warning(f"[generate_code] git status 失败: {result.stderr.strip()}")
        return []

    changed_files = []
    for line in result.stdout.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed_files.append(os.path.join(repo_dir, path))
    return sorted(changed_files)


def _restore_protected_files(repo_dir: str, original_mtimes: dict[str, float] | None = None) -> list[str]:
    """检查被 Hermes 修改的 CMakeLists.txt，如果引用了不存在的源文件则恢复原版。"""
    import re
    violations = []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return violations
        modified_files = result.stdout.strip().split("\n")
        for fpath in modified_files:
            if not fpath:
                continue
            basename = os.path.basename(fpath)
            if basename != "CMakeLists.txt":
                continue
            full_path = os.path.join(repo_dir, fpath)
            if not os.path.isfile(full_path):
                continue
            cmake_dir = os.path.dirname(full_path)
            diff_result = subprocess.run(
                ["git", "diff", "--unified=0", "HEAD", "--", fpath],
                cwd=repo_dir, capture_output=True, text=True, timeout=10
            )
            added_lines = [
                ln[1:] for ln in diff_result.stdout.splitlines()
                if ln.startswith("+") and not ln.startswith("+++")
            ]
            added_text = "\n".join(added_lines)
            source_refs = re.findall(r'[\s(]([^\s()]*\.(?:cpp|cc|cxx|c))\b', added_text)
            has_missing = False
            missing_files = []
            for src in source_refs:
                src_path = os.path.join(cmake_dir, src) if not os.path.isabs(src) else os.path.join(repo_dir, src)
                if not os.path.isfile(src_path):
                    missing_files.append(src)
                    has_missing = True
            if has_missing:
                violation_msg = f"{fpath} 引用了不存在的源文件: {missing_files}"
                logger.warning(f"[generate_code] CMakeLists.txt {violation_msg}")
                subprocess.run(
                    ["git", "checkout", "--", fpath],
                    cwd=repo_dir, capture_output=True, timeout=10
                )
                if original_mtimes is not None and os.path.isfile(full_path):
                    old_mtime = original_mtimes.get(full_path)
                    if old_mtime is not None:
                        try:
                            os.utime(full_path, (old_mtime, old_mtime))
                        except OSError:
                            pass
                violations.append(violation_msg)
    except Exception as e:
        logger.warning(f"[generate_code] 检查 CMakeLists.txt 时出错: {e}")
    return violations


def _run_hermes(
    repo_dir: str,
    prompt: str,
    *,
    model: str = MODEL,
    hide_git_metadata: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> HermesRunOutcome:
    """调用 Hermes CLI，返回类型化的执行结果。

    Hermes 通过 `hermes chat -q "prompt"` 调用，使用当前请求的 UT 模型/API 配置。
    """
    logger.info("[generate_code] === Hermes CLI 调用开始 ===")
    logger.info(f"[generate_code] 工作目录: {repo_dir}")
    logger.info(f"[generate_code] prompt 长度: {len(prompt)} chars")
    logger.debug(f"[generate_code] prompt 前 500 字符: {prompt[:500]}")

    # 检查 hermes 命令是否可用
    try:
        which_result = subprocess.run(
            ["which", "hermes"],
            capture_output=True, text=True, timeout=5
        )
        hermes_path = which_result.stdout.strip()
        logger.info(f"[generate_code] hermes 路径: {hermes_path or '未找到'}")
        if not hermes_path:
            logger.error("[generate_code] hermes 命令不在 PATH 中")
            return HermesRunOutcome((), "hermes 命令不在 PATH 中", HermesFailureKind.REQUEST_INVALID)
    except Exception as e:
        logger.warning(f"[generate_code] 检查 hermes 路径失败: {e}")

    before_files = _snapshot_files(repo_dir)

    # Hermes 单次非交互调用
    cmd = [
        "hermes", "chat",
        "-q", prompt,
        "--provider", "relay",
        "--model", model.split("/", 1)[-1],
    ]
    logger.info("[generate_code] 执行命令: hermes chat -q <prompt>")

    try:
        with _hide_git_metadata(repo_dir, enabled=hide_git_metadata), _hermes_runtime(
            model,
            guard_system_mutations=hide_git_metadata,
        ) as env:
            proc = subprocess.Popen(
                cmd,
                cwd=repo_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            stdout_lines = []
            stderr_lines = []
            reported_progress = set()
            start_time = _time.time()

            output_events: queue.Queue[tuple[str, str | None]] = queue.Queue()

            def pump_stream(name: str, stream) -> None:
                try:
                    while True:
                        line = stream.readline()
                        if not line:
                            break
                        output_events.put((name, line.rstrip()))
                finally:
                    output_events.put((name, None))

            readers = [
                threading.Thread(target=pump_stream, args=("stdout", proc.stdout), daemon=True),
                threading.Thread(target=pump_stream, args=("stderr", proc.stderr), daemon=True),
            ]
            for reader in readers:
                reader.start()

            open_streams = {"stdout", "stderr"}
            while open_streams:
                elapsed = _time.time() - start_time
                remaining = CODING_AGENT_TIMEOUT - elapsed
                if remaining <= 0:
                    proc.kill()
                    wait = getattr(proc, "wait", None)
                    if callable(wait):
                        try:
                            wait(timeout=5)
                        except Exception:
                            pass
                    for reader in readers:
                        reader.join(timeout=1)
                    while True:
                        try:
                            stream_name, line = output_events.get_nowait()
                        except queue.Empty:
                            break
                        if line is None:
                            continue
                        (stdout_lines if stream_name == "stdout" else stderr_lines).append(line)
                    timeout_reason = _diagnose_timeout(stdout_lines, stderr_lines, elapsed)
                    logger.error(f"[generate_code] Hermes CLI 超时 ({int(elapsed)}s)，已终止")
                    logger.error(f"[generate_code] 超时诊断: {timeout_reason}")
                    stdout_tail = _redact_hermes_text(" | ".join(stdout_lines[-5:])) if stdout_lines else "无"
                    logger.error(f"[generate_code] 最后 stdout: {stdout_tail}")
                    if stderr_lines:
                        stderr_tail = _redact_hermes_text(" | ".join(stderr_lines[-3:]))
                        logger.error(f"[generate_code] stderr: {stderr_tail}")
                    return _hermes_timeout_outcome(stdout_lines, stderr_lines, elapsed)
                try:
                    stream_name, line = output_events.get(timeout=min(1.0, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    open_streams.discard(stream_name)
                    continue
                if stream_name == "stderr":
                    stderr_lines.append(line)
                    continue
                stdout_lines.append(line)
                summary = _owner_progress_summary(line)
                if summary and summary not in reported_progress and progress_callback is not None:
                    reported_progress.add(summary)
                    try:
                        progress_callback(summary)
                    except Exception:
                        pass
                if line and len(line) < 500:
                    logger.info(f"[Hermes] {_redact_hermes_text(line)}")
                elif line:
                    logger.debug(f"[Hermes] {_redact_hermes_text(line[:500])}...")

            wait = getattr(proc, "wait", None)
            if callable(wait) and proc.poll() is None:
                wait(timeout=5)

            returncode = proc.returncode
            elapsed_total = int(_time.time() - start_time)
            logger.info(f"[generate_code] Hermes CLI 退出码: {returncode} (耗时 {elapsed_total}s)")
            logger.info(f"[generate_code] stdout 总行数: {len(stdout_lines)}")

            if stderr_lines:
                logger.warning(f"[generate_code] Hermes stderr ({len(stderr_lines)} 行):")
                for line in stderr_lines:
                    logger.warning(f"[generate_code] [stderr] {_redact_hermes_text(line)}")

            if returncode != 0:
                logger.error(f"[generate_code] Hermes 退出码非 0 ({returncode})，完整 stdout:")
                for line in stdout_lines:
                    logger.error(f"[generate_code] [stdout] {_redact_hermes_text(line)}")
                diagnostic = _redact_hermes_text("\n".join(stdout_lines[-20:]))
                error = _redact_hermes_text(f"退出码 {returncode}: {' | '.join(stderr_lines[-3:])}")
                failure = classify_model_failure(error)
                if failure.switchable:
                    return _provider_error_outcome(error, diagnostic=diagnostic)
                return HermesRunOutcome((), diagnostic or error, HermesFailureKind.REQUEST_INVALID)

            api_error = _extract_hermes_api_error(stdout_lines, stderr_lines)
            if api_error:
                logger.error(f"[generate_code] Hermes API 调用失败: {api_error}")
                diagnostic = _redact_hermes_text("\n".join(stdout_lines[-20:]))
                return _provider_error_outcome(api_error, diagnostic=diagnostic)

    except FileNotFoundError:
        logger.error("[generate_code] hermes 命令未找到，请确认 Dockerfile 中已安装 Hermes CLI")
        logger.error("[generate_code] 检查: which hermes 返回什么？PATH 是否包含 /usr/local/bin？")
        return HermesRunOutcome((), "hermes 命令未找到", HermesFailureKind.REQUEST_INVALID)
    except Exception as e:
        logger.error(f"[generate_code] Hermes CLI 调用异常: {e}")
        import traceback
        logger.error(f"[generate_code] 异常堆栈: {traceback.format_exc()}")
        failure = classify_model_failure(e)
        if failure.switchable:
            return _provider_error_outcome(e)
        return HermesRunOutcome((), str(e), HermesFailureKind.REQUEST_INVALID)

    # 安全网：恢复被 Hermes 意外修改的构建配置文件
    violations = _restore_protected_files(repo_dir, original_mtimes=before_files)
    if violations:
        logger.warning(f"[generate_code] 安全网拦截: {violations}")

    # 读取 Git working tree，覆盖源码、构建脚本和配置文件等所有变更。
    new_files = _get_changed_files(repo_dir)

    if new_files:
        logger.info(f"[generate_code] Hermes 生成了 {len(new_files)} 个文件:")
        for f in new_files:
            logger.info(f"[generate_code]   - {f}")
    else:
        logger.warning("[generate_code] Hermes CLI 未修改任何仓库文件")
        # 记录 git diff 摘要供排查
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=repo_dir, capture_output=True, text=True, timeout=10
            )
            if diff_result.stdout.strip():
                logger.info(f"[generate_code] git diff --stat:\n{diff_result.stdout.strip()}")
            else:
                logger.info("[generate_code] git diff 为空（仓库无任何变更）")
        except Exception:
            pass

    logger.info("[generate_code] === Hermes CLI 调用结束 ===")
    diagnostic = _redact_hermes_text("\n".join(stdout_lines).strip())
    return HermesRunOutcome(tuple(new_files), diagnostic[-4000:])


def _diagnose_timeout(stdout_lines: list[str], stderr_lines: list[str], elapsed: float) -> str:
    """根据 CLI 的部分输出诊断超时原因。"""
    all_output = "\n".join(stdout_lines + stderr_lines).lower()

    if "429" in all_output or "rate limit" in all_output or "too many requests" in all_output:
        return "LLM 429 限流 - API 请求过于频繁，需要等待后重试"

    if "unauthorized" in all_output or "401" in all_output or "auth" in all_output and "fail" in all_output:
        return "认证失败 - API key 可能过期或无效"

    connection_failed = "connection" in all_output and ("refused" in all_output or "reset" in all_output)
    if "timeout" in all_output or "timed out" in all_output or connection_failed:
        return "网络超时 - 无法连接到 API 服务"

    if "too long" in all_output or "context length" in all_output or "token limit" in all_output:
        return "Prompt 过长 - 超出模型 context window 限制"

    if not stdout_lines:
        return "无输出 - CLI 启动后无任何响应，可能是进程挂起或 PATH 问题"

    search_count = sum(1 for line in stdout_lines if "search" in line.lower() or "read" in line.lower())
    if search_count > 20:
        return f"搜索循环 - 执行了 {search_count} 次搜索/读取操作后超时"

    if len(stdout_lines) < 5:
        return f"API 响应慢 - 仅 {len(stdout_lines)} 行输出后超时"

    return f"未知原因 - 已有 {len(stdout_lines)} 行输出，最后活动在第 {int(elapsed)}s"


# ── Copilot CLI 调用逻辑（已注释保留，如需回退取消注释即可）──
#
# def _run_copilot(repo_dir: str, prompt: str) -> list[str]:
#     """调用 Copilot CLI 在 repo 中生成代码，返回生成的文件路径列表。"""
#     logger.info(f"[generate_code] 调用 Copilot CLI (prompt 长度: {len(prompt)} chars)...")
#     before_files = _snapshot_files(repo_dir)
#     cmd = [
#         "copilot",
#         "-p", prompt,
#         "--allow-all-tools",
#         "--deny-tool=shell(git push)",
#         "--deny-tool=shell(git commit)",
#         "--deny-tool=shell(rm)",
#     ]
#     try:
#         proc = subprocess.Popen(
#             cmd,
#             cwd=repo_dir,
#             stdout=subprocess.PIPE,
#             stderr=subprocess.PIPE,
#             text=True,
#         )
#         stdout_lines = []
#         start_time = _time.time()
#         while True:
#             elapsed = _time.time() - start_time
#             if elapsed > CODING_AGENT_TIMEOUT:
#                 proc.kill()
#                 logger.error(f"[generate_code] Copilot CLI 超时 ({int(elapsed)}s)，已终止")
#                 return []
#             line = proc.stdout.readline()
#             if line:
#                 stripped = line.rstrip()
#                 stdout_lines.append(stripped)
#                 if stripped and len(stripped) < 500:
#                     logger.info(f"[Copilot] {stripped}")
#             elif proc.poll() is not None:
#                 remaining = proc.stdout.read()
#                 if remaining:
#                     stdout_lines.extend(remaining.rstrip().split("\n"))
#                 break
#         returncode = proc.returncode
#         logger.info(f"[generate_code] Copilot CLI 退出码: {returncode} (耗时 {int(_time.time() - start_time)}s)")
#     except Exception as e:
#         logger.error(f"[generate_code] Copilot CLI 调用异常: {e}")
#         return []
#     violations = _restore_protected_files(repo_dir, original_mtimes=before_files)
#     if violations:
#         logger.warning(f"[generate_code] 安全网拦截: {violations}")
#     after_files = _snapshot_files(repo_dir)
#     new_files = _diff_snapshots(before_files, after_files)
#     if new_files:
#         logger.info(f"[generate_code] Copilot 生成了 {len(new_files)} 个文件: {new_files}")
#     else:
#         logger.warning("[generate_code] Copilot CLI 未生成任何新测试文件")
#     return new_files
