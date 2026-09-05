"""Consolidate verified episodes into project memories and promote proven globals.

Consolidation runs outside the live repair path. The final-report worker records
pending episodes; a bounded batch command processes pending or retryable failures.
This prevents a memory-model outage from delaying repair completion and gives
operators a deterministic retry surface.

The consolidator receives only a sanitized episode, never raw logs or full diffs.
It emits schema-versioned JSON with controlled values. The stable pattern key is
a hash of schema version plus the five controlled classifications. A pattern
containing ``other`` may remain project-scoped but is not eligible for global
promotion.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field

from pr_agent.feedback.store import _connect, get_db_path
from pr_agent.log import get_logger
from pr_agent.storage.sqlite import run_write_transaction
from ut_agent.repair_memory.config import load_repair_memory_settings
from ut_agent.repair_memory.models import (
    MEMORY_SCHEMA_VERSION,
    EmbeddingStatus,
    MemoryScope,
    MemoryStatus,
    RepairEpisode,
    RepairMemory,
    RepairMemoryEmbedding,
)
from ut_agent.repair_memory.store import (
    ClaimedEpisode,
    claim_pending_episodes,
    commit_legacy_memory_migration,
    list_legacy_memories,
    list_memory_supporting_episodes,
    mark_episode_consolidated,
    mark_episode_failed,
    mark_episode_invalid,
    mark_legacy_memory_needs_review,
    record_legacy_migration_failure,
)
from ut_agent.structured_output import StrictOutputModel, call_structured_output

#: Controlled taxonomy allowlists. A candidate containing any value outside
#: these sets is rejected. ``other`` is allowed for project scope but blocks
#: global promotion.
LANGUAGES = frozenset({"cpp", "python", "build_config", "other"})
BUILD_SYSTEMS = frozenset({"cmake", "bazel", "make", "python_packaging", "other"})
FAILURE_FAMILIES = frozenset(
    {
        "missing_member",
        "missing_header",
        "undefined_symbol",
        "type_mismatch",
        "test_assertion",
        "dependency_api_drift",
        "build_config",
        "other",
    }
)
ROOT_CAUSE_CLASSES = frozenset(
    {
        "interface_drift",
        "missing_dependency",
        "incorrect_test_assumption",
        "production_bug",
        "build_config_mismatch",
        "other",
    }
)
REPAIR_ACTION_CLASSES = frozenset(
    {
        "align_current_interface",
        "add_dependency",
        "adjust_test_or_mock",
        "fix_production_logic",
        "update_build_config",
        "other",
    }
)

#: Maximum items per list field in a candidate.
_MAX_LIST_ITEMS = 5

#: Maximum text length per string field in a candidate.
_MAX_TEXT_LENGTH = 500

#: Pattern key length (truncated SHA-256 hex digest).
_PATTERN_KEY_LENGTH = 24

#: Secret/path markers that must never appear in a global candidate.
_SECRET_MARKERS = frozenset({"api_key", "token", "password", "secret", "bearer", "authorization"})

# Claude may add this exact presentation envelope even when the relay receives
# ``response_format={"type": "json_object"}``. Only a full-body JSON fence is
# unwrapped; prose, multiple fences, and all other Markdown remain invalid.
_FULL_JSON_FENCE_RE = re.compile(
    r"\A```json[ \t]*\r?\n(?P<body>\{.*\})\r?\n```[ \t]*\Z",
    re.DOTALL,
)

# At least one CJK unified ideograph is required in each user-visible
# explanation. Technical identifiers and compiler output may remain verbatim as
# long as the surrounding explanation is Chinese.
_HAN_TEXT_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class MemoryCandidateValidationError(ValueError):
    """Raised when a consolidation candidate fails schema or taxonomy validation."""


class GlobalMemoryLeakError(ValueError):
    """Raised when a global candidate contains project-private identifiers."""


class RetryableConsolidationError(RuntimeError):
    """Raised when consolidation fails due to a transient provider error."""


_BoundedCandidateText = Annotated[str, Field(min_length=1, max_length=_MAX_TEXT_LENGTH)]


class RepairMemoryCandidateOutput(StrictOutputModel):
    """Strict model-owned structure before Repair Memory semantic validation."""

    schema_version: Literal[1]
    language: Literal["cpp", "python", "build_config", "other"]
    build_system: Literal["cmake", "bazel", "make", "python_packaging", "other"]
    failure_family: Literal[
        "missing_member",
        "missing_header",
        "undefined_symbol",
        "type_mismatch",
        "test_assertion",
        "dependency_api_drift",
        "build_config",
        "other",
    ]
    root_cause_class: Literal[
        "interface_drift",
        "missing_dependency",
        "incorrect_test_assumption",
        "production_bug",
        "build_config_mismatch",
        "other",
    ]
    repair_action_class: Literal[
        "align_current_interface",
        "add_dependency",
        "adjust_test_or_mock",
        "fix_production_logic",
        "update_build_config",
        "other",
    ]
    problem_pattern: _BoundedCandidateText
    applicability: list[_BoundedCandidateText] = Field(max_length=_MAX_LIST_ITEMS)
    anti_conditions: list[_BoundedCandidateText] = Field(max_length=_MAX_LIST_ITEMS)
    repair_guidance: _BoundedCandidateText
    validation_guidance: list[_BoundedCandidateText] = Field(max_length=_MAX_LIST_ITEMS)


@dataclass(frozen=True)
class MemoryCandidate:
    """A parsed, validated consolidation candidate ready for storage."""

    schema_version: int
    language: str
    build_system: str
    failure_family: str
    root_cause_class: str
    repair_action_class: str
    problem_pattern: str
    applicability: tuple[str, ...]
    anti_conditions: tuple[str, ...]
    repair_guidance: str
    validation_guidance: tuple[str, ...]


@dataclass(frozen=True)
class BatchSummary:
    """Counts from one consolidation batch run."""

    claimed: int
    completed: int
    failed: int
    invalid: int


@dataclass(frozen=True)
class PromotionSummary:
    """Counts from one global promotion run."""

    promoted: int
    skipped: int


@dataclass(frozen=True)
class LegacyMigrationSummary:
    """Counts from one bounded legacy-memory migration batch."""

    selected: int = 0
    migrated: int = 0
    marked_for_review: int = 0
    failed: int = 0


def _validate_taxonomy(field: str, value: str, allowlist: frozenset[str]) -> str:
    if value not in allowlist:
        raise MemoryCandidateValidationError(field)
    return value


def _validate_text(value: Any, field: str, limit: int = _MAX_TEXT_LENGTH) -> str:
    text = str(value or "").strip()
    if not text:
        raise MemoryCandidateValidationError(field)
    if len(text) > limit:
        raise MemoryCandidateValidationError(field)
    return text


def _validate_list(values: Any, field: str, limit: int = _MAX_LIST_ITEMS) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise MemoryCandidateValidationError(field)
    if len(values) > limit:
        raise MemoryCandidateValidationError(field)
    output: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            raise MemoryCandidateValidationError(field)
        if len(text) > _MAX_TEXT_LENGTH:
            raise MemoryCandidateValidationError(field)
        output.append(text)
    return tuple(output)


def contains_han_text(value: str) -> bool:
    """Return whether ``value`` contains Chinese explanatory text."""
    return _HAN_TEXT_RE.search(value) is not None


def _validate_chinese_text(value: Any, field: str) -> str:
    text = _validate_text(value, field)
    if not contains_han_text(text):
        raise MemoryCandidateValidationError(f"content_locale:{field}")
    return text


def _validate_chinese_list(values: Any, field: str) -> tuple[str, ...]:
    output = _validate_list(values, field)
    if any(not contains_han_text(item) for item in output):
        raise MemoryCandidateValidationError(f"content_locale:{field}")
    return output


def candidate_from_structured_output(value: RepairMemoryCandidateOutput) -> MemoryCandidate:
    """Apply content safety rules after strict structural validation."""
    payload = value.model_dump(mode="json")
    lowered = json.dumps(payload, ensure_ascii=False).lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise MemoryCandidateValidationError(f"secret_marker:{marker}")
    return MemoryCandidate(
        schema_version=MEMORY_SCHEMA_VERSION,
        language=_validate_taxonomy("language", value.language, LANGUAGES),
        build_system=_validate_taxonomy("build_system", value.build_system, BUILD_SYSTEMS),
        failure_family=_validate_taxonomy("failure_family", value.failure_family, FAILURE_FAMILIES),
        root_cause_class=_validate_taxonomy("root_cause_class", value.root_cause_class, ROOT_CAUSE_CLASSES),
        repair_action_class=_validate_taxonomy(
            "repair_action_class", value.repair_action_class, REPAIR_ACTION_CLASSES
        ),
        problem_pattern=_validate_chinese_text(value.problem_pattern, "problem_pattern"),
        applicability=_validate_chinese_list(value.applicability, "applicability"),
        anti_conditions=_validate_chinese_list(value.anti_conditions, "anti_conditions"),
        repair_guidance=_validate_chinese_text(value.repair_guidance, "repair_guidance"),
        validation_guidance=_validate_chinese_list(value.validation_guidance, "validation_guidance"),
    )


def parse_memory_candidate(text: str) -> MemoryCandidate:
    """Parse and validate one consolidation candidate JSON object.

    Rejects Markdown fences, missing fields, extra taxonomy values, secret
    markers, and oversized text. Never silently coerces invalid data.
    """
    if not text or not text.strip():
        raise MemoryCandidateValidationError("empty")
    stripped = text.strip()
    if stripped.startswith("```"):
        raise MemoryCandidateValidationError("markdown_fence")
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise MemoryCandidateValidationError("invalid_json") from error
    if not isinstance(decoded, dict):
        raise MemoryCandidateValidationError("not_object")

    if int(decoded.get("schema_version") or 0) != MEMORY_SCHEMA_VERSION:
        raise MemoryCandidateValidationError("schema_version")

    lowered = json.dumps(decoded, ensure_ascii=False).lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise MemoryCandidateValidationError(f"secret_marker:{marker}")

    return MemoryCandidate(
        schema_version=MEMORY_SCHEMA_VERSION,
        language=_validate_taxonomy("language", str(decoded.get("language") or ""), LANGUAGES),
        build_system=_validate_taxonomy(
            "build_system", str(decoded.get("build_system") or ""), BUILD_SYSTEMS
        ),
        failure_family=_validate_taxonomy(
            "failure_family", str(decoded.get("failure_family") or ""), FAILURE_FAMILIES
        ),
        root_cause_class=_validate_taxonomy(
            "root_cause_class", str(decoded.get("root_cause_class") or ""), ROOT_CAUSE_CLASSES
        ),
        repair_action_class=_validate_taxonomy(
            "repair_action_class",
            str(decoded.get("repair_action_class") or ""),
            REPAIR_ACTION_CLASSES,
        ),
        problem_pattern=_validate_chinese_text(decoded.get("problem_pattern"), "problem_pattern"),
        applicability=_validate_chinese_list(decoded.get("applicability"), "applicability"),
        anti_conditions=_validate_chinese_list(decoded.get("anti_conditions"), "anti_conditions"),
        repair_guidance=_validate_chinese_text(decoded.get("repair_guidance"), "repair_guidance"),
        validation_guidance=_validate_chinese_list(decoded.get("validation_guidance"), "validation_guidance"),
    )


def _parse_model_candidate_response(text: str) -> MemoryCandidate:
    """Remove one canonical transport fence, then apply strict validation."""
    stripped = str(text or "").strip()
    match = _FULL_JSON_FENCE_RE.fullmatch(stripped)
    if match is not None:
        stripped = match.group("body")
    return parse_memory_candidate(stripped)


def candidate_from_payload(payload: dict[str, Any]) -> MemoryCandidate:
    """Parse a candidate from a dict payload (convenience for tests)."""
    return parse_memory_candidate(json.dumps(payload, ensure_ascii=False))


def pattern_key_for(candidate: MemoryCandidate) -> str:
    """Return a stable pattern key derived from the five controlled classifications.

    Free-text fields (problem_pattern, applicability, etc.) do not affect the key,
    so rewording the same pattern produces the same key.
    """
    raw = (
        f"v{candidate.schema_version}:"
        f"{candidate.language}:{candidate.build_system}:"
        f"{candidate.failure_family}:{candidate.root_cause_class}:"
        f"{candidate.repair_action_class}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_PATTERN_KEY_LENGTH]


def _is_promotable(candidate: MemoryCandidate) -> bool:
    """Return True when a candidate's taxonomy is eligible for global promotion."""
    return (
        candidate.language != "other"
        and candidate.build_system != "other"
        and candidate.failure_family != "other"
        and candidate.root_cause_class != "other"
        and candidate.repair_action_class != "other"
    )


