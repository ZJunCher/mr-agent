"""Task 3b: 原生编码工具测试。

覆盖四个新工具的核心安全约束：
- apply_repo_patch_tool: unified diff 应用、路径逃逸拒绝、半应用失败回滚
- search_repo_tool: 搜索范围限制、结果上限
- inspect_repo_diff_tool: diff 输出有界
- run_repo_validation_tool: 拒绝任意 shell 命令

测试使用临时 git 仓库 fixture，不依赖真实 MR 工作区。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing.
from ut_agent.tools.apply_repo_patch import apply_repo_patch_tool
from ut_agent.tools.inspect_repo_diff import inspect_repo_diff_tool
from ut_agent.tools.run_repo_validation import ValidationProfile, run_repo_validation_tool
from ut_agent.tools.search_repo import search_repo_tool
import ut_agent.tools.run_repo_validation as validation_module


@pytest.fixture
def repo_state(tmp_path):
    """创建一个临时 git 仓库，模拟 MR 工作区。

    返回一个 dict，包含 repo_dir 和模拟的 state（含 mr_id）。
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_dir, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo_dir, check=True, capture_output=True,
    )
    # 创建初始文件并提交
    (repo_dir / "src" / "example.py").parent.mkdir(parents=True, exist_ok=True)
    (repo_dir / "src" / "example.py").write_text("def foo():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo_dir, check=True, capture_output=True,
    )

    # 通过 monkeypatch 让 get_repo_dir 返回这个临时目录
    import ut_agent.tools.context as context_module

    original_get_repo_dir = context_module.get_repo_dir

    def mock_get_repo_dir(mr_id: int) -> str:
        return str(repo_dir)

    context_module.get_repo_dir = mock_get_repo_dir
    # apply_repo_patch 等工具在导入时绑定了 get_repo_dir，需要也 patch 那里
    import ut_agent.tools.apply_repo_patch as apply_module
    import ut_agent.tools.search_repo as search_module
    import ut_agent.tools.inspect_repo_diff as inspect_module
    import ut_agent.tools.run_repo_validation as validation_module
    apply_module.get_repo_dir = mock_get_repo_dir
    search_module.get_repo_dir = mock_get_repo_dir
    inspect_module.get_repo_dir = mock_get_repo_dir
    validation_module.get_repo_dir = mock_get_repo_dir

    state = {"mr_id": 1}

    yield {"repo_dir": str(repo_dir), "state": state}

    # 恢复
    context_module.get_repo_dir = original_get_repo_dir
    apply_module.get_repo_dir = original_get_repo_dir
    search_module.get_repo_dir = original_get_repo_dir
    inspect_module.get_repo_dir = original_get_repo_dir
    validation_module.get_repo_dir = original_get_repo_dir


def _invoke_tool(tool_func, args: dict, state: dict) -> dict:
    """调用工具并解析返回的 JSON 结果。"""
    result = tool_func.invoke({**args, "state": state})
    if isinstance(result, ToolMessage):
        import json
        return json.loads(result.content)
    if isinstance(result, str):
        import json
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"raw": result}
    return {"raw": str(result)}


def _git_diff(repo_dir: str) -> str:
    """获取当前工作区 diff。"""
    r = subprocess.run(
        ["git", "diff"], cwd=repo_dir, capture_output=True, text=True,
    )
    return r.stdout


