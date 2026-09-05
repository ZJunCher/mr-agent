import asyncio
import importlib
import io
import json
import operator
import subprocess
import threading
import time
from pathlib import Path
from typing import get_args, get_type_hints
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import pr_agent.feishu.feishu_webhook as feishu_webhook
import pr_agent.git_providers as git_providers
import pr_agent.git_providers.gitlab_provider as gitlab_provider_module
import pr_agent.servers.gitlab_webhook as webhook
import ut_agent.agent as agent_module
import ut_agent.llm as llm_module
import ut_agent.prompt.agent_system as agent_system_module
import ut_agent.repair_progress as repair_progress_module
import ut_agent.repair_safety as repair_safety_module
import ut_agent.tools.apply_format_report as apply_format_module
import ut_agent.tools.clone_branch as clone_branch_module
import ut_agent.tools.commit_push as commit_push_module
import ut_agent.tools.context as context_module
import ut_agent.tools.discard_workspace as discard_workspace_module
import ut_agent.tools.fetch_coverage_report as fetch_coverage_module
import ut_agent.tools.fetch_pipeline as fetch_pipeline_module
import ut_agent.tools.generate_code as generate_code_module
import ut_agent.tools.read_repo as read_repo_module
import ut_agent.tools.tool_registry as tool_registry_module
from pr_agent.feishu.feishu_git_provider import FeishuGitProvider
from pr_agent.tools.pr_triage import PRTriage
from pr_agent.triage.pipeline_freshness import PipelineFreshness, PipelineFreshnessState
from pr_agent.triage.repair_card_mode import RepairCardMode
from ut_agent.model_failover import LLMCallOutcome, ModelAttempt, ModelHealthStore
from ut_agent.state import AgentState
from ut_agent.tools.context import ToolContext

MR_547_NODE_NAME_ERROR = (
    "src/eabot_das_manager/eabot_das_manager/src/eabot_das_manager_component.cpp:142:23: "
    "error: 'RemoteControl_Request' has no member named 'node_name'"
)


@pytest.fixture(autouse=True)
def hermes_repair_backend(monkeypatch):
    """Keep the legacy regression matrix explicit about the backend it exercises."""
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "hermes")


@pytest.fixture
def native_backend(monkeypatch):
    import ut_agent.config as config_module

    monkeypatch.setattr(config_module, "REPAIR_BACKEND", "native")


def test_feishu_command_does_not_patch_global_provider_registry(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    captured = {}

    class GitLabProvider:
        def __init__(self, pr_url):
            self.pr_url = pr_url

    async def fake_handle_request(_self, pr_url, _command):
        captured["provider"] = git_providers.get_git_provider_with_context(pr_url)
        entered.set()
        await release.wait()

    monkeypatch.setattr(gitlab_provider_module, "GitLabProvider", GitLabProvider)
    monkeypatch.setattr(feishu_webhook.PRAgent, "handle_request", fake_handle_request)
    original_factory = git_providers._GIT_PROVIDERS["gitlab"]

    async def run():
        task = asyncio.create_task(feishu_webhook.run_pr_agent("https://gitlab.example/mr/1", "/triage", "user-1"))
        await entered.wait()
        try:
            assert git_providers._GIT_PROVIDERS["gitlab"] is original_factory
        finally:
            release.set()
            await task

    asyncio.run(run())

    assert isinstance(captured["provider"], FeishuGitProvider)
    assert captured["provider"].feishu_sender_id == "user-1"


def test_downstream_pipeline_failure_does_not_send_duplicate_card(monkeypatch):
    class Settings:
        @staticmethod
        def get(key, default=None):
            return True if key == "FEISHU.NOTIFY_ON_PIPELINE_FAILURE" else default

    class Response:
        ok = True

        @staticmethod
        def json():
            return []

    calls = []
    monkeypatch.setattr(webhook, "get_settings", lambda: Settings())
    monkeypatch.setattr(webhook, "_read_feishu_setting", lambda *_args: "app-id")
    monkeypatch.setattr(
        webhook,
        "_get_failed_pipeline_jobs",
        lambda project_id, pipeline_id: calls.append((project_id, pipeline_id)) or [],
    )
    monkeypatch.setattr(webhook._requests, "get", lambda *_args, **_kwargs: Response())

    asyncio.run(
        webhook._notify_feishu_pipeline_failure(
            {
                "project": {"id": 2},
                "object_attributes": {
                    "id": 27642,
                    "ref": "feature",
                    "status": "failed",
                    "source": "parent_pipeline",
                },
            }
        )
    )

    assert calls == []


def test_webhook_fetches_cross_project_downstream_jobs(monkeypatch):
    class Response:
        ok = True
        status_code = 200

        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    responses = {
        "/api/v4/projects/23/pipelines/91/jobs": [],
        "/api/v4/projects/23/pipelines/91/bridges": [
            {"downstream_pipeline": {"id": 92, "project_id": 42}}
        ],
        "/api/v4/projects/42/pipelines/92/jobs": [
            {"id": 12, "name": "child_build", "status": "failed"}
        ],
        "/api/v4/projects/42/pipelines/92/bridges": [],
    }
    calls = []

    def api_get(path, **_kwargs):
        calls.append(path)
        return Response(responses[path])

    monkeypatch.setattr(webhook, "_gitlab_api_get", api_get)

    jobs = webhook._collect_failed_pipeline_jobs_recursive(23, 91)

    assert [job["name"] for job in jobs] == ["child_build"]
    assert "/api/v4/projects/42/pipelines/92/jobs" in calls


def test_pipeline_failure_card_identifies_mr_and_carries_card_id():
    markdown, actions, title, binding = webhook._build_pipeline_failure_card(
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        mr_url="https://gitlab.example.com/eabot/cook/-/merge_requests/538",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        categories=["build"],
        failed_jobs=[{"name": "build_release_arm64"}],
    )

    assert title == "【eabot/cook !538】流水线失败"
    assert actions[0]["card_id"] == binding.card_id
    assert actions[0]["pipeline_id"] == 29415
    assert actions[0]["pipeline_sha"] == "abc123"
    assert actions[0]["category"] == "build"
    assert actions[0]["command"] == "triage"
    assert actions[0]["label"] == "修复编译错误"
    assert len(actions) == 1
    assert binding.repair_items[0].category.value == "build"
    assert binding.repair_items[0].failed_job_names == ("build_release_arm64",)
    assert binding.repair_card_mode == "multi_select"
    assert binding.original_markdown == markdown
    assert binding.source_branch == "feature/lidar"
    assert "build_release_arm64" in markdown
    assert binding.mr_url in markdown


def test_pipeline_failure_card_preserves_gitlab_author():
    _, _, _, binding = webhook._build_pipeline_failure_card(
        project_id="eabot/control",
        mr_iid=8,
        mr_title="control reconstruction",
        mr_author_username="xiaoyu.li",
        mr_url="https://gitlab.example.com/eabot/control/-/merge_requests/8",
        source_branch="jack/dev/common_rec",
        pipeline_id=31089,
        pipeline_sha="750bb8c0",
        categories=["unknown"],
        failed_jobs=[{"name": "mr_title_check", "id": 99853}],
    )

    assert binding.mr_author_username == "xiaoyu.li"


def test_pipeline_failure_card_explains_non_repairable_format_job():
    markdown, actions, _, binding = webhook._build_pipeline_failure_card(
        project_id="eabot/map_nav_loc",
        mr_iid=6,
        mr_title="format pipeline failure",
        mr_url="https://gitlab.example.com/eabot/map_nav_loc/-/merge_requests/6",
        source_branch="feature/map",
        pipeline_id=33603,
        pipeline_sha="abc123",
        categories=["format"],
        failed_jobs=[{
            "id": 108359,
            "name": "code_format_check",
            "web_url": "https://gitlab.example.com/eabot/map_nav_loc/-/jobs/108359",
            "auto_repair_eligible": False,
            "format_job_disposition": {
                "kind": "ci_job_configuration",
                "summary": "CI 传给 git diff 的基准 Commit 为空，格式检查尚未开始。",
                "job_url": "https://gitlab.example.com/eabot/map_nav_loc/-/jobs/108359",
            },
        }],
    )

    assert "格式检查尚未开始" in markdown
    assert "[查看 Job 日志](https://gitlab.example.com/eabot/map_nav_loc/-/jobs/108359)" in markdown
    assert "可自动修复代码格式" not in markdown
    assert actions == []
    assert binding.repair_items == ()


def test_format_job_preflight_annotation_uses_exact_trace(monkeypatch):
    class Response:
        ok = True
        text = "ERROR: git diff failed: fatal: ambiguous argument '': unknown revision"

    monkeypatch.setattr(webhook, "_gitlab_api_get", lambda *_args, **_kwargs: Response())

    jobs = webhook._annotate_format_job_dispositions(2, [{
        "id": 108359,
        "name": "code_format_check",
        "web_url": "https://gitlab.example/jobs/108359",
    }])

    assert jobs[0]["auto_repair_eligible"] is False
    assert jobs[0]["format_job_disposition"]["kind"] == "ci_job_configuration"


def test_format_job_preflight_trace_failure_keeps_repair_eligible(monkeypatch):
    class Response:
        ok = False
        status_code = 503
        text = ""

    monkeypatch.setattr(webhook, "_gitlab_api_get", lambda *_args, **_kwargs: Response())

    original = {"id": 108359, "name": "code_format_check"}
    jobs = webhook._annotate_format_job_dispositions(2, [original])

    assert jobs == [original]
    assert jobs[0] is not original


def test_stale_pipeline_failure_does_not_collect_jobs_or_send_card(monkeypatch):
    import pr_agent.distributed.runtime as runtime_module

    class Settings:
        @staticmethod
        def get(key, default=None):
            values = {
                "FEISHU.NOTIFY_ON_PIPELINE_FAILURE": True,
                "GITLAB.URL": "https://gitlab.example",
                "GITLAB.PERSONAL_ACCESS_TOKEN": "",
            }
            return values.get(key, default)

    class Response:
        ok = True

        @staticmethod
        def json():
            return [{
                "iid": 536,
                "sha": "new-sha",
                "title": "repair",
                "web_url": "https://gitlab.example/eabot/cook/-/merge_requests/536",
                "author": {"username": "developer"},
            }]

    broker = Mock(save_triage_card=AsyncMock(), enqueue_notification=AsyncMock())
    runtime = Mock(mode="queue", task_id="pipeline-event", broker=broker)
    monkeypatch.setattr(runtime_module, "get_execution_runtime", lambda: runtime)
    monkeypatch.setattr(webhook, "get_settings", lambda: Settings())
    monkeypatch.setattr(webhook, "_read_feishu_setting", lambda *_args: "app-id")
    failed_jobs = Mock(return_value=[])
    monkeypatch.setattr(webhook, "_get_failed_pipeline_jobs", failed_jobs)
    monkeypatch.setattr(webhook, "_should_suppress_pipeline_card", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(webhook._requests, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(
        webhook,
        "check_pipeline_freshness",
        lambda **_kwargs: PipelineFreshness(
            PipelineFreshnessState.STALE_HEAD,
            head_sha="new-sha",
            reason="head_sha_changed",
        ),
        raising=False,
    )

    asyncio.run(webhook._notify_feishu_pipeline_failure({
        "project": {"id": 2, "path_with_namespace": "eabot/cook"},
        "object_attributes": {
            "id": 30401,
            "ref": "feature/fix",
            "sha": "old-sha",
            "status": "failed",
            "source": "push",
        },
    }))

    failed_jobs.assert_not_called()
    broker.save_triage_card.assert_not_awaited()
    broker.enqueue_notification.assert_not_awaited()


def test_pipeline_notification_does_not_trust_mr_list_sha(monkeypatch):
    import pr_agent.distributed.runtime as runtime_module

    class Settings:
        @staticmethod
        def get(key, default=None):
            values = {
                "FEISHU.NOTIFY_ON_PIPELINE_FAILURE": True,
                "GITLAB.URL": "https://gitlab.example",
                "GITLAB.PERSONAL_ACCESS_TOKEN": "",
            }
            return values.get(key, default)

    class Response:
        ok = True

        @staticmethod
        def json():
            return [{
                "iid": 536,
                "sha": "stale-summary-sha",
                "title": "repair",
                "web_url": "https://gitlab.example/eabot/cook/-/merge_requests/536",
                "author": {"username": "developer"},
            }]

    broker = Mock(
        save_triage_card=AsyncMock(return_value=True),
        enqueue_notification=AsyncMock(return_value=True),
    )
    runtime = Mock(mode="queue", task_id="pipeline-event", broker=broker)
    monkeypatch.setattr(runtime_module, "get_execution_runtime", lambda: runtime)
    monkeypatch.setattr(webhook, "get_settings", lambda: Settings())
    monkeypatch.setattr(webhook, "_read_feishu_setting", lambda *_args: "app-id")
    suppress_args = {}

    def should_suppress(*args, **kwargs):
        suppress_args.update(kwargs)
        return False

    monkeypatch.setattr(webhook, "_should_suppress_pipeline_card", should_suppress)
    monkeypatch.setattr(webhook._requests, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(
        webhook,
        "_get_failed_pipeline_jobs",
        lambda *_args: [{"name": "build_release_arm64", "status": "failed"}],
    )
    freshness_args = {}

    def check_freshness(**kwargs):
        freshness_args.update(kwargs)
        return PipelineFreshness(
            PipelineFreshnessState.CURRENT,
            head_sha="event-sha",
            latest_pipeline_id=31223,
            latest_pipeline_status="failed",
        )

    monkeypatch.setattr(webhook, "check_pipeline_freshness", check_freshness)

    asyncio.run(webhook._notify_feishu_pipeline_failure({
        "project": {"id": 2, "path_with_namespace": "eabot/cook"},
        "object_attributes": {
            "id": 31223,
            "ref": "feature/fix",
            "sha": "event-sha",
            "status": "failed",
            "source": "merge_request_event",
        },
    }))

    assert "mr_payload" not in freshness_args
    assert freshness_args["pipeline_id"] == 31223
    assert freshness_args["pipeline_sha"] == "event-sha"
    assert suppress_args["pipeline_id"] == 31223
    assert suppress_args["pipeline_source"] == "merge_request_event"
    broker.save_triage_card.assert_awaited_once()
    broker.enqueue_notification.assert_awaited_once()


def test_pipeline_failure_card_feature_flag_restores_category_actions(monkeypatch):
    monkeypatch.setattr(
        "pr_agent.triage.repair_card_mode.repair_card_mode",
        lambda: RepairCardMode.LEGACY_ACTIONS,
    )

    _, actions, _, binding = webhook._build_pipeline_failure_card(
        project_id="eabot/cook",
        mr_iid=538,
        mr_title="lidar udp",
        mr_url="https://gitlab.example.com/eabot/cook/-/merge_requests/538",
        source_branch="feature/lidar",
        pipeline_id=29415,
        pipeline_sha="abc123",
        categories=["format", "build"],
        failed_jobs=[{"name": "code_format_check"}, {"name": "build_release_arm64"}],
    )

    assert [action["command"] for action in actions] == ["fix-format", "triage"]
    assert [item.category.value for item in binding.repair_items] == ["format", "build"]


def test_triage_uses_mr_pipeline_and_recursive_failed_jobs(monkeypatch):
    pipeline = type("Pipeline", (), {
        "id": 27908,
        "status": "failed",
        "sha": "94ae4952",
    })()

    class PipelineManager:
        @staticmethod
        def list(**_kwargs):
            return [pipeline]

    merge_request = type("MergeRequest", (), {
        "iid": 521,
        "pipelines": PipelineManager(),
    })()
    project = type("Project", (), {
        "id": 2,
        "mergerequests": type("MergeRequests", (), {"get": staticmethod(lambda _iid: merge_request)})(),
    })()
    provider = type("Provider", (), {
        "id_project": "eabot/cook",
        "pr": merge_request,
        "gl": type("GitLab", (), {
            "projects": type("Projects", (), {"get": staticmethod(lambda _project_id: project)})(),
        })(),
        "get_pr_branch": staticmethod(lambda: "feature"),
    })()
    failed_jobs = [{
        "name": "build_release_arm64",
        "status": "failed",
        "stage": "build",
        "web_url": "https://gitlab.example/jobs/1",
    }]
    calls = []
    monkeypatch.setattr(
        webhook,
        "_get_failed_pipeline_jobs",
        lambda project_id, pipeline_id: calls.append((project_id, pipeline_id)) or failed_jobs,
    )

    triage = PRTriage.__new__(PRTriage)
    triage.git_provider = provider

    assert triage._fetch_failed_pipeline_info() == (failed_jobs, 27908, "94ae4952")
    assert calls == [(2, 27908)]


def test_triage_uses_latest_mr_pipeline_even_when_older_pipeline_failed(monkeypatch):
    old_failed = type("Pipeline", (), {"id": 28161, "status": "failed", "sha": "old"})()
    latest_success = type("Pipeline", (), {"id": 28177, "status": "success", "sha": "new"})()

    class PipelineManager:
        @staticmethod
        def list(**_kwargs):
            return [old_failed, latest_success]

    merge_request = type("MergeRequest", (), {"iid": 300, "pipelines": PipelineManager()})()
    project = type("Project", (), {
        "id": 2,
        "mergerequests": type("MergeRequests", (), {"get": staticmethod(lambda _iid: merge_request)})(),
    })()
    provider = type("Provider", (), {
        "id_project": "eabot/huygens",
        "pr": merge_request,
        "gl": type("GitLab", (), {
            "projects": type("Projects", (), {"get": staticmethod(lambda _project_id: project)})(),
        })(),
        "get_pr_branch": staticmethod(lambda: "feature"),
    })()
    calls = []
    monkeypatch.setattr(
        webhook,
        "_get_failed_pipeline_jobs",
        lambda project_id, pipeline_id: calls.append((project_id, pipeline_id)) or [],
    )
    triage = PRTriage.__new__(PRTriage)
    triage.git_provider = provider

    assert triage._fetch_failed_pipeline_info() == ([], 28177, "new")
    assert calls == [(2, 28177)]


def test_scoped_triage_uses_exact_pipeline_and_selected_jobs(monkeypatch):
    exact_pipeline = type("Pipeline", (), {"id": 28161, "status": "failed", "sha": "exact-sha"})()
    project = type(
        "Project",
        (),
        {
            "id": 2,
            "pipelines": type("Pipelines", (), {"get": staticmethod(lambda _pipeline_id: exact_pipeline)})(),
        },
    )()
    provider = type(
        "Provider",
        (),
        {
            "id_project": "eabot/huygens",
            "pr": type("MergeRequest", (), {"iid": 300})(),
            "gl": type(
                "GitLab",
                (),
                {"projects": type("Projects", (), {"get": staticmethod(lambda _project_id: project)})()},
            )(),
            "get_pr_branch": staticmethod(lambda: "feature"),
        },
    )()
    failed_jobs = [
        {"name": "code_format_check", "status": "failed"},
        {"name": "clang_tidy_check", "status": "failed"},
        {"name": "build_release_arm64", "status": "failed"},
    ]
    monkeypatch.setattr(webhook, "_get_failed_pipeline_jobs", lambda _project_id, _pipeline_id: failed_jobs)
    triage = PRTriage.__new__(PRTriage)
    triage.git_provider = provider
    triage.pipeline_id = 28161
    triage.pipeline_sha = "exact-sha"
    triage.selected_categories = ("clang", "build")

    jobs, pipeline_id, sha = triage._fetch_failed_pipeline_info()

    assert pipeline_id == 28161
    assert sha == "exact-sha"
    assert [job["name"] for job in jobs] == ["clang_tidy_check", "build_release_arm64"]


def test_fetch_pipeline_logs_includes_downstream_failed_jobs(monkeypatch):
    failed_job = type("Job", (), {"id": 89061, "name": "build_release_arm64", "status": "failed"})()
    child = type("Pipeline", (), {
        "id": 27909,
        "status": "failed",
        "coverage": None,
        "jobs": type("Jobs", (), {"list": staticmethod(lambda **_kwargs: [failed_job])})(),
        "bridges": type("Bridges", (), {"list": staticmethod(lambda **_kwargs: [])})(),
    })()
    bridge = type("Bridge", (), {
        "name": "trigger_jobs",
        "downstream_pipeline": {"id": 27909},
    })()
    parent = type("Pipeline", (), {
        "id": 27908,
        "status": "failed",
        "coverage": None,
        "jobs": type("Jobs", (), {"list": staticmethod(lambda **_kwargs: [])})(),
        "bridges": type("Bridges", (), {"list": staticmethod(lambda **_kwargs: [bridge])})(),
    })()
    project = type("Project", (), {
        "pipelines": type("Pipelines", (), {
            "get": staticmethod(lambda pipeline_id: {27908: parent, 27909: child}[pipeline_id]),
        })(),
    })()
    provider = type("Provider", (), {
        "id_project": "eabot/cook",
        "gl": type("GitLab", (), {
            "projects": type("Projects", (), {"get": staticmethod(lambda _project_id: project)})(),
        })(),
    })()
    monkeypatch.setattr(ToolContext, "git_provider", provider)
    monkeypatch.setattr(fetch_pipeline_module, "_get_job_log_tail", lambda *_args: "compiler error")

    result = fetch_pipeline_module.fetch_pipeline_logs_tool.func(
        pipeline_id=27908,
        state={"project_id": "eabot/cook"},
    )

    assert '"name": "build_release_arm64"' in result
    assert '"log_tail": "compiler error"' in result


def test_fetch_pipeline_logs_includes_code_format_report(monkeypatch):
    failed_job = type("Job", (), {"id": 89421, "name": "code_format_check", "status": "failed"})()
    pipeline = type("Pipeline", (), {
        "id": 28013,
        "status": "failed",
        "coverage": None,
        "jobs": type("Jobs", (), {"list": staticmethod(lambda **_kwargs: [failed_job])})(),
        "bridges": type("Bridges", (), {"list": staticmethod(lambda **_kwargs: [])})(),
    })()
    job = type("ProjectJob", (), {
        "artifact": staticmethod(
            lambda path: (
                b"src/eabot_das_ssm/src/ssm.cpp: changed lines need clang-format\n"
                b"-    auto to_seconds = [](const auto& stamp) {\n"
                b"+    auto to_seconds = [](const auto &stamp) {\n"
            ) if path == "code-format-report.txt" else b""
        ),
    })()
    project = type("Project", (), {
        "pipelines": type("Pipelines", (), {"get": staticmethod(lambda _pipeline_id: pipeline)})(),
        "jobs": type("Jobs", (), {"get": staticmethod(lambda _job_id: job)})(),
    })()
    provider = type("Provider", (), {
        "id_project": "eabot/cook",
        "gl": type("GitLab", (), {
            "projects": type("Projects", (), {"get": staticmethod(lambda _project_id: project)})(),
        })(),
    })()
    monkeypatch.setattr(ToolContext, "git_provider", provider)
    monkeypatch.setattr(fetch_pipeline_module, "_get_job_log_tail", lambda *_args: "format check failed")

    result = fetch_pipeline_module.fetch_pipeline_logs_tool.func(
        pipeline_id=28013,
        state={"project_id": "eabot/cook"},
    )

    assert "code-format-report.txt" in result
    assert "const auto &stamp" in result


def test_fetch_pipeline_logs_returns_work_items_and_filters_exact_job(monkeypatch):
    jobs = [
        type("Job", (), {"id": 11, "name": "build_release_arm64", "status": "failed"})(),
        type("Job", (), {"id": 12, "name": "code_format_check", "status": "failed"})(),
        type("Job", (), {"id": 13, "name": "mr_merge_commit_check", "status": "failed"})(),
        type("Job", (), {"id": 14, "name": "x86_64_ut_coverage_check", "status": "failed"})(),
    ]
    pipeline = type("Pipeline", (), {
        "id": 28177,
        "status": "failed",
        "sha": "abc123",
        "coverage": None,
        "jobs": type("Jobs", (), {"list": staticmethod(lambda **_kwargs: jobs)})(),
        "bridges": type("Bridges", (), {"list": staticmethod(lambda **_kwargs: [])})(),
    })()
    project = type("Project", (), {
        "pipelines": type("Pipelines", (), {"get": staticmethod(lambda _pipeline_id: pipeline)})(),
    })()
    provider = type("Provider", (), {
        "id_project": "eabot/chogori",
        "gl": type("GitLab", (), {
            "projects": type("Projects", (), {"get": staticmethod(lambda _project_id: project)})(),
        })(),
    })()
    monkeypatch.setattr(ToolContext, "git_provider", provider)
    monkeypatch.setattr(
        fetch_pipeline_module,
        "_get_failed_job_diagnostics",
        lambda _project, job, _lines: f"{job.name} diagnostic",
    )

    result = json.loads(fetch_pipeline_module.fetch_pipeline_logs_tool.func(
        pipeline_id=28177,
        job_name="mr_merge_commit_check",
        state={"project_id": "eabot/chogori"},
    ))

    assert result["failed_jobs"] == [{
        "job_id": 13,
        "pipeline_id": 28177,
        "name": "mr_merge_commit_check",
        "status": "failed",
        "log_tail": "mr_merge_commit_check diagnostic",
        "diagnostic_candidates": [],
        "diagnostic_candidate_count": 0,
        "diagnostic_candidates_truncated": False,
        "diagnostic_identity_count": 0,
        "omitted_diagnostic_identity_count": 0,
        "causal_lines": ["mr_merge_commit_check diagnostic"],
        "log_context": "mr_merge_commit_check diagnostic",
    }]
    core_work_items = [
        {key: item[key] for key in ("job_id", "pipeline_id", "job_name", "kind", "required_tool")}
        for item in result["work_items"]
    ]
    assert core_work_items == [
        {
            "job_id": 11,
            "pipeline_id": 28177,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
        },
        {
            "job_id": 12,
            "pipeline_id": 28177,
            "job_name": "code_format_check",
            "kind": "format",
            "required_tool": "apply_format_report_tool",
        },
        {
            "job_id": 13,
            "pipeline_id": 28177,
            "job_name": "mr_merge_commit_check",
            "kind": "merge_check",
            "required_tool": "generate_code_tool",
        },
        {
            "job_id": 14,
            "pipeline_id": 28177,
            "job_name": "x86_64_ut_coverage_check",
            "kind": "coverage",
            "required_tool": "fetch_coverage_report_tool",
        },
    ]
    assert all(item["root_cause_id"] for item in result["work_items"])
    assert result["root_cause_groups"]


def test_selected_build_scope_excludes_new_format_work_item():
    from ut_agent.state import AgentState

    failed_jobs = [
        {"name": "build_release_arm64", "status": "failed"},
        {"name": "x86_64_ut_coverage_check", "status": "failed"},
        {"name": "code_format_check", "status": "failed"},
    ]

    scoped = fetch_pipeline_module._scope_failed_jobs(failed_jobs, {"selected_categories": ["build"]})

    assert "selected_categories" in AgentState.__annotations__
    assert [job["name"] for job in scoped] == ["build_release_arm64", "x86_64_ut_coverage_check"]


def test_apply_format_report_uses_artifact_without_local_formatter(monkeypatch, tmp_path):
    repo_dir = tmp_path / "workspace" / "mr_300" / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    source = repo_dir / "src" / "main.cpp"
    source.parent.mkdir()
    source.write_text("int  value;\n")
    report = (
        "--- a/src/main.cpp\n"
        "+++ b/src/main.cpp\n"
        "@@ -1,1 +1,1 @@\n"
        "-int  value;\n"
        "+int value;\n"
    )
    job = type("Job", (), {
        "id": 12,
        "trace": staticmethod(lambda: b"format failed"),
        "artifact": staticmethod(lambda path: report.encode() if path == "code-format-report.txt" else b""),
    })()
    project = type("Project", (), {
        "jobs": type("Jobs", (), {"get": staticmethod(lambda _job_id: job)})(),
    })()
    provider = type("Provider", (), {
        "id_project": "",
        "gl": type("GitLab", (), {
            "projects": type("Projects", (), {"get": staticmethod(lambda _project_id: project)})(),
        })(),
    })()
    monkeypatch.setattr(ToolContext, "git_provider", provider)
    monkeypatch.setattr(ToolContext, "output_dir", str(tmp_path / "workspace"))

    result = json.loads(apply_format_module.apply_format_report_tool.func(
        pipeline_id=28177,
        job_id=12,
        state={"mr_id": 300, "project_id": "eabot/chogori"},
    ))

    assert result == {
        "status": "changed",
        "pipeline_id": 28177,
        "job_id": 12,
        "job_name": "code_format_check",
        "changed_files": ["src/main.cpp"],
        "message": "已从 code-format-report.txt 应用 1 个文件的格式修复。",
    }
    assert source.read_text() == "int value;\n"


def test_discard_workspace_tool_reverts_all_uncommitted_changes(monkeypatch, tmp_path):
    import subprocess as sp
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_300" / "repo"
    repo.mkdir(parents=True)
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "src.cpp").write_text("int main() { return 0; }\n")
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / "src.cpp").write_text("junk modification\n")
    (repo / "junk_test_plan.md").write_text("junk file from hermes\n")
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))

    result = json.loads(discard_workspace_module.discard_workspace_tool.func(
        reason="Hermes 生成了与修复任务无关的文件",
        state={"mr_id": 300},
    ))

    assert result["status"] == "success"
    assert sorted(result["discarded_files"]) == ["junk_test_plan.md", "src.cpp"]
    assert (repo / "src.cpp").read_text() == "int main() { return 0; }\n"
    assert not (repo / "junk_test_plan.md").exists()


