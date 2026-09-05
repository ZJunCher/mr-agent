"""Parse and validate ``[repair_memory]`` settings.

``parse_repair_memory_settings`` is a pure function that validates a raw mapping
and returns a frozen ``RepairMemorySettings``. It does not import Dynaconf, so
unit tests can exercise it without triggering the global settings load chain.

``load_repair_memory_settings`` is the runtime entry point that reads the
Dynaconf ``REPAIR_MEMORY`` section and delegates to the pure parser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ut_agent.repair_memory.models import RetrievalMode

#: Default database path when no override is supplied. Production should mount
#: this on a persistent host volume. Tests always pass an explicit path.
DEFAULT_MEMORY_DB_PATH = "/app/data/feedback/repair_memory.db"


@dataclass(frozen=True)
class RepairMemorySettings:
    """Validated repair-memory configuration.

    All confidence values are bounded to ``[0.0, 1.0]``. All limit values are
    positive integers. ``project_allowlist`` is a tuple; an empty tuple enables
    no project, while ``("*",)`` explicitly enables all projects.
    """

    capture_enabled: bool
    retrieval_mode: RetrievalMode
    promotion_enabled: bool
    project_allowlist: tuple[str, ...]
    max_hints: int
    max_prompt_chars: int
    min_score: int
    retrieval_timeout_ms: int
    stale_days: int
    global_min_projects: int
    needs_review_min_attempts: int
    needs_review_confidence: float
    episode_retention_days: int
    hit_retention_days: int
    consolidation_batch_size: int
    consolidation_lease_seconds: int
    consolidation_model_timeout_seconds: int
    consolidation_poll_seconds: int
    embedding_service_url: str
    embedding_model_name: str
    embedding_model_revision: str
    embedding_dimensions: int
    semantic_timeout_ms: int
    embedding_batch_timeout_seconds: float
    embedding_batch_size: int
    semantic_min_similarity: float
    semantic_candidate_limit_per_scope: int
    project_initial_confidence: float
    global_initial_confidence: float
    support_confidence_increment: float
    success_confidence_increment: float
    failure_confidence_decrement: float


def project_allowed(project: str, allowlist: tuple[str, ...]) -> bool:
    """Return True when ``project`` is permitted by the allowlist.

    An empty allowlist enables nothing. ``"*"`` explicitly enables all projects.
    A non-empty allowlist without ``"*"`` enables only exact matches.
    """
    return bool(project and ("*" in allowlist or project in allowlist))


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise ValueError(f"repair_memory.{key} must be a boolean, got {value!r}")


def _as_int(value: Any, key: str, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"repair_memory.{key} must be an integer, got {value!r}") from error
    if parsed < minimum:
        raise ValueError(f"repair_memory.{key} must be >= {minimum}, got {parsed}")
    return parsed


def _as_float(value: Any, key: str, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"repair_memory.{key} must be a number, got {value!r}") from error
    if parsed < minimum or parsed > maximum:
        raise ValueError(
            f"repair_memory.{key} must be within [{minimum}, {maximum}], got {parsed}"
        )
    return parsed


def _as_allowlist(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped == "*":
            return ("*",)
        return tuple(item.strip() for item in stripped.split(",") if item.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ValueError(f"repair_memory.project_allowlist must be a list or string, got {value!r}")


def _as_non_empty_string(value: Any, key: str) -> str:
    parsed = str(value or "").strip()
    if not parsed:
        raise ValueError(f"repair_memory.{key} must be a non-empty string")
    return parsed


def _as_retrieval_mode(value: Any) -> RetrievalMode:
    if isinstance(value, RetrievalMode):
        return value
    if isinstance(value, str):
        try:
            return RetrievalMode(value.strip().lower())
        except ValueError as error:
            raise ValueError(
                f"repair_memory.retrieval_mode must be one of "
                f"{[m.value for m in RetrievalMode]}, got {value!r}"
            ) from error
    raise ValueError(f"repair_memory.retrieval_mode must be a string, got {value!r}")


def parse_repair_memory_settings(raw: Mapping[str, Any] | None) -> RepairMemorySettings:
    """Validate a raw mapping and return a frozen ``RepairMemorySettings``.

    Missing keys fall back to disabled defaults so an accidental switch to
    ``capture_enabled=true`` or ``retrieval_mode="inject"`` is inert until
    rollout scope is deliberately configured.
    """
    data: dict[str, Any] = dict(raw or {})

    max_hints = _as_int(data.get("max_hints", 3), "max_hints")
    max_prompt_chars = _as_int(data.get("max_prompt_chars", 2000), "max_prompt_chars")
    min_score = _as_int(data.get("min_score", 60), "min_score", minimum=0)
    retrieval_timeout_ms = _as_int(data.get("retrieval_timeout_ms", 75), "retrieval_timeout_ms")
    stale_days = _as_int(data.get("stale_days", 180), "stale_days")
    global_min_projects = _as_int(data.get("global_min_projects", 2), "global_min_projects")
    needs_review_min_attempts = _as_int(
        data.get("needs_review_min_attempts", 3), "needs_review_min_attempts"
    )
    episode_retention_days = _as_int(
        data.get("episode_retention_days", 365), "episode_retention_days"
    )
    hit_retention_days = _as_int(data.get("hit_retention_days", 365), "hit_retention_days")
    consolidation_batch_size = _as_int(
        data.get("consolidation_batch_size", 50), "consolidation_batch_size"
    )
    consolidation_lease_seconds = _as_int(
        data.get("consolidation_lease_seconds", 300), "consolidation_lease_seconds"
    )
    consolidation_model_timeout_seconds = _as_int(
        data.get("consolidation_model_timeout_seconds", 60), "consolidation_model_timeout_seconds"
    )
    consolidation_poll_seconds = _as_int(
        data.get("consolidation_poll_seconds", 60), "consolidation_poll_seconds"
    )
    embedding_service_url = _as_non_empty_string(
        data.get("embedding_service_url", "http://bge-m3-service:8080"),
        "embedding_service_url",
    ).rstrip("/")
    embedding_model_name = _as_non_empty_string(
        data.get("embedding_model_name", "BAAI/bge-m3"), "embedding_model_name"
    )
    embedding_model_revision = _as_non_empty_string(
        data.get(
            "embedding_model_revision",
            "5617a9f61b028005a4858fdac845db406aefb181",
        ),
        "embedding_model_revision",
    )
    embedding_dimensions = _as_int(
        data.get("embedding_dimensions", 1024), "embedding_dimensions"
    )
    semantic_timeout_ms = _as_int(data.get("semantic_timeout_ms", 1500), "semantic_timeout_ms")
    embedding_batch_timeout_seconds = _as_float(
        data.get("embedding_batch_timeout_seconds", 30.0),
        "embedding_batch_timeout_seconds",
        minimum=0.001,
        maximum=3600.0,
    )
    embedding_batch_size = _as_int(data.get("embedding_batch_size", 16), "embedding_batch_size")
    semantic_min_similarity = _as_float(
        data.get("semantic_min_similarity", 0.55), "semantic_min_similarity"
    )
    semantic_candidate_limit_per_scope = _as_int(
        data.get("semantic_candidate_limit_per_scope", 500),
        "semantic_candidate_limit_per_scope",
    )

    project_initial_confidence = _as_float(
        data.get("project_initial_confidence", 0.60), "project_initial_confidence"
    )
    global_initial_confidence = _as_float(
        data.get("global_initial_confidence", 0.70), "global_initial_confidence"
    )
    support_confidence_increment = _as_float(
        data.get("support_confidence_increment", 0.05), "support_confidence_increment"
    )
    success_confidence_increment = _as_float(
        data.get("success_confidence_increment", 0.03), "success_confidence_increment"
    )
    failure_confidence_decrement = _as_float(
        data.get("failure_confidence_decrement", 0.02), "failure_confidence_decrement"
    )
    needs_review_confidence = _as_float(
        data.get("needs_review_confidence", 0.45), "needs_review_confidence"
    )

    return RepairMemorySettings(
        capture_enabled=_as_bool(data.get("capture_enabled", False), "capture_enabled"),
        retrieval_mode=_as_retrieval_mode(data.get("retrieval_mode", "off")),
        promotion_enabled=_as_bool(data.get("promotion_enabled", False), "promotion_enabled"),
        project_allowlist=_as_allowlist(data.get("project_allowlist", ())),
        max_hints=max_hints,
        max_prompt_chars=max_prompt_chars,
        min_score=min_score,
        retrieval_timeout_ms=retrieval_timeout_ms,
        stale_days=stale_days,
        global_min_projects=global_min_projects,
        needs_review_min_attempts=needs_review_min_attempts,
        needs_review_confidence=needs_review_confidence,
        episode_retention_days=episode_retention_days,
        hit_retention_days=hit_retention_days,
        consolidation_batch_size=consolidation_batch_size,
        consolidation_lease_seconds=consolidation_lease_seconds,
        consolidation_model_timeout_seconds=consolidation_model_timeout_seconds,
        consolidation_poll_seconds=consolidation_poll_seconds,
        embedding_service_url=embedding_service_url,
        embedding_model_name=embedding_model_name,
        embedding_model_revision=embedding_model_revision,
        embedding_dimensions=embedding_dimensions,
        semantic_timeout_ms=semantic_timeout_ms,
        embedding_batch_timeout_seconds=embedding_batch_timeout_seconds,
        embedding_batch_size=embedding_batch_size,
        semantic_min_similarity=semantic_min_similarity,
        semantic_candidate_limit_per_scope=semantic_candidate_limit_per_scope,
        project_initial_confidence=project_initial_confidence,
        global_initial_confidence=global_initial_confidence,
        support_confidence_increment=support_confidence_increment,
        success_confidence_increment=success_confidence_increment,
        failure_confidence_decrement=failure_confidence_decrement,
    )


def load_repair_memory_settings() -> RepairMemorySettings:
    """Read the Dynaconf ``REPAIR_MEMORY`` section and return validated settings.

    This is the runtime entry point. It imports ``get_settings`` lazily so the
    pure parser remains testable without triggering the global settings load.
    """
    from pr_agent.config_loader import get_settings

    section = get_settings().get("REPAIR_MEMORY", {}) or {}
    return parse_repair_memory_settings(dict(section))