def _sanitized_episode_payload(episode: ClaimedEpisode | RepairEpisode) -> dict[str, Any]:
    """Return bounded repair evidence without project, MR, SHA, URL, or source paths."""
    return {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "language_hints": list(episode.language_hints),
        "build_system_hints": list(episode.build_system_hints),
        "failure_family_hint": _classify_failure_family(episode.causal_tokens),
        "diagnostic_fingerprint": episode.diagnostic_fingerprint,
        "causal_tokens": list(episode.causal_tokens[:8]),
        "root_cause": episode.root_cause,
        "solution_summary": episode.solution_summary,
        "measures": list(episode.measures),
        "changed_file_extensions": sorted(
            {ext for path in episode.changed_files for ext in [path[path.rfind(".") :].lower()] if "." in path}
        ),
    }


def _build_consolidation_input(episode: ClaimedEpisode) -> str:
    """Build the bounded user prompt for one episode."""
    payload = _sanitized_episode_payload(episode)
    return (
        "Convert the verified repair episode below into one generic Repair Memory.\n"
        f"{_CANDIDATE_CONTRACT}\n"
        "[VERIFIED_REPAIR_EPISODE]\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "[/VERIFIED_REPAIR_EPISODE]"
    )


def _build_correction_input(
    episode: ClaimedEpisode,
    error_code: str,
    attempt: int,
) -> str:
    """Build a bounded correction request without echoing rejected model text."""
    return (
        f"Correction attempt {attempt} of 2. The previous response failed validation: "
        f"{error_code[:120]}.\n"
        f"{_build_consolidation_input(episode)}"
    )


