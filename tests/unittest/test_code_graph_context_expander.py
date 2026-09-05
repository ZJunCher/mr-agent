import os
import subprocess
import tempfile

import pytest

from pr_agent.algo.code_graph.context_expander import ChangedFile, build_related_files_context
from pr_agent.config_loader import get_settings


class _FakeTokenHandler:
    def count_tokens(self, text: str) -> int:
        return len(text)


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _write(root, relpath, content):
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


@pytest.fixture
def target_branch_repo():
    with tempfile.TemporaryDirectory() as repo_dir:
        _run(["git", "init", "-b", "main"], cwd=repo_dir)
        _run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir)
        _run(["git", "config", "user.name", "Test"], cwd=repo_dir)
        _write(repo_dir, "pkg/__init__.py", "")
        _write(repo_dir, "pkg/reviewer.py", "class Reviewer:\n    pass\n")
        _write(repo_dir, "pkg/consumer.py", "from pkg.reviewer import Reviewer\n\ndef use():\n    return Reviewer()\n")
        _run(["git", "add", "."], cwd=repo_dir)
        _run(["git", "commit", "-m", "initial"], cwd=repo_dir)
        yield repo_dir


@pytest.fixture(autouse=True)
def _code_graph_settings():
    settings = get_settings()
    previous = {
        "enabled": settings.get("pr_reviewer.code_graph.enabled", False),
        "storage_root": settings.get("pr_reviewer.code_graph.storage_root"),
        "token_budget": settings.get("pr_reviewer.code_graph.token_budget"),
    }
    settings.set("pr_reviewer.code_graph.enabled", True)
    yield
    for key, value in previous.items():
        settings.set(f"pr_reviewer.code_graph.{key}", value)


def test_disabled_returns_empty_string(target_branch_repo):
    get_settings().set("pr_reviewer.code_graph.enabled", False)
    changed = [ChangedFile(relpath="pkg/reviewer.py", new_content="class Reviewer:\n    pass\n")]
    with tempfile.TemporaryDirectory() as storage_root:
        get_settings().set("pr_reviewer.code_graph.storage_root", storage_root)
        assert build_related_files_context(changed, target_branch_repo, target_branch_repo, "main", _FakeTokenHandler()) == ""


def test_reverse_dependency_is_included(target_branch_repo):
    changed = [ChangedFile(relpath="pkg/reviewer.py", new_content="class Reviewer:\n    pass\n")]
    with tempfile.TemporaryDirectory() as storage_root:
        get_settings().set("pr_reviewer.code_graph.storage_root", storage_root)
        get_settings().set("pr_reviewer.code_graph.token_budget", 8000)
        result = build_related_files_context(changed, target_branch_repo, target_branch_repo, "main", _FakeTokenHandler())
    assert "Related Files" in result
    assert "pkg/consumer.py" in result


def test_forward_dependency_from_new_content_is_included(target_branch_repo):
    changed = [ChangedFile(relpath="pkg/consumer.py", new_content="from pkg.reviewer import Reviewer\n")]
    with tempfile.TemporaryDirectory() as storage_root:
        get_settings().set("pr_reviewer.code_graph.storage_root", storage_root)
        get_settings().set("pr_reviewer.code_graph.token_budget", 8000)
        result = build_related_files_context(changed, target_branch_repo, target_branch_repo, "main", _FakeTokenHandler())
    assert "pkg/reviewer.py" in result


def test_unsupported_file_extension_returns_empty(target_branch_repo):
    changed = [ChangedFile(relpath="README.md", new_content="# hello\n")]
    with tempfile.TemporaryDirectory() as storage_root:
        get_settings().set("pr_reviewer.code_graph.storage_root", storage_root)
        assert build_related_files_context(changed, target_branch_repo, target_branch_repo, "main", _FakeTokenHandler()) == ""


def test_token_budget_of_zero_excludes_all_files(target_branch_repo):
    changed = [ChangedFile(relpath="pkg/reviewer.py", new_content="class Reviewer:\n    pass\n")]
    with tempfile.TemporaryDirectory() as storage_root:
        get_settings().set("pr_reviewer.code_graph.storage_root", storage_root)
        get_settings().set("pr_reviewer.code_graph.token_budget", 0)
        assert build_related_files_context(changed, target_branch_repo, target_branch_repo, "main", _FakeTokenHandler()) == ""


def test_invalid_clone_url_degrades_to_empty_string():
    changed = [ChangedFile(relpath="pkg/reviewer.py", new_content="class Reviewer:\n    pass\n")]
    with tempfile.TemporaryDirectory() as storage_root:
        get_settings().set("pr_reviewer.code_graph.storage_root", storage_root)
        assert build_related_files_context(
            changed, "/path/does/not/exist", "/path/does/not/exist", "main", _FakeTokenHandler()
        ) == ""
