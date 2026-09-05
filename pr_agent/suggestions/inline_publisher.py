"""Filter, render, and publish ``/improve`` suggestions as inline suggestions.

Phase 1 (additive, gray-rollout by project): only high-confidence, locatable,
one-click-appliable suggestions are published as GitLab-native inline
suggestions on the exact diff lines. The feature is fully guarded (defaults
off) and every entry point is wrapped so a failure can never break the summary
comment or the MR flow.
"""

import difflib
import textwrap
from typing import List, Optional, Tuple

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.suggestions import inline_gate_status
from pr_agent.suggestions.inline_gate import gate_suggestions
from pr_agent.suggestions.inline_selfcheck import run_phase2
from pr_agent.suggestions.store import save_suggestion_thread


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _is_zh() -> bool:
    try:
        lang = str(get_settings().get("config.response_language", "en-US")).lower()
        return lang.startswith("zh")
    except Exception:
        return False


def _normalize_score(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        score = int(value)
    except Exception:
        return None
    return max(0, min(10, score))


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _strip_severity_line(text: str) -> str:
    if not text:
        return text
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines):
        first = lines[i].strip()
        if first.lower().startswith("severity:") or first.startswith("严重性"):
            del lines[i]
            if i < len(lines) and not lines[i].strip():
                del lines[i]
    return "\n".join(lines).strip("\n")


def _localize(text: str, is_zh: bool) -> str:
    if not is_zh or not text:
        return text
    rep = [
        ("Severity: Blocker", "严重性：阻断"),
        ("Severity: High", "严重性：高"),
        ("Severity: Medium", "严重性：中"),
        ("Severity: Low", "严重性：低"),
        ("Why it's wrong:", "原因："),
        ("Why it’s wrong:", "原因："),
        ("Trigger:", "触发场景："),
        ("Fix:", "修复建议："),
        ("Test:", "测试："),
    ]
    for a, b in rep:
        text = text.replace(a, b)
    return text


def _impact_label(suggestion: dict, content: str, is_zh: bool) -> str:
    # Impact/severity is independent from score (score indicates likelihood).
    level_raw = ""
    for key in ("impact", "risk", "severity", "impact_level"):
        value = suggestion.get(key)
        if value:
            level_raw = str(value).strip()
            break
    if not level_raw and content:
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            lower = line.lower()
            if lower.startswith("severity:") or line.startswith("严重性：") or line.startswith("严重性:"):
                level_raw = line.split(":", 1)[-1].strip().strip("：").strip()
                break
    normalized = level_raw.lower().replace("：", ":")
    if normalized in {"blocker", "阻断"}:
        return "阻断" if is_zh else "Blocker"
    if normalized in {"high", "高"}:
        return "高" if is_zh else "High"
    if normalized in {"medium", "中"}:
        return "中" if is_zh else "Medium"
    if normalized in {"low", "低"}:
        return "低" if is_zh else "Low"
    return "未标注" if is_zh else "Unspecified"


def make_suggestion_id(index: int) -> str:
    return f"SUG-{int(index):03d}"


def make_publish_marker(run_id: str, suggestion_id: str) -> str:
    return f"pr-agent-suggestion:{run_id}:{suggestion_id}"


def render_fallback_body(suggestion: dict, marker: str, line_link: str, is_zh: bool) -> str:
    heading = "### 代码建议（已降级为普通评论）" if is_zh else "### Code suggestion (fallback comment)"
    location = str(suggestion.get("relevant_file") or "")
    start = int(suggestion.get("relevant_lines_start") or 0)
    end = int(suggestion.get("relevant_lines_end") or start)
    location_text = f"[{location}:{start}-{end}]({line_link})" if line_link else f"{location}:{start}-{end}"
    patch = "\n".join(difflib.unified_diff(
        str(suggestion.get("existing_code") or "").splitlines(),
        str(suggestion.get("improved_code") or "").splitlines(),
        fromfile="before",
        tofile="after",
        lineterm="",
    ))
    content = _localize(str(suggestion.get("suggestion_content") or ""), is_zh)
    summary = str(suggestion.get("one_sentence_summary") or "").strip()
    description = "\n\n".join(item for item in (summary, content) if item)
    return f"{heading}\n\n{location_text}\n\n{description}\n\n```diff\n{patch}\n```\n\n<!-- {marker} -->"


