import asyncio
import hashlib
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.distributed.broker import EffectRecord, MrLease
from pr_agent.distributed.models import MrKey
from pr_agent.distributed.runtime import ExecutionRuntime, execution_context
from pr_agent.feishu.feishu_git_provider import FeishuGitProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools.pr_fix_format import _REPORT_LINE_RE, _REPORT_URL_RE, PRFixFormat
from pr_agent.triage.repair_rollback import RepairCommitEntry, RepairCommitManifest


@pytest.fixture
def tool():
    """构造一个 PRFixFormat 实例，绕过真实 GitLab 连接（仅测试纯逻辑方法）。"""
    with patch(
        "pr_agent.tools.pr_fix_format.get_git_provider_with_context",
        return_value=MagicMock(),
    ):
        return PRFixFormat("https://gitlab.example.com/group/proj/-/merge_requests/1")


def test_run_accepts_feishu_wrapped_gitlab_provider():
    original_provider = GitLabProvider.__new__(GitLabProvider)
    original_provider.gl = MagicMock()
    original_provider.id_project = "group/project"
    original_provider.get_pr_branch = MagicMock(return_value="feature")
    wrapped_provider = FeishuGitProvider.__new__(FeishuGitProvider)
    wrapped_provider.original_provider = original_provider

    with patch("pr_agent.tools.pr_fix_format.get_git_provider_with_context", return_value=wrapped_provider):
        format_tool = PRFixFormat("https://gitlab.example.com/group/proj/-/merge_requests/1")
    format_tool._resolve_pipeline = MagicMock(return_value=None)

    asyncio.run(format_tool.run())

    format_tool._resolve_pipeline.assert_called_once()


def test_run_reports_ci_job_configuration_failure_without_commit():
    original_provider = GitLabProvider.__new__(GitLabProvider)
    original_provider.gl = MagicMock()
    original_provider.id_project = "group/project"
    original_provider.get_pr_branch = MagicMock(return_value="feature")
    project = MagicMock()
    original_provider.gl.projects.get.return_value = project
    full_job = MagicMock()
    full_job.trace.return_value = (
        b"ERROR: git diff failed: fatal: ambiguous argument '': unknown revision or path not in the working tree.\n"
    )
    full_job.artifact.side_effect = Exception("404")
    project.jobs.get.return_value = full_job
    failed_job = SimpleNamespace(
        id=108359,
        name="code_format_check",
        status="failed",
        web_url="https://gitlab.example/group/project/-/jobs/108359",
    )

    with patch("pr_agent.tools.pr_fix_format.get_git_provider_with_context", return_value=original_provider):
        format_tool = PRFixFormat("https://gitlab.example.com/group/proj/-/merge_requests/1")
    format_tool._resolve_pipeline = MagicMock(return_value=SimpleNamespace(id=33603))
    format_tool._collect_all_pipelines = MagicMock(return_value=[SimpleNamespace(id=33603)])
    format_tool._find_failed_format_jobs = MagicMock(return_value=[failed_job])
    format_tool._commit_changes = MagicMock()

    result = asyncio.run(format_tool.run(publish_result=False))

    assert result.pushed_sha == ""
    assert result.failure_kind == "ci_job_configuration"
    assert "基准 Commit 为空" in result.failure_summary
    assert result.job_url.endswith("/jobs/108359")
    assert "[查看 Job 日志]" in result.status_markdown
    assert full_job.trace.call_count == 1
    format_tool._commit_changes.assert_not_called()


