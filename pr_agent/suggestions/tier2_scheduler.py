"""Fire-and-forget Tier-2 scheduling for /improve suggestions (Pipeline v2).

Tier-2 (heavy_repair.py) is slow (full clone + a Copilot CLI session) and
must never block the main /improve comment from being published. This module
provides schedule_tier2, called AFTER the main comment + inline suggestions
have already been published: it fires run_heavy_repair as a background
asyncio task (never awaited by the caller) and, once it completes, publishes
whatever it produced:
  - one_click -> appended as additional inline suggestions through the same
    publisher Task 12 already gates behind pipeline_v2_enabled. When a single
    original suggestion required edits at more than one code location (same
    source_task_id), a short overview comment is posted first explaining
    that the following N suggestions are companion edits for the same issue
    and should be applied together.
  - copy_patch -> not published. Files outside the diff cannot become GitLab
    inline suggestions, and summary rows are reserved for successfully
    published inline discussions.
  - failed -> a text-only fallback comment, never silently dropped

An optional on_complete callback lets the caller refresh anything it
published before Tier-2 ran (e.g. the /improve summary table), since Tier-2
results land well after that initial publish.

Independent `tier2_enabled` switch (default false): when off, schedule_tier2
is a no-op and returns None immediately -- Tier-0/Tier-1/rendering continue
to work completely unaffected.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Awaitable, Callable, List, Optional

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def _cfg(key: str, default=None):
    return get_settings().get(f"pr_code_suggestions.{key}", default)


def _is_zh() -> bool:
    try:
        return str(get_settings().get("config.response_language", "en-US")).lower().startswith("zh")
    except Exception:
        return False


def render_text_only_fallback(failed_items: List[tuple]) -> str:
    """Render a text-only fallback comment for tasks Tier-2 could not resolve
    at all (manifest said failed, or no diff/hunk was produced). Never
    silently dropped -- every task id must appear."""
    is_zh = _is_zh()
    header = ("## ⚠️ 以下建议未能自动修复为可应用状态\n\n" if is_zh else
              "## ⚠️ The following suggestions could not be auto-repaired into an appliable state\n\n")
    lines = [f"- **{task_id}**: {reason}" for task_id, reason in (failed_items or [])]
    return header + "\n".join(lines)


def render_multi_location_overview(count: int, label: str, summary: str) -> str:
    """Render an overview comment for a group of one-click suggestions that
    are all companion edits Tier-2 produced for the SAME original suggestion
    (multiple, usually non-contiguous, code locations). GitLab's inline
    suggestion API can only attach one contiguous diff per comment, so these
    edits cannot be merged into a single one-click button -- this overview
    is posted immediately before them so the reviewer understands they
    belong together and should be applied as a set. Never raises."""
    is_zh = _is_zh()
    label = label or ("未标注" if is_zh else "unlabeled")
    summary = summary or ("(无摘要)" if is_zh else "(no summary)")
    if is_zh:
        return (
            f"🔗 以下 {count} 条建议是同一个问题（{label}：{summary}）的配套修改，"
            f"涉及 {count} 处代码位置。若采纳，请将下面这 {count} 条建议一并批量应用。"
        )
    return (
        f"🔗 The following {count} suggestions are companion edits for the same issue "
        f"({label}: {summary}), touching {count} code locations. If you accept this, "
        f"please apply all {count} suggestions below together."
    )


def _group_by_source_task(one_click: List[dict]) -> "OrderedDict[str, List[dict]]":
    """Group one-click suggestions by their originating Tier-2 task id,
    preserving first-seen order. Items without a source_task_id (shouldn't
    happen from heavy_repair.py, but never trust upstream blindly) are each
    treated as their own singleton group."""
    groups: "OrderedDict[str, List[dict]]" = OrderedDict()
    for idx, item in enumerate(one_click or []):
        key = item.get("source_task_id") or f"_no_task_id_{idx}"
        groups.setdefault(key, []).append(item)
    return groups


async def _run_and_publish(
    git_provider,
    tasks: List[dict],
    store_path: Optional[str] = None,
    on_complete: Optional[Callable[[dict], Awaitable[None]]] = None,
    source: str = "mr_create",
    prompt_provenance=None,
) -> dict:
    """The actual background work: run Tier-2, then publish whatever it
    produced. Never raises (logs and returns a summary dict on failure)."""
    from pr_agent.suggestions import inline_publisher
    from pr_agent.suggestions.heavy_repair import run_heavy_repair

    try:
        result = await run_heavy_repair(git_provider, tasks)
    except Exception as e:
        get_logger().exception(f"Tier-2 run_heavy_repair raised: {e}")
        return {"one_click": 0, "copy_patch": 0, "failed": len(tasks)}

    one_click = result.get("one_click") or []
    copy_patch = result.get("copy_patch") or []
    failed = result.get("failed") or []

    if one_click:
        # A single original suggestion that needed edits at multiple code
        # locations produces multiple one_click items sharing the same
        # source_task_id. GitLab has no way to bundle them into one
        # applyable suggestion, so post a short overview comment right
        # before the group explaining they belong together.
        try:
            for items in _group_by_source_task(one_click).values():
                if len(items) > 1:
                    first = items[0]
                    overview = render_multi_location_overview(
                        len(items), first.get("label", ""), first.get("one_sentence_summary", ""))
                    git_provider.publish_comment(overview)
        except Exception as e:
            get_logger().exception(f"Tier-2 multi-location overview publish failed: {e}")

        try:
            inline_result = await inline_publisher.publish_inline_suggestions_async(
                git_provider, one_click, store_path=store_path, source=source,
                prompt_provenance=prompt_provenance)
            inline_publisher.backfill_note_urls(one_click, (inline_result or {}).get("published_locations") or [])
        except Exception as e:
            get_logger().exception(f"Tier-2 one_click publish failed: {e}")

    # copy_patch is intentionally not published or added to the summary.

    if failed:
        try:
            git_provider.publish_comment(render_text_only_fallback(failed))
        except Exception as e:
            get_logger().exception(f"Tier-2 failed-fallback publish failed: {e}")

    if on_complete is not None:
        try:
            await on_complete(result)
        except Exception as e:
            get_logger().exception(f"Tier-2 on_complete callback failed: {e}")

    get_logger().info(
        f"Tier-2 finished: {len(one_click)} one_click, {len(copy_patch)} copy_patch, {len(failed)} failed")
    return {"one_click": len(one_click), "copy_patch": len(copy_patch), "failed": len(failed)}


def schedule_tier2(
    git_provider,
    pending_tasks: List[dict],
    store_path: Optional[str] = None,
    on_complete: Optional[Callable[[dict], Awaitable[None]]] = None,
    source: str = "mr_create",
    prompt_provenance=None,
):
    """Fire Tier-2 as a detached background asyncio task if there is
    anything to do and the independent tier2_enabled switch is on. Returns
    the created Task (mainly so tests/callers can await it deterministically;
    production callers ignore the return value -- that's what makes this
    fire-and-forget), or None when there is nothing to schedule / the switch
    is off. Never raises, never blocks the caller.

    `source` is forwarded to inline_publisher for the one_click suggestions
    Tier-2 publishes ("mr_create" or "improve_command"; see
    inline_publisher.inline_feature_enabled) so Tier-2's own inline publishing
    respects whichever entry point's on/off switch triggered this run.

    `on_complete`, when given, is awaited with the raw classification dict
    ({"one_click": [...], "copy_patch": [...], "failed": [...]}) after all of
    Tier-2's own publishing is done -- callers use this to refresh content
    they published earlier (e.g. the /improve summary table) so it reflects
    what Tier-2 resolved instead of going stale. A failure inside on_complete
    is logged and never propagates.
    """
    if not pending_tasks:
        return None
    if not bool(_cfg("tier2_enabled", False)):
        get_logger().info(
            "Tier-2 disabled (pr_code_suggestions.tier2_enabled=false); "
            f"{len(pending_tasks)} suggestion(s) will not be auto-repaired further")
        return None
    try:
        return asyncio.create_task(_run_and_publish(git_provider, pending_tasks, store_path,
                                                       on_complete, source, prompt_provenance))
    except Exception as e:
        get_logger().exception(f"failed to schedule Tier-2: {e}")
        return None