def test_apply_format_report_blocked_message_includes_job_failure_root_cause(monkeypatch, tmp_path):
    repo = tmp_path / "workspace" / "mr_300" / "repo"
    (repo / ".git").mkdir(parents=True)
    job = type("Job", (), {
        "id": 90243,
        "trace": staticmethod(lambda: (
            b"Checking format...\n"
            b"fatal: bad object 7b292e05202822bbdd561392cb9e3714e4e943f3\n"
            b"ERROR: Job failed: exit code 1\n"
        )),
        "artifact": staticmethod(lambda _path: (_ for _ in ()).throw(Exception("404: 404 Not Found"))),
    })()
    project = type("Project", (), {
        "jobs": type("Jobs", (), {"get": staticmethod(lambda _job_id: job)})(),
    })()
    provider = type("Provider", (), {
        "id_project": "",
        "gl": type("GitLab", (), {
            "projects": type("Projects", (), {"get": staticmethod(lambda _project_id: project)})(),
            "session": None,
        })(),
    })()
    monkeypatch.setattr(ToolContext, "git_provider", provider)
    monkeypatch.setattr(ToolContext, "output_dir", str(tmp_path / "workspace"))

    result = json.loads(apply_format_module.apply_format_report_tool.func(
        pipeline_id=28229,
        job_id=90243,
        state={"mr_id": 300, "project_id": "eabot/chogori"},
    ))

    assert result["status"] == "blocked"
    assert "bad object 7b292e05" in result["message"]
    assert "job 自身执行失败" in result["message"]


def test_pipeline_log_summary_preserves_earlier_fallback_and_later_compiler_error():
    lines = [f"setup line {i}" for i in range(40)]
    lines.append("ci_deps file not found or download failed (HTTP 404), using default deps.yml")
    lines.extend(f"dependency line {i}" for i in range(40))
    lines.append("src/main.cpp:15:5: error: consteval does not name a type")
    lines.extend(f"compiler context {i}" for i in range(40))
    trace = "\n".join(lines).encode()
    job = type("Job", (), {"trace": staticmethod(lambda: trace)})()
    project = type("Project", (), {
        "jobs": type("Jobs", (), {"get": staticmethod(lambda _job_id: job)})(),
    })()

    summary = fetch_pipeline_module._get_job_log_tail(project, 1, 30)

    assert "download failed" in summary
    assert "consteval does not name a type" in summary
    assert summary.index("download failed") < summary.index("consteval does not name a type")


def test_pipeline_log_summary_includes_dependency_clone_failure():
    lines = ["setup"] * 20
    lines.extend([
        "SSH 连接检测失败: gitlab.example.com",
        "无法访问仓库: logan",
        *["help text"] * 80,
        "预处理失败（退出码: 1）",
    ])
    job = type("Job", (), {"trace": staticmethod(lambda: "\n".join(lines).encode())})()
    project = type("Project", (), {
        "jobs": type("Jobs", (), {"get": staticmethod(lambda _job_id: job)})(),
    })()

    summary = fetch_pipeline_module._get_job_log_tail(project, 1, 30)

    assert "无法访问仓库: logan" in summary


def test_missing_coverage_artifact_is_explicitly_unknown(monkeypatch):
    job = type("Job", (), {
        "artifact": staticmethod(lambda _path: (_ for _ in ()).throw(Exception("404 Not Found"))),
    })()
    project = type("Project", (), {
        "jobs": type("Jobs", (), {"get": staticmethod(lambda _job_id: job)})(),
    })()
    provider = type("Provider", (), {
        "id_project": "eabot/cook",
        "gl": type("GitLab", (), {
            "projects": type("Projects", (), {"get": staticmethod(lambda _project_id: project)})(),
        })(),
    })()
    monkeypatch.setattr(ToolContext, "git_provider", provider)

    result = fetch_coverage_module.fetch_changed_lines_report(123)
    tool_result = json.loads(fetch_coverage_module.fetch_coverage_report_tool.func(job_id=123, state={}))

    assert result["status"] == "unknown"
    assert result["available"] is False
    assert tool_result["status"] == "unknown"
    assert "404" in tool_result["message"]


def test_ut_agent_starts_with_a_user_message(monkeypatch, tmp_path):
    received_state = {}

    class Graph:
        async def ainvoke(self, state, config=None):
            received_state.update(state)
            return {"messages": [{"role": "assistant", "content": "done"}]}

    agent = agent_module.UTAgent.__new__(agent_module.UTAgent)
    agent.graph = Graph()
    monkeypatch.setattr(agent_module, "CONVERSATION_LOG", str(tmp_path / "conversation.log"))

    asyncio.run(
        agent.run(
            {
                "trigger_type": "pipeline_failed",
                "mr_id": 514,
                "title": "Test",
                "source_branch": "feature",
                "target_branch": "master",
            }
        )
    )

    assert received_state["messages"][0]["role"] == "user"
    assert received_state["messages"][0]["content"]


def test_agent_state_accumulates_message_history():
    messages_type = get_type_hints(AgentState, include_extras=True)["messages"]

    assert operator.add in get_args(messages_type)


def test_noarg_tool_schema_requires_nonempty_reason():
    definitions = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in tool_registry_module.get_tool_definitions()
    }

    for tool_name in ("analyze_diff_tool", "clone_source_branch_tool", "commit_and_push_tool"):
        parameters = definitions[tool_name]
        assert parameters["required"] == ["reason"]
        assert parameters["properties"]["reason"]["type"] == "string"
        assert parameters["properties"]["reason"]["minLength"] == 1


def test_agent_llm_converts_tool_messages_to_openai_format(monkeypatch):
    captured = {}

    class Response:
        choices = [type("Choice", (), {"message": {"role": "assistant", "content": "done"}})()]

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)

    asyncio.run(
        llm_module.call_agent_llm(
            "system",
            [
                {"role": "user", "content": "start"},
                ToolMessage(content="result", tool_call_id="call-1"),
            ],
            [],
        )
    )

    assert captured["messages"][2] == {
        "role": "tool",
        "content": "result",
        "tool_call_id": "call-1",
    }


def test_agent_llm_uses_auto_tool_choice(monkeypatch):
    captured = {}

    class Response:
        choices = [type("Choice", (), {"message": {"role": "assistant", "content": "done"}})()]

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)

    asyncio.run(llm_module.call_agent_llm("system", [{"role": "user", "content": "start"}], []))

    assert captured["tool_choice"] == "auto"


def test_agent_llm_retries_malformed_tool_response_then_recovers(monkeypatch):
    attempts = 0

    async def fake_completion(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = type("Message", (), {"content": "现在克隆仓库", "tool_calls": []})()
        else:
            function = type("Function", (), {
                "name": "clone_source_branch_tool",
                "arguments": '{"reason": "查看源码"}',
            })()
            tool_call = type("ToolCall", (), {"function": function})()
            message = type("Message", (), {"content": "现在克隆仓库", "tool_calls": [tool_call]})()
        choice = type("Choice", (), {"message": message, "finish_reason": "tool_calls"})()
        return type("Response", (), {"id": f"response-{attempts}", "choices": [choice]})()

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)

    result = asyncio.run(llm_module.call_agent_llm("system", [], []))

    assert attempts == 2
    assert result.tool_calls[0].function.name == "clone_source_branch_tool"


def test_agent_llm_exhausts_fallbacks_after_malformed_tool_responses(monkeypatch):
    attempts = 0

    async def fake_completion(**_kwargs):
        nonlocal attempts
        attempts += 1
        message = type("Message", (), {"content": "现在克隆仓库", "tool_calls": []})()
        choice = type("Choice", (), {"message": message, "finish_reason": "tool_calls"})()
        return type("Response", (), {"id": f"response-{attempts}", "choices": [choice]})()

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)

    result = asyncio.run(llm_module.call_agent_llm("system", [], []))

    assert attempts == len(llm_module.MODEL_CANDIDATES) * 3
    assert result["content"].startswith(llm_module.MODEL_UNAVAILABLE_PREFIX)


def test_agent_llm_returns_plain_text_stop_without_protocol_retry(monkeypatch):
    attempts = 0

    async def fake_completion(**_kwargs):
        nonlocal attempts
        attempts += 1
        message = type("Message", (), {"content": "继续分析", "tool_calls": []})()
        choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
        return type("Response", (), {"id": "response-stop", "choices": [choice]})()

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)

    result = asyncio.run(llm_module.call_agent_llm("system", [], []))

    assert attempts == 1
    assert result.content == "继续分析"


def test_agent_protocol_error_is_preserved_in_result_and_finished_card():
    error = f"{llm_module.AGENT_LLM_PROTOCOL_ERROR_PREFIX}：中转响应缺少工具内容。"
    messages = [{"role": "assistant", "content": error}]

    result = agent_module._extract_result({"iteration": 3, "max_iterations": 30}, messages)
    card = agent_module._build_finished_card({}, messages, result, error)

    assert result["error"] == error
    assert "工具调用响应异常" in card


def test_agent_llm_retries_timeout_then_succeeds(monkeypatch):
    attempts = 0

    async def fake_completion(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("relay timed out")
        message = type("Message", (), {"content": "ok"})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)

    result = asyncio.run(llm_module.call_agent_llm("system", [], []))

    assert result.content == "ok"
    assert attempts == 2


