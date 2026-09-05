"""validate_suggestions_scenario_constraints persists filtered suggestions."""
import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_provider(project="group/cook", mr_iid="10"):
    """Build a mock git_provider with the attributes the collection code reads."""
    provider = MagicMock()
    provider.id_project = project
    provider.id_mr = mr_iid
    provider.get_pr_url.return_value = f"https://gitlab/{project}/-/merge_requests/{mr_iid}"
    mr = MagicMock()
    mr.author = {"username": "alice", "name": "Alice"}
    provider.mr = mr
    refs = {"head_sha": "abc123def"}
    provider.get_diff_refs.return_value = refs
    return provider


def _make_suggestion(idx, label="possible bug", score=9):
    return {
        "relevant_file": f"src/file{idx}.go",
        "relevant_lines_start": 10 + idx,
        "relevant_lines_end": 12 + idx,
        "label": label,
        "score": score,
        "one_sentence_summary": f"fix issue {idx}",
        "suggestion_content": "Trigger: when x > 5\nwhy...\nfix...",
        "existing_code": "old",
        "improved_code": "new",
    }


def _patch_settings(gs, enabled=True, persist=True):
    """Configure get_settings mock for the collection code."""
    gs.return_value.get.side_effect = lambda key, default=None: {
        "pr_code_suggestions.scenario_validation_enabled": enabled,
        "pr_code_suggestions.scenario_validation_max_candidates": 30,
        "pr_code_suggestions.scenario_validation_fail_action": "skip",
        "pr_code_suggestions.scenario_validation_model": "anthropic/claude-opus-4-8",
        "pr_code_suggestions.filter_persistence_enabled": persist,
    }.get(key, default)
    gs.return_value.config.get.side_effect = lambda key, default=None: default
    gs.return_value.config.model = "anthropic/claude-sonnet-4-5"


def test_persists_filtered_suggestions(tmp_path: Path):
    from pr_agent.suggestions.store import init_filtered_table
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    db = str(tmp_path / "f.db")
    init_filtered_table(db)

    provider = _make_provider()
    # Force _local_trigger_reason to block all (missing prefix)
    suggestions = [_make_suggestion(i, label="") for i in range(3)]

    instance = MagicMock(spec=PRCodeSuggestions)
    instance.git_provider = provider
    instance._local_trigger_reason = MagicMock(return_value="scenario_missing_prefix")
    instance.pr_url = "https://gitlab/group/cook/-/merge_requests/10"
    instance._filter_mr_context = MagicMock(return_value={
        "project": "group/cook", "mr_iid": "10",
        "mr_url": "https://gitlab/group/cook/-/merge_requests/10",
        "mr_author": "alice", "commit_sha": "abc123def",
    })

    with patch("pr_agent.tools.pr_code_suggestions.get_settings") as gs, \
         patch("pr_agent.tools.pr_code_suggestions.save_filtered_suggestion") as save_mock:
        _patch_settings(gs)
        save_mock.side_effect = lambda record, path=None: None  # no-op, just track calls

        data = {"code_suggestions": suggestions}
        result = asyncio.run(
            PRCodeSuggestions.validate_suggestions_scenario_constraints(instance, data)
        )

    # All 3 blocked, none kept
    assert result["code_suggestions"] == []
    # save_filtered_suggestion called 3 times
    assert save_mock.call_count == 3
    # Check first call's record
    first_record = save_mock.call_args_list[0][0][0]
    assert first_record["skip_reason"] == "scenario_missing_prefix"
    assert first_record["judge_model"] == "anthropic/claude-opus-4-8"
    assert first_record["filter_stage"] == "scenario_validation"
    assert first_record["project"] == "group/cook"
    assert first_record["mr_iid"] == "10"
    assert first_record["mr_author"] == "alice"
    assert first_record["commit_sha"] == "abc123def"


def test_disabled_switch_skips_persistence(tmp_path: Path):
    from pr_agent.suggestions.store import init_filtered_table
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    db = str(tmp_path / "f.db")
    init_filtered_table(db)

    provider = _make_provider()
    suggestions = [_make_suggestion(0, label="")]

    instance = MagicMock(spec=PRCodeSuggestions)
    instance.git_provider = provider
    instance._local_trigger_reason = MagicMock(return_value="scenario_missing_prefix")

    with patch("pr_agent.tools.pr_code_suggestions.get_settings") as gs, \
         patch("pr_agent.tools.pr_code_suggestions.save_filtered_suggestion") as save_mock:
        _patch_settings(gs, persist=False)

        data = {"code_suggestions": suggestions}
        asyncio.run(
            PRCodeSuggestions.validate_suggestions_scenario_constraints(instance, data)
        )

    # Persistence disabled: save_filtered_suggestion not called
    assert save_mock.call_count == 0


def test_persistence_failure_does_not_break_flow(tmp_path: Path):
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

    provider = _make_provider()
    suggestions = [_make_suggestion(0, label="")]

    instance = MagicMock(spec=PRCodeSuggestions)
    instance.git_provider = provider
    instance._local_trigger_reason = MagicMock(return_value="scenario_missing_prefix")

    with patch("pr_agent.tools.pr_code_suggestions.get_settings") as gs, \
         patch("pr_agent.tools.pr_code_suggestions.save_filtered_suggestion", side_effect=Exception("disk full")):
        _patch_settings(gs)

        data = {"code_suggestions": suggestions}
        # Must not raise
        result = asyncio.run(
            PRCodeSuggestions.validate_suggestions_scenario_constraints(instance, data)
        )
    assert result["code_suggestions"] == []
