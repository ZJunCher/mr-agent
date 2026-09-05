"""Build and upload a downloadable Markdown report for the mr_create combined
summary, so users can hand it to another AI model for a second opinion.

Everything in this module is best-effort: any failure is caught and logged,
never raised, so a report-export problem can never break the main mr_create
publish flow (see pr_agent/tools/pr_mr_create.py).
"""

import time
import uuid
from typing import Optional, Tuple

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def _build_diff_text(git_provider) -> str:
    """Return a plain-text unified diff dump of every changed file in the PR.

    GitLab's API returns each file's ``patch`` as the raw hunk body only (the
    ``@@ ... @@`` lines), without a ``diff --git``/``---``/``+++`` header, so
    we prepend our own header to make the output a conventional unified diff
    that's easy for a human or another model to read.
    """
    diff_files = git_provider.get_diff_files()
    parts = []
    for f in diff_files:
        old_name = f.old_filename or f.filename
        new_name = f.filename
        header = f"diff --git a/{old_name} b/{new_name}"
        patch = f.patch or ""
        parts.append(f"{header}\n{patch}")
    return "\n".join(parts)


def _safe_call(fn, default=""):
    try:
        return fn()
    except Exception:
        return default


def _build_report_markdown(git_provider, combined_md: str, diff_text: str) -> str:
    """Render the full downloadable report: PR metadata + the mr_create
    combined summary (verbatim) + the PR's complete unified diff.

    Metadata lookups are individually best-effort: a failure fetching any one
    field (title, MR link, branches) falls back to an empty string rather
    than aborting the whole report, since the summary + diff are the parts
    that matter most to a downstream AI reviewer.
    """
    title = _safe_call(lambda: git_provider.get_title())
    mr_link = _safe_call(lambda: git_provider.get_pr_id())
    source_branch = _safe_call(lambda: git_provider.get_pr_branch())
    target_branch = _safe_call(lambda: getattr(git_provider.mr, "target_branch", ""))

    lines = [
        "# PR-Agent 报告导出",
        "",
        f"- MR: {mr_link}",
        f"- 标题: {title}",
        f"- 分支: {source_branch} → {target_branch}",
        "",
        "## 评审 + 建议摘要",
        "",
        combined_md,
        "",
        "## 完整 Diff",
        "",
        "```diff",
        diff_text,
        "```",
        "",
    ]
    return "\n".join(lines)


def _build_report_filename(git_provider) -> str:
    """Return a unique-per-PR-state filename so multiple downloaded reports
    (across different MRs, or re-runs on the same MR after new commits) never
    collide with each other in a browser's Downloads folder.

    Prefers ``report_mr<iid>_<short-head-sha>.md`` when both are available:
    stable across retries for the exact same commit, but changes on every new
    push (so an updated report never silently overwrites/gets confused with
    an older one for the same MR). Falls back to a MR-scoped timestamp, then
    to a random suffix, so a filename is always produced even when some
    provider metadata is missing.
    """
    try:
        mr_iid = git_provider.id_mr
    except Exception:
        mr_iid = None

    try:
        head_sha = git_provider.mr.diff_refs.get("head_sha")
    except Exception:
        head_sha = None

    if mr_iid and head_sha:
        return f"report_mr{mr_iid}_{str(head_sha)[:8]}.md"
    if mr_iid:
        return f"report_mr{mr_iid}_{int(time.time())}.md"
    return f"report_{uuid.uuid4().hex[:8]}.md"


def _upload_report(git_provider, report_md: str, filename: str) -> str:
    """Upload the report via GitLab's Uploads API and return an absolute URL.

    GitLab exposes TWO different routes for reading back an uploaded file:

    1. The web route: ``https://<host>/<namespace>/<project>/uploads/<secret>/<filename>``
       (built by prefixing the response's ``url`` field with the project's
       ``web_url``). This route is served by a separate web controller that,
       on at least one of our self-hosted GitLab instances, was observed to
       404 for private projects even for members with correct access — a
       server-side quirk that has nothing to do with permissions, the file's
       existence, or our code (confirmed by fetching the SAME file through
       the API route below, which worked).
    2. The REST API route: ``https://<host>/api/v4/projects/<id>/uploads/<secret>/<filename>``.
       This is served by the API controller and, per GitLab's own docs,
       accepts either an API token OR a logged-in browser session — and was
       confirmed working where the web route 404'd.

    We deliberately build the API-style link here, not the web-style one,
    to sidestep that web-controller quirk.

    Raises on any failure; callers are expected to catch and log (see
    build_and_upload_report), matching this module's best-effort contract.
    """
    project = git_provider.gl.projects.get(git_provider.id_project)
    result = project.upload(filename, filedata=report_md.encode("utf-8"))
    # result["url"] looks like "/uploads/<secret>/<filename>"; pull the
    # secret/filename back out of it rather than reusing our local `filename`
    # again, so this still works if GitLab ever renames on upload (e.g. to
    # dedupe a collision).
    secret, uploaded_filename = result["url"].strip("/").split("/")[-2:]
    base_url = git_provider.gl.url.rstrip("/")
    return f"{base_url}/api/v4/projects/{project.id}/uploads/{secret}/{uploaded_filename}"


def build_and_upload_report(git_provider, combined_md: str) -> Optional[Tuple[str, str]]:
    """Build the report and upload it, returning (download_url, filename) or None.

    ``combined_md`` should be the review + improve summary text ONLY (not the
    help/usage-guide section, not the LLM-status-feedback section) — callers
    are responsible for excluding those before calling this, since they add
    noise a downstream AI reviewer doesn't need.

    Returns None (never raises) when the feature is disabled via config or
    when any step of building/uploading the report fails.
    """
    if not get_settings().get("pr_mr_create.report_export.enabled", True):
        return None
    try:
        diff_text = _build_diff_text(git_provider)
        report_md = _build_report_markdown(git_provider, combined_md, diff_text)
        filename = _build_report_filename(git_provider)
        download_url = _upload_report(git_provider, report_md, filename)
        return download_url, filename
    except Exception as e:
        get_logger().warning(f"report export failed, skipping download link: {e}")
        return None
