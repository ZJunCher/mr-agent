"""Production-path paired replay for global Prompt proposals."""

from __future__ import annotations

import copy
import tomllib

from pr_agent.config_loader import get_settings
from pr_agent.eval.conditions import EvaluationTreatment, compare_paired_conditions
from pr_agent.eval.production_replay import ProductionReplayRequest, run_production_replay
from pr_agent.suggestions.prompt_evolution.high_fidelity_evaluator import (
    _action_for,
    _decimal_text,
    _score,
)
from pr_agent.suggestions.prompt_evolution.models import (
    HighFidelityCaseResult,
    HighFidelityEvaluationReport,
    Outcome,
    PromptEvaluationBatch,
    PromptProposal,
    ReplayAction,
)
from pr_agent.suggestions.prompt_evolution.prompt_surface import GLOBAL_PROMPT_PATHS


def _empty_project_skill(project: str) -> str:
    escaped = project.replace("\\", "\\\\").replace('"', '\\"')
    return f'schema_version = 1\nname = "evaluation"\nproject = "{escaped}"\nrules = []\n'


def build_candidate_prompt_settings(base_settings, workspace, proposal: PromptProposal):
    """Apply complete candidate TOML files to an isolated settings copy."""
    candidate = copy.deepcopy(base_settings)
    for change in proposal.changes:
        if change.path not in GLOBAL_PROMPT_PATHS:
            raise ValueError(f"non-global Prompt path in global evaluator: {change.path}")
        base_content = workspace.files.get(change.path) or ""
        base_sections = tomllib.loads(base_content) if base_content else {}
        candidate_sections = tomllib.loads(change.content)
        for section in base_sections:
            if hasattr(candidate, "unset"):
                candidate.unset(section)
        for section, values in candidate_sections.items():
            candidate.set(section, values)
    return candidate


