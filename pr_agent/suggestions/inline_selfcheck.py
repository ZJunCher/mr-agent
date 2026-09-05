"""Phase 2 LLM self-check for inline suggestions.

Two layers run after the Phase 1 heuristic gate, on the surviving candidates:

- 2A ``run_selfcheck`` / ``selfcheck_single``: one lightweight LLM call per
  candidate. The model judges whether the improved_code completely and safely
  fixes the stated issue. Any failing field blocks the suggestion. On any error
  the suggestion is blocked (fail-closed) unless ``inline_selfcheck_fail_action``
  is ``pass``.

- 2B ``deconflict``: when >=2 candidates target the same file and collide
  (overlapping/adjacent lines, both add declarations, or share a newly
  introduced identifier), an LLM proposes keep/rewrite/drop so the suggestions
  can be one-click-applied together without stacking into a compile error
  (the MR !432 redeclaration case). A rewritten product is only published after
  it re-passes Phase 1 (``check_suggestion``) and 2A. On any LLM error the
  conflict group falls back to keeping only the highest-scored suggestion.

Everything is async and never raises out of the public orchestrators; a failure
degrades to a safe fallback so the MR flow is never broken.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.language_router import detect_language_from_files, language_scopes_for_mode
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.suggestions.deterministic_fix import detect_conflict_groups
from pr_agent.suggestions.inline_gate import (
    _build_head_map,
    _head_for,
    check_suggestion,
)
from pr_agent.suggestions.project_prompt_rules import (
    ProjectSkillSession,
    project_skill_should_inject,
    project_skill_should_load,
)

_SELFCHECK_FIELDS = ("complete_fix", "self_consistent", "safe_to_apply", "format_plausible")
_CONTEXT_RADIUS = 30


def _cfg(key: str, default=None):
    return get_settings().get(f"pr_code_suggestions.{key}", default)


def _is_zh() -> bool:
    try:
        return str(get_settings().get("config.response_language", "en-US")).lower().startswith("zh")
    except Exception:
        return False


def _append_project_context(prompt: str, context: str) -> str:
    return f"{prompt.rstrip()}\n\n{context}" if context else prompt


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _parse_json(text: str) -> dict:
    """Extract a JSON object from an LLM response (tolerates ```json fences)."""
    if not text:
        raise ValueError("empty response")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(cleaned[start:end + 1])
    raise ValueError("no JSON object found in response")


def _context_window(head_file: str, start, end) -> str:
    if not head_file:
        return ""
    lines = head_file.splitlines()
    s = _to_int(start) or 1
    e = _to_int(end) or s
    lo = max(0, s - 1 - _CONTEXT_RADIUS)
    hi = min(len(lines), e + _CONTEXT_RADIUS)
    return "\n".join(lines[lo:hi])


def _render(template: str, variables: dict) -> str:
    return Environment(undefined=StrictUndefined).from_string(template).render(variables)


def _provider_filenames(git_provider) -> list[str]:
    """Best-effort changed filenames without requiring a specific provider implementation."""
    try:
        files = git_provider.get_files()
        if files:
            return [str(path) for path in files]
    except Exception:
        pass
    try:
        diff_files = git_provider.get_diff_files()
    except Exception:
        diff_files = getattr(git_provider, "diff_files", ()) or ()
    filenames = []
    for item in diff_files or ():
        filename = getattr(item, "filename", None) or getattr(item, "new_path", None)
        if filename:
            filenames.append(str(filename))
    return filenames


# --------------------------------------------------------------------------- #
# 2A: single self-check
# --------------------------------------------------------------------------- #
async def selfcheck_single(ai_handler, suggestion: dict, head_file: str,
                            project_prompt_rules: str = "") -> Optional[str]:
    """Return a ``selfcheck_<field>`` skip reason, or None when the suggestion
    passes. Fail-closed (returns ``selfcheck_error``) on any error unless
    ``inline_selfcheck_fail_action`` is ``pass``."""
    fail_action = str(_cfg("inline_selfcheck_fail_action", "skip") or "skip").lower()
    try:
        variables = {
            "language": "Chinese" if _is_zh() else "English",
            "suggestion_content": str(suggestion.get("suggestion_content", "") or ""),
            "existing_code": str(suggestion.get("existing_code", "") or ""),
            "improved_code": str(suggestion.get("improved_code", "") or ""),
            "context": _context_window(
                head_file,
                suggestion.get("relevant_lines_start"),
                suggestion.get("relevant_lines_end"),
            ),
            "project_prompt_rules": project_prompt_rules,
        }
        system = _render(get_settings().pr_inline_selfcheck_prompt.system, variables)
        user = _render(get_settings().pr_inline_selfcheck_prompt.user, variables)
        user = _append_project_context(user, project_prompt_rules)
        model = _cfg("inline_selfcheck_model", None) or get_settings().config.model
        response, _ = await ai_handler.chat_completion(
            model=model, system=system, user=user, temperature=0.1
        )
        data = _parse_json(response)
    except Exception as e:
        get_logger().warning(f"inline self-check failed: {e}")
        return None if fail_action == "pass" else "selfcheck_error"

    for field in _SELFCHECK_FIELDS:
        if not _as_bool(data.get(field, True)):
            return f"selfcheck_{field}"
    return None


async def run_selfcheck(ai_handler, git_provider, suggestions: list,
                    project_prompt_rules: str = "") -> Tuple[list, list]:
    """Self-check each candidate (2A). Returns (passed, blocked) where blocked
    items are ``(suggestion, skip_reason)``. Never raises."""
    if not _cfg("inline_selfcheck_enabled", True):
        return list(suggestions or []), []
    try:
        head_map = _build_head_map(git_provider)
    except Exception:
        head_map = {}
    try:
        max_candidates = int(_cfg("inline_selfcheck_max_candidates", 5))
    except Exception:
        max_candidates = 5

    passed, blocked = [], []
    checked = 0
    for sugg in suggestions or []:
        if checked >= max_candidates:
            passed.append(sugg)  # over the cap: pass through unchecked
            continue
        checked += 1
        try:
            head_file = _head_for(head_map, sugg.get("relevant_file", "") or "")
            reason = await selfcheck_single(ai_handler, sugg, head_file, project_prompt_rules)
        except Exception as e:
            get_logger().warning(f"inline self-check orchestration error: {e}")
            reason = None
        if reason:
            blocked.append((sugg, reason))
        else:
            passed.append(sugg)
    return passed, blocked


# --------------------------------------------------------------------------- #
# 2B: conflict detection (local prefilter, no LLM)
#
# The detection logic now lives in deterministic_fix.py (Pipeline v2's
# zero-LLM repair layer uses the identical heuristic); it is imported at the
# top of this file and re-exported here so existing callers/tests that do
# `from pr_agent.suggestions.inline_selfcheck import detect_conflict_groups`
# keep working unchanged.
# --------------------------------------------------------------------------- #
def _legacy_detect_conflict_groups(suggestions: list, head_map: dict) -> List[List[int]]:
    try:
        adjacency = int(_cfg("inline_conflict_adjacency_lines", 3))
    except Exception:
        adjacency = 3
    return detect_conflict_groups(suggestions, head_map, adjacency)


# --------------------------------------------------------------------------- #
# 2B: de-conflict orchestration
# --------------------------------------------------------------------------- #
def _render_group_for_prompt(group_ids: List[str], group_suggs: List[dict]) -> str:
    blocks = []
    for sid, sugg in zip(group_ids, group_suggs):
        blocks.append(
            f"[{sid}] lines {sugg.get('relevant_lines_start')}-{sugg.get('relevant_lines_end')}\n"
            f"issue: {sugg.get('suggestion_content', '')}\n"
            f"existing_code:\n{sugg.get('existing_code', '')}\n"
            f"improved_code:\n{sugg.get('improved_code', '')}"
        )
    return "\n\n".join(blocks)


async def _resolve_group(ai_handler, git_provider, head_map, indices, ids, suggestions,
                         outcomes, project_prompt_rules: str = "") -> None:
    group_suggs = [suggestions[i] for i in indices]
    group_ids = [ids[i] for i in indices]
    rel = str(group_suggs[0].get("relevant_file", "") or "").strip()
    head_file = _head_for(head_map, rel)

    try:
        variables = {
            "language": "Chinese" if _is_zh() else "English",
            "file": rel,
            "context": _context_window(
                head_file,
                min(_to_int(s.get("relevant_lines_start")) or 1 for s in group_suggs),
                max(_to_int(s.get("relevant_lines_end")) or 1 for s in group_suggs),
            ),
            "suggestions": _render_group_for_prompt(group_ids, group_suggs),
            "project_prompt_rules": project_prompt_rules,
        }
        system = _render(get_settings().pr_inline_deconflict_prompt.system, variables)
        user = _render(get_settings().pr_inline_deconflict_prompt.user, variables)
        user = _append_project_context(user, project_prompt_rules)
        model = _cfg("inline_selfcheck_model", None) or get_settings().config.model
        response, _ = await ai_handler.chat_completion(
            model=model, system=system, user=user, temperature=0.1
        )
        data = _parse_json(response)
        resolved = {str(r.get("id")): r for r in (data.get("resolved") or [])}
    except Exception as e:
        get_logger().warning(f"inline de-conflict failed, keeping top-scored only: {e}")
        _fallback_keep_top1(indices, suggestions, outcomes)
        return

    for i in indices:
        entry = resolved.get(ids[i])
        action = str(entry.get("action", "keep")).lower() if entry else "keep"
        if action == "drop":
            outcomes[i] = ("blocked", "conflict_dropped")
        elif action == "rewrite":
            await _apply_rewrite(ai_handler, suggestions[i], entry, head_file, outcomes, i, project_prompt_rules)
        else:
            outcomes[i] = ("keep", suggestions[i])


async def _apply_rewrite(ai_handler, sugg, entry, head_file, outcomes, i,
                    project_prompt_rules: str = "") -> None:
    candidate = dict(sugg)
    if entry.get("improved_code") is not None:
        candidate["improved_code"] = entry.get("improved_code")
    if _to_int(entry.get("relevant_lines_start")) is not None:
        candidate["relevant_lines_start"] = _to_int(entry.get("relevant_lines_start"))
    if _to_int(entry.get("relevant_lines_end")) is not None:
        candidate["relevant_lines_end"] = _to_int(entry.get("relevant_lines_end"))

    # Re-check the machine-synthesized product: Phase 1 gate + 2A self-check.
    if check_suggestion(candidate, head_file) is not None:
        outcomes[i] = ("blocked", "conflict_rewrite_failed")
        return
    if await selfcheck_single(ai_handler, candidate, head_file, project_prompt_rules) is not None:
        outcomes[i] = ("blocked", "conflict_rewrite_failed")
        return
    candidate["rewritten"] = True
    outcomes[i] = ("keep", candidate)


def _fallback_keep_top1(indices, suggestions, outcomes) -> None:
    def _score(i):
        return _to_int(suggestions[i].get("score")) or 0

    keep = max(indices, key=_score)
    for i in indices:
        if i == keep:
            outcomes[i] = ("keep", suggestions[i])
        else:
            outcomes[i] = ("blocked", "conflict_selfcheck_error")


async def deconflict(ai_handler, git_provider, suggestions: list,
                   project_prompt_rules: str = "") -> Tuple[list, list]:
    """Resolve cross-suggestion conflicts (2B). Returns (passed, blocked).
    Never raises."""
    suggestions = list(suggestions or [])
    if not _cfg("inline_conflict_check_enabled", True) or len(suggestions) < 2:
        return suggestions, []
    try:
        head_map = _build_head_map(git_provider)
    except Exception:
        head_map = {}

    groups = _legacy_detect_conflict_groups(suggestions, head_map)
    if not groups:
        return suggestions, []

    ids = {i: f"S{i + 1}" for i in range(len(suggestions))}
    outcomes: dict = {i: ("keep", suggestions[i]) for i in range(len(suggestions))}
    for indices in groups:
        try:
            await _resolve_group(ai_handler, git_provider, head_map, indices, ids,
                                 suggestions, outcomes, project_prompt_rules)
        except Exception as e:
            get_logger().warning(f"inline de-conflict group error: {e}")
            _fallback_keep_top1(indices, suggestions, outcomes)

    passed, blocked = [], []
    for i in range(len(suggestions)):
        kind, payload = outcomes[i]
        if kind == "keep":
            passed.append(payload)
        else:
            blocked.append((suggestions[i], payload))
    return passed, blocked


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #
async def run_phase2(git_provider, suggestions: list, ai_handler=None) -> Tuple[list, list]:
    """Run 2A self-check then 2B de-conflict on Phase-1 survivors.

    Returns (passed, blocked). Never raises: a catastrophic orchestration error
    degrades to letting the (already Phase-1-gated) candidates through so the MR
    flow is never broken.
    """
    suggestions = list(suggestions or [])
    if not suggestions:
        return suggestions, []
    selfcheck_on = bool(_cfg("inline_selfcheck_enabled", True))
    conflict_on = bool(_cfg("inline_conflict_check_enabled", True))
    if not selfcheck_on and not conflict_on:
        return suggestions, []
    try:
        if ai_handler is None:
            from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
            ai_handler = LiteLLMAIHandler()

        session = getattr(git_provider, "_project_skill_session", None)
        if not isinstance(session, ProjectSkillSession):
            session = ProjectSkillSession.load(
                git_provider,
                str(getattr(git_provider, "id_project", "") or ""),
                enabled=project_skill_should_load(),
            )
        detected_language = detect_language_from_files(_provider_filenames(git_provider))
        effective = session.effective(
            "inline_selfcheck",
            languages=language_scopes_for_mode(detected_language),
            files=tuple(
                dict.fromkeys(str(suggestion.get("relevant_file") or "") for suggestion in suggestions)
            ),
        )
        project_rules = effective.render_context() if project_skill_should_inject("inline_selfcheck") else ""
        passed, blocked = await run_selfcheck(ai_handler, git_provider, suggestions, project_rules)
        passed, blocked2 = await deconflict(ai_handler, git_provider, passed, project_rules)
        return passed, list(blocked) + list(blocked2)
    except Exception as e:
        get_logger().exception(f"inline phase2 self-check failed: {e}")
        return suggestions, []
