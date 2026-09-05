"""Deterministic Prompt safety validator.

Only ``passed=True`` proposals may reach the publisher. Every check is
deterministic (no LLM). Imports the exact whitelist and mirror families from
``prompt_surface.py`` so the validator owns no second path source of truth.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import tomllib
from typing import Iterable

from jinja2 import Environment, StrictUndefined, meta

from pr_agent.algo.language_router import language_scope_for_file
from pr_agent.suggestions.project_prompt_rules import ProjectRuleSet, parse_project_rules
from pr_agent.suggestions.prompt_evolution.gitlab_publisher import PromptWorkspace
from pr_agent.suggestions.prompt_evolution.models import (
    MISSING_FILE_HASH,
    CandidateScope,
    EligibleCandidate,
    Evidence,
    PromptFileChange,
    PromptProposal,
    ValidationReport,
)
from pr_agent.suggestions.prompt_evolution.prompt_surface import (
    GENERATION_ALL,
    GENERATION_PYTHON,
    REFLECTION_ALL,
    SINGLE_FILE_FAMILIES,
    is_allowed_prompt_path,
    project_rule_repo_path,
)

_SECRET_PATTERNS = re.compile(
    r"(?i)(private key|glpat-|password\s*=|token\s*=|xox[bap]-|sk-[a-z0-9]{20,})"
)
_MAX_PROMPT_FILE_CHARS = 200_000
_MAX_FILES_PER_MR = 20
_MAX_DIFF_LINES = 600

_FAMILY_TO_PATHS: dict[str, frozenset[str]] = {
    "generation_all": GENERATION_ALL,
    "generation_python": GENERATION_PYTHON,
    "reflection_all": REFLECTION_ALL,
    "scenario_validator": frozenset({SINGLE_FILE_FAMILIES["scenario_validator"]}),
    "inline_selfcheck": frozenset({SINGLE_FILE_FAMILIES["inline_selfcheck"]}),
    "tier1_repair": frozenset({SINGLE_FILE_FAMILIES["tier1_repair"]}),
}


def _sha256(content: str | None) -> str:
    if content is None:
        return MISSING_FILE_HASH
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _evidence_index(candidates: Iterable[EligibleCandidate]) -> dict[str, Evidence]:
    index: dict[str, Evidence] = {}
    for candidate in candidates:
        for item in candidate.cluster.evidence:
            index[item.suggestion_id] = item
    return index


def _candidate_by_evidence(candidates: Iterable[EligibleCandidate]) -> dict[str, EligibleCandidate]:
    mapping: dict[str, EligibleCandidate] = {}
    for candidate in candidates:
        for item in candidate.cluster.evidence:
            mapping[item.suggestion_id] = candidate
    return mapping


def _base_template_variables(path: str, base_content: str | None) -> set[str]:
    if not base_content:
        return set()
    try:
        ast = Environment(undefined=StrictUndefined).parse(base_content)
        return set(meta.find_undeclared_variables(ast))
    except Exception:
        return set()


def validate_proposal(proposal: PromptProposal, candidates: tuple[EligibleCandidate, ...],
                      workspace: PromptWorkspace,
                      *, max_files: int = _MAX_FILES_PER_MR,
                      max_prompt_file_chars: int = _MAX_PROMPT_FILE_CHARS,
                      max_diff_lines: int = _MAX_DIFF_LINES,
                      max_project_rule_edits: int = 3) -> ValidationReport:
    errors: list[str] = []
    evidence_index = _evidence_index(candidates)
    candidate_by_id = _candidate_by_evidence(candidates)

    if not proposal.changes:
        errors.append("empty_changes")
    if len(proposal.changes) > max_files:
        errors.append("too_many_files")

    all_change_ids: set[str] = set()
    for change in proposal.changes:
        all_change_ids.update(change.evidence_ids)

    # proposal-level IDs must equal union of change-level IDs
    if set(proposal.evidence_ids) != all_change_ids:
        errors.append("evidence_id_mismatch")

    # every evidence ID must resolve to exactly one candidate
    for eid in proposal.evidence_ids:
        if eid not in evidence_index:
            errors.append(f"unknown_evidence:{eid}")

    for change in proposal.changes:
        errors.extend(_validate_change(change, proposal, candidates, workspace,
                                       evidence_index, candidate_by_id, max_prompt_file_chars))

    changed_lines = sum(
        _changed_content_lines(workspace.files.get(change.path), change.content)
        for change in proposal.changes
    )
    if changed_lines > max_diff_lines:
        errors.append("diff_too_large")

    for change in proposal.changes:
        if change.family != "project_rule" or workspace.files.get(change.path) is None:
            continue
        try:
            from pr_agent.suggestions.prompt_evolution.project_skill_optimizer import semantic_skill_diff

            project_candidates = {
                candidate.project
                for evidence_id in change.evidence_ids
                if (candidate := candidate_by_id.get(evidence_id)) is not None
            }
            if len(project_candidates) == 1 and None not in project_candidates:
                project = next(iter(project_candidates))
                semantic_diff = semantic_skill_diff(
                    str(workspace.files[change.path]),
                    change.content,
                    str(project),
                )
                if semantic_diff.edit_count > int(max_project_rule_edits):
                    errors.append("textual_learning_rate_exceeded")
        except ValueError:
            pass  # strict project-rule parsing already reports the stable validation error

    return ValidationReport(
        passed=not errors,
        errors=tuple(sorted(set(errors))),
        checks=(
            "evidence_scope", "path_whitelist", "base_hash", "toml", "jinja",
            "mirror", "size", "diff_size", "secrets",
            "textual_learning_rate",
        ),
    )


def _changed_content_lines(base_content: str | None, proposed_content: str) -> int:
    diff = difflib.unified_diff(
        (base_content or "").splitlines(),
        proposed_content.splitlines(),
        lineterm="",
    )
    return sum(
        1
        for line in diff
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


def _validate_change(change: PromptFileChange, proposal: PromptProposal,
                     candidates: tuple[EligibleCandidate, ...], workspace: PromptWorkspace,
                     evidence_index: dict[str, Evidence],
                     candidate_by_id: dict[str, EligibleCandidate],
                     max_prompt_file_chars: int) -> list[str]:
    errors: list[str] = []

    # 1. evidence scope
    if not change.evidence_ids:
        errors.append(f"empty_evidence:{change.path}")
    for eid in change.evidence_ids:
        ev = evidence_index.get(eid)
        if ev is None:
            errors.append(f"unknown_evidence:{eid}")
            continue
        cand = candidate_by_id.get(eid)
        if cand is None:
            errors.append(f"unresolved_candidate:{eid}")
            continue
        if cand.scope is CandidateScope.PROJECT:
            expected_path = project_rule_repo_path(cand.project or "")
            if change.path != expected_path:
                errors.append(f"scope_violation:{eid}:project_path")
            if change.family != "project_rule":
                errors.append(f"scope_violation:{eid}:project_family")
        else:  # GLOBAL
            if change.family == "project_rule":
                errors.append(f"scope_violation:{eid}:global_project_rule")
            if change.family == "generation_python" and not ev.file_path.endswith(".py"):
                errors.append(f"scope_violation:{eid}:python_file")

    # 2. path whitelist
    if not is_allowed_prompt_path(change.path):
        errors.append("path_not_allowed")
        return errors  # no point checking further

    # 3. base hash
    base_content = workspace.files.get(change.path)
    expected_hash = _sha256(base_content)
    if change.expected_base_sha256 != expected_hash:
        errors.append("base_hash_mismatch")

    # 4. TOML parse
    if not change.content:
        errors.append("empty_content")
        return errors
    try:
        tomllib.loads(change.content)
    except Exception:
        errors.append("toml_invalid")
        return errors

    # 5. Jinja parse
    try:
        Environment(undefined=StrictUndefined).parse(change.content)
    except Exception as exc:
        errors.append(f"jinja_invalid:{type(exc).__name__}")

    # 6. undeclared variables (compare to base template)
    base_vars = _base_template_variables(change.path, base_content)
    try:
        ast = Environment(undefined=StrictUndefined).parse(change.content)
        new_vars = set(meta.find_undeclared_variables(ast))
        # project_prompt_rules is pre-approved
        new_vars.discard("project_prompt_rules")
        unexpected = new_vars - base_vars
        if unexpected:
            errors.append(f"jinja_new_variables:{','.join(sorted(unexpected))}")
    except Exception:
        pass  # already reported above

    # 7. size + unchanged + secrets
    if len(change.content) > max_prompt_file_chars:
        errors.append("file_too_large")
    if change.content == base_content:
        errors.append("unchanged_content")
    if _SECRET_PATTERNS.search(change.content):
        errors.append("secrets_detected")

    # 8. project-rule strict parse
    if change.family == "project_rule":
        try:
            candidate_projects = {
                candidate_by_id[evidence_id].project
                for evidence_id in change.evidence_ids
                if evidence_id in candidate_by_id
                and candidate_by_id[evidence_id].scope is CandidateScope.PROJECT
            }
            if len(candidate_projects) != 1 or None in candidate_projects:
                errors.append("project_rule_candidate_mismatch")
                return errors
            derived_project = next(iter(candidate_projects))
            if base_content is None:
                errors.append("project_skill_not_opted_in")
                return errors
            proposed_rules = parse_project_rules(change.content, derived_project)
            errors.extend(_validate_project_rule_languages(
                change, base_content, derived_project, proposed_rules, evidence_index
            ))
        except ValueError as exc:
            if "project rule schema/project mismatch" in str(exc):
                errors.append("project_rule_path_mismatch")
            else:
                errors.append(f"project_rule_invalid:{exc}")

    # mirror family completeness
    family_paths = _FAMILY_TO_PATHS.get(change.family)
    if family_paths is not None:
        # If the family is a mirror set, every file in the set must be changed.
        if change.path in family_paths and len(family_paths) > 1:
            changed_paths = {c.path for c in proposal.changes}
            if not family_paths <= changed_paths:
                errors.append(f"mirror_family_incomplete:{change.family}")

    return errors


def _validate_project_rule_languages(change: PromptFileChange, base_content: str | None,
                                     project: str, proposed_rules: ProjectRuleSet,
                                     evidence_index: dict[str, Evidence]) -> list[str]:
    """Require changed project rules to match the language of their evidence."""
    if base_content:
        try:
            base_rules = parse_project_rules(base_content, project)
        except ValueError:
            return ["project_rule_base_invalid"]
    else:
        base_rules = ProjectRuleSet(project)

    base_by_id = {rule.id: rule for rule in base_rules.rules}
    proposed_by_id = {rule.id: rule for rule in proposed_rules.rules}
    if set(base_by_id) - set(proposed_by_id):
        return ["project_rule_deletion"]

    if (
        base_rules.name != proposed_rules.name
        or base_rules.description != proposed_rules.description
        or base_rules.project != proposed_rules.project
    ):
        return ["project_skill_metadata_change"]

    changed_rules = [rule for rule in proposed_rules.rules if base_by_id.get(rule.id) != rule]
    if not changed_rules:
        return []

    evidence_languages = {
        language
        for evidence_id in change.evidence_ids
        if (evidence := evidence_index.get(evidence_id)) is not None
        if (language := language_scope_for_file(evidence.file_path)) is not None
    }
    for rule in changed_rules:
        base_rule = base_by_id.get(rule.id)
        if (
            (base_rule is None and rule.references)
            or (base_rule is not None and rule.references != base_rule.references)
        ):
            return ["project_rule_reference_change"]
        if base_rule is not None:
            if rule.targets != base_rule.targets or rule.languages != base_rule.languages:
                return ["project_rule_scope_change"]
            if len(rule.instruction) < len(base_rule.instruction):
                return ["project_rule_instruction_weakened"]
            if base_rule.paths:
                if not set(rule.paths).issubset(base_rule.paths):
                    return ["project_rule_paths_broadened"]
            elif not rule.paths:
                pass
            if not set(base_rule.exclude_paths).issubset(rule.exclude_paths):
                return ["project_rule_exclusion_removed"]
        if frozenset(rule.languages) != frozenset(evidence_languages):
            return ["project_rule_language_mismatch"]
    return []
