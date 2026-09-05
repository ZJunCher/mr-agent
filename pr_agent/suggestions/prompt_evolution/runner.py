"""Weekly Prompt evolution runner: state-machine orchestration.

Every side effect is injectable so tests stay network-free. The runner
acquires a Redis lease, reconciles MR state, freezes a Prompt workspace,
clusters evidence, generates one proposal, validates it, and publishes at
most one Draft MR per ISO week. Failures map to FAILED_RETRYABLE or
FAILED_TERMINAL; the same batch resumes from immutable snapshots.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from datetime import datetime, timedelta, timezone

from pr_agent.algo.language_router import language_scope_for_file

# Import config_loader before pr_agent.log to avoid a circular import
# (pr_agent.log -> config_loader -> Dynaconf -> custom_merge_loader -> pr_agent.log).
from pr_agent.config_loader import get_settings  # noqa: F401  (import side-effect ordering)
from pr_agent.log import get_logger
from pr_agent.suggestions.project_prompt_rules import (
    ProjectRuleSet,
    filter_project_rules,
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
    PromptProposal,
    ValidationReport,
)
from pr_agent.suggestions.prompt_evolution.project_skill_optimizer import (
    InsufficientValidationEvidence,
)
from pr_agent.suggestions.prompt_evolution.prompt_evaluation_batch import build_prompt_evaluation_batch
from pr_agent.suggestions.prompt_evolution.prompt_surface import GLOBAL_PROMPT_PATHS, project_rule_repo_path
from pr_agent.suggestions.prompt_provenance import (
    compute_global_prompt_set_hash,
    compute_prompt_set_hash_from_contents,
)

_CN = timezone(timedelta(hours=8))
_TERMINAL_RESUME = {
    EvolutionRunStatus.MR_OPEN, EvolutionRunStatus.MERGED, EvolutionRunStatus.CLOSED,
    EvolutionRunStatus.COMPLETED_NO_CHANGE, EvolutionRunStatus.DRY_RUN_VALIDATED,
    EvolutionRunStatus.OPTIMIZATION_REJECTED, EvolutionRunStatus.INSUFFICIENT_VALIDATION,
    EvolutionRunStatus.FAILED_TERMINAL, EvolutionRunStatus.SUPERSEDED,
}


class PromptEvolutionUnavailable(RuntimeError):
    """Raised when Redis or required infrastructure is unavailable."""


async def _wait_for_event(event: asyncio.Event, timeout: float) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return True
    except TimeoutError:
        return False


def _sanitize_error_message(value: object, limit: int = 500) -> str:
    message = " ".join(str(value).split())
    message = re.sub(r"(?i)(token|password|api[_-]?key)\s*[:=]\s*\S+", r"\1=[REDACTED]", message)
    message = re.sub(r"(?i)\b(?:glpat-|sk-)[A-Za-z0-9_-]+", "[REDACTED]", message)
    return message[:limit]


def _iso_week_slug(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-w{iso.week:02d}"


def _batch_id(now: datetime, target_project: str, target_branch: str, dry_run: bool) -> str:
    slug = hashlib.sha256(f"{target_project}\n{target_branch}".encode("utf-8")).hexdigest()[:8]
    batch = f"{_iso_week_slug(now)}-{slug}"
    return f"{batch}-dry" if dry_run else batch


def _candidate_fingerprint(candidate: EligibleCandidate) -> str:
    raw = f"{candidate.scope.value}\n{candidate.project or ''}\n{candidate.cluster.cluster_key}\n{candidate.source_prompt_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PromptEvolutionRunner:
    def __init__(self, *, settings, store, leases, publisher, evidence_loader,
                 clusterer, aggregator, agent, validator, owner: str, now: datetime,
                 candidate_scopes: frozenset[CandidateScope] | None = None,
                 high_fidelity_evaluator=None, behavioral_model: str = "") -> None:
        self.settings = settings
        self.store = store
        self.leases = leases
        self.publisher = publisher
        self.evidence_loader = evidence_loader
        self.clusterer = clusterer
        self.aggregator = aggregator
        self.agent = agent
        self.validator = validator
        self.owner = owner
        self.now = now
        self.candidate_scopes = candidate_scopes or frozenset(CandidateScope)
        self.high_fidelity_evaluator = high_fidelity_evaluator
        self.behavioral_model = behavioral_model

    def _cfg(self):
        return self.settings.prompt_evolution

    async def run(self, dry_run: bool = True) -> EvolutionRun:
        cfg = self._cfg()
        if not dry_run and not cfg.enabled:
            raise ValueError("publish mode requires prompt_evolution.enabled=true")
        if not cfg.target_project or not cfg.target_branch:
            raise ValueError("target_project and target_branch are required")

        # 1. Acquire lease (fail before any source/model/GitLab call).
        lease = None
        renewal_task = None
        lost_lease = asyncio.Event()
        stop_renewal = asyncio.Event()
        try:
            try:
                lease = await self.leases.acquire(cfg.target_project, self.owner, cfg.lease_seconds)
            except (ConnectionError, OSError) as exc:
                raise PromptEvolutionUnavailable(f"redis unavailable: {exc}") from exc
            renewal_task = asyncio.create_task(self._renew_lease(lease, lost_lease, stop_renewal))
            return await self._run_with_lease(dry_run, lease, lost_lease)
        finally:
            stop_renewal.set()
            if renewal_task is not None:
                await renewal_task
            if lease is not None:
                try:
                    await self.leases.release(lease)
                except Exception as exc:
                    get_logger().warning(f"lease release failed: {exc}")

    def _lease_renew_interval_seconds(self) -> float:
        return float(max(1, int(self._cfg().lease_seconds) // 3))

    async def _renew_lease(self, lease, lost: asyncio.Event, stop: asyncio.Event) -> None:
        while not await _wait_for_event(stop, self._lease_renew_interval_seconds()):
            try:
                if not await self.leases.renew(lease, self._cfg().lease_seconds):
                    lost.set()
                    return
            except Exception as exc:
                get_logger().warning(f"prompt evolution lease renewal failed: {type(exc).__name__}")
                lost.set()
                return

    async def _run_with_lease(self, dry_run: bool, lease, lost_lease: asyncio.Event) -> EvolutionRun:
        cfg = self._cfg()

        # 2. Reconcile MR state before candidate selection.
        for run in self.store.list_reconcilable_mrs():
            if run.target_project != cfg.target_project:
                continue
            try:
                state = self.publisher.get_mr_state(run.mr_iid)
                self.store.mark_mr_state(
                    run.mr_iid,
                    state,
                    self.now.isoformat(),
                    target_project=run.target_project,
                )
            except Exception as exc:
                get_logger().warning(f"MR reconcile failed for {run.mr_iid}: {exc}")

        # 3. Compute hashes and freeze base SHA.
        global_hash = compute_global_prompt_set_hash()
        base_sha = self.publisher.get_target_head(cfg.target_branch)
        workspace = self.publisher.load_workspace(
            cfg.target_project, cfg.target_branch, base_sha, tuple(GLOBAL_PROMPT_PATHS))
        # target_prompt_set_hash reflects only the files actually present in the
        # frozen workspace (production reads all 14; tests may supply a subset).
        target_contents = {
            path.replace("pr_agent/settings/", "", 1): (content or "")
            for path, content in workspace.files.items()
        }
        target_paths = tuple(sorted(target_contents))
        target_hash = compute_prompt_set_hash_from_contents(target_contents, target_paths) if target_paths else ""

        # 4. Build batch ID and start/resume run.
        batch_id = _batch_id(self.now, cfg.target_project, cfg.target_branch, dry_run)
        run = self.store.start_run(batch_id, cfg.target_project, cfg.target_branch, base_sha,
                                   global_hash, target_hash, lease.fencing_token)

        # 5. Resume terminal runs as-is.
        if run.status in _TERMINAL_RESUME:
            return run

        if target_hash != global_hash:
            self.store.update_run(
                run.run_id,
                EvolutionRunStatus.FAILED_RETRYABLE,
                error_code="target_prompt_version_mismatch",
                error_message="target branch Prompt files do not match the deployed Prompt version",
            )
            return self._rehydrate(batch_id)

        # 6. Load source snapshot.
        prior_watermark = self.store.get_watermark()
        try:
            snapshot = self.evidence_loader.load(
                prior_watermark=prior_watermark,
                window_days=cfg.window_days,
                unhandled_after_days=cfg.unhandled_after_days,
                now=self.now,
            )
        except Exception as exc:
            return self._fail_retryable(run.run_id, batch_id, "evidence_source_unavailable", exc)
        self.store.save_source_snapshot(run.run_id, snapshot.evidence)
        if not snapshot.has_new_signal:
            self.store.update_run(run.run_id, EvolutionRunStatus.COMPLETED_NO_CHANGE)
            if not dry_run:
                self.store.set_watermark(snapshot.watermark)
            return self._rehydrate(batch_id)

        # 7. Cluster and aggregate.
        self.store.update_run(run.run_id, EvolutionRunStatus.AGGREGATING)
        eligible_evidence = tuple(e for e in snapshot.evidence
                                  if e.outcome in {Outcome.ACCEPTED, Outcome.REJECTED, Outcome.UNHANDLED})
        try:
            cluster_result = self.clusterer.cluster(
                evidence=eligible_evidence,
                system_prefix="",
                user_template="",
            )
            if inspect.isawaitable(cluster_result):
                cluster_result = await cluster_result
            clusters, errors = cluster_result
        except Exception as exc:
            return self._fail_retryable(run.run_id, batch_id, "clustering_failed", exc)
        if errors and not clusters:
            return self._fail_retryable(
                run.run_id, batch_id, "clustering_models_unavailable", errors[0][1]
            )
        if clusters:
            candidates = self.aggregator.select(clusters, _aggregation_thresholds(cfg), global_hash)
        else:
            candidates = ()
        candidates = sorted(candidates, key=lambda c: (-c.cluster.negative_weight, c.candidate_id))
        candidates = [candidate for candidate in candidates if candidate.scope in self.candidate_scopes]
        candidates = candidates[: cfg.max_candidates_per_run]
        # Suppress already-open/merged/closed fingerprints.
        candidates = tuple(
            c for c in candidates
            if not self.store.candidate_is_suppressed(
                _candidate_fingerprint(c), c.source_prompt_hash,
                c.cluster.negative_weight, self.now.isoformat(),
                cfg.closed_candidate_cooldown_days)
        )
        try:
            candidates, workspace, project_mismatch = self._load_project_workspaces(
                candidates, workspace, cfg, base_sha
            )
        except Exception as exc:
            return self._fail_retryable(run.run_id, batch_id, "project_rule_workspace_invalid", exc)
        if not candidates:
            fields = {}
            if project_mismatch:
                fields = {
                    "error_code": "project_prompt_version_mismatch",
                    "error_message": "project Prompt rules no longer match the evidence version",
                }
            self.store.update_run(run.run_id, EvolutionRunStatus.COMPLETED_NO_CHANGE, **fields)
            if not dry_run:
                self.store.set_watermark(snapshot.watermark)
            return self._rehydrate(batch_id)

        evaluation_batch = None
        high_fidelity_enabled = bool(getattr(cfg, "global_prompt_high_fidelity_enabled", False))
        if high_fidelity_enabled:
            if self.high_fidelity_evaluator is None:
                return self._fail_retryable(
                    run.run_id,
                    batch_id,
                    "global_prompt_high_fidelity_unavailable",
                    "high-fidelity evaluator is not configured",
                )
            try:
                evaluation_batch = build_prompt_evaluation_batch(
                    candidates,
                    snapshot.evidence,
                    base_prompt_hash=global_hash,
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
                    error_code="insufficient_global_prompt_evaluation_evidence",
                    error_message=_sanitize_error_message(exc),
                )
                return self._rehydrate(batch_id)
            self.store.save_prompt_evaluation_batch(run.run_id, evaluation_batch)
            candidates = evaluation_batch.training_candidates

        # 8. Save candidates and evidence snapshots.
        for candidate in candidates:
            stored_id = self.store.save_candidate(run.run_id, candidate, _candidate_fingerprint(candidate))
            self.store.save_evidence_snapshot(stored_id, candidate.cluster.evidence)

        # 9. Generate and validate proposal.
        self.store.update_run(run.run_id, EvolutionRunStatus.GENERATING)
        try:
            proposal = await self.agent.generate(candidates, workspace)
        except Exception as exc:
            get_logger().warning(f"agent generate failed: {exc}")
            return self._fail_retryable(run.run_id, batch_id, "proposal_generation_failed", exc)

        self.store.update_run(run.run_id, EvolutionRunStatus.VALIDATING)
        report = self._validate(proposal, candidates, workspace, cfg)
        if not report.passed:
            # Regenerate once with stable error codes.
            try:
                proposal = await self.agent.regenerate(candidates, workspace, report)
            except Exception as exc:
                get_logger().warning(f"agent regenerate failed: {exc}")
                return self._fail_retryable(run.run_id, batch_id, "proposal_regeneration_failed", exc)
            report = self._validate(proposal, candidates, workspace, cfg)
            if not report.passed:
                self.store.save_proposal(run.run_id, proposal)
                self.store.save_validation(run.run_id, report)
                self.store.update_run(
                    run.run_id,
                    EvolutionRunStatus.FAILED_TERMINAL,
                    error_code="proposal_validation_failed",
                    error_message=_sanitize_error_message(",".join(report.errors)),
                )
                if not dry_run:
                    self.store.set_watermark(snapshot.watermark)
                return self._rehydrate(batch_id)

        self.store.save_proposal(run.run_id, proposal)
        self.store.save_validation(run.run_id, report)

        behavioral_report = None
        if evaluation_batch is not None:
            try:
                behavioral_report = await self.high_fidelity_evaluator.evaluate_pair(
                    evaluation_batch,
                    workspace,
                    proposal,
                    model=self.behavioral_model,
                    minimum_score_delta=float(cfg.global_prompt_high_fidelity_minimum_score_delta),
                )
            except Exception as exc:
                return self._fail_retryable(
                    run.run_id,
                    batch_id,
                    "global_prompt_high_fidelity_failed",
                    exc,
                )
            self.store.save_prompt_behavioral_report(run.run_id, behavioral_report)
            if not behavioral_report.passed:
                insufficient = "insufficient_high_fidelity_evidence" in behavioral_report.errors
                self.store.update_run(
                    run.run_id,
                    EvolutionRunStatus.INSUFFICIENT_VALIDATION
                    if insufficient
                    else EvolutionRunStatus.OPTIMIZATION_REJECTED,
                    error_code=(
                        "insufficient_high_fidelity_evidence"
                        if insufficient
                        else "global_prompt_high_fidelity_gate_rejected"
                    ),
                    error_message=_sanitize_error_message(",".join(behavioral_report.errors)),
                )
                return self._rehydrate(batch_id)

        if dry_run:
            self.store.update_run(run.run_id, EvolutionRunStatus.DRY_RUN_VALIDATED)
            return self._rehydrate(batch_id)

        # 10. Publish: assert fence, then create Draft MR.
        if lost_lease.is_set():
            return self._fail_retryable(run.run_id, batch_id, "lease_lost", "lease renewal failed")
        try:
            await self.leases.assert_current(lease)
        except (LostEvolutionLease, RuntimeError) as exc:
            get_logger().warning(f"lease lost before publish: {exc}")
            return self._fail_retryable(run.run_id, batch_id, "lease_lost", exc)

        self.store.update_run(run.run_id, EvolutionRunStatus.PUBLISHING)
        try:
            async def _assert_fence():
                if lost_lease.is_set():
                    raise LostEvolutionLease("lease renewal failed")
                await self.leases.assert_current(lease)

            draft = await self.publisher.publish_draft_mr(
                batch_id=batch_id,
                branch_name=f"{cfg.branch_prefix}/{batch_id}",
                target_branch=cfg.target_branch,
                base_sha=base_sha,
                changes=proposal.changes,
                description=_render_description(
                    batch_id,
                    proposal,
                    report,
                    behavioral_report=behavioral_report,
                    split_hash=evaluation_batch.split_hash if evaluation_batch else "",
                ),
                assert_fence=_assert_fence,
            )
            self.store.update_run(run.run_id, EvolutionRunStatus.MR_OPEN,
                                  branch_name=f"{cfg.branch_prefix}/{batch_id}",
                                  commit_sha=draft.commit_sha, mr_iid=draft.mr_iid, mr_url=draft.mr_url)
            self.store.set_watermark(snapshot.watermark)
            return self._rehydrate(batch_id)
        except Exception as exc:
            get_logger().warning(f"publish failed: {exc}")
            return self._fail_retryable(run.run_id, batch_id, "publish_failed", exc)

    def _load_project_workspaces(self, candidates: tuple[EligibleCandidate, ...],
                                 global_workspace: PromptWorkspace, cfg, base_sha: str):
        paths = tuple(sorted({
            project_rule_repo_path(candidate.project)
            for candidate in candidates
            if candidate.scope is CandidateScope.PROJECT and candidate.project
        }))
        if not paths:
            return candidates, global_workspace, False
        project_workspace = self.publisher.load_workspace(
            cfg.target_project, cfg.target_branch, base_sha, paths
        )
        valid_candidates = []
        valid_paths = set()
        had_mismatch = False
        for candidate in candidates:
            if candidate.scope is not CandidateScope.PROJECT or not candidate.project:
                valid_candidates.append(candidate)
                continue
            path = project_rule_repo_path(candidate.project)
            content = project_workspace.files.get(path)
            rule_set = (
                parse_project_rules(content, candidate.project)
                if content is not None
                else ProjectRuleSet(candidate.project)
            )
            evidence_languages = {
                language
                for item in candidate.cluster.evidence
                if (language := language_scope_for_file(item.file_path)) is not None
            }
            acceptable_hashes = {
                project_rules_hash(rule_set),
                project_rules_hash(filter_project_rules(rule_set, evidence_languages)),
            }
            evidence_hashes = {item.project_rules_hash for item in candidate.cluster.evidence}
            if evidence_hashes and "" not in evidence_hashes and evidence_hashes <= acceptable_hashes:
                valid_candidates.append(candidate)
                valid_paths.add(path)
            else:
                had_mismatch = True
        files = dict(global_workspace.files)
        files.update({path: project_workspace.files[path] for path in valid_paths})
        workspace = PromptWorkspace(
            global_workspace.project_path,
            global_workspace.target_branch,
            global_workspace.base_sha,
            files,
        )
        return tuple(valid_candidates), workspace, had_mismatch

    def _validate(self, proposal, candidates, workspace, cfg):
        return self.validator.validate(
            proposal,
            candidates,
            workspace,
            max_files=cfg.max_files_per_mr,
            max_prompt_file_chars=cfg.max_prompt_file_chars,
            max_diff_lines=cfg.max_diff_lines,
        )

    def _fail_retryable(self, run_id: str, batch_id: str, code: str, error: object) -> EvolutionRun:
        self.store.update_run(
            run_id,
            EvolutionRunStatus.FAILED_RETRYABLE,
            error_code=code,
            error_message=_sanitize_error_message(error),
        )
        return self._rehydrate(batch_id)

    def _rehydrate(self, batch_id: str) -> EvolutionRun:
        run = self.store.get_run_by_batch(batch_id)
        assert run is not None
        return run


def _aggregation_thresholds(cfg):
    from pr_agent.suggestions.prompt_evolution.aggregator import AggregationThresholds
    return AggregationThresholds(
        project_min_negative_weight=cfg.project_min_negative_weight,
        project_min_negative_ratio=cfg.project_min_negative_ratio,
        project_min_mrs=cfg.project_min_mrs,
        unhandled_only_min_count=cfg.unhandled_only_min_count,
        unhandled_only_min_mrs=cfg.unhandled_only_min_mrs,
        global_min_negative_weight=cfg.global_min_negative_weight,
        global_min_negative_ratio=cfg.global_min_negative_ratio,
        global_min_projects=cfg.global_min_projects,
        global_min_mrs=cfg.global_min_mrs,
    )


def _render_description(
    batch_id: str,
    proposal: PromptProposal,
    report: ValidationReport,
    *,
    behavioral_report=None,
    split_hash: str = "",
) -> str:
    if behavioral_report is None:
        behavioral = "disabled for this runner"
    else:
        behavioral = f"""passed={behavioral_report.passed}
