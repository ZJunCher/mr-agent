#!/usr/bin/env python3
"""
V4A Patch Format Parser

Parses the V4A patch format used by codex, cline, and other coding agents.

V4A Format:
    *** Begin Patch
    *** Update File: path/to/file.py
    @@ optional context hint @@
     context line (space prefix)
    -removed line (minus prefix)
    +added line (plus prefix)
    *** Add File: path/to/new.py
    +new file content
    +line 2
    *** Delete File: path/to/old.py
    *** Move File: old/path.py -> new/path.py
    *** End Patch

Usage:
    from tools.patch_parser import parse_v4a_patch, apply_v4a_operations

    operations, error = parse_v4a_patch(patch_content)
    if error:
        print(f"Parse error: {error}")
    else:
        result = apply_v4a_operations(operations, file_ops)
"""

import difflib
import inspect
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Any
from enum import Enum


class OperationType(Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


@dataclass
class HunkLine:
    """A single line in a patch hunk."""
    prefix: str  # ' ', '-', or '+'
    content: str


@dataclass
class Hunk:
    """A group of changes within a file."""
    context_hint: Optional[str] = None
    lines: List[HunkLine] = field(default_factory=list)


@dataclass
class PatchOperation:
    """A single operation in a V4A patch."""
    operation: OperationType
    file_path: str
    new_path: Optional[str] = None  # For move operations
    hunks: List[Hunk] = field(default_factory=list)
    content: Optional[str] = None  # For add file operations


def parse_v4a_patch(patch_content: str) -> Tuple[List[PatchOperation], Optional[str]]:
    """
    Parse a V4A format patch.

    Args:
        patch_content: The patch text in V4A format

    Returns:
        Tuple of (operations, error_message)
        - If successful: (list_of_operations, None)
        - If failed: ([], error_description)
    """
    # Split into lines, tolerating a CRLF patch body: strip the trailing
    # ``\r`` from each line. Without this, a CRLF-encoded patch keeps ``\r``
    # inside every HunkLine.content and injects stray carriage returns into an
    # LF target file (and the anchored ``...\s*$`` Begin/End markers would fail
    # to match because of the trailing ``\r``).
    lines = [ln[:-1] if ln.endswith('\r') else ln for ln in patch_content.split('\n')]
    operations: List[PatchOperation] = []

    # Find patch boundaries. Markers must occupy the whole line at column 0:
    # content lines like "+*** End Patch" or " *** End Patch" (e.g. docs
    # about the patch format) must not truncate the patch or reset the
    # start boundary.
    start_idx = None
    end_idx = None
    begin_marker = re.compile(r'^\*\*\*\s*Begin\s+Patch\s*$')
    end_marker = re.compile(r'^\*\*\*\s*End\s+Patch\s*$')
    for i, line in enumerate(lines):
        if begin_marker.match(line):
            start_idx = i
        elif end_marker.match(line):
            end_idx = i
            break

    if start_idx is None:
        # Try to parse without explicit begin marker
        start_idx = -1

    if end_idx is None:
        end_idx = len(lines)

    # Parse operations between boundaries
    i = start_idx + 1
    current_op: Optional[PatchOperation] = None
    current_hunk: Optional[Hunk] = None

    while i < end_idx:
        line = lines[i]

        # Check for file operation markers
        update_match = re.match(r'\*\*\*\s*Update\s+File:\s*(.+)', line)
        add_match = re.match(r'\*\*\*\s*Add\s+File:\s*(.+)', line)
        delete_match = re.match(r'\*\*\*\s*Delete\s+File:\s*(.+)', line)
        move_match = re.match(r'\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)', line)

        if update_match:
            # Save previous operation
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.UPDATE,
                file_path=update_match.group(1).strip()
            )
            current_hunk = None

        elif add_match:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.ADD,
                file_path=add_match.group(1).strip()
            )
            current_hunk = Hunk()

        elif delete_match:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.DELETE,
                file_path=delete_match.group(1).strip()
            )
            operations.append(current_op)
            current_op = None
            current_hunk = None

        elif move_match:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.MOVE,
                file_path=move_match.group(1).strip(),
                new_path=move_match.group(2).strip()
            )
            operations.append(current_op)
            current_op = None
            current_hunk = None

        elif line.startswith('@@'):
            # Context hint / hunk marker
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)

                # Extract context hint
                hint_match = re.match(r'@@\s*(.+?)\s*@@', line)
                hint = hint_match.group(1) if hint_match else None
                current_hunk = Hunk(context_hint=hint)

        elif current_op and line:
            # Parse hunk line
            if current_hunk is None:
                current_hunk = Hunk()

            if line.startswith('+'):
                current_hunk.lines.append(HunkLine('+', line[1:]))
            elif line.startswith('-'):
                current_hunk.lines.append(HunkLine('-', line[1:]))
            elif line.startswith(' '):
                current_hunk.lines.append(HunkLine(' ', line[1:]))
            elif line.startswith('\\'):
                # "\ No newline at end of file" marker - skip
                pass
            else:
                # Treat as context line (implicit space prefix)
                current_hunk.lines.append(HunkLine(' ', line))

        i += 1

    # Don't forget the last operation
    if current_op:
        if current_hunk and current_hunk.lines:
            current_op.hunks.append(current_hunk)
        operations.append(current_op)

    # Validate the parsed result
    if not operations:
        # Empty patch is not an error — callers get [] and can decide
        return operations, None

    parse_errors: List[str] = []
    for op in operations:
        if not op.file_path:
            parse_errors.append("Operation with empty file path")
        if op.operation == OperationType.UPDATE and not op.hunks:
            parse_errors.append(f"UPDATE {op.file_path!r}: no hunks found")
        if op.operation == OperationType.MOVE and not op.new_path:
            parse_errors.append(f"MOVE {op.file_path!r}: missing destination path (expected 'src -> dst')")

    if parse_errors:
        return [], "Parse error: " + "; ".join(parse_errors)

    return operations, None



