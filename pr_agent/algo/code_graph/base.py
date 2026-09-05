"""Shared types and helpers for the code-graph dependency analysis package.

This package builds a lightweight, FILE-LEVEL dependency graph (not a
symbol/function-level call graph) so PR-Agent can hand the reviewing LLM a
small set of files related to the ones changed in a PR, without shipping the
whole repository as context. See
docs/superpowers/specs/2026-07-14-code-graph-context-design.md for the full
design rationale.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DependencyEdge:
    """A single directed file-level dependency: `source` depends on `target`.

    Both paths are POSIX-style, relative to the repository root (e.g.
    "pr_agent/tools/pr_reviewer.py"), so they can be used as stable
    dictionary keys and SQLite primary keys regardless of the OS the code
    runs on.
    """
    source: str
    target: str


def normalize_repo_path(path: str) -> str:
    """Normalize a filesystem path to a POSIX-style, repo-root-relative
    string - the canonical key used for files throughout this package.
    """
    normalized = path.replace(os.sep, "/").replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized
