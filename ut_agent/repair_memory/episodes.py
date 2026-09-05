"""Capture one immutable episode per verified repair action.

An episode represents one ``RepairAction`` that passed exact-SHA Pipeline
validation, not an entire task. A task may fix several independent root-cause
groups, and combining them would create a non-atomic memory.

Capture is best-effort: storage and consolidation errors never change the
task's already established result. The helper applies the feature gate and
final-report dependency; the caller does not need to check them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pr_agent.feedback.timez import now_cn_iso
from pr_agent.log import get_logger
from pr_agent.triage.final_repair_report import (
    FinalRepairReportInput,
    FinalRepairReportState,
    final_repair_report_enabled,
)
from pr_agent.triage.repair_details import RepairAction, sanitize_repair_text
from pr_agent.triage.repair_rollback import RepairCommitManifest
from ut_agent.repair_memory.config import load_repair_memory_settings, project_allowed
from ut_agent.repair_memory.models import RepairEpisode
from ut_agent.repair_memory.store import save_episode
from ut_agent.repair_progress import diagnostic_fingerprint

if TYPE_CHECKING:
    from pr_agent.triage.pipeline_repair import PipelineRepairState

#: Maximum number of causal tokens extracted from root-cause text.
_MAX_CAUSAL_TOKENS = 12

#: Minimum token length to be considered a meaningful causal identifier.
_MIN_TOKEN_LENGTH = 4

#: File extension to language hint mapping. Order matters: longer extensions
#: are checked first so ``.hpp`` matches before ``.h``.
_EXTENSION_LANGUAGE: tuple[tuple[str, str], ...] = (
    (".cpp", "cpp"),
    (".cc", "cpp"),
    (".cxx", "cpp"),
    (".hpp", "cpp"),
    (".h", "cpp"),
    (".c", "cpp"),
    (".py", "python"),
    (".pyi", "python"),
    (".cmake", "cmake"),
    (".txt", "build_config"),
)

#: Job-name keywords to build-system family hints.
_JOB_BUILD_SYSTEM: tuple[tuple[str, str], ...] = (
    ("cmake", "cmake"),
    ("ninja", "cmake"),
    ("bazel", "bazel"),
    ("make", "make"),
    ("build", "cmake"),
    ("compile", "cmake"),
    ("coverage", "cmake"),
    ("format", "make"),
)

#: Bounded identifier extraction pattern. Matches word-like tokens that are
#: likely to be symbols, members, or paths mentioned in causal text.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./:-]{3,}")


def _eligible_action(
    action: RepairAction,
    value: FinalRepairReportInput,
    manifest: RepairCommitManifest,
) -> bool:
    """Return True when an action may become a retrievable project episode."""
    manifest_shas = {entry.commit_sha for entry in manifest.entries}
    return bool(
        value.final_pipeline_status == "success"
        and manifest.validate_static().ok
        and manifest.final_repair_sha == value.final_sha
        and action.status == "verified"
        and action.validation_pipeline_id == value.final_pipeline_id
        and action.commit_sha in manifest_shas
        and action.root_cause_group_id
        and action.root_cause
        and (action.solution_summary or action.measures)
        and action.changed_files
        and set(action.categories) != {"format"}
    )


def _language_hints(changed_files: tuple[str, ...]) -> tuple[str, ...]:
    """Derive language hints from changed-file extensions."""
    hints: list[str] = []
    for path in changed_files:
        lowered = path.lower()
        for extension, language in _EXTENSION_LANGUAGE:
            if lowered.endswith(extension):
                if language not in hints:
                    hints.append(language)
                break
    return tuple(hints) or ("other",)


def _build_system_hints(
    job_names: tuple[str, ...], changed_files: tuple[str, ...]
) -> tuple[str, ...]:
    """Derive build-system hints from job names and changed paths."""
    hints: list[str] = []
    for name in job_names:
        lowered = name.lower()
        for keyword, family in _JOB_BUILD_SYSTEM:
            if keyword in lowered and family not in hints:
                hints.append(family)
                break
    for path in changed_files:
        lowered = path.lower()
        if lowered.endswith((".cmake", "cmakelists.txt")) and "cmake" not in hints:
            hints.append("cmake")
        elif lowered.endswith("build.bazel") and "bazel" not in hints:
            hints.append("bazel")
        elif lowered.endswith("makefile") and "make" not in hints:
            hints.append("make")
    return tuple(hints) or ("other",)


def _causal_tokens(root_cause: str, causal_lines: tuple[str, ...]) -> tuple[str, ...]:
    """Extract bounded causal identifiers from root-cause and causal lines."""
    tokens: list[str] = []
    sources = (root_cause, *causal_lines)
    for source in sources:
        for match in _TOKEN_RE.findall(source):
            token = match.strip(".:")
            if (
                len(token) >= _MIN_TOKEN_LENGTH
                and token.lower() not in {"the", "this", "that", "with", "from", "have", "been"}
                and token not in tokens
            ):
                tokens.append(token)
            if len(tokens) >= _MAX_CAUSAL_TOKENS:
                return tuple(tokens)
    return tuple(tokens)


def _diagnostic_fingerprint(
    causal_lines: tuple[str, ...], job_names: tuple[str, ...]
) -> str:
    """Produce a normalized diagnostic fingerprint from causal lines."""
    if not causal_lines:
        return ""
    job = job_names[0] if job_names else ""
    return diagnostic_fingerprint(causal_lines[0], job_name=job)


def _episode_id(
    task_id: str,
    action_identity: str,
    final_sha: str,
    report_input_digest: str,
) -> str:
    """Build a stable episode ID from task, action, final SHA, and report digest."""
    raw = f"{task_id}:{action_identity}:{final_sha}:{report_input_digest}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"episode:{digest}"


def _has_ambiguous_overlap(actions: tuple[RepairAction, ...]) -> bool:
    """Return True when two independently described actions share changed files."""
    eligible = [action for action in actions if action.changed_files]
    for index, first in enumerate(eligible):
        for second in eligible[index + 1 :]:
            if (
                first.root_cause_group_id
                and second.root_cause_group_id
                and first.root_cause_group_id != second.root_cause_group_id
                and set(first.changed_files) & set(second.changed_files)
            ):
                return True
    return False


def build_verified_repair_episodes(
    value: FinalRepairReportInput,
    state: FinalRepairReportState,
    manifest: RepairCommitManifest,
    actions: Iterable[RepairAction],
) -> tuple[RepairEpisode, ...]:
    """Build eligible, sanitized episodes for one verified repair task.

    Returns one episode per verified unambiguous action. Actions that fail any
    eligibility gate are omitted. Never raises; callers should treat an empty
    tuple as "no eligible episode".
    """
    try:
        actions_tuple = tuple(actions)
        if not actions_tuple:
            return ()
        if _has_ambiguous_overlap(actions_tuple):
            return ()
        report_input_digest = value.digest()
        state_input_digest = state.input_digest or report_input_digest
        if state_input_digest != report_input_digest:
            return ()
        episodes: list[RepairEpisode] = []
        for action in actions_tuple:
            if not _eligible_action(action, value, manifest):
                continue
            action_identity = action.action_id or action.root_cause_group_id
            if not action_identity:
                continue
            fingerprint_source = action.root_cause or (value.causal_lines[0] if value.causal_lines else "")
            fingerprint = diagnostic_fingerprint(
                fingerprint_source,
                job_name=action.job_names[0] if action.job_names else "",
            )
            tokens = _causal_tokens(action.root_cause, value.causal_lines)
            episode = RepairEpisode(
                episode_id=_episode_id(
                    value.repair_task_id, action_identity, value.final_sha, report_input_digest
                ),
                task_id=value.repair_task_id,
                action_identity=action_identity,
                root_cause_group_id=action.root_cause_group_id,
                project=value.project_id,
                mr_iid=value.mr_iid,
                source_pipeline_id=value.source_pipeline_id,
                source_sha=value.source_sha,
                final_pipeline_id=value.final_pipeline_id,
                final_sha=value.final_sha,
                categories=tuple(action.categories),
                job_names=tuple(action.job_names),
                language_hints=_language_hints(action.changed_files),
                build_system_hints=_build_system_hints(action.job_names, action.changed_files),
                diagnostic_fingerprint=fingerprint,
                causal_tokens=tokens,
                root_cause=sanitize_repair_text(action.root_cause, 500),
                solution_summary=sanitize_repair_text(action.solution_summary, 500),
                measures=tuple(sanitize_repair_text(item, 400) for item in action.measures),
                changed_files=tuple(sanitize_repair_text(item, 300) for item in action.changed_files),
                report_input_digest=report_input_digest,
                report_source=state.report.source if state.report is not None else "model",
                eligibility_reason="eligible",
                created_at=now_cn_iso(),
            )
            episodes.append(episode)
        return tuple(episodes)
    except Exception as error:
        get_logger().error(f"Failed to build repair memory episodes: {type(error).__name__}")
        return ()


def record_verified_repair_episodes(
    value: FinalRepairReportInput,
    state: FinalRepairReportState,
    manifest: RepairCommitManifest,
    repair_state: "PipelineRepairState",
    *,
    path: str | None = None,
) -> tuple[str, ...]:
    """Build and persist eligible episodes idempotently. Never raises.

    Applies the feature gate and final-report dependency. Returns persisted
    episode IDs; an empty tuple means no episode was eligible or capture is
    disabled. Storage failures return an empty tuple without affecting repair.
    """
    try:
        settings = load_repair_memory_settings()
        if not settings.capture_enabled:
            return ()
        if not project_allowed(value.project_id, settings.project_allowlist):
            return ()
        if not final_repair_report_enabled():
            return ()
        episodes = build_verified_repair_episodes(
            value, state, manifest, repair_state.repair_actions
        )
        return tuple(episode.episode_id for episode in episodes if save_episode(episode, path))
    except Exception as error:
        get_logger().error(f"Failed to record repair memory episodes: {type(error).__name__}")
        return ()
