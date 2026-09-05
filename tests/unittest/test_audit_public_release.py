import subprocess
from pathlib import Path

from scripts.audit_public_release import load_denylist, scan_history, scan_tree


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> str:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release-test@users.noreply.github.com")
    (repo / "README.md").write_text("safe\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return _git(repo, "rev-parse", "HEAD")


def test_scan_tree_reports_forbidden_paths_and_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    forbidden_cases = {
        "runtime.log": "pipeline output",
        "resume.pdf": "%PDF-personal-document",
        "settings.toml": 'api_key = "' + "sk-" + 'live-example-value"',
        "deploy.md": "https://git." + "inter" + "nal.example.net/group/repo",
        "debug.txt": "/" + "Users/example/private/project",
        "private.pem": "-----BEGIN " + "PRIVATE KEY-----",
    }
    for name, content in forbidden_cases.items():
        (repo / name).write_text(content, encoding="utf-8")

    findings = scan_tree(repo, ())

    assert {finding.path for finding in findings} == set(forbidden_cases)
    assert all(finding.revision == "WORKTREE" for finding in findings)


def test_scan_tree_allows_documented_safe_examples(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / ".secrets_template.toml").write_text('api_key = "your_api_key"\n', encoding="utf-8")
    (repo / ".env.example").write_text("TOKEN=your_token\n", encoding="utf-8")
    (repo / "docs.md").write_text("https://gitlab.example.com/group/project\n", encoding="utf-8")
    (repo / "test.cpp").write_text("testing::internal::CaptureStdout();\n", encoding="utf-8")

    assert scan_tree(repo, ()) == []


def test_external_denylist_does_not_echo_private_value(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    private_value = "corp.private.example"
    (repo / "config.md").write_text(f"https://{private_value}/project\n", encoding="utf-8")
    denylist_path = tmp_path / "denylist.txt"
    denylist_path.write_text(private_value + "\nperson@example.net\n", encoding="utf-8")

    denylist = load_denylist(denylist_path)
    findings = scan_tree(repo, denylist)

    assert len(findings) == 1
    assert findings[0].rule == "private-denylist-1"
    assert private_value not in repr(findings[0])


def test_scan_history_finds_secret_deleted_in_later_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _init_repo(repo)
    secret_path = repo / "config.txt"
    secret_path.write_text("token=" + "ghp_" + "a" * 24 + "\n", encoding="utf-8")
    _git(repo, "add", "config.txt")
    _git(repo, "commit", "-m", "add config")
    secret_path.unlink()
    _git(repo, "add", "-u")
    _git(repo, "commit", "-m", "remove config")

    findings = scan_history(repo, base, ())

    assert any(finding.path == "config.txt" and finding.rule == "provider-token" for finding in findings)


def test_scan_tree_with_base_ignores_unchanged_public_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _init_repo(repo)
    public_example = "token=" + "sk-" + "x" * 24 + "\n"
    (repo / "public-example.md").write_text(public_example, encoding="utf-8")
    _git(repo, "add", "public-example.md")
    _git(repo, "commit", "-m", "public example")
    trusted_base = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("custom change\n", encoding="utf-8")

    assert scan_tree(repo, (), base=trusted_base) == []
    assert scan_tree(repo, (), base=base)