def _classify_failure_family(causal_tokens: tuple[str, ...]) -> str:
    """Heuristic hint for the consolidator; not the final classification."""
    text = " ".join(causal_tokens).lower()
    if "member" in text:
        return "missing_member"
    if "header" in text or "file" in text:
        return "missing_header"
    if "reference" in text or "undefined" in text:
        return "undefined_symbol"
    if "type" in text or "convert" in text:
        return "type_mismatch"
    if "assert" in text:
        return "test_assertion"
    if "cmake" in text or "build" in text:
        return "build_config"
    return "other"


def _memory_from_candidate(
    candidate: MemoryCandidate,
    *,
    scope: MemoryScope,
    scope_key: str,
    episode: ClaimedEpisode,
    confidence: float,
    support_episode_count: int,
    support_project_count: int,
) -> RepairMemory:
    """Build a ``RepairMemory`` from a candidate and supporting episode."""
    from pr_agent.feedback.timez import now_cn_iso

    now = now_cn_iso()
    return RepairMemory(
        memory_id=f"mem:{scope.value}:{scope_key}:{pattern_key_for(candidate)}",
        scope=scope,
        scope_key=scope_key,
        pattern_key=pattern_key_for(candidate),
        pattern_version=candidate.schema_version,
        language=candidate.language,
        build_system=candidate.build_system,
        failure_family=candidate.failure_family,
        root_cause_class=candidate.root_cause_class,
        repair_action_class=candidate.repair_action_class,
        diagnostic_fingerprint=episode.diagnostic_fingerprint,
        causal_tokens=episode.causal_tokens,
        problem_pattern=candidate.problem_pattern,
        applicability=candidate.applicability,
        anti_conditions=candidate.anti_conditions,
        repair_guidance=candidate.repair_guidance,
        validation_guidance=candidate.validation_guidance,
        confidence=confidence,
        support_episode_count=support_episode_count,
        support_project_count=support_project_count,
        settled_attempts=0,
        immediate_successes=0,
        status=MemoryStatus.ACTIVE,
        content_locale="zh-CN",
        created_at=now,
        updated_at=now,
        last_reinforced_at=now,
    )


