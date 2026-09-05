"""
commit_and_push 工具 - 将生成的测试文件提交并推送到 MR 源分支。

在克隆的仓库中添加生成的测试代码文件，commit 后 push 到远端源分支。
"""
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from ut_agent.push_attempt import PushAttemptIdentity, build_push_attempt, recover_push_attempt

logger = logging.getLogger("ut_agent")


def _tool_result(
    status: str,
    changed: bool,
    commit_sha: str | None,
    source_branch: str,
    message: str,
    error_code: str | None = None,
    retryable: bool | None = None,
    attempt: PushAttemptIdentity | None = None,
) -> str:
    result = {
        "status": status,
        "changed": changed,
        "commit_sha": commit_sha,
        "source_branch": source_branch,
        "message": message,
    }
    if error_code is not None:
        result["error_code"] = error_code
    if retryable is not None:
        result["retryable"] = retryable
    if attempt is not None:
        result.update(attempt.result_fields())
    if status == "success" and commit_sha:
        result["_facts"] = [f"已推送 commit: {commit_sha} 到分支 {source_branch}"]
    if attempt is not None:
        try:
            from pr_agent.distributed.runtime import get_execution_runtime

            runtime = get_execution_runtime()
            if runtime is not None:
                runtime.record_lifecycle_sync("git_publish", "end", segment_id=attempt.attempt_id)
                if status == "success" and commit_sha:
                    runtime.record_repair_progress_sync(
                        "committing",
                        "修复提交已推送",
                        metadata={"commit_sha": commit_sha},
                    )
        except Exception:
            pass
    return json.dumps(result, ensure_ascii=False)


def commit_and_push(
    repo_dir: str,
    patch_files: list[str],
    source_branch: str,
    mr_id: int,
    author_name: str = "UT Agent",
    author_email: str = "ut-agent@noreply.local",
) -> str:
    """
    将生成的测试文件复制到仓库目录、commit 并 push 到源分支。

    参数:
        repo_dir: 克隆的仓库本地路径
        patch_files: 生成的测试代码文件绝对路径列表
        source_branch: MR 源分支名
        mr_id: MR 编号
        author_name: commit 作者名
        author_email: commit 作者邮箱

    返回:
        成功: "OK: pushed N files to {branch}"
        失败: "ERROR: ..." 错误信息
    """
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return f"ERROR: {repo_dir} 不是有效的 git 仓库"

    if not patch_files:
        return "ERROR: 没有需要提交的文件"

    # 过滤只提交测试代码文件（排除中间计划/日志文件）
    test_files_to_add = []
    abs_repo = os.path.abspath(repo_dir)
    for fpath in patch_files:
        if not os.path.isfile(fpath):
            logger.warning(f"[upload] 文件不存在，跳过: {fpath}")
            continue
        abs_fpath = os.path.abspath(fpath)
        if not abs_fpath.startswith(abs_repo):
            logger.warning(f"[upload] 文件不在 repo 目录内，跳过: {fpath}")
            continue
        rel_path = os.path.relpath(abs_fpath, abs_repo)
        test_files_to_add.append(rel_path)

    if not test_files_to_add:
        return "ERROR: 过滤后没有可提交的测试文件"

    # 配置 git 用户信息
    _run_git(repo_dir, ["config", "user.name", author_name])
    _run_git(repo_dir, ["config", "user.email", author_email])

    # git add
    for rel_path in test_files_to_add:
        ret = _run_git(repo_dir, ["add", rel_path])
        if ret.startswith("ERROR:"):
            return ret
    logger.info(f"[upload] git add 完成: {len(test_files_to_add)} 个文件")

    # git commit
    commit_msg = f"[UT Agent] MR !{mr_id}: 自动生成单元测试\n\n添加 {len(test_files_to_add)} 个测试文件"
    ret = _run_git(repo_dir, ["commit", "-m", commit_msg])
    if ret.startswith("ERROR:"):
        # 如果没有变更需要提交（文件内容相同），不算错误
        if "nothing to commit" in ret:
            return "OK: 无新变更需要提交"
        return ret

    # 获取 commit hash 并展示，方便审查
    commit_hash = _run_git(repo_dir, ["rev-parse", "HEAD"])
    logger.info(f"[upload] git commit 完成, commit={commit_hash}")
    print(f"[UT Agent] Commit: {commit_hash}")

    # git push
    ret = _run_git(repo_dir, ["push", "origin", source_branch])
    if ret.startswith("ERROR:"):
        return ret
    logger.info(f"[upload] git push 完成: {source_branch}")

    return f"OK: pushed {len(test_files_to_add)} files to {source_branch}, commit={commit_hash}"


