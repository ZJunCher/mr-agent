import asyncio
import json
import subprocess

import pytest

import pr_agent.config_loader  # noqa: F401 - Initialize Dynaconf before importing ut_agent.
import ut_agent.agent as agent_module
import ut_agent.config as config_module
import ut_agent.execution_policy as execution_policy
import ut_agent.tools.commit_push as commit_push_module
from ut_agent.repair_coordinator import (
    PublicationPhase,
    TerminalPipelineProof,
    build_repair_snapshot,
    terminal_guard,
)
from ut_agent.tools.context import ToolContext


@pytest.fixture(autouse=True)
def hermes_backend(monkeypatch):
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "hermes")


def _exchange(name: str, call_id: str, result: dict, arguments: dict | None = None) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments or {})},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result),
        },
    ]


def _push(commit_sha: str, attempt_id: str) -> list[dict]:
    return _exchange(
        "commit_and_push_tool",
        f"push-{attempt_id}",
        {
            "status": "success",
            "changed": True,
            "commit_sha": commit_sha,
            "attempt_id": attempt_id,
        },
    )


def _pipeline(
    commit_sha: str,
    attempt_id: str,
    status: str,
    pipeline_id: int,
    *,
    requested_sha: str | None = None,
) -> list[dict]:
    return _exchange(
        "wait_pipeline_tool",
        f"wait-{attempt_id}-{pipeline_id}",
        {
            "status": "success",
            "requested_commit_sha": requested_sha or commit_sha,
            "matched_commit_sha": commit_sha,
            "attempt_id": attempt_id,
            "pipeline_id": pipeline_id,
            "validation_pipeline_id": pipeline_id,
            "pipeline_status": status,
            "failed_jobs": [] if status == "success" else [{"name": "build", "status": "failed"}],
        },
    )


def _generated_repair(status: str, *, validation_code: str = "") -> list[dict]:
    result = {
        "status": status,
        "operation": "repair_session",
        "job_name": "build_release_arm64",
        "root_cause_id": "root-cook-561",
        "changed_files": ["src/component.cpp"],
        "terminal_protocol_status": "valid_candidate" if status == "changed" else "malformed",
        "terminal_validation_error_code": validation_code,
    }
    if status == "changed":
        result["repair_report"] = {
            "schema_version": 1,
            "root_cause_summary": "接口不匹配",
            "solution_summary": "更新调用方",
            "rationale": "保持公开接口边界",
            "file_explanations": [{"path": "src/component.cpp", "summary": "更新调用"}],
            "diagnostic_dispositions": [],
        }
    return _exchange(
        "generate_code_tool",
        f"generate-{status}",
        result,
        {
            "operation": "repair_session",
            "job_name": "build_release_arm64",
            "root_cause_id": "root-cook-561",
        },
    )


def test_valid_alias_replay_result_requires_commit_instead_of_discard():
    state = {
        "trigger_type": "pipeline_failed",
        "messages": _generated_repair("changed"),
    }

    assert execution_policy.validate_tool_call(state, "commit_and_push_tool") == (True, "")
    allowed, reason = execution_policy.validate_tool_call(state, "discard_workspace_tool")
    assert allowed is False
    assert "有效修复报告" in reason


def test_unknown_alias_partial_changes_require_discard_and_reject_commit():
    state = {
        "trigger_type": "pipeline_failed",
        "messages": _generated_repair(
            "partial_changes",
            validation_code="diagnostic_identity_unknown",
        ),
    }

    allowed, reason = execution_policy.validate_tool_call(state, "commit_and_push_tool")
    assert allowed is False
    assert "diagnostic_identity_unknown" in reason
    assert execution_policy.validate_tool_call(state, "discard_workspace_tool") == (True, "")


def test_valid_repair_result_commits_exact_path_to_temporary_remote(monkeypatch, tmp_path):
    branch = "fixture/repair"
    remote = tmp_path / "remote.git"
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_561" / "repo"

    def git(*args: str, cwd=None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "--bare", str(remote))
    git("init", str(repo))
    git("checkout", "-b", branch, cwd=repo)
    git("config", "user.name", "Fixture Author", cwd=repo)
    git("config", "user.email", "fixture@example.test", cwd=repo)
    source = repo / "src" / "component.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int component = 0;\n", encoding="utf-8")
    git("add", "src/component.cpp", cwd=repo)
    git("commit", "-m", "fixture base", cwd=repo)
    git("remote", "add", "origin", str(remote), cwd=repo)
    git("push", "-u", "origin", branch, cwd=repo)
    base_sha = git("rev-parse", "HEAD", cwd=repo)
    source.write_text("int repaired_component = 1;\n", encoding="utf-8")

    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    payload = json.loads(commit_push_module.commit_and_push_tool.func(state={
        "mr_id": 561,
        "source_branch": branch,
        "trigger_type": "pipeline_failed",
        "messages": _generated_repair("changed"),
    }))

    assert payload["status"] == "success"
    assert payload["base_sha"] == base_sha
    assert payload["attempt_sequence"] == 1
    pushed_sha = git(f"--git-dir={remote}", "rev-parse", f"refs/heads/{branch}")
    assert pushed_sha == payload["commit_sha"]
    assert git(f"--git-dir={remote}", "rev-parse", f"{pushed_sha}^") == base_sha
    assert git(
        f"--git-dir={remote}",
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        pushed_sha,
    ).splitlines() == ["src/component.cpp"]
    commit_message = git(f"--git-dir={remote}", "show", "-s", "--format=%B", pushed_sha)
    assert "[pr-agent-task:local-mr-561:push-attempt:1:" in commit_message
    assert git("status", "--porcelain", cwd=repo) == ""


