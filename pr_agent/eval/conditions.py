"""Canonical paired-replay conditions and deterministic Skill rollout policy."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from enum import StrEnum

_IMMUTABLE_REF = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class EvaluationTreatment(StrEnum):
    PROJECT_SKILL = "project_skill"
    GLOBAL_PROMPT = "global_prompt"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvaluationConditionManifest:
    schema_version: int
    project: str
    mr_iid: str
    command: str
    base_sha: str
    head_sha: str
    target_sha: str
    model: str
    temperature: float
    max_model_tokens: int
    global_prompt_set_hash: str
    prompt_bundle_hash: str
    config_hash: str
    diff_hash: str
    chunk_plan_hash: str
    context_hash: str
    output_schema: str
    parser_version: str
    skill_hash: str
    captured_at: str

    def canonical_payload(self, *, include_skill: bool = True) -> dict:
        payload = asdict(self)
        if not include_skill:
            payload.pop("skill_hash", None)
        return payload

    @property
    def manifest_hash(self) -> str:
        return _hash(self.canonical_payload())

    @property
    def comparable_hash(self) -> str:
        return _hash(self.canonical_payload(include_skill=False))


@dataclass(frozen=True)
class PairedConditionCheck:
    matched: bool
    mismatched_fields: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class CanarySelection:
    selected: bool
    bucket: int
    percent: int
    approved_ref: str
    assignment_hash: str
    reason: str


def build_condition_manifest(
    *,
    project: str,
    mr_iid: str,
    command: str,
    base_sha: str,
    head_sha: str,
    target_sha: str,
    model: str,
    temperature: float,
    max_model_tokens: int,
    global_prompt_set_hash: str,
    prompt_bundle_hash: str,
    config: object,
    diff_hash: str,
    chunk_plan_hash: str,
    context_hash: str,
    output_schema: str,
    parser_version: str,
    skill_hash: str,
    captured_at: str,
) -> EvaluationConditionManifest:
    if command not in {"review", "improve"}:
        raise ValueError("evaluation command must be review or improve")
    return EvaluationConditionManifest(
        schema_version=1,
        project=str(project),
        mr_iid=str(mr_iid),
        command=command,
        base_sha=str(base_sha),
        head_sha=str(head_sha),
        target_sha=str(target_sha),
        model=str(model),
        temperature=float(temperature),
        max_model_tokens=int(max_model_tokens),
        global_prompt_set_hash=str(global_prompt_set_hash),
        prompt_bundle_hash=str(prompt_bundle_hash),
        config_hash=_hash(config),
        diff_hash=str(diff_hash),
        chunk_plan_hash=str(chunk_plan_hash),
        context_hash=str(context_hash),
        output_schema=str(output_schema),
        parser_version=str(parser_version),
        skill_hash=str(skill_hash),
        captured_at=str(captured_at),
    )


def compare_paired_conditions(
    baseline: EvaluationConditionManifest,
    candidate: EvaluationConditionManifest,
    *,
    treatment: EvaluationTreatment = EvaluationTreatment.PROJECT_SKILL,
) -> PairedConditionCheck:
    if treatment is EvaluationTreatment.PROJECT_SKILL:
        treatment_fields = {"skill_hash"}
        if baseline.skill_hash == candidate.skill_hash:
            return PairedConditionCheck(False, error="skill_hash_unchanged")
    else:
        treatment_fields = {"global_prompt_set_hash", "prompt_bundle_hash"}
        if all(getattr(baseline, name) == getattr(candidate, name) for name in treatment_fields):
            return PairedConditionCheck(False, error="prompt_hash_unchanged")
    mismatched = tuple(
        field.name
        for field in fields(EvaluationConditionManifest)
        if field.name not in treatment_fields and getattr(baseline, field.name) != getattr(candidate, field.name)
    )
    return PairedConditionCheck(not mismatched, mismatched, "condition_mismatch" if mismatched else "")


def select_canary(
    project: str,
    mr_iid: str,
    head_sha: str,
    *,
    approved_ref: str,
    percent: int,
) -> CanarySelection:
    ref = str(approved_ref or "")
    if not _IMMUTABLE_REF.fullmatch(ref):
        raise ValueError("canary requires an immutable lowercase commit ref")
    percentage = int(percent)
    if percentage < 0 or percentage > 100:
        raise ValueError("canary percent must be between 0 and 100")
    assignment = f"{project}\0{mr_iid}\0{head_sha}".encode("utf-8")
    digest = hashlib.sha256(assignment).hexdigest()
    bucket = int.from_bytes(bytes.fromhex(digest[:16]), "big") % 100
    selected = bucket < percentage
    return CanarySelection(
        selected=selected,
        bucket=bucket,
        percent=percentage,
        approved_ref=ref,
        assignment_hash=digest,
        reason="selected" if selected else "stable_skill",
    )