def test_agent_llm_exhausts_fallbacks_after_transient_failures(monkeypatch):
    attempts = 0

    async def fake_completion(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("relay timed out")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)

    result = asyncio.run(llm_module.call_agent_llm("system", [], []))

    assert result["content"].startswith(llm_module.MODEL_UNAVAILABLE_PREFIX)
    assert attempts == len(llm_module.MODEL_CANDIDATES) * 3


def test_agent_retries_when_gateway_returns_plan_without_tool_call():
    state = {
        "messages": [{"role": "assistant", "content": "让我先查看流水线日志。"}],
        "iteration": 1,
        "max_iterations": 30,
    }

    assert agent_module.route_after_agent(state) == "agent"


def test_agent_node_returns_ai_message_for_tool_execution(monkeypatch):
    async def fake_call_agent_llm(**_kwargs):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": "fetch-1",
                "type": "function",
                "function": {"name": "fetch_pipeline_logs_tool", "arguments": "{}"},
            }],
        }

    monkeypatch.setattr(agent_module, "call_agent_llm", fake_call_agent_llm)

    result = asyncio.run(agent_module.agent_node({
        "messages": [{"role": "user", "content": "start"}],
        "iteration": 0,
        "max_iterations": 30,
    }))

    assert isinstance(result["messages"][0], AIMessage)
    assert result["messages"][0].tool_calls[0]["name"] == "fetch_pipeline_logs_tool"
    assert agent_module.route_after_agent({**result, "max_iterations": 30}) == "tools"


def test_agent_node_stops_immediately_when_all_model_routes_fail(monkeypatch):
    async def fake_call_agent_llm(**_kwargs):
        return LLMCallOutcome(
            response=None,
            model=None,
            attempts=(
                ModelAttempt("anthropic/claude-sonnet-5", "model_unavailable", "no distributor"),
                ModelAttempt("anthropic/claude-opus-4-8", "model_unavailable", "no distributor"),
                ModelAttempt("anthropic/claude-opus-4-6", "model_unavailable", "no distributor"),
            ),
            terminal_error="模型服务暂时不可用；已尝试模型：claude-sonnet-5、claude-opus-4-8、claude-opus-4-6。",
        )

    monkeypatch.setattr(agent_module, "call_agent_llm", fake_call_agent_llm)

    result = asyncio.run(agent_module.agent_node({
        "messages": [{"role": "user", "content": "start"}],
        "iteration": 0,
        "max_iterations": 30,
    }))

    assert result["iteration"] == 1
    assert result["model_terminal_error"].startswith("模型服务暂时不可用")
    assert result["model_terminal_failure_kind"] == "provider_unavailable"
    assert result["attempted_models"] == [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-4-6",
    ]
    assert agent_module.route_after_agent({**result, "max_iterations": 30}) == agent_module.END
    extracted = agent_module._extract_result(result, result["messages"])
    card = agent_module._build_finished_card(result, result["messages"], extracted, "")
    assert "模型服务暂时不可用" in card


def test_pipeline_failure_rejects_unsuccessful_finish_before_job_evidence(monkeypatch):
    async def fake_call_agent_llm(**_kwargs):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": "finish-1",
                "type": "function",
                "function": {
                    "name": "finish_tool",
                    "arguments": '{"success": false, "summary": "与当前 MR 无关"}',
                },
            }],
        }

    monkeypatch.setattr(agent_module, "call_agent_llm", fake_call_agent_llm)

    result = asyncio.run(agent_module.agent_node({
        "trigger_type": "pipeline_failed",
        "messages": [{"role": "user", "content": "start"}],
        "iteration": 27,
        "max_iterations": 30,
    }))

    assert result["messages"][0].tool_calls == []
    assert "逐个处理" in result["messages"][0].content
    assert "max_iterations" not in result


def test_pipeline_failure_rejects_generic_code_attempt_without_job_evidence(monkeypatch):
    async def fake_call_agent_llm(**_kwargs):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": "finish-1",
                "type": "function",
                "function": {
                    "name": "finish_tool",
                    "arguments": '{"success": false, "summary": "Hermes 无法修复"}',
                },
            }],
        }

    monkeypatch.setattr(agent_module, "call_agent_llm", fake_call_agent_llm)
    previous_attempt = AIMessage(
        content="",
        tool_calls=[{"id": "generate-1", "name": "generate_code_tool", "args": {}}],
    )

    result = asyncio.run(agent_module.agent_node({
        "trigger_type": "pipeline_failed",
        "messages": [previous_attempt],
        "iteration": 4,
        "max_iterations": 30,
    }))

    assert result["messages"][0].tool_calls == []
    assert "逐个处理" in result["messages"][0].content


def test_agent_stops_retrying_content_only_responses_at_iteration_limit():
    state = {
        "messages": [{"role": "assistant", "content": "让我先查看流水线日志。"}],
        "iteration": 30,
        "max_iterations": 30,
    }

    assert agent_module.route_after_agent(state) == agent_module.END


def test_finish_tool_is_executed_before_agent_stops():
    state = {
        "messages": [{
            "role": "assistant",
            "tool_calls": [{
                "id": "finish-1",
                "type": "function",
                "function": {"name": "finish_tool", "arguments": "{}"},
            }],
        }],
        "iteration": 3,
        "max_iterations": 30,
    }

    assert agent_module.route_after_agent(state) == "tools"


def test_tool_call_at_iteration_limit_is_executed():
    state = {
        "messages": [AIMessage(content="", tool_calls=[{
            "id": "repair-30",
            "name": "generate_code_tool",
            "args": {"operation": "repair", "job_name": "build_release_arm64"},
        }])],
        "iteration": 30,
        "max_iterations": 30,
    }

    assert agent_module.route_after_agent(state) == "tools"


def test_final_tool_result_does_not_start_iteration_31():
    state = {
        "messages": [ToolMessage(
            content=json.dumps({"status": "repair_no_changes", "failure_kind": "repair_no_changes"}),
            tool_call_id="repair-30",
        )],
        "iteration": 30,
        "max_iterations": 30,
    }

    assert agent_module.route_after_tools(state) == agent_module.END


def test_agent_stops_after_finish_tool_result():
    state = {
        "messages": [ToolMessage(
            content="FINISHED: success=True, summary=修复并验证完成",
            tool_call_id="finish-1",
        )],
        "iteration": 3,
        "max_iterations": 30,
    }

    assert agent_module.route_after_tools(state) == agent_module.END


def test_agent_stops_after_non_retryable_tool_result():
    state = {
        "messages": [ToolMessage(
            content=json.dumps({
                "status": "blocked",
                "retryable": False,
                "error_code": "remote_branch_changed",
                "message": "remote branch advanced",
            }),
            tool_call_id="push-1",
        )],
        "iteration": 9,
        "max_iterations": 30,
    }

    assert agent_module.route_after_tools(state) == agent_module.END


def test_ut_agent_rejects_unverified_success_content(monkeypatch, tmp_path):
    class Graph:
        async def ainvoke(self, _state, config=None):
            return {"messages": [ToolMessage(
                content="FINISHED: success=True, summary=修复并验证完成",
                tool_call_id="finish-1",
            )]}

    agent = agent_module.UTAgent.__new__(agent_module.UTAgent)
    agent.graph = Graph()
    monkeypatch.setattr(agent_module, "CONVERSATION_LOG", str(tmp_path / "conversation.log"))

    result = asyncio.run(agent.run({
        "trigger_type": "pipeline_failed",
        "mr_id": 518,
        "title": "Test",
        "source_branch": "feature",
        "target_branch": "master",
    }))

    assert result["response"].startswith("FINISHED: success=False")
    assert "修复并验证完成" not in result["response"]
    assert result["result"]["final_pipeline_status"] == "unknown"


def test_repository_tools_select_current_mr_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    wrong_repo = workspace / "mr_522" / "repo"
    current_repo = workspace / "mr_523" / "repo"
    for repo in (wrong_repo, current_repo):
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
    (wrong_repo / "src" / "main.cpp").write_text("wrong\n", encoding="utf-8")
    (current_repo / "src" / "main.cpp").write_text("current\n", encoding="utf-8")
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))

    result = read_repo_module.read_repo_file_tool.func(
        file_path="src/main.cpp",
        state={"mr_id": 523},
    )

    assert "current" in result
    assert "wrong" not in result


def test_workspace_identity_includes_project_and_mr(tmp_path):
    cook_key = context_module.workspace_key("eabot/cook", 448)
    huygens_key = context_module.workspace_key("eabot/huygens", 448)

    assert cook_key != huygens_key
    assert cook_key.endswith("_mr_448")
    assert huygens_key.endswith("_mr_448")
    assert context_module.workspace_path(str(tmp_path), "eabot/cook", 448, "repo") != (
        context_module.workspace_path(str(tmp_path), "eabot/huygens", 448, "repo")
    )


def test_repository_and_conversation_paths_are_project_scoped(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    provider = type("Provider", (), {"id_project": "eabot/huygens"})()
    repo = workspace / context_module.workspace_key(provider.id_project, 448) / "repo"
    (repo / ".git").mkdir(parents=True)
    token = context_module.init_context(provider, str(workspace))
    monkeypatch.setattr(agent_module, "CONVERSATION_LOG", str(workspace / "logs" / "conversation.log"))

    try:
        assert context_module.get_repo_dir(448) == str(repo)
        cook_log = agent_module._conversation_log_path({"project_id": "eabot/cook", "mr_id": 448})
        huygens_log = agent_module._conversation_log_path({"project_id": "eabot/huygens", "mr_id": 448})
        assert cook_log != huygens_log
    finally:
        context_module.reset_context(token)


def test_run_locks_are_shared_only_by_same_project_mr():
    cook = {"project_id": "eabot/cook", "mr_id": 448}
    huygens = {"project_id": "eabot/huygens", "mr_id": 448}

    assert agent_module._run_lock(cook) is agent_module._run_lock(cook)
    assert agent_module._run_lock(cook) is not agent_module._run_lock(huygens)


def test_read_repo_file_supports_targeted_line_window(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)
    source = repo / "main.cpp"
    source.write_text("".join(f"line {line}\n" for line in range(1, 201)), encoding="utf-8")
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))

    result = read_repo_module.read_repo_file_tool.func(
        file_path="main.cpp",
        start_line=138,
        max_lines=10,
        state={"mr_id": 523},
    )

    assert result.startswith("[FACT] 已读文件: main.cpp (L138-L147)\n[CONTENT]\nL138: line 138")
    assert "L147: line 147" in result
    assert "line 137" not in result
    assert len(result) < 2000


def test_read_repo_file_rejects_paths_outside_current_mr(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)
    (workspace / "secret.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))

    result = read_repo_module.read_repo_file_tool.func(
        file_path="../../secret.txt",
        state={"mr_id": 523},
    )

    assert result == "ERROR: 文件路径超出当前 MR 仓库"


def test_generate_code_returns_hermes_diagnosis_without_changes(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    monkeypatch.setattr(
        generate_code_module,
        "load_prompt",
        lambda _name: "{task_description} {repo_dir} {mr_id} {iteration}",
    )
    monkeypatch.setattr(
        generate_code_module,
        "_run_hermes",
        lambda repo_dir, _prompt, **_kwargs: generate_code_module.HermesRunOutcome(
            (), "诊断完成：trace_id 字段不存在"
        ),
    )

    result = generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description="诊断编译错误",
        languages=["cpp"],
        state={"mr_id": 523, "iteration": 4},
    )

    payload = json.loads(result)
    assert {
        key: payload[key]
        for key in ("status", "operation", "job_name", "changed_files", "diagnostic", "message")
    } == {
        "status": "no_changes",
        "operation": "generate",
        "job_name": "build_release_arm64",
        "changed_files": [],
        "diagnostic": "诊断完成：trace_id 字段不存在",
        "message": "Hermes 已完成诊断，但未修改文件。",
    }
    assert payload["root_cause_id"]
    assert payload["progress_fingerprint"]
    assert payload["hermes_duration_ms"] >= 0


def test_generate_code_retries_transient_hermes_failure_when_tree_is_unchanged(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    monkeypatch.setattr(
        generate_code_module,
        "load_prompt",
        lambda _name: "{task_description} {repo_dir} {mr_id} {iteration}",
    )
    monkeypatch.setattr(generate_code_module, "_get_changed_files", lambda _repo: [])
    sleep_calls = []
    monkeypatch.setattr(generate_code_module._time, "sleep", lambda seconds: sleep_calls.append(seconds))
    results = iter([
        generate_code_module._provider_error_outcome("API Error: Error code: 500 - internal server error"),
        generate_code_module.HermesRunOutcome(("src/main.cpp",), "fixed"),
    ])
    calls = 0

    def fake_run_hermes(_repo, _prompt, **_kwargs):
        nonlocal calls
        calls += 1
        return next(results)

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)

    result = generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description="修复编译错误",
        languages=["cpp"],
        state={"mr_id": 523, "iteration": 1},
    )

    assert json.loads(result)["status"] == "changed"
    assert json.loads(result)["changed_files"] == ["src/main.cpp"]
    assert calls == 2
    assert sleep_calls == [2]


def test_generate_code_switches_hermes_model_when_route_is_unavailable(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    monkeypatch.setattr(
        generate_code_module,
        "load_prompt",
        lambda _name: "{task_description} {repo_dir} {mr_id} {iteration}",
    )
    monkeypatch.setattr(generate_code_module, "_get_changed_files", lambda _repo: [])
    monkeypatch.setattr(generate_code_module, "MODEL_CANDIDATES", (
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-4-6",
    ))
    monkeypatch.setattr(
        generate_code_module,
        "_MODEL_HEALTH_STORE",
        ModelHealthStore(
            None,
            base_url="https://relay.example",
            cooldown_seconds=300,
            probe_lease_seconds=30,
        ),
    )
    attempted_models = []

    def fake_run_hermes(_repo, _prompt, *, model, **_kwargs):
        attempted_models.append(model)
        if model == "anthropic/claude-sonnet-5":
            return generate_code_module._provider_error_outcome("model_not_found: 无可用渠道（distributor）")
        return generate_code_module.HermesRunOutcome(("src/main.cpp",), "fixed")

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)

    result = generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description="修复编译错误",
        languages=["cpp"],
        state={"mr_id": 523, "iteration": 1},
    )
    payload = json.loads(result)

    assert attempted_models == ["anthropic/claude-sonnet-5", "anthropic/claude-opus-4-8"]
    assert payload["status"] == "changed"
    assert payload["model"] == "anthropic/claude-opus-4-8"
    assert payload["attempted_models"] == attempted_models
    assert payload["model_failover_count"] == 1


def test_generate_code_search_loop_does_not_try_fallback_model(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    monkeypatch.setattr(generate_code_module, "load_prompt", lambda _name: "{task_description}")
    monkeypatch.setattr(generate_code_module, "_get_changed_files", lambda _repo: [])
    monkeypatch.setattr(generate_code_module, "MODEL_CANDIDATES", (
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
    ))
    calls = []

    def fake_run_hermes(_repo, _prompt, *, model, **_kwargs):
        calls.append(model)
        return generate_code_module.HermesRunOutcome(
            (), "searched 96 files", generate_code_module.HermesFailureKind.SEARCH_LOOP
        )

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)
    result = json.loads(generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description="调查编译错误",
        operation="investigate",
        root_cause_id="root-1",
        state={"mr_id": 523, "iteration": 1, "trigger_type": "pipeline_failed"},
    ))

    assert calls == ["anthropic/claude-sonnet-5"]
    assert result["status"] == "investigation_timeout"
    assert result["failure_kind"] == "search_loop"
    assert result["model_failover_count"] == 0


def test_generate_code_does_not_retry_deterministic_hermes_api_error(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    monkeypatch.setattr(
        generate_code_module,
        "load_prompt",
        lambda _name: "{task_description} {repo_dir} {mr_id} {iteration}",
    )
    monkeypatch.setattr(generate_code_module, "_get_changed_files", lambda _repo: [])
    calls = 0

    def fake_run_hermes(_repo, _prompt, **_kwargs):
        nonlocal calls
        calls += 1
        return generate_code_module._provider_error_outcome(
            "API Error: Error code: 400 - invalid tool schema",
            diagnostic="API Error: Error code: 400",
        )

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)

    result = generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description="修复编译错误",
        languages=["cpp"],
        state={"mr_id": 523, "iteration": 1},
    )
    payload = json.loads(result)

    assert payload["status"] == "coding_infra_error"
    assert "400" in payload["message"]
    assert calls == 1


def test_generate_code_does_not_retry_after_partial_changes(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    monkeypatch.setattr(
        generate_code_module,
        "load_prompt",
        lambda _name: "{task_description} {repo_dir} {mr_id} {iteration}",
    )
    changed_files = iter([[], [str(repo / "src/main.cpp")]])
    monkeypatch.setattr(generate_code_module, "_get_changed_files", lambda _repo: next(changed_files))
    calls = 0

    def fake_run_hermes(_repo, _prompt, **_kwargs):
        nonlocal calls
        calls += 1
        return generate_code_module.HermesRunOutcome(
            (), "请求超时", generate_code_module.HermesFailureKind.EXECUTION_BUDGET_EXHAUSTED
        )

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)

    result = generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description="修复编译错误",
        languages=["cpp"],
        state={"mr_id": 523, "iteration": 1},
    )

    assert json.loads(result)["status"] == "partial_changes"
    assert json.loads(result)["changed_files"] == ["src/main.cpp"]
    assert calls == 1