def test_latest_published_attempt_without_exact_pipeline_is_unverified():
    snapshot = build_repair_snapshot(_push("sha-3", "attempt-3"))

    assert snapshot.publication_phase is PublicationPhase.UNVERIFIED
    assert snapshot.latest_pushed_sha == "sha-3"
    assert snapshot.latest_attempt_id == "attempt-3"
    assert snapshot.terminal_proof is None
    assert snapshot.requires_exact_pipeline is True


def test_old_pipeline_cannot_verify_latest_attempt():
    messages = _push("sha-2", "attempt-2")
    messages += _pipeline("sha-2", "attempt-2", "failed", 34700)
    messages += _push("sha-3", "attempt-3")

    snapshot = build_repair_snapshot(messages)

    assert snapshot.publication_phase is PublicationPhase.UNVERIFIED
    assert snapshot.latest_exact_pipeline is None
    assert snapshot.terminal_proof is None


def test_wrong_attempt_or_requested_sha_cannot_verify_latest_attempt():
    messages = _push("sha-3", "attempt-3")
    messages += _pipeline("sha-3", "attempt-2", "failed", 34713)
    messages += _pipeline("sha-3", "attempt-3", "failed", 34714, requested_sha="sha-2")

    snapshot = build_repair_snapshot(messages)

    assert snapshot.publication_phase is PublicationPhase.UNVERIFIED
    assert snapshot.latest_exact_pipeline is None


def test_exact_attempt_pipeline_builds_terminal_proof():
    messages = _push("sha-3", "attempt-3")
    messages += _pipeline("sha-3", "attempt-3", "success", 34713)

    snapshot = build_repair_snapshot(messages)

    assert snapshot.publication_phase is PublicationPhase.TERMINAL
    assert snapshot.terminal_proof == TerminalPipelineProof(
        attempt_id="attempt-3",
        commit_sha="sha-3",
        pipeline_id=34713,
        status="success",
    )
    assert terminal_guard(snapshot) == (True, "")


def test_nonterminal_exact_pipeline_still_requires_wait():
    messages = _push("sha-3", "attempt-3")
    messages += _pipeline("sha-3", "attempt-3", "running", 34713)

    snapshot = build_repair_snapshot(messages)
    allowed, reason = terminal_guard(snapshot)

    assert snapshot.publication_phase is PublicationPhase.NONTERMINAL
    assert snapshot.requires_exact_pipeline is True
    assert allowed is False
    assert "sha-3" in reason


def test_legacy_exact_sha_pipeline_without_attempt_id_is_accepted():
    messages = _push("sha-3", "attempt-3")
    pipeline_messages = _pipeline("sha-3", "attempt-3", "failed", 34713)
    payload = json.loads(pipeline_messages[-1]["content"])
    payload.pop("attempt_id")
    pipeline_messages[-1]["content"] = json.dumps(payload)
    messages += pipeline_messages

    snapshot = build_repair_snapshot(messages)

    assert snapshot.publication_phase is PublicationPhase.TERMINAL
    assert snapshot.terminal_proof == TerminalPipelineProof(
        attempt_id="attempt-3",
        commit_sha="sha-3",
        pipeline_id=34713,
        status="failed",
    )


def test_iteration_limit_cannot_end_immediately_after_push():
    state = {
        "trigger_type": "pipeline_failed",
        "messages": _push("sha-3", "attempt-3"),
        "iteration": 30,
        "max_iterations": 30,
    }

    result = asyncio.run(agent_module.agent_node(state))
    tool_call = result["messages"][0].tool_calls[0]

    assert tool_call["name"] == "wait_pipeline_tool"
    assert tool_call["args"] == {"commit_sha": "sha-3"}


def test_route_after_push_crosses_iteration_limit_to_schedule_exact_wait():
    state = {
        "trigger_type": "pipeline_failed",
        "messages": _push("sha-3", "attempt-3"),
        "iteration": 30,
        "max_iterations": 30,
    }

    assert agent_module.route_after_tools(state) == "agent"


def test_result_extraction_does_not_reuse_old_pipeline_for_new_push():
    messages = _push("sha-2", "attempt-2")
    messages += _pipeline("sha-2", "attempt-2", "failed", 34700)
    messages += _push("sha-3", "attempt-3")

    result = agent_module._extract_result({"iteration": 30, "max_iterations": 30}, messages)

    assert result["pushed_sha"] == "sha-3"
    assert result["final_pipeline_status"] == "unknown"
    assert result["terminal_proof"] is None
    assert result["pending_attempt"] == {"attempt_id": "attempt-3", "commit_sha": "sha-3"}