def _upsert_project_memory(
    candidate: MemoryCandidate,
    episode: ClaimedEpisode,
    *,
    path: str,
) -> bool:
    """Upsert a project memory and link evidence in one transaction.

    Recomputes support counts from unique evidence rows and sets pre-outcome
    confidence to ``min(0.80, base + (support-1)*increment)``.
    """
    settings = load_repair_memory_settings()
    base = settings.project_initial_confidence
    increment = settings.support_confidence_increment

    def write(conn) -> bool:
        pattern_key = pattern_key_for(candidate)
        conn.row_factory = None
        existing = conn.execute(
            "SELECT memory_id, support_episode_count, settled_attempts, immediate_successes "
            "FROM repair_memories "
            "WHERE scope = 'project' AND scope_key = ? AND pattern_key = ? "
            "AND status = 'active'",
            (episode.project, pattern_key),
        ).fetchone()
        if existing is None:
            support_count = 1
            confidence = min(0.80, base)
            memory = _memory_from_candidate(
                candidate,
                scope=MemoryScope.PROJECT,
                scope_key=episode.project,
                episode=episode,
                confidence=confidence,
                support_episode_count=support_count,
                support_project_count=1,
            )
            placeholders = ",".join("?" for _ in range(29))
            conn.execute(
                f"INSERT OR REPLACE INTO repair_memories ({_MEMORY_COLUMNS}) VALUES ({placeholders})",
                _memory_to_row(memory),
            )
            conn.execute(
                "INSERT OR IGNORE INTO repair_memory_evidence "
                "(memory_id, episode_id, relation, created_at) VALUES (?, ?, ?, ?)",
                (memory.memory_id, episode.episode_id, "support", _now_iso()),
            )
        else:
            memory_id = existing[0]
            conn.execute(
                "INSERT OR IGNORE INTO repair_memory_evidence "
                "(memory_id, episode_id, relation, created_at) VALUES (?, ?, ?, ?)",
                (memory_id, episode.episode_id, "support", _now_iso()),
            )
            support_count = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT episode_id) FROM repair_memory_evidence WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()[0]
            )
            settled = int(existing[2])
            successes = int(existing[3])
            confidence = min(
                0.95,
                max(
                    0.30,
                    min(0.80, base + max(0, support_count - 1) * increment)
                    + successes * settings.success_confidence_increment
                    - (settled - successes) * settings.failure_confidence_decrement,
                ),
            )
            conn.execute(
                "UPDATE repair_memories SET confidence = ?, support_episode_count = ?, "
                "updated_at = ?, last_reinforced_at = ? WHERE memory_id = ?",
                (confidence, support_count, _now_iso(), _now_iso(), memory_id),
            )
        return True

    try:
        return run_write_transaction(path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to upsert project memory: {type(error).__name__}")
        return False


def validate_global_candidate(
    candidate: MemoryCandidate,
    supporting_episodes: tuple[dict[str, Any], ...],
) -> MemoryCandidate:
    """Validate that a global candidate contains no project-private identifiers.

    Rejects any candidate whose text contains a supporting project name, MR,
    source path, specific internal symbol, raw diagnostic line, or secret marker.
    """
    full_text = json.dumps(
        {
            "problem_pattern": candidate.problem_pattern,
            "applicability": list(candidate.applicability),
            "anti_conditions": list(candidate.anti_conditions),
            "repair_guidance": candidate.repair_guidance,
            "validation_guidance": list(candidate.validation_guidance),
        },
        ensure_ascii=False,
    ).lower()

    for episode in supporting_episodes:
        project = str(episode.get("project") or "").lower()
        if project and project in full_text:
            raise GlobalMemoryLeakError(f"project name leaked: {project}")
        for path in episode.get("changed_files", ()):
            lowered_path = str(path).lower()
            if len(lowered_path) >= 4 and lowered_path in full_text:
                raise GlobalMemoryLeakError(f"source path leaked: {lowered_path}")

    for marker in _SECRET_MARKERS:
        if marker in full_text:
            raise GlobalMemoryLeakError(f"secret marker leaked: {marker}")

    return candidate


def _find_promotable_patterns(path: str) -> tuple[tuple[str, MemoryCandidate], ...]:
    """Find project patterns with support from at least two distinct projects.

    Returns ``(pattern_key, representative_candidate)`` pairs. The candidate is
    reconstructed from the first active project memory's stored fields.
    """
    settings = load_repair_memory_settings()
    min_projects = settings.global_min_projects

    conn = _connect(path)
    try:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT pattern_key, COUNT(DISTINCT scope_key) as project_count "
            "FROM repair_memories "
            "WHERE scope = 'project' AND status = 'active' "
            "GROUP BY pattern_key HAVING project_count >= ?",
            (min_projects,),
        ).fetchall()
        promotable: list[tuple[str, MemoryCandidate]] = []
        for row in rows:
            pattern_key = row[0]
            first = conn.execute(
                "SELECT language, build_system, failure_family, root_cause_class, "
                "repair_action_class, problem_pattern, applicability_json, "
                "anti_conditions_json, repair_guidance, validation_guidance_json "
                "FROM repair_memories "
                "WHERE scope = 'project' AND status = 'active' AND pattern_key = ? "
                "LIMIT 1",
                (pattern_key,),
            ).fetchone()
            if first is None:
                continue
            candidate = MemoryCandidate(
                schema_version=MEMORY_SCHEMA_VERSION,
                language=first[0],
                build_system=first[1],
                failure_family=first[2],
                root_cause_class=first[3],
                repair_action_class=first[4],
                problem_pattern=first[5],
                applicability=tuple(json.loads(first[6] or "[]")),
                anti_conditions=tuple(json.loads(first[7] or "[]")),
                repair_guidance=first[8],
                validation_guidance=tuple(json.loads(first[9] or "[]")),
            )
            if not _is_promotable(candidate):
                continue
            promotable.append((pattern_key, candidate))
        return tuple(promotable)
    except Exception as error:
        get_logger().error(f"Failed to find promotable patterns: {type(error).__name__}")
        return ()
    finally:
        conn.close()


