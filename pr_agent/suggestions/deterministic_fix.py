"""Tier-0 deterministic repair layer for /improve suggestions (Pipeline v2).

Evolves inline_gate.py's G1-G4 checks from "detect and block" into "detect
and repair", at zero LLM cost (pure string/regex/difflib processing).
Suggestions this layer cannot repair are packaged into a RepairTask (see
run_deterministic_fix in a later part of this module) for Tier-1 (small
model) and, failing that, Tier-2 (heavy Copilot CLI channel) to act on.

Every public function here either returns a clear "could not repair" signal
(None / a task / a non-empty reason string) or a repaired result -- none of
them raise out to the caller, so a single suggestion's parsing quirk can
never break the rest of the /improve run.
"""
from __future__ import annotations

import difflib
import re
from typing import List, Optional, Tuple

from pr_agent.algo.hunk_line_matcher import find_lines_in_new_hunk
# config_loader must be imported before pr_agent.log: pr_agent.log's own
# module-level init imports pr_agent.config_loader, and if THIS module
# imports pr_agent.log first (as the very first pr_agent submodule touched
# in a fresh process), Dynaconf's custom_merge_loader re-imports pr_agent.log
# while it is still mid-init, raising a circular ImportError. Every other
# module in pr_agent/suggestions/ (inline_gate.py, inline_selfcheck.py, ...)
# follows this same "config_loader first" ordering for the same reason.
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def _cfg(key: str, default=None):
    return get_settings().get(f"pr_code_suggestions.{key}", default)


# --------------------------------------------------------------------------- #
# head-file lookup helpers (duplicated from inline_gate.py by design -- see
# "Small private helpers are duplicated per-module" in this plan's Global
# Constraints; this keeps deterministic_fix.py import-independent from the
# legacy module it is meant to eventually replace)
# --------------------------------------------------------------------------- #
def _build_head_map(git_provider) -> dict:
    files = None
    try:
        files = git_provider.get_diff_files()
    except Exception:
        files = getattr(git_provider, "diff_files", None)
    head_map = {}
    for f in files or []:
        try:
            filename = getattr(f, "filename", None)
            head = getattr(f, "head_file", None)
            if filename and head:
                head_map[filename] = head
        except Exception:
            continue
    return head_map


def _head_for(head_map: dict, relevant_file: str) -> str:
    if not relevant_file:
        return ""
    if relevant_file in head_map:
        return head_map[relevant_file]
    for filename, head in head_map.items():
        if filename.endswith(relevant_file) or relevant_file.endswith(filename):
            return head
    return ""


# --------------------------------------------------------------------------- #
# existing_code fuzzy-match repair (evolves inline_gate.py's G4 existing_mismatch)
# --------------------------------------------------------------------------- #
def fuzzy_match_existing_code(existing_code: str, head_file: str,
                               threshold: float) -> Optional[Tuple[str, int, int]]:
    """Find the most similar contiguous line range in head_file for existing_code.

    Slides a window the same length as existing_code across every position in
    head_file and scores each with difflib.SequenceMatcher. Returns
    (matched_text, line_start, line_end) [1-based, inclusive] for the best
    window when its ratio >= threshold, else None. O(n*m) in file/snippet
    length -- fine for the single-file, human-sized inputs this operates on.
    """
    if not existing_code or not head_file:
        return None
    target_lines = existing_code.splitlines()
    n = len(target_lines)
    if n == 0:
        return None
    head_lines = head_file.splitlines()
    if len(head_lines) < n:
        return None

    target_text = "\n".join(target_lines)
    best_ratio = 0.0
    best_start = None
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(target_text)
    for start in range(0, len(head_lines) - n + 1):
        matcher.set_seq1("\n".join(head_lines[start:start + n]))
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start

    if best_start is None or best_ratio < threshold:
        return None
    matched_lines = head_lines[best_start:best_start + n]
    return "\n".join(matched_lines), best_start + 1, best_start + n


