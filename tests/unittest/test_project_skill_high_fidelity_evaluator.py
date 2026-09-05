import asyncio
import tempfile
from dataclasses import replace

from pr_agent.eval.conditions import build_condition_manifest
from pr_agent.eval.production_replay import (
    NormalizedReviewItem,
    ProductionReplayResult,
)
from pr_agent.eval.store import find_replayable_runs, save_review_run
from pr_agent.suggestions.prompt_evolution.high_fidelity_evaluator import ProjectSkillHighFidelityEvaluator
from pr_agent.suggestions.prompt_evolution.models import Evidence, Outcome, SkillOptimizationBatch


def _case(case_id, mr_iid, outcome, content):
    return Evidence(
        suggestion_id=case_id,
        project="eabot/cook",
        mr_iid=mr_iid,
        mr_url=f"https://gitlab/eabot/cook/-/merge_requests/{mr_iid}",
        created_at="2026-08-27T00:00:00+08:00",
        file_path=f"src/{mr_iid}.py",
        label="正确性",
        summary=content,
        suggestion_content=content,
        outcome=outcome,
        weight=1.0,
        global_prompt_set_hash="global",
        prompt_bundle_hash="bundle",
        line_start=10,
        line_end=12,
    )


def _batch():
    cases = (
        _case("accepted", "1", Outcome.ACCEPTED, "keep"),
        _case("rejected", "2", Outcome.REJECTED, "bad"),
    )
    return SkillOptimizationBatch("eabot/cook", "manifest", (), cases, ("accepted",), "split")


def _record(mr_iid):
    return {
        "review_id": f"r-{mr_iid}",
        "created_at": "2026-08-27T12:00:00+08:00",
        "pr_url": f"https://gitlab/eabot/cook/-/merge_requests/{mr_iid}",
        "project": "eabot/cook",
        "mr_iid": mr_iid,
        "base_sha": "a" * 40,
        "head_sha": ("b" if mr_iid == "1" else "d") * 40,
        "input": {"title": "frozen"},
    }


def _item(mr_iid, content):
    return NormalizedReviewItem(
        f"src/{mr_iid}.py", 10, 12, "正确性", content, content, f"fp-{content}",
    )


def _condition(request, skill_hash, plan="plan"):
    return build_condition_manifest(
        project=request.project,
        mr_iid=request.mr_iid,
        command=request.command,
        base_sha=request.base_sha,
        head_sha=request.head_sha,
        target_sha=request.target_sha,
        model=request.model,
        temperature=0.2,
        max_model_tokens=32000,
        global_prompt_set_hash="global",
        prompt_bundle_hash="bundle",
        config={"same": True},
        diff_hash=f"diff-{request.mr_iid}",
        chunk_plan_hash=plan,
        context_hash="context",
        output_schema="PRCodeSuggestions:v1",
        parser_version="improve-yaml:v1",
        skill_hash=skill_hash,
        captured_at=request.captured_at,
    )


class Replay:
    def __init__(self, *, mismatch=False, partial=False):
        self.requests = []
        self.mismatch = mismatch
        self.partial = partial

    async def __call__(self, request):
        self.requests.append(request)
        candidate = "candidate" in request.skill_content
        if request.mr_iid == "1":
            items = (_item("1", "keep"),)
        elif candidate:
            items = ()
        else:
            items = (_item("2", "bad"),)
        plan = "candidate-plan" if candidate and self.mismatch else "plan"
        return ProductionReplayResult(
            "ok",
            request.command,
            output={},
            normalized_items=items,
            coverage_status="partial" if candidate and self.partial else "complete",
            condition=_condition(request, "candidate" if candidate else "baseline", plan),
        )


def test_high_fidelity_pair_uses_identical_records_and_accepts_real_improvement():
    replay = Replay()
    evaluator = ProjectSkillHighFidelityEvaluator(
        lambda project, mrs: [_record(mr) for mr in mrs], replay_runner=replay, min_mrs=2,
    )

    report = asyncio.run(evaluator.evaluate_pair(
        _batch(), "baseline skill", "candidate skill", target_sha="c" * 40, model="model",
    ))

    assert report.passed
    assert report.baseline_score == "0.5"
    assert report.candidate_score == "1"
    assert report.replayed_mrs == ("1", "2")
    assert len(replay.requests) == 4
    assert replay.requests[0].base_sha == replay.requests[1].base_sha
    assert replay.requests[0].head_sha == replay.requests[1].head_sha


def test_condition_mismatch_rejects_pair_before_scoring_passes():
    evaluator = ProjectSkillHighFidelityEvaluator(
        lambda project, mrs: [_record(mr) for mr in mrs], replay_runner=Replay(mismatch=True), min_mrs=2,
    )

    report = asyncio.run(evaluator.evaluate_pair(
        _batch(), "baseline skill", "candidate skill", target_sha="c" * 40, model="model",
    ))

    assert not report.passed
    assert any(error.startswith("condition_mismatch:2") for error in report.errors)


def test_partial_diff_coverage_rejects_candidate():
    evaluator = ProjectSkillHighFidelityEvaluator(
        lambda project, mrs: [_record(mr) for mr in mrs], replay_runner=Replay(partial=True), min_mrs=2,
    )

    report = asyncio.run(evaluator.evaluate_pair(
        _batch(), "baseline skill", "candidate skill", target_sha="c" * 40, model="model",
    ))

    assert not report.passed
    assert "incomplete_diff_coverage:1" in report.errors


def test_insufficient_replayable_mrs_fails_closed_without_model_runs():
    replay = Replay()
    evaluator = ProjectSkillHighFidelityEvaluator(lambda project, mrs: [_record("1")],
                                                  replay_runner=replay, min_mrs=2)

    report = asyncio.run(evaluator.evaluate_pair(
        _batch(), "baseline skill", "candidate skill", target_sha="c" * 40, model="model",
    ))

    assert not report.passed
    assert report.errors == ("insufficient_high_fidelity_evidence",)
    assert replay.requests == []


def test_max_mrs_is_a_deterministic_high_fidelity_cap_not_a_missing_case_error():
    batch = _batch()
    extra = _case("extra-rejected", "3", Outcome.REJECTED, "extra-bad")
    batch = replace(batch, selection_cases=batch.selection_cases + (extra,))
    replay = Replay()
    evaluator = ProjectSkillHighFidelityEvaluator(
        lambda project, mrs: [_record(mr) for mr in mrs], replay_runner=replay, min_mrs=2, max_mrs=2,
    )

    report = asyncio.run(evaluator.evaluate_pair(
        batch, "baseline skill", "candidate skill", target_sha="c" * 40, model="model",
    ))

    assert report.passed
    assert report.replayed_mrs == ("1", "2")
    assert "incomplete_case_results" not in report.errors


def test_find_replayable_runs_filters_incomplete_records_and_decodes_input():
    with tempfile.TemporaryDirectory() as directory:
        path = f"{directory}/eval.db"
        save_review_run({
            **_record("1"),
            "input": {"title": "frozen"},
            "model": "model",
        }, path=path)
        save_review_run({
            **_record("2"),
            "review_id": "missing-input",
            "input": None,
            "model": "model",
        }, path=path)

        records = find_replayable_runs("eabot/cook", ("1", "2"), path=path)

    assert [record["mr_iid"] for record in records] == ["1"]
    assert records[0]["input"] == {"title": "frozen"}