def backfill_note_urls(suggestions: list, published_locations: list) -> list:
    """Mutate `suggestions` in place, setting `inline_note_url` on every
    suggestion dict whose publisher-assigned ID matches a published inline
    location. Legacy callers without IDs fall back to file/range matching.

    `published_locations` is the list already returned by
    publish_inline_suggestions_async's summary (see that function's
    docstring): each entry has relevant_file/relevant_lines_start/
    relevant_lines_end/note_url. A suggestion with no matching location (or
    whose matching location has no note_url) is left completely untouched --
    callers should treat a missing/falsy `inline_note_url` as "no inline
    suggestion was published for this one" and render no link. Returns only
    suggestions that received a note URL, in their original order.
    """
    for sugg in suggestions or []:
        sugg.pop("inline_note_url", None)
    if not published_locations:
        return []
    by_id = {}
    by_key = {}
    for loc in published_locations:
        note_url = loc.get("note_url")
        if not note_url:
            continue
        suggestion_id = loc.get("suggestion_id")
        if suggestion_id:
            by_id[str(suggestion_id)] = note_url
        key = (
            str(loc.get("relevant_file", "")).strip(),
            loc.get("relevant_lines_start"),
            loc.get("relevant_lines_end"),
        )
        by_key.setdefault(key, note_url)
    published = []
    for sugg in suggestions or []:
        suggestion_id = sugg.get("_inline_suggestion_id")
        key = (
            str(sugg.get("relevant_file", "")).strip(),
            sugg.get("relevant_lines_start"),
            sugg.get("relevant_lines_end"),
        )
        note_url = by_id.get(str(suggestion_id)) if suggestion_id else by_key.get(key)
        if note_url:
            sugg["inline_note_url"] = note_url
            published.append(sugg)
    return published


def project_allowed(project_id, allowlist) -> bool:
    """Return True when the project is allowed to publish inline suggestions.

    Inline suggestions are an allowlist-gated rollout, so an empty (or missing)
    allowlist denies all projects rather than opening the feature globally. This
    is defense in depth against a config being accidentally cleared. A literal
    ``"*"`` entry is an explicit global opt-in that opens the feature to every
    project (used when rolling out beyond the initial gray-release repos).
    Entries are compared as strings so both project paths ("group/cook") and
    numeric ids ("123") work.
    """
    allow = {str(x).strip() for x in (allowlist or []) if str(x).strip()}
    if not allow:
        return False
    if "*" in allow:
        return True
    return str(project_id).strip() in allow


def inline_feature_enabled(source: str = "mr_create") -> bool:
    """Whether inline suggestions should be published for this entry point.

    `source` identifies which caller is asking: "mr_create" (the MR
    create/reopen composite command) or "improve_command" (a user manually
    running /improve). Each has its own independent on/off switch so either
    entry point can be rolled out or reverted without affecting the other;
    both still share the master switch and project allowlist.
    """
    try:
        enabled = bool(get_settings().get("pr_code_suggestions.inline_suggestions_enabled", False))
        if not enabled:
            return False
        setting_key = (
            "pr_code_suggestions.inline_suggestions_on_improve_command"
            if source == "improve_command"
            else "pr_code_suggestions.inline_suggestions_on_mr_create"
        )
        return bool(get_settings().get(setting_key, False))
    except Exception:
        return False


