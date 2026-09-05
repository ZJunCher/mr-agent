"""Durable, resume-safe task lifecycle telemetry."""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Iterable


class LifecyclePhase(StrEnum):
    CREATED = "created"
    QUEUE = "queue"
    SAME_MR_WAIT = "same_mr_wait"
    CONTEXT = "context"
    HERMES = "hermes"
    GIT_PUBLISH = "git_publish"
    PIPELINE_WAIT = "pipeline_wait"
    POST_PIPELINE = "post_pipeline"
    TERMINAL = "terminal"
    NOTIFICATION = "notification"


class LifecycleKind(StrEnum):
    POINT = "point"
    START = "start"
    END = "end"


def _event_id(task_id: str, phase: LifecyclePhase, segment_id: str, kind: LifecycleKind) -> str:
    value = f"{task_id}\x1f{phase.value}\x1f{segment_id}\x1f{kind.value}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class LifecycleEvent:
    task_id: str
    phase: LifecyclePhase
    kind: LifecycleKind
    occurred_at: float
    segment_id: str
    event_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        task_id: str,
        phase: LifecyclePhase | str,
        kind: LifecycleKind | str,
        *,
        segment_id: str = "default",
        occurred_at: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "LifecycleEvent":
        normalized_phase = LifecyclePhase(phase)
        normalized_kind = LifecycleKind(kind)
        return cls(
            task_id=task_id,
            phase=normalized_phase,
            kind=normalized_kind,
            occurred_at=float(occurred_at if occurred_at is not None else time.time()),
            segment_id=str(segment_id or "default"),
            event_id=_event_id(task_id, normalized_phase, str(segment_id or "default"), normalized_kind),
            metadata=dict(metadata or {}),
        )

    def to_json(self) -> str:
        value = asdict(self)
        value["phase"] = self.phase.value
        value["kind"] = self.kind.value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> "LifecycleEvent":
        decoded = json.loads(value)
        return cls(
            task_id=str(decoded["task_id"]),
            phase=LifecyclePhase(decoded["phase"]),
            kind=LifecycleKind(decoded["kind"]),
            occurred_at=float(decoded["occurred_at"]),
            segment_id=str(decoded["segment_id"]),
            event_id=str(decoded["event_id"]),
            metadata=dict(decoded.get("metadata") or {}),
        )


@dataclass(frozen=True)
class LifecycleSummary:
    processing_total_ms: int | None
    delivery_total_ms: int | None
    queue_duration_ms: int
    same_mr_wait_ms: int
    context_duration_ms: int
    hermes_duration_ms: int
    git_publish_duration_ms: int
    pipeline_wait_duration_ms: int
    post_pipeline_duration_ms: int
    notification_duration_ms: int
    incomplete_segments: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pipeline_wait_segment(attempt_id: str, sha: str, pipeline_id: int | None) -> str:
    identity = attempt_id or sha or "unknown"
    return f"{identity}:{pipeline_id if pipeline_id is not None else 'discovery'}"


def _point_time(events: list[LifecycleEvent], phase: LifecyclePhase, kind: LifecycleKind) -> float | None:
    matches = [event.occurred_at for event in events if event.phase is phase and event.kind is kind]
    return max(matches) if matches else None


def summarize_lifecycle(events: Iterable[LifecycleEvent]) -> LifecycleSummary:
    ordered = sorted(events, key=lambda event: (event.occurred_at, event.event_id))
    starts: dict[tuple[LifecyclePhase, str], LifecycleEvent] = {}
    durations: dict[LifecyclePhase, float] = {phase: 0.0 for phase in LifecyclePhase}
    incomplete = []
    for event in ordered:
        key = (event.phase, event.segment_id)
        if event.kind is LifecycleKind.START:
            starts.setdefault(key, event)
        elif event.kind is LifecycleKind.END:
            start = starts.pop(key, None)
            if start is not None and event.occurred_at >= start.occurred_at:
                durations[event.phase] += event.occurred_at - start.occurred_at
    incomplete.extend(f"{phase.value}:{segment_id}" for phase, segment_id in starts)

    created = _point_time(ordered, LifecyclePhase.CREATED, LifecycleKind.POINT)
    terminal = _point_time(ordered, LifecyclePhase.TERMINAL, LifecycleKind.POINT)
    notification_end = _point_time(ordered, LifecyclePhase.NOTIFICATION, LifecycleKind.END)
    processing_total = None if created is None or terminal is None else max(0, int((terminal - created) * 1000))
    delivery_total = (
        None if created is None or notification_end is None else max(0, int((notification_end - created) * 1000))
    )

    def milliseconds(phase: LifecyclePhase) -> int:
        return max(0, int(durations[phase] * 1000))

    return LifecycleSummary(
        processing_total_ms=processing_total,
        delivery_total_ms=delivery_total,
        queue_duration_ms=milliseconds(LifecyclePhase.QUEUE),
        same_mr_wait_ms=milliseconds(LifecyclePhase.SAME_MR_WAIT),
        context_duration_ms=milliseconds(LifecyclePhase.CONTEXT),
        hermes_duration_ms=milliseconds(LifecyclePhase.HERMES),
        git_publish_duration_ms=milliseconds(LifecyclePhase.GIT_PUBLISH),
        pipeline_wait_duration_ms=milliseconds(LifecyclePhase.PIPELINE_WAIT),
        post_pipeline_duration_ms=milliseconds(LifecyclePhase.POST_PIPELINE),
        notification_duration_ms=milliseconds(LifecyclePhase.NOTIFICATION),
        incomplete_segments=tuple(sorted(incomplete)),
    )