def test_commit_push_selects_current_mr_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    for mr_id in (522, 523):
        (workspace / f"mr_{mr_id}" / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    repo_calls = []

    def fake_run_git(repo_dir, args, timeout=120):
        repo_calls.append((repo_dir, args, timeout))
        return ""

    monkeypatch.setattr(commit_push_module, "_run_git", fake_run_git)
    monkeypatch.setattr(commit_push_module, "_git_exit_code", lambda _repo, _args: (0, ""))

    result = commit_push_module.commit_and_push_tool.func(
        state={"mr_id": 523, "source_branch": "feature/fix"},
    )

    assert json.loads(result) == {
        "status": "no_changes",
        "changed": False,
        "commit_sha": None,
        "source_branch": "feature/fix",
        "message": "OK: 无新变更需要提交",
    }
    assert repo_calls[0][0] == str(workspace / "mr_523" / "repo")


def test_commit_boundary_rechecks_current_dependency_contract(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_549" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    monkeypatch.setattr(
        "ut_agent.workspace.validate_state_workspace",
        lambda *_args, **_kwargs: type("Validation", (), {"ok": True})(),
    )
    monkeypatch.setattr(commit_push_module, "_run_git", lambda _repo, _args, timeout=120: "")
    monkeypatch.setattr(commit_push_module, "_git_exit_code", lambda _repo, _args: (0, ""))
    contract = "int64 timestamp_ns\nuint32 command\nstring trace_id\nstring optional\n"
    captured = []

    def validate(_repo, evidence_sources=()):
        captured.append(list(evidence_sources))
        return True, ""

    monkeypatch.setattr(repair_safety_module, "validate_member_substitutions", validate)
    messages = _tool_exchange("resolve_dependency_evidence_tool", "dependency-549", {
        "status": "resolved",
        "root_cause_id": "root-549",
        "project_path": "eabot/lhotse",
        "declared_branch": "dev",
        "resolved_sha": "lhotse-current-sha",
        "file_path": "eabot_msgs/srv/RemoteControl.srv",
        "content_sha256": "contract-digest",
        "content": contract,
    }, {"job_name": "build_release_arm64", "root_cause_id": "root-549"})

    result = json.loads(commit_push_module.commit_and_push_tool.func(state={
        "mr_id": 549,
        "source_branch": "feature/fix",
        "trigger_type": "pipeline_failed",
        "messages": messages,
    }))

    assert result["status"] == "no_changes", result
    assert captured == [[contract]]


def test_commit_push_configures_local_identity_before_commit(monkeypatch, tmp_path):
    repo = tmp_path / "workspace" / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(tmp_path / "workspace"))
    calls = []
    git_state = {"head": "base", "remote": "base"}

    def fake_run_git(_repo_dir, args):
        calls.append(args)
        if args == ["diff", "--cached", "--binary", "--full-index", "--no-ext-diff"]:
            return "diff --git a/src/main.cpp b/src/main.cpp"
        if args == ["rev-parse", "HEAD"]:
            return git_state["head"]
        if args == ["rev-parse", "abc123^"]:
            return "base"
        if args == ["rev-parse", "abc123^{tree}"]:
            return "repair-tree"
        if args == ["rev-parse", "base^{tree}"]:
            return "base-tree"
        if args == ["ls-remote", "origin", "refs/heads/feature/fix"]:
            return f"{git_state['remote']}\trefs/heads/feature/fix"
        if args[:2] == ["commit", "-m"]:
            git_state["head"] = "abc123"
            return ""
        if args == ["push", "origin", "feature/fix"]:
            git_state["remote"] = git_state["head"]
            return ""
        if args == ["status", "--porcelain"]:
            return ""
        return ""

    monkeypatch.setattr(commit_push_module, "_run_git", fake_run_git)
    monkeypatch.setattr(commit_push_module, "_git_exit_code", lambda _repo, _args: (1, ""))

    result = commit_push_module.commit_and_push_tool.func(
        state={"mr_id": 523, "source_branch": "feature/fix"},
    )

    payload = json.loads(result)
    assert payload == {
        "status": "success",
        "changed": True,
        "commit_sha": "abc123",
        "source_branch": "feature/fix",
        "message": "OK: pushed to feature/fix, commit=abc123",
        "_facts": ["已推送 commit: abc123 到分支 feature/fix"],
        "attempt_id": payload["attempt_id"],
        "attempt_sequence": 1,
        "base_sha": "base",
        "diff_digest": payload["diff_digest"],
    }
    commit_call = next(call for call in calls if call[:2] == ["commit", "-m"])
    assert "[pr-agent-task:local-mr-523:push-attempt:1:" in commit_call[2]


def test_wait_pipeline_result_is_bound_to_requested_sha(monkeypatch):
    monkeypatch.setattr(
        fetch_pipeline_module,
        "fetch_pipeline_feedback",
        lambda _sha: {
            "status": "success",
            "pipeline_id": 28161,
            "pipeline_status": "success",
            "pipeline_sha": "old-sha",
            "failed_jobs": [],
            "message": "old pipeline passed",
        },
    )

    result = json.loads(fetch_pipeline_module.wait_pipeline_tool.func(commit_sha="new-sha", state={}))

    assert result["status"] == "error"
    assert result["requested_commit_sha"] == "new-sha"
    assert result["matched_commit_sha"] == "old-sha"
    assert "不匹配" in result["message"]


def _tool_exchange(name: str, call_id: str, result: dict, args: dict | None = None):
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}]),
        ToolMessage(content=json.dumps(result), tool_call_id=call_id),
    ]


def test_extract_result_reports_evidence_backed_verified_build_repair():
    root_cause_id = "missing-rslidar-msg"
    initial_pipeline = {
        "status": "success",
        "pipeline_id": 29414,
        "pipeline_status": "failed",
        "failed_jobs": [
            {
                "job_id": 41,
                "pipeline_id": 29414,
                "name": "build_release_arm64",
                "status": "failed",
                "causal_lines": ["CMake Error: Could not find rslidar_msgConfig.cmake"],
            }
        ],
        "work_items": [{
            "job_id": 41,
            "pipeline_id": 29414,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
            "root_cause_id": root_cause_id,
            "canonical_job_name": "build_release_arm64",
        }],
        "root_cause_groups": [{
            "root_cause_id": root_cause_id,
            "canonical_diagnostic": "CMake Error: Could not find rslidar_msgConfig.cmake",
            "canonical_job_name": "build_release_arm64",
            "job_names": ["build_release_arm64"],
        }],
    }
    messages = _tool_exchange("fetch_pipeline_logs_tool", "fetch-source", initial_pipeline)
    messages += _tool_exchange(
        "generate_code_tool",
        "repair-build",
        {
            "status": "changed",
            "operation": "repair",
            "job_name": "build_release_arm64",
            "root_cause_id": root_cause_id,
            "changed_files": ["CMakeLists.txt", "package.xml"],
            "repair_report": {
                "schema_version": 1,
                "root_cause_summary": "构建配置仍声明已删除且未使用的依赖。",
                "solution_summary": "从构建和包清单中移除未使用的 rslidar_msg 依赖。",
                "rationale": "源码未使用该包，移除残留声明后 CMake 不再查找不存在的依赖。",
                "file_explanations": [
                    {"path": "CMakeLists.txt", "summary": "移除构建依赖声明。"},
                    {"path": "package.xml", "summary": "移除包依赖声明。"},
                ],
            },
            "file_changes": [
                {"path": "CMakeLists.txt", "change_type": "modified", "additions": 0, "deletions": 3, "hunks": []},
                {"path": "package.xml", "change_type": "modified", "additions": 0, "deletions": 1, "hunks": []},
            ],
            "diagnostic": (
                "修复完成。\n"
                "- CMakeLists.txt：移除未使用的 rslidar_msg 构建依赖。\n"
                "- package.xml：移除对应依赖声明。"
            ),
            "message": "Hermes 已修改 2 个文件。",
        },
        {
            "operation": "repair",
            "job_name": "build_release_arm64",
            "root_cause_id": root_cause_id,
            "task_description": "根据当前根因执行最小安全修复",
        },
    )
    messages += _tool_exchange("commit_and_push_tool", "push-build", {
        "status": "success",
        "changed": True,
        "commit_sha": "f883827",
        "source_branch": "feature/fix",
        "message": "pushed",
    })
    messages += _tool_exchange("wait_pipeline_tool", "wait-build", {
        "status": "success",
        "requested_commit_sha": "f883827",
        "matched_commit_sha": "f883827",
        "root_pipeline_id": 29415,
        "validation_pipeline_id": 29415,
        "pipeline_id": 29415,
        "pipeline_status": "success",
        "coverage": 63.04,
        "coverage_source": "changed_lines",
        "coverage_status": "reported",
        "failed_jobs": [],
    })

    result = agent_module._extract_result({"iteration": 5}, messages)
    action = result["repair_actions"][0]

    assert action["categories"] == ["build"]
    assert action["root_cause"] == "CMake Error: Could not find rslidar_msgConfig.cmake"
    assert action["changed_files"] == ["CMakeLists.txt", "package.xml"]
    assert action["measures"] == []
    assert action["solution_summary"] == "从构建和包清单中移除未使用的 rslidar_msg 依赖。"
    assert action["rationale"] == "源码未使用该包，移除残留声明后 CMake 不再查找不存在的依赖。"
    assert [item["path"] for item in action["file_changes"]] == ["CMakeLists.txt", "package.xml"]
    assert action["file_changes"][0]["summary"] == "移除构建依赖声明。"
    assert action["commit_sha"] == "f883827"
    assert action["validation_pipeline_id"] == 29415
    assert action["status"] == "verified"
    assert result["final_coverage"] == 63.04
    assert result["coverage_source"] == "changed_lines"
    assert result["coverage_status"] == "reported"
    assert result["pipeline_groups"][-1]["coverage_source"] == "changed_lines"


def test_extract_result_reports_honest_no_change_action():
    root_cause_id = "build-root"
    messages = _tool_exchange("fetch_pipeline_logs_tool", "fetch-no-change", {
        "status": "success",
        "pipeline_id": 30001,
        "pipeline_status": "failed",
        "failed_jobs": [{"name": "build_release_arm64", "status": "failed"}],
        "root_cause_groups": [{
            "root_cause_id": root_cause_id,
            "canonical_diagnostic": "undefined reference to sensor_start",
            "canonical_job_name": "build_release_arm64",
            "job_names": ["build_release_arm64"],
        }],
    })
    messages += _tool_exchange("generate_code_tool", "repair-no-change", {
        "status": "repair_no_changes",
        "operation": "repair",
        "job_name": "build_release_arm64",
        "root_cause_id": root_cause_id,
        "changed_files": [],
        "diagnostic": "检查了调用点，但没有产生代码修改。",
        "message": "Hermes 已尝试修复，但未产生仓库修改。",
    }, {
        "operation": "repair",
        "job_name": "build_release_arm64",
        "root_cause_id": root_cause_id,
        "task_description": "根据当前根因执行最小安全修复",
    })

    result = agent_module._extract_result({"iteration": 4}, messages)
    action = result["repair_actions"][0]

    assert action["root_cause"] == "undefined reference to sensor_start"
    assert action["changed_files"] == []
    assert action["commit_sha"] == ""
    assert action["status"] == "no_changes"


def test_repair_action_is_verified_when_only_another_category_still_fails():
    root_cause_id = "build-fixed"
    messages = _tool_exchange("fetch_pipeline_logs_tool", "fetch-build", {
        "status": "success",
        "pipeline_id": 31000,
        "pipeline_status": "failed",
        "failed_jobs": [{"name": "build_release_arm64", "status": "failed"}],
        "root_cause_groups": [{
            "root_cause_id": root_cause_id,
            "canonical_diagnostic": "missing dependency",
            "job_names": ["build_release_arm64"],
        }],
    })
    messages += _tool_exchange("generate_code_tool", "repair-build", {
        "status": "changed",
        "operation": "repair",
        "job_name": "build_release_arm64",
        "root_cause_id": root_cause_id,
        "changed_files": ["CMakeLists.txt"],
        "diagnostic": "CMakeLists.txt：移除错误依赖。",
    }, {
        "operation": "repair",
        "job_name": "build_release_arm64",
        "root_cause_id": root_cause_id,
    })
    messages += _tool_exchange("commit_and_push_tool", "push-build", {
        "status": "success",
        "changed": True,
        "commit_sha": "fixed-build-sha",
    })
    messages += _tool_exchange("wait_pipeline_tool", "wait-format", {
        "status": "success",
        "requested_commit_sha": "fixed-build-sha",
        "matched_commit_sha": "fixed-build-sha",
        "pipeline_id": 31001,
        "validation_pipeline_id": 31001,
        "pipeline_status": "failed",
        "failed_jobs": [{"name": "code_format_check", "status": "failed"}],
    })

    result = agent_module._extract_result({"iteration": 4}, messages)

    assert result["repair_actions"][0]["status"] == "verified"
    assert result["repair_actions"][0]["validation_pipeline_id"] == 31001


def _blocker_record(job_name: str) -> dict:
    return {
        "schema_version": 1,
        "outcome": "blocked",
        "job_name": job_name,
        "blocker_type": "external_dependency",
        "root_cause": "Required dependency is absent from the CI image and directly used by compiled source.",
        "ci_evidence": [{"job_name": job_name, "observation": "Compiler reports the dependency is missing."}],
        "repository_evidence": [{
            "kind": "source_reference",
            "locator": "src/main.cpp:12",
            "observation": "Compiled source directly uses symbols from the dependency.",
        }],
        "attempted_repairs": ["Checked vendored sources and a repository-local fallback."],
        "why_no_safe_repo_change": "Removing the dependency leaves required source symbols undefined.",
        "suggested_action": "在 CI 环境中安装所需依赖后重试流水线。",
    }


def _blocker_resolution_exchanges(job_name: str, call_prefix: str) -> list:
    repair_args = {
        "job_name": job_name,
        "operation": "repair",
        "task_description": "尝试最小安全修复",
    }
    messages = _tool_exchange(
        "generate_code_tool",
        f"{call_prefix}-repair",
        {
            "status": "repair_no_changes",
            "operation": "repair",
            "job_name": job_name,
            "changed_files": [],
            "diagnostic": "No safe repository edit was produced after checking current source and build evidence.",
            "message": "repair completed without changes",
        },
        repair_args,
    )
    messages += _tool_exchange(
        "generate_code_tool",
        f"{call_prefix}-verify",
        {
            "status": "blocked",
            "operation": "verify_blocker",
            "job_name": job_name,
            "changed_files": [],
            "diagnostic": "structured blocker verified",
            "message": "blocked",
            "blocker": _blocker_record(job_name),
        },
        {
            "job_name": job_name,
            "operation": "verify_blocker",
            "task_description": "验证结构化阻塞证据",
        },
    )
    return messages


def _verified_finish_messages(pipeline_status: str, failed_jobs=None):
    messages = _tool_exchange(
        "commit_and_push_tool",
        "push",
        {
            "status": "success",
            "changed": True,
            "commit_sha": "new-sha",
            "source_branch": "feature",
            "message": "pushed",
        },
    )
    messages += _tool_exchange(
        "wait_pipeline_tool",
        "wait",
        {
            "status": "success",
            "requested_commit_sha": "new-sha",
            "matched_commit_sha": "new-sha",
            "pipeline_id": 28177,
            "pipeline_status": pipeline_status,
            "failed_jobs": failed_jobs or [],
            "message": pipeline_status,
        },
    )
    return messages


def test_unpushed_fix_keeps_confirmed_pipeline_truth_and_reports_no_new_pipeline():
    messages = _tool_exchange(
        "fetch_pipeline_logs_tool",
        "original-pipeline",
        {
            "status": "success",
            "requested_commit_sha": "original-sha",
            "matched_commit_sha": "original-sha",
            "pipeline_id": 29907,
            "pipeline_status": "failed",
            "failed_jobs": [{"name": "build_release_arm64", "status": "failed"}],
            "work_items": [{"job_name": "build_release_arm64", "kind": "build"}],
            "message": "failed",
        },
    )
    messages += _tool_exchange(
        "commit_and_push_tool",
        "push-conflict",
        {
            "status": "blocked",
            "changed": True,
            "commit_sha": "local-only-sha",
            "source_branch": "feature/fix",
            "error_code": "remote_branch_changed",
            "retryable": False,
            "message": "远端分支已变化，拒绝覆盖",
        },
    )
    messages += _tool_exchange(
        "fetch_pipeline_logs_tool",
        "local-only-lookup",
        {
            "status": "error",
            "requested_commit_sha": "local-only-sha",
            "matched_commit_sha": None,
            "message": "找不到该 commit 对应的流水线",
        },
    )

    result = agent_module._extract_result({"iteration": 9, "max_iterations": 30}, messages)
    card = agent_module._build_finished_card({}, messages, result, "")

    assert result["pushed_sha"] is None
    assert result["result_pipeline_id"] == 0
    assert result["final_pipeline_status"] == "unknown"
    assert result["pipeline_groups"][-1]["validation_pipeline_id"] == 29907
    assert result["finish_reason"] == "remote_branch_changed"
    assert "修复提交未能推送" in card
    assert "没有创建新的验证流水线" in card
    assert "Pipeline: error" not in card


def test_result_ignores_pipeline_from_wrong_matched_sha():
    messages = _tool_exchange(
        "commit_and_push_tool",
        "push-new",
        {
            "status": "success",
            "changed": True,
            "commit_sha": "new-sha",
            "source_branch": "feature/fix",
            "message": "pushed",
        },
    )
    messages += _tool_exchange(
        "wait_pipeline_tool",
        "mismatched-pipeline",
        {
            "status": "error",
            "requested_commit_sha": "new-sha",
            "matched_commit_sha": "old-sha",
            "pipeline_id": 29906,
            "pipeline_status": "success",
            "failed_jobs": [],
            "message": "流水线 SHA 不匹配",
        },
    )

    result = agent_module._extract_result({"iteration": 4, "max_iterations": 30}, messages)

    assert result["pushed_sha"] == "new-sha"
    assert result["final_pipeline_status"] == "unknown"
    assert result["success"] == 0


def test_finish_success_rejects_failed_pipeline_for_last_pushed_sha():
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = _verified_finish_messages(
        "failed",
        [{"name": "build_release_arm64", "log_tail": "Could not find eabot_cmake"}],
    )

    accepted, reason = policy.validate_finish({"messages": messages}, {"success": True})

    assert accepted is False
    assert "new-sha" in reason
    assert "failed" in reason


def test_execution_ledger_records_job_specific_tool_args_and_results():
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = _tool_exchange(
        "generate_code_tool",
        "generate-build",
        {
            "status": "no_changes",
            "job_name": "build_release_arm64",
            "changed_files": [],
            "diagnostic": "dependency source inspected",
        },
        {"job_name": "build_release_arm64", "task_description": "inspect build"},
    )

    ledger = policy.build_execution_ledger(messages)

    assert len(ledger.tool_attempts) == 1
    assert ledger.tool_attempts[0].name == "generate_code_tool"
    assert ledger.tool_attempts[0].args["job_name"] == "build_release_arm64"
    assert ledger.tool_attempts[0].result["status"] == "no_changes"


def test_execution_ledger_deduplicates_push_attempts_and_uses_highest_sequence():
    policy = importlib.import_module("ut_agent.execution_policy")
    first = {
        "status": "success",
        "changed": True,
        "commit_sha": "sha-1",
        "attempt_id": "task:1:base:diff-1",
        "attempt_sequence": 1,
    }
    second = {
        "status": "success",
        "changed": True,
        "commit_sha": "sha-2",
        "attempt_id": "task:2:sha-1:diff-2",
        "attempt_sequence": 2,
    }
    messages = _tool_exchange("commit_and_push_tool", "push-1", first)
    messages += _tool_exchange("commit_and_push_tool", "push-2", second)
    messages += _tool_exchange("commit_and_push_tool", "push-1-replay", first)

    ledger = policy.build_execution_ledger(messages)
    result = agent_module._extract_result({"iteration": 5}, messages)

    assert len(ledger.pushes) == 2
    assert ledger.last_pushed_sha == "sha-2"
    assert [push["attempt_sequence"] for push in result["push_attempts"]] == [1, 2]


def test_finish_failure_requires_prescribed_action_for_every_work_item():
    policy = importlib.import_module("ut_agent.execution_policy")
    pipeline_result = {
        "status": "success",
        "pipeline_id": 28177,
        "pipeline_status": "failed",
        "failed_jobs": [
            {"job_id": 12, "pipeline_id": 28177, "name": "code_format_check", "status": "failed"},
            {"job_id": 13, "pipeline_id": 28177, "name": "mr_merge_commit_check", "status": "failed"},
        ],
        "work_items": [
            {
                "job_id": 12,
                "pipeline_id": 28177,
                "job_name": "code_format_check",
                "kind": "format",
                "required_tool": "apply_format_report_tool",
            },
            {
                "job_id": 13,
                "pipeline_id": 28177,
                "job_name": "mr_merge_commit_check",
                "kind": "merge_check",
                "required_tool": "generate_code_tool",
            },
        ],
    }
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline", pipeline_result)
    messages += _tool_exchange(
        "generate_code_tool",
        "wrong-format-attempt",
        {
            "status": "no_changes",
            "job_name": "code_format_check",
            "changed_files": [],
            "diagnostic": "clang-format is not installed",
        },
        {"job_name": "code_format_check", "task_description": "try format"},
    )
    messages += _tool_exchange(
        "generate_code_tool",
        "merge-attempt",
        {
            "status": "no_changes",
            "job_name": "mr_merge_commit_check",
            "changed_files": [],
            "diagnostic": "merge commit policy rejects the current parent count",
        },
        {"job_name": "mr_merge_commit_check", "task_description": "inspect merge check"},
    )

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "无法自动修复"},
    )

    assert accepted is False
    assert "code_format_check" in reason
    assert "apply_format_report_tool" in reason


