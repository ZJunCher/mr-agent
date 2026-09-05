"""Python `import` / `from ... import ...` dependency extraction.

Only resolves imports to files that actually exist under `project_root` -
third-party packages (stdlib, pip-installed) have no matching file and are
silently skipped, which is the desired behavior: we only care about
first-party, in-repo dependencies.
"""

import ast
import os
from typing import List, Optional, Tuple

from pr_agent.algo.code_graph.base import normalize_repo_path


def extract_python_imports(file_relpath: str, file_content: str, project_root: str) -> List[str]:
    """Parse a Python file's source and return the repo-relative paths of
    every other in-repo file it imports.

    - Absolute and relative `import` / `from ... import ...` statements are
      resolved to concrete files under `project_root` when the target
      exists on disk.
    - `from package import Name` where `Name` is not itself a submodule
      file is chased one level into `package/__init__.py` to find which
      submodule actually re-exports `Name` (falls back to depending on
      `__init__.py` itself if no match is found there).
    - Dynamic imports (`importlib.import_module(some_variable)`) are
      ordinary function-call expressions in the AST, not `Import` /
      `ImportFrom` nodes, so this walk never visits them - they are
      skipped by construction, per the project's "abandon dynamic import"
      rule (no special-casing needed).
    - A file that fails to parse (e.g. a syntax error) yields no
      dependencies rather than raising, so one bad file cannot break a
      whole-repository scan.
    """
    try:
        tree = ast.parse(file_content, filename=file_relpath)
    except (SyntaxError, ValueError):
        return []

    current_dir = os.path.dirname(file_relpath)
    current_file = normalize_repo_path(file_relpath)
    resolved: List[str] = []
    seen = set()

    def add(path: Optional[str]) -> None:
        if path and path != current_file and path not in seen:
            seen.add(path)
            resolved.append(path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target, _is_package = _resolve_module_or_package(alias.name.split("."), project_root)
                add(target)

        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                base_dir = _relative_base_dir(current_dir, node.level)
                base_parts = (base_dir.split("/") if base_dir else [])
                if node.module:
                    base_parts = base_parts + node.module.split(".")
            else:
                base_parts = node.module.split(".") if node.module else []

            if not base_parts:
                continue

            resolved_target, is_package = _resolve_module_or_package(base_parts, project_root)
            if resolved_target is None:
                continue

            if not is_package:
                add(resolved_target)
                continue

            package_dir_abs = os.path.join(project_root, *base_parts)
            for alias in node.names:
                imported_name = alias.name
                submodule_file = os.path.join(package_dir_abs, imported_name) + ".py"
                if os.path.isfile(submodule_file):
                    add(normalize_repo_path(os.path.relpath(submodule_file, project_root)))
                    continue
                subpackage_init = os.path.join(package_dir_abs, imported_name, "__init__.py")
                if os.path.isfile(subpackage_init):
                    add(normalize_repo_path(os.path.relpath(subpackage_init, project_root)))
                    continue
                init_file_abs = os.path.join(package_dir_abs, "__init__.py")
                chased = _chase_reexport(init_file_abs, project_root, imported_name)
                add(chased if chased else resolved_target)

    return resolved


def _relative_base_dir(current_dir: str, level: int) -> str:
    """Compute the package directory a relative import's dots point to.

    `level=1` (`from . import x`) means "the package containing the current
    module", i.e. `current_dir` itself. Each additional dot removes one
    more trailing directory segment (`level=2` -> parent package, etc).
    """
    parts = current_dir.split("/") if current_dir else []
    trim = level - 1
    if trim > 0:
        parts = parts[:-trim] if trim <= len(parts) else []
    return "/".join(parts)


def _resolve_module_or_package(parts: List[str], project_root: str) -> Tuple[Optional[str], bool]:
    """Resolve dotted module path segments to a file under `project_root`.

    Returns (repo_relative_path_or_None, is_package). `is_package` is True
    when the resolved file is a package's `__init__.py`, signaling the
    caller that it may need to chase re-exports for specific imported
    names rather than depending on the whole package.
    """
    module_file = os.path.join(project_root, *parts) + ".py"
    if os.path.isfile(module_file):
        return normalize_repo_path(os.path.relpath(module_file, project_root)), False

    package_init = os.path.join(project_root, *parts, "__init__.py")
    if os.path.isfile(package_init):
        return normalize_repo_path(os.path.relpath(package_init, project_root)), True

    return None, False


def _chase_reexport(package_init_path_abs: str, project_root: str, name: str) -> Optional[str]:
    """Look inside a package's `__init__.py` for a relative import that
    re-exports `name` from one of its submodules, e.g.
    `from .pr_reviewer import PRReviewer` re-exporting `PRReviewer`.

    Only chases one level (does not follow a re-export through a chain of
    multiple `__init__.py` files) - a deliberate scope limit, not a bug.
    """
    try:
        with open(package_init_path_abs, "r", encoding="utf-8") as f:
            init_source = f.read()
        init_tree = ast.parse(init_source, filename=package_init_path_abs)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None

    package_dir_abs = os.path.dirname(package_init_path_abs)
    for node in ast.walk(init_tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.level >= 1 and node.module:
            for alias in node.names:
                imported_name = alias.asname or alias.name
                if imported_name == name:
                    submodule_file = os.path.join(package_dir_abs, *node.module.split(".")) + ".py"
                    if os.path.isfile(submodule_file):
                        return normalize_repo_path(os.path.relpath(submodule_file, project_root))
    return None