def self_reflect_allowed(git_provider) -> bool:
    """Whether /improve self-reflection should run for this project.

    Self-reflection is what assigns ``relevant_lines_start/end`` (needed to place
    inline suggestions on exact diff lines). To keep the gray rollout's extra
    cost and behavior change scoped to the allowlisted project(s), only run it
    when the inline master switch is on and the project is allowlisted. Turning
    off ``inline_suggestions_enabled`` fully reverts to prior global behavior.
    """
    try:
        if not bool(get_settings().get("pr_code_suggestions.inline_suggestions_enabled", False)):
            return False
        project_id = getattr(git_provider, "id_project", None)
        allowlist = get_settings().get("pr_code_suggestions.inline_suggestions_project_allowlist", []) or []
        return project_allowed(project_id, allowlist)
    except Exception:
        return False


def _provider_supported(git_provider) -> bool:
    return callable(getattr(git_provider, "publish_inline_suggestions", None))


# --------------------------------------------------------------------------- #
# selection + rendering
# --------------------------------------------------------------------------- #
def select_inline_candidates(
    code_suggestions: List[dict], min_score: int, max_lines: int
) -> Tuple[List[dict], List[Tuple[dict, str]]]:
    """Split suggestions into (publishable, [(skipped, reason), ...])."""
    selected: List[dict] = []
    skipped: List[Tuple[dict, str]] = []
    for sugg in code_suggestions or []:
        reason = _skip_reason(sugg, min_score, max_lines)
        if reason:
            skipped.append((sugg, reason))
        else:
            selected.append(sugg)
    return selected, skipped


def _skip_reason(sugg: dict, min_score: int, max_lines: int) -> Optional[str]:
    if not str(sugg.get("relevant_file", "") or "").strip():
        return "no_file"
    score = _normalize_score(sugg.get("score"))
    if score is None or score < int(min_score):
        return "low_score"
    if not str(sugg.get("improved_code", "") or "").strip():
        return "no_improved_code"
    if not str(sugg.get("existing_code", "") or "").strip():
        return "no_existing_code"
    start = _to_int(sugg.get("relevant_lines_start"))
    end = _to_int(sugg.get("relevant_lines_end"))
    if start is None or end is None or start <= 0 or end < start:
        return "invalid_lines"
    if (end - start + 1) > int(max_lines):
        return "too_large"
    return None


def render_inline_body(suggestion: dict, suggestion_id: str, is_zh: bool) -> str:
    """Render a compact comment body: header + one-line issue + suggestion block
    + a collapsed <details> with the full rationale, plus a tracking anchor."""
    label = str(suggestion.get("label", "") or "").strip()
    content_raw = str(suggestion.get("suggestion_content", "") or "")
    impact = _impact_label(suggestion, content_raw, is_zh)
    score = _normalize_score(suggestion.get("score"))
    summary = str(suggestion.get("one_sentence_summary", "") or "").strip()
    improved = str(suggestion.get("improved_code", "") or "").rstrip("\n")
    content = _localize(_strip_severity_line(content_raw).strip(), is_zh)

    if is_zh:
        score_txt = "" if score is None else f"（{score}）"
        header = f"**PR-Agent｜{label}｜{impact}{score_txt}**"
        issue_line = f"问题：{summary}" if summary else ""
        details_summary = "展开原因与验证"
        collapse_summary = "查看修改建议（点击展开）"
    else:
        score_txt = "" if score is None else f" ({score})"
        header = f"**PR-Agent | {label} | {impact}{score_txt}**"
        issue_line = f"Issue: {summary}" if summary else ""
        details_summary = "Details"
        collapse_summary = "View suggested change (click to expand)"

    collapsed = bool(get_settings().get("pr_code_suggestions.inline_suggestion_collapsed", False))
    parts = [header, ""]
    if issue_line:
        parts += [issue_line, ""]
    suggestion_block = f"```suggestion\n{improved}\n```"
    if collapsed:
        # GitLab 18.7 keeps the Apply button working for suggestion blocks
        # nested inside <details> (verified on 18.7.1-jh).
        parts += [f"<details>\n<summary>{collapse_summary}</summary>\n\n{suggestion_block}\n\n</details>"]
    else:
        parts += [suggestion_block]
    if content:
        parts += ["", f"<details>\n<summary>{details_summary}</summary>\n\n{content}\n</details>"]
    if is_zh:
        gate_guidance = (
            "⚠️ 请处理该建议后再合并：点击\"应用建议\"采纳，或点击\"解决主题\"拒绝，"
            "若拒绝请在下方回复栏给出拒绝的原因。"
        )
    else:
        gate_guidance = (
            "⚠️ Please act on this suggestion before merging: click \"Apply\" to accept, "
            "or \"Resolve thread\" to decline (please leave a brief reason on the left if declining)."
        )
    parts += ["", gate_guidance]
    parts += ["", f"<!-- pr-agent-suggestion:{suggestion_id} -->"]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# context helpers
