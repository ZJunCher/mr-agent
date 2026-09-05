"""Prompt Evolution Agent: one weekly proposal from eligible candidates.

Renders the static Agent Prompt with ``StrictUndefined``, calls the
``submit_prompt_proposal`` tool, and enforces evidence-ID integrity and
scope rules before converting the Pydantic output to immutable domain types.
"""
from __future__ import annotations

import json
from typing import Literal

from jinja2 import Environment, StrictUndefined
from pydantic import BaseModel

from pr_agent.config_loader import get_settings
from pr_agent.suggestions.prompt_evolution.gitlab_publisher import PromptWorkspace
from pr_agent.suggestions.prompt_evolution.models import (
    CandidateScope,
    EligibleCandidate,
    Evidence,
    Outcome,
    PromptChangeKind,
    PromptFileChange,
    PromptProposal,
    ValidationReport,
)
from pr_agent.suggestions.prompt_evolution.prompt_surface import project_rule_repo_path


class PromptFileChangeOutput(BaseModel):
    path: str
    family: Literal[
        "generation_all", "generation_python", "reflection_all",
        "scenario_validator", "inline_selfcheck", "tier1_repair", "project_rule",
    ]
    expected_base_sha256: str
    content: str
    evidence_ids: list[str]


class PromptProposalOutput(BaseModel):
    rationale: str
    change_kind: Literal["conservative_tightening", "specific_rule"]
    evidence_ids: list[str]
    changes: list[PromptFileChangeOutput]


def _evidence_index(candidates: tuple[EligibleCandidate, ...]) -> dict[str, Evidence]:
    index: dict[str, Evidence] = {}
    for candidate in candidates:
        for item in candidate.cluster.evidence:
            index[item.suggestion_id] = item
    return index


def _candidate_for_evidence_id(candidates: tuple[EligibleCandidate, ...]) -> dict[str, EligibleCandidate]:
    mapping: dict[str, EligibleCandidate] = {}
    for candidate in candidates:
        for item in candidate.cluster.evidence:
            mapping[item.suggestion_id] = candidate
    return mapping


