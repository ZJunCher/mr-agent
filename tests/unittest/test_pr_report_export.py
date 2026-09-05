from unittest.mock import MagicMock

from pr_agent.algo.types import FilePatchInfo
from pr_agent.tools.pr_report_export import _build_diff_text


def test_build_diff_text_single_file():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = [
        FilePatchInfo(
            base_file="old content",
            head_file="new content",
            patch="@@ -1,2 +1,2 @@\n-old line\n+new line\n",
            filename="src/foo.py",
        )
    ]
    diff_text = _build_diff_text(git_provider)
    assert "diff --git a/src/foo.py b/src/foo.py" in diff_text
    assert "@@ -1,2 +1,2 @@" in diff_text
    assert "-old line" in diff_text
    assert "+new line" in diff_text


def test_build_diff_text_renamed_file_uses_old_and_new_names():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = [
        FilePatchInfo(
            base_file="",
            head_file="",
            patch="@@ -1 +1 @@\n-x\n+y\n",
            filename="src/new_name.py",
            old_filename="src/old_name.py",
        )
    ]
    diff_text = _build_diff_text(git_provider)
    assert "diff --git a/src/old_name.py b/src/new_name.py" in diff_text


def test_build_diff_text_multiple_files_joined():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = [
        FilePatchInfo(base_file="", head_file="", patch="@@ -1 +1 @@\n-a\n+b\n", filename="a.py"),
        FilePatchInfo(base_file="", head_file="", patch="@@ -1 +1 @@\n-c\n+d\n", filename="b.py"),
    ]
    diff_text = _build_diff_text(git_provider)
    assert diff_text.index("diff --git a/a.py b/a.py") < diff_text.index("diff --git a/b.py b/b.py")


def test_build_diff_text_empty_files_list_returns_empty_string():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = []
    assert _build_diff_text(git_provider) == ""


from pr_agent.tools.pr_report_export import _build_report_markdown


def _make_git_provider(title="Fix the bug", pr_id="http://gl/mr/1",
                       source_branch="feature/x", target_branch="main"):
    git_provider = MagicMock()
    git_provider.get_title.return_value = title
    git_provider.get_pr_id.return_value = pr_id
    git_provider.get_pr_branch.return_value = source_branch
    git_provider.mr = MagicMock()
    git_provider.mr.target_branch = target_branch
    return git_provider


def test_build_report_markdown_includes_pr_metadata():
    git_provider = _make_git_provider()
    report = _build_report_markdown(git_provider, "## Review\nsome content", "diff --git a/x b/x\n@@ ...")
    assert "Fix the bug" in report
    assert "http://gl/mr/1" in report
    assert "feature/x" in report
    assert "main" in report


def test_build_report_markdown_includes_summary_and_diff():
    git_provider = _make_git_provider()
    report = _build_report_markdown(git_provider, "## Review\nsome content", "diff --git a/x b/x\n@@ ...")
    assert "## Review\nsome content" in report
    assert "diff --git a/x b/x" in report
    assert "```diff" in report


def test_build_report_markdown_handles_missing_metadata_gracefully():
    git_provider = MagicMock()
    git_provider.get_title.side_effect = Exception("boom")
    git_provider.get_pr_id.side_effect = Exception("boom")
    git_provider.get_pr_branch.side_effect = Exception("boom")
    git_provider.mr = MagicMock()
    del git_provider.mr.target_branch  # simulate missing attribute
    report = _build_report_markdown(git_provider, "## Review\nsome content", "diff text")
    assert "## Review\nsome content" in report
    assert "diff text" in report


from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_report_export import _build_report_filename, build_and_upload_report


def _make_full_git_provider(mr_iid=7, head_sha="deadbeef1234"):
    git_provider = _make_git_provider()
    git_provider.get_diff_files.return_value = [
        FilePatchInfo(base_file="", head_file="", patch="@@ -1 +1 @@\n-a\n+b\n", filename="a.py"),
    ]
    git_provider.id_project = 42
    git_provider.id_mr = mr_iid
    git_provider.mr.diff_refs = {"head_sha": head_sha}
    git_provider.gl = MagicMock()
    git_provider.gl.url = "https://gitlab.example.com"
    project = MagicMock()
    project.id = 1
    project.web_url = "https://gitlab.example.com/eabot/chogori"
    project.upload.return_value = {"url": f"/uploads/abc123/report_mr{mr_iid}_{head_sha[:8]}.md"}
    git_provider.gl.projects.get.return_value = project
    return git_provider, project


def test_build_report_filename_uses_mr_iid_and_short_head_sha():
    git_provider = _make_git_provider()
    git_provider.id_mr = 7
    git_provider.mr.diff_refs = {"head_sha": "deadbeef1234567"}
    filename = _build_report_filename(git_provider)
    assert filename == "report_mr7_deadbeef.md"


def test_build_report_filename_falls_back_to_timestamp_without_head_sha():
    git_provider = _make_git_provider()
    git_provider.id_mr = 9
    git_provider.mr.diff_refs = {}
    filename = _build_report_filename(git_provider)
    assert filename.startswith("report_mr9_")
    assert filename.endswith(".md")


def test_build_report_filename_falls_back_to_random_suffix_without_mr_iid():
    git_provider = MagicMock()
    git_provider.id_mr = None
    git_provider.mr = MagicMock()
    del git_provider.mr.diff_refs
    filename = _build_report_filename(git_provider)
    assert filename.startswith("report_")
    assert filename.endswith(".md")


def test_build_and_upload_report_returns_url_and_filename_on_success():
    get_settings().set("pr_mr_create.report_export.enabled", True)
    git_provider, project = _make_full_git_provider(mr_iid=7, head_sha="deadbeef1234")
    result = build_and_upload_report(git_provider, "## Review\nbody")
    download_url, filename = result
    # Must be the API-style route (/api/v4/projects/<id>/uploads/<secret>/<filename>),
    # NOT the web-style route (<web_url>/uploads/<secret>/<filename>) -- the web
    # route was observed to 404 for logged-in project members on at least one
    # self-hosted GitLab instance, while the API route (which also honors a
    # logged-in browser session, not just an API token) worked for the same file.
    assert download_url == "https://gitlab.example.com/api/v4/projects/1/uploads/abc123/report_mr7_deadbeef.md"
    assert filename == "report_mr7_deadbeef.md"
    project.upload.assert_called_once()
    call_kwargs = project.upload.call_args
    assert call_kwargs[0][0] == "report_mr7_deadbeef.md"
    assert b"## Review\nbody" in call_kwargs[1]["filedata"]


def test_build_and_upload_report_returns_none_when_disabled():
    get_settings().set("pr_mr_create.report_export.enabled", False)
    git_provider, project = _make_full_git_provider()
    result = build_and_upload_report(git_provider, "## Review\nbody")
    assert result is None
    project.upload.assert_not_called()


def test_build_and_upload_report_returns_none_on_upload_exception():
    get_settings().set("pr_mr_create.report_export.enabled", True)
    git_provider, project = _make_full_git_provider()
    project.upload.side_effect = Exception("network error")
    result = build_and_upload_report(git_provider, "## Review\nbody")
    assert result is None


def test_build_and_upload_report_returns_none_on_diff_fetch_exception():
    get_settings().set("pr_mr_create.report_export.enabled", True)
    git_provider, project = _make_full_git_provider()
    git_provider.get_diff_files.side_effect = Exception("api down")
    result = build_and_upload_report(git_provider, "## Review\nbody")
    assert result is None
    project.upload.assert_not_called()