def test_finish_failure_requires_coverage_remediation_when_report_is_available():
    policy = importlib.import_module("ut_agent.execution_policy")
    work_item = {
        "job_id": 14,
        "pipeline_id": 28177,
        "job_name": "x86_64_ut_coverage_check",
        "kind": "coverage",
        "required_tool": "fetch_coverage_report_tool",
    }
    messages = _tool_exchange(
        "fetch_pipeline_logs_tool",
        "pipeline",
        {
            "status": "success",
            "pipeline_id": 28177,
            "pipeline_status": "failed",
            "failed_jobs": [{"job_id": 14, "name": work_item["job_name"], "status": "failed"}],
            "work_items": [work_item],
        },
    )
    messages += _tool_exchange(
        "fetch_coverage_report_tool",
        "coverage",
        {"status": "success", "available": True, "files": [{"path": "src/a.cpp", "uncovered": [{"line": 3}]}]},
        {"job_id": 14},
    )

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "覆盖率不足"},
    )

    assert accepted is False
    assert "generate_code_tool" in reason
    assert work_item["job_name"] in reason


def test_finish_failure_accepts_complete_job_specific_blockers():
    policy = importlib.import_module("ut_agent.execution_policy")
    work_items = [
        {
            "job_id": 12,
            "pipeline_id": 28177,
            "job_name": "code_format_check",
            "kind": "format",
            "required_tool": "apply_format_report_tool",
        },
        {
            "job_id": 14,
            "pipeline_id": 28177,
            "job_name": "x86_64_ut_coverage_check",
            "kind": "coverage",
            "required_tool": "fetch_coverage_report_tool",
        },
        {
            "job_id": 11,
            "pipeline_id": 28177,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
        },
    ]
    messages = _tool_exchange(
        "fetch_pipeline_logs_tool",
        "pipeline",
        {
            "status": "success",
            "pipeline_id": 28177,
            "pipeline_status": "failed",
            "failed_jobs": [{"job_id": item["job_id"], "name": item["job_name"]} for item in work_items],
            "work_items": work_items,
        },
    )
    messages += _tool_exchange(
        "apply_format_report_tool",
        "format",
        {"status": "blocked", "job_id": 12, "job_name": "code_format_check", "changed_files": []},
        {"pipeline_id": 28177, "job_id": 12, "job_name": "code_format_check"},
    )
    messages += _tool_exchange(
        "fetch_coverage_report_tool",
        "coverage",
        {"status": "unknown", "available": False, "reason": "artifact 404"},
        {"job_id": 14},
    )
    messages += _blocker_resolution_exchanges("build_release_arm64", "complete-build")

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "已逐项取证，格式报告与覆盖率报告均不可用，构建依赖来源已检查。"},
    )

    assert accepted is True
    assert reason == ""


def test_failed_finish_validates_one_canonical_action_for_shared_root_cause():
    policy = importlib.import_module("ut_agent.execution_policy")
    work_items = [
        {
            "job_id": 11,
            "pipeline_id": 28177,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
            "root_cause_id": "same-compiler-error",
            "canonical_job_name": "build_release_arm64",
        },
        {
            "job_id": 14,
            "pipeline_id": 28177,
            "job_name": "x86_64_ut_coverage_check",
            "kind": "coverage",
            "required_tool": "fetch_coverage_report_tool",
            "root_cause_id": "same-compiler-error",
            "canonical_job_name": "build_release_arm64",
        },
    ]
    messages = _tool_exchange(
        "fetch_pipeline_logs_tool",
        "pipeline-shared-root",
        {
            "status": "success",
            "pipeline_id": 28177,
            "pipeline_status": "failed",
            "failed_jobs": [{"job_id": item["job_id"], "name": item["job_name"]} for item in work_items],
            "work_items": work_items,
        },
    )
    messages += _blocker_resolution_exchanges("build_release_arm64", "shared-root")

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "同一编译根因已经完成修复尝试和阻塞验证。"},
    )

    assert accepted is True
    assert reason == ""


def test_committed_repair_followed_by_exact_failed_pipeline_is_a_completed_attempt():
    policy = importlib.import_module("ut_agent.execution_policy")
    work_item = {
        "job_id": 11,
        "pipeline_id": 28177,
        "job_name": "build_release_arm64",
        "kind": "build",
        "required_tool": "generate_code_tool",
        "root_cause_id": "same-compiler-error",
        "canonical_job_name": "build_release_arm64",
    }
    messages = _tool_exchange(
        "fetch_pipeline_logs_tool",
        "source-pipeline",
        {
            "status": "success",
            "requested_commit_sha": "source-sha",
            "matched_commit_sha": "source-sha",
            "pipeline_id": 28177,
            "pipeline_status": "failed",
            "failed_jobs": [{"job_id": 11, "name": "build_release_arm64"}],
            "work_items": [work_item],
        },
    )
    messages += _tool_exchange(
        "generate_code_tool",
        "repair-change",
        {
            "status": "changed",
            "operation": "repair",
            "job_name": "build_release_arm64",
            "root_cause_id": "same-compiler-error",
            "changed_files": ["src/handler.cpp"],
            "diagnostic": "Applied the evidence-backed member access repair.",
        },
        {
            "job_name": "build_release_arm64",
            "operation": "repair",
            "root_cause_id": "same-compiler-error",
        },
    )
    messages += _tool_exchange(
        "commit_and_push_tool",
        "repair-push",
        {
            "status": "success",
            "changed": True,
            "commit_sha": "repair-sha",
            "attempt_id": "task:1:source:diff",
        },
    )
    messages += _tool_exchange(
        "wait_pipeline_tool",
        "repair-pipeline",
        {
            "status": "success",
            "requested_commit_sha": "repair-sha",
            "matched_commit_sha": "repair-sha",
            "pipeline_id": 28188,
            "pipeline_status": "failed",
            "failed_jobs": [{"job_id": 21, "name": "build_release_arm64"}],
            "work_items": [{**work_item, "job_id": 21, "pipeline_id": 28188}],
        },
    )

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "修复已提交并由精确 SHA 流水线验证，根因仍然存在。"},
    )

    assert accepted is True
    assert reason == ""


def test_finish_failure_rejects_unresolved_placeholder_summary():
    policy = importlib.import_module("ut_agent.execution_policy")
    accepted, reason = policy.validate_finish(
        {
            "trigger_type": "pipeline_failed",
            "messages": _tool_exchange(
                "fetch_pipeline_logs_tool",
                "pipeline",
                {
                    "status": "success",
                    "pipeline_id": 28177,
                    "pipeline_status": "failed",
                    "failed_jobs": [],
                    "work_items": [],
                },
            ),
        },
        {"success": False, "summary": "mr_merge_commit_check 需要查看具体原因"},
    )

    assert accepted is False
    assert "未完成表述" in reason


def test_pipeline_failed_trigger_uses_repair_prompt_not_ut_prompt(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_300" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    prompts = {
        "generate_patch_system": "UT-PROMPT 单元测试工程师",
        "generate_patch_cpp": "UT-CPP",
        "generate_patch_python": "UT-PY",
        "generate_patch_user": "{task_description} {repo_dir} {mr_id} {iteration}",
        "generate_fix_system": "REPAIR-PROMPT 禁止创建测试文件",
        "generate_fix_user": "FIX-USER {task_description} {repo_dir} {mr_id} {iteration}",
    }
    monkeypatch.setattr(generate_code_module, "load_prompt", lambda name: prompts[name])
    captured = {}

    def fake_run_hermes(_repo, prompt, hide_git_metadata=False, **_kwargs):
        captured["prompt"] = prompt
        captured["hide_git_metadata"] = hide_git_metadata
        return generate_code_module.HermesRunOutcome((), "调查完成：eabot_cmake 为外部依赖，仓库内无来源")

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)

    result = generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description="调查 eabot_cmake 缺失",
        operation="repair",
        languages=["cpp"],
        state={"mr_id": 300, "iteration": 1, "trigger_type": "pipeline_failed"},
    )

    assert json.loads(result)["status"] == "repair_no_changes"
    assert "REPAIR-PROMPT" in captured["prompt"]
    assert "FIX-USER" in captured["prompt"]
    assert "UT-PROMPT" not in captured["prompt"]
    assert "UT-CPP" not in captured["prompt"]
    assert captured["hide_git_metadata"] is True


def test_finish_failure_evidence_survives_requery_of_same_pipeline():
    policy = importlib.import_module("ut_agent.execution_policy")
    pipeline_result = {
        "status": "success",
        "pipeline_id": 28177,
        "pipeline_status": "failed",
        "failed_jobs": [{"job_id": 11, "name": "build_release_arm64", "status": "failed"}],
        "work_items": [{
            "job_id": 11,
            "pipeline_id": 28177,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
        }],
    }
    messages = _tool_exchange("fetch_pipeline_logs_tool", "first-query", pipeline_result)
    messages += _blocker_resolution_exchanges("build_release_arm64", "requery")
    # 重新查询同一条流水线不应清零已完成的取证
    messages += _tool_exchange("fetch_pipeline_logs_tool", "second-query", pipeline_result)

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "外部依赖 eabot_cmake 缺失，属 CI 环境问题。"},
    )

    assert accepted is True
    assert reason == ""


def test_finish_failure_requires_new_repair_after_discarded_junk_changes():
    policy = importlib.import_module("ut_agent.execution_policy")
    pipeline_result = {
        "status": "success",
        "pipeline_id": 28177,
        "pipeline_status": "failed",
        "failed_jobs": [{"job_id": 11, "name": "build_release_arm64", "status": "failed"}],
        "work_items": [{
            "job_id": 11,
            "pipeline_id": 28177,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
        }],
    }
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline", pipeline_result)
    messages += _tool_exchange(
        "generate_code_tool",
        "junk-generation",
        {
            "status": "changed",
            "operation": "repair",
            "job_name": "build_release_arm64",
            "changed_files": ["junk_test_plan.md"],
            "diagnostic": "investigation confirmed eabot_cmake is an external dependency missing from CI",
        },
        {"job_name": "build_release_arm64", "operation": "repair", "task_description": "fix"},
    )
    messages += _tool_exchange(
        "discard_workspace_tool",
        "discard-junk",
        {
            "status": "success",
            "discarded_files": ["junk_test_plan.md"],
            "message": "已丢弃与修复任务无关的工作区修改",
        },
        {"reason": "Hermes 生成了与任务无关的文件"},
    )

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "外部依赖 eabot_cmake 缺失，垃圾修改已丢弃。"},
    )

    assert accepted is False
    assert 'operation="repair"' in reason


def test_finish_failure_still_forces_action_when_changes_not_discarded():
    policy = importlib.import_module("ut_agent.execution_policy")
    pipeline_result = {
        "status": "success",
        "pipeline_id": 28177,
        "pipeline_status": "failed",
        "failed_jobs": [{"job_id": 11, "name": "build_release_arm64", "status": "failed"}],
        "work_items": [{
            "job_id": 11,
            "pipeline_id": 28177,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
        }],
    }
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline", pipeline_result)
    messages += _tool_exchange(
        "generate_code_tool",
        "generation",
        {
            "status": "changed",
            "operation": "repair",
            "job_name": "build_release_arm64",
            "changed_files": ["src/fix.cpp"],
            "diagnostic": "fixed the include path for the failing translation unit",
        },
        {"job_name": "build_release_arm64", "operation": "repair", "task_description": "fix"},
    )

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "改了但不想提交。"},
    )

    assert accepted is False
    assert "提交并验证" in reason
    assert "discard_workspace_tool" in reason


def test_finish_failure_accepts_dependency_not_found_diagnostic():
    """依赖包缺失只有完成 repair + verify_blocker 后才能结束。"""
    policy = importlib.import_module("ut_agent.execution_policy")
    pipeline_result = {
        "status": "success",
        "pipeline_id": 28177,
        "pipeline_status": "failed",
        "failed_jobs": [{"job_id": 11, "name": "build_release_arm64", "status": "failed"}],
        "work_items": [{
            "job_id": 11,
            "pipeline_id": 28177,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
        }],
    }
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline", pipeline_result)
    messages += _blocker_resolution_exchanges("build_release_arm64", "dependency")

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "eabot_cmake 为外部依赖，CI 环境缺失。"},
    )

    assert accepted is True
    assert reason == ""


def test_finish_failure_still_rejects_local_tool_missing_excuse():
    policy = importlib.import_module("ut_agent.execution_policy")
    pipeline_result = {
        "status": "success",
        "pipeline_id": 28177,
        "pipeline_status": "failed",
        "failed_jobs": [{"job_id": 12, "name": "some_check", "status": "failed"}],
        "work_items": [{
            "job_id": 12,
            "pipeline_id": 28177,
            "job_name": "some_check",
            "kind": "other",
            "required_tool": "generate_code_tool",
        }],
    }
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline", pipeline_result)
    messages += _tool_exchange(
        "generate_code_tool",
        "excuse",
        {
            "status": "coding_infra_error",
            "operation": "repair",
            "job_name": "some_check",
            "changed_files": [],
            "diagnostic": "clang-format-18 is not installed in this environment and cannot proceed",
            "message": "local tool missing",
        },
        {"job_name": "some_check", "operation": "repair", "task_description": "fix"},
    )

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "工具缺失。"},
    )

    assert accepted is False
    assert "repair 操作未完成" in reason


def test_failure_signature_counts_parent_and_child_pipeline_once():
    """父流水线和 downstream 子流水线共享同一 SHA，只能算一次失败观察。"""
    policy = importlib.import_module("ut_agent.execution_policy")
    failed_jobs = [{
        "job_id": 11,
        "name": "build_release_arm64",
        "status": "failed",
        "log_tail": "Could not find eabot_cmake",
    }]
    messages = _tool_exchange("fetch_pipeline_logs_tool", "parent", {
        "status": "success",
        "matched_commit_sha": "same-sha",
        "pipeline_id": 28177,
        "pipeline_status": "failed",
        "failed_jobs": failed_jobs,
        "message": "failed",
    })
    messages += _tool_exchange("fetch_pipeline_logs_tool", "child", {
        "status": "success",
        "matched_commit_sha": "same-sha",
        "pipeline_id": 28179,
        "pipeline_status": "failed",
        "failed_jobs": failed_jobs,
        "message": "failed",
    })

    allowed, reason = policy.validate_tool_call({"messages": messages}, "generate_code_tool")

    assert allowed is True
    assert reason == ""


def test_repeated_pipeline_observation_does_not_force_global_finish(monkeypatch):
    """仅重复观察相似失败不能跳过实际修复链并强制结束整个任务。"""
    async def fake_call_agent_llm(**_kwargs):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": "generate",
                "type": "function",
                "function": {
                    "name": "generate_code_tool",
                    "arguments": '{"job_name": "build_release_arm64", "task_description": "again"}',
                },
            }],
        }

    # 两个不同 SHA 的流水线出现相同失败签名，但没有对应的修改、推送和精确 SHA 验证链。
    messages = []
    for index in range(2):
        messages += _tool_exchange(
            "wait_pipeline_tool",
            f"failed-{index}",
            {
                "status": "success",
                "requested_commit_sha": f"sha-{index}",
                "matched_commit_sha": f"sha-{index}",
                "pipeline_id": 300 + index,
                "pipeline_status": "failed",
                "failed_jobs": [{
                    "job_id": 11,
                    "name": "build_release_arm64",
                    "status": "failed",
                    "log_tail": "Could not find eabot_cmake",
                }],
                "work_items": [{
                    "job_id": 11,
                    "pipeline_id": 300 + index,
                    "job_name": "build_release_arm64",
                    "kind": "build",
                    "required_tool": "generate_code_tool",
                }],
                "message": "failed",
            },
        )
    monkeypatch.setattr(agent_module, "call_agent_llm", fake_call_agent_llm)

    result = asyncio.run(agent_module.agent_node({
        "trigger_type": "pipeline_failed",
        "messages": messages,
        "iteration": 10,
        "max_iterations": 30,
    }))

    next_call = result["messages"][0].tool_calls[0]
    assert next_call["name"] == "clone_source_branch_tool"


def test_failed_finish_rejection_lists_all_missing_actions_with_exact_args():
    policy = importlib.import_module("ut_agent.execution_policy")
    work_items = [
        {
            "job_id": 12,
            "pipeline_id": 28229,
            "job_name": "code_format_check",
            "kind": "format",
            "required_tool": "apply_format_report_tool",
        },
        {
            "job_id": 14,
            "pipeline_id": 28229,
            "job_name": "x86_64_ut_coverage_check",
            "kind": "coverage",
            "required_tool": "fetch_coverage_report_tool",
        },
    ]
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline", {
        "status": "success",
        "pipeline_id": 28229,
        "pipeline_status": "failed",
        "failed_jobs": [{"job_id": item["job_id"], "name": item["job_name"]} for item in work_items],
        "work_items": work_items,
    })

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "环境问题。"},
    )

    assert accepted is False
    # 一次性列出全部缺口和精确参数，模型不需要逐项试错
    assert "apply_format_report_tool" in reason and "job_id=12" in reason
    assert "fetch_coverage_report_tool" in reason and "job_id=14" in reason


def test_finish_failure_allows_empty_work_items_with_pipeline_evidence():
    """流水线失败但没有任何 failed job（如全部 canceled）时不能死锁。"""
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline", {
        "status": "success",
        "pipeline_id": 28229,
        "pipeline_status": "failed",
        "failed_jobs": [],
        "work_items": [],
    })

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "流水线整体失败但无失败 job，疑似被取消。"},
    )

    assert accepted is True
    assert reason == ""


def test_finish_failure_evidence_survives_parent_child_pipeline_switch():
    """先查父流水线、取证、再查同 SHA 的子流水线，取证不能被清零。"""
    policy = importlib.import_module("ut_agent.execution_policy")

    def pipeline_result(pipeline_id):
        return {
            "status": "success",
            "matched_commit_sha": "same-sha",
            "pipeline_id": pipeline_id,
            "pipeline_status": "failed",
            "failed_jobs": [{"job_id": 11, "name": "build_release_arm64", "status": "failed"}],
            "work_items": [{
                "job_id": 11,
                "pipeline_id": pipeline_id,
                "job_name": "build_release_arm64",
                "kind": "build",
                "required_tool": "generate_code_tool",
            }],
        }

    messages = _tool_exchange("fetch_pipeline_logs_tool", "parent", pipeline_result(28177))
    messages += _blocker_resolution_exchanges("build_release_arm64", "parent-child")
    messages += _tool_exchange("fetch_pipeline_logs_tool", "child", pipeline_result(28179))

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "外部依赖缺失，属 CI 环境问题。"},
    )

    assert accepted is True
    assert reason == ""


