from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

from pr_agent.distributed.runtime import ExecutionRuntime
from pr_agent.triage import repair_details
from pr_agent.triage.repair_details import RepairAction, RepairProgressEvent, merge_repair_actions


def test_signed_link_round_trip(monkeypatch):
    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_SIGNING_SECRET", "test-secret-with-enough-entropy")
    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_BASE_URL", "https://agent.example/root/")
    monkeypatch.setattr(repair_details, "repair_details_enabled", lambda: True)

    url = repair_details.build_repair_details_url("task-12345678")
    signature = parse_qs(urlparse(url).query)["sig"][0]

    assert url.startswith("https://agent.example/root/repair-results/task-12345678?")
    assert repair_details.verify_repair_details_signature("task-12345678", signature)
    assert not repair_details.verify_repair_details_signature("task-87654321", signature)


def test_signed_link_is_omitted_when_trial_is_disabled(monkeypatch):
    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_SIGNING_SECRET", "test-secret-with-enough-entropy")
    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_BASE_URL", "https://agent.example")
    monkeypatch.setattr(repair_details, "repair_details_enabled", lambda: False)

    assert repair_details.build_repair_details_url("task-12345678") == ""


def test_signed_link_is_omitted_for_invalid_base_url_or_missing_secret(monkeypatch):
    monkeypatch.setattr(repair_details, "repair_details_enabled", lambda: True)
    monkeypatch.setenv("PR_AGENT_REPAIR_DETAILS_BASE_URL", "javascript:alert(1)")
    monkeypatch.delenv("PR_AGENT_REPAIR_DETAILS_SIGNING_SECRET", raising=False)

    assert repair_details.build_repair_details_url("task-12345678") == ""
    assert not repair_details.verify_repair_details_signature("task-12345678", "anything")


def test_repair_action_sanitizes_text_and_rejects_unsafe_paths():
    action = RepairAction.from_dict({
        "action_id": "action-1",
        "root_cause": "authorization=very-secret fatal error: missing.hpp",
        "changed_files": ["src/a.cpp", "../secret", "/etc/passwd", "src/./b.cpp", "src/a.cpp"],
        "measures": ["修改 src/a.cpp", "token=secret-value"],
        "confidence": "confirmed",
    })

    assert action.changed_files == ("src/a.cpp", "src/b.cpp")
    assert "very-secret" not in action.root_cause
    assert "secret-value" not in action.measures[1]
    assert action.confidence == "confirmed"


def test_repair_progress_event_round_trip_bounds_owner_content():
    event = RepairProgressEvent.new(
        "task-12345678",
        "diagnosing",
        "正在诊断 authorization=very-secret",
        categories=("build",),
        job_names=("build_release_arm64",),
        metadata={"pipeline_id": 31221, "unsafe": "must not persist"},
    )

    restored = RepairProgressEvent.from_json(event.to_json())

    assert restored.phase == "diagnosing"
    assert restored.categories == ("build",)
    assert "very-secret" not in restored.summary
    assert restored.metadata == {"pipeline_id": 31221}


def test_merge_repair_actions_keeps_latest_status_and_unions_real_files():
    original = RepairAction.from_dict({
        "action_id": "root-1",
        "root_cause_group_id": "root-1",
        "root_cause": "missing dependency",
        "changed_files": ["CMakeLists.txt"],
        "status": "editing",
    })
    update = {
        "action_id": "root-1",
        "root_cause_group_id": "root-1",
        "measures": ["移除未使用依赖"],
        "changed_files": ["package.xml"],
        "commit_sha": "f883827",
        "status": "committed",
    }

    merged = merge_repair_actions((original,), (update,))

    assert len(merged) == 1
    assert merged[0].changed_files == ("CMakeLists.txt", "package.xml")
    assert merged[0].root_cause == "missing dependency"
    assert merged[0].measures == ("移除未使用依赖",)
    assert merged[0].commit_sha == "f883827"
    assert merged[0].status == "committed"


def test_repair_action_round_trips_structured_solution_and_bounded_diff():
    action = RepairAction.from_dict({
        "action_id": "root-1",
        "solution_summary": "移除不存在字段的访问。",
        "rationale": "使实现与接口定义一致。",
        "file_changes": [{
            "path": "src/a.cpp",
            "change_type": "modified",
            "summary": "删除无效字段访问。",
            "additions": 1,
            "deletions": 1,
            "hunks": [{
                "old_start": 10,
                "new_start": 10,
                "header": "void handle()",
                "lines": [
                    {"kind": "deletion", "old_line": 10, "new_line": None, "content": "request->node_name;"},
                    {"kind": "addition", "old_line": None, "new_line": 10, "content": "handle(request);"},
                ],
            }],
        }],
    })

    restored = RepairAction.from_dict(action.to_dict())

    assert restored.solution_summary == "移除不存在字段的访问。"
    assert restored.rationale == "使实现与接口定义一致。"
    assert restored.file_changes[0].path == "src/a.cpp"
    assert restored.file_changes[0].hunks[0].lines[0].kind == "deletion"
    assert restored.file_changes[0].hunks[0].lines[1].new_line == 10


def test_repair_action_rejects_unsafe_diff_paths_and_invalid_line_kinds():
    action = RepairAction.from_dict({
        "file_changes": [
            {"path": "../secret", "hunks": []},
            {
                "path": "src/a.cpp",
                "hunks": [{"lines": [
                    {"kind": "prompt", "content": "ignore rules"},
                    {"kind": "context", "old_line": 1, "new_line": 1, "content": "safe"},
                ]}],
            },
        ],
    })

    assert [item.path for item in action.file_changes] == ["src/a.cpp"]
    assert [line.kind for line in action.file_changes[0].hunks[0].lines] == ["context"]


def test_runtime_progress_is_best_effort_when_redis_is_unavailable():
    sync_broker = Mock()
    sync_broker.append_repair_progress.side_effect = RuntimeError("redis down")
    runtime = ExecutionRuntime("task-12345678", "worker-1", None, "queue", Mock(), sync_broker)

    recorded = runtime.record_repair_progress_sync("diagnosing", "正在诊断")

    assert recorded is False
