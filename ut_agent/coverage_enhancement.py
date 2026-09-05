"""Run one bounded, report-driven unit-test coverage enhancement."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

@dataclass(frozen=True)
class CoverageEnhancementRequest:
    job_id: int
    job_name: str
    coverage: float
    threshold: float
    mr_id: int
    source_branch: str


@dataclass(frozen=True)
class CoverageEnhancementResult:
    status: str
    commit_sha: str = ""
    changed_files: tuple[str, ...] = ()
    uncovered_line_count: int = 0
    reason: str = ""


@dataclass(frozen=True)
class CoveragePathValidation:
    ok: bool
    reason: str = ""


_TEST_DIRS = {"test", "tests", "unittest", "unittests"}
_TEST_REGISTRATION = {"cmakelists.txt", "build", "build.bazel", "meson.build"}
_TEST_FILE_RE = re.compile(r"(?:^test_.+|.+_(?:test|unittest))(?:\.[A-Za-z0-9_+-]+)?$", re.IGNORECASE)


def validate_coverage_changed_paths(paths: tuple[str, ...] | list[str]) -> CoveragePathValidation:
    """Allow only unit-test code and registration files below test directories."""
    if not paths:
        return CoveragePathValidation(False, "补测没有产生文件修改。")
    for raw_path in paths:
        normalized = str(raw_path or "").replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            return CoveragePathValidation(False, f"补测修改路径不安全：{normalized or 'empty'}")
        directories = {part.lower() for part in path.parts[:-1]}
        in_test_dir = bool(directories & _TEST_DIRS)
        basename = path.name.lower()
        if basename in _TEST_REGISTRATION:
            if in_test_dir:
                continue
            return CoveragePathValidation(False, f"补测不得修改测试目录外的构建文件：{normalized}")
        if in_test_dir or _TEST_FILE_RE.fullmatch(path.name):
            continue
        return CoveragePathValidation(False, f"补测不得修改生产代码：{normalized}")
    return CoveragePathValidation(True)


def _uncovered_line_count(report: dict) -> int:
    files = report.get("files") or []
    return sum(
        len(file_record.get("uncovered") or [])
        for file_record in files
        if isinstance(file_record, dict)
    )


def _task_description(request: CoverageEnhancementRequest, report: dict) -> str:
    return (
        f"当前变更行覆盖率为 {request.coverage:g}%，阈值为 {request.threshold:g}%。\n\n"
        f"以下未覆盖行报告是本次补测的权威证据：\n{report.get('report_text') or ''}\n\n"
        "只允许新增或修改单元测试；如运行测试确有必要，可修改测试目录内的测试注册文件。"
        "不得修改生产源码、CI 配置、覆盖率阈值，不得删除测试、弱化断言或跳过测试。"
        "完成最小且可运行的测试修改后停止。"
    )


def _decode_tool_result(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def run_coverage_enhancement(
    request: CoverageEnhancementRequest,
    state: dict,
    *,
    fetch_report=None,
    generate=None,
    push=None,
    discard=None,
) -> CoverageEnhancementResult:
    """Fetch evidence, ask Hermes once, validate paths, and push at most once."""
    if fetch_report is None:
        from ut_agent.tools.fetch_coverage_report import fetch_changed_lines_report

        fetch_report = fetch_changed_lines_report
    report = fetch_report(request.job_id)
    uncovered_count = _uncovered_line_count(report)
    if not report.get("available") or uncovered_count <= 0:
        reason = str(report.get("reason") or "覆盖率报告没有可补测的未覆盖代码行。")
        return CoverageEnhancementResult("skipped", uncovered_line_count=uncovered_count, reason=reason)

    if generate is None:
        from ut_agent.tools.generate_code import generate_code_tool

        generate = generate_code_tool.func

    tool_state = {
        **(state or {}),
        "trigger_type": "pipeline_failed",
        "mr_id": request.mr_id,
        "source_branch": request.source_branch,
    }
    generated = _decode_tool_result(generate(
        job_name=request.job_name,
        task_description=_task_description(request, report),
        operation="coverage_enhancement",
        root_cause_id="",
        state=tool_state,
    ))
    changed_files = tuple(str(path) for path in generated.get("changed_files") or ())
    if generated.get("status") != "changed":
        if changed_files:
            if discard is None:
                from ut_agent.tools.discard_workspace import discard_workspace_tool

                discard = discard_workspace_tool.func
            discard(reason="覆盖率补测未完整结束，丢弃未提交修改。", state=tool_state)
        return CoverageEnhancementResult(
            "skipped",
            changed_files=changed_files,
            uncovered_line_count=uncovered_count,
            reason=str(generated.get("message") or "补测未产生可提交修改。"),
        )

    validation = validate_coverage_changed_paths(changed_files)
    if not validation.ok:
        if discard is None:
            from ut_agent.tools.discard_workspace import discard_workspace_tool

            discard = discard_workspace_tool.func
        discard(reason=validation.reason, state=tool_state)
        return CoverageEnhancementResult(
            "unsafe_changes",
            changed_files=changed_files,
            uncovered_line_count=uncovered_count,
            reason=validation.reason,
        )

    if push is None:
        from ut_agent.tools.commit_push import commit_and_push_tool

        push = commit_and_push_tool.func
    pushed = _decode_tool_result(push(state=tool_state))
    commit_sha = str(pushed.get("commit_sha") or "")
    if pushed.get("status") == "success" and pushed.get("changed") is True and len(commit_sha) == 40:
        return CoverageEnhancementResult(
            "pushed",
            commit_sha=commit_sha,
            changed_files=changed_files,
            uncovered_line_count=uncovered_count,
        )
    return CoverageEnhancementResult(
        "failed",
        commit_sha=commit_sha,
        changed_files=changed_files,
        uncovered_line_count=uncovered_count,
        reason=str(pushed.get("message") or "补测提交推送失败。"),
    )