def _build_global_promotion_input(candidate: MemoryCandidate) -> str:
    """Build a self-contained prompt from one sanitized project candidate."""
    payload = {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "language": candidate.language,
        "build_system": candidate.build_system,
        "failure_family": candidate.failure_family,
        "root_cause_class": candidate.root_cause_class,
        "repair_action_class": candidate.repair_action_class,
        "problem_pattern": candidate.problem_pattern,
        "applicability": list(candidate.applicability),
        "anti_conditions": list(candidate.anti_conditions),
        "repair_guidance": candidate.repair_guidance,
        "validation_guidance": list(candidate.validation_guidance),
    }
    return (
        "Generalize the project Repair Memory below into one project-independent Repair Memory.\n"
        f"{_CANDIDATE_CONTRACT}\n"
        "[PROJECT_REPAIR_MEMORY]\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "[/PROJECT_REPAIR_MEMORY]"
    )


async def consolidate_episode(
    episode: ClaimedEpisode,
    *,
    llm_call: Callable[..., Awaitable[Any]],
) -> MemoryCandidate:
    """Consolidate one episode into a validated candidate via LLM.

    Provider/model unavailability is retryable. A completed invalid response
    receives at most two bounded correction attempts before becoming invalid.
    """
    settings = load_repair_memory_settings()
    system = _CONSOLIDATION_SYSTEM_PROMPT
    validation_error: MemoryCandidateValidationError | None = None
    for attempt in range(3):
        user = (
            _build_consolidation_input(episode)
            if attempt == 0
            else _build_correction_input(episode, str(validation_error or "unknown"), attempt)
        )
        try:
            async with asyncio.timeout(settings.consolidation_model_timeout_seconds):
                return await _request_memory_candidate(system, user, llm_call=llm_call)
        except MemoryCandidateValidationError as error:
            validation_error = error
    raise validation_error or MemoryCandidateValidationError("unknown")


def _invalid_candidate_error_code(error: MemoryCandidateValidationError) -> str:
    """Return a bounded audit code for the exact rejected candidate field."""
    field = str(error).strip() or "unknown"
    return f"invalid_candidate:{field}"[:120]


def run_consolidation_batch(
    limit: int,
    owner: str,
    path: str | None = None,
    *,
    llm_call: Callable[..., Awaitable[Any]],
    lease_seconds: int | None = None,
) -> BatchSummary:
    """Claim and consolidate pending episodes synchronously for tests.

    Returns a summary of claimed, completed, failed, and invalid counts.
    Never raises; transient failures leave episodes retryable.
    """
    db_path = path or get_db_path()
    effective_lease_seconds = (
        lease_seconds
        if lease_seconds is not None
        else load_repair_memory_settings().consolidation_lease_seconds
    )
    claimed = claim_pending_episodes(
        owner,
        limit=limit,
        lease_seconds=effective_lease_seconds,
        path=db_path,
    )
    if not claimed:
        return BatchSummary(claimed=0, completed=0, failed=0, invalid=0)

    completed = 0
    failed = 0
    invalid = 0
    for episode in claimed:
        try:
            candidate = asyncio.run(consolidate_episode(episode, llm_call=llm_call))
        except RetryableConsolidationError:
            mark_episode_failed(episode.episode_id, "provider_unavailable", path=db_path)
            failed += 1
            continue
        except MemoryCandidateValidationError as error:
            error_code = _invalid_candidate_error_code(error)
            mark_episode_invalid(episode.episode_id, error_code, path=db_path)
            get_logger().warning(
                f"Repair memory candidate rejected: episode_id={episode.episode_id} "
                f"error_code={error_code}"
            )
            invalid += 1
            continue
        except Exception as error:
            mark_episode_failed(episode.episode_id, type(error).__name__, path=db_path)
            failed += 1
            continue

        if _upsert_project_memory(candidate, episode, path=db_path):
            mark_episode_consolidated(episode.episode_id, path=db_path)
            completed += 1
        else:
            mark_episode_failed(episode.episode_id, "store_error", path=db_path)
            failed += 1

    return BatchSummary(
        claimed=len(claimed), completed=completed, failed=failed, invalid=invalid
    )