def test_finish_success_ignores_old_successful_pipeline():
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = _verified_finish_messages("failed")
    messages += _tool_exchange(
        "fetch_pipeline_logs_tool",
        "old",
        {
            "status": "success",
            "requested_commit_sha": "old-sha",
            "matched_commit_sha": "old-sha",
            "pipeline_id": 28161,
            "pipeline_status": "success",
            "failed_jobs": [],
            "message": "old success",
        },
    )

    accepted, reason = policy.validate_finish({"messages": messages}, {"success": True})

    assert accepted is False
    assert "new-sha" in reason


def test_finish_success_accepts_matching_successful_pipeline():
    policy = importlib.import_module("ut_agent.execution_policy")

    accepted, reason = policy.validate_finish(
        {"messages": _verified_finish_messages("success")},
        {"success": True},
    )

    assert accepted is True
    assert reason == ""


def test_agent_node_rejects_model_success_when_latest_pipeline_failed(monkeypatch):
    async def fake_call_agent_llm(**_kwargs):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": "finish",
                "type": "function",
                "function": {
                    "name": "finish_tool",
                    "arguments": '{"success": true, "summary": "fixed"}',
                },
            }],
        }

    monkeypatch.setattr(agent_module, "call_agent_llm", fake_call_agent_llm)
    result = asyncio.run(agent_module.agent_node({
        "messages": _verified_finish_messages("failed"),
        "iteration": 3,
        "max_iterations": 30,
    }))

    assert result["messages"][0].tool_calls == []
    assert "拒绝 success=True" in result["messages"][0].content


@pytest.mark.parametrize("pipeline_status", ["failed", "canceled", "skipped", "running"])
def test_finish_success_rejects_non_success_terminal_state(pipeline_status):
    policy = importlib.import_module("ut_agent.execution_policy")

    accepted, _reason = policy.validate_finish(
        {"messages": _verified_finish_messages(pipeline_status)},
        {"success": True},
    )

    assert accepted is False


@pytest.mark.parametrize("messages", [[], [ToolMessage(content="{bad json", tool_call_id="wait")]])
def test_finish_success_rejects_missing_or_malformed_evidence(messages):
    policy = importlib.import_module("ut_agent.execution_policy")

    accepted, reason = policy.validate_finish({"messages": messages}, {"success": True})

    assert accepted is False
    assert reason


def test_execution_policy_blocks_fourth_repair_commit():
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = []
    for index in range(3):
        sha = f"sha-{index}"
        messages += _tool_exchange(
            "commit_and_push_tool",
            f"push-{index}",
            {
                "status": "success",
                "changed": True,
                "commit_sha": sha,
                "source_branch": "feature",
                "message": "pushed",
            },
        )
        messages += _tool_exchange(
            "wait_pipeline_tool",
            f"wait-{index}",
            {
                "status": "success",
                "requested_commit_sha": sha,
                "matched_commit_sha": sha,
                "pipeline_id": 100 + index,
                "pipeline_status": "failed",
                "failed_jobs": [{"name": "build_release_arm64", "status": "failed"}],
            },
        )

    allowed, reason = policy.validate_tool_call({"messages": messages}, "commit_and_push_tool")

    assert allowed is False
    assert "3" in reason


def test_execution_policy_allows_repeated_observation_without_repair_chain():
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = []
    for index in range(2):
        messages += _tool_exchange(
            "wait_pipeline_tool",
            f"wait-{index}",
            {
                "status": "success",
                "requested_commit_sha": f"sha-{index}",
                "matched_commit_sha": f"sha-{index}",
                "pipeline_id": 100 + index,
                "pipeline_status": "failed",
                "failed_jobs": [{
                    "name": "build_release_arm64",
                    "status": "failed",
                    "log_tail": "Could not find eabot_cmake",
                }],
                "message": "failed",
            },
        )

    allowed, reason = policy.validate_tool_call({"messages": messages}, "generate_code_tool")

    assert allowed is True
    assert reason == ""


def test_execution_policy_counts_repeated_queries_of_same_pipeline_once():
    policy = importlib.import_module("ut_agent.execution_policy")
    pipeline_result = {
        "status": "success",
        "requested_commit_sha": "sha-1",
        "matched_commit_sha": "sha-1",
        "pipeline_id": 28177,
        "pipeline_status": "failed",
        "failed_jobs": [{
            "name": "build_release_arm64",
            "status": "failed",
            "log_tail": "Could not find eabot_cmake",
        }],
        "message": "failed",
    }
    messages = (
        _tool_exchange("fetch_pipeline_logs_tool", "query-1", pipeline_result)
        + _tool_exchange("fetch_pipeline_logs_tool", "query-2", pipeline_result)
    )

    allowed, reason = policy.validate_tool_call({"messages": messages}, "generate_code_tool")

    assert allowed is True
    assert reason == ""


def test_pipeline_repair_requires_investigation_for_same_root_group():
    policy = importlib.import_module("ut_agent.execution_policy")

    allowed, reason = policy.validate_tool_call(
        {"trigger_type": "pipeline_failed", "messages": []},
        "generate_code_tool",
        {"job_name": "build_release_arm64", "operation": "repair", "root_cause_id": "root-1"},
    )

    assert allowed is False
    assert "investigate" in reason


def test_agent_returns_to_loop_when_repair_needs_investigation(monkeypatch):
    async def fake_call_agent_llm(**_kwargs):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": "premature-repair",
                "type": "function",
                "function": {
                    "name": "generate_code_tool",
                    "arguments": json.dumps({
                        "job_name": "build_release_arm64",
                        "operation": "repair",
                        "root_cause_id": "root-1",
                        "task_description": "fix",
                    }),
                },
            }],
        }

    monkeypatch.setattr(agent_module, "call_agent_llm", fake_call_agent_llm)
    result = asyncio.run(agent_module.agent_node({
        "trigger_type": "pipeline_failed",
        "messages": [],
        "iteration": 1,
        "max_iterations": 30,
    }))

    assert result["messages"][0].tool_calls == []
    assert "investigate" in result["messages"][0].content
    assert not result["messages"][0].content.startswith("FINISHED:")


def test_pipeline_repair_is_allowed_after_same_root_investigation():
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = _tool_exchange(
        "generate_code_tool",
        "investigate-root",
        {
            "status": "investigated",
            "operation": "investigate",
            "job_name": "build_release_arm64",
            "root_cause_id": "root-1",
            "diagnostic": "Current pipeline and checked-out interfaces prove the failing access is unsupported.",
        },
        {"job_name": "build_release_arm64", "operation": "investigate", "root_cause_id": "root-1"},
    )

    allowed, reason = policy.validate_tool_call(
        {"trigger_type": "pipeline_failed", "messages": messages},
        "generate_code_tool",
        {"job_name": "build_release_arm64", "operation": "repair", "root_cause_id": "root-1"},
    )

    assert allowed is True
    assert reason == ""


def test_third_investigation_for_same_root_cause_is_blocked(monkeypatch):
    settings = {
        "max_investigations_per_group": 2,
        "max_hermes_calls_per_group": 4,
        "max_hermes_calls_total": 12,
        "no_progress_limit": 2,
        "max_blocker_corrections": 1,
    }
    monkeypatch.setattr(
        repair_progress_module,
        "_positive_setting",
        lambda name, default: settings.get(name, default),
    )
    messages = _tool_exchange(
        "generate_code_tool",
        "investigate-1",
        {
            "status": "investigated",
            "operation": "investigate",
            "root_cause_id": "root-1",
            "progress_fingerprint": "fingerprint-1",
        },
        {"job_name": "build_release_arm64", "operation": "investigate", "root_cause_id": "root-1"},
    )
    messages += _tool_exchange(
        "generate_code_tool",
        "investigate-2",
        {
            "status": "investigation_timeout",
            "operation": "investigate",
            "root_cause_id": "root-1",
            "progress_fingerprint": "fingerprint-2",
        },
        {"job_name": "build_release_arm64", "operation": "investigate", "root_cause_id": "root-1"},
    )

    decision = repair_progress_module.evaluate_hermes_budget(
        {"messages": messages},
        "generate_code_tool",
        {"job_name": "build_release_arm64", "operation": "investigate", "root_cause_id": "root-1"},
    )

    assert decision.allowed is False
    assert decision.reason_code == "investigation_limit"


def test_unsafe_member_change_blocks_commit_until_workspace_is_discarded():
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = _tool_exchange(
        "generate_code_tool",
        "unsafe-repair",
        {
            "status": "unsafe_changes",
            "operation": "repair",
            "job_name": "build_release_arm64",
            "root_cause_id": "root-1",
            "changed_files": ["src/handler.cpp"],
            "diagnostic": "Replaced request->node_name with request->target without interface evidence.",
        },
        {"job_name": "build_release_arm64", "operation": "repair", "root_cause_id": "root-1"},
    )

    allowed, reason = policy.validate_tool_call(
        {"trigger_type": "pipeline_failed", "messages": messages},
        "commit_and_push_tool",
    )

    assert allowed is False
    assert "discard_workspace_tool" in reason

    messages += _tool_exchange(
        "discard_workspace_tool",
        "discard-unsafe",
        {"status": "success", "discarded_files": ["src/handler.cpp"]},
        {"reason": "unsupported member substitution"},
    )
    allowed, reason = policy.validate_tool_call(
        {"trigger_type": "pipeline_failed", "messages": messages},
        "commit_and_push_tool",
    )

    assert allowed is True
    assert reason == ""


def test_agent_does_not_infer_repair_limit_from_logs_and_no_change_claim(monkeypatch):
    async def fake_call_agent_llm(**_kwargs):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": "generate",
                "type": "function",
                "function": {
                    "name": "generate_code_tool",
                    "arguments": '{"task_description": "try again"}',
                },
            }],
        }

    messages = []
    for index in range(2):
        messages += _tool_exchange(
            "wait_pipeline_tool",
            f"failed-{index}",
            {
                "status": "success",
                "requested_commit_sha": f"sha-{index}",
                "matched_commit_sha": f"sha-{index}",
                "pipeline_id": 300 + index,
                "pipeline_status": "failed",
                "failed_jobs": [{
                    "job_id": 11,
                    "name": "build_release_arm64",
                    "status": "failed",
                    "log_tail": "Could not find eabot_cmake",
                }],
                "work_items": [{
                    "job_id": 11,
                    "pipeline_id": 300 + index,
                    "job_name": "build_release_arm64",
                    "kind": "build",
                    "required_tool": "generate_code_tool",
                }],
                "message": "failed",
            },
        )
    messages += _tool_exchange(
        "generate_code_tool",
        "last-build-diagnosis",
        {
            "status": "no_changes",
            "job_name": "build_release_arm64",
            "changed_files": [],
            "diagnostic": "repository, submodule, dependency fetch and CMake source inspected",
        },
        {"job_name": "build_release_arm64", "task_description": "inspect dependency"},
    )
    monkeypatch.setattr(agent_module, "call_agent_llm", fake_call_agent_llm)

    result = asyncio.run(agent_module.agent_node({
        "trigger_type": "pipeline_failed",
        "messages": messages,
        "iteration": 10,
        "max_iterations": 30,
    }))

    next_call = result["messages"][0].tool_calls[0]
    assert next_call["name"] == "clone_source_branch_tool"


def test_execution_policy_distinguishes_different_root_errors_in_same_job():
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = []
    for index, error in enumerate(("error: missing node_name", "error: eabot_cmake not found")):
        messages += _tool_exchange(
            "wait_pipeline_tool",
            f"wait-different-{index}",
            {
                "status": "success",
                "requested_commit_sha": f"sha-{index}",
                "matched_commit_sha": f"sha-{index}",
                "pipeline_id": 200 + index,
                "pipeline_status": "failed",
                "failed_jobs": [{
                    "name": "build_release_arm64",
                    "status": "failed",
                    "log_tail": f"=== job log ===\n{error}",
                }],
                "message": "failed",
            },
        )

    allowed, reason = policy.validate_tool_call({"messages": messages}, "generate_code_tool")

    assert allowed is True
    assert reason == ""


def test_runtime_tool_context_is_isolated_between_async_tasks():
    async def capture(provider, output_dir, delay):
        context_module.init_context(provider, output_dir)
        await asyncio.sleep(delay)
        return context_module.get_git_provider(), context_module.get_output_dir()

    async def run():
        return await asyncio.gather(
            capture("provider-1", "/workspace/1", 0.01),
            capture("provider-2", "/workspace/2", 0),
        )

    assert asyncio.run(run()) == [
        ("provider-1", "/workspace/1"),
        ("provider-2", "/workspace/2"),
    ]


def test_existing_clone_is_refreshed_to_source_branch(monkeypatch, tmp_path):
    repo = tmp_path / "workspace" / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    provider = type("Provider", (), {})()
    monkeypatch.setattr(clone_branch_module.subprocess, "run", fake_run)

    result = clone_branch_module.clone_source_branch(
        provider,
        str(tmp_path / "workspace"),
        523,
        "feature/fix",
    )

    assert result == str(repo)
    assert [call[0] for call in commands] == [
        ["git", "status", "--porcelain"],
        ["git", "fetch", "origin", "feature/fix", "--depth", "1"],
        ["git", "checkout", "-B", "feature/fix", "FETCH_HEAD"],
        ["git", "submodule", "update", "--init", "--recursive", "--depth", "1"],
    ]


def test_existing_clone_refresh_surfaces_timeout(monkeypatch, tmp_path):
    repo = tmp_path / "workspace" / "mr_523" / "repo"
    (repo / ".git").mkdir(parents=True)

    def timeout(*_args, **_kwargs):
        raise clone_branch_module.subprocess.TimeoutExpired(["git", "status"], 300)

    monkeypatch.setattr(clone_branch_module.subprocess, "run", timeout)

    result = clone_branch_module.clone_source_branch(
        type("Provider", (), {})(),
        str(tmp_path / "workspace"),
        523,
        "feature/fix",
    )

    assert result == "ERROR: 刷新已有仓库超时 (300s)"


def test_tool_result_truncation_preserves_first_compiler_error():
    content = (
        "setup\n"
        + ("x" * 2100)
        + "\nsrc/main.cpp:142: error: no member named trace_id\n"
        + ("y" * 1200)
        + "\nERROR: Uploading artifacts failed"
    )

    result = llm_module._truncate_tool_results([{"role": "tool", "content": content}])

    assert result[0]["content"].startswith("setup")
    assert "error: no member named trace_id" in result[0]["content"]
    assert "Uploading artifacts failed" not in result[0]["content"]
    assert result[0]["content"].endswith("...(已截断)")
    assert len(result[0]["content"]) <= 3020


def test_missing_member_name_is_preserved_in_diagnostic_identity():
    diagnostic = "main.cpp:142:23: error: Widget has no member named 'node_name'"

    assert repair_progress_module.normalize_diagnostic(diagnostic) == (
        "main.cpp: error: widget has no member named 'node_name'"
    )


def test_different_missing_members_are_different_root_causes():
    groups = repair_progress_module.build_root_cause_groups([
        {"job_id": 1, "name": "build_release_arm64", "log_tail": "error: no member named 'node_name'"},
        {"job_id": 2, "name": "clang_tidy_check", "log_tail": "error: no member named 'trace_id'"},
    ])

    assert len(groups) == 2
    assert {group.canonical_diagnostic for group in groups} == {
        "error: no member named 'node_name'",
        "error: no member named 'trace_id'",
    }


def test_pipeline_compaction_preserves_causal_line_for_every_job():
    failed_jobs = [
        {
            "job_id": index,
            "pipeline_id": 30789,
            "name": name,
            "status": "failed",
            "causal_lines": [
                f"src/component.cpp:142:23: error: Request has no member named '{member}'"
            ],
            "log_context": "x" * 4000,
            "log_tail": "x" * 4000,
        }
        for index, (name, member) in enumerate([
            ("build_release_arm64", "node_name"),
            ("x86_64_ut_coverage_check", "node_name"),
            ("clang_tidy_check", "node_name"),
        ], start=1)
    ]
    content = json.dumps({"status": "success", "failed_jobs": failed_jobs}, ensure_ascii=False)

    compacted = llm_module._compact_pipeline_result(content, 3000)

    payload = json.loads(compacted)
    assert len(payload["failed_jobs"]) == 3
    assert all("node_name" in job["causal_lines"][0] for job in payload["failed_jobs"])
    assert len(compacted) <= 3000


def test_failed_job_diagnostics_preserve_ordered_candidates_from_full_trace():
    trace = "\n".join((
        "starting command",
        "fatal: declared external reference ref-absent was not found",
        "cleanup command",
        "remote: rpc error: code = Canceled desc = request canceled",
    ))
    job = fetch_pipeline_module._with_structured_diagnostics({
        "job_id": 101,
        "pipeline_id": 501,
        "name": "build_release_arm64",
        "status": "failed",
        "log_tail": "remote: rpc error: code = Canceled desc = request canceled",
    }, trace=trace)

    candidates = job["diagnostic_candidates"]
    assert [item["line_number"] for item in candidates] == [2, 4]
    assert "ref-absent" in candidates[0]["text"]
    assert job["diagnostic_candidate_count"] == 2
    assert job["diagnostic_candidates_truncated"] is False
    assert job["diagnostic_identity_count"] == 2
    assert job["omitted_diagnostic_identity_count"] == 0
    assert all(candidate["diagnostic_identity"] for candidate in candidates)


def test_clang_diagnostic_rejects_generic_failure_counter():
    job = fetch_pipeline_module._with_structured_diagnostics(
        {"name": "clang_tidy_check", "job_id": 1, "pipeline_id": 2, "log_tail": ""},
        trace='"failure": 0\nERROR: Job failed: exit code 1',
    )

    assert job["evidence_mode"] == "raw_log_fallback"
    assert job["diagnostic_candidates"] == []
    assert job["causal_lines"] == []
    assert repair_progress_module.build_root_cause_groups([job])[0].canonical_diagnostic == ""


def test_clang_diagnostic_accepts_file_location_and_check_name():
    trace = (
        "/builds/eabot/prism/src/a.cpp:42:7: warning: loop uses floating point counter "
        "[clang-analyzer-security.FloatLoopCounter]"
    )
    job = fetch_pipeline_module._with_structured_diagnostics(
        {"name": "clang_tidy_check", "job_id": 1, "pipeline_id": 2, "log_tail": ""},
        trace=trace,
    )

    assert job["evidence_mode"] == "structured_evidence"
    assert job["diagnostic_candidates"][0]["text"] == trace
    assert job["causal_lines"] == [trace]


def test_generate_code_labels_ordered_diagnostics_as_candidates():
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline-cascade", {
        "status": "success",
        "pipeline_id": 501,
        "pipeline_status": "failed",
        "failed_jobs": [{
            "job_id": 101,
            "pipeline_id": 501,
            "name": "build_release_arm64",
            "status": "failed",
            "causal_lines": ["fatal: fallback"],
            "diagnostic_candidates": [
                {"candidate_id": "candidate-primary", "line_number": 40, "signal": "fatal", "text": "fatal: external reference was not found"},
                {"candidate_id": "candidate-secondary", "line_number": 47, "signal": "error", "text": "rpc error: request canceled"},
            ],
            "diagnostic_candidate_count": 20,
            "diagnostic_candidates_truncated": True,
        }],
        "root_cause_groups": [{
            "root_cause_id": "schedule-key",
            "canonical_diagnostic": "rpc error: request canceled",
            "canonical_job_name": "build_release_arm64",
            "job_names": ["build_release_arm64"],
            "job_ids": [101],
        }],
    })

    resolved_id, prompt = generate_code_module._pipeline_evidence_for(
        {"messages": messages}, "schedule-key", "build_release_arm64"
    )

    assert resolved_id == "schedule-key"
    assert "CI 失败证据候选" in prompt
    assert "不是已确认根因" in prompt
    assert "候选总数: 20; 已截断: 是" in prompt
    assert prompt.index("candidate-primary") < prompt.index("candidate-secondary")