split_hash={split_hash}
replayed_mrs={len(behavioral_report.replayed_mrs)}
baseline_score={behavioral_report.baseline_score}
candidate_score={behavioral_report.candidate_score}
baseline_accepted_score={behavioral_report.baseline_accepted_score}
candidate_accepted_score={behavioral_report.candidate_accepted_score}
baseline_rejected_score={behavioral_report.baseline_rejected_score}
candidate_rejected_score={behavioral_report.candidate_rejected_score}
coverage=complete
checks={', '.join(behavioral_report.checks)}
errors={', '.join(behavioral_report.errors) if behavioral_report.errors else 'none'}"""
    return f"""## Prompt evolution batch
{batch_id}

## Evidence summary
Linked evidence: {', '.join(proposal.evidence_ids)}

## Global changes
(none unless listed below)

## Project changes
(none unless listed below)

## Files and rationale
{proposal.rationale}

## Static validation
passed={report.passed}
checks={', '.join(report.checks)}
errors={', '.join(report.errors) if report.errors else 'none'}

## Behavioral evaluation
{behavioral}

## Rollback
Revert this MR and redeploy PR-Agent.
"""


def build_runner_from_settings() -> PromptEvolutionRunner:
    """Compatibility shim for callers that imported the factory from this module."""
    from pr_agent.suggestions.prompt_evolution.factory import build_runner_from_settings as build

    return build()
