import asyncio
import hashlib
from dataclasses import replace

from pr_agent.eval.conditions import build_condition_manifest
from pr_agent.eval.production_replay import NormalizedReviewItem, ProductionReplayResult
from pr_agent.suggestions.prompt_evolution.gitlab_publisher import PromptWorkspace
from pr_agent.suggestions.prompt_evolution.models import (
    Evidence,
    Outcome,
    PromptChangeKind,
    PromptEvaluationBatch,
    PromptFileChange,
    PromptProposal,
)
from pr_agent.suggestions.prompt_evolution.prompt_high_fidelity_evaluator import (
    GlobalPromptHighFidelityEvaluator,
)

PROMPT_PATH = "pr_agent/settings/code_suggestions/pr_code_suggestions_prompts.toml"


class Settings:
    def __init__(self):
        self.values = {"pr_code_suggestions_prompt": {"system": "baseline", "user": "user"}}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def unset(self, key):
        self.values.pop(key, None)


def _case(identity, mr_iid, outcome, content):
    return Evidence(
        suggestion_id=identity,
        project="eabot/cook",
        mr_iid=mr_iid,
        mr_url=f"https://gitlab/eabot/cook/-/merge_requests/{mr_iid}",
        created_at="2026-08-27T00:00:00+08:00",
        file_path=f"src/{mr_iid}.py",
        label="bug",
        summary=content,
        suggestion_content=content,
        outcome=outcome,
        weight=1.0,
        global_prompt_set_hash="global-base",
        prompt_bundle_hash="bundle-base",
        commit_sha=hashlib.sha256(f"head:{mr_iid}".encode()).hexdigest(),
        review_id=f"review-{mr_iid}",
        line_start=10,
        line_end=12,
        replayable=True,
    )


def _batch():
    cases = (
        _case("accepted", "1", Outcome.ACCEPTED, "keep"),
        _case("rejected", "2", Outcome.REJECTED, "bad"),
    )
    return PromptEvaluationBatch("global-base", (), cases, ("accepted",), "split")


def _record(mr_iid):
    return {
        "created_at": "2026-08-27T12:00:00+08:00",
        "pr_url": f"https://gitlab/eabot/cook/-/merge_requests/{mr_iid}",
        "project": "eabot/cook",
        "mr_iid": mr_iid,
        "review_id": f"review-{mr_iid}",
        "base_sha": "a" * 40,
        "head_sha": hashlib.sha256(f"head:{mr_iid}".encode()).hexdigest(),
        "input": {"title": "frozen"},
        "extra": {},
    }


def _proposal():
    content = '[pr_code_suggestions_prompt]\nsystem = "candidate"\nuser = "user"\n'
    return PromptProposal(
        "tighten",
        PromptChangeKind.CONSERVATIVE_TIGHTENING,
        ("rejected",),
        (PromptFileChange(PROMPT_PATH, "generation", "base", content, ("rejected",)),),
    )


def _workspace():
    content = '[pr_code_suggestions_prompt]\nsystem = "baseline"\nuser = "user"\n'
    return PromptWorkspace("example-group/mr-agent", "main", "a" * 40, {PROMPT_PATH: content})


def _item(mr_iid, content):
    return NormalizedReviewItem(f"src/{mr_iid}.py", 10, 12, "bug", content, content, f"fp-{content}")


class Replay:
    def __init__(self, mismatch=False):
        self.calls = []
        self.mismatch = mismatch

    async def __call__(self, request, *, settings):
        candidate = settings.get("pr_code_suggestions_prompt", {}).get("system") == "candidate"
        self.calls.append((request, candidate))
        if request.mr_iid == "1":
            items = (_item("1", "keep"),)
        elif candidate:
            items = ()
        else:
            items = (_item("2", "bad"),)
        condition = build_condition_manifest(
            project=request.project,
            mr_iid=request.mr_iid,
            command=request.command,
            base_sha=request.base_sha,
            head_sha=request.head_sha,
            target_sha=request.target_sha,
            model=request.model,
            temperature=0.2,
            max_model_tokens=32000,
            global_prompt_set_hash="global-candidate" if candidate else "global-base",
            prompt_bundle_hash="bundle-candidate" if candidate else "bundle-base",
            config={"same": True},
            diff_hash="different" if candidate and self.mismatch else f"diff-{request.mr_iid}",
            chunk_plan_hash="plan",
            context_hash="context",
            output_schema="PRCodeSuggestions:v1",
            parser_version="improve-yaml:v1",
            skill_hash="same-skill",
            captured_at=request.captured_at,
        )
        return ProductionReplayResult(
            "ok",
            request.command,
            output={},
            normalized_items=items,
            coverage_status="complete",
            condition=condition,
        )