class PromptEvolutionAgent:
    """Generate one Pydantic-validated PromptProposal per weekly batch."""

    def __init__(self, client, model: str) -> None:
        self.client = client
        self.model = model

    async def generate(self, candidates: tuple[EligibleCandidate, ...],
                       workspace: PromptWorkspace, *,
                       rejected_edits: tuple[dict, ...] = ()) -> PromptProposal:
        return await self._call(
            candidates,
            workspace,
            failed_report=None,
            rejected_edits=rejected_edits,
        )

    async def regenerate(self, candidates: tuple[EligibleCandidate, ...],
                          workspace: PromptWorkspace,
                          failed_report: ValidationReport, *,
                          rejected_edits: tuple[dict, ...] = ()) -> PromptProposal:
        return await self._call(
            candidates,
            workspace,
            failed_report=failed_report,
            rejected_edits=rejected_edits,
        )

    async def _call(self, candidates: tuple[EligibleCandidate, ...],
                    workspace: PromptWorkspace, *, failed_report: ValidationReport | None,
                    rejected_edits: tuple[dict, ...]) -> PromptProposal:
        if not candidates:
            raise ValueError("no eligible candidates supplied")

        candidates_json = json.dumps(
            [
                {
                    "candidate_id": c.candidate_id,
                    "scope": c.scope.value,
                    "project": c.project,
                    "source_prompt_hash": c.source_prompt_hash,
                    "cluster_key": c.cluster.cluster_key,
                    "evidence": [
                        {
                            "suggestion_id": e.suggestion_id,
                            "project": e.project,
                            "file_path": e.file_path,
                            "label": e.label,
                            "summary": e.summary,
                            "suggestion_content": e.suggestion_content,
                            "outcome": e.outcome.value,
                            "feedback": list(e.feedback),
                        }
                        for e in c.cluster.evidence
                    ],
                }
                for c in candidates
            ],
            ensure_ascii=False,
        )
        workspace_json = json.dumps(
            {"project_path": workspace.project_path, "target_branch": workspace.target_branch,
             "base_sha": workspace.base_sha, "files": workspace.files},
            ensure_ascii=False,
        )
        system = Environment(undefined=StrictUndefined).from_string(
            get_settings().prompt_evolution_agent_prompt.system
        ).render()
        user = Environment(undefined=StrictUndefined).from_string(
            get_settings().prompt_evolution_agent_prompt.user
        ).render(
            candidates_json=candidates_json,
            workspace_json=workspace_json,
            rejected_edits_json=json.dumps(rejected_edits, ensure_ascii=False),
            failed_report_json=json.dumps({
                "errors": list(failed_report.errors),
                "checks": list(failed_report.checks),
            }, ensure_ascii=False) if failed_report is not None else "{}",
        )

        output: PromptProposalOutput = await self.client.call(
            self.model, system, user, "submit_prompt_proposal", PromptProposalOutput
        )

        return self._validate_and_convert(output, candidates)

    def _validate_and_convert(self, output: PromptProposalOutput,
                              candidates: tuple[EligibleCandidate, ...]) -> PromptProposal:
        if not output.evidence_ids:
            raise ValueError("proposal has no evidence IDs")
        if not output.changes:
            raise ValueError("proposal has no file changes")

        evidence_index = _evidence_index(candidates)
        candidate_by_id = _candidate_for_evidence_id(candidates)

        # Every proposal-level and change-level evidence ID must belong to supplied candidates.
        all_change_ids: set[str] = set()
        for change in output.changes:
            for eid in change.evidence_ids:
                if eid not in evidence_index:
                    raise ValueError(f"change references unknown evidence ID: {eid}")
            all_change_ids.update(change.evidence_ids)
        for eid in output.evidence_ids:
            if eid not in evidence_index:
                raise ValueError(f"proposal references unknown evidence ID: {eid}")
        if set(output.evidence_ids) != all_change_ids:
            raise ValueError("proposal evidence IDs must equal the union of change evidence IDs")

        # specific_rule requires textual feedback from explicitly rejected evidence.
        if output.change_kind == "specific_rule":
            feedback_by_id = {
                item.suggestion_id: item.feedback if item.outcome is Outcome.REJECTED else ()
                for candidate in candidates
                for item in candidate.cluster.evidence
            }
            if not any(feedback_by_id.get(eid) for eid in output.evidence_ids):
                raise ValueError("specific project rule requires textual feedback")

        # Scope enforcement: project-candidate evidence may change only that project's rule file;
        # global-candidate evidence may change only a global family. generation_python requires .py files.
        for change in output.changes:
            for eid in change.evidence_ids:
                ev = evidence_index[eid]
                cand = candidate_by_id[eid]
                if cand.scope is CandidateScope.PROJECT:
                    expected_path = project_rule_repo_path(cand.project or "")
                    if change.path != expected_path:
                        raise ValueError(
                            f"project-candidate evidence {eid} may change only {expected_path}, not {change.path}"
                        )
                    if change.family != "project_rule":
                        raise ValueError("project-candidate evidence may change only project_rule files")
                else:  # GLOBAL
                    if change.family == "project_rule":
                        raise ValueError("global-candidate evidence may not change project_rule files")
                    if change.family == "generation_python":
                        if not ev.file_path.endswith(".py"):
                            raise ValueError("generation_python changes require .py evidence files")

        # specific_rule: all changed files must be project_rule and match a supplied project candidate.
        if output.change_kind == "specific_rule":
            for change in output.changes:
                if change.family != "project_rule":
                    raise ValueError("specific_rule changes must use family project_rule")

        return PromptProposal(
            rationale=output.rationale,
            change_kind=PromptChangeKind(output.change_kind),
            evidence_ids=tuple(output.evidence_ids),
            changes=tuple(
                PromptFileChange(
                    path=change.path,
                    family=change.family,
                    expected_base_sha256=change.expected_base_sha256,
                    content=change.content,
                    evidence_ids=tuple(change.evidence_ids),
                )
                for change in output.changes
            ),
        )