def test_duplicate_push_replay_does_not_invalidate_existing_exact_pipeline():
    push = _push("sha-3", "attempt-3")
    messages = push + _pipeline("sha-3", "attempt-3", "success", 34713) + push

    snapshot = build_repair_snapshot(messages)

    assert snapshot.published_attempt_count == 1
    assert snapshot.publication_phase is PublicationPhase.TERMINAL
    assert snapshot.terminal_proof is not None
    assert snapshot.terminal_proof.pipeline_id == 34713


def test_reused_attempt_id_cannot_hide_a_new_commit():
    messages = _push("sha-2", "attempt-reused")
    messages += _pipeline("sha-2", "attempt-reused", "failed", 34712)
    messages += _push("sha-3", "attempt-reused")

    snapshot = build_repair_snapshot(messages)

    assert snapshot.published_attempt_count == 2
    assert snapshot.latest_pushed_sha == "sha-3"
    assert snapshot.publication_phase is PublicationPhase.UNVERIFIED


@pytest.mark.parametrize("status", ["canceled", "skipped"])
def test_canceled_and_skipped_are_terminal_but_never_success(status):
    messages = _push("sha-3", "attempt-3")
    messages += _pipeline("sha-3", "attempt-3", status, 34713)

    snapshot = build_repair_snapshot(messages)

    assert snapshot.publication_phase is PublicationPhase.TERMINAL
    assert snapshot.terminal_proof is not None
    assert snapshot.terminal_proof.status == status


def test_malformed_pipeline_result_cannot_verify_published_attempt():
    messages = _push("sha-3", "attempt-3")
    messages += [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "wait-malformed",
                "type": "function",
                "function": {"name": "wait_pipeline_tool", "arguments": '{"commit_sha":"sha-3"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "wait-malformed", "content": "{not-json"},
    ]

    snapshot = build_repair_snapshot(messages)

    assert snapshot.publication_phase is PublicationPhase.UNVERIFIED
    assert snapshot.terminal_proof is None


def test_mr_549_incident_replay_requires_third_exact_pipeline():
    messages = _push("bdd5e6cd7d7864687f9fad98028c7cf3939b3d99", "attempt-1")
    messages += _pipeline("bdd5e6cd7d7864687f9fad98028c7cf3939b3d99", "attempt-1", "failed", 34695)
    messages += _push("a315fd825e71db75f875f0c0e442bf7773d8d28b", "attempt-2")
    messages += _pipeline("a315fd825e71db75f875f0c0e442bf7773d8d28b", "attempt-2", "failed", 34700)
    messages += _push("fbd6ba20c3138bc316147bd279cb18cebdba1ac3", "attempt-3")

    waiting = build_repair_snapshot(messages)
    assert waiting.publication_phase is PublicationPhase.UNVERIFIED
    assert waiting.latest_pushed_sha == "fbd6ba20c3138bc316147bd279cb18cebdba1ac3"

    messages += _pipeline("a315fd825e71db75f875f0c0e442bf7773d8d28b", "attempt-2", "failed", 34700)
    late_old_event = build_repair_snapshot(messages)
    assert late_old_event.publication_phase is PublicationPhase.UNVERIFIED
    assert late_old_event.terminal_proof is None

    messages += _pipeline("fbd6ba20c3138bc316147bd279cb18cebdba1ac3", "attempt-3", "failed", 34713)
    verified = build_repair_snapshot(messages)
    assert verified.terminal_proof == TerminalPipelineProof(
        attempt_id="attempt-3",
        commit_sha="fbd6ba20c3138bc316147bd279cb18cebdba1ac3",
        pipeline_id=34713,
        status="failed",
    )


def test_late_nonterminal_observation_cannot_reverse_terminal_pipeline():
    messages = _push("sha-3", "attempt-3")
    messages += _pipeline("sha-3", "attempt-3", "success", 34713)
    messages += _pipeline("sha-3", "attempt-3", "running", 34713)

    snapshot = build_repair_snapshot(messages)

    assert snapshot.publication_phase is PublicationPhase.TERMINAL
    assert snapshot.terminal_proof is not None
    assert snapshot.terminal_proof.status == "success"


def test_tool_error_with_exact_sha_cannot_create_terminal_proof():
    messages = _push("sha-3", "attempt-3")
    pipeline_messages = _pipeline("sha-3", "attempt-3", "success", 34713)
    payload = json.loads(pipeline_messages[-1]["content"])
    payload["status"] = "error"
    pipeline_messages[-1]["content"] = json.dumps(payload)
    messages += pipeline_messages

    snapshot = build_repair_snapshot(messages)

    assert snapshot.publication_phase is PublicationPhase.UNVERIFIED
    assert snapshot.terminal_proof is None
