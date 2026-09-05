import re
from functools import partial
from typing import Optional

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.config_loader import get_settings
from pr_agent.feedback import gate
from pr_agent.feedback.store import save_evolution_case, save_feedback
from pr_agent.feedback.timez import now_cn_iso
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger

# Hidden marker embedded into published reviews so a feedback can be linked back
# to the specific review it refers to. Kept in sync with PRReviewer.
REVIEW_ID_MARKER_RE = re.compile(r"<!--\s*pr_agent_review_id:\s*([0-9a-fA-F]+)\s*-->")


class PRFeedback:
    """Collect a user's rating (and optional comment) about a review result.

    Triggered via ``/feedback <score> [comment]`` (alias ``/rate``) on a merge
    request. The score and comment, together with context (MR, author, model,
    linked review id, ...), are persisted for later analysis/evaluation.

    This tool makes no LLM calls; ``ai_handler`` is accepted only for signature
    parity with the other tools.
    """

    def __init__(self, pr_url: str, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler,
                 reviewer_user: Optional[str] = None):
        self.pr_url = pr_url
        self.args = args or []
        self.ai_handler_cls = ai_handler
        self.reviewer_user = reviewer_user
        self.git_provider = get_git_provider_with_context(pr_url)
        self._eval_review_note_body = None

    def _parse_args(self):
        """Return (score, comment, error_message). score/comment are None on error."""
        if not self.args:
            return None, None, "missing_score"
        raw_score = str(self.args[0]).strip()
        comment = " ".join(str(a) for a in self.args[1:]).strip() or None
        try:
            score = int(raw_score)
        except (TypeError, ValueError):
            return None, None, "invalid_score"

        min_score = int(get_settings().get("pr_feedback.min_score", 1))
        max_score = int(get_settings().get("pr_feedback.max_score", 5))
        if score < min_score or score > max_score:
            return None, None, "out_of_range"

        threshold = int(get_settings().get("pr_feedback.comment_required_below", 5))
        if score < threshold and not comment:
            return None, None, "comment_required"
        return score, comment, None

    @staticmethod
    def _parse_evolution_case_comment(comment: Optional[str]) -> tuple[Optional[str], dict | None, str | None]:
        """Extract explicit case metadata without guessing from free text.

        Syntax: ``case=<kind> file=<path> line=<start[-end]> suggestion=<id> description``.
        """
        if not comment or not any(token.startswith("case=") for token in comment.split()):
            return comment, None, None
        metadata = {}
        description = []
        for token in comment.split():
            key, separator, value = token.partition("=")
            if separator and key in {"case", "file", "line", "suggestion"}:
                metadata[key] = value.strip()
            else:
                description.append(token)
        kind = metadata.get("case", "")
        if kind not in {"false_negative", "bad_fix"}:
            return comment, None, "invalid_case_kind"
        text = " ".join(description).strip()
        if not text:
            return comment, None, "case_description_required"
        line_start = line_end = 0
        if metadata.get("line"):
            try:
                parts = metadata["line"].split("-", 1)
                line_start = int(parts[0])
                line_end = int(parts[1]) if len(parts) == 2 else line_start
            except (TypeError, ValueError):
                return comment, None, "invalid_case_line"
        return text, {
            "kind": kind,
            "description": text,
            "file_path": metadata.get("file", ""),
            "line_start": line_start,
            "line_end": line_end,
            "suggestion_id": metadata.get("suggestion", ""),
        }, None

    def _find_latest_review_id(self) -> tuple:
        """Best-effort: find the latest review marker and its GitLab note/discussion ids.

        Returns (review_id, note_id, discussion_id) or (None, None, None) on failure.
        """
        try:
            mr = getattr(self.git_provider, "mr", None)
            if mr is None:
                return None, None, None
            candidates = []

            discussions = getattr(mr, "discussions", None)
            if discussions is not None:
                for discussion in discussions.list(get_all=True):
                    discussion_id = getattr(discussion, "id", None)
                    attributes = getattr(discussion, "attributes", {}) or {}
                    for note in attributes.get("notes", []) or []:
                        body = note.get("body", "") or ""
                        match = REVIEW_ID_MARKER_RE.search(body)
                        if match:
                            note_id = note.get("id", 0) or 0
                            updated_at = note.get("updated_at") or note.get("created_at") or ""
                            candidates.append((str(updated_at), note_id, match.group(1), note_id, discussion_id, body))

            if not candidates:
                notes = mr.notes.list(get_all=True)
                for note in notes:
                    body = getattr(note, "body", "") or ""
                    match = REVIEW_ID_MARKER_RE.search(body)
                    if match:
                        note_id = getattr(note, "id", 0) or 0
                        updated_at = getattr(note, "updated_at", None) or getattr(note, "created_at", None) or ""
                        attributes = getattr(note, "attributes", {}) or {}
                        discussion_id = getattr(note, "discussion_id", None) or attributes.get("discussion_id")
                        candidates.append((str(updated_at), note_id, match.group(1), note_id, discussion_id, body))

            if candidates:
                candidates.sort(key=lambda item: (item[0], item[1]))
                latest = candidates[-1]
                # stash the matched review body so eval capture can read it
                self._eval_review_note_body = latest[5] if len(latest) > 5 else None
                return latest[2], latest[3], latest[4]
        except Exception as e:
            get_logger().warning(f"Failed to locate review_id for feedback: {e}")
        return None, None, None

    def _collect_context(self) -> dict:
        context = {
            "pr_url": self.pr_url,
            "project": getattr(self.git_provider, "id_project", None),
            "mr_iid": getattr(self.git_provider, "id_mr", None),
            "model": get_settings().get("config.model", None),
            "source": get_settings().get("config.git_provider", "gitlab"),
        }
        try:
            mr = getattr(self.git_provider, "mr", None)
            if mr is not None:
                author = getattr(mr, "author", None)
                if isinstance(author, dict):
                    context["mr_author"] = author.get("username") or author.get("name")
                context["commit_sha"] = getattr(mr, "sha", None)
                if not context.get("pr_url"):
                    context["pr_url"] = getattr(mr, "web_url", None)
        except Exception as e:
            get_logger().warning(f"Failed to collect MR context for feedback: {e}")
        return context

    def _resolve_reviewer_user(self) -> Optional[str]:
        if self.reviewer_user:
            return self.reviewer_user
        return get_settings().get("pr_feedback.reviewer_user", None)

    def _publish(self, message: str):
        try:
            if get_settings().config.publish_output:
                self.git_provider.publish_comment(message)
        except Exception as e:
            get_logger().error(f"Failed to publish feedback confirmation: {e}")

    def _publish_in_thread(self, discussion_id: str, note_id: int, message: str):
        """Reply in the review thread and add a 👍 reaction."""
        if not get_settings().config.publish_output:
            return
        try:
            self.git_provider.reply_to_comment_from_comment_id(discussion_id, message)
        except Exception as e:
            get_logger().error(f"Failed to publish feedback confirmation in thread: {e}")
            self._publish(message)

        try:
            if note_id:
                self.git_provider.add_reaction(note_id, 'thumbsup')
        except Exception as e:
            get_logger().warning(f"Failed to add feedback reaction: {e}")

    async def run(self) -> None:
        try:
            score, comment, error = self._parse_args()
            if error:
                get_logger().info(f"Invalid /feedback command: {error}, args={self.args}")
                self._publish(self._help_message())
                return
            comment, evolution_case, case_error = self._parse_evolution_case_comment(comment)
            if case_error:
                get_logger().info(f"Invalid structured evolution case: {case_error}")
                self._publish(self._help_message())
                return

            review_id, review_note_id, review_discussion_id = self._find_latest_review_id()
            if evolution_case and not review_id:
                get_logger().info("Structured evolution case skipped: no reproducible review_id")
                self._publish(self._failure_message())
                return

            context = self._collect_context()
            record = {
                "created_at": now_cn_iso(),
                "reviewer_user": self._resolve_reviewer_user(),
                "score": score,
                "comment": comment,
                "review_id": review_id,
                **context,
            }

            saved = save_feedback(record)
            if saved:
                get_logger().info(
                    "Saved review feedback",
                    artifact={k: v for k, v in record.items() if k != "extra"},
                )
                self._maybe_capture_review_run(
                    review_id, review_note_id, review_discussion_id, context,
                    score=score, comment=comment)
                if evolution_case:
                    case_saved = save_evolution_case({
                        **evolution_case,
                        "project": str(context.get("project") or ""),
                        "mr_iid": str(context.get("mr_iid") or ""),
                        "review_id": str(review_id),
                        "head_sha": str(context.get("commit_sha") or ""),
                        "command": "review",
                        "source": "manual",
                        "created_at": record["created_at"],
                    })
                    if not case_saved:
                        get_logger().warning("Review rating saved but structured evolution case was rejected")
                gate.apply_success(self.git_provider)
                msg = self._confirmation_message(score)
                if review_discussion_id:
                    self._publish_in_thread(review_discussion_id, review_note_id, msg)
                else:
                    self._publish(msg)
            else:
                self._publish(self._failure_message())
        except Exception as e:
            get_logger().exception(f"Failed to handle /feedback command: {e}")

    def _maybe_capture_review_run(self, review_id, note_id, discussion_id, context,
                                  score=None, comment=None) -> None:
        """Opt-in: persist a baseline ``review_run`` for the offline eval benchmark.

        Guarded by ``eval.enable_capture``. Parses the frozen ``pr-agent-eval``
        marker from the located review note and stores it (with the review body
        as baseline output, plus the human ``score``/``comment``) keyed by
        ``review_id``. Best-effort; never raises.
        """
        try:
            if not get_settings().get("eval.enable_capture", False):
                return
            rid = review_id
            body = getattr(self, "_eval_review_note_body", None) or ""
            from pr_agent.eval.marker import parse_eval_marker
            from pr_agent.eval.store import save_review_run
            payload = parse_eval_marker(body) or {}
            rid = rid or payload.get("rid")
            if not rid:
                get_logger().info("Eval capture skipped: no review_id found")
                return
            input_snapshot = self._resolve_eval_input(payload)
            record = {
                "review_id": rid,
                "created_at": now_cn_iso(),
                "pr_url": payload.get("pr_url") or context.get("pr_url"),
                "provider": payload.get("provider") or context.get("source"),
                "project": payload.get("project") or context.get("project"),
                "mr_iid": payload.get("mr_iid") or context.get("mr_iid"),
                "base_sha": payload.get("base_sha"),
                "head_sha": payload.get("head_sha"),
                "start_sha": payload.get("start_sha"),
                "model": payload.get("model") or context.get("model"),
                "cfg": payload.get("cfg"),
                "review_output": body,
                "note_id": note_id,
                "discussion_id": discussion_id,
                "marker_ts": payload.get("ts"),
                "input": input_snapshot,
                "score": score,
                "comment": comment,
            }
            if save_review_run(record):
                get_logger().info("Captured review_run for eval", artifact={"review_id": rid})
        except Exception as e:
            get_logger().warning(f"Failed to capture review run for eval: {e}")

    def _resolve_eval_input(self, payload: dict) -> Optional[dict]:
        """Return the frozen review inputs for the eval set.

        Prefer the snapshot embedded in the review marker (frozen at review
        time). For older reviews whose marker has no ``input``, fall back to
        re-fetching from the live MR and tag the source so analysis can tell the
        two apart. Best-effort; never raises.
        """
        frozen = payload.get("input")
        if isinstance(frozen, dict) and frozen:
            return frozen
        try:
            gp = self.git_provider
            fetched = {
                "title": getattr(getattr(gp, "pr", None), "title", None),
                "description": gp.get_pr_description_full() if hasattr(gp, "get_pr_description_full") else None,
                "commit_messages": gp.get_commit_messages() if hasattr(gp, "get_commit_messages") else None,
                "related_tickets": get_settings().get("related_tickets", []),
                "input_source": "scoring_time_fallback",
            }
            mr = getattr(gp, "mr", None)
            if mr is not None:
                fetched["source_branch"] = getattr(mr, "source_branch", None)
                fetched["target_branch"] = getattr(mr, "target_branch", None)
            return {k: v for k, v in fetched.items() if v not in (None, [], "")}
        except Exception as e:
            get_logger().warning(f"Failed to resolve eval input fallback: {e}")
            return None

    @staticmethod
    def _is_zh() -> bool:
        lang = str(get_settings().get("config.response_language", "en-US")).lower()
        return lang.startswith("zh")

    def _confirmation_message(self, score: int) -> str:
        if self._is_zh():
            return f"✅ 已记录你的评分 {score}/5，感谢反馈👍！"
        return f"✅ Rating {score}/5 recorded. Thanks for your feedback! 👍"

    def _help_message(self) -> str:
        min_score = int(get_settings().get("pr_feedback.min_score", 1))
        max_score = int(get_settings().get("pr_feedback.max_score", 5))
        threshold = int(get_settings().get("pr_feedback.comment_required_below", 5))
        if self._is_zh():
            return (f"⚠️ 用法:`/feedback <{min_score}-{max_score}> [评论]`,"
                    f"例如 `/feedback 5` 或 `/feedback 1 误报太多`。"
                    "结构化案例可用 `case=false_negative file=src/a.py line=10 描述`，"
                    "或 `case=bad_fix suggestion=<id> 描述`。"
                    f"评分低于 {threshold} 分时必须填写评论。")
        return (f"⚠️ Usage: `/feedback <{min_score}-{max_score}> [comment]`, "
                f"e.g. `/feedback 5` or `/feedback 1 too many false positives`. "
                f"A comment is required when the score is below {threshold}.")

    def _failure_message(self) -> str:
        if self._is_zh():
            return "⚠️ 抱歉，记录反馈时出现问题，请稍后再试。"
        return "⚠️ Sorry, something went wrong while recording your feedback. Please try again later."