# =============================================================================
# Local PatchResult (replaces tools.file_operations.PatchResult)
# =============================================================================
# The upstream apply_v4a_operations() depends on Hermes' ShellFileOperations
# backend (read_file_raw, write_file, delete_file). We do NOT vendor that
# backend — our apply_repo_patch_tool uses `git apply` for filesystem writes.
# This local PatchResult is the data contract _validate_operations returns
# validation errors through; the actual apply phase is reimplemented in
# ut_agent/tools/apply_repo_patch.py.

from dataclasses import dataclass, field as _field
from typing import Dict as _Dict


@dataclass
class PatchResult:
    """Result from patching a file (local replacement for Hermes' PatchResult)."""
    success: bool = False
    diff: str = ""
    files_modified: list = _field(default_factory=list)
    files_created: list = _field(default_factory=list)
    files_deleted: list = _field(default_factory=list)
    lint: Optional[dict] = None
    lsp_diagnostics: Optional[str] = None
    error: Optional[str] = None
    no_change: bool = False
    note: Optional[str] = None

    def to_dict(self) -> dict:
        result: dict = {"success": self.success}
        if self.no_change:
            result["no_change"] = True
        if self.note:
            result["note"] = self.note
        if self.diff:
            result["diff"] = self.diff
        if self.files_modified:
            result["files_modified"] = self.files_modified
        if self.files_created:
            result["files_created"] = self.files_created
        if self.files_deleted:
            result["files_deleted"] = self.files_deleted
        if self.lint:
            result["lint"] = self.lint
        if self.lsp_diagnostics:
            result["lsp_diagnostics"] = self.lsp_diagnostics
        if self.error:
            result["error"] = self.error
        return result


def _count_occurrences(text: str, pattern: str) -> int:
    """Count non-overlapping occurrences of *pattern* in *text*."""
    count = 0
    start = 0
    while True:
        pos = text.find(pattern, start)
        if pos == -1:
            break
        count += 1
        start = pos + 1
    return count


