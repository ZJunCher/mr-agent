"""Run fixed and project-configured validation checks in an MR workspace."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from pr_agent.distributed.runtime import get_execution_runtime
from pr_agent.log import get_logger
from ut_agent.config import (
    VALIDATION_DEFAULT_TIMEOUT_SECONDS,
    VALIDATION_MAX_OUTPUT_CHARS,
    ValidationProfile,
    get_validation_profile,
)
from ut_agent.tools.context import get_repo_dir
from ut_agent.tools.repo_snapshot import RepoSnapshotError, capture_worktree_snapshot, check_worktree_diff

logger = get_logger()

_SUPPORTED_CHECKS = {
    "diff_check",
    "python_compile_check",
    "lint_check",
    "build_check",
    "test_check",
    "unit_test_check",
}
_POLL_INTERVAL_SECONDS = 0.1


def _message_content(message) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def _is_coverage_repair(state: dict) -> bool:
    categories = {str(value).strip().lower() for value in state.get("selected_categories") or ()}
    if "coverage" in categories:
        return True
    for message in state.get("messages") or ():
        try:
            payload = json.loads(_message_content(message))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for item in payload.get("work_items") or ():
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "").lower() == "coverage":
                return True
            if item.get("required_tool") == "fetch_coverage_report_tool":
                return True
    return False


def _is_test_path(path: str) -> bool:
    normalized = str(path).replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1].lower() if parts else ""
    return "tests" in {part.lower() for part in parts[:-1]} or name.startswith("test_") or name.endswith("_test.py")


def required_checks_for_paths(
    state: dict,
    changed_files: list[str],
    profile: ValidationProfile | None = None,
) -> tuple[str, ...]:
    """Return mandatory checks in stable execution order."""
    required = ["diff_check"]
    if any(path.endswith(".py") for path in changed_files):
        required.append("python_compile_check")
    if profile is not None:
        required.extend(profile.configured_checks)
    if _is_coverage_repair(state) or any(_is_test_path(path) for path in changed_files):
        test_check = "test_check" if profile is not None and profile.test_argv else "unit_test_check"
        required.append(test_check)
    # Work Items record the checks selected from the exact failed Jobs.  The last
    # unfinished item revalidates the union against the cumulative workspace Diff.
    from ut_agent.repair_plan import latest_repair_plan, required_verification_work_item_ids

    plan = latest_repair_plan(state)
    required_work_item_ids = set(required_verification_work_item_ids(state, plan))
    if plan is not None:
        for item in plan.work_items:
            if item.work_item_id in required_work_item_ids:
                required.extend(item.required_checks)
    if profile is not None and profile.effective_test_argv:
        test_alias = "test_check" if profile.test_argv else "unit_test_check"
        required = [
            test_alias if name in {"test_check", "unit_test_check"} else name
            for name in required
        ]
    return tuple(dict.fromkeys(required))


def _runtime_guard(runtime) -> None:
    if runtime is None:
        return
    runtime.raise_if_canceled()
    runtime.assert_fence_sync()


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _run_argv(
    argv: tuple[str, ...],
    cwd: str,
    timeout_seconds: int,
    max_output_chars: int,
    runtime,
    *,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Run one configured argv without a shell, polling cancellation and fencing."""
    started = time.monotonic()
    timed_out = False
    execution_error = ""
    process = None
    process_env = os.environ.copy()
    if extra_env:
        process_env.update(extra_env)
    with tempfile.TemporaryFile(mode="w+b") as output_file:
        try:
            _runtime_guard(runtime)
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=process_env,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                shell=False,
            )
            while process.poll() is None:
                try:
                    _runtime_guard(runtime)
                except Exception as exc:
                    execution_error = f"运行时安全检查失败: {exc}"
                    _terminate_process(process)
                    break
                if time.monotonic() - started >= timeout_seconds:
                    timed_out = True
                    _terminate_process(process)
                    break
                time.sleep(_POLL_INTERVAL_SECONDS)
        except Exception as exc:
            execution_error = f"检查无法启动: {exc}"
            if process is not None:
                _terminate_process(process)

        return_code = process.returncode if process is not None and process.returncode is not None else None
        output_file.seek(0)
        raw_output = output_file.read(max_output_chars + 1)
    output_truncated = len(raw_output) > max_output_chars
    output = raw_output[:max_output_chars].decode("utf-8", errors="replace")
    passed = return_code == 0 and not timed_out and not execution_error
    return {
        "passed": passed,
        "exit_code": return_code,
        "timed_out": timed_out,
        "output_truncated": output_truncated,
        "output": output,
        "error": execution_error,
    }


def _check_result(name: str, **values) -> dict:
    return {"name": name, "check": name, **values}