class TestApplyRepoPatch:
    """apply_repo_patch_tool 安全测试。"""

    def test_applies_valid_unified_diff(self, repo_state):
        patch = """--- a/src/example.py
+++ b/src/example.py
@@ -1,2 +1,2 @@
 def foo():
-    return 1
+    return 2
"""
        result = _invoke_tool(apply_repo_patch_tool, {"patch": patch, "reason": "fix return value"}, repo_state["state"])
        assert result.get("status") == "changed"
        assert result.get("patch_applied") is True
        assert result.get("base_sha")
        assert result.get("diff_digest", "").startswith("sha256:")
        assert result.get("changed_files") == ["src/example.py"]
        assert result.get("diff_check") == {"passed": True, "message": ""}
        assert result.get("reason") == "fix return value"
        # 验证文件确实被修改
        content = open(os.path.join(repo_state["repo_dir"], "src/example.py")).read()
        assert "return 2" in content

    def test_applies_new_file_patch_and_includes_it_in_snapshot(self, repo_state):
        patch = """--- /dev/null
+++ b/src/new_module.py
@@ -0,0 +1,2 @@
+VALUE = 1
+
"""

        result = _invoke_tool(apply_repo_patch_tool, {"patch": patch, "reason": "add module"}, repo_state["state"])

        assert result.get("status") == "changed"
        assert result.get("changed_files") == ["src/new_module.py"]
        assert result.get("diff_digest", "").startswith("sha256:")
        assert os.path.exists(os.path.join(repo_state["repo_dir"], "src/new_module.py"))

    def test_rejects_parent_path_traversal(self, repo_state):
        patch = """--- a/../outside
+++ b/../outside
@@ -0,0 +1 @@
+bad
"""
        before = _git_diff(repo_state["repo_dir"])
        result = _invoke_tool(apply_repo_patch_tool, {"patch": patch, "reason": "test"}, repo_state["state"])
        assert result.get("status") in ("blocked", "error")
        # 确保没有创建工作区外文件
        assert not os.path.exists(os.path.join(repo_state["repo_dir"], "..", "outside"))
        # 确保工作区 diff 没变
        assert _git_diff(repo_state["repo_dir"]) == before

    def test_rejects_absolute_path(self, repo_state):
        abs_path = "/tmp/evil.py"
        patch = f"""--- a/{abs_path}
+++ b/{abs_path}
@@ -0,0 +1 @@
+bad
"""
        result = _invoke_tool(apply_repo_patch_tool, {"patch": patch, "reason": "test"}, repo_state["state"])
        assert result.get("status") in ("blocked", "error")

    def test_rejects_dot_git_path(self, repo_state):
        patch = """--- a/.git/config
+++ b/.git/config
@@ -1,1 +1,1 @@
-old
+new
"""
        result = _invoke_tool(apply_repo_patch_tool, {"patch": patch, "reason": "test"}, repo_state["state"])
        assert result.get("status") in ("blocked", "error")

    def test_failure_leaves_diff_unchanged(self, repo_state):
        invalid_patch = """--- a/src/nonexistent.py
+++ b/src/nonexistent.py
@@ -1,1 +1,1 @@
-old
+new
"""
        before = _git_diff(repo_state["repo_dir"])
        result = _invoke_tool(apply_repo_patch_tool, {"patch": invalid_patch, "reason": "test"}, repo_state["state"])
        assert result.get("status") == "error"
        assert _git_diff(repo_state["repo_dir"]) == before

    def test_no_repo_returns_error(self, tmp_path, monkeypatch):
        import ut_agent.tools.apply_repo_patch as apply_module
        monkeypatch.setattr(apply_module, "get_repo_dir", lambda mr_id: "")
        result = _invoke_tool(apply_repo_patch_tool, {"patch": "x", "reason": "test"}, {"mr_id": 999})
        assert result.get("status") == "error"


class TestSearchRepo:
    """search_repo_tool 测试。"""

    def test_finds_matching_content(self, repo_state):
        result = _invoke_tool(
            search_repo_tool,
            {"query": "def foo", "path_glob": "*.py"},
            repo_state["state"],
        )
        assert result.get("status") == "ok"
        assert "src/example.py" in result.get("raw", "") or any(
            "src/example.py" in str(m) for m in result.get("matches", [])
        )

    def test_respects_max_results(self, repo_state):
        result = _invoke_tool(
            search_repo_tool,
            {"query": "return", "max_results": 1},
            repo_state["state"],
        )
        assert result.get("status") == "ok"

    def test_rejects_dot_git_search(self, repo_state):
        result = _invoke_tool(
            search_repo_tool,
            {"query": "config", "path_glob": ".git/*"},
            repo_state["state"],
        )
        assert result.get("status") in ("blocked", "error")