# --------------------------------------------------------------------------- #
def _commit_sha(git_provider) -> str:
    try:
        refs = git_provider.get_diff_refs()
        if isinstance(refs, dict):
            return refs.get("head_sha") or ""
    except Exception:
        pass
    return ""


def _run_id(git_provider, commit_sha: str) -> str:
    project = str(getattr(git_provider, "id_project", "") or "")
    mr = str(getattr(git_provider, "id_mr", "") or "")
    return f"{project}:{mr}:{(commit_sha or '')[:8]}"


def _dedent_snippet(git_provider, relevant_file: str, line_start: int, snippet: str) -> str:
    """Align the snippet indentation to the original diff line (best effort)."""
    try:
        diff_files = getattr(git_provider, "diff_files", None)
        if diff_files is None:
            diff_files = git_provider.get_diff_files()
        original_initial_line = None
        for f in diff_files or []:
            if str(getattr(f, "filename", "")).strip() == relevant_file:
                head_file = getattr(f, "head_file", None)
                if not head_file:
                    return snippet
                file_lines = head_file.splitlines()
                if line_start > len(file_lines) or line_start <= 0:
                    return snippet
                original_initial_line = file_lines[line_start - 1]
                break
        if original_initial_line and snippet.splitlines():
            suggested_initial_line = snippet.splitlines()[0]
            original_spaces = len(original_initial_line) - len(original_initial_line.lstrip())
            suggested_spaces = len(suggested_initial_line) - len(suggested_initial_line.lstrip())
            delta = original_spaces - suggested_spaces
            if delta > 0:
                indent_char = "\t" if original_initial_line.startswith("\t") else " "
                snippet = textwrap.indent(snippet, delta * indent_char).rstrip("\n")
    except Exception as e:
        get_logger().warning(f"inline dedent failed for {relevant_file}: {e}")
    return snippet


def _mr_url(git_provider) -> str:
    """Return the real GitLab MR URL (e.g. https://.../-/merge_requests/334).

    Uses the URL the tool was invoked with (git_provider.pr_url), which is
    already the real, human-clickable MR link. Never raises.
    """
    try:
        return str(git_provider.get_pr_url() or "")
    except Exception:
        return ""


def _mr_author(git_provider) -> str:
    """Return the MR author's username, e.g. for display in dashboards.

    Reads ``git_provider.mr.author`` (python-gitlab MR object), the same
    field used by ``pr_feedback.py`` to populate ``mr_author``. Never raises.
    """
    try:
        mr = getattr(git_provider, "mr", None)
        author = getattr(mr, "author", None) if mr is not None else None
        if isinstance(author, dict):
            return str(author.get("username") or author.get("name") or "")
    except Exception:
        pass
    return ""


