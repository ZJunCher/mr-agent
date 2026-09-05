import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pr_agent.distributed.broker import EffectRecord, MrLease
from pr_agent.distributed.models import MrKey
from pr_agent.distributed.runtime import ExecutionRuntime, execution_context
from pr_agent.triage.repair_rollback import RepairCommitManifest
from ut_agent.push_attempt import diff_digest
from ut_agent.tools import commit_push as commit_push_module
from ut_agent.tools.context import ToolContext


class MemoryEffectBroker:
    def __init__(self):
        self.effects = {}
        self.manifest_entries = []
        self.manifest_base_tree_sha = ""

    def claim_effect(self, key, _lease, metadata=None):
        return self.effects.setdefault(key, EffectRecord("started", metadata or {}))

    def update_effect_metadata(self, key, _lease, metadata):
        self.effects[key] = EffectRecord("started", metadata)
        return True

    def complete_effect(self, key, _lease, result):
        self.effects[key] = EffectRecord("completed", self.effects[key].metadata, result)
        return True

    def assert_fence(self, _lease):
        return None

    def get_task_triage_card(self, _task_id):
        return SimpleNamespace(receive_id="ou_owner")

    def get_repair_commit_manifest(self, task_id):
        if not self.manifest_entries:
            return None
        return RepairCommitManifest(
            repair_task_id=task_id,
            project_id="eabot/cook",
            mr_iid=536,
            source_branch="feature/fix",
            base_commit_sha=self.manifest_entries[0].parent_sha,
            base_tree_sha=self.manifest_base_tree_sha,
            authorized_actor_id="ou_owner",
            entries=tuple(self.manifest_entries),
        )

    def append_repair_commit(self, _task_id, entry, **kwargs):
        if not self.manifest_base_tree_sha:
            self.manifest_base_tree_sha = kwargs["base_tree_sha"]
        assert kwargs["base_tree_sha"] == self.manifest_base_tree_sha
        if entry not in self.manifest_entries:
            self.manifest_entries.append(entry)
        return self.get_repair_commit_manifest(_task_id)


class FakeGit:
    def __init__(self, diff="first-fix"):
        self.head = "a" * 40
        self.remote = "a" * 40
        self.staged_diff = diff
        self.last_diff = ""
        self.last_base = ""
        self.last_message = ""
        self.commit_count = 0
        self.calls = []
        self.push_conflict_sha = ""
        self.push_error_without_advance = False
        self.parents = {}
        self.trees = {self.head: "d" * 40}

    def stage(self, diff):
        self.staged_diff = diff

    def exit_code(self, _repo_dir, args):
        assert args == ["diff", "--cached", "--quiet", "--no-ext-diff"]
        return (1, "") if self.staged_diff else (0, "")

    def run(self, _repo_dir, args):
        self.calls.append(args)
        if args == ["add", "-A"]:
            return ""
        if args == ["diff", "--cached", "--binary", "--full-index", "--no-ext-diff"]:
            return self.staged_diff
        if args == ["diff", "HEAD^", "HEAD", "--binary", "--full-index", "--no-ext-diff"]:
            return self.last_diff
        if args == ["rev-parse", "HEAD"]:
            return self.head
        if args == ["rev-parse", "HEAD^"]:
            return self.last_base
        if args[0:1] == ["rev-parse"] and args[1].endswith("^{tree}"):
            return self.trees[args[1][:-7]]
        if args[0:1] == ["rev-parse"] and args[1].endswith("^"):
            return self.parents[args[1][:-1]]
        if args == ["log", "-1", "--pretty=%B"]:
            return self.last_message
        if args == ["ls-remote", "origin", "refs/heads/feature/fix"]:
            return f"{self.remote}\trefs/heads/feature/fix"
        if args[:2] == ["config", "user.name"] or args[:2] == ["config", "user.email"]:
            return ""
        if args[:2] == ["commit", "-m"]:
            self.commit_count += 1
            self.last_base = self.head
            self.last_diff = self.staged_diff
            self.last_message = args[2]
            self.head = chr(ord("a") + self.commit_count) * 40
            self.parents[self.head] = self.last_base
            self.trees[self.head] = chr(ord("d") + self.commit_count) * 40
            self.staged_diff = ""
            return ""
        if args == ["push", "origin", "feature/fix"]:
            if self.push_conflict_sha:
                self.remote = self.push_conflict_sha
                return "ERROR: git push 失败: non-fast-forward"
            if self.push_error_without_advance:
                return "ERROR: git push 失败: connection reset"
            self.remote = self.head
            return ""
        if args == ["status", "--porcelain"]:
            return ""
        raise AssertionError(f"unexpected git call: {args}")