def _run_diff_check(repo_dir: str, runtime) -> dict:
    _runtime_guard(runtime)
    passed, output = check_worktree_diff(repo_dir)
    _runtime_guard(runtime)
    return _check_result(
        "diff_check",
        passed=passed,
        exit_code=0 if passed else 2,
        timed_out=False,
        output_truncated=False,
        output=output,
        error="",
    )


def _run_python_compile_check(repo_dir: str, changed_files: list[str], runtime) -> dict:
    python_files = [path for path in changed_files if path.endswith(".py") and os.path.isfile(os.path.join(repo_dir, path))]
    if not python_files:
        return _check_result(
            "python_compile_check",
            passed=True,
            exit_code=0,
            timed_out=False,
            output_truncated=False,
            output="",
            error="",
        )

    outputs = []
    passed = True
    exit_code = 0
    timed_out = False
    output_truncated = False
    error = ""
    with tempfile.TemporaryDirectory(prefix="ut-agent-pycache-") as pycache_dir:
        for path in python_files:
            result = _run_argv(
                (sys.executable, "-m", "py_compile", path),
                repo_dir,
                min(60, VALIDATION_DEFAULT_TIMEOUT_SECONDS),
                VALIDATION_MAX_OUTPUT_CHARS,
                runtime,
                extra_env={"PYTHONPYCACHEPREFIX": pycache_dir},
            )
            if result["output"]:
                outputs.append(f"[{path}]\n{result['output']}")
            if not result["passed"]:
                passed = False
                exit_code = result["exit_code"]
                timed_out = result["timed_out"]
                output_truncated = result["output_truncated"]
                error = result["error"]
                break
    output = "\n".join(outputs)[:VALIDATION_MAX_OUTPUT_CHARS]
    return _check_result(
        "python_compile_check",
        passed=passed,
        exit_code=exit_code,
        timed_out=timed_out,
        output_truncated=output_truncated or len("\n".join(outputs)) > VALIDATION_MAX_OUTPUT_CHARS,
        output=output,
        error=error,
    )


def _profile_working_directory(repo_dir: str, profile: ValidationProfile) -> str | None:
    repo_root = Path(repo_dir).resolve()
    candidate = (repo_root / profile.working_directory).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        return None
    return str(candidate) if candidate.is_dir() else None


def _profile_argv(profile: ValidationProfile, check_name: str) -> tuple[str, ...]:
    return {
        "lint_check": profile.lint_argv,
        "build_check": profile.build_argv,
        "test_check": profile.effective_test_argv,
        "unit_test_check": profile.effective_test_argv,
    }.get(check_name, ())


def _run_profile_check(repo_dir: str, profile: ValidationProfile, check_name: str, runtime) -> dict:
    cwd = _profile_working_directory(repo_dir, profile)
    if cwd is None:
        return _check_result(
            check_name,
            passed=False,
            exit_code=None,
            timed_out=False,
            output_truncated=False,
            output="",
            error="配置的 working_directory 不在仓库内或不存在",
        )
    argv = _profile_argv(profile, check_name)
    if not argv:
        return _check_result(
            check_name,
            passed=False,
            exit_code=None,
            timed_out=False,
            output_truncated=False,
            output="",
            error=f"项目验证 profile 未配置 {check_name}",
            argv=[],
        )
    return _check_result(
        check_name,
        argv=list(argv),
        **_run_argv(
            argv,
            cwd,
            profile.timeout_seconds,
            VALIDATION_MAX_OUTPUT_CHARS,
            runtime,
        ),
    )