def _build_legacy_migration_input(memory: RepairMemory, episode: RepairEpisode) -> str:
    """Build a Chinese regeneration prompt from generic old text and verified evidence."""
    legacy_payload = {
        "schema_version": memory.pattern_version,
        "language": memory.language,
        "build_system": memory.build_system,
        "failure_family": memory.failure_family,
        "root_cause_class": memory.root_cause_class,
        "repair_action_class": memory.repair_action_class,
        "problem_pattern": memory.problem_pattern,
        "applicability": list(memory.applicability),
        "anti_conditions": list(memory.anti_conditions),
        "repair_guidance": memory.repair_guidance,
        "validation_guidance": list(memory.validation_guidance),
    }
    return (
        "Regenerate the legacy Repair Memory as concise Simplified Chinese using the "
        "verified supporting episode. Preserve technical identifiers verbatim.\n"
        f"{_CANDIDATE_CONTRACT}\n"
        "[LEGACY_REPAIR_MEMORY]\n"
        f"{json.dumps(legacy_payload, ensure_ascii=False, indent=2)}\n"
        "[/LEGACY_REPAIR_MEMORY]\n"
        "[SUPPORTING_EPISODE]\n"
        f"{json.dumps(_sanitized_episode_payload(episode), ensure_ascii=False, indent=2)}\n"
        "[/SUPPORTING_EPISODE]"
    )


async def _regenerate_legacy_candidate(
    memory: RepairMemory,
    episode: RepairEpisode,
    *,
    llm_call: Callable[..., Awaitable[Any]],
) -> MemoryCandidate:
    settings = load_repair_memory_settings()
    base_prompt = _build_legacy_migration_input(memory, episode)
    validation_error: MemoryCandidateValidationError | None = None
    for attempt in range(3):
        user = base_prompt
        if attempt:
            user = (
                f"Correction attempt {attempt} of 2. The previous response failed validation: "
                f"{str(validation_error or 'unknown')[:120]}.\n{base_prompt}"
            )
        try:
            async with asyncio.timeout(settings.consolidation_model_timeout_seconds):
                return await _request_memory_candidate(
                    _CONSOLIDATION_SYSTEM_PROMPT,
                    user,
                    llm_call=llm_call,
                )
        except MemoryCandidateValidationError as error:
            validation_error = error
    raise validation_error or MemoryCandidateValidationError("unknown")


