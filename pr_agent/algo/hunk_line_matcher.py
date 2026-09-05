"""Deterministic, diff-scoped line-number lookup for /improve suggestions.

The LLM self-reflection step used to be asked to *guess* which line numbers
in the diff a suggestion's `existing_code` corresponds to. That guess can
land outside this PR's actual changes (a different, unrelated part of the
file that merely looks similar), which then makes GitLab reject the inline
suggestion (`file_not_in_diff` / `line_out_of_range` / a 400 "invalid line
code" error) with the failure only visible after a wasted API call.

This module replaces that guess with a plain text search restricted to the
`__new hunk__` blocks of the diff text already generated for this PR (see
`pr_agent.algo.git_patch_processing.decouple_and_convert_to_hunks_with_lines_numbers`
for the exact format). Because the search space is exactly the diff's own
new-hunk lines, a match can never point outside the diff -- if the text
can't be found there, `None` is returned and the caller should drop the
suggestion rather than trust an unverified line range.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

_FILE_HEADER_RE = re.compile(r"^## File: '(.+?)'\s*$", re.MULTILINE)
_NEW_HUNK_LINE_RE = re.compile(r"^(\d+) ([ +])(.*)$")


def _split_by_file(diff_with_line_numbers: str) -> List[Tuple[str, str]]:
    """Return [(filename, section_text), ...] for each '## File: ...' block."""
    matches = list(_FILE_HEADER_RE.finditer(diff_with_line_numbers))
    sections = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_with_line_numbers)
        sections.append((m.group(1).strip(), diff_with_line_numbers[start:end]))
    return sections


def _find_file_section(diff_with_line_numbers: str, relevant_file: str) -> Optional[str]:
    relevant_file = relevant_file.strip()
    sections = _split_by_file(diff_with_line_numbers)
    for filename, section in sections:
        if filename == relevant_file:
            return section
    # tolerate path prefix/suffix drift (e.g. missing/extra leading "./")
    for filename, section in sections:
        if filename.endswith(relevant_file) or relevant_file.endswith(filename):
            return section
    return None


def _extract_new_hunk_blocks(section: str) -> List[List[Tuple[int, str]]]:
    """Return one list of (line_number, content) per '__new hunk__' block in
    this file's section. `content` has the leading diff-prefix char ('+' or
    ' ') already stripped."""
    blocks: List[List[Tuple[int, str]]] = []
    current: Optional[List[Tuple[int, str]]] = None
    in_new_hunk = False
    for line in section.splitlines():
        if line.strip() == "__new hunk__":
            in_new_hunk = True
            current = []
            blocks.append(current)
            continue
        if line.strip() == "__old hunk__":
            in_new_hunk = False
            current = None
            continue
        if line.startswith("@@"):
            in_new_hunk = False
            current = None
            continue
        if in_new_hunk and current is not None:
            m = _NEW_HUNK_LINE_RE.match(line)
            if m:
                line_no = int(m.group(1))
                content = m.group(3)
                current.append((line_no, content))
    return [b for b in blocks if b]


def _search_block(block: List[Tuple[int, str]], existing_lines: List[str]) -> Optional[Tuple[int, int]]:
    window = len(existing_lines)
    matches = []
    for start in range(0, len(block) - window + 1):
        candidate = block[start:start + window]
        if all(candidate[i][1].strip() == existing_lines[i].strip() for i in range(window)):
            matches.append((candidate[0][0], candidate[-1][0]))
    if len(matches) == 1:
        return matches[0]
    return None


def find_lines_in_new_hunk(diff_with_line_numbers: str, relevant_file: str,
                           existing_code: str) -> Optional[Tuple[int, int]]:
    """Find the (line_start, line_end) in the diff's own '__new hunk__' text
    that `existing_code` corresponds to, or None if it can't be found there
    unambiguously.

    Only searches within `__new hunk__` blocks for the matched file, so a
    match can never point outside this PR's actual diff. Returns None (never
    raises) for: file not present in the diff, empty `existing_code`, no
    match, or an ambiguous (2+) match.
    """
    if not diff_with_line_numbers or not relevant_file or not existing_code or not existing_code.strip():
        return None
    section = _find_file_section(diff_with_line_numbers, relevant_file)
    if section is None:
        return None
    existing_lines = [ln for ln in existing_code.splitlines() if ln.strip()] or [existing_code]
    for block in _extract_new_hunk_blocks(section):
        if len(block) < len(existing_lines):
            continue
        result = _search_block(block, existing_lines)
        if result is not None:
            return result
    return None
