"""Central logic for the mandatory feedback gate.

The gate blocks GitLab MR merge until a valid /feedback rating exists, using
GitLab commit statuses. All functions are no-ops when the gate is disabled and
never raise into the caller (webhook/command flow must not break).
"""

from typing import Optional

from pr_agent.config_loader import get_settings
from pr_agent.feedback.store import has_feedback
from pr_agent.log import get_logger


def is_enabled() -> bool:
    try:
        return bool(get_settings().get("pr_feedback.gate_enabled", False))
    except Exception:
        return False


def status_context() -> str:
    try:
        return str(get_settings().get("pr_feedback.gate_status_context", "pr-agent/feedback"))
    except Exception:
        return "pr-agent/feedback"


def comment_required_below() -> int:
    try:
        return int(get_settings().get("pr_feedback.comment_required_below", 5))
    except Exception:
        return 5


def is_zh() -> bool:
    lang = str(get_settings().get("config.response_language", "en-US")).lower()
    return lang.startswith("zh")


def guidance_md() -> str:
    threshold = comment_required_below()
    if is_zh():
        return (
            "\n\n___\n\n"
            "📝 **请先认真审阅 AI Review 结果,评分反馈后再合并**  \n"
            "回复 `/feedback <1-5> [评论]` 即可,例如 `/feedback 5` 表示很有帮助,"
            f"或 `/feedback 3 误报较多`(评分低于 {threshold} 分时请附一句简短说明)。"
            "您的每一次反馈,都会被记录并用于持续优化公司的自动化 Review 流程。一起把它打磨得更好,感谢参与 🙏"
        )
    return (
        "\n\n___\n\n"
        "📝 **Please review the AI results carefully, then rate before merging**  \n"
        "Reply `/feedback <1-5> [comment]`, e.g. `/feedback 5` if it was helpful, "
        f"or `/feedback 3 too many false positives` (a brief comment is required when the score is below {threshold}). "
        "Every piece of feedback is recorded and used to improve the company's automated review process. Thanks for taking part 🙏"
    )


def head_sha(git_provider) -> Optional[str]:
    try:
        refs = git_provider.get_diff_refs()
        if isinstance(refs, dict):
            return refs.get("head_sha")
    except Exception as e:
        get_logger().warning(f"feedback-gate: failed to resolve head sha: {e}")
    return None


def _set(git_provider, state: str, sha: Optional[str] = None) -> None:
    # Unlocking ("success") is always allowed, even when the gate is disabled, so
    # a later /feedback can release MRs that were stamped "pending" while the gate
    # was on. This can only ever unblock a merge, never block one. Locking
    # ("pending") is applied only when the gate is enabled.
    if state != "success" and not is_enabled():
        return
    try:
        sha = sha or head_sha(git_provider)
        if not sha:
            get_logger().warning("feedback-gate: no head sha; skipping commit status.")
            return
        ok = git_provider.set_commit_status(sha, state, status_context())
        if not ok:
            get_logger().warning(f"feedback-gate: set_commit_status returned False ({state}).")
    except Exception as e:
        get_logger().warning(f"feedback-gate: failed to set status {state}: {e}")


def apply_pending(git_provider) -> None:
    _set(git_provider, "pending")


def apply_success(git_provider) -> None:
    _set(git_provider, "success")


def restamp_on_push(git_provider, project, mr_iid) -> None:
    if not is_enabled():
        return
    try:
        state = "success" if has_feedback(project, mr_iid) else "pending"
        _set(git_provider, state)
    except Exception as e:
        get_logger().warning(f"feedback-gate: restamp_on_push failed: {e}")