def test_repeated_format_report_stops_before_commit():
    original_provider = GitLabProvider.__new__(GitLabProvider)
    original_provider.gl = MagicMock()
    original_provider.id_project = "group/project"
    original_provider.get_pr_branch = MagicMock(return_value="feature")
    project = MagicMock()
    original_provider.gl.projects.get.return_value = project
    failed_job = SimpleNamespace(id=1, name="code_format_check", status="failed", web_url="")
    report = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+a = 1\n"
    fingerprint = hashlib.sha256(report.encode("utf-8")).hexdigest()[:32]

    with patch("pr_agent.tools.pr_fix_format.get_git_provider_with_context", return_value=original_provider):
        format_tool = PRFixFormat(
            "https://gitlab.example.com/group/proj/-/merge_requests/1",
            seen_report_fingerprints=(fingerprint,),
        )
    format_tool._resolve_pipeline = MagicMock(return_value=SimpleNamespace(id=1))
    format_tool._collect_all_pipelines = MagicMock(return_value=[SimpleNamespace(id=1)])
    format_tool._find_failed_format_jobs = MagicMock(return_value=[failed_job])
    format_tool._get_job_trace = MagicMock(return_value="")
    format_tool._get_report_text = MagicMock(return_value=report)
    format_tool._commit_changes = MagicMock()

    result = asyncio.run(format_tool.run(publish_result=False))

    assert result.failure_kind == "repeated_report"
    assert result.report_fingerprint == fingerprint
    assert result.pushed_sha == ""
    format_tool._commit_changes.assert_not_called()


class TestParseAndApplyDiff:
    def test_single_file_replace(self, tool):
        report = (
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -1,3 +1,3 @@\n"
            " def foo():\n"
            "-    x=1\n"
            "+    x = 1\n"
            "     return x\n"
        )
        files = tool._parse_unified_diff(report)
        assert "src/foo.py" in files
        original = "def foo():\n    x=1\n    return x\n"
        fixed = tool._apply_hunks(original, files["src/foo.py"])
        assert fixed == "def foo():\n    x = 1\n    return x\n"

    def test_multi_file_diff(self, tool):
        report = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-a=1\n"
            "+a = 1\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-b=2\n"
            "+b = 2\n"
        )
        files = tool._parse_unified_diff(report)
        assert set(files.keys()) == {"a.py", "b.py"}
        assert tool._apply_hunks("a=1\n", files["a.py"]) == "a = 1\n"
        assert tool._apply_hunks("b=2\n", files["b.py"]) == "b = 2\n"

    def test_clang_format_style_header(self, tool):
        # clang-format --diff 使用 tab 分隔的注释头，且路径无 a//b/ 前缀
        report = (
            "--- src/main.cpp\t(before formatting)\n"
            "+++ src/main.cpp\t(after formatting)\n"
            "@@ -1,1 +1,1 @@\n"
            "-int  x;\n"
            "+int x;\n"
        )
        files = tool._parse_unified_diff(report)
        assert "src/main.cpp" in files
        assert tool._apply_hunks("int  x;\n", files["src/main.cpp"]) == "int x;\n"

    def test_git_diff_style_header(self, tool):
        report = (
            "diff --git a/src/main.cpp b/src/main.cpp\n"
            "index abc123..def456 100644\n"
            "--- a/src/main.cpp\n"
            "+++ b/src/main.cpp\n"
            "@@ -1,1 +1,1 @@\n"
            "-int  y;\n"
            "+int y;\n"
        )
        files = tool._parse_unified_diff(report)
        assert "src/main.cpp" in files
        assert tool._apply_hunks("int  y;\n", files["src/main.cpp"]) == "int y;\n"

    def test_parenthesis_space_header(self, tool):
        report = (
            "--- src/x.py (original)\n"
            "+++ src/x.py (reformatted)\n"
            "@@ -1 +1 @@\n"
            "-z=3\n"
            "+z = 3\n"
        )
        files = tool._parse_unified_diff(report)
        assert "src/x.py" in files

    def test_apply_returns_none_when_delete_line_mismatch(self, tool):
        report = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-not_the_real_line\n"
            "+fixed\n"
        )
        files = tool._parse_unified_diff(report)
        # 源文件与 diff 的删除行不一致，应放弃修复（返回 None）而不是破坏文件
        assert tool._apply_hunks("real_line\n", files["f.py"]) is None

    def test_preserve_no_trailing_newline(self, tool):
        report = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-a=1\n"
            "+a = 1\n"
        )
        files = tool._parse_unified_diff(report)
        # 原文无末尾换行，修复后也不应引入末尾换行
        assert tool._apply_hunks("a=1", files["f.py"]) == "a = 1"

    def test_apply_diff_can_add_missing_final_newline(self, tool):
        report = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-raise SystemExit(main())\n"
            "\\ No newline at end of file\n"
            "+raise SystemExit(main())\n"
        )

        files = tool._parse_unified_diff(report)

        assert tool._apply_hunks("raise SystemExit(main())", files["f.py"]) == "raise SystemExit(main())\n"

    def test_extract_path_variants(self):
        assert PRFixFormat._extract_path("b/src/foo.py") == "src/foo.py"
        assert PRFixFormat._extract_path("a/src/foo.py") == "src/foo.py"
        assert PRFixFormat._extract_path("src/foo.py\t(reformatted)") == "src/foo.py"
        assert PRFixFormat._extract_path("src/foo.py (original)") == "src/foo.py"

    def test_extract_path_strips_ci_absolute_prefix(self):
        # CI 构建目录的绝对路径应被归一化为仓库相对路径
        assert PRFixFormat._extract_path("/builds/eabot/cook/src/foo.py") == "src/foo.py"
        assert PRFixFormat._extract_path("b/builds/eabot/cook/src/foo.py") == "src/foo.py"
        # 末尾不带斜杠的 CI 前缀
        assert PRFixFormat._extract_path("/builds/eabot/cook/main.py") == "main.py"
        # 相对路径保持不变（回归保护）
        assert PRFixFormat._extract_path("src/foo.py") == "src/foo.py"