def _runtime(broker):
    return ExecutionRuntime(
        "task-536",
        "worker-1",
        MrLease(MrKey("eabot/cook", 536), "worker-1", 7),
        "queue",
        AsyncMock(),
        broker,
    )


def _state(previous_results=()):
    messages = []
    for index, result in enumerate(previous_results, start=1):
        call_id = f"push-{index}"
        messages.extend([
            {"tool_calls": [{"id": call_id, "name": "commit_and_push_tool", "args": {}}]},
            {"tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)},
        ])
    return {"mr_id": 536, "source_branch": "feature/fix", "messages": messages}


def _native_state(validated_diff: str) -> dict:
    digest = diff_digest(validated_diff)
    base_sha = "a" * 40
    messages = [
        {
            "tool_calls": [{
                "id": "patch",
                "name": "apply_repo_patch_tool",
                "args": {"patch": "diff", "reason": "fix"},
            }],
        },
        {
            "tool_call_id": "patch",
            "content": json.dumps({
                "status": "changed",
                "patch_applied": True,
                "base_sha": base_sha,
                "diff_digest": digest,
                "changed_files": ["src/example.py"],
            }),
        },
        {
            "tool_calls": [{
                "id": "inspect",
                "name": "inspect_repo_diff_tool",
                "args": {"start_line": 1},
            }],
        },
        {
            "tool_call_id": "inspect",
            "content": json.dumps({
                "status": "ok",
                "base_sha": base_sha,
                "diff_digest": digest,
                "total_lines": 1,
                "page": {"start_line": 1, "end_line": 1, "has_more": False, "next_start_line": None},
            }),
        },
        {
            "tool_calls": [{
                "id": "validation",
                "name": "run_repo_validation_tool",
                "args": {"checks": []},
            }],
        },
        {
            "tool_call_id": "validation",
            "content": json.dumps({
                "status": "ok",
                "all_passed": True,
                "base_sha": base_sha,
                "validated_diff_digest": digest,
                "required_checks": ["diff_check"],
                "executed_checks": [{"name": "diff_check", "check": "diff_check", "passed": True}],
            }),
        },
    ]
    return {
        "mr_id": 536,
        "source_branch": "feature/fix",
        "trigger_type": "pipeline_failed",
        "messages": messages,
    }


def _setup(monkeypatch, tmp_path, git):
    repo = tmp_path / "workspace" / "mr_536" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr(commit_push_module, "_run_git", git.run)
    monkeypatch.setattr(commit_push_module, "_git_exit_code", git.exit_code)


def _run(runtime, state):
    with execution_context(runtime):
        return json.loads(commit_push_module.commit_and_push_tool.func(state=state))


def test_new_diff_after_first_push_creates_second_commit(monkeypatch, tmp_path):
    git = FakeGit("first-fix")
    _setup(monkeypatch, tmp_path, git)
    broker = MemoryEffectBroker()
    runtime = _runtime(broker)

    first = _run(runtime, _state())
    git.stage("format-fix")
    second = _run(runtime, _state([first]))

    assert first["attempt_sequence"] == 1
    assert first["commit_sha"] == "b" * 40
    assert second["attempt_sequence"] == 2
    assert second["commit_sha"] == "c" * 40
    assert second["attempt_id"] != first["attempt_id"]
    assert git.commit_count == 2
    assert len(broker.effects) == 2
    assert [entry.sequence for entry in broker.manifest_entries] == [1, 2]
    assert broker.manifest_base_tree_sha == "d" * 40


def test_completed_attempt_without_tool_message_recovers_without_second_commit(monkeypatch, tmp_path):
    git = FakeGit("first-fix")
    _setup(monkeypatch, tmp_path, git)
    broker = MemoryEffectBroker()
    runtime = _runtime(broker)

    first = _run(runtime, _state())
    recovered = _run(runtime, _state())

    assert recovered == first
    assert git.commit_count == 1
    assert len(broker.effects) == 1


def test_uncertain_push_response_retries_same_commit(monkeypatch, tmp_path):
    git = FakeGit("first-fix")
    git.push_error_without_advance = True
    _setup(monkeypatch, tmp_path, git)
    broker = MemoryEffectBroker()
    runtime = _runtime(broker)

    first = _run(runtime, _state())
    git.push_error_without_advance = False
    recovered = _run(runtime, _state())

    assert first["status"] == "error"
    assert first["commit_sha"] == "b" * 40
    assert recovered["status"] == "success"
    assert recovered["commit_sha"] == "b" * 40
    assert recovered["attempt_id"] == first["attempt_id"]
    assert git.commit_count == 1


def test_clean_worktree_with_recorded_attempt_returns_no_changes(monkeypatch, tmp_path):
    git = FakeGit("first-fix")
    _setup(monkeypatch, tmp_path, git)
    broker = MemoryEffectBroker()
    runtime = _runtime(broker)

    first = _run(runtime, _state())
    result = _run(runtime, _state([first]))

    assert result["status"] == "no_changes"
    assert result["changed"] is False
    assert result["commit_sha"] is None
    assert "attempt_id" not in result
    assert len(broker.effects) == 1


def test_commit_push_rejects_remote_advance_without_force_push(monkeypatch, tmp_path):
    git = FakeGit("first-fix")
    git.remote = "someone-else"
    _setup(monkeypatch, tmp_path, git)
    broker = MemoryEffectBroker()
    result = _run(_runtime(broker), _state())

    assert result["status"] == "blocked"
    assert result["error_code"] == "remote_branch_changed"
    assert result["retryable"] is False
    assert "拒绝覆盖" in result["message"]
    assert not any(call and call[0] == "push" for call in git.calls)


def test_commit_push_marks_initial_push_conflict_terminal(monkeypatch, tmp_path):
    git = FakeGit("first-fix")
    git.push_conflict_sha = "advanced"
    _setup(monkeypatch, tmp_path, git)
    broker = MemoryEffectBroker()
    result = _run(_runtime(broker), _state())

    assert result["status"] == "blocked"
    assert result["error_code"] == "remote_branch_changed"
    assert result["retryable"] is False
    assert result["commit_sha"] == "b" * 40
    effect = next(iter(broker.effects.values()))
    assert effect.status == "completed"
    commit_calls = [call for call in git.calls if call[:2] == ["commit", "-m"]]
    assert len(commit_calls) == 1
    assert "[pr-agent-task:task-536:push-attempt:1:" in commit_calls[0][2]


def test_native_commit_rejects_staged_diff_changed_after_validation(monkeypatch, tmp_path):
    import ut_agent.config as config_module
    import ut_agent.repair_safety as safety_module

    git = FakeGit("changed-after-validation")
    _setup(monkeypatch, tmp_path, git)
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")
    monkeypatch.setattr(safety_module, "validate_member_substitutions", lambda *_args, **_kwargs: (True, ""))
    broker = MemoryEffectBroker()

    result = _run(_runtime(broker), _native_state("first-fix"))

    assert result["status"] == "blocked"
    assert result["error_code"] == "native_commit_digest_mismatch"
    assert result["retryable"] is True
    assert git.commit_count == 0
    assert ["push", "origin", "feature/fix"] not in git.calls
    assert broker.effects == {}


def test_native_commit_accepts_digest_identical_staged_diff(monkeypatch, tmp_path):
    import ut_agent.config as config_module
    import ut_agent.repair_safety as safety_module

    git = FakeGit("first-fix")
    _setup(monkeypatch, tmp_path, git)
    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")
    monkeypatch.setattr(safety_module, "validate_member_substitutions", lambda *_args, **_kwargs: (True, ""))

    result = _run(_runtime(MemoryEffectBroker()), _native_state("first-fix"))

    assert result["status"] == "success"
    assert result["commit_sha"] == "b" * 40
    assert git.commit_count == 1
    assert ["push", "origin", "feature/fix"] in git.calls
