"""Single source of truth for the editable Prompt surface.

The Agent, validator, publisher, and runner all import this module so the
whitelist of globally editable Prompt files, mirror families, and safe
project-rule repo paths is defined exactly once.
"""
from __future__ import annotations

from pr_agent.suggestions.project_prompt_rules import PROJECT_SKILL_MANIFEST_PATH, safe_project_parts
from pr_agent.suggestions.prompt_provenance import GLOBAL_IMPROVE_PROMPT_PATHS

SETTINGS_PREFIX = "pr_agent/settings/"
GLOBAL_PROMPT_PATHS = frozenset(f"{SETTINGS_PREFIX}{path}" for path in GLOBAL_IMPROVE_PROMPT_PATHS)
GENERATION_ALL = frozenset(path for path in GLOBAL_PROMPT_PATHS if "pr_code_suggestions_prompts" in path)
GENERATION_PYTHON = frozenset(path for path in GENERATION_ALL if "_python" in path)
REFLECTION_ALL = frozenset(path for path in GLOBAL_PROMPT_PATHS if "_reflect_prompts" in path)
SINGLE_FILE_FAMILIES = {
    "scenario_validator": f"{SETTINGS_PREFIX}code_suggestions/pr_code_suggestions_scenario_validator_prompts.toml",
    "inline_selfcheck": f"{SETTINGS_PREFIX}pr_inline_selfcheck_prompts.toml",
    "tier1_repair": f"{SETTINGS_PREFIX}pr_tier1_repair_prompts.toml",
}
PROJECT_RULE_PREFIX = ".pr_agent/skills/review/"


def project_rule_repo_path(project: str) -> str:
    safe_project_parts(project)
    return PROJECT_SKILL_MANIFEST_PATH


def project_from_rule_repo_path(path: str) -> str:
    if path != PROJECT_SKILL_MANIFEST_PATH:
        raise ValueError("path is not the fixed Project Skill manifest")
    raise ValueError("project identity must come from the candidate, not the repository path")


def is_allowed_prompt_path(path: str) -> bool:
    if path in GLOBAL_PROMPT_PATHS:
        return True
    return path == PROJECT_SKILL_MANIFEST_PATH