class TestRealReportFormat:
    """基于 code_format_check 真实产出的 code-format-report.txt 结构的测试。"""

    REPORT = (
        "code format 检查报告\n"
        "============================================================\n"
        "C++ 变更文件数: 2\n"
        "\n"
        "检查总结:\n"
        "- clang-format: ❌ 失败 (检查 2，失败 1)\n"
        "- black: ✅ 通过 (检查 0，失败 0)\n"
        "\n"
        "问题明细:\n"
        "- src/mod/src/impl.cpp: changed lines need clang-format\n"
        "- /builds/eabot/cook/src/mod/src/impl.cpp:12:61: error: code should be clang-formatted"
        " [-Wclang-format-violations]\n"
        "Foo::Foo(rclcpp::Node* node)\n"
        "                            ^\n"
        "- --- a/src/mod/src/impl.cpp\n"
        "+++ b/src/mod/src/impl.cpp (clang-format)\n"
        "@@ -1,4 +1,3 @@\n"
        " int a;\n"
        "-Foo::Foo(rclcpp::Node* node)\n"
        "-    : node_(node) {\n"
        "+Foo::Foo(rclcpp::Node* node) : node_(node) {\n"
        " int b;\n"
        "\n"
        "- src/mod/test/t.cpp: changed lines need clang-format\n"
        "- --- a/src/mod/test/t.cpp\n"
        "+++ b/src/mod/test/t.cpp (clang-format)\n"
        "@@ -1,2 +1,1 @@\n"
        "-int  x\n"
        "-  ;\n"
        "+int x;\n"
    )

    def test_parses_files_from_real_report(self, tool):
        files = tool._parse_unified_diff(self.REPORT)
        assert set(files.keys()) == {"src/mod/src/impl.cpp", "src/mod/test/t.cpp"}

    def test_apply_fix_from_real_report(self, tool):
        files = tool._parse_unified_diff(self.REPORT)
        original1 = "int a;\nFoo::Foo(rclcpp::Node* node)\n    : node_(node) {\nint b;\n"
        fixed1 = tool._apply_hunks(original1, files["src/mod/src/impl.cpp"])
        assert fixed1 == "int a;\nFoo::Foo(rclcpp::Node* node) : node_(node) {\nint b;\n"

        original2 = "int  x\n  ;\n"
        fixed2 = tool._apply_hunks(original2, files["src/mod/test/t.cpp"])
        assert fixed2 == "int x;\n"

    def test_error_detail_noise_lines_are_ignored(self, tool):
        # "问题明细"里的错误行、代码片段、^ 指示行不应被误解析成 diff
        files = tool._parse_unified_diff(self.REPORT)
        hunks1 = files["src/mod/src/impl.cpp"]
        assert len(hunks1) == 1
        tags = [tag for tag, _ in hunks1[0]["lines"]]
        assert tags == [" ", "-", "-", "+", " "]