class GlobalPromptHighFidelityEvaluator:
    """Compare baseline and candidate Prompt settings on identical frozen MRs."""

    def __init__(
        self,
        record_loader,
        *,
        replay_runner=run_production_replay,
        settings_factory=get_settings,
        review_record_loader=None,
        min_mrs: int = 1,
        max_mrs: int = 10,
        command: str = "improve",
    ) -> None:
        self.record_loader = record_loader
        self.replay_runner = replay_runner
        self.settings_factory = settings_factory
        self.review_record_loader = review_record_loader
        self.min_mrs = int(min_mrs)
        self.max_mrs = int(max_mrs)
        self.command = command

    async def evaluate_pair(
        self,
        batch: PromptEvaluationBatch,
        workspace,
        proposal: PromptProposal,
        *,
        model: str,
        minimum_score_delta: float = 0.0,
    ) -> HighFidelityEvaluationReport:
        requested = tuple(sorted({(case.project, case.mr_iid) for case in batch.selection_cases}))
        if len(requested) > self.max_mrs:
            return self._failure("high_fidelity_mr_limit_exceeded", ())
        records = {}
        exact_identity = self.review_record_loader is not None and all(
            case.review_id and case.commit_sha for case in batch.selection_cases
        )
        if exact_identity:
            cases_by_key = {
                key: tuple(case for case in batch.selection_cases if (case.project, case.mr_iid) == key)
                for key in requested
            }
            review_ids = tuple(sorted({case.review_id for case in batch.selection_cases}))
            for record in self.review_record_loader(review_ids):
                key = (str(record.get("project") or ""), str(record.get("mr_iid") or ""))
                cases = cases_by_key.get(key, ())
                identities = {(case.review_id, case.commit_sha) for case in cases}
                identity = (str(record.get("review_id") or ""), str(record.get("head_sha") or ""))
                if len(identities) == 1 and identity in identities:
                    records[key] = record
        else:
            for project in sorted({project for project, _mr in requested}):
                mr_iids = tuple(mr for item_project, mr in requested if item_project == project)
                for record in self.record_loader(project, mr_iids):
                    records[(project, str(record.get("mr_iid") or ""))] = record
        replayed = tuple(key for key in requested if key in records)
        replayed_labels = tuple(f"{project}!{mr}" for project, mr in replayed)
        if len(replayed) < self.min_mrs:
            return self._failure("insufficient_high_fidelity_evidence", replayed_labels)

        baseline_settings = copy.deepcopy(self.settings_factory())
        try:
            candidate_settings = build_candidate_prompt_settings(baseline_settings, workspace, proposal)
        except (TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
            return self._failure(f"candidate_settings_invalid:{type(exc).__name__}", replayed_labels)

        errors = []
        case_results = []
        condition_hashes = []
        baseline_actions = {}
        candidate_actions = {}
        cases_by_key = {
            key: tuple(case for case in batch.selection_cases if (case.project, case.mr_iid) == key)
            for key in replayed
        }
        for project, mr_iid in replayed:
            record = records[(project, mr_iid)]
            extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
            cases = cases_by_key[(project, mr_iid)]
            skill_content = str(extra.get("project_skill_content") or _empty_project_skill(project))
            target_sha = str(
                extra.get("project_skill_target_sha")
                or next((case.project_skill_target_sha for case in cases if case.project_skill_target_sha), "")
                or record.get("head_sha")
                or ""
            )
            common = dict(
                project=project,
                mr_iid=mr_iid,
                pr_url=str(record.get("pr_url") or ""),
                base_sha=str(record.get("base_sha") or ""),
                head_sha=str(record.get("head_sha") or ""),
                target_sha=target_sha,
                input_snapshot=record.get("input") or {},
                skill_content=skill_content,
                command=self.command,
                model=model,
                captured_at=str(record.get("created_at") or ""),
            )
            baseline = await self.replay_runner(ProductionReplayRequest(**common), settings=baseline_settings)
            candidate = await self.replay_runner(ProductionReplayRequest(**common), settings=candidate_settings)
            label = f"{project}!{mr_iid}"
            execution_cases = tuple(
                case for case in cases
                if case.case_kind in {"output_schema_error", "parser_error", "incomplete_coverage"}
            )
            if execution_cases:
                if len(execution_cases) != len(cases) or len({case.case_kind for case in execution_cases}) != 1:
                    errors.append(f"mixed_execution_cases:{label}")
                    continue
                kind = execution_cases[0].case_kind
                baseline_failed = (
                    baseline.coverage_status != "complete"
                    if kind == "incomplete_coverage"
                    else baseline.status != "ok"
                )
                candidate_fixed = (
                    candidate.status == "ok"
                    and candidate.coverage_status == "complete"
                    and candidate.condition is not None
                )
                if not baseline_failed:
                    errors.append(f"baseline_failure_not_reproduced:{label}:{kind}")
                if not candidate_fixed:
                    errors.append(f"candidate_execution_failure:{label}:{kind}")
                if baseline.condition is not None and candidate.condition is not None:
                    condition = compare_paired_conditions(
                        baseline.condition,
                        candidate.condition,
                        treatment=EvaluationTreatment.GLOBAL_PROMPT,
                    )
                    if not condition.matched:
                        suffix = ",".join(condition.mismatched_fields) or condition.error
                        errors.append(f"condition_mismatch:{label}:{suffix}")
                        continue
                baseline_hash = (
                    baseline.condition.manifest_hash if baseline.condition is not None else f"error:{kind}"
                )
                candidate_hash = (
                    candidate.condition.manifest_hash if candidate.condition is not None else f"error:{kind}"
                )
                condition_hashes.append((label, baseline_hash, candidate_hash))
                for case in execution_cases:
                    baseline_action = ReplayAction.EMIT if baseline_failed else ReplayAction.SUPPRESS
                    candidate_action = ReplayAction.SUPPRESS if candidate_fixed else ReplayAction.EMIT
                    baseline_actions[case.suggestion_id] = baseline_action
                    candidate_actions[case.suggestion_id] = candidate_action
                    case_results.append(HighFidelityCaseResult(
                        case.suggestion_id,
                        label,
                        case.outcome,
                        baseline_action,
                        candidate_action,
                        baseline_hash,
                        candidate_hash,
                    ))
                continue
            if baseline.status != "ok" or candidate.status != "ok":
                errors.append(f"replay_failed:{label}")
                continue
            if baseline.coverage_status != "complete" or candidate.coverage_status != "complete":
                errors.append(f"incomplete_diff_coverage:{label}")
                continue
            if baseline.condition is None or candidate.condition is None:
                errors.append(f"missing_condition_manifest:{label}")
                continue
            condition = compare_paired_conditions(
                baseline.condition,
                candidate.condition,
                treatment=EvaluationTreatment.GLOBAL_PROMPT,
            )
            if not condition.matched:
                suffix = ",".join(condition.mismatched_fields) or condition.error
                errors.append(f"condition_mismatch:{label}:{suffix}")
                continue
            condition_hashes.append((label, baseline.condition.manifest_hash, candidate.condition.manifest_hash))
            for case in cases:
                baseline_action, baseline_ambiguous = _action_for(case, baseline.normalized_items)
                candidate_action, candidate_ambiguous = _action_for(case, candidate.normalized_items)
                if baseline_ambiguous or candidate_ambiguous:
                    errors.append(f"ambiguous_result_match:{case.suggestion_id}")
                    continue
                baseline_actions[case.suggestion_id] = baseline_action
                candidate_actions[case.suggestion_id] = candidate_action
                case_results.append(HighFidelityCaseResult(
                    case.suggestion_id,
                    label,
                    case.outcome,
                    baseline_action,
                    candidate_action,
                    baseline.condition.manifest_hash,
                    candidate.condition.manifest_hash,
                ))

        evaluated_ids = {item.case_id for item in case_results}
        eligible = tuple(
            case for case in batch.selection_cases
            if (case.project, case.mr_iid) in set(replayed) and case.suggestion_id in evaluated_ids
        )
        expected = tuple(case for case in batch.selection_cases if (case.project, case.mr_iid) in set(replayed))
        if len(eligible) != len(expected):
            errors.append("incomplete_case_results")
        accepted = tuple(case for case in eligible if case.outcome is Outcome.ACCEPTED)
        rejected = tuple(case for case in eligible if case.outcome is Outcome.REJECTED)
        baseline_score = _score(eligible, baseline_actions)
        candidate_score = _score(eligible, candidate_actions)
        baseline_accepted = _score(accepted, baseline_actions)
        candidate_accepted = _score(accepted, candidate_actions)
        baseline_rejected = _score(rejected, baseline_actions)
        candidate_rejected = _score(rejected, candidate_actions)
        from decimal import Decimal

        if candidate_score <= baseline_score:
            errors.append("high_fidelity_score_not_improved")
        if candidate_score - baseline_score < Decimal(str(minimum_score_delta)):
            errors.append("high_fidelity_score_delta_too_small")
        if candidate_accepted < baseline_accepted:
            errors.append("high_fidelity_accepted_regression")
        if candidate_rejected < baseline_rejected:
            errors.append("high_fidelity_rejected_regression")
        unique = tuple(dict.fromkeys(errors))
        return HighFidelityEvaluationReport(
            not unique,
            unique,
            ("production_path", "prompt_only_treatment", "complete_coverage", "deterministic_score"),
            replayed_labels,
            tuple(case_results),
            _decimal_text(baseline_score),
            _decimal_text(candidate_score),
            _decimal_text(baseline_accepted),
            _decimal_text(candidate_accepted),
            _decimal_text(baseline_rejected),
            _decimal_text(candidate_rejected),
            tuple(condition_hashes),
        )

    @staticmethod
    def _failure(error: str, replayed: tuple[str, ...]) -> HighFidelityEvaluationReport:
        return HighFidelityEvaluationReport(False, (error,), (), replayed, (), "0", "0", "0", "0", "0", "0", ())
