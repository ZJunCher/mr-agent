"""Outcome derivation for weekly Prompt evolution evidence.

Reads only ``published_suggestions`` (left-joined with ``mr_inventory`` and
``inline_suggestion_feedback``) and classifies each row into one of five
outcomes. Never queries the deprecated review-feedback/score table.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from pr_agent.suggestions.prompt_evolution.models import Evidence, Outcome, SourceSnapshot

_CN = timezone(timedelta(hours=8))
_SECRET_PATTERNS = re.compile(
    r"(?i)(glpat-[a-z0-9_-]+|gh[pousr]_[a-z0-9]{36,}|xox[bap]-[a-z0-9-]+|"
    r"sk-[a-z0-9]{20,}|-----begin [a-z ]+ private key-----)"
)
_MAX_FEEDBACK_CHARS = 500
_MAX_FEEDBACK_COMMENTS = 5
_MAX_REPLAY_CODE_CHARS = 12_000


def to_cn(value) -> datetime | None:
    """Parse an ISO timestamp and normalize to Asia/Shanghai tz, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CN)
    return dt.astimezone(_CN)


def classify_outcome(row: dict, mr: dict, now: datetime, unhandled_after_days: int) -> Outcome:
    """State-precedence classification.

    Order: applied > resolved > closed (invalid) > merged/old (unhandled) > pending.
    """
    if row.get("applied_at"):
        return Outcome.ACCEPTED
    if row.get("resolved_at"):
        return Outcome.REJECTED
    state = str(mr.get("state") or "").lower()
    if state == "closed":
        return Outcome.INVALID
    created = to_cn(row.get("created_at"))
    if state == "merged" or (created is not None and created <= now - timedelta(days=unhandled_after_days)):
        return Outcome.UNHANDLED
    return Outcome.PENDING


def outcome_weight(outcome: Outcome, *, accepted_weight: float, rejected_weight: float,
                   unhandled_weight: float) -> float:
    return {
        Outcome.ACCEPTED: accepted_weight,
        Outcome.REJECTED: rejected_weight,
        Outcome.UNHANDLED: unhandled_weight,
        Outcome.PENDING: 0.0,
        Outcome.INVALID: 0.0,
    }[outcome]


def _normalize_feedback(raw: str) -> str:
    """Strip markdown code fences, collapse whitespace, redact secrets, truncate."""
    if not raw:
        return ""
    text = str(raw)
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    text = _SECRET_PATTERNS.sub("[REDACTED]", text)
    text = " ".join(text.split())
    if len(text) > _MAX_FEEDBACK_CHARS:
        text = text[:_MAX_FEEDBACK_CHARS]
    return text


def _sanitize_feedback_rows(rows: Sequence) -> tuple[str, ...]:
    """Keep at most 5 ordered, normalized, redacted feedback comments."""
    cleaned: list[str] = []
    for row in rows:
        body = row.get("body") if isinstance(row, dict) else None
        normalized = _normalize_feedback(body or "")
        if normalized:
            cleaned.append(normalized)
        if len(cleaned) >= _MAX_FEEDBACK_COMMENTS:
            break
    return tuple(cleaned)