def test_pipeline_compaction_retains_candidate_order_and_truncation_metadata():
    job = fetch_pipeline_module._with_structured_diagnostics({
        "job_id": 101,
        "pipeline_id": 501,
        "name": "build_release_arm64",
        "status": "failed",
        "log_tail": "error: fallback",
    }, trace="\n".join(f"error: failure-{index}" for index in range(20)))
    content = json.dumps({
        "status": "success",
        "pipeline_id": 501,
        "pipeline_status": "failed",
        "failed_jobs": [job],
    })

    compacted = json.loads(llm_module._compact_pipeline_result(content, 5000))
    compact_job = compacted["failed_jobs"][0]

    assert compact_job["diagnostic_candidate_count"] == 20
    assert compact_job["diagnostic_candidates_truncated"] is True
    assert compact_job["diagnostic_identity_count"] == 20
    assert compact_job["omitted_diagnostic_identity_count"] == 8
    assert compact_job["diagnostic_candidates"][0]["diagnostic_identity"]
    assert compact_job["diagnostic_candidates"][0]["line_number"] == 1
    assert compact_job["diagnostic_candidates"][-1]["line_number"] == 20


def test_mr_549_large_pipeline_compaction_keeps_node_name_error():
    job = fetch_pipeline_module._with_structured_diagnostics({
        "job_id": 99429,
        "pipeline_id": 30960,
        "name": "build_release_arm64",
        "status": "failed",
        "log_tail": MR_547_NODE_NAME_ERROR + ("\ncontext" * 800),
    })
    group = repair_progress_module.build_root_cause_groups([job])[0]
    payload = {
        "status": "success",
        "requested_commit_sha": "e575a8e9a1dd6604eff2e87fb7a5719f3502b44d",
        "matched_commit_sha": "e575a8e9a1dd6604eff2e87fb7a5719f3502b44d",
        "pipeline_id": 30960,
        "root_pipeline_id": 30959,
        "validation_pipeline_id": 30960,
        "pipeline_ids": list(range(28000, 31000)),
        "pipeline_status": "failed",
        "failed_jobs": [job],
        "work_items": fetch_pipeline_module._build_work_items([job]),
        "root_cause_groups": [group.to_dict()],
    }

    compacted = llm_module._compact_pipeline_result(json.dumps(payload), 3000)

    assert compacted is not None
    assert len(compacted) <= 3000
    assert "node_name" in compacted
    compacted_payload = json.loads(compacted)
    assert "pipeline_ids" not in compacted_payload
    assert compacted_payload["root_cause_groups"][0]["root_cause_id"] == group.root_cause_id


def test_pipeline_facts_include_canonical_root_cause():
    facts = fetch_pipeline_module._pipeline_facts({
        "pipeline_status": "failed",
        "failed_jobs": [{"name": "build_release_arm64"}],
        "root_cause_groups": [{
            "root_cause_id": "root-1",
            "canonical_job_name": "build_release_arm64",
            "canonical_diagnostic": "component.cpp:142: error: no member named 'node_name'",
        }],
    })

    assert any("node_name" in fact for fact in facts)


def test_mr_547_diagnostic_reaches_investigation_and_repair_prompts(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_547" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    monkeypatch.setattr(generate_code_module, "load_prompt", lambda _name: "{task_description}")
    monkeypatch.setattr(generate_code_module, "_get_changed_files", lambda _repo: [])
    monkeypatch.setattr(
        repair_safety_module,
        "validate_member_substitutions",
        lambda _repo, _evidence=(): (True, ""),
    )
    job = fetch_pipeline_module._with_structured_diagnostics({
        "job_id": 1,
        "pipeline_id": 30789,
        "name": "build_release_arm64",
        "status": "failed",
        "log_tail": ("context\n" * 500) + MR_547_NODE_NAME_ERROR,
    })
    group = repair_progress_module.build_root_cause_groups([job])[0]
    compacted = json.loads(llm_module._compact_pipeline_result(json.dumps({
        "status": "success",
        "pipeline_id": 30789,
        "pipeline_status": "failed",
        "failed_jobs": [job],
        "root_cause_groups": [group.to_dict()],
    }), 3000))
    evidence = json.dumps(compacted, ensure_ascii=False)
    prompts = []
    outcomes = iter([
        generate_code_module.HermesRunOutcome((), "接口中不存在 node_name"),
        generate_code_module.HermesRunOutcome((str(repo / "src/component.cpp"),), "已修复 node_name 访问"),
    ])

    def fake_run_hermes(_repo, prompt, **_kwargs):
        prompts.append(prompt)
        return next(outcomes)

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)
    common = {
        "job_name": "build_release_arm64",
        "root_cause_id": group.root_cause_id,
        "state": {"mr_id": 547, "iteration": 1, "trigger_type": "pipeline_failed"},
    }
    investigation = json.loads(generate_code_module.generate_code_tool.func(
        task_description=evidence,
        operation="investigate",
        **common,
    ))
    repair = json.loads(generate_code_module.generate_code_tool.func(
        task_description=evidence,
        operation="repair",
        **common,
    ))

    assert investigation["status"] == "investigated"
    assert repair["status"] == "changed"
    assert all("node_name" in prompt for prompt in prompts)
    assert all("<member>" not in prompt for prompt in prompts)
    messages = _tool_exchange(
        "generate_code_tool",
        "investigate-547",
        investigation,
        {"job_name": "build_release_arm64", "operation": "investigate", "root_cause_id": group.root_cause_id},
    )
    messages += _tool_exchange(
        "generate_code_tool",
        "repair-547",
        repair,
        {"job_name": "build_release_arm64", "operation": "repair", "root_cause_id": group.root_cause_id},
    )
    policy = importlib.import_module("ut_agent.execution_policy")
    assert policy.validate_tool_call({"messages": messages}, "commit_and_push_tool") == (True, "")


def test_mr_549_diagnostic_is_injected_when_outer_agent_description_is_vague(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_549" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    monkeypatch.setattr(generate_code_module, "load_prompt", lambda _name: "{task_description}")
    monkeypatch.setattr(generate_code_module, "_get_changed_files", lambda _repo: [])
    job = fetch_pipeline_module._with_structured_diagnostics({
        "job_id": 99429,
        "pipeline_id": 30960,
        "name": "build_release_arm64",
        "status": "failed",
        "log_tail": MR_547_NODE_NAME_ERROR,
    })
    group = repair_progress_module.build_root_cause_groups([job])[0]
    pipeline_result = {
        "status": "success",
        "pipeline_id": 30960,
        "pipeline_status": "failed",
        "failed_jobs": [job],
        "work_items": fetch_pipeline_module._build_work_items([job]),
        "root_cause_groups": [group.to_dict()],
    }
    messages = _tool_exchange(
        "fetch_pipeline_logs_tool",
        "pipeline-549",
        pipeline_result,
        {"pipeline_id": 30960, "job_name": "build_release_arm64"},
    )
    prompts = []

    def fake_run_hermes(_repo, prompt, **_kwargs):
        prompts.append(prompt)
        return generate_code_module.HermesRunOutcome((), "已核对当前接口")

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)

    result = json.loads(generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description="调查当前编译失败，不做修改。",
        operation="investigate",
        root_cause_id="",
        state={
            "mr_id": 549,
            "iteration": 11,
            "trigger_type": "pipeline_failed",
            "messages": messages,
        },
    ))

    assert result["status"] == "investigated"
    assert result["root_cause_id"] == group.root_cause_id
    assert len(prompts) == 1
    assert MR_547_NODE_NAME_ERROR in prompts[0]
    assert group.root_cause_id in prompts[0]
    assert "不要搜索 CI 日志" in prompts[0]


def test_current_dependency_contract_reaches_investigation_and_repair(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "mr_549" / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(ToolContext, "output_dir", str(workspace))
    monkeypatch.setattr(generate_code_module, "load_prompt", lambda _name: "{task_description}")
    monkeypatch.setattr(generate_code_module, "_get_changed_files", lambda _repo: [])
    monkeypatch.setattr(
        repair_safety_module,
        "validate_member_substitutions",
        lambda _repo, _evidence=(): (True, ""),
    )
    job = fetch_pipeline_module._with_structured_diagnostics({
        "job_id": 99429,
        "pipeline_id": 30960,
        "name": "build_release_arm64",
        "status": "failed",
        "log_tail": MR_547_NODE_NAME_ERROR,
    })
    group = repair_progress_module.build_root_cause_groups([job])[0]
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline-549", {
        "status": "success",
        "pipeline_id": 30960,
        "pipeline_status": "failed",
        "failed_jobs": [job],
        "root_cause_groups": [group.to_dict()],
    })
    messages += _tool_exchange("resolve_dependency_evidence_tool", "dependency-549", {
        "status": "resolved",
        "root_cause_id": group.root_cause_id,
        "project_path": "eabot/lhotse",
        "declared_branch": "dev",
        "resolved_sha": "lhotse-current-sha",
        "file_path": "eabot_msgs/srv/RemoteControl.srv",
        "content_sha256": "contract-digest",
        "content": (
            "int64 timestamp_ns\nuint32 command\nstring trace_id\nstring optional\n"
            "---\nint64 timestamp_ns\nstring trace_id\nbool success\n"
        ),
    }, {
        "job_name": "build_release_arm64",
        "root_cause_id": group.root_cause_id,
    })
    prompts = []
    outcomes = iter([
        generate_code_module.HermesRunOutcome((), "已核对当前接口"),
        generate_code_module.HermesRunOutcome((str(repo / "src/component.cpp"),), "已移除无效路由"),
    ])

    def fake_run_hermes(_repo, prompt, **_kwargs):
        prompts.append(prompt)
        return next(outcomes)

    monkeypatch.setattr(generate_code_module, "_run_hermes", fake_run_hermes)
    state = {
        "mr_id": 549,
        "iteration": 11,
        "trigger_type": "pipeline_failed",
        "messages": messages,
    }

    investigation = json.loads(generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description="调查失败。",
        operation="investigate",
        root_cause_id=group.root_cause_id,
        state=state,
    ))
    repair = json.loads(generate_code_module.generate_code_tool.func(
        job_name="build_release_arm64",
        task_description="修复失败。",
        operation="repair",
        root_cause_id=group.root_cause_id,
        state=state,
    ))

    assert investigation["status"] == "investigated"
    assert repair["status"] == "changed"
    assert len(prompts) == 2
    assert all("## 当前声明依赖接口（只读快照）" in prompt for prompt in prompts)
    assert all("eabot/lhotse" in prompt and "lhotse-current-sha" in prompt for prompt in prompts)
    assert all("uint32 command" in prompt and "string optional" in prompt for prompt in prompts)
    assert all("string node_name" not in prompt and "string target" not in prompt for prompt in prompts)


def test_precise_ci_evidence_allows_repair_after_one_investigation_timeout():
    job = fetch_pipeline_module._with_structured_diagnostics({
        "job_id": 99429,
        "pipeline_id": 30960,
        "name": "build_release_arm64",
        "status": "failed",
        "log_tail": MR_547_NODE_NAME_ERROR,
    })
    group = repair_progress_module.build_root_cause_groups([job])[0]
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline-549", {
        "status": "success",
        "pipeline_id": 30960,
        "pipeline_status": "failed",
        "failed_jobs": [job],
        "work_items": fetch_pipeline_module._build_work_items([job]),
        "root_cause_groups": [group.to_dict()],
    })
    messages += _tool_exchange("generate_code_tool", "investigate-549", {
        "status": "investigation_timeout",
        "operation": "investigate",
        "job_name": "build_release_arm64",
        "root_cause_id": group.root_cause_id,
        "failure_kind": "search_loop",
        "diagnostic": "搜索操作超过本次执行预算。",
    }, {
        "job_name": "build_release_arm64",
        "operation": "investigate",
        "root_cause_id": group.root_cause_id,
    })
    state = {"trigger_type": "pipeline_failed", "messages": messages}
    policy = importlib.import_module("ut_agent.execution_policy")

    repair_allowed, repair_reason = policy.validate_tool_call(state, "generate_code_tool", {
        "job_name": "build_release_arm64",
        "operation": "repair",
        "root_cause_id": group.root_cause_id,
    })
    investigate_allowed, investigate_reason = policy.validate_tool_call(state, "generate_code_tool", {
        "job_name": "build_release_arm64",
        "operation": "investigate",
        "root_cause_id": group.root_cause_id,
    })

    assert repair_allowed is True
    assert repair_reason == ""
    assert investigate_allowed is False
    assert 'operation="repair"' in investigate_reason


def test_failed_summary_uses_job_status_not_pipeline_fetch_status():
    job = fetch_pipeline_module._with_structured_diagnostics({
        "job_id": 99429,
        "pipeline_id": 30960,
        "name": "build_release_arm64",
        "status": "failed",
        "log_tail": MR_547_NODE_NAME_ERROR,
    })
    pipeline_result = {
        "status": "success",
        "pipeline_id": 30960,
        "pipeline_status": "failed",
        "failed_jobs": [job],
        "work_items": fetch_pipeline_module._build_work_items([job]),
        "root_cause_groups": [group.to_dict() for group in repair_progress_module.build_root_cause_groups([job])],
    }
    messages = _tool_exchange(
        "fetch_pipeline_logs_tool",
        "pipeline-549-first",
        pipeline_result,
        {"pipeline_id": 30960, "job_name": "build_release_arm64"},
    )
    messages += _tool_exchange(
        "fetch_pipeline_logs_tool",
        "pipeline-549-repeat",
        pipeline_result,
        {"pipeline_id": 30960, "job_name": "build_release_arm64"},
    )

    summary = importlib.import_module("ut_agent.execution_policy").build_failed_summary(
        {"messages": messages},
        "根因组已达到调查上限",
    )

    assert "build_release_arm64: failed" in summary
    assert "build_release_arm64: success" not in summary
    assert "node_name" in summary


def test_iteration_limit_after_repair_change_reports_pending_commit():
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline-30", {
        "status": "success",
        "pipeline_id": 30789,
        "pipeline_status": "failed",
        "failed_jobs": [{"name": "build_release_arm64"}],
    })
    messages += _tool_exchange("generate_code_tool", "repair-30", {
        "status": "changed",
        "operation": "repair",
        "changed_files": ["src/component.cpp"],
    }, {"operation": "repair", "job_name": "build_release_arm64"})
    state = {"iteration": 30, "max_iterations": 30, "messages": messages}

    result = agent_module._extract_result(state, messages)

    assert result["success"] == 0
    assert result["pushed_sha"] is None
    assert result["result_pipeline_id"] == 0
    assert result["final_pipeline_status"] == "unknown"
    assert "已产生修改" in result["error"]
    assert "尚未提交" in result["error"]


def test_iteration_limit_after_push_reports_pending_exact_pipeline():
    messages = _tool_exchange("commit_and_push_tool", "push-30", {
        "status": "success",
        "changed": True,
        "commit_sha": "fixed-sha",
    })
    state = {"iteration": 30, "max_iterations": 30, "messages": messages}

    result = agent_module._extract_result(state, messages)

    assert "fixed-sha" in result["error"]
    assert "尚未完成精确流水线验证" in result["error"]


def test_search_loop_terminal_is_not_reported_as_model_unavailable():
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline-1", {
        "status": "success",
        "pipeline_id": 30789,
        "pipeline_status": "failed",
        "failed_jobs": [{"name": "build_release_arm64"}],
    })
    messages += _tool_exchange("generate_code_tool", "investigate-1", {
        "status": "investigation_timeout",
        "operation": "investigate",
        "failure_kind": "search_loop",
        "diagnostic": "搜索操作超过本次执行预算。",
    }, {"operation": "investigate", "job_name": "build_release_arm64"})
    state = {"messages": messages, "iteration": 30, "max_iterations": 30}

    result = agent_module._extract_result(state, messages)
    card = agent_module._build_finished_card(state, messages, result, "")

    assert result["pushed_sha"] is None
    assert result["result_pipeline_id"] == 0
    assert result["final_pipeline_status"] == "unknown"
    assert "自动调查超时" in card
    assert "模型服务暂时不可用" not in card


def test_incomplete_repair_session_is_reported_as_protocol_failure_not_model_outage():
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline-561", {
        "status": "success",
        "pipeline_id": 34949,
        "pipeline_status": "failed",
        "failed_jobs": [{"name": "build_release_arm64", "status": "failed"}],
    })
    messages += _tool_exchange("generate_code_tool", "repair-561", {
        "status": "coding_infra_error",
        "operation": "repair_session",
        "failure_kind": "session_incomplete",
        "model_failure_code": "tool_protocol_error",
        "message": "自动修复会话多次未返回完整结果；已完成同模型重试和备用模型切换，未产生可提交修改。",
    }, {"operation": "repair_session", "job_name": "build_release_arm64"})
    state = {"messages": messages, "iteration": 30, "max_iterations": 30}

    result = agent_module._extract_result(state, messages)
    card = agent_module._build_finished_card(state, messages, result, "")

    assert result["terminal_failure_kind"] == "session_incomplete"
    assert "修复会话多次未返回完整结果" in result["error"]
    assert "修复会话多次未返回完整结果" in card
    assert "模型服务不可用" not in card


def test_identity_validation_failure_is_preserved_as_the_terminal_cause():
    summary = "缺少 16 条诊断身份，存在 16 条未知身份。"
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline-561-identity", {
        "status": "success",
        "pipeline_id": 35071,
        "pipeline_status": "failed",
        "failed_jobs": [{"name": "build_release_arm64", "status": "failed"}],
    })
    messages += _tool_exchange("generate_code_tool", "repair-561-identity", {
        "status": "partial_changes",
        "operation": "repair_session",
        "failure_kind": "partial_changes",
        "changed_files": ["src/component.cpp"],
        "terminal_validation_error_code": "diagnostic_identity_mismatch",
        "terminal_validation_summary": summary,
        "normalized_diagnostic_alias_count": 0,
        "message": "自动修复已产生修改，但报告校验失败。",
    }, {"operation": "repair_session", "job_name": "build_release_arm64"})

    result = agent_module._extract_result(
        {"messages": messages, "iteration": 30, "max_iterations": 30},
        messages,
    )
    card = agent_module._build_finished_card({}, messages, result, "")

    assert result["terminal_validation_error_code"] == "diagnostic_identity_mismatch"
    assert result["terminal_validation_summary"] == summary
    assert result["normalized_diagnostic_alias_count"] == 0
    assert result["error"] == summary
    assert summary in card
    assert "模型服务" not in card
    assert "工具调用流" not in card