def _existing_code_matches_at_claimed_lines(suggestion: dict, head_file: str) -> bool:
    """Positional check: does existing_code actually sit at the claimed
    relevant_lines_start/end in head_file?

    This is deliberately stricter than the legacy G4 check (which only
    verifies the text exists *somewhere* in the file via a stripped-line set
    membership test): "right text, wrong line number" still passes the legacy
    check but would silently corrupt unrelated code when GitLab applies the
    line-anchored suggestion, which is exactly the class of bug this
    redesign exists to catch.
    """
    existing = suggestion.get("existing_code", "") or ""
    existing_lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]
    if not existing_lines:
        return True
    try:
        start = int(suggestion.get("relevant_lines_start"))
        end = int(suggestion.get("relevant_lines_end"))
    except Exception:
        return False
    head_lines = head_file.splitlines()
    if start <= 0 or end < start or start > len(head_lines):
        return False
    claimed = [ln.strip() for ln in head_lines[start - 1:end] if ln.strip()]
    matches = sum(1 for a, b in zip(existing_lines, claimed) if a == b)
    return (matches / len(existing_lines)) >= 0.7


def repair_existing_mismatch(suggestion: dict, head_file: str,
                              threshold: float = 0.85) -> Optional[dict]:
    """Repair a suggestion whose existing_code doesn't sit at its claimed
    line range. Returns a NEW dict (the input is never mutated) with
    corrected existing_code/relevant_lines_start/relevant_lines_end, or None
    when no confidently-similar window is found (caller should escalate)."""
    match = fuzzy_match_existing_code(suggestion.get("existing_code", "") or "", head_file, threshold)
    if match is None:
        return None
    matched_text, line_start, line_end = match
    fixed = dict(suggestion)
    fixed["existing_code"] = matched_text
    fixed["relevant_lines_start"] = line_start
    fixed["relevant_lines_end"] = line_end
    return fixed