def _legacy_replacement(
    memory: RepairMemory,
    candidate: MemoryCandidate,
) -> tuple[RepairMemory, RepairMemoryEmbedding]:
    from pr_agent.feedback.timez import now_cn_iso
    from ut_agent.repair_memory.embedding import build_memory_embedding_text, embedding_source_hash

    settings = load_repair_memory_settings()
    now = now_cn_iso()
    next_version = memory.pattern_version + 1
    replacement = RepairMemory(
        memory_id=f"{memory.memory_id}:zh-v{next_version}",
        scope=memory.scope,
        scope_key=memory.scope_key,
        pattern_key=memory.pattern_key,
        pattern_version=next_version,
        language=candidate.language,
        build_system=candidate.build_system,
        failure_family=candidate.failure_family,
        root_cause_class=candidate.root_cause_class,
        repair_action_class=candidate.repair_action_class,
        diagnostic_fingerprint=memory.diagnostic_fingerprint,
        causal_tokens=memory.causal_tokens,
        problem_pattern=candidate.problem_pattern,
        applicability=candidate.applicability,
        anti_conditions=candidate.anti_conditions,
        repair_guidance=candidate.repair_guidance,
        validation_guidance=candidate.validation_guidance,
        confidence=memory.confidence,
        support_episode_count=memory.support_episode_count,
        support_project_count=memory.support_project_count,
        settled_attempts=memory.settled_attempts,
        immediate_successes=memory.immediate_successes,
        status=MemoryStatus.ACTIVE,
        content_locale="zh-CN",
        supersedes_id=memory.memory_id,
        manual_reason="legacy_memory_migration",
        created_at=now,
        updated_at=now,
        last_reinforced_at=memory.last_reinforced_at or now,
    )
    embedding_text = build_memory_embedding_text(replacement)
    pending_embedding = RepairMemoryEmbedding(
        memory_id=replacement.memory_id,
        model_name=settings.embedding_model_name,
        model_revision=settings.embedding_model_revision,
        dimensions=settings.embedding_dimensions,
        vector_blob=b"",
        source_hash=embedding_source_hash(
            embedding_text,
            model_name=settings.embedding_model_name,
            model_revision=settings.embedding_model_revision,
        ),
        status=EmbeddingStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    return replacement, pending_embedding


def migrate_legacy_memories(
    *,
    limit: int,
    owner: str,
    llm_call: Callable[..., Awaitable[Any]],
    path: str | None = None,
) -> LegacyMigrationSummary:
    """Regenerate a bounded set of legacy memories from verified evidence."""
    if limit <= 0:
        return LegacyMigrationSummary()
    selected = list_legacy_memories(limit=limit, path=path)
    migrated = marked_for_review = failed = 0
    for memory in selected:
        episodes = list_memory_supporting_episodes(memory.memory_id, path)
        if not episodes:
            if mark_legacy_memory_needs_review(memory.memory_id, path):
                marked_for_review += 1
            else:
                failed += 1
            continue
        try:
            candidate = asyncio.run(
                _regenerate_legacy_candidate(memory, episodes[0], llm_call=llm_call)
            )
            replacement, pending_embedding = _legacy_replacement(memory, candidate)
            if commit_legacy_memory_migration(
                memory.memory_id,
                replacement,
                pending_embedding,
                owner=owner,
                path=path,
            ):
                migrated += 1
                continue
            error_code = "store_error"
        except Exception as error:
            error_code = type(error).__name__
        record_legacy_migration_failure(
            memory.memory_id,
            error_code,
            owner=owner,
            path=path,
        )
        failed += 1
    return LegacyMigrationSummary(
        selected=len(selected),
        migrated=migrated,
        marked_for_review=marked_for_review,
        failed=failed,
    )


async def promote_ready_patterns(
    path: str | None = None,
    *,
    dry_run: bool = False,
    llm_call: Callable[..., Awaitable[Any]] | None = None,
) -> PromotionSummary:
    """Promote project patterns with independent support from >= 2 projects.

    Sends only already sanitized project candidate fields with all project/MR/SHA/path
    identifiers removed, asks for one generic candidate, and validates de-identification
    against every supporting episode. Re-running promotion must not create a second
    active global row.
    """
    db_path = path or get_db_path()
    promotable = _find_promotable_patterns(db_path)
    if not promotable:
        return PromotionSummary(promoted=0, skipped=0)

    settings = load_repair_memory_settings()
    promoted = 0
    skipped = 0

    for pattern_key, project_candidate in promotable:
        existing_global = _active_global_for_pattern(db_path, pattern_key)
        if existing_global is not None:
            skipped += 1
            continue

        episodes = _supporting_episodes_for_pattern(db_path, pattern_key)
        if len(episodes) < settings.global_min_projects:
            skipped += 1
            continue

        try:
            if llm_call is not None:
                async with asyncio.timeout(settings.consolidation_model_timeout_seconds):
                    candidate = await _request_memory_candidate(
                        _CONSOLIDATION_SYSTEM_PROMPT,
                        _build_global_promotion_input(project_candidate),
                        llm_call=llm_call,
                    )
            else:
                candidate = project_candidate

            validate_global_candidate(candidate, episodes)
        except Exception as error:
            get_logger().warning(
                f"Global promotion rejected for pattern {pattern_key}: {type(error).__name__}"
            )
            skipped += 1
            continue

        if dry_run:
            promoted += 1
            continue

        global_memory = _build_global_memory(candidate, pattern_key, episodes)
        if _upsert_global_memory(global_memory, episodes, path=db_path):
            promoted += 1
        else:
            skipped += 1

    return PromotionSummary(promoted=promoted, skipped=skipped)


def _active_global_for_pattern(path: str, pattern_key: str) -> str | None:
    """Return the memory_id of an active global memory for ``pattern_key``."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT memory_id FROM repair_memories "
            "WHERE scope = 'global' AND pattern_key = ? AND status = 'active'",
            (pattern_key,),
        ).fetchone()
        return row[0] if row is not None else None
    except Exception:
        return None
    finally:
        conn.close()


def _supporting_episodes_for_pattern(
    path: str, pattern_key: str
) -> tuple[dict[str, Any], ...]:
    """Return supporting episodes with project and changed_files for de-identification."""
    conn = _connect(path)
    try:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT e.episode_id, e.changed_files_json, e.project "
            "FROM repair_memory_evidence ev "
            "JOIN repair_memory_episodes e ON e.episode_id = ev.episode_id "
            "JOIN repair_memories m ON m.memory_id = ev.memory_id "
            "WHERE m.pattern_key = ? AND m.status = 'active'",
            (pattern_key,),
        ).fetchall()
        return tuple(
            {
                "episode_id": row[0],
                "changed_files": tuple(json.loads(row[1] or "[]")),
                "project": row[2],
            }
            for row in rows
        )
    except Exception:
        return ()
    finally:
        conn.close()


def _build_global_memory(
    candidate: MemoryCandidate,
    pattern_key: str,
    episodes: tuple[dict[str, Any], ...],
) -> RepairMemory:
    """Build a global ``RepairMemory`` from a validated candidate."""
    from pr_agent.feedback.timez import now_cn_iso

    now = now_cn_iso()
    projects = {ep["project"] for ep in episodes}
    return RepairMemory(
        memory_id=f"mem:global:{pattern_key}",
        scope=MemoryScope.GLOBAL,
        scope_key="*",
        pattern_key=pattern_key,
        pattern_version=candidate.schema_version,
        language=candidate.language,
        build_system=candidate.build_system,
        failure_family=candidate.failure_family,
        root_cause_class=candidate.root_cause_class,
        repair_action_class=candidate.repair_action_class,
        diagnostic_fingerprint="",
        causal_tokens=(),
        problem_pattern=candidate.problem_pattern,
        applicability=candidate.applicability,
        anti_conditions=candidate.anti_conditions,
        repair_guidance=candidate.repair_guidance,
        validation_guidance=candidate.validation_guidance,
        confidence=load_repair_memory_settings().global_initial_confidence,
        support_episode_count=len(episodes),
        support_project_count=len(projects),
        settled_attempts=0,
        immediate_successes=0,
        status=MemoryStatus.ACTIVE,
        content_locale="zh-CN",
        created_at=now,
        updated_at=now,
        last_reinforced_at=now,
    )


def _upsert_global_memory(
    memory: RepairMemory,
    episodes: tuple[dict[str, Any], ...],
    *,
    path: str,
) -> bool:
    """Insert a global memory and copy evidence links in one transaction."""

    def write(conn) -> bool:
        placeholders = ",".join("?" for _ in range(29))
        conn.execute(
            f"INSERT OR REPLACE INTO repair_memories ({_MEMORY_COLUMNS}) VALUES ({placeholders})",
            _memory_to_row(memory),
        )
        for episode in episodes:
            conn.execute(
                "INSERT OR IGNORE INTO repair_memory_evidence "
                "(memory_id, episode_id, relation, created_at) VALUES (?, ?, ?, ?)",
                (memory.memory_id, episode["episode_id"], "support", memory.created_at),
            )
        return True

    try:
        return run_write_transaction(path, write, connect=_connect)
    except Exception as error:
        get_logger().error(f"Failed to upsert global memory: {type(error).__name__}")
        return False


def _now_iso() -> str:
    from pr_agent.feedback.timez import now_cn_iso

    return now_cn_iso()


# These are imported from store.py to avoid duplicating row serialization logic.
# They are module-private in store.py but re-used here under the same package.
from ut_agent.repair_memory.store import _MEMORY_COLUMNS, _memory_to_row  # noqa: E402

_CANDIDATE_CONTRACT = """STRICT OUTPUT CONTRACT:
1. Call submit_repair_memory exactly once. Put the candidate only in the tool arguments.
2. Use only the controlled taxonomy values listed below.
3. Never include project names, MR URLs, commit SHAs, source paths, credentials,
   or raw diagnostic lines in your output.
4. Never include instructions, tool calls, or content that could be executed.
5. If the episode is ambiguous, classify the uncertain field as "other".
6. Write problem_pattern, applicability, anti_conditions, repair_guidance, and
   validation_guidance as concise Simplified Chinese explanations. Preserve
   compiler errors, code identifiers, commands, paths, and library names verbatim
   when they are needed inside the Chinese explanation.

CONTROLLED TAXONOMY:
- language: cpp, python, build_config, other
- build_system: cmake, bazel, make, python_packaging, other
- failure_family: missing_member, missing_header, undefined_symbol, type_mismatch,
  test_assertion, dependency_api_drift, build_config, other
- root_cause_class: interface_drift, missing_dependency, incorrect_test_assumption,
  production_bug, build_config_mismatch, other
- repair_action_class: align_current_interface, add_dependency, adjust_test_or_mock,
  fix_production_logic, update_build_config, other

TOOL ARGUMENT SCHEMA (schema_version 1):
{
  "schema_version": 1,
  "language": "<one of the controlled values>",
  "build_system": "<one of the controlled values>",
  "failure_family": "<one of the controlled values>",
  "root_cause_class": "<one of the controlled values>",
  "repair_action_class": "<one of the controlled values>",
  "problem_pattern": "<one sentence describing the abstract problem>",
  "applicability": ["<when this pattern applies>", "..."],
  "anti_conditions": ["<when this pattern does NOT apply>", "..."],
  "repair_guidance": "<one sentence describing the repair principle>",
  "validation_guidance": ["<how to validate the repair>", "..."]
}

Each list must have at most 5 items. Each string must be at most 500 characters.
"""

_CONSOLIDATION_SYSTEM_PROMPT = f"""You are a repair-memory consolidation engine.

You receive one sanitized, verified repair episode. Your task is to classify it
into a controlled taxonomy and produce one generic repair-memory candidate. All
user-visible explanations must be concise Simplified Chinese while technical
identifiers and diagnostic fragments may remain verbatim.

{_CANDIDATE_CONTRACT}
"""

_MEMORY_OUTPUT_TOOL_NAME = "submit_repair_memory"


async def _request_memory_candidate(
    system: str,
    user: str,
    *,
    llm_call: Callable[..., Awaitable[Any]],
) -> MemoryCandidate:
    outcome = await call_structured_output(
        system,
        user,
        output_model=RepairMemoryCandidateOutput,
        tool_name=_MEMORY_OUTPUT_TOOL_NAME,
        tool_description="提交经过抽象和脱敏的 Repair Memory 候选。",
        llm_call=llm_call,
        temperature=0.0,
        max_tokens=600,
    )
    if outcome.terminal_error:
        raise RetryableConsolidationError(outcome.terminal_error)
    if outcome.value is None:
        raise MemoryCandidateValidationError(outcome.validation_error or "tool_protocol")
    return candidate_from_structured_output(outcome.value)
