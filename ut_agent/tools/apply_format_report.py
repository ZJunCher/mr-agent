"""Apply CI-generated format patches to the current UT Agent workspace."""

import json
import re
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from pr_agent.tools.pr_fix_format import PRFixFormat
from ut_agent.tools.context import get_git_provider, get_repo_dir

_FAILURE_HINT_RE = re.compile(r"fatal:|error[:\s]|failed", re.IGNORECASE)


def _job_failure_hint(job) -> str:
    """从 job 日志中提取第一条真实失败原因行。"""
    try:
        trace = job.trace()
        if isinstance(trace, bytes):
            trace = trace.decode("utf-8", errors="replace")
    except Exception:
        return ""
    for line in (trace or "").splitlines():
        stripped = line.strip()
        if stripped and _FAILURE_HINT_RE.search(stripped):
            return stripped[:200]
    return ""


@tool
def apply_format_report_tool(
    pipeline_id: int,
    job_id: int,
    job_name: str = "code_format_check",
    work_item_id: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Apply a failed format job's code-format-report.txt patch locally.

    This deterministic path does not invoke clang-format or Hermes. The resulting working-tree
    changes must still be committed with commit_and_push_tool.
    """
    mr_id = state.get("mr_id", 0) if state else 0
    repo_dir = get_repo_dir(mr_id)
    if not repo_dir:
        return json.dumps({
            "status": "error",
            "pipeline_id": pipeline_id,
            "job_id": job_id,
            "job_name": job_name,
            "changed_files": [],
            "work_item_id": work_item_id,
            "message": f"MR !{mr_id} 仓库未克隆，请先调用 clone_source_branch_tool。",
        }, ensure_ascii=False)

    from ut_agent.workspace import validate_state_workspace

    workspace_validation = validate_state_workspace(state, repo_dir, allow_dirty=True)
    if not workspace_validation.ok:
        return json.dumps({
            "status": "blocked",
            "pipeline_id": pipeline_id,
            "job_id": job_id,
            "job_name": job_name,
            "changed_files": [],
            "work_item_id": work_item_id,
            "error_code": workspace_validation.error_code,
            "retryable": False,
            "message": workspace_validation.message,
        }, ensure_ascii=False)

    git_provider = get_git_provider()
    project_id = state.get("project_id") if state else None
    project_id = project_id or getattr(git_provider, "id_project", "")
    try:
        project = git_provider.gl.projects.get(project_id)
        job = project.jobs.get(job_id)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "pipeline_id": pipeline_id,
            "job_id": job_id,
            "job_name": job_name,
            "changed_files": [],
            "work_item_id": work_item_id,
            "message": f"获取格式 job 失败: {e}",
        }, ensure_ascii=False)

    formatter = PRFixFormat.__new__(PRFixFormat)
    formatter.git_provider = git_provider
    formatter.report_artifact_fallback = "code-format-report.txt"
    report = formatter._get_report_text(project, job)
    if not report.strip():
        hint = _job_failure_hint(job)
        detail = f"（job 日志根因: {hint}）" if hint else ""
        return json.dumps({
            "status": "blocked",
            "pipeline_id": pipeline_id,
            "job_id": job_id,
            "job_name": job_name,
            "changed_files": [],
            "work_item_id": work_item_id,
            "message": (
                f"code-format-report.txt 不可用{detail}；"
                "格式检查 job 未生成报告，属于 job 自身执行失败，而非代码格式问题。"
            ),
        }, ensure_ascii=False)

    file_hunks = formatter._parse_unified_diff(report)
    if not file_hunks:
        return json.dumps({
            "status": "blocked",
            "pipeline_id": pipeline_id,
            "job_id": job_id,
            "job_name": job_name,
            "changed_files": [],
            "work_item_id": work_item_id,
            "message": "code-format-report.txt 不包含可应用的 unified diff。",
        }, ensure_ascii=False)

    try:
        from ut_agent.config import REPAIR_BACKEND
    except Exception:
        REPAIR_BACKEND = "hermes"
    if state and state.get("trigger_type") == "pipeline_failed" and REPAIR_BACKEND == "native":
        from ut_agent.execution_policy import validate_tool_call
        from ut_agent.tools.apply_repo_patch import apply_repo_patch_tool

        patch_args = {
            "patch": report,
            "reason": f"Apply CI format artifact for {job_name} ({job_id}).",
            "work_item_id": work_item_id,
        }
        allowed, reason = validate_tool_call(state, "apply_repo_patch_tool", patch_args)
        if not allowed:
            return json.dumps({
                "status": "blocked",
                "pipeline_id": pipeline_id,
                "job_id": job_id,
                "job_name": job_name,
                "changed_files": [],
                "work_item_id": work_item_id,
                "message": reason,
            }, ensure_ascii=False)
        result = json.loads(apply_repo_patch_tool.func(**patch_args, state=state))
        result.update({
            "pipeline_id": pipeline_id,
            "job_id": job_id,
            "job_name": job_name,
            "work_item_id": work_item_id,
        })
        return json.dumps(result, ensure_ascii=False)

    repo_path = Path(repo_dir).resolve()
    changes: dict[Path, str] = {}
    skipped = []
    for relative_path, hunks in file_hunks.items():
        target = (repo_path / relative_path).resolve()
        if not target.is_relative_to(repo_path):
            skipped.append({"file": relative_path, "reason": "路径超出仓库"})
            continue
        try:
            original = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            skipped.append({"file": relative_path, "reason": f"读取失败: {e}"})
            continue
        new_content = formatter._apply_hunks(original, hunks)
        if new_content is None:
            skipped.append({"file": relative_path, "reason": "报告与当前文件不匹配"})
        elif new_content != original:
            changes[target] = new_content

    for target, content in changes.items():
        target.write_text(content, encoding="utf-8")

    changed_files = sorted(str(path.relative_to(repo_path)) for path in changes)
    result = {
        "status": "changed" if changed_files else "no_changes",
        "pipeline_id": pipeline_id,
        "job_id": job_id,
        "job_name": job_name,
        "changed_files": changed_files,
        "message": (
            f"已从 code-format-report.txt 应用 {len(changed_files)} 个文件的格式修复。"
            if changed_files
            else "格式报告未产生可提交的工作区变更。"
        ),
    }
    if work_item_id:
        result["work_item_id"] = work_item_id
    if skipped:
        result["skipped_files"] = skipped
    return json.dumps(result, ensure_ascii=False)
