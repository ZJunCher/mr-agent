"""Strict, owner-facing repair reports and bounded Git diff facts."""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Iterable

REPAIR_REPORT_START = "<REPAIR_REPORT>"
REPAIR_REPORT_END = "</REPAIR_REPORT>"
_SUMMARY_LIMIT = 500
_FILE_SUMMARY_LIMIT = 400
_MAX_FILE_EXPLANATIONS = 40
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$")


def _sanitize(value: object, limit: int) -> str:
    from pr_agent.triage.repair_details import sanitize_repair_text

    return sanitize_repair_text(value, limit)


def _safe_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path or path.startswith("/") or "\x00" in path:
        return ""
    normalized = posixpath.normpath(path)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return ""
    return _sanitize(normalized, 300)


@dataclass(frozen=True)
class RepairFileExplanation:
    path: str
    summary: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RepairExplanation:
    root_cause_summary: str
    solution_summary: str
    rationale: str
    file_explanations: tuple[RepairFileExplanation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "root_cause_summary": self.root_cause_summary,
            "solution_summary": self.solution_summary,
            "rationale": self.rationale,
            "file_explanations": [item.to_dict() for item in self.file_explanations],
        }


def _final_report_payload(text: str) -> dict[str, Any] | None:
    end = str(text or "").rfind(REPAIR_REPORT_END)
    if end < 0:
        return None
    start = str(text or "").rfind(REPAIR_REPORT_START, 0, end)
    if start < 0:
        return None
    raw = str(text)[start + len(REPAIR_REPORT_START):end].strip()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def parse_repair_report(text: str, changed_files: Iterable[str]) -> RepairExplanation | None:
    """Parse the final valid owner report and correlate it with real Git changes."""
    payload = _final_report_payload(text)
    if payload is None or payload.get("schema_version") != 1:
        return None

    root_cause = _sanitize(payload.get("root_cause_summary"), _SUMMARY_LIMIT)
    solution = _sanitize(payload.get("solution_summary"), _SUMMARY_LIMIT)
    rationale = _sanitize(payload.get("rationale"), _SUMMARY_LIMIT)
    if not root_cause or not solution or not rationale:
        return None

    real_paths = {_safe_path(path) for path in changed_files}
    real_paths.discard("")
    explanations = []
    seen_paths = set()
    raw_explanations = payload.get("file_explanations")
    if isinstance(raw_explanations, list):
        for item in raw_explanations:
            if not isinstance(item, dict):
                continue
            path = _safe_path(item.get("path"))
            summary = _sanitize(item.get("summary"), _FILE_SUMMARY_LIMIT)
            if not path or path not in real_paths or path in seen_paths or not summary:
                continue
            explanations.append(RepairFileExplanation(path, summary))
            seen_paths.add(path)
            if len(explanations) >= _MAX_FILE_EXPLANATIONS:
                break

    return RepairExplanation(root_cause, solution, rationale, tuple(explanations))


def _repo_path(repo_dir: str, value: object) -> str:
    repo = os.path.realpath(repo_dir)
    raw = str(value or "").strip()
    absolute = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(repo, raw))
    try:
        if os.path.commonpath((repo, absolute)) != repo:
            return ""
    except ValueError:
        return ""
    return _safe_path(os.path.relpath(absolute, repo))


def _run_git_diff(repo_dir: str, path: str) -> str:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", path],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=10,
    ).returncode == 0
    command = ["git", "diff", "--no-ext-diff", "--no-color", "--unified=3"]
    if tracked:
        command.extend(["HEAD", "--", path])
    else:
        command.extend(["--no-index", "--", "/dev/null", path])
    result = subprocess.run(command, cwd=repo_dir, capture_output=True, text=True, errors="replace", timeout=15)
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip()[:300] or f"git diff exit={result.returncode}")
    return result.stdout


def _change_type(patch: str) -> str:
    if "\nnew file mode " in f"\n{patch}":
        return "added"
    if "\ndeleted file mode " in f"\n{patch}":
        return "deleted"
    if "\nrename from " in f"\n{patch}" and "\nrename to " in f"\n{patch}":
        return "renamed"
    return "modified"


