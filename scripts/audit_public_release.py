#!/usr/bin/env python3
"""Audit a release tree and its custom Git history without echoing secrets."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Finding:
    revision: str
    path: str
    rule: str


FORBIDDEN_PATH_PATTERNS = (
    re.compile(r"(^|/)(\.env($|\.)|.*\.log$|.*\.db$|.*\.sqlite3?$)"),
    re.compile(r"(^|/)(workspace/logs|document_backups|vibe-resume)(/|$)"),
    re.compile(r"(^|/).*(resume|auth-qr|credential).*(\.pdf|\.png|\.json)$", re.I),
)

SAFE_TEMPLATE_PATHS = frozenset({".env.example", "pr_agent/settings/.secrets_template.toml"})

FORBIDDEN_TEXT_PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    "provider-token": re.compile(rb"(?<![A-Za-z0-9_-])(?:sk-|ghp_|github_pat_|glpat-)[A-Za-z0-9_-]{16,}"),
    "aws-access-key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "internal-host": re.compile(rb"(?:https?://)?(?:[A-Za-z0-9-]+\.)*internal(?:[./]|$)", re.I),
    "personal-path": re.compile(rb"/(?:Users|home)/[^/\s]+/"),
}


def _git(root: Path, *args: str, text: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout


def load_denylist(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            values.append(value.casefold())
    return tuple(values)


def _is_binary(content: bytes) -> bool:
    return b"\0" in content[:8192]


def _scan_blob(revision: str, path: str, content: bytes, denylist: tuple[str, ...]) -> list[Finding]:
    findings = []
    if path not in SAFE_TEMPLATE_PATHS:
        for pattern in FORBIDDEN_PATH_PATTERNS:
            if pattern.search(path):
                findings.append(Finding(revision, path, "forbidden-path"))
                break
    if _is_binary(content):
        return findings
    for rule, pattern in FORBIDDEN_TEXT_PATTERNS.items():
        if pattern.search(content):
            findings.append(Finding(revision, path, rule))
    folded_content = content.decode("utf-8", errors="ignore").casefold()
    for index, value in enumerate(denylist, start=1):
        if value in folded_content:
            findings.append(Finding(revision, path, f"private-denylist-{index}"))
    return findings


def _deduplicate(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(set(findings), key=lambda item: (item.revision, item.path, item.rule))


def _changed_worktree_paths(root: Path, base: str) -> set[str]:
    raw_changed = _git(root, "diff", "--name-only", "-z", base, "--")
    raw_untracked = _git(root, "ls-files", "-o", "--exclude-standard", "-z")
    assert isinstance(raw_changed, bytes)
    assert isinstance(raw_untracked, bytes)
    return {
        raw_path.decode("utf-8", errors="surrogateescape")
        for raw_path in (raw_changed + raw_untracked).split(b"\0")
        if raw_path
    }


def scan_tree(root: Path, denylist: tuple[str, ...], base: str | None = None) -> list[Finding]:
    raw_paths = _git(root, "ls-files", "-co", "--exclude-standard", "-z")
    assert isinstance(raw_paths, bytes)
    changed_paths = _changed_worktree_paths(root, base) if base else None
    findings = []
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if changed_paths is not None and path not in changed_paths:
            continue
        absolute_path = root / path
        if absolute_path.is_file() and not absolute_path.is_symlink():
            findings.extend(_scan_blob("WORKTREE", path, absolute_path.read_bytes(), denylist))
    return _deduplicate(findings)


def scan_history(root: Path, base: str, denylist: tuple[str, ...]) -> list[Finding]:
    raw_revisions = _git(root, "rev-list", "--reverse", f"{base}..HEAD", text=True)
    assert isinstance(raw_revisions, str)
    findings = []
    for revision in raw_revisions.splitlines():
        raw_paths = _git(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", revision)
        assert isinstance(raw_paths, bytes)
        for raw_path in raw_paths.split(b"\0"):
            if not raw_path:
                continue
            path = raw_path.decode("utf-8", errors="surrogateescape")
            exists = subprocess.run(
                ["git", "cat-file", "-e", f"{revision}:{path}"],
                cwd=root,
                capture_output=True,
            ).returncode == 0
            if not exists:
                continue
            content = _git(root, "show", f"{revision}:{path}")
            assert isinstance(content, bytes)
            findings.extend(_scan_blob(revision, path, content, denylist))
    return _deduplicate(findings)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", help="Scan every commit after this public base revision")
    parser.add_argument("--denylist-file", type=Path)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    root = args.root.resolve()
    denylist = load_denylist(args.denylist_file)
    findings = scan_tree(root, denylist, base=args.base)
    if args.base:
        findings.extend(scan_history(root, args.base, denylist))
    findings = _deduplicate(findings)
    for finding in findings:
        print(f"{finding.revision}:{finding.path}:{finding.rule}")
    if findings:
        print(f"privacy audit failed with {len(findings)} finding(s)")
        return 1
    print("privacy audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