def _run_git(repo_dir: str, args: list[str]) -> str:
    """在 repo_dir 中执行 git 命令。"""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return f"ERROR: git {args[0]} 失败 (exit={result.returncode}): {stderr}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"ERROR: git {args[0]} 超时 (120s)"
    except Exception as e:
        return f"ERROR: git {args[0]} 异常: {e}"


def _remote_branch_sha(repo_dir: str, source_branch: str) -> tuple[str, str | None]:
    result = _run_git(repo_dir, ["ls-remote", "origin", f"refs/heads/{source_branch}"])
    if result.startswith("ERROR:"):
        return "", result
    first_line = result.splitlines()[0] if result.strip() else ""
    return (first_line.split()[0] if first_line else ""), None


def _effect_tool_result(
    effects,
    effect_name: str,
    status: str,
    changed: bool,
    commit_sha: str | None,
    source_branch: str,
    message: str,
    error_code: str | None = None,
    retryable: bool | None = None,
    attempt: PushAttemptIdentity | None = None,
) -> str:
    encoded = _tool_result(
        status,
        changed,
        commit_sha,
        source_branch,
        message,
        error_code=error_code,
        retryable=retryable,
        attempt=attempt,
    )
    if effects is not None:
        effects.complete(effect_name, json.loads(encoded))
    return encoded


def _git_exit_code(repo_dir: str, args: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} 超时 (120s)"
    except Exception as exc:
        return 2, f"git {args[0]} 异常: {exc}"


def _has_staged_changes(repo_dir: str) -> tuple[bool, str | None]:
    return_code, error = _git_exit_code(repo_dir, ["diff", "--cached", "--quiet", "--no-ext-diff"])
    if return_code == 0:
        return False, None
    if return_code == 1:
        return True, None
    return False, f"ERROR: 无法检查暂存区变更: {error or f'git diff exit={return_code}'}"


def _previous_pushes(state: dict) -> list[dict]:
    from ut_agent.execution_policy import build_execution_ledger

    try:
        return build_execution_ledger(state.get("messages", [])).pushes
    except Exception as exc:
        logger.warning(f"[commit_push] 无法读取历史 push attempts: {exc}")
        return []


def _native_commit_decision(state: dict):
    if state.get("trigger_type") != "pipeline_failed":
        return None
    try:
        from ut_agent.config import REPAIR_BACKEND
    except Exception:
        return None
    if REPAIR_BACKEND != "native":
        return None

    from ut_agent.execution_policy import build_execution_ledger
    from ut_agent.native_repair_state import evaluate_native_commit

    ledger = build_execution_ledger(state.get("messages", []))
    return evaluate_native_commit(ledger.tool_attempts)


def _finish_attempt(
    effects,
    attempt: PushAttemptIdentity,
    status: str,
    changed: bool,
    commit_sha: str | None,
    source_branch: str,
    message: str,
    *,
    error_code: str | None = None,
    retryable: bool | None = None,
) -> str:
    if effects is not None:
        return _effect_tool_result(
            effects,
            attempt.effect_name,
            status,
            changed,
            commit_sha,
            source_branch,
            message,
            error_code=error_code,
            retryable=retryable,
            attempt=attempt,
        )
    return _tool_result(
        status,
        changed,
        commit_sha,
        source_branch,
        message,
        error_code=error_code,
        retryable=retryable,
        attempt=attempt,
    )


