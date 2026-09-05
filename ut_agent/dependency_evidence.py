"""Read current interfaces from dependencies declared by the checked-out MR only."""

import base64
import configparser
import hashlib
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import yaml

from pr_agent.triage.failure_explanations import sanitize_failure_text
from ut_agent.blocker_evidence import validate_blocker_record

MAX_DECLARED_PROJECTS = 12
MAX_TREE_ENTRIES = 5_000
MAX_MATCHES = 20
MAX_FILE_BYTES = 64 * 1024
MAX_EVIDENCE_CHARS = 20_000
MAX_MANIFEST_BYTES = 256 * 1024
MAX_NAMESPACE_PROJECTS = 100
MAX_DISCOVERY_MATCHES = 10
DISCOVERY_CACHE_TTL_SECONDS = 300
MAX_BRANCH_CATALOG = 300
MAX_BRANCH_CANDIDATES = 20
MAX_TREE_ENTRIES_PER_SCOPE = 5_000
MAX_VERIFIED_BRANCH_MATCHES = 5

_SCP_GIT_URL = re.compile(r"^[^@\s]+@[^:\s]+:(?P<path>[^#?\s]+?)(?:\.git)?$")
_PROJECT_PATH = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")
_ROS_TYPE = re.compile(
    r"(?P<package>[A-Za-z][A-Za-z0-9_]*)::(?P<kind>msg|srv|action)::"
    r"(?P<interface>[A-Za-z][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_EXPLICIT_INTERFACE_FILE = re.compile(r"(?P<filename>[A-Za-z][A-Za-z0-9_.-]*\.(?:proto|idl))", re.IGNORECASE)
_MISSING_HEADER_RE = re.compile(
    r"fatal error:\s*(?P<pkg>[A-Za-z][A-Za-z0-9_]*)/(?P<kind>msg|srv|action)/"
    r"(?P<snake>[a-z][a-z0-9_]*)\.(?:hpp|h)\s*:\s*No such file or directory",
    re.IGNORECASE,
)
_ROS_GENERATED_SUFFIXES = ("Request", "Response", "Goal", "Result", "Feedback")
_MISSING_PACKAGE_PATTERNS = (
    re.compile(r'package configuration file provided by ["\'](?P<package>[A-Za-z][A-Za-z0-9_.+-]*)["\']', re.I),
    re.compile(r"\bfind_package\s*\(\s*(?P<package>[A-Za-z][A-Za-z0-9_.+-]*)", re.I),
    re.compile(r"\b(?P<package>[A-Za-z][A-Za-z0-9_.+-]*)Config\.cmake\b", re.I),
)
_DISCOVERY_CACHE: dict[tuple[int, str, str], tuple[float, Any, dict[str, Any]]] = {}


def _snake_to_pascal(value: str) -> str:
    return "".join(part.capitalize() for part in value.split("_") if part)


class DependencyManifestError(ValueError):
    """The current checkout declares more dependency scope than the resolver accepts."""


@dataclass(frozen=True)
class DeclaredDependency:
    module: str
    project_path: str
    branch: str


@dataclass(frozen=True)
class InterfaceQuery:
    package: str
    kind: str
    interface: str
    filename: str


def _safe_text_file(repo_root: Path, relative_path: str) -> str:
    path = (repo_root / relative_path).resolve()
    if repo_root != path and repo_root not in path.parents:
        return ""
    try:
        if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _project_path_from_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = _SCP_GIT_URL.fullmatch(raw)
    if match:
        path = match.group("path")
    else:
        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https", "ssh", "git"} and parsed.hostname:
            path = parsed.path
        elif not parsed.scheme and _PROJECT_PATH.fullmatch(raw.removesuffix(".git").strip("/")):
            path = raw
        else:
            return ""
    path = path.strip("/").removesuffix(".git")
    if not _PROJECT_PATH.fullmatch(path) or any(part in {".", ".."} for part in path.split("/")):
        return ""
    return path


def _yaml_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("deps", "dependencies", "repositories", "repos"):
        nested = value.get(key)
        if isinstance(nested, list):
            return [entry for entry in nested if isinstance(entry, dict)]
        if isinstance(nested, dict):
            entries = []
            for module, entry in nested.items():
                if isinstance(entry, dict):
                    entries.append({"module": module, **entry})
            return entries
    if value and all(isinstance(entry, dict) for entry in value.values()):
        return [{"module": module, **entry} for module, entry in value.items()]
    return []


def _dependency_from_entry(entry: dict[str, Any]) -> DeclaredDependency | None:
    module = str(entry.get("module") or entry.get("name") or entry.get("path") or "").strip()
    project_path = _project_path_from_url(
        entry.get("url") or entry.get("repository") or entry.get("repo") or entry.get("git")
    )
    branch = str(entry.get("branch") or entry.get("ref") or entry.get("revision") or "").strip()
    if not module or not project_path or not branch:
        return None
    return DeclaredDependency(module, project_path, branch)


def parse_declared_dependencies(repo_dir: str) -> list[DeclaredDependency]:
    """Parse only dependency manifests inside the current checkout."""
    root = Path(repo_dir).resolve()
    dependencies = []
    for relative_path in ("dev_kit/deps.yml", "deps.yml"):
        text = _safe_text_file(root, relative_path)
        if not text:
            continue
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        for entry in _yaml_entries(parsed):
            dependency = _dependency_from_entry(entry)
            if dependency is not None:
                dependencies.append(dependency)

    gitmodules = _safe_text_file(root, ".gitmodules")
    if gitmodules:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(gitmodules)
        except configparser.Error:
            parser = configparser.ConfigParser(interpolation=None)
        for section in parser.sections():
            if not section.startswith("submodule "):
                continue
            dependency = _dependency_from_entry({
                "module": parser.get(section, "path", fallback=""),
                "url": parser.get(section, "url", fallback=""),
                "branch": parser.get(section, "branch", fallback=""),
            })
            if dependency is not None:
                dependencies.append(dependency)

    deduplicated = []
    seen = set()
    for dependency in dependencies:
        if dependency.project_path in seen:
            continue
        seen.add(dependency.project_path)
        deduplicated.append(dependency)
    if len(deduplicated) > MAX_DECLARED_PROJECTS:
        raise DependencyManifestError(
            f"dependency manifest declares {len(deduplicated)} projects; maximum is {MAX_DECLARED_PROJECTS}"
        )
    return deduplicated


def _manifest_entries(text: str) -> list[dict[str, Any]]:
    try:
        return _yaml_entries(yaml.safe_load(text))
    except yaml.YAMLError:
        return []


def _dependency_identity(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    module = str(entry.get("module") or entry.get("name") or entry.get("path") or "").strip()
    repository_url = str(
        entry.get("url") or entry.get("repository") or entry.get("repo") or entry.get("git") or ""
    ).strip()
    branch = str(entry.get("branch") or entry.get("ref") or entry.get("revision") or "").strip()
    return module, _project_path_from_url(repository_url), repository_url, branch


def validate_discovered_provider_changes(
    repo_dir: str,
    snapshots: Iterable[dict[str, Any]],
    changed_files: Iterable[str],
) -> tuple[bool, str]:
    """Ensure a dependency-manifest edit contains only the exact service-authorized provider additions."""
    root = Path(repo_dir).resolve()
    discovered = [
        item for item in snapshots
        if isinstance(item, dict) and item.get("evidence_kind") == "discovered_provider"
    ]
    if not discovered:
        return True, ""
    changed_relative = set()
    for value in changed_files:
        path = Path(str(value))
        try:
            changed_relative.add(str((path if path.is_absolute() else root / path).resolve().relative_to(root)))
        except (OSError, ValueError):
            return False, "自动修复修改了工作区之外的文件"

    for manifest_path in sorted({str(item.get("dependency_manifest_path") or "") for item in discovered}):
        if not manifest_path or manifest_path not in changed_relative:
            continue
        manifest = (root / manifest_path).resolve()
        if root != manifest and root not in manifest.parents:
            return False, "依赖清单路径超出当前工作区"
        current_text = _safe_text_file(root, manifest_path)
        if not current_text:
            return False, f"依赖清单 {manifest_path} 无法解析或为空"
        baseline = subprocess.run(
            ["git", "show", f"HEAD:{manifest_path}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if baseline.returncode != 0:
            return False, f"无法读取 {manifest_path} 的修改前内容"
        before = {_dependency_identity(entry) for entry in _manifest_entries(baseline.stdout)}
        after = {_dependency_identity(entry) for entry in _manifest_entries(current_text)}
        if ("", "", "", "") in after:
            return False, f"依赖清单 {manifest_path} 包含不完整条目"
        removed = before - after
        added = after - before
        allowed = {
            (
                str(item.get("module") or ""),
                str(item.get("project_path") or ""),
                str(item.get("repository_url") or ""),
                str(item.get("declared_branch") or ""),
            )
            for item in discovered
            if str(item.get("dependency_manifest_path") or "") == manifest_path
        }
        if removed:
            return False, f"依赖清单 {manifest_path} 删除或改写了已有依赖"
        if not added:
            return False, f"依赖清单 {manifest_path} 未加入已核验的缺失依赖"
        if not added <= allowed:
            return False, f"依赖清单 {manifest_path} 加入了未经核验的仓库或分支"
    return True, ""


def derive_interface_queries(diagnostic: str) -> list[InterfaceQuery]:
    """Derive exact interface filenames without guessing a replacement member."""
    queries = []
    bounded = str(diagnostic or "")[:MAX_EVIDENCE_CHARS]
    for match in _ROS_TYPE.finditer(bounded):
        package = match.group("package")
        kind = match.group("kind").lower()
        interface = match.group("interface").rstrip("_")
        for suffix in _ROS_GENERATED_SUFFIXES:
            marker = f"_{suffix}"
            if interface.endswith(marker):
                interface = interface[:-len(marker)]
                break
        if interface:
            queries.append(InterfaceQuery(package, kind, interface, f"{interface}.{kind}"))
    for match in _EXPLICIT_INTERFACE_FILE.finditer(bounded):
        filename = match.group("filename")
        kind = filename.rsplit(".", 1)[-1].lower()
        queries.append(InterfaceQuery("", kind, filename.rsplit(".", 1)[0], filename))
    for match in _MISSING_HEADER_RE.finditer(bounded):
        package = match.group("pkg")
        kind = match.group("kind").lower()
        interface = _snake_to_pascal(match.group("snake"))
        if interface:
            queries.append(InterfaceQuery(package, kind, interface, f"{interface}.{kind}"))
    return list(dict.fromkeys(queries))


def derive_missing_package_names(diagnostic: str) -> list[str]:
    """Extract exact missing package names from CMake diagnostics without inferring a provider."""
    bounded = str(diagnostic or "")[:MAX_EVIDENCE_CHARS]
    names = []
    for pattern in _MISSING_PACKAGE_PATTERNS:
        names.extend(match.group("package") for match in pattern.finditer(bounded))
    return list(dict.fromkeys(names))


def _object_value(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _dependency_manifest_path(repo_dir: str) -> str:
    root = Path(repo_dir).resolve()
    return next(
        (relative for relative in ("dev_kit/deps.yml", "deps.yml") if (root / relative).is_file()),
        "",
    )


def _package_manifest_name(raw: bytes) -> str:
    if len(raw) > MAX_FILE_BYTES:
        return ""
    try:
        root = ElementTree.fromstring(raw.decode("utf-8"))
    except (ElementTree.ParseError, UnicodeDecodeError):
        return ""
    name = root.find("name")
    return str(name.text or "").strip() if name is not None else ""


def _branch_sha(project, branch_name: str) -> str:
    branch = project.branches.get(branch_name)
    resolved_sha = str(_object_value(branch, "commit", {}).get("id") or "").strip()
    if not resolved_sha:
        raise ValueError("branch has no commit SHA")
    return resolved_sha


def _read_bounded_project_file(project, file_path: str, ref: str) -> bytes:
    raw = _decode_project_file(project.files.get(file_path=file_path, ref=ref))
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("project file exceeds read limit")
    return raw


def resolve_declared_package_provider(
    gl,
    dependencies: list[DeclaredDependency],
    package_name: str,
) -> dict[str, Any]:
    """Resolve one declared dependency as the exact package provider using package.xml only."""
    package_name = str(package_name or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.+-]*", package_name):
        return {"status": "not_found", "package_name": package_name, "errors": []}

    matches: dict[tuple[str, str, str], dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    seen_errors: set[tuple[str, str]] = set()

    def record_error(project_path: str, exc: Exception) -> None:
        category = type(exc).__name__
        key = (project_path, category)
        if key in seen_errors or len(errors) >= MAX_DECLARED_PROJECTS:
            return
        seen_errors.add(key)
        errors.append({"project_path": project_path, "category": category})

    for dependency in dependencies[:MAX_DECLARED_PROJECTS]:
        try:
            project = gl.projects.get(dependency.project_path)
            resolved_sha = _branch_sha(project, dependency.branch)
        except Exception as exc:
            record_error(dependency.project_path, exc)
            continue
        for package_path in (package_name, f"src/{package_name}"):
            manifest_path = f"{package_path}/package.xml"
            try:
                raw = _read_bounded_project_file(project, manifest_path, resolved_sha)
            except Exception as exc:
                record_error(dependency.project_path, exc)
                continue
            if _package_manifest_name(raw) != package_name:
                continue
            key = (dependency.project_path, resolved_sha, package_path)
            matches[key] = {
                "module": dependency.module,
                "project_path": dependency.project_path,
                "declared_branch": dependency.branch,
                "resolved_sha": resolved_sha,
                "package_name": package_name,
                "package_path": package_path,
                "manifest_path": manifest_path,
            }

    values = list(matches.values())
    if not values:
        return {
            "status": "not_found",
            "package_name": package_name,
            "errors": errors,
        }
    if len(values) > 1:
        return {
            "status": "ambiguous",
            "package_name": package_name,
            "matches": values[:MAX_MATCHES],
            "errors": errors,
        }
    return {"status": "resolved", **values[0], "errors": errors}


def _verified_package_candidate(gl, summary: Any, package_name: str) -> dict[str, Any] | None:
    project_path = str(_object_value(summary, "path_with_namespace") or "")
    if not _PROJECT_PATH.fullmatch(project_path):
        return None
    project = gl.projects.get(project_path)
    branch_name = str(
        _object_value(summary, "default_branch") or _object_value(project, "default_branch") or ""
    ).strip()
    repository_url = str(
        _object_value(summary, "ssh_url_to_repo") or _object_value(project, "ssh_url_to_repo") or ""
    ).strip()
    if not branch_name or _project_path_from_url(repository_url) != project_path:
        return None
    branch = project.branches.get(branch_name)
    resolved_sha = str(_object_value(branch, "commit", {}).get("id") or "")
    if len(resolved_sha) != 40 and not resolved_sha:
        return None
    for package_path in (package_name, f"src/{package_name}"):
        file_path = f"{package_path}/package.xml"
        try:
            raw = _decode_project_file(project.files.get(file_path=file_path, ref=resolved_sha))
        except Exception:
            continue
        if _package_manifest_name(raw) != package_name:
            continue
        return {
            "module": project_path.rsplit("/", 1)[-1],
            "project_path": project_path,
            "repository_url": repository_url,
            "declared_branch": branch_name,
            "resolved_sha": resolved_sha,
            "package_path": package_path,
            "file_path": file_path,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "content": raw.decode("utf-8", errors="replace")[:MAX_EVIDENCE_CHARS],
        }
    return None


def discover_unique_package_provider(
    gl,
    repo_dir: str,
    current_project_id: str,
    diagnostic: str,
) -> dict[str, Any]:
    """Discover one verified package provider in the current GitLab namespace using read-only APIs."""
    packages = derive_missing_package_names(diagnostic)
    manifest_path = _dependency_manifest_path(repo_dir)
    project_id = str(current_project_id or "").strip("/")
    if len(packages) != 1 or not manifest_path or not _PROJECT_PATH.fullmatch(project_id):
        return {"status": "not_applicable", "message": "missing package or dependency manifest is not exact"}
    namespace, package_name = project_id.split("/", 1)[0], packages[0]
    cache_key = (id(gl), namespace, package_name)
    cached = _DISCOVERY_CACHE.get(cache_key)
    if cached is not None and cached[0] > time.monotonic() and cached[1] is gl:
        return dict(cached[2])
    try:
        group = gl.groups.get(namespace)
        summaries = list(group.projects.list(include_subgroups=True, per_page=100, get_all=True))
    except Exception as exc:
        return {"status": "error", "message": f"namespace project listing failed: {type(exc).__name__}"}
    if len(summaries) > MAX_NAMESPACE_PROJECTS:
        return {"status": "limit_exceeded", "message": f"namespace exceeds {MAX_NAMESPACE_PROJECTS} projects"}

    matches = []
    for summary in summaries:
        candidate_path = str(_object_value(summary, "path_with_namespace") or "")
        if (
            not candidate_path
            or candidate_path == project_id
            or bool(_object_value(summary, "archived", False))
            or "third-party" in candidate_path.split("/")
        ):
            continue
        try:
            candidate = _verified_package_candidate(gl, summary, package_name)
        except Exception:
            candidate = None
        if candidate is not None:
            matches.append(candidate)
            if len(matches) > MAX_DISCOVERY_MATCHES:
                return {"status": "ambiguous", "message": "too many verified package providers"}

    if not matches:
        result = {"status": "not_found", "package_name": package_name}
    elif len(matches) > 1:
        result = {
            "status": "ambiguous",
            "package_name": package_name,
            "matches": [
                {
                    "project_path": match["project_path"],
                    "declared_branch": match["declared_branch"],
                    "package_path": match["package_path"],
                }
                for match in matches
            ],
        }
    else:
        result = {
            "status": "resolved",
            "evidence_kind": "discovered_provider",
            "package_name": package_name,
            "dependency_manifest_path": manifest_path,
            **matches[0],
        }
    _DISCOVERY_CACHE[cache_key] = (time.monotonic() + DISCOVERY_CACHE_TTL_SECONDS, gl, dict(result))
    return result


def _tree_entries(project, resolved_sha: str) -> Iterable[dict[str, Any]]:
    return project.repository_tree(ref=resolved_sha, recursive=True, iterator=True)


def _candidate_matches(path: str, query: InterfaceQuery) -> bool:
    normalized = path.strip("/")
    if not normalized.endswith(f"/{query.filename}") and normalized != query.filename:
        return False
    if query.kind in {"msg", "srv", "action"}:
        return f"/{query.kind}/{query.filename}" in f"/{normalized}"
    return True


def _list_branch_names(project, limit: int) -> list[str]:
    try:
        branches = project.branches.list(iterator=True, per_page=min(limit, 100))
    except Exception:
        return []
    names = []
    for branch in branches:
        name = str(_object_value(branch, "name") or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


_BRANCH_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")
_BRANCH_NUMBER_ALIASES = {"2": "two"}


def _branch_tokens(value: str) -> tuple[str, ...]:
    words = []
    for segment in re.split(r"[/_.-]+", str(value or "")):
        for word in _BRANCH_WORD.findall(segment):
            normalized = _BRANCH_NUMBER_ALIASES.get(word.lower(), word.lower())
            if normalized:
                words.append(normalized)
    return tuple(words)


def rank_dependency_branches(
    branch_names: Iterable[str],
    *,
    source_branch: str,
    queries: Iterable[InterfaceQuery],
    context_text: str = "",
) -> list[str]:
    """Rank a bounded branch catalog deterministically; ranking is not verification."""
    source_tokens = set(_branch_tokens(source_branch))
    context_tokens = set(_branch_tokens(context_text))
    for query in queries:
        context_tokens.update(_branch_tokens(query.interface))
    normalized_source = "/".join(_branch_tokens(source_branch))
    unique_names = list(dict.fromkeys(str(name).strip() for name in branch_names if str(name).strip()))

    def score(item: tuple[int, str]) -> tuple[int, int, int, int, str]:
        index, name = item
        tokens = set(_branch_tokens(name))
        normalized_name = "/".join(_branch_tokens(name))
        exact_source = int(bool(normalized_source) and normalized_name == normalized_source)
        source_overlap = len(tokens & source_tokens)
        context_overlap = len(tokens & context_tokens)
        prefix_overlap = sum(
            1
            for token in tokens
            for source_token in source_tokens
            if len(token) >= 3 and len(source_token) >= 3 and (
                token.startswith(source_token) or source_token.startswith(token)
            )
        )
        return (-exact_source, -source_overlap, -prefix_overlap, -context_overlap, f"{index:06d}:{name.lower()}")

    return [name for _, name in sorted(enumerate(unique_names), key=score)]


def verify_interfaces_on_branch(
    project,
    branch_name: str,
    package_path: str,
    queries: Iterable[InterfaceQuery],
) -> dict[str, Any]:
    """Verify observed interfaces using one package-scoped, read-only tree scan."""
    query_list = list(dict.fromkeys(queries))
    try:
        resolved_sha = _branch_sha(project, branch_name)
        entries = project.repository_tree(
            ref=resolved_sha,
            path=package_path,
            recursive=True,
            iterator=True,
        )
        file_paths: dict[str, str] = {}
        entry_count = 0
        for entry in entries:
            entry_count += 1
            if entry_count > MAX_TREE_ENTRIES_PER_SCOPE:
                return {
                    "branch": branch_name,
                    "resolved_sha": resolved_sha,
                    "verification_complete": False,
                    "matched_queries": [query.filename for query in query_list if query.filename in file_paths],
                    "missing_queries": [],
                    "file_paths": file_paths,
                    "checked_entry_count": MAX_TREE_ENTRIES_PER_SCOPE,
                    "error_category": "scope_limit_exceeded",
                }
            if not isinstance(entry, dict) or str(entry.get("type") or "") != "blob":
                continue
            path = str(entry.get("path") or "")
            for query in query_list:
                if query.filename not in file_paths and _candidate_matches(path, query):
                    file_paths[query.filename] = path
        matched = [query.filename for query in query_list if query.filename in file_paths]
        missing = [query.filename for query in query_list if query.filename not in file_paths]
        return {
            "branch": branch_name,
            "resolved_sha": resolved_sha,
            "verification_complete": True,
            "matched_queries": matched,
            "missing_queries": missing,
            "file_paths": file_paths,
            "checked_entry_count": entry_count,
            "error_category": "",
        }
    except Exception as exc:
        return {
            "branch": branch_name,
            "resolved_sha": "",
            "verification_complete": False,
            "matched_queries": [],
            "missing_queries": [],
            "file_paths": {},
            "checked_entry_count": 0,
            "error_category": type(exc).__name__,
        }


def _branch_file_path(project, branch_name: str, query: InterfaceQuery) -> str:
    """Return the matching tree path on this branch, or '' if not present. Read-only."""
    try:
        branch = project.branches.get(branch_name)
        resolved_sha = str(_object_value(branch, "commit", {}).get("id") or "")
        if not resolved_sha:
            return ""
        for entry in _tree_entries(project, resolved_sha):
            if not isinstance(entry, dict) or str(entry.get("type") or "") != "blob":
                continue
            path = str(entry.get("path") or "")
            if _candidate_matches(path, query):
                return path
    except Exception:
        return ""
    return ""


def _last_touching_commit(project, path: str, branch_name: str) -> dict[str, Any] | None:
    """Find the most recent commit on the declared branch that touched this now-missing path. Read-only."""
    try:
        commits = project.commits.list(ref_name=branch_name, path=path, per_page=1, get_all=False)
    except Exception:
        return None
    if not commits:
        return None
    commit = commits[0]
    return {
        "commit_sha": str(_object_value(commit, "id") or "")[:12],
        "author": str(_object_value(commit, "author_name") or ""),
        "committed_date": str(_object_value(commit, "committed_date") or "")[:10],
        "title": str(_object_value(commit, "title") or "")[:200],
    }


def search_missing_interfaces_elsewhere(
    gl,
    project_path: str,
    queries: Iterable[InterfaceQuery],
    declared_branch: str,
    package_path: str,
    *,
    source_branch: str = "",
    context_text: str = "",
) -> dict[str, Any]:
    """Find verified candidate branches in the same provider repository using bounded package scans."""
    query_list = list(dict.fromkeys(queries))
    try:
        project = gl.projects.get(project_path)
    except Exception as exc:
        return {
            "status": "error",
            "project_path": project_path,
            "error_category": type(exc).__name__,
        }

    branch_names = _list_branch_names(project, MAX_BRANCH_CATALOG)
    catalog_truncated = len(branch_names) >= MAX_BRANCH_CATALOG
    candidate_names = [name for name in branch_names if name != declared_branch]
    ranked_names = rank_dependency_branches(
        candidate_names,
        source_branch=source_branch,
        queries=query_list,
        context_text=context_text,
    )[:MAX_BRANCH_CANDIDATES]

    completed = []
    incomplete = []
    for branch_name in ranked_names:
        verification = verify_interfaces_on_branch(project, branch_name, package_path, query_list)
        if verification.get("verification_complete"):
            completed.append(verification)
        else:
            incomplete.append(verification)

    query_count = len(query_list)
    full_candidates = [
        item
        for item in completed
        if query_count and len(item.get("matched_queries") or ()) == query_count
    ]
    partial_candidates = [
        item
        for item in completed
        if item.get("matched_queries") and len(item.get("matched_queries") or ()) < query_count
    ]
    if len(full_candidates) == 1:
        candidate_kind = "unique_verified_candidate"
    elif len(full_candidates) > 1:
        candidate_kind = "multiple_verified_candidates"
    elif partial_candidates:
        candidate_kind = "partial_candidate"
    else:
        candidate_kind = "no_verified_candidate"

    return {
        "status": "searched",
        "project_path": project_path,
        "declared_branch": declared_branch,
        "package_path": package_path,
        "queries": [asdict(query) for query in query_list],
        "candidate_kind": candidate_kind,
        "verified_candidates": full_candidates[:MAX_VERIFIED_BRANCH_MATCHES],
        "partial_candidates": partial_candidates[:MAX_VERIFIED_BRANCH_MATCHES],
        "checked_branch_count": len(ranked_names),
        "incomplete_candidate_count": len(incomplete),
        "catalog_truncated": catalog_truncated,
    }


def search_missing_interface_elsewhere(
    gl,
    project_path: str,
    query: InterfaceQuery,
    declared_branch: str,
) -> dict[str, Any]:
    """Read-only detective work for an interface missing from the declared branch: which other branches of the
    SAME upstream project still contain it, the last commit that touched its expected path on the declared
    branch, and (if still not found anywhere in that project) whether a sibling project in the same GitLab
    namespace now hosts it instead (e.g. the package was migrated to a different repository during a
    refactor). Performs GitLab read operations only; never writes to any repository."""
    search = search_missing_interfaces_elsewhere(
        gl,
        project_path,
        [query],
        declared_branch,
        query.package,
    )
    if search.get("status") != "searched":
        return search
    try:
        project = gl.projects.get(project_path)
    except Exception as exc:
        return {"status": "error", "message": f"upstream project lookup failed: {type(exc).__name__}"}

    present_branches = [
        {
            "branch": str(candidate.get("branch") or ""),
            "file_path": str((candidate.get("file_paths") or {}).get(query.filename) or ""),
        }
        for candidate in search.get("verified_candidates") or ()
    ]

    removal = None
    for candidate_path in (f"{query.interface}.{query.kind}", f"{query.kind}/{query.filename}"):
        removal = _last_touching_commit(project, candidate_path, declared_branch)
        if removal is not None:
            break

    migrated_to = []
    if not present_branches:
        migrated_to = search_missing_interface_across_namespace(gl, project_path, query)

    return {
        "status": "searched",
        "project_path": project_path,
        "declared_branch": declared_branch,
        "query": asdict(query),
        "present_on_branches": present_branches[:5],
        "removal_commit": removal,
        "migrated_to_projects": migrated_to,
        "candidate_kind": search.get("candidate_kind"),
        "checked_branch_count": search.get("checked_branch_count", 0),
        "catalog_truncated": bool(search.get("catalog_truncated")),
    }


def search_missing_interface_across_namespace(
    gl,
    current_project_path: str,
    query: InterfaceQuery,
) -> list[dict[str, Any]]:
    """Read-only: search sibling projects in the same top-level GitLab namespace for this interface file on
    their default branch, bounded by MAX_NAMESPACE_PROJECTS. Detects the 'moved to a different repository'
    refactor case. Never writes anything; returns at most MAX_DISCOVERY_MATCHES candidates."""
    project_id = str(current_project_path or "").strip("/")
    if not _PROJECT_PATH.fullmatch(project_id):
        return []
    namespace = project_id.split("/", 1)[0]
    try:
        group = gl.groups.get(namespace)
        summaries = list(group.projects.list(include_subgroups=True, per_page=100, get_all=True))
    except Exception:
        return []
    if len(summaries) > MAX_NAMESPACE_PROJECTS:
        return []

    matches = []
    for summary in summaries:
        candidate_path = str(_object_value(summary, "path_with_namespace") or "")
        if (
            not candidate_path
            or candidate_path == project_id
            or bool(_object_value(summary, "archived", False))
            or "third-party" in candidate_path.split("/")
        ):
            continue
        default_branch = str(_object_value(summary, "default_branch") or "").strip()
        if not default_branch:
            continue
        try:
            candidate_project = gl.projects.get(candidate_path)
            path = _branch_file_path(candidate_project, default_branch, query)
        except Exception:
            path = ""
        if path:
            matches.append({"project_path": candidate_path, "branch": default_branch, "file_path": path})
            if len(matches) > MAX_DISCOVERY_MATCHES:
                return matches[:MAX_DISCOVERY_MATCHES]
    return matches


def describe_missing_interface_evidence(evidence: dict[str, Any]) -> str:
    """Render read-only upstream evidence as owner-facing natural language. Purely template-based (no model
    text) so the sentence can only state facts this module has itself verified via GitLab read APIs."""
    if evidence.get("status") != "searched":
        return ""
    project_path = str(evidence.get("project_path") or "")
    declared_branch = str(evidence.get("declared_branch") or "")
    query = evidence.get("query") or {}
    filename = str(query.get("filename") or "")
    if not project_path or not filename:
        return ""
    parts = [f"上游包 {project_path}（当前声明分支 {declared_branch}）中不存在 {filename}。"]
    removal = evidence.get("removal_commit")
    if isinstance(removal, dict) and removal.get("commit_sha"):
        who_when = ""
        if removal.get("author") and removal.get("committed_date"):
            who_when = f"（{removal['author']}，{removal['committed_date']}）"
        title = f'，说明："{removal["title"]}"' if removal.get("title") else ""
        parts.append(f"该文件最后一次被提交 {removal['commit_sha']}{who_when} 修改{title}。")
    present = evidence.get("present_on_branches") or []
    migrated_to = evidence.get("migrated_to_projects") or []
    if present:
        branch_list = "、".join(f"`{item['branch']}`" for item in present[:3])
        parts.append(f"分支 {branch_list} 上仍包含该文件，可作为替代来源。")
    elif migrated_to:
        if len(migrated_to) == 1:
            target = migrated_to[0]
            parts.append(
                f"该文件目前实际位于仓库 `{target['project_path']}`"
                f"（分支 `{target['branch']}`，路径 `{target['file_path']}`），疑似已迁移到该仓库。"
            )
        else:
            targets = "、".join(f"`{item['project_path']}`" for item in migrated_to[:3])
            parts.append(f"在同一命名空间下的 {targets} 等多个仓库中都发现了同名文件，无法唯一确定迁移目标。")
    else:
        parts.append("已检查的其他分支和同命名空间下的仓库均未找到该文件。")
    suggestion = evidence.get("suggested_manifest_change")
    if isinstance(suggestion, str) and suggestion:
        parts.append(suggestion)
    return " ".join(parts)


def build_deps_manifest_migration_suggestion(
    repo_dir: str,
    old_project_path: str,
    old_branch: str,
    new_project_path: str,
) -> str:
    """Render a human-reviewable text suggestion for updating a deps.yml entry whose upstream package appears
    to have migrated to a different repository. Only returns text when the exact declared block (matching
    both old_project_path and old_branch, adjacent to each other) can be located unambiguously. Never writes to
    disk, never commits, never pushes — purely advisory text for a human to review and apply themselves. The
    branch is deliberately left unchanged: migrating repositories does not imply the same branch name exists
    or is correct on the new repository, so a human must confirm which branch to declare."""
    root = Path(repo_dir).resolve()
    manifest_path = _dependency_manifest_path(repo_dir)
    if not manifest_path:
        return ""
    text = _safe_text_file(root, manifest_path)
    if not text:
        return ""
    block_re = re.compile(
        r"(?P<url_indent>[ \t]*)url:[ \t]*\S*" + re.escape(old_project_path) + r"(?:\.git)?[ \t]*\n"
        r"(?P<mid>(?:[ \t]*\S.*\n)*?)"
        r"(?P<branch_indent>[ \t]*)branch:[ \t]*" + re.escape(old_branch) + r"[ \t]*\n?"
    )
    matches = list(block_re.finditer(text))
    if len(matches) != 1:
        return ""
    match = matches[0]
    old_block = match.group(0).rstrip("\n")
    gitlab_host = os.environ.get("GITLAB_HOST", "gitlab.example.com")
    new_url_line = f"{match.group('url_indent')}url: git@{gitlab_host}:{new_project_path}.git"
    unchanged_branch_line = f"{match.group('branch_indent')}branch: {old_branch}"
    new_block = "\n".join(
        line for line in (new_url_line, match.group("mid").rstrip("\n"), unchanged_branch_line) if line
    )
    return (
        f"建议人工核实并修改 {manifest_path}（不会自动提交）：\n"
        f"--- 修改前 ---\n{old_block}\n"
        f"--- 修改后（仅更新仓库地址，分支需要人工确认是否仍然存在/正确）---\n{new_block}"
    )


def _decode_project_file(source) -> bytes:
    decoded = source.decode() if hasattr(source, "decode") else None
    if isinstance(decoded, bytes):
        return decoded
    if isinstance(decoded, str):
        return decoded.encode("utf-8")
    content = getattr(source, "content", "")
    if isinstance(content, bytes):
        return base64.b64decode(content)
    return base64.b64decode(str(content).encode("ascii"))


def _describe_candidate_search(
    provider: dict[str, Any],
    current_branch: dict[str, Any],
    candidate_search: dict[str, Any],
) -> str:
    missing = "、".join(str(name) for name in current_branch.get("missing_queries") or ())
    prefix = (
        f"依赖仓库 {provider.get('project_path')} 的声明分支 {provider.get('declared_branch')}"
        f"（{provider.get('resolved_sha')}）缺少已观察接口：{missing or '未识别'}。"
    )
    candidates = candidate_search.get("verified_candidates") or ()
    kind = str(candidate_search.get("candidate_kind") or "")
    if kind == "unique_verified_candidate" and candidates:
        candidate = candidates[0]
        return (
            f"{prefix} 已确认候选分支 {candidate.get('branch')}（{candidate.get('resolved_sha')}）"
            "包含全部已观察接口；请维护者确认整体兼容性后调整依赖。"
        )
    if kind == "multiple_verified_candidates":
        names = "、".join(str(item.get("branch") or "") for item in candidates)
        return f"{prefix} 已找到多个包含全部已观察接口的候选：{names}；请维护者选择并确认。"
    if kind == "partial_candidate":
        return f"{prefix} 仅找到包含部分接口的候选，不能据此自动选择依赖分支。"
    return f"{prefix} 在本次受限检查范围内未确认唯一完整候选。"


def _dependency_suggested_action(result: dict[str, Any]) -> str:
    candidates = result.get("verified_candidates") or ()
    candidate_kind = str(result.get("candidate_kind") or "")
    project_path = str(result.get("project_path") or "上游依赖仓库")
    if candidate_kind == "unique_verified_candidate" and candidates:
        branch = str(candidates[0].get("branch") or "")
        return (
            f"请维护者确认候选分支 {branch} 的整体 ABI 和行为兼容性；确认后人工调整 {project_path} 的"
            "依赖分支。系统不会自动切换依赖。"
        )
    if candidate_kind == "multiple_verified_candidates" and candidates:
        branches = "、".join(str(item.get("branch") or "") for item in candidates)
        return (
            f"已验证多个完整候选分支：{branches}。请选择并确认其中一个分支的整体兼容性后人工调整依赖；"
            "系统不会自动选择。"
        )
    if candidate_kind == "partial_candidate":
        return "当前只找到包含部分接口的候选；请维护者确认正确的上游版本或补齐接口，禁止自动切换。"
    return "请维护者确认正确的上游依赖版本或在上游补齐缺失接口；当前仓库无法安全生成这些接口。"


def dependency_evidence_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Return bounded, JSON-safe dependency facts for terminal persistence."""
    queries = [
        {"filename": str(item.get("filename") or "")[:200]}
        for item in result.get("queries") or ()
        if isinstance(item, dict) and str(item.get("filename") or "")
    ][:20]

    def candidate_snapshot(item: object) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        paths = item.get("file_paths") if isinstance(item.get("file_paths"), dict) else {}
        return {
            "branch": str(item.get("branch") or "")[:300],
            "resolved_sha": str(item.get("resolved_sha") or "")[:100],
            "verification_complete": bool(item.get("verification_complete")),
            "matched_queries": [str(value)[:200] for value in item.get("matched_queries") or ()][:20],
            "missing_queries": [str(value)[:200] for value in item.get("missing_queries") or ()][:20],
            "file_paths": {
                str(name)[:200]: str(path)[:1_000]
                for name, path in list(paths.items())[:20]
            },
        }

    candidates = [
        snapshot
        for snapshot in (candidate_snapshot(item) for item in result.get("verified_candidates") or ())
        if snapshot is not None
    ][:MAX_VERIFIED_BRANCH_MATCHES]
    partial_candidates = [
        snapshot
        for snapshot in (candidate_snapshot(item) for item in result.get("partial_candidates") or ())
        if snapshot is not None
    ][:MAX_VERIFIED_BRANCH_MATCHES]
    current = candidate_snapshot(result.get("current_branch")) or {}
    return {
        "project_path": str(result.get("project_path") or "")[:500],
        "declared_branch": str(result.get("declared_branch") or "")[:300],
        "declared_sha": str(result.get("declared_sha") or "")[:100],
        "package_path": str(result.get("package_path") or "")[:1_000],
        "queries": queries,
        "current_branch": current,
        "candidate_kind": str(result.get("candidate_kind") or "")[:100],
        "verified_candidates": candidates,
        "partial_candidates": partial_candidates,
        "checked_branch_count": min(max(int(result.get("checked_branch_count") or 0), 0), MAX_BRANCH_CANDIDATES),
        "catalog_truncated": bool(result.get("catalog_truncated")),
    }


def build_dependency_blocker(result: dict[str, Any], job_name: str) -> dict[str, Any]:
    """Build one Schema v1 external-dependency blocker exclusively from verified resolver facts."""
    root_cause = str(result.get("owner_facing_analysis") or "").strip()
    diagnostic = str(result.get("primary_diagnostic") or "").strip()
    project_path = str(result.get("project_path") or "").strip()
    declared_branch = str(result.get("declared_branch") or "").strip()
    declared_sha = str(result.get("declared_sha") or "").strip()
    current = result.get("current_branch") if isinstance(result.get("current_branch"), dict) else {}
    missing = "、".join(str(value) for value in current.get("missing_queries") or ())
    repository_evidence = []
    if project_path and declared_branch and declared_sha and current.get("verification_complete") and missing:
        repository_evidence.append({
            "kind": "declared_dependency",
            "locator": f"{project_path}@{declared_sha}",
            "observation": f"声明分支 {declared_branch} 的 package 目录已完成只读核验，缺少：{missing}",
        })
    return {
        "schema_version": 1,
        "outcome": "blocked",
        "job_name": str(job_name or ""),
        "blocker_type": "external_dependency",
        "root_cause": root_cause,
        "ci_evidence": [{"job_name": str(job_name or ""), "observation": diagnostic}],
        "repository_evidence": repository_evidence,
        "attempted_repairs": ["只读核验当前声明分支和有界候选分支中的接口文件。"],
        "why_no_safe_repo_change": "缺失接口由声明依赖提供，当前仓库不能安全生成或猜测上游接口定义。",
        "suggested_action": _dependency_suggested_action(result),
    }


def resolve_current_dependency_evidence(
    gl,
    repo_dir: str,
    diagnostic: str,
    current_project_id: str = "",
    source_branch: str = "",
) -> dict[str, Any]:
    """Resolve one current declared interface using read-only GitLab APIs."""
    queries = derive_interface_queries(diagnostic)
    if not queries:
        if derive_missing_package_names(diagnostic) and current_project_id:
            return discover_unique_package_provider(gl, repo_dir, current_project_id, diagnostic)
        return {"status": "not_applicable", "message": "diagnostic does not identify a dependency interface"}
    try:
        dependencies = parse_declared_dependencies(repo_dir)
    except DependencyManifestError as exc:
        return {"status": "error", "message": str(exc)}
    if not dependencies:
        return {"status": "not_found", "message": "current checkout declares no usable dependencies"}

    package_names = list(dict.fromkeys(query.package for query in queries if query.package))
    if len(package_names) != 1:
        return {
            "status": "ambiguous" if package_names else "not_found",
            "message": "diagnostic does not identify exactly one dependency package",
            "queries": [asdict(query) for query in queries],
        }
    provider = resolve_declared_package_provider(gl, dependencies, package_names[0])
    if provider.get("status") != "resolved":
        return {
            **provider,
            "queries": [asdict(query) for query in queries],
        }

    dependency = DeclaredDependency(
        str(provider.get("module") or ""),
        str(provider["project_path"]),
        str(provider["declared_branch"]),
    )
    resolved_sha = str(provider["resolved_sha"])
    try:
        project = gl.projects.get(dependency.project_path)
    except Exception as exc:
        return {
            "status": "error",
            "message": f"declared dependency lookup failed: {type(exc).__name__}",
        }

    current_branch = verify_interfaces_on_branch(
        project,
        dependency.branch,
        str(provider.get("package_path") or ""),
        queries,
    )
    if not current_branch.get("verification_complete"):
        return {
            "status": (
                "limit_exceeded"
                if current_branch.get("error_category") == "scope_limit_exceeded"
                else "error"
            ),
            "message": "当前依赖 package 目录未完成核验",
            "project_path": dependency.project_path,
            "declared_branch": dependency.branch,
            "declared_sha": resolved_sha,
            "package_path": str(provider.get("package_path") or ""),
            "current_branch": current_branch,
        }

    if current_branch.get("missing_queries"):
        candidate_search = search_missing_interfaces_elsewhere(
            gl,
            dependency.project_path,
            queries,
            dependency.branch,
            str(provider.get("package_path") or ""),
            source_branch=source_branch,
            context_text=diagnostic,
        )
        result = {
            "status": "not_found",
            "evidence_kind": "declared_interface_missing",
            "module": dependency.module,
            "package_name": package_names[0],
            "project_path": dependency.project_path,
            "declared_branch": dependency.branch,
            "declared_sha": resolved_sha,
            "resolved_sha": resolved_sha,
            "package_path": str(provider.get("package_path") or ""),
            "queries": [asdict(query) for query in queries],
            "current_branch": current_branch,
            "candidate_kind": str(candidate_search.get("candidate_kind") or "no_verified_candidate"),
            "verified_candidates": list(candidate_search.get("verified_candidates") or ()),
            "partial_candidates": list(candidate_search.get("partial_candidates") or ()),
            "checked_branch_count": int(candidate_search.get("checked_branch_count") or 0),
            "incomplete_candidate_count": int(candidate_search.get("incomplete_candidate_count") or 0),
            "catalog_truncated": bool(candidate_search.get("catalog_truncated")),
            "primary_diagnostic": str(diagnostic or "")[:2_000],
        }
        result["owner_facing_analysis"] = _describe_candidate_search(provider, current_branch, candidate_search)

        if len(queries) == 1 and not result["verified_candidates"]:
            query = queries[0]
            migrated_to = search_missing_interface_across_namespace(gl, dependency.project_path, query)
            evidence = {
                "status": "searched",
                "project_path": dependency.project_path,
                "declared_branch": dependency.branch,
                "query": asdict(query),
                "present_on_branches": [],
                "removal_commit": _last_touching_commit(project, query.filename, dependency.branch),
                "migrated_to_projects": migrated_to,
            }
            if len(migrated_to) == 1:
                evidence["suggested_manifest_change"] = build_deps_manifest_migration_suggestion(
                    repo_dir,
                    dependency.project_path,
                    dependency.branch,
                    migrated_to[0]["project_path"],
                )
            legacy_analysis = describe_missing_interface_evidence(evidence)
            if legacy_analysis:
                result["upstream_evidence"] = evidence
                result["owner_facing_analysis"] = f"{result['owner_facing_analysis']} {legacy_analysis}"
        elif len(queries) == 1:
            candidate = result["verified_candidates"][0]
            result["upstream_evidence"] = {
                "status": "searched",
                "project_path": dependency.project_path,
                "declared_branch": dependency.branch,
                "query": asdict(queries[0]),
                "present_on_branches": [
                    {
                        "branch": candidate["branch"],
                        "file_path": candidate["file_paths"][queries[0].filename],
                    }
                ],
                "removal_commit": None,
                "migrated_to_projects": [],
            }
        return result

    query = queries[0]
    path = str((current_branch.get("file_paths") or {}).get(query.filename) or "")
    if len(queries) != 1 or not path:
        return {
            "status": "resolved",
            "module": dependency.module,
            "project_path": dependency.project_path,
            "declared_branch": dependency.branch,
            "resolved_sha": resolved_sha,
            "package_path": str(provider.get("package_path") or ""),
            "interfaces": current_branch,
        }
    try:
        raw = _decode_project_file(project.files.get(file_path=path, ref=resolved_sha))
    except Exception as exc:
        return {"status": "error", "message": f"current dependency file read failed: {type(exc).__name__}"}
    if len(raw) > MAX_FILE_BYTES:
        return {"status": "limit_exceeded", "message": f"dependency interface exceeds {MAX_FILE_BYTES} bytes"}
    content = raw.decode("utf-8", errors="replace")[:MAX_EVIDENCE_CHARS]
    return {
        "status": "resolved",
        "module": dependency.module,
        "project_path": dependency.project_path,
        "declared_branch": dependency.branch,
        "resolved_sha": resolved_sha,
        "package_path": str(provider.get("package_path") or ""),
        "file_path": path,
        "query": asdict(query),
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "content": content,
    }


def _message_tool_calls(message) -> list[dict[str, Any]]:
    calls = message.get("tool_calls", []) if isinstance(message, dict) else getattr(message, "tool_calls", [])
    return [call for call in calls if isinstance(call, dict)]


def _message_content(message) -> str:
    return str(message.get("content", "")) if isinstance(message, dict) else str(getattr(message, "content", ""))


def _message_tool_call_id(message) -> str:
    value = message.get("tool_call_id", "") if isinstance(message, dict) else getattr(message, "tool_call_id", "")
    return str(value or "")


def dependency_evidence_from_messages(messages: list, root_cause_id: str = "") -> list[dict[str, Any]]:
    """Recover successful resolver snapshots from actual tool exchanges only."""
    resolver_calls: dict[str, dict[str, Any]] = {}
    evidence = []
    seen = set()
    total_chars = 0
    for message in messages:
        for tool_call in _message_tool_calls(message):
            name = tool_call.get("name") or tool_call.get("function", {}).get("name")
            if name != "resolve_dependency_evidence_tool":
                continue
            arguments = tool_call.get("args")
            if arguments is None:
                arguments = tool_call.get("function", {}).get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            resolver_calls[str(tool_call.get("id") or "")] = arguments if isinstance(arguments, dict) else {}

        arguments = resolver_calls.get(_message_tool_call_id(message))
        if arguments is None:
            continue
        try:
            result = json.loads(_message_content(message))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(result, dict) or result.get("status") != "resolved":
            continue
        resolved_root_cause_id = str(result.get("root_cause_id") or arguments.get("root_cause_id") or "")
        if root_cause_id and resolved_root_cause_id != root_cause_id:
            continue
        key = (
            str(result.get("project_path") or ""),
            str(result.get("resolved_sha") or ""),
            str(result.get("file_path") or ""),
            str(result.get("content_sha256") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        item = {
            "root_cause_id": resolved_root_cause_id,
            "evidence_kind": str(result.get("evidence_kind") or "declared_interface"),
            "package_name": str(result.get("package_name") or ""),
            "module": str(result.get("module") or ""),
            "project_path": key[0],
            "repository_url": str(result.get("repository_url") or ""),
            "declared_branch": str(result.get("declared_branch") or ""),
            "resolved_sha": key[1],
            "package_path": str(result.get("package_path") or ""),
            "file_path": key[2],
            "dependency_manifest_path": str(result.get("dependency_manifest_path") or ""),
            "content_sha256": key[3],
            "content": str(result.get("content") or ""),
        }
        metadata_chars = sum(len(str(value)) for name, value in item.items() if name != "content")
        remaining = MAX_EVIDENCE_CHARS - total_chars - metadata_chars
        if remaining <= 0:
            break
        item["content"] = item["content"][:remaining]
        total_chars += metadata_chars + len(item["content"])
        evidence.append(item)
    return evidence


def dependency_blockers_from_messages(messages: list) -> list[dict[str, Any]]:
    """Recover validated external-dependency blockers from paired resolver tool exchanges only."""
    resolver_calls: dict[str, dict[str, Any]] = {}
    records = []
    seen = set()
    for message in messages:
        for tool_call in _message_tool_calls(message):
            name = tool_call.get("name") or tool_call.get("function", {}).get("name")
            if name != "resolve_dependency_evidence_tool":
                continue
            arguments = tool_call.get("args")
            if arguments is None:
                arguments = tool_call.get("function", {}).get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            resolver_calls[str(tool_call.get("id") or "")] = arguments if isinstance(arguments, dict) else {}

        arguments = resolver_calls.get(_message_tool_call_id(message))
        if arguments is None:
            continue
        try:
            result = json.loads(_message_content(message))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(result, dict) or result.get("status") != "blocked":
            continue
        job_name = str(result.get("job_name") or arguments.get("job_name") or "")
        blocker = result.get("blocker")
        if validate_blocker_record(blocker, job_name) is not None:
            continue
        if not isinstance(blocker, dict) or blocker.get("blocker_type") != "external_dependency":
            continue
        root_cause_id = str(result.get("root_cause_id") or arguments.get("root_cause_id") or "")
        identity = (root_cause_id, job_name)
        if identity in seen:
            continue
        seen.add(identity)
        evidence = result.get("dependency_evidence")
        if not isinstance(evidence, dict):
            evidence = dependency_evidence_snapshot(result)
        records.append({
            "root_cause_id": sanitize_failure_text(root_cause_id, 200),
            "job_name": sanitize_failure_text(job_name, 120),
            "blocker_type": "external_dependency",
            "root_cause": sanitize_failure_text(blocker.get("root_cause"), 1_000),
            "suggested_action": sanitize_failure_text(blocker.get("suggested_action"), 1_000),
            "dependency_evidence": evidence,
        })
        if len(records) >= 20:
            break
    return records
