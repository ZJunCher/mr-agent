"""Deterministic safety checks for model-generated pipeline repairs."""

import re
import subprocess
from collections import Counter
from collections.abc import Iterable

_SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".idl", ".msg", ".srv", ".action", ".proto",
}
_MEMBER_ACCESS_RE = re.compile(r"\b(?P<object>[A-Za-z_]\w*)\s*->\s*(?P<member>[A-Za-z_]\w*)\b")
_STRING_RE = re.compile(r'R"[^\n]*?\([^)]*\)[^\n]*?"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//.*$")


def _run_git(repo_dir: str, args: list[str], *, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=text,
        timeout=30,
    )


def _changed_member_pairs(diff: str) -> list[tuple[str, str, str, str]]:
    pairs = []
    file_path = ""
    removed: list[tuple[str, str]] = []
    added: list[tuple[str, str]] = []

    def flush() -> None:
        removed_counts = Counter(removed)
        added_counts = Counter(added)
        common = removed_counts & added_counts
        removed_counts -= common
        added_counts -= common
        for old_object, old_member in removed_counts.elements():
            for new_object, new_member in added_counts.elements():
                if old_object == new_object and old_member != new_member:
                    pairs.append((file_path, old_object, old_member, new_member))
        removed.clear()
        added.clear()

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            file_path = line[6:]
            continue
        if line.startswith("@@"):
            flush()
            continue
        if line.startswith("-") and not line.startswith("---"):
            removed.extend(
                (match.group("object"), match.group("member"))
                for match in _MEMBER_ACCESS_RE.finditer(line[1:])
            )
        elif line.startswith("+") and not line.startswith("+++"):
            added.extend(
                (match.group("object"), match.group("member"))
                for match in _MEMBER_ACCESS_RE.finditer(line[1:])
            )
    flush()
    return sorted(set(pairs))


def _strip_non_code(text: str) -> str:
    value = _BLOCK_COMMENT_RE.sub(" ", text)
    lines = []
    for line in value.splitlines():
        line = _STRING_RE.sub(" ", line)
        lines.append(_LINE_COMMENT_RE.sub("", line))
    return "\n".join(lines)


def _decode_source(raw: bytes) -> str:
    """Decode a source blob without assuming every repository uses UTF-8."""
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _head_sources(repo_dir: str) -> list[str]:
    listed = _run_git(repo_dir, ["ls-tree", "-r", "--name-only", "HEAD"])
    if listed.returncode != 0:
        return []
    sources = []
    for path in listed.stdout.splitlines():
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if suffix not in _SOURCE_SUFFIXES:
            continue
        content = _run_git(repo_dir, ["show", f"HEAD:{path}"], text=False)
        if content.returncode == 0:
            sources.append(_strip_non_code(_decode_source(content.stdout)))
    return sources


def _has_head_member_evidence(sources: list[str], member: str) -> bool:
    escaped = re.escape(member)
    access = re.compile(rf"(?:->|\.)\s*{escaped}\b")
    cpp_declaration = re.compile(
        rf"(?m)(?:^|[;{{}}])\s*"
        rf"(?:[A-Za-z_]\w*(?:::\w+)*(?:\s*<[^;{{}}]+>)?[\s*&]+)+{escaped}\s*(?:[;={{])"
    )
    interface_declaration = re.compile(
        rf"(?m)^\s*[A-Za-z_]\w*(?:[/<>,\[\]:]\w*)*\s+{escaped}(?:\s*=.*)?\s*$"
    )
    return any(
        access.search(source) or cpp_declaration.search(source) or interface_declaration.search(source)
        for source in sources
    )


def validate_member_substitutions(
    repo_dir: str,
    evidence_sources: Iterable[str] = (),
) -> tuple[bool, str]:
    """Reject guessed pointer-member substitutions unsupported by HEAD or current contracts."""
    diff = _run_git(repo_dir, ["diff", "--unified=0", "--no-color", "HEAD"])
    if diff.returncode != 0:
        return False, f"无法检查修复 diff: {diff.stderr.strip() or 'git diff failed'}"
    substitutions = _changed_member_pairs(diff.stdout)
    if not substitutions:
        return True, ""

    sources = _head_sources(repo_dir)
    sources.extend(_strip_non_code(str(source)) for source in evidence_sources if str(source).strip())
    for path, object_name, old_member, new_member in substitutions:
        if _has_head_member_evidence(sources, new_member):
            continue
        return False, (
            f"不安全的字段替换：{path or '未知文件'} 中 {object_name}->{old_member} 被改为 "
            f"{object_name}->{new_member}，但当前 HEAD 的源码和接口定义中找不到 {new_member} 的有效证据。"
        )
    return True, ""