def _repair_commit_metadata(
    repo_dir: str,
    attempt: PushAttemptIdentity,
    commit_sha: str,
    source_branch: str,
    pushed_at: str = "",
) -> dict:
    parent_sha = _run_git(repo_dir, ["rev-parse", f"{commit_sha}^"])
    tree_sha = _run_git(repo_dir, ["rev-parse", f"{commit_sha}^{{tree}}"])
    parent_tree_sha = _run_git(repo_dir, ["rev-parse", f"{attempt.base_sha}^{{tree}}"])
    if any(value.startswith("ERROR:") for value in (parent_sha, tree_sha, parent_tree_sha)):
        raise RuntimeError("无法读取修复提交的父提交或 tree")
    if parent_sha != attempt.base_sha:
        raise RuntimeError(f"修复提交父 SHA 不匹配：expected={attempt.base_sha}, actual={parent_sha}")
    return {
        **attempt.result_fields(),
        "commit_sha": commit_sha,
        "parent_sha": parent_sha,
        "tree_sha": tree_sha,
        # Keep the legacy field name; the value is this attempt's parent tree.
        "base_tree_sha": parent_tree_sha,
        "source_branch": source_branch,
        "previous_remote_sha": attempt.base_sha,
        "task_marker": attempt.marker,
        "pushed_at": pushed_at or datetime.now(timezone.utc).isoformat(),
    }


def _record_confirmed_repair_commit(runtime, attempt: PushAttemptIdentity, metadata: dict) -> None:
    if runtime is None or runtime.mode != "queue":
        return
    from pr_agent.triage.repair_rollback import RepairCommitEntry

    runtime.record_repair_commit_sync(
        RepairCommitEntry(
            sequence=attempt.sequence,
            commit_sha=str(metadata["commit_sha"]),
            parent_sha=str(metadata["parent_sha"]),
            tree_sha=str(metadata["tree_sha"]),
            effect_id=attempt.effect_name,
            task_marker=attempt.marker,
            pushed_at=str(metadata["pushed_at"]),
        ),
        parent_tree_sha=str(metadata["base_tree_sha"]),
        source_branch=str(metadata["source_branch"]),
    )


def _finish_confirmed_push(
    repo_dir: str,
    source_branch: str,
    attempt: PushAttemptIdentity,
    effects,
    runtime,
    metadata: dict,
    message: str,
) -> str:
    evidence = metadata
    required = {"commit_sha", "parent_sha", "tree_sha", "base_tree_sha", "pushed_at"}
    if not required.issubset(evidence):
        evidence = _repair_commit_metadata(
            repo_dir,
            attempt,
            str(metadata.get("commit_sha") or _run_git(repo_dir, ["rev-parse", "HEAD"])),
            source_branch,
            str(metadata.get("pushed_at") or ""),
        )
        if effects is not None:
            effects.record_metadata(attempt.effect_name, evidence)
    _record_confirmed_repair_commit(runtime, attempt, evidence)
    return _finish_attempt(
        effects,
        attempt,
        "success",
        True,
        str(evidence["commit_sha"]),
        source_branch,
        message,
    )


def _resume_push_attempt(
    repo_dir: str,
    source_branch: str,
    attempt: PushAttemptIdentity,
    effects,
    runtime,
    metadata: dict,
) -> str:
    recorded_sha = str(metadata.get("commit_sha") or "")
    if str(metadata.get("source_branch") or source_branch) != source_branch:
        return _finish_attempt(
            effects,
            attempt,
            "blocked",
            True,
            recorded_sha or None,
            source_branch,
            "ERROR: 已记录的 push 目标分支与当前分支不一致，拒绝继续",
            error_code="push_target_mismatch",
            retryable=False,
        )

    local_sha = _run_git(repo_dir, ["rev-parse", "HEAD"])
    if not recorded_sha:
        recorded_sha = local_sha
        metadata = {
            **attempt.result_fields(),
            "commit_sha": recorded_sha,
            "source_branch": source_branch,
            "previous_remote_sha": attempt.base_sha,
        }
        effects.record_metadata(attempt.effect_name, metadata)
    if local_sha != recorded_sha:
        return _finish_attempt(
            effects,
            attempt,
            "blocked",
            True,
            recorded_sha,
            source_branch,
            f"ERROR: 本地 HEAD {local_sha} 与待恢复 commit {recorded_sha} 不一致，拒绝生成第二个 commit",
            error_code="commit_recovery_mismatch",
            retryable=False,
        )

    remote_sha, remote_error = _remote_branch_sha(repo_dir, source_branch)
    if remote_error is not None:
        return _tool_result(
            "error", True, recorded_sha, source_branch, remote_error, attempt=attempt
        )
    if remote_sha == recorded_sha:
        return _finish_confirmed_push(
            repo_dir,
            source_branch,
            attempt,
            effects,
            runtime,
            {**metadata, "commit_sha": recorded_sha},
            f"OK: push already completed, commit={recorded_sha}",
        )
    previous_remote_sha = str(metadata.get("previous_remote_sha") or attempt.base_sha)
    if remote_sha != previous_remote_sha:
        return _finish_attempt(
            effects,
            attempt,
            "blocked",
            True,
            recorded_sha,
            source_branch,
            f"ERROR: 远端分支已从 {previous_remote_sha} 变化为 {remote_sha}，拒绝覆盖",
            error_code="remote_branch_changed",
            retryable=False,
        )
    runtime.assert_fence_sync()
    push_result = _run_git(repo_dir, ["push", "origin", source_branch])
    remote_sha, remote_error = _remote_branch_sha(repo_dir, source_branch)
    if remote_sha == recorded_sha:
        return _finish_confirmed_push(
            repo_dir,
            source_branch,
            attempt,
            effects,
            runtime,
            {**metadata, "commit_sha": recorded_sha},
            f"OK: pushed to {source_branch}, commit={recorded_sha}",
        )
    if remote_error is None and remote_sha != previous_remote_sha:
        return _finish_attempt(
            effects,
            attempt,
            "blocked",
            True,
            recorded_sha,
            source_branch,
            f"ERROR: 远端分支已从 {previous_remote_sha} 变化为 {remote_sha}，拒绝覆盖",
            error_code="remote_branch_changed",
            retryable=False,
        )
    return _tool_result(
        "error",
        True,
        recorded_sha,
        source_branch,
        push_result if push_result.startswith("ERROR:") else "ERROR: push 后远端 SHA 未更新",
        attempt=attempt,
    )


