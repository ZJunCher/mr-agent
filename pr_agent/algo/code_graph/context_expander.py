"""Build bounded, file-level dependency context for PR reviews.

The public entrypoint is deliberately best-effort: a failed clone, parser, or
cache update returns no extra context so the existing diff-only review remains
available.
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Tuple

from pr_agent.algo.code_graph.cpp_resolver import extract_cpp_includes
from pr_agent.algo.code_graph.graph_store import GraphStore
from pr_agent.algo.code_graph.python_resolver import extract_python_imports
from pr_agent.algo.code_graph.repo_manager import (
    branch_storage_dir,
    changed_files_since,
    cleanup_stale_if_due,
    clone_or_update,
)
from pr_agent.config_loader import get_settings

_logger = logging.getLogger(__name__)

_LANGUAGE_EXTENSIONS = {
    "python": (".py",),
    "cpp": (".c", ".h", ".cc", ".cpp", ".hpp", ".cxx"),
}
_RESOLVERS = {".py": extract_python_imports}
for _extension in _LANGUAGE_EXTENSIONS["cpp"]:
    _RESOLVERS[_extension] = extract_cpp_includes


@dataclass
class ChangedFile:
    """The changed-file data needed to calculate related-file context."""

    relpath: str
    new_content: str


def build_related_files_context(
    changed_files: List[ChangedFile],
    clone_url: str,
    repo_url_for_key: str,
    target_branch: str,
    token_handler,
) -> str:
    """Return markdown context for related files, or ``""`` on any failure."""
    cfg = get_settings().get("pr_reviewer.code_graph", {})
    if not cfg or not cfg.get("enabled", False):
        return ""
    if not changed_files or not clone_url or not repo_url_for_key or not target_branch:
        return ""

    try:
        return _build(changed_files, clone_url, repo_url_for_key, target_branch, token_handler, cfg)
    except Exception as exc:
        _logger.warning("code_graph: skipping related-files context due to error: %s", exc)
        return ""


def _build(changed_files, clone_url, repo_url_for_key, target_branch, token_handler, cfg) -> str:
    supported_extensions = tuple(
        extension
        for language in cfg.get("supported_languages", ["python", "cpp"])
        for extension in _LANGUAGE_EXTENSIONS.get(language, ())
    )
    relevant_changed = [changed for changed in changed_files if changed.relpath.endswith(supported_extensions)]
    if not relevant_changed:
        return ""

    storage_root = cfg.get("storage_root", "/app/data/code_graph")
    repo_dir = clone_or_update(
        clone_url,
        repo_url_for_key,
        target_branch,
        storage_root,
        cfg.get("clone_timeout_seconds", 60),
    )
    if not repo_dir:
        return ""

    cleanup_stale_if_due(storage_root, cfg.get("stale_graph_ttl_days", 15))
    branch_dir = branch_storage_dir(storage_root, repo_url_for_key, target_branch)
    store = GraphStore(os.path.join(branch_dir, "graph.db"))
    _sync_graph(store, repo_dir, branch_dir, supported_extensions)
    ranked = _rank_related_files(relevant_changed, repo_dir, store, cfg.get("max_hops", 2))
    return _render_context(ranked, repo_dir, token_handler, cfg.get("token_budget", 8000))


def _sync_graph(store: GraphStore, repo_dir: str, branch_dir: str, supported_extensions: Tuple[str, ...]) -> None:
    head_marker = os.path.join(branch_dir, "graph_head.txt")
    previous_head = None
    if os.path.isfile(head_marker):
        with open(head_marker, "r") as marker:
            previous_head = marker.read().strip() or None

    changed, new_head = changed_files_since(repo_dir, previous_head)
    if changed is None:
        paths_to_index = _walk_supported_files(repo_dir, supported_extensions)
    else:
        paths_to_index = [path for path in changed if path.endswith(supported_extensions)]

    for relpath in paths_to_index:
        absolute_path = os.path.join(repo_dir, relpath)
        if os.path.isfile(absolute_path):
            _reindex_file(store, repo_dir, relpath)
        else:
            store.remove_file(relpath)

    with open(head_marker, "w") as marker:
        marker.write(new_head)


def _walk_supported_files(repo_dir: str, supported_extensions: Tuple[str, ...]) -> List[str]:
    paths = []
    for root, directories, files in os.walk(repo_dir):
        if ".git" in directories:
            directories.remove(".git")
        for filename in files:
            if filename.endswith(supported_extensions):
                absolute_path = os.path.join(root, filename)
                paths.append(os.path.relpath(absolute_path, repo_dir).replace(os.sep, "/"))
    return paths


def _reindex_file(store: GraphStore, repo_dir: str, relpath: str) -> None:
    try:
        with open(os.path.join(repo_dir, relpath), "r", encoding="utf-8") as source:
            content = source.read()
    except (OSError, UnicodeDecodeError):
        store.replace_file_edges(relpath, [])
        return

    resolver = _RESOLVERS.get(os.path.splitext(relpath)[1])
    dependencies = resolver(relpath, content, repo_dir) if resolver else []
    store.replace_file_edges(relpath, dependencies)


def _rank_related_files(
    changed_files: List[ChangedFile],
    repo_dir: str,
    store: GraphStore,
    max_hops: int,
) -> List[Tuple[str, int, bool]]:
    """Rank by distance, preferring reverse edges at equal distance.

    Reverse dependencies identify callers likely affected by a changed file;
    forward dependencies provide the changed file's own required background.
    """
    best: Dict[str, Tuple[int, bool]] = {}
    changed_paths = {changed.relpath for changed in changed_files}
    for changed in changed_files:
        resolver = _RESOLVERS.get(os.path.splitext(changed.relpath)[1])
        for path in resolver(changed.relpath, changed.new_content, repo_dir) if resolver else []:
            _consider(best, path, 1, False)
        for path, hop in store.get_reverse(changed.relpath, max_hops).items():
            _consider(best, path, hop, True)

    ranked = [(path, hop, is_reverse) for path, (hop, is_reverse) in best.items() if path not in changed_paths]
    return sorted(ranked, key=lambda item: (item[1], not item[2], item[0]))


def _consider(best: Dict[str, Tuple[int, bool]], path: str, hop: int, is_reverse: bool) -> None:
    previous = best.get(path)
    if previous is None or hop < previous[0] or (hop == previous[0] and is_reverse and not previous[1]):
        best[path] = (hop, is_reverse)


def _render_context(
    ranked: List[Tuple[str, int, bool]],
    repo_dir: str,
    token_handler,
    token_budget: int,
) -> str:
    if not ranked or token_budget <= 0:
        return ""

    header = "\n\n## Related Files (dependency context, not part of this PR's diff)\n"
    used_tokens = token_handler.count_tokens(header)
    sections = []
    for relpath, hop, is_reverse in ranked:
        try:
            with open(os.path.join(repo_dir, relpath), "r", encoding="utf-8") as source:
                content = source.read()
        except (OSError, UnicodeDecodeError):
            continue

        relationship = "depends on the changed file" if is_reverse else "depended on by the changed file"
        section = f"\n### {relpath} ({relationship}, {hop} hop away)\n```\n{content}\n```\n"
        section_tokens = token_handler.count_tokens(section)
        if used_tokens + section_tokens > token_budget:
            continue
        used_tokens += section_tokens
        sections.append(section)

    return header + "".join(sections) if sections else ""
