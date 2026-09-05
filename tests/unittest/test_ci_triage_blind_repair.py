import json

import pytest

import pr_agent.config_loader  # noqa: F401 - Initialize Dynaconf before importing ut_agent.
from ut_agent.dependency_evidence import resolve_current_dependency_evidence
from ut_agent.pipeline_actions import next_mandatory_pipeline_action


@pytest.fixture(autouse=True)
def hermes_backend(monkeypatch):
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "hermes")

CURRENT_DIAGNOSTIC = (
    "src/eabot_das_manager_component.cpp:142:23: error: "
    "eabot_msgs::srv::RemoteControl_Request_ has no member named 'node_name'"
)
CURRENT_CONTRACT = (
    "int64 timestamp_ns\n"
    "uint32 command\n"
    "string trace_id\n"
    "string optional\n"
    "---\n"
    "int64 timestamp_ns\n"
    "string trace_id\n"
    "bool success\n"
)


def _exchange(name, index, arguments, result):
    call_id = f"blind-{index}-{name}"
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }],
        },
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)},
    ]


def _failed_pipeline():
    return {
        "status": "success",
        "requested_commit_sha": "source-sha",
        "matched_commit_sha": "source-sha",
        "pipeline_id": 30960,
        "pipeline_status": "failed",
        "failed_jobs": [{
            "job_id": 99429,
            "pipeline_id": 30960,
            "name": "build_release_arm64",
            "status": "failed",
            "causal_lines": [CURRENT_DIAGNOSTIC],
        }],
        "work_items": [{
            "job_id": 99429,
            "pipeline_id": 30960,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
            "root_cause_id": "root-current",
            "canonical_job_name": "build_release_arm64",
        }],
        "root_cause_groups": [{
            "root_cause_id": "root-current",
            "canonical_job_name": "build_release_arm64",
            "job_names": ["build_release_arm64"],
            "canonical_diagnostic": CURRENT_DIAGNOSTIC,
        }],
    }


class _File:
    def __init__(self, content):
        self._content = content

    def decode(self):
        return self._content


class _Files:
    def get(self, *, file_path, ref):
        assert ref == "current-lhotse-sha"
        files = {
            "eabot_msgs/package.xml": b"<package><name>eabot_msgs</name></package>",
            "eabot_msgs/srv/RemoteControl.srv": CURRENT_CONTRACT.encode(),
        }
        if file_path not in files:
            raise FileNotFoundError(file_path)
        return _File(files[file_path])


class _Branches:
    def get(self, branch):
        assert branch == "dev"
        return type("Branch", (), {"commit": {"id": "current-lhotse-sha"}})()


class _Project:
    branches = _Branches()
    files = _Files()

    @staticmethod
    def repository_tree(*, ref, recursive, iterator, path=None):
        assert (ref, path, recursive, iterator) == ("current-lhotse-sha", "eabot_msgs", True, True)
        return iter([
            {"type": "blob", "path": "eabot_msgs/package.xml"},
            {"type": "blob", "path": "eabot_msgs/srv/RemoteControl.srv"},
        ])

    def __getattr__(self, name):
        raise AssertionError(f"dependency mutation/history API is forbidden: {name}")


class _Projects:
    @staticmethod
    def get(project_path):
        assert project_path == "eabot/lhotse"
        return _Project()


class _GitLab:
    projects = _Projects()

    def __getattr__(self, name):
        raise AssertionError(f"dependency mutation/history API is forbidden: {name}")


def test_blind_current_contract_repair_trace_is_deterministic(tmp_path):
    repo = tmp_path / "repo"
    (repo / "dev_kit").mkdir(parents=True)
    (repo / "dev_kit" / "deps.yml").write_text(
        "dependencies:\n"
        "  - module: lhotse\n"
        "    url: git@gitlab.example.com:eabot/lhotse.git\n"
        "    branch: dev\n",
        encoding="utf-8",
    )
    source = repo / "component.cpp"
    source.write_text(
        "void handle(Request *request) {\n"
        "  if (request->node_name == local_name()) {\n"
        "    dispatch(request->command, request->trace_id, request->optional);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    dependency_snapshot_before = (repo / "dev_kit" / "deps.yml").read_bytes()
    state = {
        "trigger_type": "pipeline_failed",
        "pipeline_id": 30960,
        "commit_sha": "source-sha",
        "messages": [],
    }
    trace = []

    for index in range(12):
        action = next_mandatory_pipeline_action(state)
        assert action is not None
        label = action.name
        if action.name == "generate_code_tool":
            label += f":{action.arguments['operation']}"
        elif action.name == "finish_tool":
            label += ":success" if action.arguments["success"] else ":failed"
        trace.append(label)

        if action.name == "fetch_pipeline_logs_tool":
            result = _failed_pipeline()
        elif action.name == "clone_source_branch_tool":
            state["workspace_snapshot"] = {"status": "ready", "local_sha": "source-sha"}
            result = {"status": "ready", "local_sha": "source-sha"}
        elif action.name == "resolve_dependency_evidence_tool":
            result = resolve_current_dependency_evidence(_GitLab(), str(repo), CURRENT_DIAGNOSTIC)
            result.update({"root_cause_id": "root-current", "job_name": "build_release_arm64"})
            assert result["status"] == "resolved"
        elif action.name == "generate_code_tool" and action.arguments["operation"] == "investigate":
            result = {
                "status": "investigated",
                "operation": "investigate",
                "root_cause_id": "root-current",
                "job_name": "build_release_arm64",
                "diagnostic": "Current request contract does not declare node_name.",
            }
        elif action.name == "generate_code_tool" and action.arguments["operation"] == "repair":
            source.write_text(
                "void handle(Request *request) {\n"
                "  dispatch(request->command, request->trace_id, request->optional);\n"
                "}\n",
                encoding="utf-8",
            )
            result = {
                "status": "changed",
                "operation": "repair",
                "root_cause_id": "root-current",
                "job_name": "build_release_arm64",
                "changed_files": ["component.cpp"],
            }
        elif action.name == "commit_and_push_tool":
            result = {
                "status": "success",
                "changed": True,
                "commit_sha": "fixed-sha",
                "attempt_id": "blind-push-1",
            }
        elif action.name == "wait_pipeline_tool":
            result = {
                "status": "success",
                "requested_commit_sha": "fixed-sha",
                "matched_commit_sha": "fixed-sha",
                "pipeline_id": 31000,
                "pipeline_status": "success",
                "failed_jobs": [],
                "attempt_id": "blind-push-1",
            }
        elif action.name == "finish_tool":
            break
        else:
            raise AssertionError(f"unexpected blind action: {action}")
        state["messages"] += _exchange(action.name, index, action.arguments, result)

    assert trace == [
        "fetch_pipeline_logs_tool",
        "clone_source_branch_tool",
        "resolve_dependency_evidence_tool",
        "generate_code_tool:investigate",
        "generate_code_tool:repair",
        "commit_and_push_tool",
        "wait_pipeline_tool",
        "finish_tool:success",
    ]
    assert "request->node_name" not in source.read_text(encoding="utf-8")
    assert "request->target" not in source.read_text(encoding="utf-8")
    assert dependency_snapshot_before == (repo / "dev_kit" / "deps.yml").read_bytes()
    assert len(trace) < 12