@tool
def commit_and_push_to_mr(state: Annotated[dict, InjectedState]) -> str:
    """将已生成的测试代码文件提交并推送到 MR 源分支。

    自动从当前状态获取仓库路径、patch 文件列表和源分支信息。
    执行 git add/commit/push 将测试代码推送到远端。

    返回: 成功/失败描述。
    """
    repo_dir = state.get("repo", "")
    if " (" in repo_dir:
        repo_dir = repo_dir.split(" (")[0]
    generated_patches = state.get("generated_patches") or []
    source_branch = state["source_branch"]
    mr_id = state["mr_id"]

    return commit_and_push(
        repo_dir=repo_dir,
        patch_files=generated_patches,
        source_branch=source_branch,
        mr_id=mr_id,
    )


@tool
def commit_and_push_tool(state: Annotated[dict, InjectedState]) -> str:
    """将仓库中所有新增/修改的文件提交并推送到 MR 源分支。

    自动检测仓库中所有变更文件（git add -A），提交并推送。
    返回推送结果和 commit hash。

    返回: 成功/失败描述（含 commit hash）。
    """
    from pr_agent.distributed.runtime import get_execution_runtime
    from ut_agent.tools.context import get_repo_dir

    runtime = get_execution_runtime()
    if runtime is not None:
        runtime.raise_if_canceled()

    mr_id = state.get("mr_id", 0) if state else 0
    repo_dir = get_repo_dir(mr_id)
    source_branch = state.get("source_branch", "") if state else ""

    if not repo_dir:
        message = f"ERROR: MR !{mr_id} 仓库未克隆，请先调用 clone_source_branch_tool"
        return _tool_result("error", False, None, source_branch, message)

    if not source_branch:
        return _tool_result("error", False, None, source_branch, "ERROR: 无 source_branch")

    from ut_agent.workspace import validate_state_workspace

    workspace_validation = validate_state_workspace(state, repo_dir, allow_dirty=True)
    if not workspace_validation.ok:
        return _tool_result(
            "blocked",
            False,
            None,
            source_branch,
            workspace_validation.message,
            error_code=workspace_validation.error_code,
            retryable=False,
        )

    if state and state.get("trigger_type") == "pipeline_failed":
        from ut_agent.dependency_evidence import dependency_evidence_from_messages
        from ut_agent.repair_safety import validate_member_substitutions

        evidence_sources = [
            item["content"]
            for item in dependency_evidence_from_messages(state.get("messages", []))
            if item.get("content")
        ]
        safe, safety_reason = validate_member_substitutions(repo_dir, evidence_sources)
        if not safe:
            return _tool_result(
                "blocked",
                True,
                None,
                source_branch,
                f"ERROR: {safety_reason} 请先调用 discard_workspace_tool。",
                error_code="unsupported_member_substitution",
                retryable=False,
            )

    effects = None
    runtime = None
    try:
        from pr_agent.distributed.effects import SyncEffectGuard
        from pr_agent.distributed.runtime import get_execution_runtime

        runtime = get_execution_runtime()
        if runtime is not None and runtime.mode == "queue":
            effects = SyncEffectGuard(runtime)
    except ImportError:
        pass

    # git add -A（添加所有变更）
    ret = _run_git(repo_dir, ["add", "-A"])
    if ret.startswith("ERROR:"):
        return _tool_result("error", False, None, source_branch, ret)

    previous_pushes = _previous_pushes(state or {})
    has_changes, staged_error = _has_staged_changes(repo_dir)
    if staged_error is not None:
        return _tool_result("error", False, None, source_branch, staged_error)
    if not has_changes:
        if effects is not None:
            recovered = recover_push_attempt(
                repo_dir,
                runtime.task_id,
                previous_pushes,
                _run_git,
            )
            if recovered is not None:
                effect = effects.claim(recovered.effect_name)
                if effect.status == "completed":
                    result = effect.result if isinstance(effect.result, dict) else {}
                    completed_sha = str(result.get("commit_sha") or "")
                    if completed_sha and effect.metadata.get("pushed_at"):
                        _record_confirmed_repair_commit(
                            runtime,
                            recovered,
                            {**effect.metadata, "commit_sha": completed_sha},
                        )
                    return json.dumps(effect.result, ensure_ascii=False)
                return _resume_push_attempt(
                    repo_dir,
                    source_branch,
                    recovered,
                    effects,
                    runtime,
                    dict(effect.metadata),
                )
        return _tool_result("no_changes", False, None, source_branch, "OK: 无新变更需要提交")

    task_id = runtime.task_id if effects is not None else f"local-mr-{mr_id}"
    try:
        attempt = build_push_attempt(repo_dir, task_id, previous_pushes, _run_git)
    except RuntimeError as exc:
        return _tool_result("error", False, None, source_branch, f"ERROR: {exc}")

    native_decision = _native_commit_decision(state or {})
    if native_decision is not None and not native_decision.allowed:
        return _tool_result(
            "blocked",
            True,
            None,
            source_branch,
            f"ERROR: {native_decision.message}",
            error_code=native_decision.error_code,
            retryable=True,
            attempt=attempt,
        )
    if native_decision is not None and attempt.diff_digest != native_decision.validated_diff_digest:
        return _tool_result(
            "blocked",
            True,
            None,
            source_branch,
            "ERROR: 最终暂存 Diff 与已验证 Diff 不一致，请重新检查并验证。",
            error_code="native_commit_digest_mismatch",
            retryable=True,
            attempt=attempt,
        )

    if runtime is not None:
        runtime.record_lifecycle_sync("git_publish", "start", segment_id=attempt.attempt_id)

    effect = effects.claim(attempt.effect_name) if effects is not None else None
    metadata = dict(effect.metadata) if effect is not None else {}
    if effect is not None and effect.status == "completed":
        result = effect.result if isinstance(effect.result, dict) else {}
        completed_sha = str(result.get("commit_sha") or "")
        if completed_sha and metadata.get("pushed_at"):
            _record_confirmed_repair_commit(runtime, attempt, {**metadata, "commit_sha": completed_sha})
        runtime.record_lifecycle_sync("git_publish", "end", segment_id=attempt.attempt_id)
        return json.dumps(effect.result, ensure_ascii=False)
    if metadata.get("commit_sha"):
        return _resume_push_attempt(
            repo_dir,
            source_branch,
            attempt,
            effects,
            runtime,
            metadata,
        )

    local_sha = _run_git(repo_dir, ["rev-parse", "HEAD"])
    if local_sha != attempt.base_sha:
        return _finish_attempt(
            effects,
            attempt,
            "blocked",
            True,
            None,
            source_branch,
            f"ERROR: 本地 HEAD 已从 {attempt.base_sha} 变化为 {local_sha}，拒绝提交",
            error_code="commit_recovery_mismatch",
            retryable=False,
        )
    previous_remote_sha, remote_error = _remote_branch_sha(repo_dir, source_branch)
    if remote_error is not None:
        return _tool_result("error", True, None, source_branch, remote_error, attempt=attempt)
    if previous_remote_sha != attempt.base_sha:
        return _finish_attempt(
            effects,
            attempt,
            "blocked",
            True,
            None,
            source_branch,
            f"ERROR: 远端分支已从 {attempt.base_sha} 变化为 {previous_remote_sha}，拒绝覆盖",
            error_code="remote_branch_changed",
            retryable=False,
        )
    if effects is not None:
        effects.record_metadata(
            attempt.effect_name,
            {
                **attempt.result_fields(),
                "source_branch": source_branch,
                "previous_remote_sha": previous_remote_sha,
            },
        )

    for key, value in (("user.name", "UT Agent"), ("user.email", "ut-agent@noreply.local")):
        ret = _run_git(repo_dir, ["config", key, value])
        if ret.startswith("ERROR:"):
            return _tool_result("error", True, None, source_branch, ret, attempt=attempt)

    # commit
    commit_msg = f"[UT Agent] MR !{mr_id}: 自动代码变更"
    if effects is not None:
        runtime.assert_fence_sync()
    commit_msg += f"\n\n{attempt.marker}"
    ret = _run_git(repo_dir, ["commit", "-m", commit_msg])
    if ret.startswith("ERROR:"):
        if "nothing to commit" in ret:
            return _tool_result(
                "no_changes",
                False,
                None,
                source_branch,
                "OK: 无新变更需要提交",
                attempt=attempt,
            )
        return _tool_result("error", True, None, source_branch, ret, attempt=attempt)

    commit_hash = _run_git(repo_dir, ["rev-parse", "HEAD"])
    if commit_hash.startswith("ERROR:") or commit_hash == attempt.base_sha:
        return _finish_attempt(
            effects,
            attempt,
            "blocked",
            True,
            None,
            source_branch,
            f"ERROR: commit 后未生成新 SHA: {commit_hash}",
            error_code="commit_recovery_mismatch",
            retryable=False,
        )

    if effects is not None:
        evidence = _repair_commit_metadata(repo_dir, attempt, commit_hash, source_branch)
        effects.record_metadata(
            attempt.effect_name,
            evidence,
        )
        runtime.assert_fence_sync()
    else:
        evidence = {"commit_sha": commit_hash}

    # push
    ret = _run_git(repo_dir, ["push", "origin", source_branch])
    if ret.startswith("ERROR:"):
        if effects is not None:
            remote_sha, remote_error = _remote_branch_sha(repo_dir, source_branch)
            if remote_sha == commit_hash:
                message = f"OK: push already completed, commit={commit_hash}"
                return _finish_confirmed_push(
                    repo_dir, source_branch, attempt, effects, runtime, evidence, message
                )
            if remote_error is None and previous_remote_sha and remote_sha != previous_remote_sha:
                return _finish_attempt(
                    effects,
                    attempt,
                    "blocked",
                    True,
                    commit_hash,
                    source_branch,
                    f"ERROR: 远端分支已从 {previous_remote_sha} 变化为 {remote_sha}，拒绝覆盖",
                    error_code="remote_branch_changed",
                    retryable=False,
                )
        return _tool_result("error", True, commit_hash, source_branch, ret, attempt=attempt)

    remote_sha, remote_error = _remote_branch_sha(repo_dir, source_branch)
    if remote_sha != commit_hash:
        if remote_error is None and remote_sha != previous_remote_sha:
            return _finish_attempt(
                effects,
                attempt,
                "blocked",
                True,
                commit_hash,
                source_branch,
                f"ERROR: 远端分支已从 {previous_remote_sha} 变化为 {remote_sha}，拒绝覆盖",
                error_code="remote_branch_changed",
                retryable=False,
            )
        return _tool_result(
            "error",
            True,
            commit_hash,
            source_branch,
            remote_error or "ERROR: push 后远端 SHA 未更新",
            attempt=attempt,
        )
    worktree_status = _run_git(repo_dir, ["status", "--porcelain"])
    if worktree_status.startswith("ERROR:"):
        return _tool_result("error", True, commit_hash, source_branch, worktree_status, attempt=attempt)
    if worktree_status.strip():
        return _tool_result(
            "error",
            True,
            commit_hash,
            source_branch,
            "ERROR: push 后工作区仍有未提交变更",
            attempt=attempt,
        )

    message = f"OK: pushed to {source_branch}, commit={commit_hash}"
    return _finish_confirmed_push(repo_dir, source_branch, attempt, effects, runtime, evidence, message)