class TestReportUrlRegex:
    def test_extract_job_and_artifact_from_log(self):
        log = (
            "Summary:\n"
            "- clang-format: FAILED (checked=26, failed=16)\n"
            "\u274c code format check failed\n"
            "\u8be6\u60c5\u89c1\u62a5\u544a: https://gitlab.example.com/eabot/cook/-/jobs/33956/"
            "artifacts/raw/code-format-report.txt\n"
            "ERROR: Job failed: command terminated with exit code 1\n"
        )
        m = _REPORT_URL_RE.search(log)
        assert m is not None
        assert m.group(1) == "33956"
        assert m.group(2) == "code-format-report.txt"


class TestReportLineRegex:
    def test_extract_report_link_from_detail_line(self):
        # 真实日志样例：“详情见报告:”行后跟链接，下一行是“参考文档:”飞书链接
        log = (
            "Summary:\n"
            "- clang-format: FAILED (checked=26, failed=16)\n"
            "- black: FAILED (checked=8, failed=4)\n"
            "❌ code format check failed\n"
            "详情见报告: https://gitlab.example.com/eabot/cook/-/jobs/33956/"
            "artifacts/raw/code-format-report.txt\n"
            "参考文档: https://vcncw01cnbip.feishu.cn/wiki/NMFzwstpFi8YIQk3euXc3iWCn7d\n"
            "ERROR: Job failed: command terminated with exit code 1\n"
        )
        m = _REPORT_LINE_RE.search(log)
        assert m is not None
        # 只取报告链接，不该误取“参考文档:”的飞书链接
        assert m.group(1) == (
            "https://gitlab.example.com/eabot/cook/-/jobs/33956/artifacts/raw/code-format-report.txt"
        )
        assert "feishu.cn" not in m.group(1)

    def test_full_width_colon_supported(self):
        log = "详情见报告：https://gitlab.x/g/p/-/jobs/1/artifacts/raw/r.txt\n"
        m = _REPORT_LINE_RE.search(log)
        assert m is not None
        assert m.group(1) == "https://gitlab.x/g/p/-/jobs/1/artifacts/raw/r.txt"


class TestReportMissingDetail:
    def test_build_comment_includes_job_failure_detail(self, tool):
        comment = tool._build_comment([], [], report_missing=True, detail="fatal: bad object 7b292e05")
        assert "fatal: bad object 7b292e05" in comment
        assert "job 自身执行失败" in comment

    def test_build_comment_without_detail_keeps_original_text(self, tool):
        comment = tool._build_comment([], [], report_missing=True)
        assert "未能获取或解析格式报告" in comment

    def test_job_failure_hint_extracts_first_error_line(self, tool):
        full_job = MagicMock()
        full_job.trace.return_value = (
            b"Checking...\n"
            b"fatal: bad object 7b292e05202822bbdd561392cb9e3714e4e943f3\n"
            b"ERROR: Job failed\n"
        )
        project = MagicMock()
        project.jobs.get.return_value = full_job
        job = MagicMock()
        job.id = 90243
        assert "bad object 7b292e05" in tool._job_failure_hint(project, job)
        project.jobs.get.assert_called_once_with(90243)

    def test_job_failure_hint_tolerates_trace_error(self, tool):
        project = MagicMock()
        project.jobs.get.side_effect = Exception("boom")
        assert tool._job_failure_hint(project, MagicMock()) == ""


def test_commit_changes_returns_created_commit_sha(tool):
    project = MagicMock()
    project.branches.get.return_value.commit = {"id": "a" * 40}
    base_commit = MagicMock(tree_id="c" * 40)
    pushed_commit = MagicMock(
        tree_id="d" * 40,
        parent_ids=["a" * 40],
        message="style: 自动修复代码格式 [format-bot]",
    )
    project.commits.get.side_effect = [base_commit, pushed_commit, pushed_commit]
    project.commits.create.return_value.id = "b" * 40

    result = tool._commit_changes(project, "feature", {"src/a.cpp": "fixed"})

    assert result.pushed_sha == "b" * 40
    assert project.commits.create.call_args.args[0]["actions"][0]["last_commit_id"] == "a" * 40