def _sanitize_replay_code(raw: object) -> str:
    text = _SECRET_PATTERNS.sub("[REDACTED]", str(raw or ""))
    return text[:_MAX_REPLAY_CODE_CHARS]


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def build_evidence(
    published_rows: Iterable[dict],
    mr_inventory: dict,
    feedback_by_key: dict,
    *,
    now: datetime,
    window_days: int,
    unhandled_after_days: int,
    prior_watermark: str | None,
    accepted_weight: float = 1.0,
    rejected_weight: float = 1.0,
    unhandled_weight: float = 0.25,
) -> SourceSnapshot:
    """Build a frozen source snapshot from published suggestion rows.

    - ``published_rows``: dicts from ``published_suggestions``.
    - ``mr_inventory``: ``{(project, mr_iid): {"state": ..., "updated_at": ...}}``.
    - ``feedback_by_key``: ``{(project, mr_iid, suggestion_id): [comment dicts]}``.

    Rows classified PENDING remain in the snapshot but never enter clustering
    (callers filter on ``outcome``). ``has_new_signal`` is true when any row
    matured or any timestamp moved past the prior watermark.
    """
    cutoff = now - timedelta(days=window_days)
    prior_dt = to_cn(prior_watermark) if prior_watermark else None
    evidence: list[Evidence] = []
    has_new_signal = False

    for row in published_rows:
        created_dt = to_cn(row.get("created_at"))
        if created_dt is None or created_dt < cutoff:
            continue
        project = str(row.get("project") or "")
        mr_iid = str(row.get("mr_iid") or "")
        mr = mr_inventory.get((project, mr_iid), {})
        outcome = classify_outcome(row, mr, now, unhandled_after_days)
        weight = outcome_weight(
            outcome,
            accepted_weight=accepted_weight,
            rejected_weight=rejected_weight,
            unhandled_weight=unhandled_weight,
        )
        feedback = _sanitize_feedback_rows(
            feedback_by_key.get((project, mr_iid, str(row.get("suggestion_id") or "")), ())
        )
        try:
            rule_ids = tuple(str(value) for value in json.loads(row.get("project_skill_rule_ids_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            rule_ids = ()
        try:
            reference_hashes = tuple(
                (str(path), str(content_hash))
                for path, content_hash in json.loads(
                    row.get("project_skill_reference_hashes_json") or "{}"
                ).items()
            )
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            reference_hashes = ()
        evidence.append(Evidence(
            suggestion_id=str(row.get("suggestion_id") or ""),
            project=project,
            mr_iid=mr_iid,
            mr_url=str(row.get("mr_url") or ""),
            created_at=row.get("created_at") or "",
            file_path=str(row.get("file_path") or ""),
            label=str(row.get("label") or ""),
            summary=str(row.get("one_sentence_summary") or ""),
            suggestion_content=str(row.get("suggestion_content") or ""),
            outcome=outcome,
            weight=weight,
            global_prompt_set_hash=str(row.get("global_prompt_set_hash") or ""),
            prompt_bundle_hash=str(row.get("prompt_bundle_hash") or ""),
            project_rules_hash=str(row.get("project_rules_hash") or ""),
            project_skill_hash=str(row.get("project_skill_hash") or ""),
            project_skill_manifest_hash=str(row.get("project_skill_manifest_hash") or ""),
            project_skill_target_sha=str(row.get("project_skill_target_sha") or ""),
            project_skill_status=str(row.get("project_skill_status") or ""),
            project_skill_rule_ids=rule_ids,
            project_skill_reference_hashes=reference_hashes,
            feedback=feedback,
            existing_code=_sanitize_replay_code(row.get("existing_code")),
            improved_code=_sanitize_replay_code(row.get("improved_code")),
            commit_sha=str(row.get("commit_sha") or "")[:128],
            line_start=_safe_int(row.get("line_start")),
            line_end=_safe_int(row.get("line_end")),
            case_kind=str(row.get("case_kind") or ""),
            expected_action=str(row.get("expected_action") or ""),
            review_id=str(row.get("review_id") or ""),
            replayable=bool(row.get("replayable")),
        ))

        # has_new_signal: applied/resolved/feedback/mr_updated past watermark
        for ts_key in ("applied_at", "resolved_at"):
            ts = to_cn(row.get(ts_key))
            if ts is not None and (prior_dt is None or ts > prior_dt):
                has_new_signal = True
        mr_updated = to_cn(mr.get("updated_at"))
        if mr_updated is not None and (prior_dt is None or mr_updated > prior_dt):
            has_new_signal = True
        # newly matured: created + unhandled_after_days lies after watermark and <= now
        if created_dt is not None:
            matures_at = created_dt + timedelta(days=unhandled_after_days)
            if matures_at <= now and (prior_dt is None or matures_at > prior_dt):
                has_new_signal = True

    return SourceSnapshot(
        evidence=tuple(evidence),
        watermark=now.isoformat(),
        has_new_signal=has_new_signal,
    )