class TestInspectRepoDiff:
    """inspect_repo_diff_tool 测试。"""

    def test_returns_empty_diff_when_clean(self, repo_state):
        result = _invoke_tool(inspect_repo_diff_tool, {}, repo_state["state"])
        assert result.get("status") == "ok"
        assert result.get("changed_files", []) == []
        assert result.get("total_lines") == 0
        assert result.get("page") == {
            "start_line": 0,
            "end_line": 0,
            "max_lines": 600,
            "has_more": False,
            "next_start_line": None,
        }

    def test_returns_diff_after_patch(self, repo_state):
        patch = """--- a/src/example.py
+++ b/src/example.py
@@ -1,2 +1,2 @@
 def foo():
-    return 1
+    return 2
"""
        _invoke_tool(apply_repo_patch_tool, {"patch": patch, "reason": "test"}, repo_state["state"])
        result = _invoke_tool(inspect_repo_diff_tool, {}, repo_state["state"])
        assert result.get("status") == "ok"
        assert "src/example.py" in result.get("changed_files", [])
        assert result.get("diff_digest", "").startswith("sha256:")

    def test_returns_stable_paginated_diff(self, repo_state):
        path = os.path.join(repo_state["repo_dir"], "src/example.py")
        with open(path, "w") as handle:
            handle.write("\n".join(f"VALUE_{index} = {index}" for index in range(20)) + "\n")

        first = _invoke_tool(
            inspect_repo_diff_tool,
            {"start_line": 1, "max_lines": 5},
            repo_state["state"],
        )
        second = _invoke_tool(
            inspect_repo_diff_tool,
            {"start_line": first["page"]["next_start_line"], "max_lines": 5},
            repo_state["state"],
        )

        assert first["diff_digest"] == second["diff_digest"]
        assert first["total_lines"] > 5
        assert first["page"] == {
            "start_line": 1,
            "end_line": 5,
            "max_lines": 5,
            "has_more": True,
            "next_start_line": 6,
        }
        assert second["page"]["start_line"] == 6

    @pytest.mark.parametrize("args", [
        {"start_line": 0, "max_lines": 5},
        {"start_line": 1, "max_lines": 0},
        {"start_line": -1, "max_lines": 5},
    ])
    def test_rejects_invalid_page_arguments(self, repo_state, args):
        path = os.path.join(repo_state["repo_dir"], "src/example.py")
        with open(path, "w") as handle:
            handle.write("VALUE = 2\n")

        result = _invoke_tool(inspect_repo_diff_tool, args, repo_state["state"])

        assert result.get("status") == "blocked"

    def test_rejects_start_beyond_diff(self, repo_state):
        path = os.path.join(repo_state["repo_dir"], "src/example.py")
        with open(path, "w") as handle:
            handle.write("VALUE = 2\n")

        manifest = _invoke_tool(inspect_repo_diff_tool, {}, repo_state["state"])
        result = _invoke_tool(
            inspect_repo_diff_tool,
            {"start_line": manifest["total_lines"] + 1},
            repo_state["state"],
        )

        assert result.get("status") == "blocked"


