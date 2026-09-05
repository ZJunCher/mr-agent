"""Deterministic Native Repair evidence reduction and commit readiness checks."""

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class NativeRepairEvidence:
    last_patch_sequence: int = -1
    last_patch_status: str = ""
    last_patch_work_item_id: str = ""
    base_sha: str = ""
    diff_digest: str = ""
    changed_files: tuple[str, ...] = ()
    failed_patch_after_success: bool = False
    total_lines: int = 0
    covered_intervals: tuple[tuple[int, int], ...] = ()
    diff_review_complete: bool = False
    last_inspect_sequence: int = -1
    validation_sequence: int = -1
    validation_status: str = ""
    validated_diff_digest: str = ""
    required_checks: tuple[str, ...] = ()
    executed_checks: tuple[str, ...] = ()
    all_passed: bool = False
    validation_error_code: str = ""


@dataclass(frozen=True)
class NativeCommitDecision:
    allowed: bool
    error_code: str = ""
    message: str = ""
    validated_diff_digest: str = ""
    validated_base_sha: str = ""
    next_start_line: int | None = None


@dataclass
class _Reduction:
    evidence: NativeRepairEvidence
    diff_review_stale: bool = False
    validation_stale: bool = False
    missing_required_checks: tuple[str, ...] = ()
    failed_required_checks: tuple[str, ...] = ()


def _merge_intervals(intervals: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)


def _next_start_line(intervals: tuple[tuple[int, int], ...], total_lines: int) -> int | None:
    expected = 1
    for start, end in intervals:
        if start > expected:
            return expected
        expected = max(expected, end + 1)
    return expected if expected <= total_lines else None


def _result(attempt: Any) -> dict[str, Any]:
    value = getattr(attempt, "result", None)
    return value if isinstance(value, dict) else {}


def _replace_evidence(evidence: NativeRepairEvidence, **updates: Any) -> NativeRepairEvidence:
    values = {field: getattr(evidence, field) for field in evidence.__dataclass_fields__}
    values.update(updates)
    return NativeRepairEvidence(**values)


def _successful_patch(result: dict[str, Any]) -> bool:
    return (
        result.get("status") == "changed"
        and result.get("patch_applied") is True
        and bool(result.get("base_sha"))
        and bool(result.get("diff_digest"))
    )


def _reduce_native_repair_evidence(attempts: list[Any]) -> _Reduction:
    reduction = _Reduction(NativeRepairEvidence())
    required_passed: dict[str, bool] = {}

    for attempt in attempts:
        name = str(getattr(attempt, "name", ""))
        sequence = int(getattr(attempt, "sequence", -1))
        result = _result(attempt)
        evidence = reduction.evidence

        if name == "discard_workspace_tool" and result.get("status") == "success":
            reduction = _Reduction(NativeRepairEvidence())
            required_passed = {}
            continue

        if name in {"apply_repo_patch_tool", "apply_format_report_tool"}:
            status = str(result.get("status") or "")
            if _successful_patch(result):
                reduction = _Reduction(NativeRepairEvidence(
                    last_patch_sequence=sequence,
                    last_patch_status=status,
                    last_patch_work_item_id=str(result.get("work_item_id") or ""),
                    base_sha=str(result["base_sha"]),
                    diff_digest=str(result["diff_digest"]),
                    changed_files=tuple(str(path) for path in result.get("changed_files") or () if str(path)),
                ))
                required_passed = {}
            elif evidence.diff_digest:
                reduction.evidence = _replace_evidence(
                    evidence,
                    last_patch_sequence=sequence,
                    last_patch_status=status,
                    failed_patch_after_success=True,
                )
            else:
                reduction.evidence = _replace_evidence(
                    evidence,
                    last_patch_sequence=sequence,
                    last_patch_status=status,
                )
            continue

        if name == "inspect_repo_diff_tool" and evidence.diff_digest and result.get("status") == "ok":
            reduction.evidence = _replace_evidence(evidence, last_inspect_sequence=sequence)
            evidence = reduction.evidence
            same_identity = (
                str(result.get("base_sha") or "") == evidence.base_sha
                and str(result.get("diff_digest") or "") == evidence.diff_digest
            )
            page = result.get("page") if isinstance(result.get("page"), dict) else {}
            try:
                total_lines = int(result.get("total_lines") or 0)
                start_line = int(page.get("start_line") or 0)
                end_line = int(page.get("end_line") or 0)
            except (TypeError, ValueError):
                total_lines = start_line = end_line = 0
            valid_range = total_lines > 0 and 1 <= start_line <= end_line <= total_lines
            consistent_total = evidence.total_lines in {0, total_lines}
            if not same_identity or not valid_range or not consistent_total:
                reduction.diff_review_stale = True
                continue
            intervals = _merge_intervals((*evidence.covered_intervals, (start_line, end_line)))
            reduction.evidence = _replace_evidence(
                evidence,
                total_lines=total_lines,
                covered_intervals=intervals,
                diff_review_complete=_next_start_line(intervals, total_lines) is None,
            )
            reduction.diff_review_stale = False
            continue

        if name == "run_repo_validation_tool" and evidence.diff_digest:
            status = str(result.get("status") or "")
            error_code = str(result.get("error_code") or "")
            validated_digest = str(result.get("validated_diff_digest") or "")
            required = tuple(dict.fromkeys(
                str(check) for check in result.get("required_checks") or () if str(check)
            ))
            executed_results = [item for item in result.get("executed_checks") or () if isinstance(item, dict)]
            executed_names = tuple(dict.fromkeys(
                str(item.get("name") or item.get("check") or "")
                for item in executed_results
                if str(item.get("name") or item.get("check") or "")
            ))
            required_passed = {
                str(item.get("name") or item.get("check") or ""): item.get("passed") is True
                for item in executed_results
                if str(item.get("name") or item.get("check") or "")
            }
            reduction.evidence = _replace_evidence(
                evidence,
                validation_sequence=sequence,
                validation_status=status,
                validated_diff_digest=validated_digest,
                required_checks=required,
                executed_checks=executed_names,
                all_passed=result.get("all_passed") is True,
                validation_error_code=error_code,
            )
            evidence = reduction.evidence
            same_identity = (
                str(result.get("base_sha") or "") == evidence.base_sha
                and validated_digest == evidence.diff_digest
            )
            reduction.validation_stale = not evidence.diff_review_complete or not same_identity
            reduction.missing_required_checks = tuple(check for check in required if check not in executed_names)
            reduction.failed_required_checks = tuple(
                check for check in required if check in executed_names and not required_passed.get(check, False)
            )

    return reduction