def _base_record(sugg: dict, run_id, project, mr_iid, commit_sha, is_zh, mr_url: str = "",
                 mr_author: str = "", prompt_provenance=None) -> dict:
    content = str(sugg.get("suggestion_content", "") or "")
    record = {
        "review_id": run_id,
        "project": project,
        "mr_iid": mr_iid,
        "mr_url": mr_url,
        "mr_author": mr_author,
        "commit_sha": commit_sha,
        "file_path": str(sugg.get("relevant_file", "") or "").strip(),
        "line_start": _to_int(sugg.get("relevant_lines_start")),
        "line_end": _to_int(sugg.get("relevant_lines_end")),
        "label": str(sugg.get("label", "") or "").strip(),
        "severity": _impact_label(sugg, content, is_zh),
        "score": _normalize_score(sugg.get("score")),
        "one_sentence_summary": str(sugg.get("one_sentence_summary", "") or "").strip(),
        "suggestion_content": content,
        "existing_code": sugg.get("existing_code"),
        "improved_code": sugg.get("improved_code"),
        "resolved_by_stage": str(sugg.get("resolved_by_stage", "") or "").strip(),
        "tier2_duration_ms": sugg.get("tier2_duration_ms"),
    }
    if prompt_provenance is not None:
        record.update(prompt_provenance.as_record())
    return record


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def publish_inline_suggestions(git_provider, code_suggestions: List[dict],
                               store_path: Optional[str] = None,
                               source: str = "mr_create",
                               prompt_provenance=None) -> dict:
    """Synchronous wrapper around :func:`publish_inline_suggestions_async`.

    Kept for callers outside an event loop. Never raises.
    """
    import asyncio
    try:
        return asyncio.run(
            publish_inline_suggestions_async(git_provider, code_suggestions, store_path,
                                             source=source, prompt_provenance=prompt_provenance)
        )
    except Exception as e:
        get_logger().exception(f"inline suggestion publishing failed: {e}")
        return {"published": 0, "fallback_published": 0, "skipped": 0, "failed": 0}


def _note_url(mr_url: str, note_id) -> str:
    """Build a deep link that scrolls straight to a published GitLab inline
    suggestion (discussion note), e.g. ``{mr_url}#note_12345``.

    Mirrors GitlabProvider.get_comment_url's anchor format, but works from a
    bare note_id since publish_inline_suggestions returns ids, not note
    objects.
    """
    if not mr_url or not note_id:
        return ""
    return f"{mr_url}#note_{note_id}"


