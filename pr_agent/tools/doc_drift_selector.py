"""Candidate document selection for the doc-drift detector.

Two layers, kept deliberately separate so the decision logic is unit-testable
without any git/network IO:

- Pure logic (``select_candidates`` and helpers): given a flat list of
  repo-relative doc paths plus the changed files, decide which docs are
  candidates. Candidate set = global docs (fixed, always considered) ∪
  neighbour docs (same directory or any ancestor directory of a changed file),
  deduped and capped, with global docs prioritised under the cap.

- IO layer (``gather_candidate_docs``): clone the repo, walk it to discover all
  doc-like files, run the pure selection, and read the selected files'
  contents. Never raises; on any failure returns an empty result so the MR flow
  is never broken.
"""
from __future__ import annotations

import os
import re
from tempfile import TemporaryDirectory

from pr_agent.config_loader import get_settings  # noqa: F401  # load config before log to avoid a circular import
from pr_agent.log import get_logger


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a glob (supporting ``**``) into a compiled regex.

    ``**`` matches across directory separators, ``*`` matches within a single
    path segment, ``?`` matches a single non-separator character.
    """
    i = 0
    out = ["^"]
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # ``**`` (optionally followed by ``/``) matches any depth
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def match_glob(path: str, patterns: list[str]) -> bool:
    """Return True if ``path`` matches any of the given globs."""
    normalized = path.replace("\\", "/").lstrip("/")
    for pattern in patterns or []:
        if _glob_to_regex(pattern.replace("\\", "/").lstrip("/")).match(normalized):
            return True
    return False


def ancestor_dirs(changed_files: list[str]) -> set[str]:
    """Directories that are the same as, or an ancestor of, a changed file.

    The repository root is represented by the empty string ``""``.
    """
    dirs: set[str] = set()
    for f in changed_files or []:
        parts = f.replace("\\", "/").lstrip("/").split("/")[:-1]  # drop filename
        dirs.add("")  # root always included
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return dirs


def _dirname(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("/")
    return normalized.rsplit("/", 1)[0] if "/" in normalized else ""


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def select_candidates(
    all_doc_paths: list[str],
    changed_files: list[str],
    global_globs: list[str],
    ancestor_globs: list[str],
    max_docs: int,
) -> list[str]:
    """Select candidate docs from the repository's doc files.

    Returns repo-relative paths, global docs first, deduped and capped at
    ``max_docs``. When the cap is hit, global docs are kept preferentially and
    only neighbour docs are truncated.
    """
    norm = [p.replace("\\", "/").lstrip("/") for p in all_doc_paths or []]

    global_docs = [p for p in norm if match_glob(p, global_globs)]

    dirs = ancestor_dirs(changed_files)
    neighbour_docs = [
        p
        for p in norm
        if _dirname(p) in dirs and match_glob(_basename(p), ancestor_globs)
    ]

    ordered: list[str] = []
    seen: set[str] = set()
    for p in global_docs + neighbour_docs:
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    if max_docs and max_docs > 0 and len(ordered) > max_docs:
        get_logger().warning(
            f"doc-drift: candidate docs ({len(ordered)}) exceed max_docs_per_mr "
            f"({max_docs}); truncating (global docs kept first)."
        )
        ordered = ordered[:max_docs]
    return ordered


def _walk_doc_paths(repo_root: str, doc_exts: list[str]) -> list[str]:
    """Walk ``repo_root`` and return repo-relative posix paths of doc files."""
    dotless = [e.lower().lstrip(".") for e in doc_exts]
    results: list[str] = []
    for root, _dirs, files in os.walk(repo_root):
        # Skip the .git directory to save time.
        if ".git" in root.split(os.sep):
            continue
        for file in files:
            if any(file.lower().endswith(f".{ext}") for ext in dotless):
                rel = os.path.relpath(os.path.join(root, file), repo_root)
                results.append(rel.replace(os.sep, "/"))
    return results


def gather_candidate_docs(
    git_provider,
    repo_url: str,
    changed_files: list[str],
    global_globs: list[str],
    ancestor_globs: list[str],
    doc_exts: list[str],
    max_docs: int,
    max_doc_chars: int = 20000,
) -> dict[str, str]:
    """Clone the repo, select candidate docs, and read their contents.

    Returns a mapping of repo-relative path -> content. Never raises; returns
    an empty dict on any failure.
    """
    try:
        with TemporaryDirectory() as tmp_dir:
            cloned = git_provider.clone(repo_url, tmp_dir, remove_dest_folder=False)
            if not cloned:
                get_logger().warning(f"doc-drift: failed to clone {repo_url}")
                return {}
            repo_root = cloned.path

            all_doc_paths = _walk_doc_paths(repo_root, doc_exts)
            selected = select_candidates(
                all_doc_paths, changed_files, global_globs, ancestor_globs, max_docs
            )
            if not selected:
                get_logger().info("doc-drift: no candidate docs found for this MR.")
                return {}

            contents: dict[str, str] = {}
            for rel in selected:
                abs_path = os.path.join(repo_root, rel.replace("/", os.sep))
                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        text = f.read()
                except Exception as e:
                    get_logger().warning(f"doc-drift: cannot read {rel}: {e}")
                    continue
                if not re.search(r"[a-zA-Z]", text):
                    continue
                if len(text) > max_doc_chars:
                    get_logger().warning(
                        f"doc-drift: {rel} length {len(text)} exceeds "
                        f"{max_doc_chars}; trimming."
                    )
                    text = text[:max_doc_chars]
                contents[rel] = text.strip()
            return contents
    except Exception:
        get_logger().exception("doc-drift: gather_candidate_docs failed; returning empty.")
        return {}