def test_git_tree_hash_matches_git_directory_sorting(tool, tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitlab").mkdir()
    (tmp_path / ".gitlab" / "child.yml").write_text("child\n", encoding="utf-8")
    (tmp_path / ".gitlab-ci.yml").write_text("pipeline\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
        cwd=tmp_path,
        check=True,
    )
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    lines = subprocess.run(
        ["git", "ls-tree", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    entries = []
    for line in reversed(lines):
        metadata, name = line.split("\t", 1)
        mode, entry_type, object_id = metadata.split()
        entries.append({"mode": mode, "type": entry_type, "id": object_id, "name": name})
    project = MagicMock()
    project.commits.get.return_value = MagicMock(tree_id="")
    project.repository_tree.return_value = entries

    assert tool._commit_tree_sha(project, "a" * 40) == expected


def test_format_commit_after_repair_keeps_original_manifest_base_tree(tool):
    base_sha = "a" * 40
    repair_sha = "b" * 40
    format_sha = "c" * 40
    base_tree_sha = "d" * 40
    repair_tree_sha = "e" * 40
    format_tree_sha = "f" * 40
    first_entry = RepairCommitEntry(
        sequence=1,
        commit_sha=repair_sha,
        parent_sha=base_sha,
        tree_sha=repair_tree_sha,
        effect_id="repair-effect",
        task_marker="[pr-agent-task:task-549:push-attempt:1:marker]",
        pushed_at="2026-08-18T08:28:18+00:00",
    )
    manifest = RepairCommitManifest(
        repair_task_id="task-549",
        project_id="eabot/cook",
        mr_iid=549,
        source_branch="feature/fix",
        base_commit_sha=base_sha,
        base_tree_sha=base_tree_sha,
        authorized_actor_id="ou_owner",
        entries=(first_entry,),
    )
    sync_broker = MagicMock()
    sync_broker.is_cancel_requested.return_value = False
    sync_broker.assert_fence = MagicMock()
    sync_broker.get_task_triage_card.return_value = SimpleNamespace(receive_id="ou_owner")
    sync_broker.get_repair_commit_manifest.return_value = manifest
    sync_broker.claim_effect.return_value = EffectRecord("started", {})
    sync_broker.update_effect_metadata.return_value = True
    sync_broker.complete_effect.return_value = True
    sync_broker.append_repair_commit.side_effect = lambda _task_id, entry, **_kwargs: replace(
        manifest,
        entries=(first_entry, entry),
    )
    runtime = ExecutionRuntime(
        "task-549",
        "worker-1",
        MrLease(MrKey("eabot/cook", 549), "worker-1", 7),
        "queue",
        AsyncMock(),
        sync_broker,
    )

    project = MagicMock()
    project.branches.get.return_value.commit = {"id": repair_sha}
    repair_commit = MagicMock(tree_id=repair_tree_sha)
    format_commit = MagicMock(
        tree_id=format_tree_sha,
        parent_ids=[repair_sha],
        message="",
    )
    project.commits.get.side_effect = lambda sha: repair_commit if sha == repair_sha else format_commit

    def create_commit(payload):
        format_commit.message = payload["commit_message"]
        project.branches.get.return_value.commit = {"id": format_sha}
        return SimpleNamespace(id=format_sha)

    project.commits.create.side_effect = create_commit

    with execution_context(runtime):
        result = tool._commit_changes(project, "feature/fix", {"src/a.cpp": "fixed\n"})

    assert result.pushed_sha == format_sha
    appended_entry = sync_broker.append_repair_commit.call_args.args[1]
    assert appended_entry.sequence == 2
    assert appended_entry.parent_sha == repair_sha
    assert sync_broker.append_repair_commit.call_args.kwargs["base_tree_sha"] == base_tree_sha


def test_format_commit_reconciles_transient_pre_push_parent_tree_mismatch(tool):
    base_sha = "a" * 40
    pushed_sha = "b" * 40
    stale_tree_sha = "c" * 40
    authoritative_tree_sha = "d" * 40
    pushed_tree_sha = "e" * 40
    sync_broker = MagicMock()
    sync_broker.is_cancel_requested.return_value = False
    sync_broker.assert_fence = MagicMock()
    sync_broker.get_task_triage_card.return_value = SimpleNamespace(receive_id="ou_owner")
    sync_broker.get_repair_commit_manifest.return_value = None
    sync_broker.claim_effect.return_value = EffectRecord("started", {})
    sync_broker.update_effect_metadata.return_value = True
    sync_broker.complete_effect.return_value = True
    runtime = ExecutionRuntime(
        "task-15",
        "worker-1",
        MrLease(MrKey("eabot/map_nav_loc", 15), "worker-1", 7),
        "queue",
        AsyncMock(),
        sync_broker,
    )
    project = MagicMock()
    project.branches.get.return_value.commit = {"id": base_sha}
    base_fetches = 0
    pushed_message = ""

    def get_commit(sha):
        nonlocal base_fetches
        if sha == base_sha:
            base_fetches += 1
            return MagicMock(tree_id=stale_tree_sha if base_fetches == 1 else authoritative_tree_sha)
        return MagicMock(
            tree_id=pushed_tree_sha,
            parent_ids=[base_sha],
            message=pushed_message,
        )

    project.commits.get.side_effect = get_commit

    def create_commit(payload):
        nonlocal pushed_message
        pushed_message = payload["commit_message"]
        project.branches.get.return_value.commit = {"id": pushed_sha}
        return SimpleNamespace(id=pushed_sha)

    project.commits.create.side_effect = create_commit

    with execution_context(runtime):
        result = tool._commit_changes(project, "feature/fix", {"src/a.cpp": "fixed\n"})

    assert result.pushed_sha == pushed_sha
    assert sync_broker.append_repair_commit.call_args.kwargs["base_tree_sha"] == authoritative_tree_sha


def test_format_commit_retries_ledger_after_remote_push_is_confirmed(tool):
    base_sha = "a" * 40
    pushed_sha = "b" * 40
    base_tree_sha = "c" * 40
    pushed_tree_sha = "d" * 40
    sync_broker = MagicMock()
    sync_broker.is_cancel_requested.return_value = False
    sync_broker.assert_fence = MagicMock()
    sync_broker.get_task_triage_card.return_value = SimpleNamespace(receive_id="ou_owner")
    sync_broker.get_repair_commit_manifest.return_value = None
    sync_broker.claim_effect.return_value = EffectRecord("started", {})
    sync_broker.update_effect_metadata.return_value = True
    sync_broker.complete_effect.return_value = True
    sync_broker.append_repair_commit.side_effect = [RuntimeError("redis reply lost"), MagicMock()]
    runtime = ExecutionRuntime(
        "task-549",
        "worker-1",
        MrLease(MrKey("eabot/cook", 549), "worker-1", 7),
        "queue",
        AsyncMock(),
        sync_broker,
    )
    project = MagicMock()
    project.branches.get.return_value.commit = {"id": base_sha}
    base_commit = MagicMock(tree_id=base_tree_sha)
    pushed_commit = MagicMock(
        tree_id=pushed_tree_sha,
        parent_ids=[base_sha],
        message="",
        diff=MagicMock(return_value=[]),
    )
    project.commits.get.side_effect = lambda sha: base_commit if sha == base_sha else pushed_commit

    def create_commit(payload):
        pushed_commit.message = payload["commit_message"]
        pushed_commit.diff.return_value = [{"new_path": "src/a.cpp", "old_path": "src/a.cpp"}]
        project.branches.get.return_value.commit = {"id": pushed_sha}
        return SimpleNamespace(id=pushed_sha)

    project.commits.create.side_effect = create_commit

    with execution_context(runtime):
        result = tool._commit_changes(project, "feature/fix", {"src/a.cpp": "fixed\n"})

    assert result.pushed_sha == pushed_sha
    assert sync_broker.append_repair_commit.call_count == 2
    sync_broker.complete_effect.assert_called_once()


class TestCommandRegistration:
    def test_fix_format_command_aliases_registered(self):
        # /fix-format 手动命令与下划线别名都映射到 PRFixFormat
        from pr_agent.agent.pr_agent import command2class

        assert command2class.get("fix-format") is PRFixFormat
        assert command2class.get("fix_format") is PRFixFormat
