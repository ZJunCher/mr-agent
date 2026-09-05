"""Lock/unlock logic for the inline-suggestion attention gate.

Mirrors pr_agent/feedback/gate.py's safety semantics on a fully independent
GitLab commit-status context: locking ("pending") only happens while the
gate is enabled for the given project; unlocking ("success") is always
allowed so a previously-locked MR can self-heal once the feature is turned
off. All functions are no-ops (or return safe defaults) when disabled and
never raise into the caller.
"""

from typing import Optional

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.suggestions.store import all_threads_satisfied


def _project_in_allowlist(project_id, allowlist) -> bool:
    """Match project_id against gate_project_allowlist.

    git_provider.id_project is always the full GitLab path (e.g.
    "eabot/cook"), but the allowlist is configured with bare repo names
    (e.g. "cook") for readability. Match on both the full path and its
    basename (the part after the last "/") so either form works in config.
    A literal "*" opens the gate to every project.
    """
    if project_id is None:
        return False
    pid = str(project_id).strip()
    allow = {str(x).strip() for x in (allowlist or []) if str(x).strip()}
    if not allow:
        return False
    if "*" in allow:
        return True
    if pid in allow:
        return True
    basename = pid.rsplit("/", 1)[-1]
    return basename in allow


def is_enabled(project_id=None) -> bool:
    """True when the master switch is on and (if project_id is given) the
    project is in gate_project_allowlist. Never raises.

    Webhook-triggered recompute (push/note/MR-update) must gate on this full
    check -- not allowlist alone -- mirroring pr_agent/feedback/gate.py's
    restamp_on_push: while the gate is off, no new commit status (not even a
    harmless "success") should appear on a project's MRs. Turning the gate
    back off after it was on leaves previously-locked MRs locked until an
    explicit unlock action (the standalone scripts/inline_gate_sweep.py, same
    role as feedback_gate_sweep.py) rather than silently via webhook traffic.
    """
    try:
        if not bool(get_settings().get("pr_inline_suggestion_gate.gate_enabled", False)):
            return False
        if project_id is None:
            return True
        allowlist = get_settings().get("pr_inline_suggestion_gate.gate_project_allowlist", []) or []
        return _project_in_allowlist(project_id, allowlist)
    except Exception:
        return False


def status_context() -> str:
    try:
        return str(get_settings().get(
            "pr_inline_suggestion_gate.gate_status_context",
            "pr-agent/inline-suggestions（请查看下方建议）",
        ))
    except Exception:
        return "pr-agent/inline-suggestions（请查看下方建议）"


def _is_zh() -> bool:
    try:
        lang = str(get_settings().get("config.response_language", "en-US")).lower()
        return lang.startswith("zh")
    except Exception:
        return False


def _description(state: str) -> str:
    if _is_zh():
        if state == "success":
            return "所有内联建议均已处理完毕，可以合并。"
        return "存在待处理的内联建议，请在下方建议中点击\"应用建议\"或\"解决主题\"处理后再合并。"
    if state == "success":
        return "All inline suggestions have been handled; ready to merge."
    return "There are unhandled inline suggestions below. Click \"Apply\" or \"Resolve thread\" on each before merging."


def _head_sha(git_provider) -> Optional[str]:
    try:
        refs = git_provider.get_diff_refs()
        if isinstance(refs, dict):
            return refs.get("head_sha")
    except Exception as e:
        get_logger().warning(f"inline-gate: failed to resolve head sha: {e}")
    return None


def _set(git_provider, state: str, project_id=None, sha: Optional[str] = None) -> None:
    if state != "success" and not is_enabled(project_id):
        return
    try:
        sha = sha or _head_sha(git_provider)
        if not sha:
            get_logger().warning("inline-gate: no head sha; skipping commit status.")
            return
        ok = git_provider.set_commit_status(sha, state, status_context(), description=_description(state))
        if not ok:
            get_logger().warning(f"inline-gate: set_commit_status returned False ({state}).")
    except Exception as e:
        get_logger().warning(f"inline-gate: failed to set status {state}: {e}")


def apply_pending(git_provider, project_id=None) -> None:
    _set(git_provider, "pending", project_id=project_id)


def apply_success(git_provider, project_id=None) -> None:
    _set(git_provider, "success", project_id=project_id)


def recompute(git_provider, project, mr_iid, path: Optional[str] = None) -> None:
    """Recompute and (re)apply the gate state for a single MR based on the
    real applied/resolved state already persisted in the store. Never raises."""
    try:
        state = "success" if all_threads_satisfied(project, mr_iid, path=path) else "pending"
        _set(git_provider, state, project_id=project)
    except Exception as e:
        get_logger().warning(f"inline-gate: recompute failed for {project}/{mr_iid}: {e}")
