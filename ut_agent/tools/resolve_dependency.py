"""Injected-state tool for read-only current dependency contract evidence."""

import json
from dataclasses import dataclass
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from pr_agent.triage.failure_explanations import sanitize_failure_text
from ut_agent.blocker_evidence import validate_blocker_record
from ut_agent.dependency_evidence import (
    MAX_EVIDENCE_CHARS,
    build_dependency_blocker,
    dependency_evidence_snapshot,
    resolve_current_dependency_evidence,
)
from ut_agent.tools.context import get_git_provider, get_repo_dir


@dataclass(frozen=True)
class RootCauseEvidenceBundle:
    root_cause_id: str
    diagnostic_text: str


def _root_cause_evidence_from_state(
    state: dict,
    root_cause_id: str,
    job_name: str,
) -> RootCauseEvidenceBundle:
    try:
        from ut_agent.execution_policy import build_execution_ledger

        pipelines = build_execution_ledger(state.get("messages", [])).pipelines
    except Exception:
        return RootCauseEvidenceBundle("", "")
    for pipeline in reversed(pipelines):
        if str(pipeline.get("pipeline_status") or "") != "failed":
            continue
        groups = [group for group in pipeline.get("root_cause_groups") or [] if isinstance(group, dict)]
        group = next((item for item in groups if str(item.get("root_cause_id") or "") == root_cause_id), None)
        if group is None:
            group = next(
                (
                    item
                    for item in groups
                    if job_name == str(item.get("canonical_job_name") or "")
                    or job_name in {str(name) for name in item.get("job_names") or []}
                ),
                None,
            )
        if group is None:
            continue

        resolved_root_cause_id = str(group.get("root_cause_id") or root_cause_id)
        matching_job_names = {
            str(name)
            for name in group.get("job_names") or ()
            if str(name)
        }
        canonical_job_name = str(group.get("canonical_job_name") or "")
        if canonical_job_name:
            matching_job_names.add(canonical_job_name)
        if job_name:
            matching_job_names.add(job_name)

        raw_values: list[object] = [group.get("canonical_diagnostic")]
        for failed_job in pipeline.get("failed_jobs") or ():
            if not isinstance(failed_job, dict) or str(failed_job.get("name") or "") not in matching_job_names:
                continue
            for candidate in failed_job.get("diagnostic_candidates") or ():
                raw_values.append(candidate.get("text") if isinstance(candidate, dict) else candidate)
            raw_values.extend(failed_job.get("causal_lines") or ())
            log_context = failed_job.get("log_context")
            if isinstance(log_context, (list, tuple)):
                raw_values.extend(log_context)
            else:
                raw_values.append(log_context)

        values = []
        seen = set()
        total_chars = 0
        for raw_value in raw_values:
            remaining = MAX_EVIDENCE_CHARS - total_chars
            if remaining <= 0:
                break
            value = sanitize_failure_text(raw_value, min(2_000, remaining)).strip()
            if not value or value in seen:
                continue
            separator_chars = 1 if values else 0
            if len(value) + separator_chars > remaining:
                value = value[: max(0, remaining - separator_chars)].rstrip()
            if not value:
                break
            seen.add(value)
            values.append(value)
            total_chars += len(value) + separator_chars
        if values:
            return RootCauseEvidenceBundle(resolved_root_cause_id, "\n".join(values))
    return RootCauseEvidenceBundle("", "")


def _root_cause_from_state(state: dict, root_cause_id: str, job_name: str) -> tuple[str, str]:
    bundle = _root_cause_evidence_from_state(state, root_cause_id, job_name)
    return bundle.root_cause_id, bundle.diagnostic_text


@tool
def resolve_dependency_evidence_tool(
    job_name: str,
    root_cause_id: str,
    state: Annotated[dict, InjectedState],
) -> str:
    """Read the current interface identified by a CI root cause from dependencies declared by this MR checkout.

    The project, branch, and file are derived by the system. This tool performs GitLab read operations only and never
    accepts an arbitrary repository path from the model.

    Args:
        job_name: Canonical failed CI job from root_cause_groups.
        root_cause_id: Exact root-cause identity from root_cause_groups.
    """
    mr_id = int(state.get("mr_id") or 0)
    repo_dir = get_repo_dir(mr_id)
    if not repo_dir:
        return json.dumps({"status": "error", "message": "请先克隆 MR 源分支"}, ensure_ascii=False)
    evidence_bundle = _root_cause_evidence_from_state(state, root_cause_id, job_name)
    if not evidence_bundle.diagnostic_text:
        return json.dumps(
            {"status": "error", "message": "未找到该 root_cause_id 的精确流水线证据"},
            ensure_ascii=False,
        )
    git_provider = get_git_provider()
    if git_provider is None or getattr(git_provider, "gl", None) is None:
        return json.dumps({"status": "error", "message": "GitLab provider 未初始化"}, ensure_ascii=False)

    current_project_id = str(
        getattr(git_provider, "id_project", "") or state.get("project_id") or ""
    )
    result = resolve_current_dependency_evidence(
        git_provider.gl,
        repo_dir,
        evidence_bundle.diagnostic_text,
        current_project_id=current_project_id,
        source_branch=str(state.get("source_branch") or ""),
    )
    result["root_cause_id"] = evidence_bundle.root_cause_id or root_cause_id
    result["job_name"] = job_name
    if (
        result.get("status") == "not_found"
        and result.get("evidence_kind") == "declared_interface_missing"
    ):
        blocker = build_dependency_blocker(result, job_name)
        validation_error = validate_blocker_record(blocker, job_name)
        if validation_error:
            result["status"] = "error"
            result["validation_error"] = sanitize_failure_text(validation_error, 500)
        else:
            result["status"] = "blocked"
            result["blocker"] = blocker
            result["dependency_evidence"] = dependency_evidence_snapshot(result)
    if result.get("status") == "resolved":
        if result.get("evidence_kind") == "discovered_provider":
            result["_facts"] = [
                (
                    f"已只读核验缺失包 {result['package_name']} 的唯一提供仓库: "
                    f"{result['project_path']}@{result['resolved_sha']}:{result['file_path']} "
                    f"(默认分支 {result['declared_branch']})"
                )[:1200]
            ]
        else:
            result["_facts"] = [
                (
                    f"当前依赖接口: {result['project_path']}@{result['resolved_sha']}:{result['file_path']} "
                    f"(声明分支 {result['declared_branch']}, sha256 {result['content_sha256']})"
                )[:1200]
            ]
    else:
        result["_facts"] = [f"当前依赖接口解析: {result.get('status')} ({result.get('message', '')})"[:1200]]
        if result.get("owner_facing_analysis"):
            result["_facts"].append(str(result["owner_facing_analysis"])[:1200])
    return json.dumps(result, ensure_ascii=False)
