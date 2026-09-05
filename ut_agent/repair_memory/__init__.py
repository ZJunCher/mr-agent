"""Lazy public surface for the UT-Agent repair-memory subsystem.

The embedding image imports ``ut_agent.repair_memory.embedding`` without the
database, LLM, and PR-Agent dependencies used by consolidation and retrieval.
Keep this package initializer lightweight so that importing one submodule does
not eagerly load the complete repair-memory runtime.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "DEFAULT_MEMORY_DB_PATH": ("ut_agent.repair_memory.config", "DEFAULT_MEMORY_DB_PATH"),
    "RepairMemorySettings": ("ut_agent.repair_memory.config", "RepairMemorySettings"),
    "load_repair_memory_settings": ("ut_agent.repair_memory.config", "load_repair_memory_settings"),
    "parse_repair_memory_settings": ("ut_agent.repair_memory.config", "parse_repair_memory_settings"),
    "project_allowed": ("ut_agent.repair_memory.config", "project_allowed"),
    "BatchSummary": ("ut_agent.repair_memory.consolidate", "BatchSummary"),
    "GlobalMemoryLeakError": ("ut_agent.repair_memory.consolidate", "GlobalMemoryLeakError"),
    "MemoryCandidate": ("ut_agent.repair_memory.consolidate", "MemoryCandidate"),
    "MemoryCandidateValidationError": ("ut_agent.repair_memory.consolidate", "MemoryCandidateValidationError"),
    "PromotionSummary": ("ut_agent.repair_memory.consolidate", "PromotionSummary"),
    "parse_memory_candidate": ("ut_agent.repair_memory.consolidate", "parse_memory_candidate"),
    "pattern_key_for": ("ut_agent.repair_memory.consolidate", "pattern_key_for"),
    "promote_ready_patterns": ("ut_agent.repair_memory.consolidate", "promote_ready_patterns"),
    "run_consolidation_batch": ("ut_agent.repair_memory.consolidate", "run_consolidation_batch"),
    "validate_global_candidate": ("ut_agent.repair_memory.consolidate", "validate_global_candidate"),
    "build_verified_repair_episodes": ("ut_agent.repair_memory.episodes", "build_verified_repair_episodes"),
    "record_verified_repair_episodes": ("ut_agent.repair_memory.episodes", "record_verified_repair_episodes"),
    "MEMORY_SCHEMA_VERSION": ("ut_agent.repair_memory.models", "MEMORY_SCHEMA_VERSION"),
    "MemoryEvent": ("ut_agent.repair_memory.models", "MemoryEvent"),
    "MemoryScope": ("ut_agent.repair_memory.models", "MemoryScope"),
    "MemoryStatus": ("ut_agent.repair_memory.models", "MemoryStatus"),
    "RepairEpisode": ("ut_agent.repair_memory.models", "RepairEpisode"),
    "RepairMemory": ("ut_agent.repair_memory.models", "RepairMemory"),
    "RepairMemoryHint": ("ut_agent.repair_memory.models", "RepairMemoryHint"),
    "RepairQuery": ("ut_agent.repair_memory.models", "RepairQuery"),
    "RetrievalMode": ("ut_agent.repair_memory.models", "RetrievalMode"),
    "RetrievalResult": ("ut_agent.repair_memory.models", "RetrievalResult"),
    "SettlementSummary": ("ut_agent.repair_memory.outcomes", "SettlementSummary"),
    "memory_effectiveness_summary": ("ut_agent.repair_memory.outcomes", "memory_effectiveness_summary"),
    "settle_immediate_pipeline": ("ut_agent.repair_memory.outcomes", "settle_immediate_pipeline"),
    "settle_without_validation": ("ut_agent.repair_memory.outcomes", "settle_without_validation"),
    "render_historical_hints": ("ut_agent.repair_memory.prompt", "render_historical_hints"),
    "classify_failure_family": ("ut_agent.repair_memory.retrieve", "classify_failure_family"),
    "retrieve_repair_hints": ("ut_agent.repair_memory.retrieve", "retrieve_repair_hints"),
    "score_memory": ("ut_agent.repair_memory.retrieve", "score_memory"),
    "init_repair_memory_tables": ("ut_agent.repair_memory.store", "init_repair_memory_tables"),
    "list_attempt_hits": ("ut_agent.repair_memory.store", "list_attempt_hits"),
    "list_memories": ("ut_agent.repair_memory.store", "list_memories"),
    "list_memory_events": ("ut_agent.repair_memory.store", "list_memory_events"),
    "load_episode": ("ut_agent.repair_memory.store", "load_episode"),
    "load_memory": ("ut_agent.repair_memory.store", "load_memory"),
    "save_episode": ("ut_agent.repair_memory.store", "save_episode"),
    "save_memory": ("ut_agent.repair_memory.store", "save_memory"),
    "save_memory_with_evidence": ("ut_agent.repair_memory.store", "save_memory_with_evidence"),
    "update_memory_status": ("ut_agent.repair_memory.store", "update_memory_status"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
