"""Structured evidence contract for terminal CI triage blockers."""

import json

BLOCKER_JSON_BEGIN = "BEGIN_TRIAGE_BLOCKER_JSON"
BLOCKER_JSON_END = "END_TRIAGE_BLOCKER_JSON"

ALLOWED_BLOCKER_TYPES = {
    "external_dependency",
    "ci_environment",
    "provider_outage",
    "permissions",
    "missing_required_input",
    "unsupported_repository_state",
}


def _is_non_empty_string(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_blocker_record(record: object, job_name: str) -> str | None:
    """Return a validation error, or ``None`` when blocker evidence is complete."""
    if not isinstance(record, dict):
        return "blocker JSON 必须是对象"
    if record.get("schema_version") != 1:
        return "blocker schema_version 必须为 1"
    if record.get("outcome") != "blocked":
        return 'blocker outcome 必须为 "blocked"'
    if record.get("job_name") != job_name:
        return f"blocker job_name 必须匹配当前失败 job: {job_name}"
    if record.get("blocker_type") not in ALLOWED_BLOCKER_TYPES:
        return "blocker_type 不受支持"

    for field in ("root_cause", "why_no_safe_repo_change", "suggested_action"):
        if not _is_non_empty_string(record.get(field)):
            return f"blocker {field} 不能为空"

    ci_evidence = record.get("ci_evidence")
    if not isinstance(ci_evidence, list) or not ci_evidence:
        return "blocker CI 证据不能为空"
    for item in ci_evidence:
        if not isinstance(item, dict):
            return "blocker CI 证据项必须是对象"
        if item.get("job_name") != job_name:
            return f"blocker CI 证据必须匹配当前失败 job: {job_name}"
        if not _is_non_empty_string(item.get("observation")):
            return "blocker CI 证据 observation 不能为空"

    repository_evidence = record.get("repository_evidence")
    if not isinstance(repository_evidence, list) or not repository_evidence:
        return "blocker 仓库证据不能为空"
    for item in repository_evidence:
        if not isinstance(item, dict):
            return "blocker 仓库证据项必须是对象"
        for field in ("kind", "locator", "observation"):
            if not _is_non_empty_string(item.get(field)):
                return f"blocker 仓库证据 {field} 不能为空"

    attempted_repairs = record.get("attempted_repairs")
    if not isinstance(attempted_repairs, list) or not attempted_repairs:
        return "blocker attempted_repairs 不能为空"
    if any(not _is_non_empty_string(item) for item in attempted_repairs):
        return "blocker attempted_repairs 每一项都必须是非空字符串"

    return None


def parse_blocker_record(text: str, job_name: str) -> tuple[dict | None, str | None]:
    """Parse the final marker-delimited blocker record from Hermes output."""
    start = text.rfind(BLOCKER_JSON_BEGIN)
    if start < 0:
        return None, "缺少 blocker JSON 起始标记"

    payload_start = start + len(BLOCKER_JSON_BEGIN)
    end = text.find(BLOCKER_JSON_END, payload_start)
    if end < 0:
        return None, "缺少 blocker JSON 结束标记"

    try:
        record = json.loads(text[payload_start:end].strip())
    except json.JSONDecodeError as error:
        return None, f"blocker JSON 无法解析: {error.msg}"

    validation_error = validate_blocker_record(record, job_name)
    if validation_error:
        return None, validation_error
    return record, None
