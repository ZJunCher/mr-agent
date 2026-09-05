"""Collapsed markdown report builder for the doc-drift detector.

Pure logic (no IO) so it is fully unit-testable.

Structure:
  <details>                          ← outer fold (outer summary line only)
    <summary>📄 文档漂移检测…</summary>
    <details>                        ← per-doc fold, collapsed by default
      <summary>🔴 high · path (clickable)</summary>
      - 冲突：<excerpt> — <diff_reason>（代码位置：<link>）
      - 建议：…
    </details>
    …
  </details>
"""
from __future__ import annotations

import re

_SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}
_SEVERITY_BADGE = {"high": "🔴 high", "medium": "🟡 medium", "low": "⚪ low"}


def _severity_rank(severity: str) -> int:
    return _SEVERITY_ORDER.get(str(severity or "").strip().lower(), 0)


def _norm_severity(severity: str) -> str:
    s = str(severity or "").strip().lower()
    return s if s in _SEVERITY_ORDER else "low"


def _parse_location(loc: str) -> tuple[str, int | None, int | None]:
    """Parse 'path:157' / 'path:120-134' / 'path' into (path, start, end)."""
    loc = str(loc or "").strip()
    if not loc:
        return "", None, None
    m = re.match(r"^(.*?):(\d+)(?:\s*[-~]\s*(\d+))?$", loc)
    if not m:
        return loc, None, None
    path = m.group(1).strip()
    start = int(m.group(2))
    end = int(m.group(3)) if m.group(3) else None
    return path, start, end


def _sanitize_line(text: str) -> str:
    """Return a safe single-line string: strip code fences, table pipes, heading markers.

    Does NOT truncate — callers rely on the model to produce concise text.
    """
    text = str(text or "").strip()
    # Remove fenced code blocks entirely
    text = re.sub(r"```[\s\S]*?```", "[code]", text)
    # Take first non-empty, non-table-separator line
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip markdown table separator rows (e.g. |---|---|)
        if re.match(r"^[|\s:\-]+$", line):
            continue
        # Strip leading markdown heading markers
        line = re.sub(r"^#{1,6}\s+", "", line)
        # Strip leading "N." section numbering (e.g. "4. 输出超时")
        line = re.sub(r"^\d+\.\s+", "", line)
        # Handle table rows: strip all pipes, collapse spaces, drop leading row index
        if "|" in line:
            line = re.sub(r"\|", " ", line)
            line = re.sub(r"\s+", " ", line).strip()
            # Drop a bare leading number (table row index like "3 kOutputTimeout …")
            line = re.sub(r"^\d+\s+", "", line)
        return line
    # Fallback: collapse whitespace
    return re.sub(r"\s+", " ", text)


# Keep old name as alias so existing tests continue to pass
def _sanitize_excerpt(text: str, max_len: int = 120) -> str:
    """Deprecated shim — calls _sanitize_line (no truncation)."""
    return _sanitize_line(text)


def _link(text: str, url: str | None) -> str:
    """Return a markdown link when a url is available, else bold text."""
    if url:
        return f"[**{text}**]({url})"
    return f"**{text}**"


def make_link_builder(git_provider):
    """Build a ``(path, start, end) -> url`` callable from a git provider.

    Uses ``git_provider.get_line_link``. Returns None when the provider cannot
    build links, so callers degrade to plain bold text. Never raises.
    """
    get_line_link = getattr(git_provider, "get_line_link", None)
    if not callable(get_line_link):
        return None

    def _builder(path, start=None, end=None):
        try:
            return get_line_link(path, -1 if start is None else start, end)
        except Exception:
            return None

    return _builder


def filter_and_sort_results(results: list[dict], severity_threshold: str) -> list[dict]:
    """Keep stale docs at or above the threshold, sorted by severity desc."""
    threshold = _severity_rank(severity_threshold) or _SEVERITY_ORDER["medium"]
    kept = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        if not r.get("is_stale"):
            continue
        if _severity_rank(r.get("severity")) < threshold:
            continue
        kept.append(r)
    kept.sort(key=lambda r: _severity_rank(r.get("severity")), reverse=True)
    return kept


def _render_doc_block(result: dict, is_zh: bool, link_builder=None, as_list_item: bool = False) -> str:
    """Render one stale doc as a collapsible <details> block.

    The summary line is a clickable link to the file. Inside: one bullet per
    conflict (sanitised single-line excerpt + diff_reason + code_location link),
    then a suggestion bullet.
    """
    severity = _norm_severity(result.get("severity"))
    badge = _SEVERITY_BADGE[severity]
    doc_path = str(result.get("doc_path", "") or "").strip()

    def _safe_link(path, start=None, end=None):
        if not link_builder or not path:
            return None
        try:
            return link_builder(path, start, end)
        except Exception:
            return None

    doc_url = _safe_link(doc_path, -1, None)
    summary = _link(f"{badge} · {doc_path}", doc_url)

    inner_lines = []
    conflict_label = "冲突" if is_zh else "Conflict"
    loc_label = "代码位置" if is_zh else "Code location"
    for c in result.get("conflicts") or []:
        if not isinstance(c, dict):
            continue
        excerpt = _sanitize_excerpt(c.get("doc_excerpt", ""))
        reason = str(c.get("diff_reason", "") or "").strip()
        reason = _sanitize_line(reason) if reason else ""
        location = str(c.get("code_location", "") or "").strip().splitlines()[0].strip() \
            if c.get("code_location") else ""
        if not (excerpt or reason):
            continue
        if is_zh:
            text = f"- **{conflict_label}**：{excerpt} — {reason}"
        else:
            text = f"- **{conflict_label}**: {excerpt} — {reason}"
        if location:
            path, start, end = _parse_location(location)
            loc_url = _safe_link(path, start, end)
            loc_md = f"[{location}]({loc_url})" if loc_url else location
            text += f"（{loc_label}：{loc_md}）" if is_zh else f" ({loc_label}: {loc_md})"
        inner_lines.append(text)

    suggestion = _sanitize_line(result.get("suggestion", ""))
    if suggestion:
        suggestion_label = "建议" if is_zh else "Suggestion"
        inner_lines.append(f"- **{suggestion_label}**：{suggestion}" if is_zh
                           else f"- **{suggestion_label}**: {suggestion}")

    body = "\n".join(inner_lines)
    block = f"<details>\n<summary>{summary}</summary>\n\n{body}\n</details>"
    return f"<li>{block}</li>" if as_list_item else block


def build_drift_report(
    results: list[dict],
    severity_threshold: str,
    is_zh: bool = True,
    collapsed: bool = True,
    link_builder=None,
) -> str | None:
    """Build the MR comment. Returns None when nothing to report."""
    kept = filter_and_sort_results(results, severity_threshold)
    if not kept:
        return None

    if is_zh:
        title = f"📄 文档漂移检测：发现 {len(kept)} 处可能过期 · 点击展开"
    else:
        title = f"📄 Doc drift: {len(kept)} doc(s) may be stale · click to expand"

    inner = "<ul type=\"none\">\n" + \
        "\n".join(_render_doc_block(r, is_zh, link_builder, as_list_item=True) for r in kept) + \
        "\n</ul>"

    if not collapsed:
        return f"### {title}\n\n{inner}"

    return f"<details>\n<summary>{title}</summary>\n\n{inner}\n\n</details>"
