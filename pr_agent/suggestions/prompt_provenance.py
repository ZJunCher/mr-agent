from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

from pr_agent.suggestions.project_prompt_rules import EffectiveProjectSkill, ProjectRuleSet, project_rules_hash

GLOBAL_IMPROVE_PROMPT_PATHS = (
    "code_suggestions/pr_code_suggestions_prompts.toml",
    "code_suggestions/pr_code_suggestions_prompts_v2.toml",
    "code_suggestions/pr_code_suggestions_prompts_v3.toml",
    "code_suggestions/pr_code_suggestions_prompts_not_decoupled.toml",
    "code_suggestions/pr_code_suggestions_prompts_not_decoupled_v2.toml",
    "code_suggestions/pr_code_suggestions_prompts_not_decoupled_v3.toml",
    "code_suggestions/pr_code_suggestions_prompts_python.toml",
    "code_suggestions/pr_code_suggestions_prompts_python_v2.toml",
    "code_suggestions/pr_code_suggestions_prompts_python_v3.toml",
    "code_suggestions/pr_code_suggestions_reflect_prompts.toml",
    "code_suggestions/pr_code_suggestions_reflect_prompts_v2.toml",
    "code_suggestions/pr_code_suggestions_scenario_validator_prompts.toml",
    "pr_inline_selfcheck_prompts.toml",
    "pr_tier1_repair_prompts.toml",
)
_SETTINGS_ROOT = Path(__file__).resolve().parents[1] / "settings"


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PromptProvenance:
    global_prompt_set_hash: str
    project_rules_hash: str
    prompt_bundle_hash: str
    prompt_version: str = ""
    project_skill_hash: str = ""
    project_skill_manifest_hash: str = ""
    project_skill_target_sha: str = ""
    project_skill_status: str = ""
    project_skill_rule_ids_json: str = "[]"
    project_skill_matched_files_json: str = "{}"
    project_skill_reference_hashes_json: str = "{}"

    def as_record(self) -> dict[str, str]:
        return {
            "global_prompt_set_hash": self.global_prompt_set_hash,
            "project_rules_hash": self.project_rules_hash,
            "prompt_bundle_hash": self.prompt_bundle_hash,
            "prompt_version": self.prompt_version,
            "project_skill_hash": self.project_skill_hash,
            "project_skill_manifest_hash": self.project_skill_manifest_hash,
            "project_skill_target_sha": self.project_skill_target_sha,
            "project_skill_status": self.project_skill_status,
            "project_skill_rule_ids_json": self.project_skill_rule_ids_json,
            "project_skill_matched_files_json": self.project_skill_matched_files_json,
            "project_skill_reference_hashes_json": self.project_skill_reference_hashes_json,
        }


def compute_global_prompt_set_hash(
    settings_root: Path | None = None,
    prompt_paths: Sequence[str] = GLOBAL_IMPROVE_PROMPT_PATHS,
) -> str:
    root = settings_root or _SETTINGS_ROOT
    contents = {path: (root / path).read_text(encoding="utf-8") for path in prompt_paths}
    return compute_prompt_set_hash_from_contents(contents, prompt_paths)


@lru_cache(maxsize=1)
def compute_deployed_global_prompt_set_hash() -> str:
    return compute_global_prompt_set_hash()


def compute_prompt_set_hash_from_contents(
    contents: Mapping[str, str],
    prompt_paths: Sequence[str] = GLOBAL_IMPROVE_PROMPT_PATHS,
) -> str:
    payload = [(path, contents[path]) for path in sorted(prompt_paths)]
    return _digest(payload)


def build_prompt_provenance(
    rule_set: ProjectRuleSet,
    effective_templates: Mapping[str, str],
    *,
    settings_root: Path | None = None,
    prompt_paths: Sequence[str] = GLOBAL_IMPROVE_PROMPT_PATHS,
    prompt_version: str = "",
    effective_skill: EffectiveProjectSkill | None = None,
) -> PromptProvenance:
    global_hash = (
        compute_deployed_global_prompt_set_hash()
        if settings_root is None and tuple(prompt_paths) == GLOBAL_IMPROVE_PROMPT_PATHS
        else compute_global_prompt_set_hash(settings_root, prompt_paths)
    )
    rules_hash = effective_skill.skill_hash if effective_skill is not None else project_rules_hash(rule_set)
    skill_payload = {}
    if effective_skill is not None:
        skill_payload = {
            "target_sha": effective_skill.target_sha,
            "status": effective_skill.status,
            "manifest_hash": effective_skill.manifest_hash,
            "rule_ids": effective_skill.selected_rule_ids,
            "matched_files": effective_skill.matched_files,
            "reference_hashes": effective_skill.reference_hashes,
        }
    bundle_hash = _digest({
        "templates": dict(sorted(effective_templates.items())),
        "rules": rules_hash,
        "project_skill": skill_payload,
    })
    return PromptProvenance(
        global_hash,
        rules_hash,
        bundle_hash,
        prompt_version,
        project_skill_hash=effective_skill.skill_hash if effective_skill is not None else "",
        project_skill_manifest_hash=effective_skill.manifest_hash if effective_skill is not None else "",
        project_skill_target_sha=effective_skill.target_sha if effective_skill is not None else "",
        project_skill_status=effective_skill.status if effective_skill is not None else "",
        project_skill_rule_ids_json=json.dumps(
            list(effective_skill.selected_rule_ids) if effective_skill is not None else [],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        project_skill_matched_files_json=json.dumps(
            dict(effective_skill.matched_files) if effective_skill is not None else {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        project_skill_reference_hashes_json=json.dumps(
            dict(effective_skill.reference_hashes) if effective_skill is not None else {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def build_project_skill_usage_identity(effective_skill: EffectiveProjectSkill, command: str) -> tuple[str, str]:
    """Return the deployed global anchor and immutable review/improve bundle hash."""
    global_hash = compute_deployed_global_prompt_set_hash()
    return global_hash, _digest({
        "command": str(command),
        "global_prompt_set_hash": global_hash,
        "project_skill_hash": effective_skill.skill_hash,
        "target_sha": effective_skill.target_sha,
        "rule_ids": effective_skill.selected_rule_ids,
        "reference_hashes": effective_skill.reference_hashes,
    })