def _brace_delta(code: str) -> int:
    stripped = re.sub(r'//[^\n]*|#[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "", code or "")
    return stripped.count("{") - stripped.count("}")


def validate_repaired_suggestion(suggestion: dict, head_file: str) -> str:
    """Re-run the deterministic checks against a Tier-1/Tier-2 repaired
    suggestion (spec: "must re-pass the deterministic checks -- existing_code
    exact match, brace balance -- to count as repaired"). Returns "" when it
    passes, else a short human-readable failure reason. Never raises."""
    try:
        if head_file and not _existing_code_matches_at_claimed_lines(suggestion, head_file):
            return "existing_code no longer matches head_file at the claimed lines"
        existing = suggestion.get("existing_code", "") or ""
        improved = suggestion.get("improved_code", "") or ""
        if _brace_delta(existing) != _brace_delta(improved):
            return "brace balance changed between existing_code and improved_code"
        return ""
    except Exception as e:
        get_logger().warning(f"validate_repaired_suggestion error: {e}")
        return f"validation error: {e}"


# --------------------------------------------------------------------------- #
# Final deterministic position normalization (Task 1)
#
# After Tier-0 / Tier-1 resolution the model-assigned line numbers are still
# not authoritative. These helpers derive the canonical (start, end) by:
#   1. Locating existing_code via an EXACT, UNIQUE strip-match in head_file.
#   2. Verifying that location overlaps a new-side diff hunk (using the same
#      hunk-parsing logic as hunk_line_matcher.find_lines_in_new_hunk).
# Both requirements must be satisfied before a suggestion is allowed through
# to inline publication.
# --------------------------------------------------------------------------- #
def _exact_match_in_head_file(existing_code: str, head_file: str) -> Optional[Tuple[int, int]]:
    """Find the exact, unique location of existing_code in head_file.

    Uses the same strip-based comparison as find_lines_in_new_hunk so that
    indentation differences between the model's snippet and the actual file
    are tolerated. Returns (line_start, line_end) [1-based, inclusive] when
    there is exactly one match, None otherwise (no match or ambiguous).
    Never raises.
    """
    if not existing_code or not head_file:
        return None
    target_lines = [ln.strip() for ln in existing_code.splitlines() if ln.strip()]
    if not target_lines:
        return None
    head_lines = head_file.splitlines()
    n = len(target_lines)
    if len(head_lines) < n:
        return None
    matches = []
    for start in range(len(head_lines) - n + 1):
        if [head_lines[start + i].strip() for i in range(n)] == target_lines:
            matches.append((start + 1, start + n))  # 1-based inclusive
    return matches[0] if len(matches) == 1 else None


def normalize_final_position(suggestion: dict, head_file: str, diff_patches: str) -> Tuple[Optional[dict], str]:
    """Derive the authoritative line position for a resolved suggestion by:
      1. Locating existing_code via an exact, unique match in head_file
         (model-provided line numbers are discarded entirely).
      2. Confirming the same match exists in a new-side diff hunk via
         find_lines_in_new_hunk.

    Returns (corrected_suggestion, "") on success, or (None, reason) when:
    - existing_code is absent from or ambiguous in head_file (no / duplicate match)
    - the match location does not overlap any new-side diff hunk
    Never raises.
    """
    try:
        existing_code = (suggestion.get("existing_code", "") or "").strip()
        relevant_file = str(suggestion.get("relevant_file", "") or "").strip()

        if not existing_code:
            return None, "existing_code is required for final position validation"
        if not relevant_file:
            return None, "relevant_file is required for final position validation"
        if not head_file:
            return None, "head_file is required for final position validation"
        if not diff_patches:
            return None, "diff_patches is required for final position validation"

        # Step 1: locate existing_code with an exact, unique match in head_file.
        match = _exact_match_in_head_file(existing_code, head_file)
        if match is None:
            return None, "existing_code not found uniquely in head_file"
        line_start, line_end = match

        # Step 2: verify the exact match is the same location GitLab can anchor in the diff.
        hunk_result = find_lines_in_new_hunk(diff_patches, relevant_file, existing_code)
        if hunk_result is None or hunk_result != (line_start, line_end):
            return None, "existing_code location does not match a unique new-side diff hunk"

        fixed = dict(suggestion)
        fixed["relevant_lines_start"] = line_start
        fixed["relevant_lines_end"] = line_end
        return fixed, ""
    except Exception as e:
        get_logger().warning(f"normalize_final_position error: {e}")
        return None, f"error: {e}"


def apply_final_normalization(resolved: List[dict], head_map: dict,
                              diff_patches: str) -> Tuple[List[dict], List[dict]]:
    """Apply normalize_final_position to every Tier-0/Tier-1 resolved suggestion.

    Suggestions that pass are returned in the first list (with corrected line
    numbers). Suggestions that fail are silently dropped from the inline
    pipeline and returned in the second list for logging purposes. Never raises.
    """
    still_resolved: List[dict] = []
    rejected: List[dict] = []
    for sugg in resolved or []:
        try:
            relevant_file = str(sugg.get("relevant_file", "") or "").strip()
            head_file = _head_for(head_map, relevant_file)
            fixed, reason = normalize_final_position(sugg, head_file, diff_patches)
            if fixed is not None:
                still_resolved.append(fixed)
            else:
                get_logger().info(
                    "normalize_final_position rejected suggestion",
                    artifact={"relevant_file": relevant_file, "reason": reason},
                )
                rejected.append(sugg)
        except Exception as e:
            get_logger().warning(f"apply_final_normalization error for one suggestion: {e}")
            rejected.append(sugg)
    return still_resolved, rejected


# --------------------------------------------------------------------------- #
# new_dependency split (evolves inline_gate.py's G2 check)
# --------------------------------------------------------------------------- #
_INCLUDE_LINE_RE = re.compile(r'^\s*#\s*include\s*[<"][^>"]+[>"]\s*$')
_IMPORT_LINE_RE = re.compile(r'^\s*(?:import\s+[\w.]+|from\s+[\w.]+\s+import\s+.+)\s*$')


def _find_dependency_anchor(head_file: str, is_python: bool) -> Tuple[str, int]:
    """Return (anchor_line_text, 1-based_line_number) to insert the new
    include/import line after. Falls back to line 1 when the file has no
    existing dependency line of the relevant kind."""
    lines = head_file.splitlines()
    pattern = _IMPORT_LINE_RE if is_python else _INCLUDE_LINE_RE
    last_match_idx = None
    for i, line in enumerate(lines):
        if pattern.match(line):
            last_match_idx = i
    if last_match_idx is not None:
        return lines[last_match_idx], last_match_idx + 1
    return (lines[0] if lines else ""), 1


def split_new_dependency(suggestion: dict, head_file: str) -> Optional[List[dict]]:
    """Split a suggestion whose improved_code adds a new #include/import line
    into two independently appliable suggestions:
      A. inserts the new dependency line, anchored after the file's last
         existing dependency line (or at line 1 if there is none)
      B. the original change, with the dependency line(s) removed

    Returns None when no new dependency line is found in improved_code
    (nothing to split -- caller should try a different repair path)."""
    improved = suggestion.get("improved_code", "") or ""
    existing = suggestion.get("existing_code", "") or ""
    is_python = str(suggestion.get("relevant_file", "")).endswith(".py")
    pattern = _IMPORT_LINE_RE if is_python else _INCLUDE_LINE_RE

    dep_lines = []
    for ln in improved.splitlines():
        if not pattern.match(ln):
            continue
        stripped = ln.strip()
        if stripped and (stripped in head_file or stripped in existing):
            continue  # already present -- not actually a new dependency
        dep_lines.append(ln)
    if not dep_lines:
        return None

    remainder_lines = [ln for ln in improved.splitlines() if ln not in dep_lines]
    suggestion_b = dict(suggestion)
    suggestion_b["improved_code"] = "\n".join(remainder_lines)
    suggestion_b["structural_issue"] = "none"

    anchor_text, anchor_line = _find_dependency_anchor(head_file, is_python)
    suggestion_a = dict(suggestion)
    suggestion_a["existing_code"] = anchor_text
    suggestion_a["improved_code"] = (
        anchor_text + "\n" + "\n".join(dep_lines) if anchor_text else "\n".join(dep_lines)
    )
    suggestion_a["relevant_lines_start"] = anchor_line
    suggestion_a["relevant_lines_end"] = anchor_line
    suggestion_a["one_sentence_summary"] = "补充新增依赖"
    suggestion_a["suggestion_content"] = "补充建议所需的新增依赖（头文件/模块导入）。"
    suggestion_a["structural_issue"] = "none"
    return [suggestion_a, suggestion_b]

# --------------------------------------------------------------------------- #
# cross-file companion lookup
# --------------------------------------------------------------------------- #
def prepare_cross_file_context(suggestion: dict, diff_file_paths: set, head_map: dict) -> dict:
    """Enrich a cross_file suggestion with a companion-file lookup.

    Zero-LLM: this stage cannot invent the companion file's edit, only tell
    the next stage whether it CAN be attempted. Returns:
    - {"needs_tier2": True, "companion_head_file": None} when companion_file
      is empty or not part of the current diff -- Tier-1 has no reliable way
      to write to a file outside the diff, so only Tier-2 (full clone) can
      reach it.
    - {"needs_tier2": False, "companion_head_file": <str>} when companion_file
      IS part of the current diff -- Tier-1 can be given this file's content
      and attempt a two-file repair in a single LLM call.
    """
    companion = str(suggestion.get("companion_file", "") or "").strip()
    if not companion or companion not in diff_file_paths:
        return {"needs_tier2": True, "companion_head_file": None}
    return {"needs_tier2": False, "companion_head_file": head_map.get(companion, "")}


# --------------------------------------------------------------------------- #
# conflict detection (migrated from inline_selfcheck.py's 2B pre-filter --
# this module is now the single source of truth; inline_selfcheck.py
# re-exports this same function so its existing imports/tests keep working)
# --------------------------------------------------------------------------- #
_DECL_PATTERNS = (
    re.compile(r"\b[A-Za-z]\w*_\s*;"),                       # trailing-underscore member: foo_;
    re.compile(r"\bm_[A-Za-z]\w*\b"),                        # m_foo style member
    re.compile(r"^\s*[\w:<>\*&,\s]+\s+[A-Za-z_]\w*\s*;\s*$"),  # simple "type name;" declaration
)


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _is_new_declaration(code: str) -> bool:
    for line in (code or "").splitlines():
        if "(" in line or ")" in line:
            continue  # skip function signatures / calls
        for pat in _DECL_PATTERNS:
            if pat.search(line):
                return True
    return False


def _new_identifiers(code: str, head_file: str) -> set:
    head = head_file or ""
    ids: set = set()
    for m in re.finditer(r"\b([A-Za-z]\w*_)\b", code or ""):
        ids.add(m.group(1))
    for m in re.finditer(r"\b(m_[A-Za-z]\w*)\b", code or ""):
        ids.add(m.group(1))
    return {i for i in ids if not re.search(rf"\b{re.escape(i)}\b", head)}


def _lines_overlap_or_adjacent(a: dict, b: dict, adjacency: int) -> bool:
    a_s, a_e = _to_int(a.get("relevant_lines_start")), _to_int(a.get("relevant_lines_end"))
    b_s, b_e = _to_int(b.get("relevant_lines_start")), _to_int(b.get("relevant_lines_end"))
    if None in (a_s, a_e, b_s, b_e):
        return False
    if a_s <= b_e and b_s <= a_e:
        return True  # overlap
    distance = min(abs(a_s - b_e), abs(b_s - a_e))
    return distance <= adjacency


def _conflict_pair(a: dict, b: dict, head_file: str, adjacency: int) -> bool:
    # Suggestions produced by splitting the SAME original suggestion (e.g. a
    # new_dependency split into an include-line half + a body half) are
    # designed to be applied together and can never really conflict with
    # each other, even when their line ranges happen to fall within the
    # adjacency window. Legacy callers never set `_origin_id`, so this is a
    # no-op for them (both sides are None -> guarded off below).
    origin_a, origin_b = a.get("_origin_id"), b.get("_origin_id")
    if origin_a is not None and origin_a == origin_b:
        return False
    if _lines_overlap_or_adjacent(a, b, adjacency):
        return True
    a_imp, b_imp = a.get("improved_code", "") or "", b.get("improved_code", "") or ""
    if _is_new_declaration(a_imp) and _is_new_declaration(b_imp):
        return True
    if _new_identifiers(a_imp, head_file) & _new_identifiers(b_imp, head_file):
        return True
    return False


def detect_conflict_groups(suggestions: list, head_map: dict, adjacency: int = 3) -> List[List[int]]:
    """Return index groups (size >= 2) whose suggestions may collide when
    applied together to the same file. Pure local heuristics, no LLM."""
    by_file: dict = {}
    for idx, sugg in enumerate(suggestions or []):
        rel = str(sugg.get("relevant_file", "") or "").strip()
        if rel:
            by_file.setdefault(rel, []).append(idx)

    groups: List[List[int]] = []
    for rel, indices in by_file.items():
        if len(indices) < 2:
            continue
        head_file = _head_for(head_map, rel)
        parent = {i: i for i in indices}

        def find(x, parent=parent):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        has_edge = {i: False for i in indices}
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                ai, bi = indices[i], indices[j]
                if _conflict_pair(suggestions[ai], suggestions[bi], head_file, adjacency):
                    parent[find(ai)] = find(bi)
                    has_edge[ai] = has_edge[bi] = True

        comps: dict = {}
        for i in indices:
            if has_edge[i]:
                comps.setdefault(find(i), []).append(i)
        for members in comps.values():
            if len(members) >= 2:
                groups.append(sorted(members))

    groups.sort(key=lambda g: g[0])
    return groups


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def _new_task(relevant_file: str, structural_issue: str, members: List[dict], fix_note: str,
              companion_head_file: Optional[str] = None, needs_tier2: bool = False) -> dict:
    return {
        "kind": "merged" if len(members) > 1 else "single",
        "relevant_file": relevant_file,
        "structural_issue": structural_issue,
        "members": members,
        "companion_head_file": companion_head_file,
        "needs_tier2": needs_tier2,
        "fix_note": fix_note,
    }


def _fix_one(sugg: dict, head_map: dict, diff_file_paths: set, fuzzy_threshold: float):
    """Return (fixed_list, task_or_none) for a single suggestion."""
    relevant_file = str(sugg.get("relevant_file", "") or "").strip()
    head_file = _head_for(head_map, relevant_file)
    structural_issue = str(sugg.get("structural_issue", "none") or "none")

    if head_file and not _existing_code_matches_at_claimed_lines(sugg, head_file):
        fixed = repair_existing_mismatch(sugg, head_file, fuzzy_threshold)
        if fixed is not None:
            fixed["resolved_by_stage"] = "deterministic_fix"
            return [fixed], None
        return [], _new_task(
            relevant_file, "existing_mismatch", [sugg],
            "existing_code not found in head_file above the fuzzy-match threshold",
        )

    if structural_issue == "new_dependency":
        split = split_new_dependency(sugg, head_file)
        if split is not None:
            for s in split:
                s["resolved_by_stage"] = "deterministic_fix"
            return split, None
        return [], _new_task(
            relevant_file, "new_dependency", [sugg],
            "new_dependency flagged but no #include/import line found to split",
        )

    if structural_issue == "cross_file":
        routing = prepare_cross_file_context(sugg, diff_file_paths, head_map)
        note = (
            "companion file not in diff; only Tier-2 (full-repo clone) can repair it"
            if routing["needs_tier2"] else
            "companion file is in diff; needs a two-file repair from Tier-1"
        )
        return [], _new_task(
            relevant_file, "cross_file", [sugg], note,
            companion_head_file=routing["companion_head_file"],
            needs_tier2=routing["needs_tier2"],
        )

    if structural_issue == "incomplete_patch":
        return [], _new_task(
            relevant_file, "incomplete_patch", [sugg],
            "incomplete/ellipsis patch or unbalanced braces; no deterministic fix",
        )

    sugg = dict(sugg)
    sugg["resolved_by_stage"] = "reflect_pass"
    return [sugg], None


def _strip_internal_keys(d: dict) -> dict:
    d.pop("_origin_id", None)
    return d


def run_deterministic_fix(head_map: dict, suggestions: list, fuzzy_threshold: Optional[float] = None,
                           conflict_adjacency: Optional[int] = None) -> Tuple[List[dict], List[dict]]:
    """Run the zero-LLM repair layer over self-reflect survivors (score > 0).

    fuzzy_threshold/conflict_adjacency default to the `pr_code_suggestions.
    fix_fuzzy_match_threshold` / `inline_conflict_adjacency_lines` settings
    when not given explicitly (mirrors gate_suggestions/run_phase2's own
    _cfg-driven defaults).

    Returns (resolved, tasks). `resolved` suggestions are ready for stage (6)
    rendering as-is. `tasks` are RepairTask dicts for Tier-1 (and, failing
    that, Tier-2) to act on. Never raises: an unexpected error on one
    suggestion degrades to a "single" RepairTask for it, rather than
    crashing the whole run.
    """
    if fuzzy_threshold is None:
        fuzzy_threshold = float(_cfg("fix_fuzzy_match_threshold", 0.85))
    if conflict_adjacency is None:
        conflict_adjacency = int(_cfg("inline_conflict_adjacency_lines", 3))

    diff_file_paths = set(head_map.keys())
    resolved: List[dict] = []
    tasks: List[dict] = []

    for origin_id, sugg in enumerate(suggestions or []):
        try:
            fixed_list, task = _fix_one(sugg, head_map, diff_file_paths, fuzzy_threshold)
        except Exception as e:
            get_logger().warning(f"deterministic_fix error on a suggestion, routing to tier1: {e}")
            fixed_list, task = [], _new_task(
                str(sugg.get("relevant_file", "")), "error", [sugg], f"deterministic_fix error: {e}")
        for item in fixed_list:
            item["_origin_id"] = origin_id
        resolved.extend(fixed_list)
        if task is not None:
            for member in task["members"]:
                member["_origin_id"] = origin_id
            tasks.append(task)

    # Conflict detection runs over already-resolved suggestions: two items
    # that are EACH individually fine can still collide when applied
    # together (e.g. two separate suggestions each adding the same member).
    groups = detect_conflict_groups(resolved, head_map, conflict_adjacency)
    if groups:
        flat_conflict_idx = {i for g in groups for i in g}
        new_resolved = [s for i, s in enumerate(resolved) if i not in flat_conflict_idx]
        for group in groups:
            members = [resolved[i] for i in group]
            rel = str(members[0].get("relevant_file", ""))
            tasks.append(_new_task(rel, "conflict", members,
                                    f"{len(members)} suggestions conflict when applied together"))
        resolved = new_resolved

    resolved = [_strip_internal_keys(s) for s in resolved]
    for task in tasks:
        task["members"] = [_strip_internal_keys(m) for m in task["members"]]
    return resolved, tasks