def build_native_repair_evidence(attempts: list[Any]) -> NativeRepairEvidence:
    """Reduce ordered tool attempts to the current Native Repair evidence."""
    return _reduce_native_repair_evidence(attempts).evidence


def evaluate_native_commit(attempts: list[Any]) -> NativeCommitDecision:
    """Fail closed unless the current patch was fully inspected and successfully validated."""
    reduction = _reduce_native_repair_evidence(attempts)
    evidence = reduction.evidence

    if not evidence.diff_digest:
        return NativeCommitDecision(
            False,
            "native_patch_missing",
            "请先调用 apply_repo_patch_tool 成功应用补丁并取得 Diff 身份。",
        )
    if evidence.failed_patch_after_success:
        return NativeCommitDecision(
            False,
            "native_patch_failed_after_success",
            "最后一次 apply_repo_patch_tool 失败；请重新成功应用补丁，或丢弃工作区修改。",
        )
    if reduction.diff_review_stale:
        return NativeCommitDecision(
            False,
            "native_diff_review_stale",
            "inspect_repo_diff_tool 返回的基线、Diff 哈希、总行数或页范围与当前补丁不一致；"
            "请重新检查。",
            next_start_line=_next_start_line(evidence.covered_intervals, evidence.total_lines),
        )
    if not evidence.diff_review_complete:
        next_line = _next_start_line(evidence.covered_intervals, evidence.total_lines) or 1
        return NativeCommitDecision(
            False,
            "native_diff_review_incomplete",
            f"Diff 尚未完整检查；请调用 inspect_repo_diff_tool(start_line={next_line})。",
            next_start_line=next_line,
        )
    if evidence.validation_sequence < 0:
        return NativeCommitDecision(
            False,
            "native_validation_missing",
            "请在完整检查 Diff 后调用 run_repo_validation_tool。",
        )
    if evidence.validation_error_code == "validation_profile_missing":
        return NativeCommitDecision(
            False,
            "native_validation_profile_missing",
            "项目缺少必需的验证 profile，不能以较弱检查替代真实测试。",
        )
    if reduction.validation_stale:
        return NativeCommitDecision(
            False,
            "native_validation_stale",
            "验证早于完整 Diff 审阅，或验证的基线/Diff 哈希与当前补丁不一致；"
            "请重新验证。",
        )
    if reduction.missing_required_checks:
        missing = ", ".join(reduction.missing_required_checks)
        return NativeCommitDecision(
            False,
            "native_validation_checks_missing",
            f"验证结果缺少必需检查：{missing}；请重新调用 run_repo_validation_tool。",
        )
    if evidence.validation_status != "ok" or not evidence.all_passed or reduction.failed_required_checks:
        return NativeCommitDecision(
            False,
            "native_validation_failed",
            "至少一项必需验证未通过，请修复失败后重新检查并验证。",
        )
    return NativeCommitDecision(
        True,
        validated_diff_digest=evidence.validated_diff_digest,
        validated_base_sha=evidence.base_sha,
    )
