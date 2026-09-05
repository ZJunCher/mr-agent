"""Pinned-model counterfactual replay for Project Skill selection cases."""
from __future__ import annotations

import json
from typing import Literal

from jinja2 import Environment, StrictUndefined
from pydantic import BaseModel, Field

from pr_agent.config_loader import get_settings
from pr_agent.suggestions.prompt_evolution.models import (
    ReplayAction,
    SkillOptimizationBatch,
    SkillReplayDecision,
    SkillReplayResult,
)


class ReplayDecisionOutput(BaseModel):
    case_id: str
    action: Literal["emit", "suppress", "revise"]
    reason: str = Field(max_length=300)


class ReplayBatchOutput(BaseModel):
    decisions: list[ReplayDecisionOutput] = Field(min_length=1, max_length=20)


class ProjectSkillReplayEvaluator:
    """Replay current and candidate manifests against one hidden selection set."""

    def __init__(self, client, model: str) -> None:
        self.client = client
        self.model = model

    async def replay_pair(
        self,
        batch: SkillOptimizationBatch,
        baseline_skill: str,
        candidate_skill: str,
    ) -> tuple[SkillReplayResult, SkillReplayResult]:
        cases_json = json.dumps([
            {
                "case_id": item.suggestion_id,
                "file_path": item.file_path,
                "label": item.label,
                "summary": item.summary,
                "suggestion_content": item.suggestion_content,
                "existing_code": item.existing_code,
                "improved_code": item.improved_code,
                "line_start": item.line_start,
                "line_end": item.line_end,
            }
            for item in batch.selection_cases
        ], ensure_ascii=False)
        prompt = get_settings().prompt_evolution_project_skill_replay_prompt
        system = Environment(undefined=StrictUndefined).from_string(prompt.system).render()
        template = Environment(undefined=StrictUndefined).from_string(prompt.user)
        baseline_user = template.render(skill_content=baseline_skill, cases_json=cases_json)
        candidate_user = template.render(skill_content=candidate_skill, cases_json=cases_json)
        first, second, actual_model = await self.client.call_pair_same_model(
            self.model,
            system,
            baseline_user,
            candidate_user,
            "submit_project_skill_replay",
            ReplayBatchOutput,
        )
        return (
            self._convert(first, batch, actual_model),
            self._convert(second, batch, actual_model),
        )

    @staticmethod
    def _convert(
        output: ReplayBatchOutput,
        batch: SkillOptimizationBatch,
        model: str,
    ) -> SkillReplayResult:
        expected_ids = set(batch.selection_ids)
        returned_ids = [item.case_id for item in output.decisions]
        if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != expected_ids:
            raise ValueError("replay case IDs must match the hidden selection set exactly")
        by_id = {item.case_id: item for item in output.decisions}
        return SkillReplayResult(
            model=model,
            decisions=tuple(
                SkillReplayDecision(
                    case_id=case_id,
                    action=ReplayAction(by_id[case_id].action),
                    reason=" ".join(by_id[case_id].reason.split())[:300],
                )
                for case_id in batch.selection_ids
            ),
        )


# Temporary import compatibility while the runner is migrated in the same feature batch.
ProjectSkillEvolutionEvaluator = ProjectSkillReplayEvaluator
