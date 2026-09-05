"""Cross-project evolution for opted-in Project Review Skills.

Project candidates are deliberately processed outside the global Prompt run:
each owning repository gets its own target-branch snapshot, lease, fencing
token, idempotent branch/commit identity, and Draft MR.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections import defaultdict
from dataclasses import replace

from pr_agent.config_loader import get_settings  # noqa: F401  (import side-effect ordering)
from pr_agent.log import get_logger
from pr_agent.suggestions.project_prompt_rules import (
    PROJECT_SKILL_MANIFEST_PATH,
    SKILL_STATUS_LOADED,
    parse_project_rules,
    project_rules_hash,
)
from pr_agent.suggestions.prompt_evolution.gitlab_publisher import PromptWorkspace
from pr_agent.suggestions.prompt_evolution.lease import LostEvolutionLease
from pr_agent.suggestions.prompt_evolution.models import (
    CandidateScope,
    EligibleCandidate,
    EvolutionRun,
    EvolutionRunStatus,
    Outcome,
    SkillOptimizationReport,
    ValidationReport,
)
from pr_agent.suggestions.prompt_evolution.project_skill_optimizer import (
    InsufficientValidationEvidence,
    build_optimization_batch,
    evaluate_skill_gate,
    semantic_skill_diff,
)
from pr_agent.suggestions.prompt_evolution.runner import (
    _aggregation_thresholds,
    _batch_id,
    _candidate_fingerprint,
    _render_description,
    _sanitize_error_message,
)
from pr_agent.suggestions.prompt_provenance import compute_global_prompt_set_hash

_TERMINAL = {
    EvolutionRunStatus.MR_OPEN,
    EvolutionRunStatus.MERGED,
    EvolutionRunStatus.CLOSED,
    EvolutionRunStatus.COMPLETED_NO_CHANGE,
    EvolutionRunStatus.DRY_RUN_VALIDATED,
    EvolutionRunStatus.OPTIMIZATION_REJECTED,
    EvolutionRunStatus.INSUFFICIENT_VALIDATION,
    EvolutionRunStatus.FAILED_TERMINAL,
    EvolutionRunStatus.SUPERSEDED,
}


class ProjectSkillEvolutionRunner:
    """Generate at most one Draft Skill MR per opted-in project and batch."""

    def __init__(self, *, settings, store, leases, publisher_factory, evidence_loader,
                 clusterer, aggregator, agent, validator, evaluator, owner: str, now,
                 high_fidelity_evaluator=None) -> None:
        self.settings = settings
        self.store = store
        self.leases = leases
        self.publisher_factory = publisher_factory
        self.evidence_loader = evidence_loader
        self.clusterer = clusterer
        self.aggregator = aggregator
        self.agent = agent
        self.validator = validator
        self.evaluator = evaluator
        self.high_fidelity_evaluator = high_fidelity_evaluator
        self.owner = owner
        self.now = now

    def _cfg(self):
        return self.settings.prompt_evolution

    async def run(self, dry_run: bool = True) -> tuple[EvolutionRun, ...]:
        cfg = self._cfg()
        if not dry_run and not cfg.enabled:
            raise ValueError("publish mode requires prompt_evolution.enabled=true")
        for existing in self.store.list_reconcilable_mrs():
            if existing.target_project == str(getattr(cfg, "target_project", "") or ""):
                continue
            try:
                publisher = self.publisher_factory(existing.target_project)
                state = publisher.get_mr_state(existing.mr_iid)
                self.store.mark_mr_state(
                    existing.mr_iid,
                    state,
                    self.now.isoformat(),
                    target_project=existing.target_project,
                )
            except Exception as exc:
                get_logger().warning(f"Project Skill MR reconcile failed for {existing.mr_iid}: {exc}")
        snapshot = self.evidence_loader.load(
            prior_watermark=self.store.get_watermark(),
            window_days=cfg.window_days,
            unhandled_after_days=cfg.unhandled_after_days,
            now=self.now,
        )
        if not snapshot.has_new_signal:
            return ()

        eligible_evidence = tuple(
            evidence
            for evidence in snapshot.evidence
            if evidence.outcome in {Outcome.ACCEPTED, Outcome.REJECTED, Outcome.UNHANDLED}
        )
        cluster_result = self.clusterer.cluster(evidence=eligible_evidence, system_prefix="", user_template="")
        if inspect.isawaitable(cluster_result):
            cluster_result = await cluster_result
        clusters, errors = cluster_result
        if errors and not clusters:
            raise RuntimeError(f"project Skill clustering unavailable: {errors[0][1]}")
        global_hash = compute_global_prompt_set_hash()
        candidates = self.aggregator.select(clusters, _aggregation_thresholds(cfg), global_hash) if clusters else ()
        candidates = tuple(
            candidate
            for candidate in sorted(candidates, key=lambda item: (-item.cluster.negative_weight, item.candidate_id))
            if candidate.scope is CandidateScope.PROJECT and candidate.project
        )[: cfg.max_candidates_per_run]

        grouped: dict[str, list[EligibleCandidate]] = defaultdict(list)
        for candidate in candidates:
            if self.store.candidate_is_suppressed(
                _candidate_fingerprint(candidate),
                candidate.source_prompt_hash,
                candidate.cluster.negative_weight,
                self.now.isoformat(),
                cfg.closed_candidate_cooldown_days,
            ):
                continue
            grouped[str(candidate.project)].append(candidate)

        results = []
        for project in sorted(grouped):
            result = await self._run_project(
                project,
                tuple(grouped[project]),
                snapshot.watermark,
                global_hash,
                dry_run,
                tuple(item for item in eligible_evidence if item.project == project),
            )
            results.append(result)
            if result.status is EvolutionRunStatus.FAILED_RETRYABLE:
                break
        return tuple(results)

    async def _run_project(self, project: str, candidates: tuple[EligibleCandidate, ...],
                           source_watermark: str, global_hash: str, dry_run: bool,
                           project_evidence: tuple = ()) -> EvolutionRun:
        cfg = self._cfg()
        lease = await self.leases.acquire(project, self.owner, cfg.lease_seconds)
        lost = asyncio.Event()
        stop = asyncio.Event()
        renewal = asyncio.create_task(self._renew_lease(lease, lost, stop))
        batch_id = ""
        run: EvolutionRun | None = None
        try:
            publisher = self.publisher_factory(project)
            target_branch = publisher.get_default_branch()
            base_sha = publisher.get_target_head(target_branch)
            batch_id = _batch_id(self.now, project, target_branch, dry_run)
            workspace = publisher.load_workspace(
                project,
                target_branch,
                base_sha,
                (PROJECT_SKILL_MANIFEST_PATH,),
            )
            content = workspace.files.get(PROJECT_SKILL_MANIFEST_PATH)
            manifest_hash = hashlib.sha256((content or "").encode("utf-8")).hexdigest() if content else ""
            run = self.store.start_run(
                batch_id,
                project,
                target_branch,
                base_sha,
                global_hash,
                manifest_hash,
                lease.fencing_token,
            )
            if run.status in _TERMINAL:
                return run

            for existing in self.store.list_reconcilable_mrs():
                if existing.target_project != project:
                    continue
                try:
                    state = publisher.get_mr_state(existing.mr_iid)
                    self.store.mark_mr_state(
                        existing.mr_iid,
                        state,
                        self.now.isoformat(),
                        target_project=project,
                    )
                except Exception as exc:
                    get_logger().warning(f"Project Skill MR reconcile failed for {existing.mr_iid}: {exc}")

            if content is None:
                self.store.update_run(
                    run.run_id,
                    EvolutionRunStatus.COMPLETED_NO_CHANGE,
                    error_code="project_skill_not_opted_in",
                    error_message="target repository has no Project Review Skill manifest",
                )
                return self._rehydrate(batch_id)
            try:
                rules = parse_project_rules(content, project)
            except ValueError as exc:
                self.store.update_run(
                    run.run_id,
                    EvolutionRunStatus.FAILED_TERMINAL,
                    error_code="project_skill_invalid",
                    error_message=_sanitize_error_message(exc),
                )
                return self._rehydrate(batch_id)

            if not self._evidence_matches_manifest(candidates, rules.manifest_hash, project_rules_hash(rules)):
                self.store.update_run(
                    run.run_id,
                    EvolutionRunStatus.SUPERSEDED,
                    error_code="project_skill_version_mismatch",
                    error_message="target Skill changed after the recorded evidence",
                )
                return self._rehydrate(batch_id)

            if not bool(getattr(cfg, "project_skill_optimizer_enabled", False)):
                self.store.update_run(
                    run.run_id,
                    EvolutionRunStatus.INSUFFICIENT_VALIDATION,
                    error_code="project_skill_optimizer_disabled",
                    error_message="project Skill optimization is disabled",
                )
                return self._rehydrate(batch_id)

            self.store.save_source_snapshot(run.run_id, project_evidence)
            try:
                optimization_batch = build_optimization_batch(
                    candidates,
                    project_evidence,
                    project=project,
                    base_manifest_hash=manifest_hash,
                    selection_ratio=float(cfg.project_skill_optimizer_selection_ratio),
                    min_train_mrs=int(cfg.project_skill_optimizer_min_train_mrs),
                    min_selection_mrs=int(cfg.project_skill_optimizer_min_selection_mrs),
                    min_control_cases=int(cfg.project_skill_optimizer_min_control_cases),
                    max_selection_cases=int(cfg.project_skill_optimizer_max_selection_cases),
                )
            except InsufficientValidationEvidence as exc:
                self.store.update_run(
                    run.run_id,
                    EvolutionRunStatus.INSUFFICIENT_VALIDATION,
                    error_code="insufficient_selection_evidence",
                    error_message=_sanitize_error_message(exc),
                )
                return self._rehydrate(batch_id)

            for candidate in candidates:
                stored_id = self.store.save_candidate(
                    run.run_id,
                    candidate,
                    _candidate_fingerprint(candidate),
                )
                self.store.save_evidence_snapshot(stored_id, candidate.cluster.evidence)

            rejected_edits = self.store.get_rejected_edit_buffer(
                project,
                manifest_hash,
                int(cfg.project_skill_optimizer_rejected_buffer_size),
            )
            self.store.update_run(run.run_id, EvolutionRunStatus.GENERATING, source_watermark=source_watermark)
            try:
                proposal = await self.agent.generate(
                    optimization_batch.training_candidates,
                    workspace,
                    rejected_edits=rejected_edits,
                )
            except Exception as exc:
                return self._fail_retryable(run, batch_id, "project_skill_generation_failed", exc)

            self.store.update_run(run.run_id, EvolutionRunStatus.VALIDATING)
            report = self.validator.validate(
                proposal,
                optimization_batch.training_candidates,
                workspace,
                max_files=1,
                max_prompt_file_chars=cfg.max_prompt_file_chars,
                max_diff_lines=cfg.max_diff_lines,
                max_project_rule_edits=int(cfg.project_skill_optimizer_edit_budget),
            )
            if not report.passed:
                try:
                    proposal = await self.agent.regenerate(
                        optimization_batch.training_candidates,
                        workspace,
                        report,
                        rejected_edits=rejected_edits,
                    )
                    report = self.validator.validate(
                        proposal,
                        optimization_batch.training_candidates,
                        workspace,
                        max_files=1,
                        max_prompt_file_chars=cfg.max_prompt_file_chars,
                        max_diff_lines=cfg.max_diff_lines,
                        max_project_rule_edits=int(cfg.project_skill_optimizer_edit_budget),
                    )
                except Exception as exc:
                    return self._fail_retryable(run, batch_id, "project_skill_regeneration_failed", exc)
            self.store.save_proposal(run.run_id, proposal)
            if not report.passed:
                self.store.save_validation(run.run_id, report)
                self.store.update_run(
                    run.run_id,
                    EvolutionRunStatus.FAILED_TERMINAL,
                    error_code="project_skill_validation_failed",
                    error_message=_sanitize_error_message(",".join(report.errors)),
                )
                return self._rehydrate(batch_id)

            candidate_content = proposal.changes[0].content
            semantic_diff = semantic_skill_diff(content, candidate_content, project)
            rejected_signatures = tuple(
                str(item.get("edit_signature") or "") for item in rejected_edits
            )
            if semantic_diff.signature in rejected_signatures:
                optimization_report = _repeated_edit_report(
                    optimization_batch.split_hash,
                    semantic_diff.signature,
                    semantic_diff.edit_count,
                    int(cfg.project_skill_optimizer_edit_budget),
                )
            else:
                try:
                    baseline_replay, candidate_replay = await self.evaluator.replay_pair(
                        optimization_batch,
                        content,
                        candidate_content,
                    )
                except Exception as exc:
                    return self._fail_retryable(run, batch_id, "project_skill_replay_failed", exc)
                optimization_report = evaluate_skill_gate(
                    optimization_batch,
                    baseline_replay,
                    candidate_replay,
                    minimum_score_delta=str(cfg.project_skill_optimizer_minimum_score_delta),
                    edit_budget=int(cfg.project_skill_optimizer_edit_budget),
                    edit_count=semantic_diff.edit_count,
                    edit_signature=semantic_diff.signature,
                    rejected_signatures=rejected_signatures,
                )
            candidate_hash = hashlib.sha256(candidate_content.encode("utf-8")).hexdigest()
            proposal_hash = hashlib.sha256(
                (proposal.rationale + "\n" + candidate_content).encode("utf-8")
            ).hexdigest()
            report = ValidationReport(
                passed=report.passed and optimization_report.passed,
                errors=tuple(sorted(set(report.errors + optimization_report.errors))),
                checks=tuple(dict.fromkeys(report.checks + optimization_report.checks)),
            )
            if not optimization_report.passed:
                self.store.save_optimization_step(
                    run.run_id,
                    project,
                    manifest_hash,
                    candidate_hash,
                    optimization_batch,
                    optimization_report,
                    proposal_hash,
                    execution_mode="fragment",
                )
                self.store.save_validation(run.run_id, report)
                self.store.update_run(
                    run.run_id,
                    EvolutionRunStatus.OPTIMIZATION_REJECTED,
                    error_code="skillopt_gate_rejected",
                    error_message=_sanitize_error_message(",".join(optimization_report.errors)),
                )
                return self._rehydrate(batch_id)
            high_fidelity_report = None
            high_fidelity_enabled = bool(getattr(cfg, "project_skill_high_fidelity_enabled", False))
            high_fidelity_mode = str(
                getattr(cfg, "project_skill_high_fidelity_gate_mode", "enforce") or "enforce"
            ).lower()
            if high_fidelity_enabled:
                if self.high_fidelity_evaluator is None:
                    return self._fail_retryable(
                        run,
                        batch_id,
                        "project_skill_high_fidelity_unavailable",
                        "high-fidelity evaluator is not configured",
                    )
                try:
                    configured_model = str(
                        getattr(getattr(self.settings, "config", None), "model", "")
                        or get_settings().config.model
                    )
                    high_fidelity_report = await self.high_fidelity_evaluator.evaluate_pair(
                        optimization_batch,
                        content,
                        candidate_content,
                        target_sha=base_sha,
                        model=configured_model,
                        minimum_score_delta=float(cfg.project_skill_optimizer_minimum_score_delta),
                    )
                except Exception as exc:
                    return self._fail_retryable(
                        run, batch_id, "project_skill_high_fidelity_failed", exc,
                    )
                if not high_fidelity_report.passed:
                    insufficient_high_fidelity = (
                        "insufficient_high_fidelity_evidence" in high_fidelity_report.errors
                    )
                    optimization_report = replace(
                        optimization_report,
                        passed=False,
                        action="insufficient" if insufficient_high_fidelity else "reject",
                        errors=tuple(dict.fromkeys(
                            optimization_report.errors + high_fidelity_report.errors
                        )),
                        checks=tuple(dict.fromkeys(
                            optimization_report.checks + high_fidelity_report.checks
                        )),
                    )
                report = ValidationReport(
                    passed=report.passed and high_fidelity_report.passed,
                    errors=tuple(dict.fromkeys(report.errors + high_fidelity_report.errors)),
                    checks=tuple(dict.fromkeys(report.checks + high_fidelity_report.checks)),
                )
            self.store.save_optimization_step(
                run.run_id,
                project,
                manifest_hash,
                candidate_hash,
                optimization_batch,
                optimization_report,
                proposal_hash,
                high_fidelity_report=high_fidelity_report,
                execution_mode=high_fidelity_mode if high_fidelity_enabled else "fragment",
            )
            self.store.save_validation(run.run_id, report)
            if high_fidelity_report is not None and not high_fidelity_report.passed:
                insufficient = "insufficient_high_fidelity_evidence" in high_fidelity_report.errors
                self.store.update_run(
                    run.run_id,
                    EvolutionRunStatus.INSUFFICIENT_VALIDATION if insufficient else EvolutionRunStatus.OPTIMIZATION_REJECTED,
                    error_code=(
                        "insufficient_high_fidelity_evidence" if insufficient else "high_fidelity_gate_rejected"
                    ),
                    error_message=_sanitize_error_message(",".join(high_fidelity_report.errors)),
                )
                return self._rehydrate(batch_id)
            gate_mode = str(cfg.project_skill_optimizer_gate_mode or "").lower()
            if dry_run or gate_mode == "shadow" or (high_fidelity_enabled and high_fidelity_mode == "shadow"):
                self.store.update_run(run.run_id, EvolutionRunStatus.DRY_RUN_VALIDATED)
                return self._rehydrate(batch_id)
            if lost.is_set():
                return self._fail_retryable(run, batch_id, "lease_lost", "lease renewal failed")
            try:
                await self.leases.assert_current(lease)
            except (LostEvolutionLease, RuntimeError) as exc:
                return self._fail_retryable(run, batch_id, "lease_lost", exc)

            self.store.update_run(run.run_id, EvolutionRunStatus.PUBLISHING)

            async def assert_fence():
                if lost.is_set():
                    raise LostEvolutionLease("lease renewal failed")
                await self.leases.assert_current(lease)

            branch_prefix = str(getattr(cfg, "project_skill_branch_prefix", "") or
                                "codex/review-skill-evolution")
            branch_name = f"{branch_prefix}/{batch_id}"
            try:
                draft = await publisher.publish_draft_mr(
                    batch_id=batch_id,
                    branch_name=branch_name,
                    target_branch=target_branch,
                    base_sha=base_sha,
                    changes=proposal.changes,
                    description=_render_project_description(
                        project,
                        batch_id,
                        proposal,
                        report,
                        optimization_report,
                    ),
                    assert_fence=assert_fence,
                )
            except Exception as exc:
                return self._fail_retryable(run, batch_id, "project_skill_publish_failed", exc)
            self.store.update_run(
                run.run_id,
                EvolutionRunStatus.MR_OPEN,
                branch_name=branch_name,
                commit_sha=draft.commit_sha,
                mr_iid=draft.mr_iid,
                mr_url=draft.mr_url,
            )
            return self._rehydrate(batch_id)
        finally:
            stop.set()
            await renewal
            try:
                await self.leases.release(lease)
            except Exception as exc:
                get_logger().warning(f"Project Skill lease release failed for {project}: {exc}")

    async def _renew_lease(self, lease, lost: asyncio.Event, stop: asyncio.Event) -> None:
        interval = float(max(1, int(self._cfg().lease_seconds) // 3))
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                if not await self.leases.renew(lease, self._cfg().lease_seconds):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    @staticmethod
    def _evidence_matches_manifest(candidates: tuple[EligibleCandidate, ...],
                                   manifest_hash: str, legacy_rules_hash: str) -> bool:
        evidence = tuple(item for candidate in candidates for item in candidate.cluster.evidence)
        if any(item.project_skill_status and item.project_skill_status != SKILL_STATUS_LOADED for item in evidence):
            return False
        recorded_manifest_hashes = {
            item.project_skill_manifest_hash for item in evidence if item.project_skill_manifest_hash
        }
        if recorded_manifest_hashes:
            return recorded_manifest_hashes == {manifest_hash}
        legacy_hashes = {item.project_rules_hash for item in evidence if item.project_rules_hash}
        return bool(legacy_hashes) and legacy_hashes == {legacy_rules_hash}

    def _fail_retryable(self, run: EvolutionRun, batch_id: str, code: str, error: object) -> EvolutionRun:
        self.store.update_run(
            run.run_id,
            EvolutionRunStatus.FAILED_RETRYABLE,
            error_code=code,
            error_message=_sanitize_error_message(error),
        )
        return self._rehydrate(batch_id)

    def _rehydrate(self, batch_id: str) -> EvolutionRun:
        run = self.store.get_run_by_batch(batch_id)
        assert run is not None
        return run


def _repeated_edit_report(
    split_hash: str,
    edit_signature: str,
    edit_count: int,
    edit_budget: int,
) -> SkillOptimizationReport:
    return SkillOptimizationReport(
        passed=False,
        action="reject",
        errors=("repeated_rejected_edit",),
        checks=("rejected_edit_buffer", "textual_learning_rate"),
        split_hash=split_hash,
        replay_model="",
        baseline_score="0",
        candidate_score="0",
        baseline_accepted_score="0",
        candidate_accepted_score="0",
        baseline_rejected_score="0",
        candidate_rejected_score="0",
        accepted_control_regressions=(),
        rejected_target_regressions=(),
        edit_budget=edit_budget,
        edit_count=edit_count,
        edit_signature=edit_signature,
    )


def _render_project_description(project, batch_id, proposal, report, optimization) -> str:
    return f"""## Project Review Skill evolution
