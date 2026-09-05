from functools import partial
from typing import Optional

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.llm_feedback import format_llm_feedback_markdown, get_llm_feedback
from pr_agent.config_loader import get_settings
from pr_agent.feedback import gate
from pr_agent.feedback.timez import now_cn_iso
from pr_agent.git_providers import get_git_provider
from pr_agent.log import get_logger
from pr_agent.suggestions import inline_publisher
from pr_agent.suggestions.review_tracking import (
    get_current_run_id,
    get_review_run,
    record_review_event,
    track_review_run,
    update_review_run,
)
from pr_agent.suggestions.tier2_scheduler import schedule_tier2
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_help_message import PRHelpMessage
from pr_agent.tools.pr_report_export import build_and_upload_report
from pr_agent.tools.pr_reviewer import PRReviewer


class PRMrCreate:
    def __init__(self, pr_url: str, args: list = None,
                 ai_handler: partial[BaseAiHandler, ] = LiteLLMAIHandler):
        self.git_provider = get_git_provider()(pr_url)
        self.ai_handler_cls = ai_handler
        self.args = args or []
        self.llm_feedback = []

    @track_review_run("auto_mr_create")
    async def run(self):
        try:
            get_logger().info("Running mr_create composite command")
            original_publish = bool(get_settings().config.publish_output)
            original_auto = bool(get_settings().config.get("is_auto_command", False))

            # prevent sub-tools from publishing multiple comments
            get_settings().config.publish_output = False
            get_settings().config.is_auto_command = True

            review_md = await self._safe_tool_run("review", lambda: PRReviewer(self.git_provider.pr_url, ai_handler=self.ai_handler_cls).run())
            # Constructed lazily inside the factory (not eagerly here) so a
            # bad pr_url/git-provider failure is still caught by
            # _safe_tool_run's try/except, exactly like the other sub-tools.
            improve_tool_holder = {}

            async def _run_improve():
                run_id = get_current_run_id()
                update_review_run(run_id, stage="improve_started", improve_started_at=now_cn_iso())
                record_review_event(run_id, "improve_started", "improve_started")
                tool = PRCodeSuggestions(self.git_provider.pr_url, ai_handler=self.ai_handler_cls)
                improve_tool_holder["tool"] = tool
                data = await tool.generate_suggestions_data()
                code_suggestions = list(data.get("code_suggestions") or [])
                if not code_suggestions:
                    current = get_review_run(run_id) if run_id else {}
                    generated_count = int(current.get("generated_count") or 0)
                    filtered_count = int(current.get("filtered_count") or 0)
                    unpublished_reason = (
                        "secondary_review_filtered"
                        if generated_count > 0 and filtered_count == generated_count
                        else None
                    )
                    update_review_run(
                        run_id, stage="published", unpublished_reason=unpublished_reason,
                    )
                    record_review_event(
                        run_id, "publishing_completed", "published", status="completed",
                        details={
                            "selected_count": 0, "skipped_count": 0, "published_count": 0,
                            "failed_count": 0, "unpublished_reason": unpublished_reason,
                        },
                    )
                    artifact = tool.no_suggestions_markdown() if generated_count == 0 else ""
                    get_settings().data = {"artifact": artifact, "code_suggestions": []}
                    return
                improve_tool_holder["report_md"] = tool.generate_summarized_suggestions(
                    {"code_suggestions": code_suggestions})
                # Reserve the summary's top timeline position without claiming
                # any inline suggestion exists before GitLab confirms it.
                pr_body = tool.generate_pending_suggestions()
                get_settings().data = {"artifact": pr_body, "code_suggestions": code_suggestions}

            improve_md = await self._safe_tool_run("improve", _run_improve)
            help_md = await self._safe_tool_run("help", lambda: PRHelpMessage(self.git_provider.pr_url, ai_handler=self.ai_handler_cls).run())

            # restore settings
            get_settings().config.publish_output = original_publish
            get_settings().config.is_auto_command = original_auto

            sections = []
            if review_md:
                sections.append(review_md.strip())
            if improve_md:
                sections.append(improve_md.strip())
            if help_md:
                sections.append(help_md.strip())
            feedback_md = format_llm_feedback_markdown(self.llm_feedback)
            if feedback_md:
                sections.append(feedback_md.strip())

            unique_sections = []
            seen = set()
            improve_kept = False
            for s in sections:
                if not s:
                    continue
                if s in seen:
                    continue
                if not improve_kept and (s.startswith("## PR Code Suggestions ✨") or s.startswith("## PR 代码建议 ✨")):
                    improve_kept = True
                    unique_sections.append(s)
                    seen.add(s)
                    continue
                if improve_kept and (s.startswith("## PR Code Suggestions ✨") or s.startswith("## PR 代码建议 ✨")):
                    continue
                unique_sections.append(s)
                seen.add(s)

            if not unique_sections:
                get_logger().info("mr_create produced no content; skipping publish")
                return

            combined = "\n\n___\n\n".join(unique_sections)

            # The report given to build_and_upload_report is deliberately
            # built from review_md + improve_md only (NOT unique_sections,
            # which also carries help_md and the LLM-status-feedback
            # section) -- the usage-guide/help text and status warnings are
            # noise for a downstream AI reviewer and don't belong in the
            # exported report, even though they still appear in the
            # published comment below.
            report_sections = []
            if review_md:
                report_sections.append(review_md.strip())
            report_improve_md = improve_tool_holder.get("report_md") or improve_md
            if report_improve_md:
                report_sections.append(report_improve_md.strip())
            report_source_md = "\n\n___\n\n".join(report_sections)

            report_result = build_and_upload_report(self.git_provider, report_source_md)
            if report_result:
                download_url, report_filename = report_result
                combined = combined + (
                    "\n\n___\n\n"
                    f"**下载报告📥 （可供本地AI确认修改）：**[{report_filename}]({download_url})"
                )

            body = combined
            if gate.is_enabled():
                body = body + gate.guidance_md()
            get_logger().debug("mr_create combined output", artifact=body)

            if original_publish:
                published_comment = self.git_provider.publish_comment(body)
                if gate.is_enabled():
                    gate.apply_pending(self.git_provider)
                # Mutable snapshot of what's currently live in the combined
                # comment, so the Tier-2 table refresh below can edit it in
                # place and know what the current body/table markdown is.
                comment_state = {"body": body, "improve_md": improve_md}

                # Publish inline suggestions AFTER the pending summary comment
                # so the overview stays first. Then replace that pending
                # section with only suggestions GitLab actually published.
                improve_tool = improve_tool_holder.get("tool")
                code_suggestions = list((getattr(improve_tool, "data", None) or {}).get("code_suggestions", []) or [])
                if code_suggestions:
                    inline_summary = await self._publish_inline_suggestions(
                        code_suggestions, getattr(improve_tool, "prompt_provenance", None))
                    published_locations = (inline_summary or {}).get("published_locations") or []
                    published_suggestions = inline_publisher.backfill_note_urls(
                        code_suggestions, published_locations)
                    improve_tool.data["code_suggestions"] = published_suggestions
                    await self._relink_improve_table(
                        improve_tool, comment_state, published_comment, published_suggestions)

                # Pipeline v2 Tier-2: fire-and-forget heavy repair for whatever
                # deterministic_fix/tier1_repair could not resolve. Scheduled
                # AFTER the main comment + inline suggestions are already
                # published, so it never delays this response. No-op when
                # tier2_enabled is false (default) or there's nothing pending.
                # When Tier-2 does resolve something, the /improve summary
                # table (embedded in this same combined comment) is stale --
                # it was rendered before Tier-2 ran -- so on_complete below
                # edits it in place to include Tier-2's additions, keeping the
                # table and the inline suggestions consistent.
                pending_tier2 = list(getattr(improve_tool_holder.get("tool"), "pending_tier2_tasks", []) or [])

                async def _on_tier2_complete(tier2_result):
                    await self._refresh_improve_table(
                        improve_tool_holder.get("tool"), comment_state, published_comment, tier2_result)

                prompt_provenance = getattr(improve_tool_holder.get("tool"), "prompt_provenance", None)
                schedule_kwargs = {"on_complete": _on_tier2_complete}
                if prompt_provenance is not None:
                    schedule_kwargs["prompt_provenance"] = prompt_provenance
                schedule_tier2(self.git_provider, pending_tier2, **schedule_kwargs)
        except Exception as e:
            run_id = get_current_run_id()
            current = get_review_run(run_id) if run_id else {}
            failure_stage = "execution_failed" if current.get("improve_started_at") else "startup_failed"
            update_review_run(
                run_id, stage=failure_stage, status="failed",
                error_code=type(e).__name__, error_message=str(e),
            )
            record_review_event(
                run_id, failure_stage, failure_stage, status="failed",
                error_code=type(e).__name__, error_message=str(e),
            )
            get_logger().exception(f"mr_create failed: {e}")

    async def _relink_improve_table(self, improve_tool, comment_state: dict,
                                     combined_comment, code_suggestions: list) -> None:
        """Replace the pending /improve section with published suggestions.

        The combined comment is created before inline suggestions so it stays
        above them in the timeline. Any failure leaves only the pending section,
        never a summary row without a corresponding inline discussion.
        """
        try:
            improve_md = comment_state.get("improve_md")
            original_body = comment_state.get("body")
            if not improve_tool or not improve_md or combined_comment is None:
                return
            updated_improve_md = improve_tool.generate_summarized_suggestions({"code_suggestions": code_suggestions})
            if not updated_improve_md or not updated_improve_md.strip():
                return
            updated_body = original_body.replace(improve_md.strip(), updated_improve_md.strip())
            if updated_body == original_body:
                get_logger().warning(
                    "Inline suggestion relink: improve section not found verbatim in the combined "
                    "comment body; skipping edit to avoid publishing a no-op or corrupted comment")
                return
            self.git_provider.edit_comment(combined_comment, updated_body)
            comment_state["body"] = updated_body
            comment_state["improve_md"] = updated_improve_md
            get_logger().info("Relinked the /improve summary table with published inline suggestion URLs")
        except Exception as e:
            get_logger().exception(f"Relinking improve table with inline suggestion links failed: {e}")

    async def _refresh_improve_table(self, improve_tool, comment_state: dict,
                                      combined_comment, tier2_result: dict) -> None:
        """Re-render the /improve summary table to include suggestions Tier-2
        published inline asynchronously, then edit the combined comment in
        place. Tier-2 copy-patch and failed one-click results are excluded.
        """
        try:
            improve_md = comment_state.get("improve_md")
            original_body = comment_state.get("body")
            one_click = list((tier2_result or {}).get("one_click") or [])
            new_suggestions = [suggestion for suggestion in one_click if suggestion.get("inline_note_url")]
            if not improve_tool or not improve_md or not new_suggestions or combined_comment is None:
                return
            existing = list((getattr(improve_tool, "data", None) or {}).get("code_suggestions", []) or [])
            merged = existing + new_suggestions
            updated_improve_md = improve_tool.generate_summarized_suggestions({"code_suggestions": merged})
            if not updated_improve_md or not updated_improve_md.strip():
                return
            updated_body = original_body.replace(improve_md.strip(), updated_improve_md.strip())
            if updated_body == original_body:
                get_logger().warning(
                    "Tier-2 table refresh: improve section not found verbatim in the combined "
                    "comment body; skipping edit to avoid publishing a no-op or corrupted comment")
                return
            self.git_provider.edit_comment(combined_comment, updated_body)
            comment_state["body"] = updated_body
            comment_state["improve_md"] = updated_improve_md
            get_logger().info(
                f"Tier-2 refreshed the /improve summary table with {len(new_suggestions)} additional suggestion(s) "
                f"({len(one_click)} one-click candidates)")
        except Exception as e:
            get_logger().exception(f"Tier-2 table refresh failed: {e}")

    async def _publish_inline_suggestions(self, code_suggestions, prompt_provenance=None) -> dict:
        try:
            return await inline_publisher.publish_inline_suggestions_async(
                self.git_provider, code_suggestions, prompt_provenance=prompt_provenance)
        except Exception as e:
            get_logger().exception(f"inline suggestion publishing failed: {e}")
            return {}

    async def _safe_tool_run(self, tool_name: str, tool_coro_factory) -> Optional[str]:
        try:
            # clear previous artifact
            if hasattr(get_settings(), "data"):
                get_settings().set("data", None)
            await tool_coro_factory()
            artifact = (getattr(get_settings(), "data", {}) or {}).get("artifact")
            self._collect_llm_feedback(tool_name)
            return artifact or ""
        except Exception as e:
            if tool_name == "improve":
                run_id = get_current_run_id()
                update_review_run(
                    run_id, stage="execution_failed", status="failed",
                    error_code=type(e).__name__, error_message=str(e),
                )
                record_review_event(
                    run_id, "execution_failed", "execution_failed", status="failed",
                    error_code=type(e).__name__, error_message=str(e),
                )
            get_logger().exception(f"Sub-tool run failed: {e}")
            self._collect_llm_feedback(tool_name)
            return ""

    def _collect_llm_feedback(self, tool_name: str) -> None:
        for item in get_llm_feedback():
            item = dict(item)
            item["context"] = tool_name
            if item not in self.llm_feedback:
                self.llm_feedback.append(item)