async def publish_inline_suggestions_async(git_provider, code_suggestions: List[dict],
                                           store_path: Optional[str] = None,
                                           source: str = "mr_create",
                                           prompt_provenance=None) -> dict:
    """Publish selected suggestions as inline suggestions and persist records.

    `source` is "mr_create" (default) or "improve_command"; see
    `inline_feature_enabled` -- each entry point has its own independent
    on/off switch.

    Never raises. Returns a summary dict with published/skipped/failed counts,
    plus "published_locations": a list of {relevant_file, relevant_lines_start,
    relevant_lines_end, note_url} for every suggestion that was actually
    posted as an inline note. Callers (e.g. pr_mr_create) use this to point
    the /improve summary table's location links at the specific inline
    suggestion thread instead of a generic file/line diff view.
    """
    summary = {
        "published": 0,
        "fallback_published": 0,
        "skipped": 0,
        "failed": 0,
        "published_locations": [],
    }
    try:
        if not code_suggestions:
            return summary
        if not inline_feature_enabled(source=source):
            return summary
        if not _provider_supported(git_provider):
            get_logger().info("inline suggestions: provider does not support inline publishing, skipping")
            return summary

        project_id = getattr(git_provider, "id_project", None)
        allowlist = get_settings().get("pr_code_suggestions.inline_suggestions_project_allowlist", []) or []
        if not project_allowed(project_id, allowlist):
            get_logger().info(f"inline suggestions: project '{project_id}' not in allowlist, skipping")
            return summary

        min_score = int(get_settings().get("pr_code_suggestions.inline_suggestion_min_score", 0))
        max_lines = int(get_settings().get("pr_code_suggestions.inline_suggestion_max_lines", 20))
        is_zh = _is_zh()

        selected, skipped = select_inline_candidates(code_suggestions, min_score, max_lines)

        # Pipeline v2 already ran deterministic_fix + Tier-1 small-model repair
        # (see PRCodeSuggestions.run_repair_pipeline) before suggestions reach
        # here: every candidate has already been positionally validated and,
        # where needed, actively repaired -- not merely screened. Re-running
        # the legacy heuristic gate / LLM self-check on top would be redundant
        # (candidates already pass the equivalent, stricter checks) and would
        # burn extra LLM calls for no benefit. Legacy behavior is unchanged
        # when pipeline_v2_enabled is false (the default).
        if not bool(get_settings().get("pr_code_suggestions.pipeline_v2_enabled", False)):
            # Phase 1 heuristic gate: block suggestions unlikely to be safely
            # one-click appliable (new symbols/dependencies, cross-file edits,
            # incomplete patches, speculative categories). Never raises.
            try:
                selected, gate_blocked = gate_suggestions(git_provider, selected)
                skipped = list(skipped) + list(gate_blocked)
            except Exception as e:
                get_logger().warning(f"inline gate failed, publishing without gate: {e}")

            # Phase 2 LLM self-check (2A) + batch de-conflict (2B): drop candidates
            # that are not complete/safe self-contained fixes, and resolve
            # cross-suggestion conflicts so multiple can be safely applied together.
            # Never raises; degrades to publishing the Phase-1 survivors.
            try:
                selected, phase2_blocked = await run_phase2(git_provider, selected)
                skipped = list(skipped) + list(phase2_blocked)
            except Exception as e:
                get_logger().warning(f"inline phase2 self-check failed, publishing survivors: {e}")

        from pr_agent.suggestions.review_tracking import (
            get_current_run_id,
            get_review_run,
            record_review_event,
            update_review_run,
        )

        tracking_run_id = get_current_run_id()
        update_review_run(
            stage="publishing", inline_selected_count=len(selected), inline_skipped_count=len(skipped),
        )
        record_review_event(
            tracking_run_id, "publishing_started", "publishing",
            details={"selected_count": len(selected), "skipped_count": len(skipped)},
        )

        commit_sha = _commit_sha(git_provider)
        run_id = _run_id(git_provider, commit_sha)
        tracking_run = get_review_run(tracking_run_id) if tracking_run_id else {}
        evidence_review_id = str(tracking_run.get("review_id") or run_id)
        project = str(project_id) if project_id is not None else ""
        mr_iid = getattr(git_provider, "id_mr", None)
        mr_url = _mr_url(git_provider)
        mr_author = _mr_author(git_provider)

        payloads = []
        meta_by_id = {}
        for idx, sugg in enumerate(selected, start=1):
            sid = make_suggestion_id(idx)
            sugg["_inline_suggestion_id"] = sid
            file_path = str(sugg.get("relevant_file", "") or "").strip()
            line_start = int(sugg.get("relevant_lines_start"))
            line_end = int(sugg.get("relevant_lines_end"))
            improved = _dedent_snippet(git_provider, file_path, line_start,
                                       str(sugg.get("improved_code", "") or "").rstrip("\n"))
            render_sugg = dict(sugg)
            render_sugg["improved_code"] = improved
            marker = make_publish_marker(run_id, sid)
            body = f"{render_inline_body(render_sugg, sid, is_zh)}\n\n<!-- {marker} -->"
            try:
                line_link = str(git_provider.get_line_link(file_path, line_start, line_end) or "")
            except Exception:
                line_link = ""
            payloads.append({
                "suggestion_id": sid,
                "body": body,
                "relevant_file": file_path,
                "relevant_lines_start": line_start,
                "relevant_lines_end": line_end,
                "original_suggestion": sugg,
                "fallback_body": render_fallback_body(render_sugg, marker, line_link, is_zh),
                "idempotency_marker": marker,
            })
            meta_by_id[sid] = sugg

        results = []
        if payloads:
            try:
                results = git_provider.publish_inline_suggestions(payloads) or []
            except Exception as e:
                get_logger().exception(f"provider publish_inline_suggestions failed: {e}")
                results = []
        res_by_id = {r.get("suggestion_id"): r for r in results}

        for p in payloads:
            sid = p["suggestion_id"]
            sugg = meta_by_id[sid]
            r = res_by_id.get(sid, {})
            status = r.get("publish_status", "failed")
            record = _base_record(
                sugg, evidence_review_id, project, mr_iid, commit_sha, is_zh, mr_url, mr_author,
                prompt_provenance,
            )
            record.update({
                "suggestion_id": sid,
                "gitlab_discussion_id": r.get("discussion_id"),
                "gitlab_note_id": r.get("note_id"),
                "publish_status": status,
                "skip_reason": r.get("skip_reason", ""),
                "state": status,
            })
            extra = {
                "provider_error": str(r.get("provider_error") or ""),
                "positions": r.get("positions") or [],
                "attempt_count": int(r.get("attempt_count") or 0),
                "delivery": status,
            }
            if sugg.get("rewritten"):
                extra["rewritten"] = True
            record["extra"] = extra
            save_suggestion_thread(record, path=store_path)
            if status in {"published", "fallback_published"}:
                summary[status] += 1
                note_url = _note_url(mr_url, r.get("note_id"))
                if note_url:
                    summary["published_locations"].append({
                        "suggestion_id": sid,
                        "relevant_file": p["relevant_file"],
                        "relevant_lines_start": p["relevant_lines_start"],
                        "relevant_lines_end": p["relevant_lines_end"],
                        "note_url": note_url,
                    })
            else:
                summary["failed"] += 1

        for order, (sugg, reason) in enumerate(skipped, start=len(payloads) + 1):
            record = _base_record(
                sugg, evidence_review_id, project, mr_iid, commit_sha, is_zh, mr_url, mr_author,
                prompt_provenance,
            )
            record.update({
                "suggestion_id": make_suggestion_id(order),
                "publish_status": "skipped",
                "skip_reason": reason,
                "state": "skipped",
            })
            save_suggestion_thread(record, path=store_path)
            summary["skipped"] += 1

        get_logger().info(f"inline suggestions summary: {summary}")

        if summary["published"] > 0 and inline_gate_status.is_enabled(project_id):
            inline_gate_status.apply_pending(git_provider, project_id=project_id)

        publish_stage = "publish_failed" if summary["failed"] else "published"
        update_review_run(
            stage=publish_stage,
            inline_selected_count=len(selected),
            inline_skipped_count=summary["skipped"],
            inline_published_count=summary["published"],
            inline_fallback_count=summary["fallback_published"],
            inline_failed_count=summary["failed"],
        )
        current = get_review_run(tracking_run_id) if tracking_run_id else {}
        generated_count = int(current.get("generated_count") or 0)
        filtered_count = int(current.get("filtered_count") or 0)
        unpublished_reason = None
        if (
            generated_count > 0
            and summary["published"] == 0
            and summary["fallback_published"] == 0
            and summary["failed"] == 0
        ):
            if filtered_count == generated_count:
                unpublished_reason = "secondary_review_filtered"
            elif summary["skipped"] > 0:
                unpublished_reason = "publishing_skipped"
            elif len(selected) == 0:
                unpublished_reason = "not_selected_for_inline"
            else:
                unpublished_reason = "unknown_unpublished"
            update_review_run(tracking_run_id, unpublished_reason=unpublished_reason)
        record_review_event(
            tracking_run_id, "publishing_completed", publish_stage,
            status="failed" if summary["failed"] else "completed",
            details={
                "selected_count": len(selected), "skipped_count": summary["skipped"],
                "published_count": summary["published"],
                "fallback_published_count": summary["fallback_published"],
                "failed_count": summary["failed"],
                "unpublished_reason": unpublished_reason,
            },
        )

        return summary
    except Exception as e:
        try:
            from pr_agent.suggestions.review_tracking import update_review_run

            update_review_run(stage="publish_failed", status="failed", error_code=type(e).__name__, error_message=str(e))
            from pr_agent.suggestions.review_tracking import get_current_run_id, record_review_event

            record_review_event(
                get_current_run_id(), "publish_failed", "publish_failed", status="failed",
                error_code=type(e).__name__, error_message=str(e),
            )
        except Exception:
            pass
        get_logger().exception(f"inline suggestion publishing failed: {e}")
        return summary