def _validate_operations(
    operations: List[PatchOperation],
    file_ops: Any,
) -> List[str]:
    """Validate all operations without writing any files.

    Returns a list of error strings; an empty list means all operations
    are valid and the apply phase can proceed safely.

    For UPDATE operations, hunks are simulated in order so that later
    hunks validate against post-earlier-hunk content (matching apply order).
    """
    # Deferred import: breaks the patch_parser ↔ fuzzy_match circular dependency
    from ut_agent.tools._vendored.fuzzy_match import fuzzy_find_and_replace

    errors: List[str] = []
    real_change_count = 0

    # Virtual filesystem overlay so inter-op state (notably a MOVE creating the
    # destination a later UPDATE targets) validates correctly. Maps a path to
    # its pending content; ``None`` marks a path moved/deleted away. UPDATE and
    # MOVE reads consult this overlay before hitting disk.
    pending_content: dict = {}   # path -> content produced by an earlier op
    removed_paths: set = set()   # paths a MOVE/DELETE has taken away

    def _read(path: str):
        """Read a path honoring the pending-move overlay."""
        if path in removed_paths and path not in pending_content:
            return None, "file not found"
        if path in pending_content:
            return pending_content[path], None
        r = file_ops.read_file_raw(path)
        if r.error:
            return None, r.error
        return r.content, None

    for op in operations:
        if op.operation != OperationType.UPDATE:
            real_change_count += 1
        if op.operation == OperationType.UPDATE:
            content, read_err = _read(op.file_path)
            if read_err:
                errors.append(f"{op.file_path}: {read_err}")
                continue

            simulated = content
            for hunk_index, hunk in enumerate(op.hunks, start=1):
                search_lines = [l.content for l in hunk.lines if l.prefix in {' ', '-'}]
                removed_lines = [l.content for l in hunk.lines if l.prefix == '-']
                added_lines = [l.content for l in hunk.lines if l.prefix == '+']
                if not removed_lines and not added_lines:
                    # Models occasionally emit inert anchor hunks between real
                    # changes. Ignore them without poisoning the atomic patch.
                    continue
                real_change_count += 1
                if not search_lines:
                    # Addition-only hunk: validate context hint uniqueness
                    if hunk.context_hint:
                        occurrences = _count_occurrences(simulated, hunk.context_hint)
                        if occurrences == 0:
                            errors.append(
                                f"{op.file_path}: addition-only hunk context hint "
                                f"'{hunk.context_hint}' not found"
                            )
                        elif occurrences > 1:
                            errors.append(
                                f"{op.file_path}: addition-only hunk context hint "
                                f"'{hunk.context_hint}' is ambiguous "
                                f"({occurrences} occurrences)"
                            )
                    continue

                search_pattern = '\n'.join(search_lines)
                replace_lines = [l.content for l in hunk.lines if l.prefix in {' ', '+'}]
                replacement = '\n'.join(replace_lines)

                if search_lines == replace_lines:
                    # Degenerate hunk whose -/+ lines are identical: the apply
                    # phase skips it as a no-op, so validation must not fail it
                    # — fuzzy_find_and_replace would reject the identical
                    # search/replacement with old_string/new_string guidance
                    # that has no meaning in V4A patch mode.
                    continue

                new_simulated, count, _strategy, match_error = fuzzy_find_and_replace(
                    simulated, search_pattern, replacement, replace_all=False
                )
                if count == 0:
                    # Already-applied hunk: validate as a no-op when the
                    # replacement text is already present (and the search
                    # text gone) — the edit landed earlier. Keeps multi-hunk
                    # patches from failing wholesale because one hunk was
                    # already applied in a prior call. The apply phase
                    # performs the same skip.
                    from ut_agent.tools._vendored.fuzzy_match import is_already_applied
                    if is_already_applied(simulated or "", search_pattern, replacement):
                        continue
                    label = f"'{hunk.context_hint}'" if hunk.context_hint else "(no hint)"
                    msg = (
                        f"{op.file_path}: hunk {hunk_index} {label} not found"
                        + (f" — {match_error}" if match_error else "")
                    )
                    try:
                        from ut_agent.tools._vendored.fuzzy_match import format_no_match_hint
                        msg += format_no_match_hint(match_error, count, search_pattern, simulated)
                    except Exception:
                        pass
                    errors.append(msg)
                else:
                    # Advance simulation so subsequent hunks validate correctly.
                    # Reuse the result from the call above — no second fuzzy run.
                    simulated = new_simulated
            # Record the post-update content so a later op (e.g. a MOVE of this
            # file) sees the edited version in the overlay.
            pending_content[op.file_path] = simulated

        elif op.operation == OperationType.DELETE:
            _content, read_err = _read(op.file_path)
            if read_err:
                errors.append(f"{op.file_path}: file not found for deletion")
            else:
                removed_paths.add(op.file_path)
                pending_content.pop(op.file_path, None)

        elif op.operation == OperationType.MOVE:
            if not op.new_path:
                errors.append(f"{op.file_path}: MOVE operation missing destination path")
                continue
            src_content, src_err = _read(op.file_path)
            if src_err:
                errors.append(f"{op.file_path}: source file not found for move")
            dst_content, dst_err = _read(op.new_path)
            if not dst_err:
                errors.append(
                    f"{op.new_path}: destination already exists — move would overwrite"
                )
            # Reflect the move in the overlay so a subsequent UPDATE of the
            # destination validates against the moved content, and the source
            # reads as gone. Only when the move itself validated cleanly.
            if not src_err and dst_err:
                pending_content[op.new_path] = src_content if src_content is not None else ""
                pending_content.pop(op.file_path, None)
                removed_paths.add(op.file_path)

        # ADD: parent directory creation handled by write_file; no pre-check needed.

    if not errors and real_change_count == 0:
        errors.append("Patch contains no changes (only context lines were provided)")

    return errors
