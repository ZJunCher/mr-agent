import asyncio
import copy
import datetime
import traceback
from collections import OrderedDict
from functools import partial
from typing import List, Tuple

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.pr_processing import (add_ai_metadata_to_diff_files,
                                         get_pr_diff,
                                         retry_with_fallback_models)
from pr_agent.algo.review_chunking import build_review_chunk_plan, coverage_for_results
from pr_agent.algo.code_graph.context_expander import ChangedFile, build_related_files_context
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import (ModelType, PRReviewHeader,
                                 convert_to_markdown_v2, github_action_output,
                                 load_yaml, show_relevant_configurations)
from pr_agent.config_loader import get_settings
from pr_agent.algo.language_router import (
    detect_language_from_files,
    get_review_prompt_pairs,
    improve_prompt_pair_languages,
    language_scopes_for_mode,
    merge_review_predictions,
)
from pr_agent.git_providers import (get_git_provider,
                                    get_git_provider_with_context)
from pr_agent.git_providers.git_provider import (IncrementalPR,
                                                 get_main_pr_language)
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage
from pr_agent.suggestions.project_prompt_rules import (
    EffectiveProjectSkill,
    ProjectSkillSession,
    append_project_skill_context,
    project_skill_should_inject,
    project_skill_should_load,
)
from pr_agent.tools.ticket_pr_compliance_check import (
    extract_and_cache_pr_tickets, extract_tickets)


