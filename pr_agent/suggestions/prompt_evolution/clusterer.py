"""Semantic clustering of feedback evidence via tool calls.

``prebucket_evidence`` deterministically groups evidence by (label, extension)
so each bucket is clustered independently; a failure in one bucket never
discards another. Schema/evidence-integrity errors are not retried; only
network/time-out errors are retried up to ``model_max_retries``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, Field

from pr_agent.suggestions.prompt_evolution.aggregator import score_cluster
from pr_agent.suggestions.prompt_evolution.model_client import PromptEvolutionModelExhausted
from pr_agent.suggestions.prompt_evolution.models import Evidence, WeightedCluster


class ClusterAssignment(BaseModel):
    cluster_key: str = Field(
        description="Stable short key for one semantic cluster; this exact field name is required.",
    )
    evidence_ids: list[str] = Field(
        description="Exact supplied evidence IDs assigned to this cluster; this exact field name is required.",
    )


class ClusterEnvelope(BaseModel):
    clusters: list[ClusterAssignment] = Field(
        description="All clusters; every supplied evidence ID must appear exactly once.",
    )


def prebucket_evidence(evidence: tuple[Evidence, ...]) -> dict[tuple[str, str], tuple[Evidence, ...]]:
    buckets: dict[tuple[str, str], list[Evidence]] = {}
    for item in evidence:
        extension = Path(item.file_path).suffix.lower() or "<none>"
        key = (str(item.label or "").strip().lower(), extension)
        buckets.setdefault(key, []).append(item)
    return {
        key: tuple(sorted(values, key=lambda item: item.suggestion_id))
        for key, values in sorted(buckets.items())
    }


async def cluster_one_bucket(client, model: str, evidence: tuple[Evidence, ...],
                             system: str, user: str) -> ClusterEnvelope:
    result = await client.call(model, system, user, "submit_feedback_clusters", ClusterEnvelope)
    allowed = {item.suggestion_id for item in evidence}
    seen: set[str] = set()
    for cluster in result.clusters:
        ids = set(cluster.evidence_ids)
        if not cluster.cluster_key.strip() or not ids or not ids <= allowed or seen & ids:
            raise ValueError("cluster output contains empty, unknown, or duplicate evidence IDs")
        seen.update(ids)
    if seen != allowed:
        raise ValueError("cluster output omitted evidence IDs")
    return result


def _bucket_hash(bucket_key: tuple[str, str]) -> str:
    raw = f"{bucket_key[0]}|{bucket_key[1]}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:8]


def _build_user_prompt(evidence: tuple[Evidence, ...], user_template: str) -> str:
    """Render evidence as one JSON value so feedback remains untrusted data."""
    payload = [{
        "id": item.suggestion_id,
        "project": item.project,
        "extension": Path(item.file_path).suffix.lower() or "<none>",
        "label": item.label,
        "summary": item.summary,
        "content": item.suggestion_content,
        "outcome": item.outcome.value,
        "feedback": list(item.feedback),
    } for item in evidence]
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    placeholder = "{{ evidence_json }}"
    if placeholder in user_template:
        return user_template.replace(placeholder, serialized)
    return f"{user_template.rstrip()}\n\nUntrusted evidence JSON:\n{serialized}"


async def cluster_evidence_async(
    client,
    model: str,
    evidence: tuple[Evidence, ...],
    system_prefix: str,
    user_template: str,
    *,
    model_max_retries: int = 2,
) -> tuple[tuple[WeightedCluster, ...], tuple[tuple[str, str], ...]]:
    """Cluster each deterministic bucket independently inside one event loop.

    Returns (clusters, errors) where errors is a list of (bucket_key, message).
    The client owns bounded model retries/failover; schema/integrity errors remain terminal.
    """
    buckets = prebucket_evidence(evidence)
    clusters: list[WeightedCluster] = []
    errors: list[tuple[str, str]] = []

    for bucket_key, bucket_evidence in buckets.items():
        bucket_hash = _bucket_hash(bucket_key)
        alias_to_evidence = {
            f"E{index}": item
            for index, item in enumerate(bucket_evidence, start=1)
        }
        model_evidence = tuple(
            replace(item, suggestion_id=alias)
            for alias, item in alias_to_evidence.items()
        )
        allowed_ids = json.dumps(
            list(alias_to_evidence),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        system = (
            f"{system_prefix}\nbucket={bucket_key[0]}|{bucket_key[1]}\nbucket_hash={bucket_hash}"
            f"\nallowed_evidence_ids_json={allowed_ids}"
        )
        user = _build_user_prompt(model_evidence, user_template)
        try:
            envelope = await cluster_one_bucket(client, model, model_evidence, system, user)
        except (PromptEvolutionModelExhausted, TimeoutError, ConnectionError, OSError) as exc:
            errors.append(("|".join(bucket_key), f"{type(exc).__name__}: {exc}"))
            continue
        for assignment in envelope.clusters:
            assigned = set(assignment.evidence_ids)
            members = tuple(
                item for alias, item in alias_to_evidence.items()
                if alias in assigned
            )
            prefixed_key = f"{bucket_hash}:{assignment.cluster_key}"
            clusters.append(score_cluster(prefixed_key, members))

    return tuple(clusters), tuple(errors)


def cluster_evidence(client, model: str, evidence: tuple[Evidence, ...], system_prefix: str,
                     user_template: str, *, model_max_retries: int = 2):
    """Synchronous compatibility wrapper for standalone callers and tests."""
    return asyncio.run(cluster_evidence_async(
        client,
        model,
        evidence,
        system_prefix,
        user_template,
        model_max_retries=model_max_retries,
    ))