Project: {project}
Batch: {batch_id}

## Evidence
Linked evidence: {', '.join(proposal.evidence_ids)}

## Proposed rule change
{proposal.rationale}

## Validation
passed={report.passed}
checks={', '.join(report.checks)}
errors={', '.join(report.errors) if report.errors else 'none'}

## SkillOpt selection gate
baseline_score={optimization.baseline_score}
candidate_score={optimization.candidate_score}
baseline_accepted_score={optimization.baseline_accepted_score}
candidate_accepted_score={optimization.candidate_accepted_score}
baseline_rejected_score={optimization.baseline_rejected_score}
candidate_rejected_score={optimization.candidate_rejected_score}
split_hash={optimization.split_hash}
replay_model={optimization.replay_model}
edit_budget={optimization.edit_budget}
edit_count={optimization.edit_count}

## Safety boundary
Only `.pr_agent/skills/review/skill.toml` is changed. References, scripts, tools,
CI configuration, and business code are outside this MR's writable surface.
This Draft MR requires the project owner's approval and is never auto-merged.

## Rollback
Close this Draft MR, or revert its single Skill commit after merge.
"""


class PromptEvolutionCoordinator:
    """Run project Skill evolution before the global evolution watermark."""

    def __init__(self, project_runner: ProjectSkillEvolutionRunner, global_runner) -> None:
        self.project_runner = project_runner
        self.global_runner = global_runner
        # Compatibility/introspection attributes used by health checks and tests.
        for name in (
            "settings", "store", "leases", "publisher", "evidence_loader",
            "clusterer", "aggregator", "agent", "validator", "owner", "now",
        ):
            setattr(self, name, getattr(global_runner, name))

    async def run(self, dry_run: bool = True) -> EvolutionRun:
        project_results = await self.project_runner.run(dry_run=dry_run)
        for result in project_results:
            if result.status is EvolutionRunStatus.FAILED_RETRYABLE:
                return result
        return await self.global_runner.run(dry_run=dry_run)