class ExecutionReplay(Replay):
    async def __call__(self, request, *, settings):
        candidate = settings.get("pr_code_suggestions_prompt", {}).get("system") == "candidate"
        if request.mr_iid == "2" and not candidate:
            self.calls.append((request, candidate))
            return ProductionReplayResult(
                "error",
                request.command,
                coverage_status="failed",
                error_code="replay_execution_failed",
                error="output schema invalid",
            )
        return await super().__call__(request, settings=settings)


def test_global_prompt_pair_accepts_real_improvement_without_settings_leak():
    source_settings = Settings()
    replay = Replay()
    evaluator = GlobalPromptHighFidelityEvaluator(
        lambda _project, mrs: [_record(mr) for mr in mrs],
        replay_runner=replay,
        settings_factory=lambda: source_settings,
        min_mrs=2,
    )

    report = asyncio.run(evaluator.evaluate_pair(
        _batch(), _workspace(), _proposal(), model="model",
    ))

    assert report.passed
    assert report.baseline_score == "0.5"
    assert report.candidate_score == "1"
    assert source_settings.get("pr_code_suggestions_prompt")["system"] == "baseline"
    assert [candidate for _request, candidate in replay.calls] == [False, True, False, True]


def test_global_prompt_pair_rejects_non_prompt_condition_drift():
    evaluator = GlobalPromptHighFidelityEvaluator(
        lambda _project, mrs: [_record(mr) for mr in mrs],
        replay_runner=Replay(mismatch=True),
        settings_factory=Settings,
        min_mrs=2,
    )

    report = asyncio.run(evaluator.evaluate_pair(
        _batch(), _workspace(), _proposal(), model="model",
    ))

    assert not report.passed
    assert any(error.startswith("condition_mismatch:") for error in report.errors)


def test_global_prompt_pair_rejects_unchanged_candidate_content():
    proposal = _proposal()
    unchanged = replace(
        proposal,
        changes=(replace(proposal.changes[0], content=_workspace().files[PROMPT_PATH]),),
    )
    evaluator = GlobalPromptHighFidelityEvaluator(
        lambda _project, mrs: [_record(mr) for mr in mrs],
        replay_runner=Replay(),
        settings_factory=Settings,
        min_mrs=2,
    )

    report = asyncio.run(evaluator.evaluate_pair(
        _batch(), _workspace(), unchanged, model="model",
    ))

    assert not report.passed
    assert any("prompt_hash_unchanged" in error for error in report.errors)


def test_global_prompt_pair_rejects_record_with_wrong_frozen_head():
    records = [_record("1"), _record("2")]
    records[0]["head_sha"] = "f" * 64
    evaluator = GlobalPromptHighFidelityEvaluator(
        lambda _project, _mrs: [],
        review_record_loader=lambda _review_ids: records,
        replay_runner=Replay(),
        settings_factory=Settings,
        min_mrs=2,
    )

    report = asyncio.run(evaluator.evaluate_pair(
        _batch(), _workspace(), _proposal(), model="model",
    ))

    assert not report.passed
    assert report.errors == ("insufficient_high_fidelity_evidence",)


def test_global_prompt_pair_accepts_candidate_that_repairs_schema_failure():
    schema_case = replace(
        _case("schema", "2", Outcome.REJECTED, "invalid schema"),
        case_kind="output_schema_error",
        expected_action="suppress",
    )
    batch = PromptEvaluationBatch(
        "global-base",
        (),
        (_case("accepted", "1", Outcome.ACCEPTED, "keep"), schema_case),
        ("accepted",),
        "split",
    )
    evaluator = GlobalPromptHighFidelityEvaluator(
        lambda _project, mrs: [_record(mr) for mr in mrs],
        replay_runner=ExecutionReplay(),
        settings_factory=Settings,
        min_mrs=2,
    )

    report = asyncio.run(evaluator.evaluate_pair(
        batch, _workspace(), _proposal(), model="model",
    ))

    assert report.passed
    assert report.baseline_score == "0.5"
    assert report.candidate_score == "1"
