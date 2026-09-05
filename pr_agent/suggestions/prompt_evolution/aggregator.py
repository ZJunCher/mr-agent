"""Deterministic aggregation: weight, threshold, and scope classification.

One semantic cluster produces at most one candidate (global or project).
All thresholds are deterministic; no LLM is involved here.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from pr_agent.suggestions.prompt_evolution.models import (
    CandidateScope,
    EligibleCandidate,
    Evidence,
    Outcome,
    WeightedCluster,
)


@dataclass(frozen=True)
class AggregationThresholds:
    project_min_negative_weight: float
    project_min_negative_ratio: float
    project_min_mrs: int
    unhandled_only_min_count: int
    unhandled_only_min_mrs: int
    global_min_negative_weight: float
    global_min_negative_ratio: float
    global_min_projects: int
    global_min_mrs: int


def score_cluster(cluster_key: str, evidence: tuple[Evidence, ...]) -> WeightedCluster:
    positive = sum(item.weight for item in evidence if item.outcome is Outcome.ACCEPTED)
    negative = sum(item.weight for item in evidence if item.outcome in {Outcome.REJECTED, Outcome.UNHANDLED})
    denominator = positive + negative
    ratio = negative / denominator if denominator else 0.0
    return WeightedCluster(cluster_key, evidence, positive, negative, ratio)


def _fingerprint(scope: CandidateScope, project: str | None, cluster_key: str,
                source_prompt_hash: str) -> str:
    payload = f"{scope.value}\n{project or ''}\n{cluster_key}\n{source_prompt_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_id(scope: CandidateScope, project: str | None, cluster_key: str,
                  source_prompt_hash: str) -> str:
    # Candidate IDs are the fingerprint prefixed by scope for readability.
    return f"{scope.value}:{_fingerprint(scope, project, cluster_key, source_prompt_hash)[:16]}"


def _distinct_mrs(evidence: Iterable[Evidence]) -> int:
    return len({(e.project, e.mr_iid) for e in evidence})


def _distinct_projects(evidence: Iterable[Evidence]) -> int:
    return len({e.project for e in evidence})


def _has_explicit_rejects(evidence: Iterable[Evidence]) -> bool:
    return any(e.outcome is Outcome.REJECTED for e in evidence)


def select_eligible_candidates(
    clusters: tuple[WeightedCluster, ...],
    thresholds: AggregationThresholds,
    current_global_hash: str,
) -> tuple[EligibleCandidate, ...]:
    """Apply deterministic scope/threshold rules to each semantic cluster.

    1. Discard evidence whose global_prompt_set_hash != current_global_hash.
    2. For each project, keep only the newest bundle's evidence for eligibility.
    3. Global candidate when negative_weight >= 5, ratio >= 0.70, >= 2 projects, >= 3 MRs.
    4. Otherwise project candidate when negative_weight >= 3, ratio >= 0.70, >= 2 MRs.
    5. Project group with zero explicit rejects needs >= 12 unhandled across >= 3 MRs.
    6. Candidate IDs/fingerprints = SHA-256 of scope, project, cluster key, source hash.
    """
    candidates: list[EligibleCandidate] = []

    for cluster in clusters:
        # Step 1: only current global hash
        current = tuple(e for e in cluster.evidence if e.global_prompt_set_hash == current_global_hash)
        if not current:
            continue

        # Step 2: per-project newest bundle
        by_project: dict[str, list[Evidence]] = defaultdict(list)
        for e in current:
            by_project[e.project].append(e)

        newest_bundle_per_project: dict[str, str] = {}
        for project, items in by_project.items():
            # newest by created_at
            newest = max(items, key=lambda e: e.created_at)
            newest_bundle_per_project[project] = newest.prompt_bundle_hash

        filtered = tuple(
            e for e in current
            if e.prompt_bundle_hash == newest_bundle_per_project.get(e.project)
        )
        if not filtered:
            continue

        # Step 3: global candidate
        global_cluster = score_cluster(cluster.cluster_key, filtered)
        if (global_cluster.negative_weight >= thresholds.global_min_negative_weight
                and global_cluster.negative_ratio >= thresholds.global_min_negative_ratio
                and _distinct_projects(filtered) >= thresholds.global_min_projects
                and _distinct_mrs(filtered) >= thresholds.global_min_mrs):
            candidates.append(EligibleCandidate(
                candidate_id=_candidate_id(CandidateScope.GLOBAL, None, cluster.cluster_key, current_global_hash),
                scope=CandidateScope.GLOBAL,
                project=None,
                source_prompt_hash=current_global_hash,
                cluster=global_cluster,
            ))
            continue

        # Step 4 + 5: project candidates grouped by (project, bundle)
        by_project_bundle: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
        for e in filtered:
            by_project_bundle[(e.project, e.prompt_bundle_hash)].append(e)

        for (project, bundle), items in by_project_bundle.items():
            project_cluster = score_cluster(cluster.cluster_key, tuple(items))
            if project_cluster.negative_weight < thresholds.project_min_negative_weight:
                continue
            if project_cluster.negative_ratio < thresholds.project_min_negative_ratio:
                continue
            if _distinct_mrs(items) < thresholds.project_min_mrs:
                continue
            if not _has_explicit_rejects(items):
                unhandled = [e for e in items if e.outcome is Outcome.UNHANDLED]
                if len(unhandled) < thresholds.unhandled_only_min_count:
                    continue
                if _distinct_mrs(unhandled) < thresholds.unhandled_only_min_mrs:
                    continue
            candidates.append(EligibleCandidate(
                candidate_id=_candidate_id(CandidateScope.PROJECT, project, cluster.cluster_key, bundle),
                scope=CandidateScope.PROJECT,
                project=project,
                source_prompt_hash=bundle,
                cluster=project_cluster,
            ))

    return tuple(candidates)
