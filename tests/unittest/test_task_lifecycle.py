from pr_agent.distributed.lifecycle import LifecycleEvent, LifecyclePhase, summarize_lifecycle


def event(phase, kind, occurred_at, segment_id="default"):
    return LifecycleEvent.new("task-1", phase, kind, occurred_at=occurred_at, segment_id=segment_id)


def test_processing_total_spans_initial_run_pipeline_wait_and_resume():
    summary = summarize_lifecycle([
        event("created", "point", 0.0),
        event("hermes", "start", 10.0, "h1"),
        event("hermes", "end", 20.0, "h1"),
        event("pipeline_wait", "start", 21.0, "p1"),
        event("pipeline_wait", "end", 81.0, "p1"),
        event("terminal", "point", 90.0),
        event("notification", "start", 90.5, "n1"),
        event("notification", "end", 91.0, "n1"),
    ])

    assert summary.processing_total_ms == 90_000
    assert summary.pipeline_wait_duration_ms == 60_000
    assert summary.delivery_total_ms == 91_000
    assert summary.hermes_duration_ms == 10_000


def test_duplicate_events_and_unmatched_end_do_not_inflate_duration():
    start = event(LifecyclePhase.HERMES, "start", 10.0, "h1")
    end = event(LifecyclePhase.HERMES, "end", 20.0, "h1")

    summary = summarize_lifecycle([
        start,
        start,
        event(LifecyclePhase.HERMES, "end", 5.0, "missing"),
        end,
        end,
    ])

    assert summary.hermes_duration_ms == 10_000


def test_unmatched_start_remains_visible():
    summary = summarize_lifecycle([event("pipeline_wait", "start", 21.0, "p1")])

    assert summary.pipeline_wait_duration_ms == 0
    assert summary.incomplete_segments == ("pipeline_wait:p1",)


def test_event_identity_is_stable_across_worker_recovery():
    first = event("git_publish", "start", 10.0, "attempt-1")
    replay = event("git_publish", "start", 12.0, "attempt-1")

    assert first.event_id == replay.event_id
