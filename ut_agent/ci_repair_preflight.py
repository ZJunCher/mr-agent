"""Exact-pattern CI blocker classification for high-confidence preflight outcomes."""

import re
from collections.abc import Iterable

_UPLOAD_PACK_CANCELED = re.compile(
    r"\brunning\s+upload-pack\s*:\s*user\s+canceled\s+the\s+request\b",
    re.IGNORECASE,
)
_REMOTE_BRANCH_NOT_FOUND = re.compile(
    r"\bfatal:\s+remote branch\s+(?P<branch>.+?)\s+not found in upstream origin\b",
    re.IGNORECASE,
)
_EMPTY_GIT_REVISION = re.compile(
    r"\bgit diff failed:\s*fatal:\s*ambiguous argument\s+['\"]{2}",
    re.IGNORECASE,
)
_CI_DEPS_FETCH_FAILED = re.compile(
    r"\bci_deps file not found or download failed\s*\(HTTP\s+\d+\)",
    re.IGNORECASE,
)
_MISSING_PACKAGE_CONFIG = re.compile(
    r"\bcould not find a package configuration file provided by\b"
    r"|\bfind a package configuration file provided by\b"
    r"|\bcould not find\s+\S+Config\.cmake\b",
    re.IGNORECASE,
)


def _ordered_lines(causal_lines: Iterable[object]) -> tuple[str, ...]:
    return tuple(line.strip() for value in causal_lines if isinstance(value, str) and (line := value.strip()))


def _primary_line(causal_lines: Iterable[object]) -> str:
    from ut_agent.ci_diagnostics import extract_diagnostic_candidates, primary_diagnostic

    lines = _ordered_lines(causal_lines)
    candidates = extract_diagnostic_candidates("\n".join(lines), limit=max(1, len(lines))).candidates
    primary = primary_diagnostic(candidates)
    return primary.text if primary is not None else ""


def build_ci_environment_blocker(job_name: str, causal_lines: Iterable[object]) -> dict | None:
    """Return a complete blocker only for an exact, high-confidence Git transport cancellation."""
    matched = _primary_line(causal_lines)
    if not _UPLOAD_PACK_CANCELED.search(matched):
        return None
    return {
        "schema_version": 1,
        "outcome": "blocked",
        "job_name": job_name,
        "blocker_type": "ci_environment",
        "root_cause": "CI Runner 在编译前拉取 Git 依赖时连接被取消。",
        "ci_evidence": [{"job_name": job_name, "observation": matched}],
        "repository_evidence": [{
            "kind": "execution_stage",
            "locator": "current MR workspace",
            "observation": "错误发生在 Git upload-pack 传输阶段，没有指向可修改的仓库源码。",
        }],
        "attempted_repairs": ["评估仓库内修改，确认业务代码无法修复 Git 传输中断。"],
        "why_no_safe_repo_change": "修改 MR 源码不会恢复 Runner 与 GitLab 之间被取消的拉取连接。",
        "suggested_action": "重新运行流水线；若重复出现，请检查 GitLab Runner 网络和依赖仓库服务。",
    }


def _deps_distribution_blocker(job_name: str, lines: tuple[str, ...]) -> dict | None:
    """Detect the exact combination: ci_deps artifact 404 fallback followed by a missing package config."""
    deps_line = next((line for line in lines if _CI_DEPS_FETCH_FAILED.search(line)), "")
    if not deps_line:
        return None
    consequence = next((line for line in lines if _MISSING_PACKAGE_CONFIG.search(line)), "")
    if not consequence:
        return None
    return {
        "schema_version": 1,
        "outcome": "blocked",
        "job_name": job_name,
        "blocker_type": "ci_environment",
        "root_cause": "CI 依赖分发制品（ci_deps deps.yml）下载失败并回退到默认配置，导致构建缺少所需依赖。",
        "ci_evidence": [
            {"job_name": job_name, "observation": deps_line},
            {"job_name": job_name, "observation": consequence},
        ],
        "repository_evidence": [{
            "kind": "execution_stage",
            "locator": "dependency provisioning",
            "observation": "失败发生在 CI 依赖分发与安装阶段，业务代码尚未进入编译。",
        }],
        "attempted_repairs": ["核对失败阶段，确认缺失依赖由 ci_deps 制品回退引起，与 MR 代码无关。"],
        "why_no_safe_repo_change": "缺失的 deps.yml 制品由外部 CI 依赖分发系统提供，修改本仓库代码无法恢复该制品。",
        "suggested_action": "检查 ci_deps 制品服务中对应分支 deps.yml 的可用性，恢复后重新运行流水线。",
    }


def build_ci_repair_preflight(job_name: str, causal_lines: Iterable[object]) -> dict | None:
    """Return a blocker only when the strongest observation has an exact, non-code meaning."""
    lines = _ordered_lines(causal_lines)
    deps_blocker = _deps_distribution_blocker(job_name, lines)
    if deps_blocker is not None:
        return deps_blocker
    matched = _primary_line(causal_lines)
    branch_match = _REMOTE_BRANCH_NOT_FOUND.search(matched)
    if branch_match:
        return {
            "schema_version": 1,
            "outcome": "blocked",
            "job_name": job_name,
            "blocker_type": "external_dependency",
            "root_cause": f"CI 配置引用的依赖分支不存在：{branch_match.group('branch')}。",
            "ci_evidence": [{"job_name": job_name, "observation": matched}],
            "repository_evidence": [{
                "kind": "execution_stage",
                "locator": "dependency checkout",
                "observation": "构建在拉取声明依赖时终止，尚未进入业务代码编译阶段。",
            }],
            "attempted_repairs": ["检查失败发生阶段，确认当前没有可验证的替代分支。"],
            "why_no_safe_repo_change": "系统无法从现有证据确定应替换为哪个分支，自动猜测会改变依赖版本。",
            "suggested_action": "恢复该依赖分支，或由维护者在依赖清单中指定确认过的替代分支。",
        }
    if _EMPTY_GIT_REVISION.search(matched):
        return {
            "schema_version": 1,
            "outcome": "blocked",
            "job_name": job_name,
            "blocker_type": "ci_environment",
            "root_cause": "格式检查 Job 使用的 Git 基准版本为空，因此检查脚本自身失败。",
            "ci_evidence": [{"job_name": job_name, "observation": matched}],
            "repository_evidence": [{
                "kind": "execution_stage",
                "locator": "format job",
                "observation": "Job 未生成格式报告，自动格式修复没有可应用的输入。",
            }],
            "attempted_repairs": ["检查格式 Job 输出，确认失败发生在生成格式报告之前。"],
            "why_no_safe_repo_change": "业务代码格式工具没有获得有效 diff 基准，无法从该 Job 推导代码改动。",
            "suggested_action": "修正 CI 模板中的 diff 基准变量后重新运行流水线。",
        }
    return build_ci_environment_blocker(job_name, causal_lines)
