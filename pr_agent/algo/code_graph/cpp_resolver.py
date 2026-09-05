"""C/C++ `#include` dependency extraction.

Only quoted includes (`#include "foo/bar.h"`) are resolved, since that is
the convention for a project's own headers. Angle-bracket includes
(`#include <vector>`) are always system/library headers and are skipped -
they never resolve to a file inside the repository being analyzed.
"""

import os
import re
from typing import List, Optional

from pr_agent.algo.code_graph.base import normalize_repo_path

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"([^"]+)"')


def extract_cpp_includes(file_relpath: str, file_content: str, project_root: str) -> List[str]:
    """Return repo-relative paths of files this C/C++ source `#include`s.

    Resolution tries two locations, in order (mirroring the two most
    common compiler search strategies for a quoted include):
    1. Relative to the including file's own directory.
    2. Relative to the project root.

    An include whose target does not exist under either candidate location
    (a third-party vendored header, or a path outside the project root) is
    skipped rather than guessed at.
    """
    current_dir = os.path.dirname(file_relpath)
    resolved: List[str] = []
    seen = set()

    for line in file_content.splitlines():
        match = _INCLUDE_RE.match(line)
        if not match:
            continue
        found = _resolve_include(match.group(1), current_dir, project_root)
        if found and found not in seen:
            seen.add(found)
            resolved.append(found)

    return resolved


def _resolve_include(include_target: str, current_dir: str, project_root: str) -> Optional[str]:
    candidate_relative_to_file = os.path.normpath(os.path.join(project_root, current_dir, include_target))
    if os.path.isfile(candidate_relative_to_file) and _is_within(candidate_relative_to_file, project_root):
        return normalize_repo_path(os.path.relpath(candidate_relative_to_file, project_root))

    candidate_relative_to_root = os.path.normpath(os.path.join(project_root, include_target))
    if os.path.isfile(candidate_relative_to_root) and _is_within(candidate_relative_to_root, project_root):
        return normalize_repo_path(os.path.relpath(candidate_relative_to_root, project_root))

    return None


def _is_within(path: str, root: str) -> bool:
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    return os.path.commonpath([root_abs, path_abs]) == root_abs
