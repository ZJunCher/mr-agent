from dataclasses import replace

import pytest

from pr_agent.eval.conditions import (
    EvaluationTreatment,
    build_condition_manifest,
    compare_paired_conditions,
    select_canary,
)


def _manifest(skill_hash="baseline", **overrides):
    values = {
        "project": "eabot/cook",
        "mr_iid": "12",
        "command": "improve",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "target_sha": "c" * 40,
        "model": "model",
        "temperature": 0.2,
        "max_model_tokens": 32000,
        "global_prompt_set_hash": "global",
        "prompt_bundle_hash": "bundle",
        "config": {"b": 2, "a": 1},
        "diff_hash": "diff",
        "chunk_plan_hash": "plan",
        "context_hash": "context",
        "output_schema": "PRCodeSuggestions:v1",
        "parser_version": "improve:v1",
        "skill_hash": skill_hash,
        "captured_at": "2026-08-27T12:00:00+08:00",
    }
    values.update(overrides)
    return build_condition_manifest(**values)


def test_manifest_hash_is_canonical_under_config_ordering():
    first = _manifest(config={"a": 1, "nested": {"x": 1, "y": 2}})
    second = _manifest(config={"nested": {"y": 2, "x": 1}, "a": 1})

    assert first.manifest_hash == second.manifest_hash
    assert first.comparable_hash == second.comparable_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "d" * 40),
        ("diff_hash", "other-diff"),
        ("chunk_plan_hash", "other-plan"),
        ("prompt_bundle_hash", "other-prompt"),
        ("model", "other-model"),
        ("config_hash", "other-config"),
        ("context_hash", "other-context"),
        ("output_schema", "OtherSchema:v1"),
    ],
)
def test_pair_comparison_rejects_every_result_affecting_difference(field, value):
    baseline = _manifest("baseline")
    candidate = replace(_manifest("candidate"), **{field: value})

    result = compare_paired_conditions(baseline, candidate)

    assert not result.matched
    assert field in result.mismatched_fields


def test_pair_comparison_allows_only_skill_hash_to_change():
    result = compare_paired_conditions(_manifest("baseline"), _manifest("candidate"))

    assert result.matched
    assert result.mismatched_fields == ()


def test_pair_comparison_requires_an_actual_skill_variant():
    result = compare_paired_conditions(_manifest("same"), _manifest("same"))

    assert not result.matched
    assert result.error == "skill_hash_unchanged"


def test_prompt_pair_allows_only_prompt_hashes_to_change():
    baseline = _manifest("same")
    candidate = replace(
        baseline,
        global_prompt_set_hash="global-candidate",
        prompt_bundle_hash="bundle-candidate",
    )

    result = compare_paired_conditions(
        baseline,
        candidate,
        treatment=EvaluationTreatment.GLOBAL_PROMPT,
    )

    assert result.matched is True


def test_prompt_pair_rejects_skill_or_non_prompt_drift():
    baseline = _manifest("same")
    candidate = replace(
        baseline,
        global_prompt_set_hash="global-candidate",
        prompt_bundle_hash="bundle-candidate",
        skill_hash="different-skill",
        diff_hash="different-diff",
    )

    result = compare_paired_conditions(
        baseline,
        candidate,
        treatment=EvaluationTreatment.GLOBAL_PROMPT,
    )

    assert result.matched is False
    assert result.mismatched_fields == ("diff_hash", "skill_hash")


def test_prompt_pair_rejects_unchanged_prompt():
    baseline = _manifest("same")

    result = compare_paired_conditions(
        baseline,
        baseline,
        treatment=EvaluationTreatment.GLOBAL_PROMPT,
    )

    assert result.error == "prompt_hash_unchanged"


def test_canary_assignment_is_deterministic_and_has_exact_boundaries():
    ref = "d" * 40

    first = select_canary("eabot/cook", "12", "b" * 40, approved_ref=ref, percent=37)
    second = select_canary("eabot/cook", "12", "b" * 40, approved_ref=ref, percent=37)

    assert first == second
    assert not select_canary("eabot/cook", "12", "b" * 40, approved_ref=ref, percent=0).selected
    assert select_canary("eabot/cook", "12", "b" * 40, approved_ref=ref, percent=100).selected


@pytest.mark.parametrize("ref", ["", "main", "A" * 40, "a" * 39, "z" * 40])
def test_canary_rejects_mutable_or_invalid_refs(ref):
    with pytest.raises(ValueError, match="immutable"):
        select_canary("eabot/cook", "12", "b" * 40, approved_ref=ref, percent=10)


@pytest.mark.parametrize("percent", [-1, 101])
def test_canary_rejects_invalid_percent(percent):
    with pytest.raises(ValueError, match="percent"):
        select_canary("eabot/cook", "12", "b" * 40, approved_ref="d" * 40, percent=percent)
