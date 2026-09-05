"""Tier-1 small-model repair retry for /improve suggestions (Pipeline v2).

Runs after deterministic_fix.py (Tier-0) fails to resolve a RepairTask. Makes
one lightweight LLM call per task (reusing the same `inline_selfcheck_model`
config as the existing self-check step), asking for a corrected patch, then
re-validates the model's output through deterministic_fix.validate_repaired_
suggestion before accepting it -- an LLM saying "fixed!" is never trusted on
its own.

Everything here is async and never raises out of its public entry points; a
failure degrades to leaving the task unresolved for Tier-2 to pick up.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

# config_loader must be imported before pr_agent.log -- see the identical
# comment in deterministic_fix.py for why.
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.suggestions.deterministic_fix import (
    _exact_match_in_head_file, _head_for, validate_repaired_suggestion)


def _cfg(key: str, default=None):
    return get_settings().get(f"pr_code_suggestions.{key}", default)


def _is_zh() -> bool:
    try:
        return str(get_settings().get("config.response_language", "en-US")).lower().startswith("zh")
    except Exception:
        return False


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


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# prompt rendering
# --------------------------------------------------------------------------- #
def _render_members(task: dict) -> str:
    blocks = []
    for i, sugg in enumerate(task["members"]):
        blocks.append(
            f"[member {i + 1}] lines {sugg.get('relevant_lines_start')}-{sugg.get('relevant_lines_end')}\n"
            f"issue: {sugg.get('suggestion_content', '')}\n"
            f"existing_code:\n{sugg.get('existing_code', '')}\n"
            f"improved_code (previously proposed, may be flawed):\n{sugg.get('improved_code', '')}"
        )
    return "\n\n".join(blocks)


def _build_prompt_variables(task: dict, head_file: str, project_prompt_rules: str = "") -> dict:
    return {
        "language": "Chinese" if _is_zh() else "English",
        "relevant_file": task.get("relevant_file", ""),
        "structural_issue": task.get("structural_issue", ""),
        "fix_note": task.get("fix_note", ""),
        "members": _render_members(task),
        "head_file": head_file or "",
        "has_companion": bool(task.get("companion_head_file")),
        "companion_file": task["members"][0].get("companion_file", "") if task["members"] else "",
        "companion_head_file": task.get("companion_head_file") or "",
        "project_prompt_rules": project_prompt_rules,
    }


async def repair_task(
    ai_handler,
    task: dict,
    head_file: str,
    model: str,
    project_prompt_rules: str = "",
) -> Optional[dict]:
    """One LLM call attempting to repair a single RepairTask.

    Returns a dict `{"primary": {...}, "companion": {...} | None}` (raw,
    unvalidated model output) when the model reports `fixable: true`, or
    None when it says `fixable: false` or the call/parse fails. Never raises.
    """
    try:
        variables = _build_prompt_variables(task, head_file, project_prompt_rules)
        system = get_settings().pr_tier1_repair_prompt.system
        user = get_settings().pr_tier1_repair_prompt.user
        from jinja2 import Environment, StrictUndefined
        env = Environment(undefined=StrictUndefined)
        system = env.from_string(system).render(variables)
        user = env.from_string(user).render(variables)
        if project_prompt_rules:
            user = f"{user.rstrip()}\n\n{project_prompt_rules}"
        response, _ = await ai_handler.chat_completion(model=model, system=system, user=user, temperature=0.1)
        data = _parse_json(response)
    except Exception as e:
        get_logger().warning(f"tier1_repair LLM call failed: {e}")
        return None

    if not data.get("fixable", False):
        return None
    primary = data.get("primary") or {}
    if not primary.get("existing_code") or not primary.get("improved_code"):
        return None
    return {"primary": primary, "companion": data.get("companion")}


def _apply_primary(task: dict, primary: dict) -> dict:
    """Merge a repaired primary edit into a renderable suggestion dict, based
    on the task's first member (metadata like label/score/summary carries
    over unchanged; only the code/line fields are replaced)."""
    base = dict(task["members"][0])
    base["existing_code"] = primary["existing_code"]
    base["improved_code"] = primary["improved_code"]
    if _to_int(primary.get("relevant_lines_start")) is not None:
        base["relevant_lines_start"] = _to_int(primary["relevant_lines_start"])
    if _to_int(primary.get("relevant_lines_end")) is not None:
        base["relevant_lines_end"] = _to_int(primary["relevant_lines_end"])
    base["structural_issue"] = "none"
    return base


def _companion_suggestion(task: dict, companion: dict) -> Optional[dict]:
    file_path = str(companion.get("file", "") or task["members"][0].get("companion_file", "")).strip()
    if not file_path or not companion.get("existing_code") or not companion.get("improved_code"):
        return None
    return {
        "relevant_file": file_path,
        "existing_code": companion["existing_code"],
        "improved_code": companion["improved_code"],
        "relevant_lines_start": _to_int(companion.get("relevant_lines_start")) or 1,
        "relevant_lines_end": _to_int(companion.get("relevant_lines_end")) or 1,
        "one_sentence_summary": task["members"][0].get("one_sentence_summary", ""),
        "suggestion_content": task["members"][0].get("suggestion_content", ""),
        "label": task["members"][0].get("label", "possible issue"),
        "score": task["members"][0].get("score", 7),
        # Without this, _extract_impact_level (pr_code_suggestions.py) finds no
        # impact/risk/severity field on this hand-built dict and always shows
        # "Unspecified"/"未标注" in the /improve table's Impact column, even
        # though the original suggestion carried a real severity. _apply_primary
        # above avoids this because it copies task["members"][0] wholesale;
        # this dict is built field-by-field and missed it.
        "severity": task["members"][0].get("severity", ""),
        "structural_issue": "none",
    }


async def run_tier1_repair(ai_handler, tasks: list, head_map: dict, model: Optional[str] = None,
                            max_retries: Optional[int] = None,
                            project_prompt_rules: str = "") -> Tuple[List[dict], List[dict]]:
    """Attempt to repair each unresolved RepairTask via a small LLM,
    re-validating every attempt through deterministic_fix.validate_repaired_
    suggestion before accepting it. Returns (resolved, still_unresolved).
    Never raises.
    """
    model = model or _cfg("tier1_repair_model", "") or get_settings().config.model
    if max_retries is None:
        max_retries = int(_cfg("tier1_repair_max_retries", 2))

    resolved: List[dict] = []
    still_unresolved: List[dict] = []

    for task in tasks or []:
        head_file = _head_for(head_map, task.get("relevant_file", ""))
        companion_head_file = task.get("companion_head_file")
        fixed_primary = None
        fixed_companion = None
        attempt_notes = []

        for attempt in range(max(1, max_retries)):
            try:
                candidate = await repair_task(ai_handler, task, head_file, model, project_prompt_rules)
            except Exception as e:
                get_logger().warning(f"tier1 repair_task raised unexpectedly: {e}")
                candidate = None
            if candidate is None:
                attempt_notes.append(f"attempt {attempt + 1}: model reported not fixable or call failed")
                continue

            primary_sugg = _apply_primary(task, candidate["primary"])
            primary_match = _exact_match_in_head_file(primary_sugg.get("existing_code", ""), head_file)
            if primary_match is None:
                attempt_notes.append(f"attempt {attempt + 1}: primary existing_code is not unique in head_file")
                continue
            primary_sugg["relevant_lines_start"], primary_sugg["relevant_lines_end"] = primary_match
            reason = validate_repaired_suggestion(primary_sugg, head_file)
            if reason:
                attempt_notes.append(f"attempt {attempt + 1}: primary failed validation ({reason})")
                continue

            companion_sugg = None
            if candidate.get("companion"):
                companion_sugg = _companion_suggestion(task, candidate["companion"])
                if companion_sugg is not None:
                    companion_match = _exact_match_in_head_file(
                        companion_sugg.get("existing_code", ""), companion_head_file or "")
                    if companion_match is None:
                        attempt_notes.append(
                            f"attempt {attempt + 1}: companion existing_code is not unique in head_file")
                        continue
                    companion_sugg["relevant_lines_start"], companion_sugg["relevant_lines_end"] = companion_match
                    companion_reason = validate_repaired_suggestion(companion_sugg, companion_head_file or "")
                    if companion_reason:
                        attempt_notes.append(f"attempt {attempt + 1}: companion failed validation ({companion_reason})")
                        continue

            fixed_primary, fixed_companion = primary_sugg, companion_sugg
            break

        if fixed_primary is None:
            merged_note = (task.get("fix_note", "") + " | tier1: " + "; ".join(attempt_notes)).strip(" |")
            still_unresolved.append(dict(task, fix_note=merged_note))
            continue

        fixed_primary["resolved_by_stage"] = "tier1_llm"
        resolved.append(fixed_primary)
        if fixed_companion is not None:
            fixed_companion["resolved_by_stage"] = "tier1_llm"
            resolved.append(fixed_companion)

    return resolved, still_unresolved