def _parse_patch(
    path: str,
    patch: str,
    *,
    max_hunks_per_file: int,
    max_lines_per_file: int,
    max_line_chars: int,
) -> dict[str, Any] | None:
    if not patch.strip():
        return None
    binary = "Binary files " in patch or "GIT binary patch" in patch
    output: dict[str, Any] = {
        "path": path,
        "change_type": _change_type(patch),
        "additions": 0,
        "deletions": 0,
        "binary": binary,
        "truncated": False,
        "omitted_lines": 0,
        "hunks": [],
    }
    if binary:
        return output

    current_hunk: dict[str, Any] | None = None
    old_line = new_line = 0
    stored_lines = 0
    for raw_line in patch.splitlines():
        match = _HUNK_HEADER_RE.match(raw_line)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(3))
            if len(output["hunks"]) >= max_hunks_per_file:
                output["truncated"] = True
                current_hunk = None
                continue
            current_hunk = {
                "old_start": old_line,
                "new_start": new_line,
                "header": _sanitize(match.group(5) or "", 200),
                "lines": [],
            }
            output["hunks"].append(current_hunk)
            continue
        if current_hunk is None or not raw_line or raw_line.startswith("\\ No newline"):
            continue
        prefix = raw_line[0]
        if prefix not in {" ", "+", "-"}:
            continue
        kind = {" ": "context", "+": "addition", "-": "deletion"}[prefix]
        line_old = old_line if kind != "addition" else None
        line_new = new_line if kind != "deletion" else None
        if kind == "addition":
            output["additions"] += 1
            new_line += 1
        elif kind == "deletion":
            output["deletions"] += 1
            old_line += 1
        else:
            old_line += 1
            new_line += 1
        if stored_lines >= max_lines_per_file:
            output["truncated"] = True
            output["omitted_lines"] += 1
            continue
        content = raw_line[1:]
        if len(content) > max_line_chars:
            content = content[:max_line_chars]
            output["truncated"] = True
        current_hunk["lines"].append({
            "kind": kind,
            "old_line": line_old,
            "new_line": line_new,
            "content": content,
        })
        stored_lines += 1
    return output


def capture_repair_diff(
    repo_dir: str,
    changed_files: Iterable[str],
    *,
    max_files: int | None = None,
    max_hunks_per_file: int | None = None,
    max_lines_per_file: int | None = None,
    max_line_chars: int | None = None,
) -> tuple[dict[str, Any], ...]:
    """Capture a bounded, structured diff for the actual changed paths."""
    from pr_agent.config_loader import get_settings

    settings = get_settings()
    max_files = max(1, int(max_files or settings.get("FEISHU.REPAIR_DETAILS_DIFF_MAX_FILES", 30) or 30))
    max_hunks_per_file = max(
        1,
        int(max_hunks_per_file or settings.get("FEISHU.REPAIR_DETAILS_DIFF_MAX_HUNKS_PER_FILE", 20) or 20),
    )
    max_lines_per_file = max(
        1,
        int(max_lines_per_file or settings.get("FEISHU.REPAIR_DETAILS_DIFF_MAX_LINES_PER_FILE", 400) or 400),
    )
    max_line_chars = max(
        16,
        int(
            max_line_chars
            if max_line_chars is not None
            else settings.get("FEISHU.REPAIR_DETAILS_DIFF_MAX_LINE_CHARS", 500) or 500
        ),
    )
    paths = sorted({path for value in changed_files if (path := _repo_path(repo_dir, value))})[:max_files]
    output = []
    for path in paths:
        try:
            patch = _run_git_diff(repo_dir, path)
            parsed = _parse_patch(
                path,
                patch,
                max_hunks_per_file=max_hunks_per_file,
                max_lines_per_file=max_lines_per_file,
                max_line_chars=max_line_chars,
            )
            if parsed is not None:
                output.append(parsed)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            try:
                from pr_agent.log import get_logger

                get_logger().warning(f"Unable to capture repair diff path={path}: {str(exc)[:300]}")
            except Exception:
                pass
    return tuple(output)