def test_unpushed_coding_infrastructure_failure_keeps_pipeline_as_evidence_only():
    messages = _tool_exchange("fetch_pipeline_logs_tool", "source-549", {
        "status": "success",
        "pipeline_id": 34796,
        "root_pipeline_id": 34795,
        "validation_pipeline_id": 34796,
        "pipeline_status": "failed",
        "matched_commit_sha": "source-sha",
        "failed_jobs": [{"name": "build_release_arm64", "status": "failed"}],
    })
    messages += _tool_exchange("generate_code_tool", "repair-549", {
        "status": "coding_infra_error",
        "operation": "repair_session",
        "job_name": "build_release_arm64",
        "changed_files": [],
        "failure_kind": "provider_unavailable",
        "message": "模型服务暂时不可用；已尝试模型：Sonnet、Opus；原因：http_503。",
    }, {"operation": "repair_session", "job_name": "build_release_arm64"})

    result = agent_module._extract_result({"iteration": 6, "max_iterations": 30}, messages)

    assert result["pushed_sha"] is None
    assert result["result_pipeline_id"] == 0
    assert result["result_pipeline_sha"] == ""
    assert result["final_pipeline_status"] == "unknown"
    assert result["pipeline_groups"][-1]["root_pipeline_id"] == 34795
    assert result["pipeline_groups"][-1]["validation_pipeline_id"] == 34796
    assert result["terminal_failure_kind"] == "provider_unavailable"
    assert result["error"] == "模型服务暂时不可用；已尝试模型：Sonnet、Opus；原因：http_503。"


def test_pipeline_tool_result_compaction_preserves_every_failed_job():
    failed_jobs = [
        {
            "job_id": index,
            "pipeline_id": 28177,
            "name": name,
            "status": "failed",
            "log_tail": f"{name} root error\n" + ("x" * 1800),
        }
        for index, name in enumerate(
            [
                "build_release_arm64",
                "code_format_check",
                "mr_merge_commit_check",
                "x86_64_ut_coverage_check",
            ],
            start=1,
        )
    ]
    content = json.dumps({
        "status": "success",
        "pipeline_id": 28177,
        "pipeline_status": "failed",
        "failed_jobs": failed_jobs,
        "work_items": [{"job_name": job["name"]} for job in failed_jobs],
        "message": "summary" * 1000,
    })

    result = llm_module._truncate_tool_results([{"role": "tool", "content": content}])
    compacted = json.loads(result[0]["content"])

    assert [job["name"] for job in compacted["failed_jobs"]] == [
        "build_release_arm64",
        "code_format_check",
        "mr_merge_commit_check",
        "x86_64_ut_coverage_check",
    ]
    assert all("root error" in job["log_tail"] for job in compacted["failed_jobs"])
    assert len(result[0]["content"]) <= 3000


def test_structured_tool_result_compaction_keeps_valid_json_and_evidence():
    content = json.dumps({
        "status": "no_changes",
        "job_name": "build_release_arm64",
        "changed_files": [],
        "diagnostic": "root cause: dependency source mismatch\n" + ("x" * 5000),
        "message": "Hermes completed",
    })

    result = llm_module._truncate_tool_results([{"role": "tool", "content": content}])
    compacted = json.loads(result[0]["content"])

    assert compacted["status"] == "no_changes"
    assert compacted["job_name"] == "build_release_arm64"
    assert "dependency source mismatch" in compacted["diagnostic"]
    assert len(result[0]["content"]) <= 3000


def test_conversation_logs_are_isolated_by_mr():
    first = agent_module._conversation_log_path({"mr_id": 522})
    second = agent_module._conversation_log_path({"mr_id": 523})

    assert first.endswith("conversation_mr_522.log")
    assert second.endswith("conversation_mr_523.log")
    assert first != second


def test_generate_code_detects_all_git_worktree_changes(monkeypatch):
    result = type("Result", (), {
        "returncode": 0,
        "stdout": " M scripts/build.sh\n?? config/tool.toml\n",
        "stderr": "",
    })()
    monkeypatch.setattr(generate_code_module.subprocess, "run", lambda *_args, **_kwargs: result)

    assert generate_code_module._get_changed_files("/repo") == [
        "/repo/config/tool.toml",
        "/repo/scripts/build.sh",
    ]


def test_hermes_runtime_uses_isolated_current_provider_config(monkeypatch):
    monkeypatch.setattr(generate_code_module, "API_KEY", "test-secret")
    monkeypatch.setattr(generate_code_module, "BASE_URL", "https://relay.example")
    monkeypatch.setattr(generate_code_module, "HERMES_API_MODE", "anthropic_messages", raising=False)
    with generate_code_module._hermes_runtime("anthropic/test-model") as env:
        config = (Path(env["HOME"]) / ".hermes" / "config.yaml").read_text()

        assert env["HERMES_RELAY_API_KEY"] == "test-secret"
        assert "test-secret" not in config
        assert "base_url: \"https://relay.example/v1\"" in config
        assert "default: \"test-model\"" in config
        assert "provider: relay" in config
        assert "api_mode: anthropic_messages" in config


def test_extract_hermes_api_error_detects_zero_exit_http_400():
    error = generate_code_module._extract_hermes_api_error([
        "API Error: Error code: 400 - Extra inputs are not permitted",
        "Non-retryable error: request rejected",
    ], [])

    assert error is not None
    assert "400" in error


def test_extract_hermes_api_error_ignores_normal_diagnostic_text():
    error = generate_code_module._extract_hermes_api_error([
        "The compiler error is caused by dependency version 400",
    ], [])

    assert error is None


def test_hermes_execution_deadline_is_not_switchable():
    outcome = generate_code_module._hermes_timeout_outcome(
        stdout_lines=["search_files", "read_file"] * 30,
        stderr_lines=[],
        elapsed=601,
    )

    assert outcome.failure_kind == generate_code_module.HermesFailureKind.SEARCH_LOOP
    assert outcome.provider_failure is None


def test_hermes_explicit_provider_timeout_is_switchable():
    outcome = generate_code_module._provider_error_outcome("API Error: ReadTimeout from relay")

    assert outcome.failure_kind == generate_code_module.HermesFailureKind.PROVIDER_TIMEOUT
    assert outcome.provider_failure.switchable is True


def test_run_hermes_detects_api_error_when_process_exits_zero(monkeypatch, tmp_path):
    class FakeProcess:
        def __init__(self):
            self.stdout = io.StringIO("API Error: Error code: 400 - invalid tool schema\n")
            self.stderr = io.StringIO("")
            self.returncode = 0

        def poll(self):
            return self.returncode

    which_result = type("Result", (), {"stdout": "/usr/local/bin/hermes\n"})()
    monkeypatch.setattr(generate_code_module.subprocess, "run", lambda *_args, **_kwargs: which_result)
    monkeypatch.setattr(generate_code_module.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())

    outcome = generate_code_module._run_hermes(
        str(tmp_path),
        "fix build",
        model="anthropic/test-model",
    )

    assert outcome.changed_files == ()
    assert "400" in outcome.diagnostic
    assert outcome.failure_kind == generate_code_module.HermesFailureKind.REQUEST_INVALID
    assert outcome.provider_failure is None


def test_run_hermes_timeout_does_not_block_on_silent_stdout(monkeypatch, tmp_path):
    released = threading.Event()

    class BlockingStream:
        def readline(self):
            released.wait()
            return ""

    class FakeProcess:
        def __init__(self):
            self.stdout = BlockingStream()
            self.stderr = BlockingStream()
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9
            released.set()

        def wait(self, timeout=None):
            return self.returncode

    which_result = type("Result", (), {"stdout": "/usr/local/bin/hermes\n"})()
    monkeypatch.setattr(generate_code_module.subprocess, "run", lambda *_args, **_kwargs: which_result)
    monkeypatch.setattr(generate_code_module.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(generate_code_module, "CODING_AGENT_TIMEOUT", 0.05)

    started = time.monotonic()
    outcome = generate_code_module._run_hermes(str(tmp_path), "fix build")

    assert time.monotonic() - started < 1
    assert outcome.failure_kind == generate_code_module.HermesFailureKind.EXECUTION_BUDGET_EXHAUSTED


def test_pipeline_hermes_runtime_blocks_system_package_mutation():
    with generate_code_module._hermes_runtime(
        "anthropic/test-model",
        guard_system_mutations=True,
    ) as env:
        result = subprocess.run(["apt-get", "update"], env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 126
    assert "system package mutation is disabled" in result.stderr


def test_execution_policy_rejects_coding_infra_error_as_complete_diagnosis():
    policy = importlib.import_module("ut_agent.execution_policy")
    pipeline_result = {
        "status": "success",
        "pipeline_id": 28177,
        "pipeline_status": "failed",
        "failed_jobs": [{
            "job_id": 13,
            "pipeline_id": 28177,
            "name": "build_release_arm64",
            "status": "failed",
        }],
        "work_items": [{
            "job_id": 13,
            "pipeline_id": 28177,
            "job_name": "build_release_arm64",
            "kind": "build",
            "required_tool": "generate_code_tool",
        }],
    }
    messages = _tool_exchange("fetch_pipeline_logs_tool", "pipeline", pipeline_result)
    messages += _tool_exchange(
        "generate_code_tool",
        "generate-build",
        {
            "status": "coding_infra_error",
            "operation": "repair",
            "job_name": "build_release_arm64",
            "changed_files": [],
            "diagnostic": "API Error: Error code: 400 - invalid tool schema",
            "message": "Hermes API failed",
        },
        {"job_name": "build_release_arm64", "operation": "repair", "task_description": "fix build"},
    )

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "Hermes API failed"},
    )

    assert accepted is False
    assert "repair 操作未完成" in reason


def test_triage_collects_diff_file_paths_for_repository_discovery(monkeypatch):
    diff = type("Diff", (), {
        "filename": "src/module/tests/test_controller.cpp",
        "patch": "@@ -1 +1 @@",
        "head_file": "new content",
        "edit_type": type("EditType", (), {"name": "ADDED"})(),
        "language": "cpp",
    })()
    merge_request = type("MergeRequest", (), {
        "iid": 523,
        "title": "Generated tests",
        "author": {"username": "agent"},
        "target_branch": "main",
    })()
    provider = type("Provider", (), {
        "pr": merge_request,
        "id_project": "eabot/cook",
        "get_pr_branch": staticmethod(lambda: "feature/tests"),
        "get_diff_files": staticmethod(lambda: [diff]),
    })()
    triage = PRTriage.__new__(PRTriage)
    triage.pr_url = "https://gitlab.example/mr/523"
    triage.git_provider = provider
    monkeypatch.setattr(triage, "_fetch_failed_pipeline_info", lambda: ([], 27932, "abc123"))

    result = triage._collect_triage_info()

    assert result["diff_files"] == [{
        "filename": "src/module/tests/test_controller.cpp",
        "patch": "@@ -1 +1 @@",
        "head_file": "new content",
        "edit_type": "ADDED",
        "language": "cpp",
    }]


def test_pipeline_failure_prompt_includes_changed_file_paths():
    prompt = agent_system_module.build_system_prompt(
        {
            "trigger_type": "pipeline_failed",
            "mr_id": 523,
            "title": "Generated tests",
            "source_branch": "feature/tests",
            "target_branch": "main",
            "failed_jobs": [],
            "diff_files": [{"filename": "src/module/tests/test_controller.cpp"}],
        },
        "tools",
    )

    assert "变更文件: src/module/tests/test_controller.cpp" in prompt


def test_pipeline_failure_prompt_requires_source_search_for_installed_headers():
    prompt = agent_system_module.build_system_prompt(
        {
            "trigger_type": "pipeline_failed",
            "mr_id": 522,
            "title": "Apply suggestion",
            "source_branch": "feature/fix",
            "target_branch": "main",
            "failed_jobs": [{"name": "build_release_arm64"}],
        },
        "tools",
    )

    assert "/builds/.../install/<package>/..." in prompt
    assert "先克隆仓库" in prompt
    assert "交给 generate_code 搜索对应源码和构建配置" in " ".join(prompt.split())
    assert "不能仅因报错路径不在仓库中就转人工" in prompt


def _native_finish_messages(work_item: dict) -> list:
    return _tool_exchange("fetch_pipeline_logs_tool", "native-pipeline", {
        "status": "success",
        "pipeline_id": 41000,
        "pipeline_status": "failed",
        "failed_jobs": [{"job_id": work_item["job_id"], "name": work_item["job_name"]}],
        "work_items": [work_item],
    })


def _native_build_work_item() -> dict:
    return {
        "job_id": 41,
        "pipeline_id": 41000,
        "job_name": "build_release_arm64",
        "kind": "build",
        "required_tool": "generate_code_tool",
    }


def test_native_failed_finish_requires_real_patch_attempt(native_backend):
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = _native_finish_messages(_native_build_work_item())

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "没有安全修改。"},
    )

    assert accepted is False
    assert "apply_repo_patch_tool" in reason
    assert "generate_code_tool" not in reason


def test_native_failed_finish_reports_failed_patch_without_hermes(native_backend):
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = _native_finish_messages(_native_build_work_item())
    messages += _tool_exchange(
        "apply_repo_patch_tool",
        "native-patch-failed",
        {"status": "error", "patch_applied": False, "message": "patch does not apply"},
    )

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "补丁未能安全应用。"},
    )

    assert accepted is False
    assert "Native patch 尚未成功" in reason
    assert "Hermes" not in reason
    assert "generate_code_tool" not in reason


def test_native_failed_finish_requires_pipeline_for_successful_patch(native_backend):
    policy = importlib.import_module("ut_agent.execution_policy")
    messages = _native_finish_messages(_native_build_work_item())
    messages += _tool_exchange("apply_repo_patch_tool", "native-patch", {
        "status": "changed",
        "patch_applied": True,
        "base_sha": "a" * 40,
        "diff_digest": "sha256:" + "b" * 64,
        "changed_files": ["src/example.py"],
    })

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "已修改但尚未提交。"},
    )

    assert accepted is False
    assert "提交并验证新流水线" in reason


def test_native_unavailable_coverage_report_remains_terminal_evidence(native_backend):
    policy = importlib.import_module("ut_agent.execution_policy")
    work_item = {
        "job_id": 42,
        "pipeline_id": 41000,
        "job_name": "x86_64_ut_coverage_check",
        "kind": "coverage",
        "required_tool": "fetch_coverage_report_tool",
    }
    messages = _native_finish_messages(work_item)
    messages += _tool_exchange(
        "fetch_coverage_report_tool",
        "native-coverage-missing",
        {"status": "unknown", "available": False, "reason": "artifact not found"},
        {"job_id": 42},
    )

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "覆盖率报告产物不可用。"},
    )

    assert accepted is True
    assert reason == ""


def test_native_available_coverage_report_requires_native_patch(native_backend):
    policy = importlib.import_module("ut_agent.execution_policy")
    work_item = {
        "job_id": 42,
        "pipeline_id": 41000,
        "job_name": "x86_64_ut_coverage_check",
        "kind": "coverage",
        "required_tool": "fetch_coverage_report_tool",
    }
    messages = _native_finish_messages(work_item)
    messages += _tool_exchange(
        "fetch_coverage_report_tool",
        "native-coverage",
        {"status": "success", "available": True, "files": [{"path": "src/example.py"}]},
        {"job_id": 42},
    )

    accepted, reason = policy.validate_finish(
        {"trigger_type": "pipeline_failed", "messages": messages},
        {"success": False, "summary": "覆盖率不足。"},
    )

    assert accepted is False
    assert "apply_repo_patch_tool" in reason
    assert "generate_code_tool" not in reason


def test_native_failed_validation_pipeline_counts_as_completed_attempt(native_backend):
    from ut_agent.repair_plan import RepairPlan, RepairVerification, RepairWorkItem

    policy = importlib.import_module("ut_agent.execution_policy")
    base_sha = "a" * 40
    commit_sha = "c" * 40
    diff_digest = "sha256:" + "b" * 64
    root_cause_id = "native-build-root"
    work_item = {
        **_native_build_work_item(),
        "root_cause_id": root_cause_id,
        "canonical_job_name": "build_release_arm64",
    }
    source_pipeline = {
        "status": "success",
        "pipeline_id": 41000,
        "pipeline_status": "failed",
        "requested_commit_sha": base_sha,
        "matched_commit_sha": base_sha,
        "failed_jobs": [{"job_id": 41, "name": work_item["job_name"], "status": "failed"}],
        "root_cause_groups": [{
            "root_cause_id": root_cause_id,
            "canonical_diagnostic": "undefined reference to Navigation::start()",
            "job_names": [work_item["job_name"]],
        }],
        "observed_jobs": [{"job_id": 41, "name": work_item["job_name"], "status": "failed"}],
        "work_items": [work_item],
    }
    plan = RepairPlan(
        plan_id="1" * 64,
        lineage_id="2" * 64,
        version=1,
        project_id="group/repo",
        mr_id=1,
        baseline_sha=base_sha,
        source_pipeline_id=41000,
        source_commit_sha=base_sha,
        source_failure_digest="3" * 64,
        evidence_cursor=1,
        created_at="2026-09-02T00:00:00+00:00",
        revision_reason="test",
        planning_mode="deterministic_fallback",
        work_items=(RepairWorkItem(
            work_item_id=root_cause_id,
            job_names=(work_item["job_name"],),
            kind="build",
            required_tool="apply_repo_patch_tool",
            failure_signature=root_cause_id,
            failure_evidence=("undefined reference to Navigation::start()",),
        ),),
    )
    verification = RepairVerification(
        plan_id=plan.plan_id,
        lineage_id=plan.lineage_id,
        plan_version=plan.version,
        work_item_id=root_cause_id,
        baseline_sha=base_sha,
        diff_digest=diff_digest,
        verdict="pass",
        causal_alignment=True,
        scope_compliant=True,
        evidence_sufficient=True,
        covered_work_item_ids=(root_cause_id,),
        reason="the exact Diff passed independent verification",
        created_at="2026-09-02T00:00:00+00:00",
    )
    messages = _tool_exchange("fetch_pipeline_logs_tool", "native-pipeline", source_pipeline)
    messages += _tool_exchange("apply_repo_patch_tool", "native-patch", {
        "status": "changed",
        "patch_applied": True,
        "work_item_id": root_cause_id,
        "base_sha": base_sha,
        "diff_digest": diff_digest,
        "changed_files": ["src/example.py"],
    })
    messages += _tool_exchange("commit_and_push_tool", "native-push", {
        "status": "success",
        "changed": True,
        "attempt_id": "native-attempt-1",
        "attempt_sequence": 1,
        "base_sha": base_sha,
        "diff_digest": diff_digest,
        "commit_sha": commit_sha,
    })
    messages += _tool_exchange("wait_pipeline_tool", "native-pipeline-failed", {
        "status": "success",
        "requested_commit_sha": commit_sha,
        "matched_commit_sha": commit_sha,
        "pipeline_id": 41001,
        "pipeline_status": "failed",
        "failed_jobs": [{"job_id": 43, "name": work_item["job_name"], "status": "failed"}],
        "root_cause_groups": [{
            "root_cause_id": root_cause_id,
            "canonical_diagnostic": "undefined reference to Navigation::start()",
            "job_names": [work_item["job_name"]],
        }],
        "observed_jobs": [{"job_id": 43, "name": work_item["job_name"], "status": "failed"}],
        "work_items": [{**work_item, "job_id": 43, "pipeline_id": 41001}],
    })

    accepted, reason = policy.validate_finish(
        {
            "trigger_type": "pipeline_failed",
            "messages": messages,
            "repair_plans": [plan.model_dump(mode="json")],
            "repair_verifications": [verification.model_dump(mode="json")],
        },
        {
            "success": False,
            "summary": "修复提交已经由精确 SHA 的新流水线验证，但失败仍然存在。",
        },
    )

    assert accepted is True
    assert reason == ""