class PRReviewer:
    """
    The PRReviewer class is responsible for reviewing a pull request and generating feedback using an AI model.
    """

    def __init__(self, pr_url: str, is_answer: bool = False, is_auto: bool = False, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler, *, git_provider=None,
                 project_skill_session: ProjectSkillSession | None = None):
        """
        Initialize the PRReviewer object with the necessary attributes and objects to review a pull request.

        Args:
            pr_url (str): The URL of the pull request to be reviewed.
            is_answer (bool, optional): Indicates whether the review is being done in answer mode. Defaults to False.
            is_auto (bool, optional): Indicates whether the review is being done in automatic mode. Defaults to False.
            ai_handler (BaseAiHandler): The AI handler to be used for the review. Defaults to None.
            args (list, optional): List of arguments passed to the PRReviewer class. Defaults to None.
        """
        self.git_provider = git_provider or get_git_provider_with_context(pr_url)
        self.args = args
        self.incremental = self.parse_incremental(args)  # -i command
        if self.incremental and self.incremental.is_incremental:
            self.git_provider.get_incremental_commits(self.incremental)

        self._changed_files = tuple(self.git_provider.get_files())
        self.main_language = get_main_pr_language(self.git_provider.get_languages(), self._changed_files)
        self.project_skill_session = project_skill_session or ProjectSkillSession.load(
            self.git_provider,
            str(getattr(self.git_provider, "id_project", "") or ""),
            enabled=project_skill_should_load(),
        )
        detected_skill_language = detect_language_from_files(list(self._changed_files))
        self.project_skill_effective = self.project_skill_session.effective(
            "review",
            languages=language_scopes_for_mode(detected_skill_language),
            files=self._changed_files,
        )
        self.pr_url = pr_url
        self.is_answer = is_answer
        self.is_auto = is_auto

        if self.is_answer and not self.git_provider.is_supported("get_issue_comments"):
            raise Exception(f"Answer mode is not supported for {get_settings().config.git_provider} for now")
        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_language
        self.patches_diff = None
        self.prediction = None
        answer_str, question_str = self._get_user_answers()
        self.pr_description, self.pr_description_files = (
            self.git_provider.get_pr_description(split_changes_walkthrough=True))
        if (self.pr_description_files and get_settings().get("config.is_auto_command", False) and
                get_settings().get("config.enable_ai_metadata", False)):
            add_ai_metadata_to_diff_files(self.git_provider, self.pr_description_files)
            get_logger().debug(f"AI metadata added to the this command")
        else:
            get_settings().set("config.enable_ai_metadata", False)
            get_logger().debug(f"AI metadata is disabled for this command")

        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.pr_description,
            "language": self.main_language,
            "diff": "",  # empty diff for initial calculation
            "related_files_context": "",  # empty for initial calculation; populated in _prepare_prediction
            "num_pr_files": self.git_provider.get_num_of_files(),
            "num_max_findings": get_settings().pr_reviewer.num_max_findings,
            "require_score": get_settings().pr_reviewer.require_score_review,
            "require_tests": get_settings().pr_reviewer.require_tests_review,
            "require_estimate_effort_to_review": get_settings().pr_reviewer.require_estimate_effort_to_review,
            "require_estimate_contribution_time_cost": get_settings().pr_reviewer.require_estimate_contribution_time_cost,
            'require_can_be_split_review': get_settings().pr_reviewer.require_can_be_split_review,
            'require_security_review': get_settings().pr_reviewer.require_security_review,
            'require_todo_scan': get_settings().pr_reviewer.get("require_todo_scan", False),
            'question_str': question_str,
            'answer_str': answer_str,
            "extra_instructions": get_settings().pr_reviewer.extra_instructions,
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "custom_labels": "",
            "enable_custom_labels": get_settings().config.enable_custom_labels,
            "is_ai_metadata":  get_settings().get("config.enable_ai_metadata", False),
            "related_tickets": get_settings().get('related_tickets', []),
            'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
            "date": datetime.datetime.now().strftime('%Y-%m-%d'),
        }
        self.vars["project_prompt_rules"] = (
            self.project_skill_effective.render_context() if project_skill_should_inject("review") else ""
        )

        use_v2_prompts = bool(get_settings().get("pr_reviewer.code_graph.enabled", False))
        # 2026-07: code_graph.enabled 现在路由到 v3 提示词（曾依次是 v2 → 现在是 v3）
        review_prompt_key = "pr_review_prompt_v3" if use_v2_prompts else "pr_review_prompt"
        review_prompt_settings = get_settings().get(review_prompt_key)
        token_user_prompt = review_prompt_settings.user
        if self.vars["project_prompt_rules"]:
            token_user_prompt = f"{token_user_prompt}\n\n{{{{ project_prompt_rules }}}}"
        self.token_handler = TokenHandler(
            self.git_provider.pr,
            self.vars,
            review_prompt_settings.system,
            token_user_prompt,
        )

    def parse_incremental(self, args: List[str]):
        is_incremental = False
        if args and len(args) >= 1:
            arg = args[0]
            if arg == "-i":
                is_incremental = True
        incremental = IncrementalPR(is_incremental)
        return incremental

    async def run(self) -> None:
        try:
            if not self.git_provider.get_files():
                get_logger().info(f"PR has no files: {self.pr_url}, skipping review")
                return None

            if self.incremental.is_incremental and not self._can_run_incremental_review():
                return None

            # if isinstance(self.args, list) and self.args and self.args[0] == 'auto_approve':
            #     get_logger().info(f'Auto approve flow PR: {self.pr_url} ...')
            #     self.auto_approve_logic()
            #     return None

            get_logger().info(f'Reviewing PR: {self.pr_url} ...')
            relevant_configs = {'pr_reviewer': dict(get_settings().pr_reviewer),
                                'config': dict(get_settings().config)}
            get_logger().debug("Relevant configs", artifacts=relevant_configs)

            # ticket extraction if exists
            await extract_and_cache_pr_tickets(self.git_provider, self.vars)

            if self.incremental.is_incremental and hasattr(self.git_provider, "unreviewed_files_set") and not self.git_provider.unreviewed_files_set:
                get_logger().info(f"Incremental review is enabled for {self.pr_url} but there are no new files")
                previous_review_url = ""
                if hasattr(self.git_provider, "previous_review"):
                    previous_review_url = self.git_provider.previous_review.html_url
                if get_settings().config.publish_output:
                    self.git_provider.publish_comment(f"Incremental Review Skipped\n"
                                    f"No files were changed since the [previous PR Review]({previous_review_url})")
                return None

            if get_settings().config.publish_output and not get_settings().config.get('is_auto_command', False):
                self.git_provider.publish_comment("Preparing review...", is_temporary=True)

            await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
            if not self.prediction:
                coverage_failure = getattr(self, "coverage_failure_message", "")
                if coverage_failure and get_settings().config.publish_output:
                    self.git_provider.publish_comment(coverage_failure)
                self.git_provider.remove_initial_comment()
                return None

            # Run doc-drift detection in parallel with review rendering when enabled.
            # Result (list or None) is stored on self for _prepare_pr_review to inject.
            self._doc_drift_kept = None
            if get_settings().get('doc_drift.enabled', True) and get_settings().get('doc_drift.show_in_review', True):
                try:
                    from pr_agent.tools.pr_doc_drift import PRDocDrift
                    _dd = PRDocDrift(self.pr_url)
                    self._doc_drift_kept = await _dd.detect()
                except Exception:
                    get_logger().exception("doc-drift: review integration failed; row will be omitted.")

            pr_review = self._prepare_pr_review()
            coverage_notice = getattr(self, "coverage_partial_notice", "")
            if coverage_notice:
                pr_review = f"{coverage_notice}\n\n{pr_review}"
            get_logger().debug(f"PR output", artifact=pr_review)

            should_publish = get_settings().config.publish_output and self._should_publish_review_no_suggestions(pr_review)
            if not should_publish:
                reason = "Review output is not published"
                if get_settings().config.publish_output:
                    reason += ": no major issues detected."
                get_logger().info(reason)
                get_settings().data = {"artifact": pr_review}
                return

            # publish the review
            pr_review = self._append_feedback_section(pr_review)
            if get_settings().pr_reviewer.persistent_comment and not self.incremental.is_incremental:
                final_update_message = get_settings().pr_reviewer.final_update_message
                self.git_provider.publish_persistent_comment(pr_review,
                                                            initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
                                                            update_header=True,
                                                            final_update_message=final_update_message, )
            else:
                self.git_provider.publish_comment(pr_review)

            self.git_provider.remove_initial_comment()
        except Exception as e:
            get_logger().error(f"Failed to review PR: {e}")

    def _should_publish_review_no_suggestions(self, pr_review: str) -> bool:
        return get_settings().pr_reviewer.get('publish_output_no_suggestions', True) or "No major issues detected" not in pr_review

    def _append_feedback_section(self, pr_review: str) -> str:
        """Append a hidden review_id marker and an optional feedback hint.

        The marker lets a later ``/feedback`` command link the rating back to
        this specific review. The hint nudges users to rate the review. Both are
        best-effort and must never break review publishing.
        """
        try:
            import uuid
            text = pr_review or ""
            if get_settings().pr_reviewer.get("enable_feedback_hint", True):
                lang = str(get_settings().config.get("response_language", "en-US")).lower()
                if lang.startswith("zh"):
                    text += ("\n\n---\n💬 这次审查对你有帮助吗？回复 `/feedback [1~5] 你的意见（可选）`"
                             " 帮助我们改进，举例：`/feedback 5` 表示很有用，或 `/feedback 1 你的意见`。")
                else:
                    text += ("\n\n---\n💬 Was this review helpful? Reply `/feedback [1-5] (optional comment)`"
                             " to help us improve. E.g., `/feedback 5` if useful, or `/feedback 1 your comment`.")
            review_id = uuid.uuid4().hex[:12]
            text += f"\n\n<!-- pr_agent_review_id: {review_id} -->"
            self._record_project_skill_usage(review_id)
            text = self._append_eval_marker(text, review_id)
            return text
        except Exception as e:
            get_logger().warning(f"Failed to append feedback section: {e}")
            return pr_review

    def _record_project_skill_usage(self, review_id: str) -> None:
        try:
            from pr_agent.feedback.store import save_project_skill_usage
            from pr_agent.suggestions.prompt_provenance import build_project_skill_usage_identity

            effective = getattr(self, "project_skill_effective", None)
            if effective is None:
                return
            global_hash, bundle_hash = build_project_skill_usage_identity(effective, "review")
            save_project_skill_usage({
                "review_id": review_id,
                "command": "review",
                "project": getattr(self.git_provider, "id_project", ""),
                "mr_iid": getattr(self.git_provider, "id_mr", ""),
                "target_branch": effective.target_branch,
                "target_sha": effective.target_sha,
                "skill_hash": effective.skill_hash,
                "manifest_hash": effective.manifest_hash,
                "load_status": effective.status,
                "selected_rule_ids": list(effective.selected_rule_ids),
                "matched_files": dict(effective.matched_files),
                "reference_hashes": dict(effective.reference_hashes),
                "global_prompt_set_hash": global_hash,
                "prompt_bundle_hash": bundle_hash,
                "truncated": effective.truncated,
                "error": effective.error,
            })
        except Exception as exc:
            get_logger().warning(f"Failed to record project Skill usage: {exc}")

    def _append_eval_marker(self, text: str, review_id: str) -> str:
        """Opt-in: stamp a hidden ``pr-agent-eval`` marker freezing review context.

        Guarded by ``eval.enable_capture``; pure append and fully wrapped so it
        can never affect review publishing. Reuses ``review_id`` as the join key.
        """
        try:
            if not get_settings().get("eval.enable_capture", False):
                return text
            from pr_agent.eval.marker import build_eval_marker
            input_snapshot = self._build_eval_input_snapshot()
            marker = build_eval_marker(self.git_provider, review_id, input_snapshot)
            if marker:
                text += f"\n{marker}"
        except Exception as e:
            get_logger().warning(f"Failed to append eval marker: {e}")
        return text

    def _build_eval_input_snapshot(self) -> dict:
        """Freeze the non-code review inputs as of review time.

        These are exactly the values fed to the review prompt (already gathered
        in ``self.vars``), so a later replay can reproduce the same inputs even
        if the MR title/description/commits change afterwards. Best-effort.
        """
        snapshot = {}
        try:
            snapshot = {
                "title": self.vars.get("title"),
                "description": self.vars.get("description"),
                "commit_messages": self.vars.get("commit_messages_str"),
                "branch": self.vars.get("branch"),
                "related_tickets": get_settings().get("related_tickets", []),
            }
            effective = getattr(self, "project_skill_effective", None)
            if effective is not None:
                snapshot["project_skill"] = {
                    "target_sha": effective.target_sha,
                    "skill_hash": effective.skill_hash,
                    "status": effective.status,
                    "rule_ids": list(effective.selected_rule_ids),
                    "reference_hashes": dict(effective.reference_hashes),
                }
            try:
                mr = getattr(self.git_provider, "mr", None)
                if mr is not None:
                    snapshot["target_branch"] = getattr(mr, "target_branch", None)
                    snapshot["source_branch"] = getattr(mr, "source_branch", None)
            except Exception:
                pass
        except Exception as e:
            get_logger().warning(f"Failed to build eval input snapshot: {e}")
        return snapshot

    async def _prepare_prediction(self, model: str) -> None:
        if get_settings().get("large_mr_review.enabled", True):
            await self._prepare_prediction_map_reduce(model)
            return
        self.patches_diff = get_pr_diff(self.git_provider,
                                        self.token_handler,
                                        model,
                                        add_line_numbers_to_hunks=True,
                                        disable_extra_lines=False,)

        if self.patches_diff:
            self.related_files_context = self._get_related_files_context()
            get_logger().debug(f"PR diff", diff=self.patches_diff)
            self.prediction = await self._get_prediction(model)
        else:
            get_logger().warning(f"Empty diff for PR: {self.pr_url}")
            self.prediction = None

    async def _prepare_prediction_map_reduce(self, model: str) -> None:
        """Review every planned Diff chunk and derive coverage from trusted ownership."""
        self.coverage_failure_message = ""
        self.coverage_partial_notice = ""
        plan = build_review_chunk_plan(
            self.git_provider,
            self.token_handler,
            model,
            add_line_numbers=True,
            max_chunks=int(get_settings().get("large_mr_review.max_chunks", 20)),
            output_buffer_tokens=int(get_settings().get("large_mr_review.output_buffer_tokens", 1500)),
            metadata_tokens=int(get_settings().get("large_mr_review.chunk_metadata_tokens", 256)),
        )
        self.review_chunk_plan = plan
        self.patches_diff = "\n\n".join(chunk.text for chunk in plan.chunks)
        if not plan.is_complete_plan:
            self.review_coverage = coverage_for_results(plan, (), ())
            self.prediction = None
            self.coverage_failure_message = self._format_coverage_message(self.review_coverage, plan.status)
            get_logger().warning(self.coverage_failure_message)
            return

        self.related_files_context = self._get_related_files_context()
        max_concurrency = max(1, int(get_settings().get("large_mr_review.max_concurrency", 4)))
        semaphore = asyncio.Semaphore(max_concurrency)

        async def predict(chunk):
            async with semaphore:
                return await self._get_prediction(model, chunk.text)

        results = await asyncio.gather(*(predict(chunk) for chunk in plan.chunks), return_exceptions=True)
        successful_ids = []
        failed_ids = []
        predictions = []
        for chunk, result in zip(plan.chunks, results):
            if isinstance(result, BaseException) or not result:
                failed_ids.append(chunk.chunk_id)
                get_logger().warning(f"Large MR review chunk failed: {chunk.chunk_id[:12]}")
                continue
            successful_ids.append(chunk.chunk_id)
            predictions.append(result)
        self.review_coverage = coverage_for_results(plan, successful_ids, failed_ids)
        if not predictions:
            self.prediction = None
        elif len(predictions) == 1:
            self.prediction = predictions[0]
        else:
            self.prediction = merge_review_predictions(predictions)

        if self.review_coverage.status != "complete":
            message = self._format_coverage_message(self.review_coverage, "chunk_failure")
            if bool(get_settings().get("large_mr_review.fail_closed", True)):
                self.prediction = None
                self.coverage_failure_message = message
            else:
                self.coverage_partial_notice = message
            get_logger().warning(message)

    @staticmethod
    def _format_coverage_message(coverage, reason: str) -> str:
        completed = len(coverage.completed_unit_ids)
        expected = len(coverage.expected_unit_ids)
        return (
            "⚠️ 大 MR 审查未完整覆盖："
            f"已处理 {completed}/{expected} 个 Diff 单元，原因 `{reason}`。"
            "系统不会把该结果标记为完整审查。"
        )

    async def _prepare_prediction_related_files_only(self) -> None:
        """Test-only seam: same diff/related-files computation as
        `_prepare_prediction`, without requiring a full `get_pr_diff` mock
        chain. Production code calls `_prepare_prediction`; this exists so
        the diff-vs-related-files separation can be exercised directly."""
        self.patches_diff = get_pr_diff(self.git_provider, self.token_handler, "gpt-4",
                                        add_line_numbers_to_hunks=True, disable_extra_lines=False)
        if self.patches_diff:
            self.related_files_context = self._get_related_files_context()

    def _get_related_files_context(self) -> str:
        """Return optional dependency context without blocking a review on failure."""
        if not get_settings().get("pr_reviewer.code_graph.enabled", False):
            return ""

        try:
            target_branch = getattr(getattr(self.git_provider, "mr", None), "target_branch", None)
            if not target_branch:
                return ""

            repo_url = self.git_provider.get_git_repo_url(self.pr_url)
            if not repo_url:
                return ""
            clone_url = self.git_provider._prepare_clone_url_with_token(repo_url)
            if not clone_url:
                return ""

            changed_files = [
                ChangedFile(relpath=file.filename, new_content=file.head_file or "")
                for file in self.git_provider.get_diff_files()
                if getattr(file, "filename", None)
            ]
            return build_related_files_context(
                changed_files, clone_url, repo_url, target_branch, self.token_handler
            )
        except Exception as exc:
            get_logger().warning(f"code_graph: failed to build related-files context: {exc}")
            return ""

    async def _get_prediction(self, model: str, patches_diff: str | None = None) -> str:
        """
        Generate AI prediction(s) for the pull request review.
        For mixed-language PRs, makes separate calls per language and merges results.
        Always produces a single unified prediction.
        """
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff if patches_diff is None else patches_diff
        variables["related_files_context"] = getattr(self, "related_files_context", "")

        environment = Environment(undefined=StrictUndefined)

        # Get prompt pairs: 1 for pure language, 2 for mixed
        changed_files = tuple(getattr(self, "_changed_files", ()) or self.git_provider.get_files())
        detected_lang = detect_language_from_files(list(changed_files))
        use_v2_prompts = bool(get_settings().get("pr_reviewer.code_graph.enabled", False))
        prompt_pairs = get_review_prompt_pairs(detected_lang, use_v2=use_v2_prompts)
        prompt_languages = improve_prompt_pair_languages(detected_lang)

        predictions = []
        for pair_index, (sys_tmpl, usr_tmpl) in enumerate(prompt_pairs):
            system_prompt = environment.from_string(sys_tmpl).render(variables)
            user_prompt = environment.from_string(usr_tmpl).render(variables)
            session = getattr(self, "project_skill_session", None)
            effective = (
                session.effective(
                    "review",
                    languages=prompt_languages[pair_index],
                    files=changed_files,
                )
                if session is not None
                else EffectiveProjectSkill("", "review", "", "", "missing", "", "")
            )
            user_prompt = append_project_skill_context(user_prompt, effective)
            response, finish_reason = await self.ai_handler.chat_completion(
                model=model,
                temperature=get_settings().config.temperature,
                system=system_prompt,
                user=user_prompt
            )
            predictions.append(response)

        # Merge if multiple predictions (mixed PR), otherwise return as-is
        if len(predictions) > 1:
            return merge_review_predictions(predictions)
        return predictions[0]

    def _prepare_pr_review(self) -> str:
        """
        Prepare the PR review by processing the AI prediction and generating a markdown-formatted text that summarizes
        the feedback.
        """
        first_key = 'review'
        last_key = 'security_concerns'
        data = load_yaml(self.prediction.strip(),
                         keys_fix_yaml=["ticket_compliance_check", "estimated_effort_to_review_[1-5]:", "security_concerns:", "key_issues_to_review:",
                                        "relevant_file:", "relevant_line:", "suggestion:"],
                         first_key=first_key, last_key=last_key)
        github_action_output(data, 'review')

        if 'review' not in data:
            get_logger().exception("Failed to parse review data", artifact={"data": data})
            return ""

        # move data['review'] 'key_issues_to_review' key to the end of the dictionary
        if 'key_issues_to_review' in data['review']:
            key_issues_to_review = data['review'].pop('key_issues_to_review')
            # inject doc_drift row just before key_issues (only when detection ran)
            if getattr(self, '_doc_drift_kept', None) is not None:
                data['review']['doc_drift'] = self._doc_drift_kept  # [] = no drift, list = stale docs
            data['review']['key_issues_to_review'] = key_issues_to_review
        else:
            if getattr(self, '_doc_drift_kept', None) is not None:
                data['review']['doc_drift'] = self._doc_drift_kept

        incremental_review_markdown_text = None
        # Add incremental review section
        if self.incremental.is_incremental:
            last_commit_url = f"{self.git_provider.get_pr_url()}/commits/" \
                              f"{self.git_provider.incremental.first_new_commit_sha}"
            incremental_review_markdown_text = f"Starting from commit {last_commit_url}"

        markdown_text = convert_to_markdown_v2(data, self.git_provider.is_supported("gfm_markdown"),
                                            incremental_review_markdown_text,
                                               git_provider=self.git_provider,
                                               files=self.git_provider.get_diff_files())

        # Add help text if gfm_markdown is supported
        if self.git_provider.is_supported("gfm_markdown") and get_settings().pr_reviewer.enable_help_text:
            markdown_text += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide:</strong></summary><hr> \n\n"
            markdown_text += HelpMessage.get_review_usage_guide()
            markdown_text += "\n</details>\n"

        # Output the relevant configurations if enabled
        if get_settings().get('config', {}).get('output_relevant_configurations', False):
            markdown_text += show_relevant_configurations(relevant_section='pr_reviewer')

        # Add custom labels from the review prediction (effort, security)
        self.set_review_labels(data)

        if markdown_text == None or len(markdown_text) == 0:
            markdown_text = ""

        return markdown_text

    def _get_user_answers(self) -> Tuple[str, str]:
        """
        Retrieves the question and answer strings from the discussion messages related to a pull request.

        Returns:
            A tuple containing the question and answer strings.
        """
        question_str = ""
        answer_str = ""

        if self.is_answer:
            discussion_messages = self.git_provider.get_issue_comments()

            for message in discussion_messages.reversed:
                if "Questions to better understand the PR:" in message.body:
                    question_str = message.body
                elif '/answer' in message.body:
                    answer_str = message.body

                if answer_str and question_str:
                    break

        return question_str, answer_str

    def _get_previous_review_comment(self):
        """
        Get the previous review comment if it exists.
        """
        try:
            if hasattr(self.git_provider, "get_previous_review"):
                return self.git_provider.get_previous_review(
                    full=not self.incremental.is_incremental,
                    incremental=self.incremental.is_incremental,
                )
        except Exception as e:
            get_logger().exception(f"Failed to get previous review comment, error: {e}")

    def _remove_previous_review_comment(self, comment):
        """
        Remove the previous review comment if it exists.
        """
        try:
            if comment:
                self.git_provider.remove_comment(comment)
        except Exception as e:
            get_logger().exception(f"Failed to remove previous review comment, error: {e}")

    def _can_run_incremental_review(self) -> bool:
        """
        Checks if we can run incremental review according the various configurations and previous review.
        """
        # checking if running is auto mode but there are no new commits
        if self.is_auto and not self.incremental.first_new_commit_sha:
            get_logger().info(f"Incremental review is enabled for {self.pr_url} but there are no new commits")
            return False

        if not hasattr(self.git_provider, "get_incremental_commits"):
            get_logger().info(f"Incremental review is not supported for {get_settings().config.git_provider}")
            return False
        # checking if there are enough commits to start the review
        num_new_commits = len(self.incremental.commits_range)
        num_commits_threshold = get_settings().pr_reviewer.minimal_commits_for_incremental_review
        not_enough_commits = num_new_commits < num_commits_threshold
        # checking if the commits are not too recent to start the review
        recent_commits_threshold = datetime.datetime.now() - datetime.timedelta(
            minutes=get_settings().pr_reviewer.minimal_minutes_for_incremental_review
        )
        last_seen_commit_date = (
            self.incremental.last_seen_commit.commit.author.date if self.incremental.last_seen_commit else None
        )
        all_commits_too_recent = (
            last_seen_commit_date > recent_commits_threshold if self.incremental.last_seen_commit else False
        )
        # check all the thresholds or just one to start the review
        condition = any if get_settings().pr_reviewer.require_all_thresholds_for_incremental_review else all
        if condition((not_enough_commits, all_commits_too_recent)):
            get_logger().info(
                f"Incremental review is enabled for {self.pr_url} but didn't pass the threshold check to run:"
                f"\n* Number of new commits = {num_new_commits} (threshold is {num_commits_threshold})"
                f"\n* Last seen commit date = {last_seen_commit_date} (threshold is {recent_commits_threshold})"
            )
            return False
        return True

    def set_review_labels(self, data):
        if not get_settings().config.publish_output:
            return

        if not get_settings().pr_reviewer.require_estimate_effort_to_review:
            get_settings().pr_reviewer.enable_review_labels_effort = False # we did not generate this output
        if not get_settings().pr_reviewer.require_security_review:
            get_settings().pr_reviewer.enable_review_labels_security = False # we did not generate this output

        if (get_settings().pr_reviewer.enable_review_labels_security or
                get_settings().pr_reviewer.enable_review_labels_effort):
            try:
                review_labels = []
                if get_settings().pr_reviewer.enable_review_labels_effort:
                    estimated_effort = data['review']['estimated_effort_to_review_[1-5]']
                    estimated_effort_number = 0
                    if isinstance(estimated_effort, str):
                        try:
                            estimated_effort_number = int(estimated_effort.split(',')[0])
                        except ValueError:
                            get_logger().warning(f"Invalid estimated_effort value: {estimated_effort}")
                    elif isinstance(estimated_effort, int):
                        estimated_effort_number = estimated_effort
                    else:
                        get_logger().warning(f"Unexpected type for estimated_effort: {type(estimated_effort)}")
                    if 1 <= estimated_effort_number <= 5:  # 1, because ...
                        review_labels.append(f'Review effort {estimated_effort_number}/5')
                if get_settings().pr_reviewer.enable_review_labels_security and get_settings().pr_reviewer.require_security_review:
                    security_concerns = data['review']['security_concerns']  # yes, because ...
                    security_concerns_bool = 'yes' in security_concerns.lower() or 'true' in security_concerns.lower()
                    if security_concerns_bool:
                        review_labels.append('Possible security concern')

                current_labels = self.git_provider.get_pr_labels(update=True)
                if not current_labels:
                    current_labels = []
                get_logger().debug(f"Current labels:\n{current_labels}")
                if current_labels:
                    current_labels_filtered = [label for label in current_labels if
                                               not label.lower().startswith('review effort') and not label.lower().startswith(
                                                   'possible security concern')]
                else:
                    current_labels_filtered = []
                new_labels = review_labels + current_labels_filtered
                if (current_labels or review_labels) and sorted(new_labels) != sorted(current_labels):
                    get_logger().info(f"Setting review labels:\n{review_labels + current_labels_filtered}")
                    self.git_provider.publish_labels(new_labels)
                else:
                    get_logger().info(f"Review labels are already set:\n{review_labels + current_labels_filtered}")
            except Exception as e:
                get_logger().error(f"Failed to set review labels, error: {e}")

    def auto_approve_logic(self):
        """
        Auto-approve a pull request if it meets the conditions for auto-approval.
        """
        if get_settings().config.enable_auto_approval:
            is_auto_approved = self.git_provider.auto_approve()
            if is_auto_approved:
                get_logger().info("Auto-approved PR")
                self.git_provider.publish_comment("Auto-approved PR")
        else:
            get_logger().info("Auto-approval option is disabled")
            self.git_provider.publish_comment("Auto-approval option for PR-Agent is disabled. "
                                              "You can enable it via a [configuration file](https://github.com/Codium-ai/pr-agent/blob/main/docs/REVIEW.md#auto-approval-1)")