@tool
def run_repo_validation_tool(
    checks: list[str] | None = None,
    work_item_id: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Run mandatory fixed checks and configured project lint/build/test commands.

    The model may request supported check identifiers, but mandatory checks are added automatically.
    Configured commands are executed as argv with shell=False inside the MR workspace.
    """
    state = state or {}
    mr_id = state.get("mr_id", 0)
    repo_dir = get_repo_dir(mr_id)
    if not repo_dir:
        return json.dumps({
            "status": "error", "message": f"MR !{mr_id} 仓库未克隆", "work_item_id": work_item_id,
        }, ensure_ascii=False)

    try:
        before = capture_worktree_snapshot(repo_dir)
    except RepoSnapshotError as exc:
        return json.dumps({
            "status": "error", "message": f"无法读取验证前工作区: {exc}", "work_item_id": work_item_id,
        }, ensure_ascii=False)

    project_id = str(state.get("project_id") or "")
    profile = get_validation_profile(project_id)
    changed_files = [item.path for item in before.changed_files]
    required_checks = required_checks_for_paths(state, changed_files, profile)
    requested_checks = list(checks or ())
    unsupported = [name for name in requested_checks if name not in _SUPPORTED_CHECKS]
    if unsupported:
        return json.dumps({
            "status": "blocked",
            "message": f"不支持的检查标识: {unsupported}。支持的检查: {sorted(_SUPPORTED_CHECKS)}",
            "base_sha": before.base_sha,
            "validated_diff_digest": before.diff_digest,
            "required_checks": list(required_checks),
            "all_passed": False,
            "work_item_id": work_item_id,
        }, ensure_ascii=False)
    test_alias = None
    if profile is not None:
        test_alias = "test_check" if profile.test_argv else "unit_test_check"
    normalized_requested = [
        test_alias if name in {"test_check", "unit_test_check"} and test_alias else name
        for name in requested_checks
    ]
    selected_checks = list(dict.fromkeys([*required_checks, *normalized_requested]))

    profile_checks = {"lint_check", "build_check", "test_check", "unit_test_check"}
    if any(check in profile_checks for check in selected_checks):
        if profile is None:
            return json.dumps({
                "status": "blocked",
                "error_code": "validation_profile_missing",
                "message": f"项目 {project_id or '<unknown>'} 未配置必需的本地验证命令",
                "base_sha": before.base_sha,
                "validated_diff_digest": before.diff_digest,
                "required_checks": list(required_checks),
                "executed_checks": [],
                "results": [],
                "all_passed": False,
                "work_item_id": work_item_id,
            }, ensure_ascii=False)
        missing_commands = [
            check
            for check in selected_checks
            if check in profile_checks and not _profile_argv(profile, check)
        ]
        if missing_commands:
            return json.dumps({
                "status": "blocked",
                "error_code": "validation_command_missing",
                "message": f"项目验证 profile 缺少命令: {missing_commands}",
                "base_sha": before.base_sha,
                "validated_diff_digest": before.diff_digest,
                "required_checks": list(required_checks),
                "executed_checks": [],
                "results": [],
                "all_passed": False,
                "work_item_id": work_item_id,
            }, ensure_ascii=False)
        if _profile_working_directory(repo_dir, profile) is None:
            return json.dumps({
                "status": "blocked",
                "error_code": "validation_working_directory_invalid",
                "message": "配置的 working_directory 不在仓库内或不存在",
                "base_sha": before.base_sha,
                "validated_diff_digest": before.diff_digest,
                "required_checks": list(required_checks),
                "executed_checks": [],
                "results": [],
                "all_passed": False,
                "work_item_id": work_item_id,
            }, ensure_ascii=False)

    runtime = get_execution_runtime()
    results = []
    for check_name in selected_checks:
        try:
            if check_name == "diff_check":
                result = _run_diff_check(repo_dir, runtime)
            elif check_name == "python_compile_check":
                result = _run_python_compile_check(repo_dir, changed_files, runtime)
            else:
                result = _run_profile_check(repo_dir, profile, check_name, runtime)
        except Exception as exc:
            result = _check_result(
                check_name,
                passed=False,
                exit_code=None,
                timed_out=False,
                output_truncated=False,
                output="",
                error=str(exc),
            )
        results.append(result)
        if result.get("passed") is not True:
            break

    try:
        after = capture_worktree_snapshot(repo_dir)
    except RepoSnapshotError as exc:
        return json.dumps({
            "status": "blocked",
            "error_code": "validation_snapshot_unavailable",
            "message": f"验证后无法读取工作区: {exc}",
            "base_sha": before.base_sha,
            "validated_diff_digest": before.diff_digest,
            "required_checks": list(required_checks),
            "executed_checks": results,
            "results": results,
            "all_passed": False,
            "work_item_id": work_item_id,
        }, ensure_ascii=False)
    if after.base_sha != before.base_sha or after.diff_digest != before.diff_digest:
        return json.dumps({
            "status": "blocked",
            "error_code": "validation_modified_workspace",
            "message": "验证命令修改了工作区，验证结果不可用于提交",
            "base_sha": before.base_sha,
            "validated_diff_digest": before.diff_digest,
            "after_diff_digest": after.diff_digest,
            "required_checks": list(required_checks),
            "executed_checks": results,
            "results": results,
            "all_passed": False,
            "work_item_id": work_item_id,
        }, ensure_ascii=False)

    all_passed = bool(results) and all(result.get("passed") is True for result in results)
    return json.dumps({
        "status": "ok",
        "all_passed": all_passed,
        "base_sha": before.base_sha,
        "validated_diff_digest": before.diff_digest,
        "required_checks": list(required_checks),
        "executed_checks": results,
        "results": results,
        "work_item_id": work_item_id,
    }, ensure_ascii=False)