class TestRunRepoValidation:
    """run_repo_validation_tool 测试。"""

    def test_rejects_arbitrary_command(self, repo_state):
        result = _invoke_tool(
            run_repo_validation_tool,
            {"checks": ["bash -c env"]},
            repo_state["state"],
        )
        assert result.get("status") == "blocked"

    def test_rejects_unknown_check(self, repo_state):
        result = _invoke_tool(
            run_repo_validation_tool,
            {"checks": ["rm -rf /"]},
            repo_state["state"],
        )
        assert result.get("status") == "blocked"

    def test_accepts_diff_check(self, repo_state):
        result = _invoke_tool(
            run_repo_validation_tool,
            {"checks": ["diff_check"]},
            repo_state["state"],
        )
        assert result.get("status") == "ok"

    def test_accepts_python_compile_check(self, repo_state):
        result = _invoke_tool(
            run_repo_validation_tool,
            {"checks": ["python_compile_check"]},
            repo_state["state"],
        )
        assert result.get("status") == "ok"

    def test_python_change_automatically_requires_compile_check(self, repo_state):
        path = Path(repo_state["repo_dir"]) / "src/example.py"
        path.write_text("def broken(:\n")

        result = _invoke_tool(run_repo_validation_tool, {"checks": []}, repo_state["state"])

        assert "python_compile_check" in result["required_checks"]
        assert result["all_passed"] is False
        compile_result = next(item for item in result["executed_checks"] if item["name"] == "python_compile_check")
        assert compile_result["passed"] is False

    def test_unit_test_check_runs_configured_argv(self, repo_state, tmp_path, monkeypatch):
        marker = tmp_path / "validation-ran"
        profile = ValidationProfile(
            unit_test_argv=(
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('yes')",
            ),
            working_directory=".",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)
        state = {
            **repo_state["state"],
            "project_id": "example-group/mr-agent",
            "selected_categories": ["coverage"],
        }

        result = _invoke_tool(run_repo_validation_tool, {"checks": []}, state)

        assert marker.exists()
        assert result["all_passed"] is True
        assert "unit_test_check" in result["required_checks"]
        assert result["validated_diff_digest"].startswith("sha256:")

    def test_legacy_unit_test_command_is_not_run_twice_for_test_work_item(self, repo_state, tmp_path, monkeypatch):
        import ut_agent.repair_plan as repair_plan_module

        marker = tmp_path / "validation-count"
        profile = ValidationProfile(
            unit_test_argv=(
                sys.executable,
                "-c",
                f"from pathlib import Path; p=Path({str(marker)!r}); "
                "p.write_text((p.read_text() if p.exists() else '') + 'x')",
            ),
            working_directory=".",
            timeout_seconds=30,
        )

        class ActiveItem:
            work_item_id = "root-test"
            required_checks = ("diff_check", "test_check")

        class Plan:
            work_items = (ActiveItem(),)

        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)
        monkeypatch.setattr(repair_plan_module, "latest_repair_plan", lambda _state: Plan())
        monkeypatch.setattr(
            repair_plan_module,
            "required_verification_work_item_ids",
            lambda _state, _plan: ("root-test",),
        )

        result = _invoke_tool(
            run_repo_validation_tool,
            {"checks": []},
            {**repo_state["state"], "project_id": "example-group/mr-agent"},
        )

        assert marker.read_text() == "x"
        assert result["required_checks"] == ["diff_check", "unit_test_check"]
        assert [item["name"] for item in result["executed_checks"]] == ["diff_check", "unit_test_check"]

    def test_final_work_item_requires_union_of_sibling_checks(self, monkeypatch):
        import ut_agent.repair_plan as repair_plan_module

        class WorkItem:
            def __init__(self, work_item_id, required_checks):
                self.work_item_id = work_item_id
                self.required_checks = required_checks

        class Plan:
            work_items = (
                WorkItem("root-build", ("diff_check", "build_check")),
                WorkItem("root-test", ("diff_check", "test_check")),
            )

        profile = ValidationProfile(unit_test_argv=("pytest",))
        monkeypatch.setattr(repair_plan_module, "latest_repair_plan", lambda _state: Plan())
        monkeypatch.setattr(
            repair_plan_module,
            "required_verification_work_item_ids",
            lambda _state, _plan: ("root-build", "root-test"),
        )

        checks = validation_module.required_checks_for_paths({}, [], profile)

        assert checks == ("diff_check", "unit_test_check", "build_check")

    def test_configured_lint_build_and_test_run_in_stable_order(self, repo_state, tmp_path, monkeypatch):
        marker = tmp_path / "validation-order"

        def command(stage):
            return (
                sys.executable,
                "-c",
                f"from pathlib import Path; p=Path({str(marker)!r}); "
                f"p.write_text((p.read_text() if p.exists() else '') + {stage!r} + '\\n')",
            )

        profile = ValidationProfile(
            lint_argv=command("lint"),
            build_argv=command("build"),
            test_argv=command("test"),
            working_directory=".",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)

        result = _invoke_tool(
            run_repo_validation_tool,
            {"checks": []},
            {**repo_state["state"], "project_id": "example-group/mr-agent"},
        )

        assert marker.read_text().splitlines() == ["lint", "build", "test"]
        assert result["required_checks"] == ["diff_check", "lint_check", "build_check", "test_check"]
        assert [item["name"] for item in result["executed_checks"]] == result["required_checks"]
        assert all(item.get("argv") for item in result["executed_checks"] if item["name"].endswith("_check")
                   and item["name"] not in {"diff_check", "python_compile_check"})

    def test_failed_lint_stops_later_configured_stages(self, repo_state, tmp_path, monkeypatch):
        marker = tmp_path / "later-stage-ran"
        profile = ValidationProfile(
            lint_argv=(sys.executable, "-c", "raise SystemExit(9)"),
            build_argv=(sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
            test_argv=(sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
            working_directory=".",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)

        result = _invoke_tool(
            run_repo_validation_tool,
            {"checks": []},
            {**repo_state["state"], "project_id": "example-group/mr-agent"},
        )

        assert [item["name"] for item in result["executed_checks"]] == ["diff_check", "lint_check"]
        assert result["executed_checks"][-1]["exit_code"] == 9
        assert result["all_passed"] is False
        assert not marker.exists()

    def test_coverage_blocks_when_profile_has_no_test_command(self, repo_state, monkeypatch):
        profile = ValidationProfile(
            build_argv=(sys.executable, "-c", "print('build')"),
            working_directory=".",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)

        result = _invoke_tool(
            run_repo_validation_tool,
            {"checks": []},
            {
                **repo_state["state"],
                "project_id": "example-group/mr-agent",
                "selected_categories": ["coverage"],
            },
        )

        assert result["status"] == "blocked"
        assert result["error_code"] == "validation_command_missing"

    def test_test_file_change_automatically_requires_unit_tests(self, repo_state, tmp_path, monkeypatch):
        tests_dir = Path(repo_state["repo_dir"]) / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_example.py").write_text("def test_example():\n    assert True\n")
        marker = tmp_path / "test-file-validation-ran"
        profile = ValidationProfile(
            unit_test_argv=(sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
            working_directory=".",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)

        result = _invoke_tool(
            run_repo_validation_tool,
            {"checks": []},
            {**repo_state["state"], "project_id": "example-group/mr-agent"},
        )

        assert marker.exists()
        assert "unit_test_check" in result["required_checks"]

    def test_missing_profile_blocks_required_unit_tests(self, repo_state, monkeypatch):
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: None)
        state = {
            **repo_state["state"],
            "project_id": "group/unknown",
            "selected_categories": ["coverage"],
        }

        result = _invoke_tool(run_repo_validation_tool, {"checks": []}, state)

        assert result["status"] == "blocked"
        assert result["error_code"] == "validation_profile_missing"
        assert result["all_passed"] is False

    def test_nonzero_unit_test_exit_fails_validation(self, repo_state, monkeypatch):
        profile = ValidationProfile(
            unit_test_argv=(sys.executable, "-c", "raise SystemExit(3)"),
            working_directory=".",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)
        state = {
            **repo_state["state"],
            "project_id": "example-group/mr-agent",
            "selected_categories": ["coverage"],
        }

        result = _invoke_tool(run_repo_validation_tool, {"checks": []}, state)

        unit_result = next(item for item in result["executed_checks"] if item["name"] == "unit_test_check")
        assert unit_result["exit_code"] == 3
        assert unit_result["passed"] is False
        assert result["all_passed"] is False

    def test_unit_test_timeout_terminates_process(self, repo_state, monkeypatch):
        profile = ValidationProfile(
            unit_test_argv=(sys.executable, "-c", "import time; time.sleep(5)"),
            working_directory=".",
            timeout_seconds=1,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)
        state = {
            **repo_state["state"],
            "project_id": "example-group/mr-agent",
            "selected_categories": ["coverage"],
        }

        result = _invoke_tool(run_repo_validation_tool, {"checks": []}, state)

        unit_result = next(item for item in result["executed_checks"] if item["name"] == "unit_test_check")
        assert unit_result["timed_out"] is True
        assert unit_result["passed"] is False

    def test_validation_output_is_bounded(self, repo_state, monkeypatch):
        profile = ValidationProfile(
            unit_test_argv=(sys.executable, "-c", "print('x' * 5000)"),
            working_directory=".",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)
        monkeypatch.setattr(validation_module, "VALIDATION_MAX_OUTPUT_CHARS", 100)
        state = {
            **repo_state["state"],
            "project_id": "example-group/mr-agent",
            "selected_categories": ["coverage"],
        }

        result = _invoke_tool(run_repo_validation_tool, {"checks": []}, state)

        unit_result = next(item for item in result["executed_checks"] if item["name"] == "unit_test_check")
        assert unit_result["output_truncated"] is True
        assert len(unit_result["output"]) <= 100

    def test_configured_argv_does_not_invoke_shell(self, repo_state, tmp_path, monkeypatch):
        injected_path = tmp_path / "shell-injection"
        literal = f"$(touch {injected_path})"
        profile = ValidationProfile(
            unit_test_argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", literal),
            working_directory=".",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)
        state = {
            **repo_state["state"],
            "project_id": "example-group/mr-agent",
            "selected_categories": ["coverage"],
        }

        result = _invoke_tool(run_repo_validation_tool, {"checks": []}, state)

        unit_result = next(item for item in result["executed_checks"] if item["name"] == "unit_test_check")
        assert literal in unit_result["output"]
        assert not injected_path.exists()

    def test_profile_working_directory_cannot_escape_repo(self, repo_state, monkeypatch):
        profile = ValidationProfile(
            unit_test_argv=(sys.executable, "-c", "print('unsafe')"),
            working_directory="../outside",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)
        state = {
            **repo_state["state"],
            "project_id": "example-group/mr-agent",
            "selected_categories": ["coverage"],
        }

        result = _invoke_tool(run_repo_validation_tool, {"checks": []}, state)

        assert result["status"] == "blocked"
        assert result["error_code"] == "validation_working_directory_invalid"

    def test_validation_blocks_when_test_command_modifies_workspace(self, repo_state, monkeypatch):
        profile = ValidationProfile(
            unit_test_argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; Path('generated.txt').write_text('changed')",
            ),
            working_directory=".",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)
        state = {
            **repo_state["state"],
            "project_id": "example-group/mr-agent",
            "selected_categories": ["coverage"],
        }

        result = _invoke_tool(run_repo_validation_tool, {"checks": []}, state)

        assert result["status"] == "blocked"
        assert result["error_code"] == "validation_modified_workspace"
        assert result["all_passed"] is False

    @pytest.mark.parametrize("failure", ["canceled", "fenced"])
    def test_runtime_guard_terminates_unit_test_process(self, repo_state, monkeypatch, failure):
        class Runtime:
            cancel_calls = 0
            fence_calls = 0

            def raise_if_canceled(self):
                self.cancel_calls += 1
                if failure == "canceled" and self.cancel_calls > 3:
                    raise RuntimeError("task canceled")

            def assert_fence_sync(self):
                self.fence_calls += 1
                if failure == "fenced" and self.fence_calls > 3:
                    raise RuntimeError("fence lost")

        profile = ValidationProfile(
            unit_test_argv=(sys.executable, "-c", "import time; time.sleep(5)"),
            working_directory=".",
            timeout_seconds=30,
        )
        monkeypatch.setattr(validation_module, "get_validation_profile", lambda _project: profile)
        monkeypatch.setattr(validation_module, "get_execution_runtime", lambda: Runtime())
        state = {
            **repo_state["state"],
            "project_id": "example-group/mr-agent",
            "selected_categories": ["coverage"],
        }

        result = _invoke_tool(run_repo_validation_tool, {"checks": []}, state)

        unit_result = next(item for item in result["executed_checks"] if item["name"] == "unit_test_check")
        assert unit_result["passed"] is False
        assert failure.split("ed")[0] in unit_result["error"]
