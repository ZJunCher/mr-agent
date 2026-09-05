import asyncio
import copy
import difflib
import hashlib
import json
import re
import textwrap
import traceback
from collections import OrderedDict
from datetime import datetime
from functools import partial
from typing import Dict, List

from jinja2 import Environment, StrictUndefined

from pr_agent.algo import MAX_TOKENS
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.code_graph.context_expander import ChangedFile, build_related_files_context
from pr_agent.algo.git_patch_processing import decouple_and_convert_to_hunks_with_lines_numbers
from pr_agent.algo.hunk_line_matcher import find_lines_in_new_hunk
from pr_agent.algo.language_router import (
    detect_language_from_files,
    get_improve_prompt_pairs,
    improve_prompt_pair_languages,
    language_scopes_for_mode,
)
from pr_agent.algo.model_resilience import ModelAttemptFailure
from pr_agent.algo.pr_processing import (
    add_ai_metadata_to_diff_files,
    get_pr_diff,
    get_pr_multi_diffs,
    retry_with_fallback_models,
)
from pr_agent.algo.review_chunking import build_review_chunk_plan, coverage_for_results
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import (
    ModelType,
    clip_tokens,
    get_max_tokens,
    get_model,
    load_yaml,
    replace_code_tags,
    show_relevant_configurations,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import (
    AzureDevopsProvider,
    GithubProvider,
    GitLabProvider,
    get_git_provider,
    get_git_provider_with_context,
)
from pr_agent.git_providers.git_provider import GitProvider, get_main_pr_language
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage
from pr_agent.suggestions import inline_publisher
from pr_agent.suggestions.project_prompt_rules import (
    EffectiveProjectSkill,
    PROJECT_SKILL_MANIFEST_PATH,
    ProjectSkillSession,
    ProjectRuleSet,
    append_project_skill_context,
    filter_project_rules,
    project_skill_should_inject,
    project_skill_should_load,
)
from pr_agent.suggestions.prompt_provenance import build_prompt_provenance
from pr_agent.suggestions.review_tracking import (
    get_current_run_id,
    record_review_event,
    track_review_run,
    update_review_run,
)
from pr_agent.suggestions.store import save_filtered_suggestion
from pr_agent.suggestions.tier2_scheduler import schedule_tier2
from pr_agent.tools.pr_description import insert_br_after_x_chars

# The self-reflect LLM call is asked for a specific top-level YAML key
# ("code_suggestions"), but reasoning models sometimes drift to a
# differently-named-but-compatible key (e.g. "suggestions"). Recognize those
# so a naming slip doesn't silently zero out every suggestion's score/line
# range (which otherwise renders as relevant_lines_start/end == -1 downstream).
_REFLECT_FEEDBACK_KEY_ALIASES = ["code_suggestions", "suggestions", "code_suggestion_feedback", "feedback"]
_TRIGGER_PREFIXES = ("Trigger:", "触发场景：", "触发场景:")
_TRIGGER_VAGUE_PATTERNS = [
    r"某些情况下", r"可能", r"偶尔", r"有时", r"大概", r"极端情况下",
    r"in some cases", r"might", r"sometimes", r"possibly", r"likely",
]
_TRIGGER_QUANTIFIED_HINTS = [
    r"<=|>=|<|>|==|!=|低于|高于|超过|不低于|不高于|等于|至少|至多|不超过",
    r"\d+(?:\.\d+)?\s*(?:m/s|km/h|ms|s|hz|fps|%|米|秒|帧|次|个)",
    r"\d+(?:\.\d+)?",
]


class SuggestionOutputSchemaError(ValueError):
    """The model output cannot satisfy the production improve Schema."""


def _extract_reflect_feedback_list(response_reflect_yaml):
    """Return (feedback_list, matched_key) from a self-reflect YAML response.

    Tries the expected key first, then known aliases. Returns ([], None) when
    the parsed YAML is empty/falsy or none of the known keys are present —
    callers should log the unmatched top-level keys for monitoring, since that
    indicates the model drifted to an entirely different (unmappable) schema.
    """
    if not response_reflect_yaml or not isinstance(response_reflect_yaml, dict):
        return [], None
    for key in _REFLECT_FEEDBACK_KEY_ALIASES:
        value = response_reflect_yaml.get(key)
        if value:
            return value, key
    return [], None


def _effective_prompt_templates(prompt_pairs, reflection_key: str) -> dict[str, str]:
    """Build the effective stage-template map used for Prompt provenance.

    Captures the exact system/user strings that will be rendered for every
    `/improve` Prompt stage (generation, reflection, scenario validation,
    inline self-check, inline de-conflict, and Tier-1 repair) so the
    prompt_bundle_hash reflects what was actually deployed at run time.
    """
    templates: dict[str, str] = {}
    for index, (system, user) in enumerate(prompt_pairs):
        templates[f"generation:{index}:system"] = system
        templates[f"generation:{index}:user"] = user
    stages = {
        "reflection": get_settings().get(reflection_key),
        "scenario_validator": get_settings().pr_code_suggestions_scenario_validator_prompt,
        "inline_selfcheck": get_settings().pr_inline_selfcheck_prompt,
        "inline_deconflict": get_settings().pr_inline_deconflict_prompt,
        "tier1_repair": get_settings().pr_tier1_repair_prompt,
    }
    for name, section in stages.items():
        templates[f"{name}:system"] = section.system
        templates[f"{name}:user"] = section.user
    return templates


class PRCodeSuggestions:
    def __init__(self, pr_url: str, cli_mode=False, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler, *, git_provider=None,
                 project_skill_session: ProjectSkillSession | None = None):

        self.git_provider = git_provider or get_git_provider_with_context(pr_url)
        self._changed_files = tuple(self.git_provider.get_files())
        self.main_language = get_main_pr_language(self.git_provider.get_languages(), self._changed_files)

        num_code_suggestions = int(get_settings().pr_code_suggestions.num_code_suggestions_per_chunk)

        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_language
        self.patches_diff = None
        self.prediction = None
        self.pr_url = pr_url
        self.cli_mode = cli_mode
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
            "diff_no_line_numbers": "",  # empty diff for initial calculation
            "related_files_context": "",  # empty for initial calculation; populated in prepare_prediction_main
            "num_code_suggestions": num_code_suggestions,
            "extra_instructions": get_settings().pr_code_suggestions.extra_instructions,
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "relevant_best_practices": "",
            "is_ai_metadata": get_settings().get("config.enable_ai_metadata", False),
            "focus_only_on_problems": get_settings().get("pr_code_suggestions.focus_only_on_problems", False),
            "date": datetime.now().strftime('%Y-%m-%d'),
            'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
        }

        use_v2_prompts = bool(get_settings().get("pr_reviewer.code_graph.enabled", False))
        # 2026-07: code_graph.enabled 现在路由到 v3 提示词（曾依次是 v2 → 现在是 v3）
        base_key = "pr_code_suggestions_prompt_v3" if use_v2_prompts else "pr_code_suggestions_prompt"
        not_decoupled_key = "pr_code_suggestions_prompt_not_decoupled_v3" if use_v2_prompts else "pr_code_suggestions_prompt_not_decoupled"

        if get_settings().pr_code_suggestions.get("decouple_hunks", True):
            base_prompt_settings = get_settings().get(base_key)
        else:
            base_prompt_settings = get_settings().get(not_decoupled_key)
        self.pr_code_suggestions_prompt_system = base_prompt_settings.system
        self.pr_code_suggestions_prompt_user = base_prompt_settings.user

        # Language-based prompt routing: store all prompt pairs for dual-call merge
        detected_lang = detect_language_from_files(list(self._changed_files))
        self._detected_improve_language = detected_lang
        self._improve_rule_languages = language_scopes_for_mode(detected_lang)
        self._improve_prompt_pairs = get_improve_prompt_pairs(
            detected_lang, self.pr_code_suggestions_prompt_system, self.pr_code_suggestions_prompt_user,
            use_v2=use_v2_prompts,
        )
        self._improve_prompt_languages = improve_prompt_pair_languages(detected_lang)
        # Use the first pair as default for token handler
        self.pr_code_suggestions_prompt_system = self._improve_prompt_pairs[0][0]
        self.pr_code_suggestions_prompt_user = self._improve_prompt_pairs[0][1]

        # Pin one project-owned Skill snapshot to the target branch SHA and
        # reuse it through generation, reflection, validation, and repair.
        self.project_skill_session = project_skill_session or ProjectSkillSession.load(
            self.git_provider,
            str(getattr(self.git_provider, "id_project", "") or ""),
            enabled=project_skill_should_load(),
        )
        setattr(self.git_provider, "_project_skill_session", self.project_skill_session)
        self.project_skill_effective = self.project_skill_session.effective(
            "improve",
            languages=self._improve_rule_languages,
            files=self._changed_files,
        )
        self.project_rule_set = ProjectRuleSet(
            project=self.project_skill_session.rule_set.project,
            rules=self.project_skill_effective.rules,
            name=self.project_skill_session.rule_set.name,
            description=self.project_skill_session.rule_set.description,
            target_branch=self.project_skill_session.rule_set.target_branch,
            target_sha=self.project_skill_session.rule_set.target_sha,
            manifest_hash=self.project_skill_session.rule_set.manifest_hash,
            status=self.project_skill_effective.status,
            error=self.project_skill_effective.error,
        )
        self.vars["project_prompt_rules"] = self._project_skill_context(
            "generation", self._improve_rule_languages,
        )
        token_user_prompt = self.pr_code_suggestions_prompt_user
        if self.vars["project_prompt_rules"]:
            token_user_prompt = f"{token_user_prompt}\n\n{{{{ project_prompt_rules }}}}"
        self.token_handler = TokenHandler(
            self.git_provider.pr,
            self.vars,
            self.pr_code_suggestions_prompt_system,
            token_user_prompt,
        )
        reflection_key = (
            "pr_code_suggestions_reflect_prompt_v2"
            if bool(get_settings().get("pr_code_suggestions.pipeline_v2_enabled", False))
            else "pr_code_suggestions_reflect_prompt"
        )
        self.prompt_provenance = build_prompt_provenance(
            filter_project_rules(self.project_rule_set, self._improve_rule_languages),
            _effective_prompt_templates(self._improve_prompt_pairs, reflection_key),
            effective_skill=self.project_skill_effective,
        )

        self.pending_tier2_tasks = []  # populated by run_repair_pipeline() when pipeline_v2 is enabled

        self.progress = f"## Generating PR code suggestions\n\n"
        self.progress += f"""\nWork in progress ...<br>\n<img src="https://codium.ai/images/pr_agent/dual_ball_loading-crop.gif" width=48>"""
        self.progress_response = None
        self.related_files_context = ""

    def _project_skill_effective(self, target: str, languages=None, files=None):
        session = getattr(self, "project_skill_session", None)
        if session is None:
            return EffectiveProjectSkill(
                project="",
                target="improve",
                target_branch="",
                target_sha="",
                status="missing",
                manifest_hash="",
                skill_hash="",
            )
        return session.effective(
            target,
            languages=self._improve_rule_languages if languages is None else languages,
            files=self._changed_files if files is None else files,
        )

    def _project_skill_context(self, target: str, languages=None, files=None) -> str:
        effective = self._project_skill_effective(target, languages, files)
        if not project_skill_should_inject(target):
            return ""
        return effective.render_context()

    def _get_related_files_context(self) -> str:
        """Best-effort file-level dependency context (see the code_graph
        package). Never raises - any failure here must not block the
        improve run; it just means no related-files section gets appended."""
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

    def _filter_mr_context(self) -> dict:
        """Return MR context (project/mr_iid/mr_url/mr_author/commit_sha) for
        persisting filtered suggestions. Never raises; missing fields become ''.
        Mirrors inline_publisher._mr_url/_mr_author/_commit_sha.
        """
        ctx = {"project": "", "mr_iid": "", "mr_url": "", "mr_author": "", "commit_sha": ""}
        try:
            ctx["project"] = str(getattr(self.git_provider, "id_project", "") or "")
        except Exception:
            pass
        try:
            ctx["mr_iid"] = str(getattr(self.git_provider, "id_mr", "") or "")
        except Exception:
            pass
        try:
            ctx["mr_url"] = str(self.git_provider.get_pr_url() or "")
        except Exception:
            pass
        try:
            mr = getattr(self.git_provider, "mr", None)
            author = getattr(mr, "author", None) if mr is not None else None
            if isinstance(author, dict):
                ctx["mr_author"] = str(author.get("username") or author.get("name") or "")
        except Exception:
            pass
        try:
            refs = self.git_provider.get_diff_refs()
            if isinstance(refs, dict):
                ctx["commit_sha"] = str(refs.get("head_sha") or "")
        except Exception:
            pass
        return ctx

    def _record_model_attempt_failure(self, failure: ModelAttemptFailure) -> None:
        safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", failure.model)[:40]
        record_review_event(
            get_current_run_id(),
            f"model_attempt_failed:{failure.attempt}:{safe_model}",
            "generating",
            error_code=failure.kind.value,
            error_message=failure.message,
            details={
                "model": failure.model,
                "deployment_configured": bool(failure.deployment_id),
                "attempt": failure.attempt,
                "elapsed_ms": failure.elapsed_ms,
            },
        )

    async def generate_suggestions_data(self) -> dict:
        """Run the model call + scenario-constraint validation and return the
        structured `{"code_suggestions": [...]}` dict, WITHOUT rendering or
        publishing anything. Callers that need the final markdown call
        `generate_summarized_suggestions(data)` themselves afterward -- this
        split lets callers (e.g. pr_mr_create.py) do additional work, such as
        publishing inline suggestions, between generation and rendering, so
        the rendered table can reflect what was actually published.
        """
        self._capture_high_fidelity_snapshot()
        data = await retry_with_fallback_models(
            self.prepare_prediction_main,
            model_type=ModelType.REGULAR,
            retry_limit=max(0, int(get_settings().config.model_retry_limit)),
            include_independent=True,
            base_seconds=max(0.0, float(get_settings().config.model_retry_base_seconds)),
            max_seconds=max(0.0, float(get_settings().config.model_retry_max_seconds)),
            on_failure=self._record_model_attempt_failure,
        )
        if not data:
            data = {"code_suggestions": []}
        generated_count = len(data.get("code_suggestions") or [])
        update_review_run(stage="scenario_validation", generated_count=generated_count)
        record_review_event(
            get_current_run_id(), "generation_completed", "generation_completed",
            details={"generated_count": generated_count},
        )
        data = await self.validate_suggestions_scenario_constraints(data)
        kept_count = len(data.get("code_suggestions") or [])
        update_review_run(
            stage="validated", generated_count=generated_count,
            kept_count=kept_count, filtered_count=max(0, generated_count - kept_count),
        )
        record_review_event(
            get_current_run_id(), "secondary_review_completed", "validated",
            details={
                "generated_count": generated_count,
                "kept_count": kept_count,
                "filtered_count": max(0, generated_count - kept_count),
            },
        )
        self.data = data
        return data

    def _capture_high_fidelity_snapshot(self) -> None:
        """Persist the immutable MR identity needed for later read-only paired replay."""
        try:
            if not bool(get_settings().get("eval.enable_capture", False)):
                return
            refs = self.git_provider.get_diff_refs()
            if not isinstance(refs, dict) or not refs.get("base_sha") or not refs.get("head_sha"):
                return
            from pr_agent.eval.store import save_review_run

            project = str(getattr(self.git_provider, "id_project", "") or "")
            mr_iid = str(getattr(self.git_provider, "id_mr", "") or "")
            effective = getattr(self, "project_skill_effective", None)
            global_prompt_hash = str(
                getattr(getattr(self, "prompt_provenance", None), "global_prompt_set_hash", "") or ""
            )
            identity = hashlib.sha256(
                (
                    f"improve\n{project}\n{mr_iid}\n{refs['head_sha']}\n"
                    f"{getattr(effective, 'skill_hash', '')}\n{global_prompt_hash}"
                ).encode("utf-8")
            ).hexdigest()[:24]
            mr = getattr(self.git_provider, "mr", None)
            frozen_input = {
                "title": self.vars.get("title"),
                "description": self.vars.get("description"),
                "commit_messages": self.vars.get("commit_messages_str"),
                "branch": self.vars.get("branch"),
                "related_tickets": get_settings().get("related_tickets", []),
                "target_branch": getattr(mr, "target_branch", None) if mr is not None else None,
                "source_branch": getattr(mr, "source_branch", None) if mr is not None else None,
            }
            skill_target_sha = str(getattr(effective, "target_sha", "") or "")
            skill_content = ""
            if skill_target_sha and hasattr(self.git_provider, "get_file_content_at_ref"):
                try:
                    skill_content = str(
                        self.git_provider.get_file_content_at_ref(
                            PROJECT_SKILL_MANIFEST_PATH,
                            skill_target_sha,
                        )
                        or ""
                    )
                except Exception:
                    skill_content = ""
            save_review_run({
                "review_id": f"improve-{identity}",
                "pr_url": str(
                    self.git_provider.get_pr_url()
                    if hasattr(self.git_provider, "get_pr_url")
                    else self.pr_url
                ),
                "provider": str(get_settings().get("config.git_provider", "") or ""),
                "project": project,
                "mr_iid": mr_iid,
                "base_sha": str(refs.get("base_sha") or ""),
                "head_sha": str(refs.get("head_sha") or ""),
                "start_sha": str(refs.get("start_sha") or refs.get("base_sha") or ""),
                "model": str(get_settings().get("config.model", "") or ""),
                "cfg": {
                    "command": "improve",
                    "project_skill_hash": str(getattr(effective, "skill_hash", "") or ""),
                    "large_mr_review_enabled": bool(
                        get_settings().get("large_mr_review.enabled", True)
                    ),
                },
                "input": {key: value for key, value in frozen_input.items() if value not in (None, "", [])},
                "extra": {
                    "capture_source": "improve_generation",
                    "project_skill_content": skill_content,
                    "project_skill_target_sha": skill_target_sha,
                },
            })
            self._evolution_review_id = f"improve-{identity}"
            update_review_run(review_id=self._evolution_review_id)
            self._record_project_skill_usage(self._evolution_review_id)
        except Exception as exc:
            get_logger().warning(f"Failed to capture improve replay snapshot: {exc}")

    @track_review_run("manual_improve")
    async def run(self):
        try:
            self._record_project_skill_usage(
                getattr(self, "_evolution_review_id", "")
                or get_current_run_id()
                or getattr(self, "pr_url", "")
            )
            if not self.git_provider.get_files():
                update_review_run(stage="skipped", status="skipped")
                get_logger().info(f"PR has no files: {self.pr_url}, skipping code suggestions")
                return None

            get_logger().info('Generating code suggestions for PR...')
            relevant_configs = {'pr_code_suggestions': dict(get_settings().pr_code_suggestions),
                                'config': dict(get_settings().config)}
            get_logger().debug("Relevant configs", artifacts=relevant_configs)

            # publish "Preparing suggestions..." comments
            if (get_settings().config.publish_output and get_settings().config.publish_output_progress and
                    not get_settings().config.get('is_auto_command', False)):
                if self.git_provider.is_supported("gfm_markdown"):
                    self.progress_response = self.git_provider.publish_comment(self.progress)
                else:
                    self.git_provider.publish_comment("Preparing suggestions...", is_temporary=True)

            data = await self.generate_suggestions_data()

            # Handle the case where the PR has no suggestions
            if (data is None or 'code_suggestions' not in data or not data['code_suggestions']):
                if self._publish_map_reduce_coverage_failure():
                    return
                await self.publish_no_suggestions()
                return

            # publish the suggestions
            if get_settings().config.publish_output:
                # If a temporary comment was published, remove it
                self.git_provider.remove_initial_comment()

                # Publish table summarized suggestions
                if ((not get_settings().pr_code_suggestions.commitable_code_suggestions) and
                        self.git_provider.is_supported("gfm_markdown")):

                    code_suggestions = data.get("code_suggestions", [])
                    pending_body = self.generate_pending_suggestions()
                    if get_settings().pr_code_suggestions.persistent_comment:
                        lang = str(get_settings().config.get("response_language", "en-US")).lower()
                        initial_hdr = "## PR 代码建议 ✨" if lang.startswith("zh") else "## PR Code Suggestions ✨"
                        published_comment, published_body = self.publish_persistent_comment_with_history(
                            self.git_provider,
                            pending_body,
                            initial_header=initial_hdr,
                            update_header=True,
                            name="suggestions",
                            final_update_message=False,
                            max_previous_comments=get_settings().pr_code_suggestions.max_history_len,
                            progress_response=self.progress_response,
                            publish_as_new_comment=True)
                    else:
                        if self.progress_response:
                            self.git_provider.remove_comment(self.progress_response)
                        published_comment = self.git_provider.publish_comment(pending_body)
                        published_body = pending_body

                    inline_summary = await self._publish_inline_suggestions_for_improve(code_suggestions)
                    published_locations = (inline_summary or {}).get("published_locations") or []
                    published_suggestions = inline_publisher.backfill_note_urls(
                        code_suggestions, published_locations)
                    data["code_suggestions"] = published_suggestions
                    self.data = data

                    # generate summarized suggestions
                    pr_body = self.generate_summarized_suggestions(data)
                    get_logger().debug(f"PR output", artifact=pr_body)

                    # If summarization failed (returned empty), fall back to the
                    # "no suggestions" path so we never publish/edit an empty
                    # body (GitLab rejects empty bodies with HTTP 400).
                    if not pr_body or not pr_body.strip():
                        get_logger().warning(
                            "generate_summarized_suggestions returned empty body; falling back to no-suggestions output")
                        await self.publish_no_suggestions()
                        return

                    # require self-review
                    if get_settings().pr_code_suggestions.demand_code_suggestions_self_review:
                        pr_body = await self.add_self_review_text(pr_body)

                    # add usage guide
                    if (get_settings().pr_code_suggestions.enable_chat_text and get_settings().config.is_auto_command
                            and isinstance(self.git_provider, GithubProvider)):
                        pr_body += "\n\n>💡 Need additional feedback ? start a [PR chat](https://chromewebstore.google.com/detail/ephlnjeghhogofkifjloamocljapahnl) \n\n"
                    if get_settings().pr_code_suggestions.enable_help_text:
                        pr_body += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide:</strong></summary><hr> \n\n"
                        pr_body += HelpMessage.get_improve_usage_guide()
                        pr_body += "\n</details>\n"

                    # Output the relevant configurations if enabled
                    if get_settings().get('config', {}).get('output_relevant_configurations', False):
                        pr_body += show_relevant_configurations(relevant_section='pr_code_suggestions')

                    final_body = published_body.replace(pending_body.strip(), pr_body.strip())
                    if final_body == published_body:
                        get_logger().warning(
                            "Pending /improve summary was not found in its published comment; leaving it unchanged")
                    else:
                        self.git_provider.edit_comment(published_comment, final_body)
                    comment_state = {"comment": published_comment, "body": final_body, "improve_md": pr_body}

                    # Pipeline v2 Tier-2: fire-and-forget heavy repair for whatever
                    # deterministic_fix/tier1_repair could not resolve, mirroring
                    # mr_create's flow (see PRMrCreate.run). No-op when
                    # tier2_enabled is false (default) or nothing is pending.
                    async def _on_tier2_complete(tier2_result):
                        await self._refresh_improve_comment_after_tier2(comment_state, tier2_result)

                    schedule_tier2(self.git_provider, self.pending_tier2_tasks,
                                   on_complete=_on_tier2_complete, source="improve_command",
                                   prompt_provenance=getattr(self, "prompt_provenance", None))

                    # dual publishing mode
                    if int(get_settings().pr_code_suggestions.dual_publishing_score_threshold) > 0:
                        await self.dual_publishing(data)
                else:
                    await self.push_inline_code_suggestions(data)
                    if self.progress_response:
                        self.git_provider.remove_comment(self.progress_response)
            else:
                get_logger().info('Code suggestions generated for PR, but not published since publish_output is False.')
                pr_body = self.generate_summarized_suggestions(data)
                get_settings().data = {"artifact": pr_body,
                                       "code_suggestions": data.get("code_suggestions", [])}
                return
        except Exception as e:
            update_review_run(stage="failed", status="failed", error_code=type(e).__name__, error_message=str(e))
            get_settings().data = {"artifact": "", "code_suggestions": [], "error": str(e)[:300]}
            get_logger().error(f"Failed to generate code suggestions for PR, error: {e}",
                               artifact={"traceback": traceback.format_exc()})
            if get_settings().config.publish_output:
                if self.progress_response:
                    self.progress_response.delete()
                else:
                    try:
                        self.git_provider.remove_initial_comment()
                        self.git_provider.publish_comment(f"Failed to generate code suggestions for PR")
                    except Exception as e:
                        get_logger().exception(f"Failed to update persistent review, error: {e}")

    def _record_project_skill_usage(self, review_id: str) -> None:
        try:
            from pr_agent.feedback.store import save_project_skill_usage
            from pr_agent.suggestions.prompt_provenance import build_project_skill_usage_identity

            effective = self.project_skill_effective
            global_hash, bundle_hash = build_project_skill_usage_identity(effective, "improve")
            save_project_skill_usage({
                "review_id": review_id,
                "command": "improve",
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
            get_logger().warning(f"Failed to record improve project Skill usage: {exc}")

    @staticmethod
    def _extract_first_non_empty_line(text: str) -> str:
        for line in (text or "").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return ""

    @classmethod
    def _local_trigger_reason(cls, suggestion: dict) -> str | None:
        content = str(suggestion.get("suggestion_content", "") or "")
        first = cls._extract_first_non_empty_line(content)
        if not first or not first.startswith(_TRIGGER_PREFIXES):
            return "scenario_missing_prefix"

        lowered = first.lower()
        for pat in _TRIGGER_VAGUE_PATTERNS:
            if re.search(pat, lowered, re.IGNORECASE):
                return "scenario_vague"

        # Require quantification hints in trigger line to avoid fuzzy wording.
        if not any(re.search(pat, first, re.IGNORECASE) for pat in _TRIGGER_QUANTIFIED_HINTS):
            return "scenario_not_quantified"
        return None

    @staticmethod
    def _parse_json_object(text: str) -> dict:
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

    async def _run_scenario_validator(self, candidates: List[dict]) -> dict:
        candidate_files = tuple(
            dict.fromkeys(str(candidate.get("relevant_file") or "") for candidate in candidates)
        )
        variables = {
            "suggestions_json": json.dumps(candidates, ensure_ascii=False, indent=2),
            "project_prompt_rules": self._project_skill_context(
                "scenario_validator", self._improve_rule_languages, candidate_files,
            ),
        }
        env = Environment(undefined=StrictUndefined)
        system = env.from_string(
            get_settings().pr_code_suggestions_scenario_validator_prompt.system
        ).render(variables)
        user = env.from_string(
            get_settings().pr_code_suggestions_scenario_validator_prompt.user
        ).render(variables)
        user = append_project_skill_context(
            user,
            self._project_skill_effective("scenario_validator", self._improve_rule_languages, candidate_files),
        )

        model = (
            get_settings().get("pr_code_suggestions.scenario_validation_model", "")
            or get_settings().config.get("model_weak", "")
            or get_settings().config.model
        )
        response, _ = await self.ai_handler.chat_completion(
            model=model,
            system=system,
            user=user,
            temperature=0.1,
        )
        return self._parse_json_object(response)

    async def validate_suggestions_scenario_constraints(self, data: dict) -> dict:
        if not bool(get_settings().get("pr_code_suggestions.scenario_validation_enabled", True)):
            return data

        suggestions = list((data or {}).get("code_suggestions") or [])
        if not suggestions:
            return data

        kept = []
        blocked_reasons = []
        llm_candidates = []
        llm_source_indexes = []

        try:
            max_candidates = int(get_settings().get("pr_code_suggestions.scenario_validation_max_candidates", 30))
        except Exception:
            max_candidates = 30

        for idx, suggestion in enumerate(suggestions):
            local_reason = self._local_trigger_reason(suggestion)
            if local_reason:
                blocked_reasons.append((idx, local_reason))
                continue
            if len(llm_candidates) < max_candidates:
                llm_source_indexes.append(idx)
                llm_candidates.append({
                    "index": len(llm_candidates) + 1,
                    "one_sentence_summary": suggestion.get("one_sentence_summary", ""),
                    "label": suggestion.get("label", ""),
                    "score": suggestion.get("score", ""),
                    "suggestion_content": suggestion.get("suggestion_content", ""),
                })
            else:
                # Above cost cap: keep only local-strict-checked suggestions.
                kept.append(suggestion)

        fail_action = str(get_settings().get("pr_code_suggestions.scenario_validation_fail_action", "skip")).lower()
        if llm_candidates:
            try:
                result = await self._run_scenario_validator(llm_candidates)
                rows = result.get("results") or []
                by_idx = {int(r.get("index")): r for r in rows if str(r.get("index", "")).isdigit()}
                for local_pos, src_idx in enumerate(llm_source_indexes, start=1):
                    row = by_idx.get(local_pos)
                    if not row:
                        blocked_reasons.append((src_idx, "scenario_validator_missing_result"))
                        continue
                    if bool(row.get("valid", False)):
                        kept.append(suggestions[src_idx])
                    else:
                        reason = "scenario_invalid"
                        if bool(row.get("missing_specific_trigger", False)):
                            reason = "scenario_missing_specific"
                        elif bool(row.get("extreme_trigger", False)):
                            reason = "scenario_extreme"
                        blocked_reasons.append((src_idx, reason))
            except Exception as e:
                get_logger().warning(f"Scenario validator failed: {e}")
                if fail_action == "pass":
                    for src_idx in llm_source_indexes:
                        kept.append(suggestions[src_idx])
                else:
                    for src_idx in llm_source_indexes:
                        blocked_reasons.append((src_idx, "scenario_validator_error"))

        if blocked_reasons:
            blocked_reasons = sorted(blocked_reasons, key=lambda x: x[0])
            get_logger().info(
                "Scenario validation filtered improve suggestions",
                artifact={
                    "total": len(suggestions),
                    "kept": len(kept),
                    "blocked": [
                        {"index": i + 1, "reason": reason}
                        for i, reason in blocked_reasons
                    ],
                },
            )

        # Persist cross-review-filtered suggestions for the dashboard. Never
        # raises; a storage failure only logs and does not break /improve.
        if blocked_reasons and bool(
            get_settings().get("pr_code_suggestions.filter_persistence_enabled", True)
        ):
            try:
                ctx = self._filter_mr_context()
                judge_model = str(
                    get_settings().get("pr_code_suggestions.scenario_validation_model", "")
                    or get_settings().config.get("model_weak", "")
                    or get_settings().config.model
                )
                for idx, reason in blocked_reasons:
                    sugg = suggestions[idx]
                    record = {
                        "review_id": getattr(self, "pr_url", "") or ctx.get("mr_url", ""),
                        "project": ctx["project"],
                        "mr_iid": ctx["mr_iid"],
                        "mr_url": ctx["mr_url"],
                        "mr_author": ctx["mr_author"],
                        "commit_sha": ctx["commit_sha"],
                        "file_path": str(sugg.get("relevant_file", "") or "").strip(),
                        "line_start": sugg.get("relevant_lines_start"),
                        "line_end": sugg.get("relevant_lines_end"),
                        "label": str(sugg.get("label", "") or "").strip(),
                        "severity": "",
                        "score": sugg.get("score"),
                        "one_sentence_summary": str(
                            sugg.get("one_sentence_summary", "") or ""
                        ).strip(),
                        "suggestion_content": str(
                            sugg.get("suggestion_content", "") or ""
                        ),
                        "existing_code": sugg.get("existing_code"),
                        "improved_code": sugg.get("improved_code"),
                        "filter_stage": "scenario_validation",
                        "skip_reason": reason,
                        "judge_model": judge_model,
                    }
                    try:
                        save_filtered_suggestion(record)
                    except Exception as pe:
                        get_logger().warning(
                            f"filter persistence failed for suggestion {idx}: {pe}"
                        )
            except Exception as e:
                get_logger().warning(f"filter persistence block failed: {e}")

        data["code_suggestions"] = kept
        return data

    async def add_self_review_text(self, pr_body):
        text = get_settings().pr_code_suggestions.code_suggestions_self_review_text
        pr_body += f"\n\n- [ ]  {text}"
        approve_pr_on_self_review = get_settings().pr_code_suggestions.approve_pr_on_self_review
        fold_suggestions_on_self_review = get_settings().pr_code_suggestions.fold_suggestions_on_self_review
        if approve_pr_on_self_review and not fold_suggestions_on_self_review:
            pr_body += ' <!-- approve pr self-review -->'
        elif fold_suggestions_on_self_review and not approve_pr_on_self_review:
            pr_body += ' <!-- fold suggestions self-review -->'
        else:
            pr_body += ' <!-- approve and fold suggestions self-review -->'
        return pr_body

    def no_suggestions_markdown(self) -> str:
        lang = str(get_settings().config.get("response_language", "en-US")).lower()
        if lang.startswith("zh"):
            return "## PR 代码建议 ✨\n\n未发现可改进建议。"
        return "## PR Code Suggestions ✨\n\nNo suggestions found to improve this PR."

    async def publish_no_suggestions(self):
        pr_body = self.no_suggestions_markdown()
        get_settings().data = {"artifact": pr_body, "code_suggestions": []}
        if (get_settings().config.publish_output and
                get_settings().pr_code_suggestions.get('publish_output_no_suggestions', True)):
            get_logger().warning('No code suggestions found for the PR.')
            get_logger().debug(f"PR output", artifact=pr_body)
            if self.progress_response:
                self.git_provider.edit_comment(self.progress_response, body=pr_body)
            else:
                self.git_provider.publish_comment(pr_body)

    async def _publish_inline_suggestions_for_improve(self, code_suggestions) -> dict:
        """Publish inline suggestions for a manually-run /improve command.

        Mirrors PRMrCreate._publish_inline_suggestions but tags the call with
        source="improve_command" so it's gated by its own independent switch
        (pr_code_suggestions.inline_suggestions_on_improve_command) instead of
        the mr_create one. Never raises.
        """
        try:
            return await inline_publisher.publish_inline_suggestions_async(
                self.git_provider, code_suggestions, source="improve_command",
                prompt_provenance=getattr(self, "prompt_provenance", None))
        except Exception as e:
            get_logger().exception(f"inline suggestion publishing failed: {e}")
            return {}

    async def _refresh_improve_comment_after_tier2(self, comment_state: dict, tier2_result: dict) -> None:
        """Re-render the /improve summary table to include suggestions Tier-2
        resolved asynchronously (after the main comment was already
        published), then edit the comment in place so the table and the
        inline suggestions never drift out of sync. Mirrors
        PRMrCreate._refresh_improve_table. Never raises; any failure just
        leaves the original (slightly stale) table as-is.
        """
        try:
            comment = comment_state.get("comment")
            improve_md = comment_state.get("improve_md")
            original_body = comment_state.get("body")
            one_click = list((tier2_result or {}).get("one_click") or [])
            new_suggestions = [suggestion for suggestion in one_click if suggestion.get("inline_note_url")]
            if comment is None or not improve_md or not new_suggestions or original_body is None:
                return
            existing = list((self.data or {}).get("code_suggestions", []) or [])
            merged = existing + new_suggestions
            updated_improve_md = self.generate_summarized_suggestions({"code_suggestions": merged})
            if not updated_improve_md or not updated_improve_md.strip():
                return
            updated_body = original_body.replace(improve_md.strip(), updated_improve_md.strip())
            if updated_body == original_body:
                get_logger().warning(
                    "Tier-2 table refresh (/improve): improve section not found verbatim in "
                    "the comment body; skipping edit to avoid publishing a no-op or corrupted comment")
                return
            self.git_provider.edit_comment(comment, updated_body)
            comment_state["body"] = updated_body
            comment_state["improve_md"] = updated_improve_md
            get_logger().info(
                f"Tier-2 refreshed the /improve summary table with {len(new_suggestions)} additional suggestion(s) "
                f"({len(one_click)} one-click candidates)")
        except Exception as e:
            get_logger().exception(f"Tier-2 table refresh for manual /improve failed: {e}")

    async def dual_publishing(self, data):
        data_above_threshold = {'code_suggestions': []}
        try:
            for suggestion in data['code_suggestions']:
                if int(suggestion.get('score', 0)) >= int(
                        get_settings().pr_code_suggestions.dual_publishing_score_threshold) \
                        and suggestion.get('improved_code'):
                    data_above_threshold['code_suggestions'].append(suggestion)
                    if not data_above_threshold['code_suggestions'][-1]['existing_code']:
                        get_logger().info(f'Identical existing and improved code for dual publishing found')
                        data_above_threshold['code_suggestions'][-1]['existing_code'] = suggestion[
                            'improved_code']
            if data_above_threshold['code_suggestions']:
                get_logger().info(
                    f"Publishing {len(data_above_threshold['code_suggestions'])} suggestions in dual publishing mode")
                await self.push_inline_code_suggestions(data_above_threshold)
        except Exception as e:
            get_logger().error(f"Failed to publish dual publishing suggestions, error: {e}")

    @staticmethod
    def publish_persistent_comment_with_history(git_provider: GitProvider,
                                                pr_comment: str,
                                                initial_header: str,
                                                update_header: bool = True,
                                                name='review',
                                                final_update_message=True,
                                                max_previous_comments=4,
                                                progress_response=None,
                                                only_fold=False,
                                                publish_as_new_comment=False):
        """Publish (or edit) the persistent /improve summary comment.

        `publish_as_new_comment`: when True, never edit an existing comment
        in place (neither `progress_response` nor a previous run's persisted
        comment) -- always delete whichever of those exist and publish a
        brand new comment instead. This matters for manual /improve, which
        publishes inline suggestions AFTER the "Preparing suggestions..."
        placeholder (`progress_response`) was already created, and, on repeat
        runs, after the previous run's summary comment was already created.
        Editing either one in place would keep the final summary comment
        pinned at that earlier position in the MR timeline -- BEFORE this
        run's inline suggestion comments, which is the wrong order. Deleting
        and republishing puts the summary at its true, current position
        (after inline suggestions), every run. Defaults to False so
        mr_create-style callers keep their prior edit-in-place behavior.

        Returns a (comment, final_body) tuple: `comment` is the GitLab note
        object that now holds the content, and `final_body` is the exact body
        string that was sent to it (including any "previous suggestions"
        history wrapping) -- callers that need to edit this same comment
        again later (e.g. to backfill Tier-2 results) use `final_body` to
        locate the current raw table substring via string replacement,
        without re-invoking this function (which would misfile the
        surrounding revision into "previous suggestions" a second time).
        """

        def _extract_link(comment_text: str):
            r = re.compile(r"<!--.*?-->")
            match = r.search(comment_text)

            up_to_commit_txt = ""
            if match:
                up_to_commit_txt = f" up to commit {match.group(0)[4:-3].strip()}"
            return up_to_commit_txt

        lang = str(get_settings().config.get("response_language", "en-US")).lower()
        is_zh = lang.startswith("zh")
        history_header = "#### 历史建议\n" if is_zh else "#### Previous suggestions\n"
        last_commit_num = git_provider.get_latest_commit_url().split('/')[-1][:7]
        if only_fold: # A user clicked on the 'self-review' checkbox
            text = get_settings().pr_code_suggestions.code_suggestions_self_review_text
            latest_suggestion_header = f"\n\n- [x]  {text}"
        else:
            latest_suggestion_header = f"最新建议截至 {last_commit_num}" if is_zh else f"Latest suggestions up to {last_commit_num}"
        latest_commit_html_comment = f"<!-- {last_commit_num} -->"
        found_comment = None

        if max_previous_comments > 0:
            try:
                prev_comments = list(git_provider.get_issue_comments())
                for comment in prev_comments:
                    if comment.body.startswith(initial_header):
                        prev_suggestions = comment.body
                        found_comment = comment
                        comment_url = git_provider.get_comment_url(comment)

                        if history_header.strip() not in comment.body:
                            # no history section
                            # extract everything between <table ...> and </table> in comment.body including both
                            # (searches for the "<table" prefix, not the exact "<table>" tag, since the
                            # opening tag may carry a width style attribute -- see the pr_body += line above)
                            table_index = comment.body.find("<table")
                            if table_index == -1:
                                git_provider.edit_comment(comment, pr_comment)
                                continue
                            # find http link from comment.body[:table_index]
                            up_to_commit_txt = _extract_link(comment.body[:table_index])
                            prev_suggestion_table = comment.body[
                                                    table_index:comment.body.rfind("</table>") + len("</table>")]

                            tick = "✅ " if "✅" in prev_suggestion_table else ""
                            # surround with details tag
                            prev_suggestion_table = f"<details><summary>{tick}{name.capitalize()}{up_to_commit_txt}</summary>\n<br>{prev_suggestion_table}\n\n</details>"

                            new_suggestion_table = pr_comment.replace(initial_header, "").strip()

                            pr_comment_updated = f"{initial_header}\n{latest_commit_html_comment}\n\n"
                            pr_comment_updated += f"{latest_suggestion_header}\n{new_suggestion_table}\n\n___\n\n"
                            pr_comment_updated += f"{history_header}{prev_suggestion_table}\n"
                        else:
                            # get the text of the previous suggestions until the latest commit
                            sections = prev_suggestions.split(history_header.strip())
                            latest_table = sections[0].strip()
                            prev_suggestion_table = sections[1].replace(history_header, "").strip()

                            # get text after the latest_suggestion_header in comment.body
                            # (searches for the "<table" prefix -- see comment above)
                            table_ind = latest_table.find("<table")
                            up_to_commit_txt = _extract_link(latest_table[:table_ind])

                            latest_table = latest_table[table_ind:latest_table.rfind("</table>") + len("</table>")]
                            # enforce max_previous_comments
                            count = prev_suggestions.count(f"\n<details><summary>{name.capitalize()}")
                            count += prev_suggestions.count(f"\n<details><summary>✅ {name.capitalize()}")
                            if count >= max_previous_comments:
                                # remove the oldest suggestion
                                prev_suggestion_table = prev_suggestion_table[:prev_suggestion_table.rfind(
                                    f"<details><summary>{name.capitalize()} up to commit")]

                            tick = "✅ " if "✅" in latest_table else ""
                            # Add to the prev_suggestions section
                            last_prev_table = f"\n<details><summary>{tick}{name.capitalize()}{up_to_commit_txt}</summary>\n<br>{latest_table}\n\n</details>"
                            prev_suggestion_table = last_prev_table + "\n" + prev_suggestion_table

                            new_suggestion_table = pr_comment.replace(initial_header, "").strip()

                            pr_comment_updated = f"{initial_header}\n"
                            pr_comment_updated += f"{latest_commit_html_comment}\n\n"
                            pr_comment_updated += f"{latest_suggestion_header}\n\n{new_suggestion_table}\n\n"
                            pr_comment_updated += "___\n\n"
                            pr_comment_updated += f"{history_header}\n"
                            pr_comment_updated += f"{prev_suggestion_table}\n"

                        get_logger().info(f"Persistent mode - updating comment {comment_url} to latest {name} message")
                        if publish_as_new_comment:
                            # Always republish at the current position (see
                            # docstring): delete whichever old comment(s)
                            # exist, then publish a fresh one.
                            if progress_response and progress_response.id != comment.id:
                                git_provider.remove_comment(progress_response)
                            git_provider.remove_comment(comment)
                            new_comment = git_provider.publish_comment(pr_comment_updated)
                            return new_comment, pr_comment_updated
                        if progress_response:  # publish to 'progress_response' comment, because it refreshes immediately
                            git_provider.edit_comment(progress_response, pr_comment_updated)
                            git_provider.remove_comment(comment)
                            comment = progress_response
                        else:
                            git_provider.edit_comment(comment, pr_comment_updated)
                        return comment, pr_comment_updated
            except Exception as e:
                get_logger().exception(f"Failed to update persistent review, error: {e}")
                pass

        # if we are here, we did not find a previous comment to update
        body = pr_comment.replace(initial_header, "").strip()
        pr_comment = f"{initial_header}\n\n{latest_commit_html_comment}\n\n{body}\n\n"
        if publish_as_new_comment:
            # Same reasoning as the "found" branch above: don't reuse
            # progress_response (created before inline suggestions were
            # published) -- delete it and publish fresh so this comment's
            # position reflects when it was actually finalized.
            if progress_response:
                git_provider.remove_comment(progress_response)
            new_comment = git_provider.publish_comment(pr_comment)
        elif progress_response:
            git_provider.edit_comment(progress_response, pr_comment)
            new_comment = progress_response
        else:
            new_comment = git_provider.publish_comment(pr_comment)
        return new_comment, pr_comment


    def extract_link(self, s):
        r = re.compile(r"<!--.*?-->")
        match = r.search(s)

        up_to_commit_txt = ""
        if match:
            up_to_commit_txt = f" up to commit {match.group(0)[4:-3].strip()}"
        return up_to_commit_txt

    async def _prepare_prediction(self, model: str) -> dict:
        self.patches_diff = get_pr_diff(self.git_provider,
                                        self.token_handler,
                                        model,
                                        add_line_numbers_to_hunks=True,
                                        disable_extra_lines=False)
        self.patches_diff_list = [self.patches_diff]
        self.patches_diff_no_line_number = self.remove_line_numbers([self.patches_diff])[0]

        if self.patches_diff:
            get_logger().debug(f"PR diff", artifact=self.patches_diff)
            self.prediction = await self._get_prediction(model, self.patches_diff, self.patches_diff_no_line_number)
        else:
            get_logger().warning(f"Empty PR diff")
            self.prediction = None

        data = self.prediction
        return data

    async def _get_prediction(self, model: str, patches_diff: str, patches_diff_no_line_number: str) -> dict:
        variables = copy.deepcopy(self.vars)
        variables["diff"] = patches_diff  # update diff
        variables["diff_no_line_numbers"] = patches_diff_no_line_number  # update diff
        variables["related_files_context"] = getattr(self, "related_files_context", "")
        environment = Environment(undefined=StrictUndefined)

        # Resolve self-reflect model up front (shared by all pairs)
        model_reflect_with_reasoning = get_model('model_reasoning')
        fallbacks = get_settings().config.fallback_models
        if model_reflect_with_reasoning == get_settings().config.model and model != get_settings().config.model and fallbacks and model == \
                fallbacks[0]:
            get_logger().warning(f"Using the same model for self-reflection as the one used for suggestions")
            model_reflect_with_reasoning = model

        # Iterate over all prompt pairs (1 for pure lang, 2 for mixed):
        # for EACH pair, make the LLM call and immediately self-reflect on its
        # own suggestions so that the reflect feedback length matches.
        # Finally merge all suggestions into a single list.
        all_suggestions = []
        for pair_idx, (sys_tmpl, usr_tmpl) in enumerate(self._improve_prompt_pairs):
            pair_variables = copy.deepcopy(variables)
            pair_effective = self._project_skill_effective(
                "generation", self._improve_prompt_languages[pair_idx], self._changed_files,
            )
            pair_variables["project_prompt_rules"] = (
                pair_effective.render_context() if project_skill_should_inject("generation") else ""
            )
            system_prompt = environment.from_string(sys_tmpl).render(pair_variables)
            user_prompt = environment.from_string(usr_tmpl).render(pair_variables)
            user_prompt = append_project_skill_context(user_prompt, pair_effective)
            response, finish_reason = await self.ai_handler.chat_completion(
                model=model, temperature=get_settings().config.temperature, system=system_prompt, user=user_prompt)
            if not get_settings().config.publish_output:
                get_settings().system_prompt = system_prompt
                get_settings().user_prompt = user_prompt

            # load suggestions from this pair's AI response
            try:
                partial_data = self._prepare_pr_code_suggestions(response)
            except Exception as e:
                get_logger().error(f"Failed to parse suggestions for prompt pair {pair_idx}, error: {e}")
                continue
            if not partial_data or "code_suggestions" not in partial_data or not partial_data["code_suggestions"]:
                continue

            # self-reflect on THIS pair's suggestions (length-matching is critical
            # because analyze_self_reflection_response requires equal lengths).
            # Scoped to allowlisted projects so the extra LLM call and score/line
            # assignment only affect the gray-rollout project(s); other projects
            # keep the prior default-score behavior.
            if inline_publisher.self_reflect_allowed(self.git_provider):
                pipeline_v2 = bool(get_settings().get("pr_code_suggestions.pipeline_v2_enabled", False))
                response_reflect = await self.self_reflect_on_suggestions(
                    partial_data["code_suggestions"], patches_diff, model=model_reflect_with_reasoning,
                    dedicated_prompt="pr_code_suggestions_reflect_prompt_v2" if pipeline_v2 else "",
                    project_rule_languages=self._improve_prompt_languages[pair_idx],
                )
            else:
                response_reflect = ""
            if response_reflect:
                await self.analyze_self_reflection_response(partial_data, response_reflect, patches_diff)
            else:
                for suggestion in partial_data["code_suggestions"]:
                    if self._normalize_score(suggestion.get("score")) is None:
                        suggestion["score"] = 7
                    suggestion["score_why"] = ""
                    suggestion.setdefault("self_contained", True)
                    suggestion.setdefault("structural_issue", "none")
                    suggestion.setdefault("companion_file", "")

            # Ensure every suggestion has relevant_lines_start/end before merging.
            # When self-reflect fails or returns partial data, these keys may be
            # missing — which causes downstream filtering to drop the suggestion.
            for suggestion in partial_data["code_suggestions"]:
                if 'relevant_lines_start' not in suggestion or suggestion.get('relevant_lines_start') in (None, ""):
                    suggestion['relevant_lines_start'] = -1
                if 'relevant_lines_end' not in suggestion or suggestion.get('relevant_lines_end') in (None, ""):
                    suggestion['relevant_lines_end'] = -1

            all_suggestions.extend(partial_data["code_suggestions"])

        data = {"code_suggestions": all_suggestions}
        return data

    async def analyze_self_reflection_response(self, data, response_reflect, patches_diff: str = ""):
        get_logger().debug(f"Self-reflection raw response: {response_reflect[:500]}")  # first 500 chars
        response_reflect_yaml = load_yaml(response_reflect)
        get_logger().debug(f"Self-reflection parsed YAML: {response_reflect_yaml}")
        code_suggestions_feedback, matched_key = _extract_reflect_feedback_list(response_reflect_yaml)
        # Guarantee the 3 Pipeline-v2 structural fields exist on every suggestion
        # up front: when the feedback list is completely empty (e.g. a
        # `code_suggestions: []` response), the per-suggestion loop below never
        # runs at all, so these keys would otherwise be entirely absent rather
        # than defaulted. Values legitimately parsed from feedback later in this
        # method overwrite these defaults (not merely `.setdefault`-guarded).
        for suggestion in data.get("code_suggestions", []):
            suggestion.setdefault("self_contained", True)
            suggestion.setdefault("structural_issue", "none")
            suggestion.setdefault("companion_file", "")
        if matched_key and matched_key != "code_suggestions":
            get_logger().warning(
                f"Self-reflection used non-standard top-level key '{matched_key}' instead of "
                f"'code_suggestions'; recovered feedback via alias matching")
        elif response_reflect_yaml and not code_suggestions_feedback:
            get_logger().warning(
                "Self-reflection response did not match any known feedback schema "
                f"(top-level keys: {list(response_reflect_yaml.keys())}); all suggestions in this "
                "batch will fall back to relevant_lines_start/end=-1 and be skipped as invalid_lines")
        get_logger().debug(f"Self-reflection feedback count: {len(code_suggestions_feedback)}")
        # Tolerate length mismatch: try to match feedback to suggestions by
        # `suggestion_summary` (one_sentence_summary). If matching fails or
        # feedback is missing, fall back to a neutral default (score=7) so
        # the suggestion is still renderable. Previously a strict
        # `len(feedback) == len(suggestions)` check silently skipped ALL
        # assignments, leaving suggestions without `score`/`relevant_lines_*`
        # which then broke downstream rendering and produced an empty body.
        feedback_by_summary = {}
        for fb in code_suggestions_feedback or []:
            key = str(fb.get("suggestion_summary", "")).strip()
            if key and key not in feedback_by_summary:
                feedback_by_summary[key] = fb

        equal_length = bool(code_suggestions_feedback) and len(code_suggestions_feedback) == len(data["code_suggestions"])

        if code_suggestions_feedback and not equal_length:
            get_logger().warning(
                f"Self-reflection length mismatch: {len(code_suggestions_feedback)} feedback vs "
                f"{len(data['code_suggestions'])} suggestions; falling back to summary-based match")

        if code_suggestions_feedback:
            for i, suggestion in enumerate(data["code_suggestions"]):
                # pick feedback by index when length matches; else by summary; else None
                if equal_length:
                    fb = code_suggestions_feedback[i]
                else:
                    summary_key = str(suggestion.get("one_sentence_summary", "")).strip()
                    fb = feedback_by_summary.get(summary_key)

                try:
                    if fb is None:
                        # No matching feedback: keep suggestion renderable with
                        # neutral score and unknown line range (-1 → score=0
                        # → filtered out downstream, which is the desired
                        # behavior since we cannot locate the code).
                        if self._normalize_score(suggestion.get("score")) is None:
                            suggestion["score"] = 7
                        suggestion.setdefault("score_why", "")
                        if 'relevant_lines_start' not in suggestion:
                            suggestion['relevant_lines_start'] = -1
                            suggestion['relevant_lines_end'] = -1
                            suggestion["score"] = 0
                        continue

                    score_from_feedback = fb.get("suggestion_score", fb.get("score"))
                    normalized_feedback_score = self._normalize_score(score_from_feedback)
                    if normalized_feedback_score is not None:
                        suggestion["score"] = normalized_feedback_score
                    elif self._normalize_score(suggestion.get("score")) is None:
                        suggestion["score"] = 7
                    suggestion["score_why"] = fb.get("why", "")
                    suggestion["self_contained"] = self._as_bool(fb.get("self_contained", True))
                    suggestion["structural_issue"] = str(fb.get("structural_issue", "none") or "none").strip().lower()
                    suggestion["companion_file"] = str(fb.get("companion_file", "") or "").strip()
                    get_logger().debug(f"Suggestion {i+1} score filled: score={suggestion['score']}, feedback_keys={list(fb.keys())}")

                    if 'relevant_lines_start' not in suggestion:
                        relevant_lines_start = fb.get('relevant_lines_start', -1)
                        relevant_lines_end = fb.get('relevant_lines_end', -1)
                        suggestion['relevant_lines_start'] = relevant_lines_start
                        suggestion['relevant_lines_end'] = relevant_lines_end
                        if relevant_lines_start < 0 or relevant_lines_end < 0:
                            suggestion["score"] = 0

                    try:
                        if get_settings().config.publish_output:
                            if not suggestion["score"]:
                                score = -1
                            else:
                                score = int(suggestion["score"])
                            label = suggestion["label"].lower().strip()
                            label = label.replace('<br>', ' ')
                            suggestion_statistics_dict = {'score': score,
                                                          'label': label}
                            get_logger().info(f"PR-Agent suggestions statistics",
                                              statistics=suggestion_statistics_dict, analytics=True)
                    except Exception as e:
                        get_logger().error(f"Failed to log suggestion statistics, error: {e}")
                        pass

                except Exception as e:  #
                    get_logger().error(f"Error processing suggestion score {i}",
                                       artifact={"suggestion": suggestion,
                                                 "code_suggestions_feedback": fb})
                    suggestion["score"] = 7
                    suggestion["score_why"] = ""

                suggestion = self.validate_one_liner_suggestion_not_repeating_code(suggestion)

                # if the before and after code is the same, clear one of them
                try:
                    if suggestion['existing_code'] == suggestion['improved_code']:
                        get_logger().debug(
                            f"edited improved suggestion {i + 1}, because equal to existing code: {suggestion['existing_code']}")
                        if get_settings().pr_code_suggestions.commitable_code_suggestions:
                            suggestion['improved_code'] = ""  # we need 'existing_code' to locate the code in the PR
                        else:
                            suggestion['existing_code'] = ""
                except Exception as e:
                    get_logger().error(f"Error processing suggestion {i + 1}, error: {e}")

        # Deterministic override: the self-reflect LLM call above is only
        # asked to *guess* relevant_lines_start/end from the diff text: that
        # guess can land outside this PR's actual changes. Replace it with a
        # plain-text match restricted to the diff's own __new hunk__ lines --
        # if the suggestion's existing_code can't be found there, drop the
        # suggestion (score=0) rather than publish/display an unverifiable
        # location. Skipped entirely when patches_diff isn't provided, to
        # keep existing callers' behavior unchanged.
        if patches_diff:
            for suggestion in data.get("code_suggestions", []):
                relevant_file = str(suggestion.get("relevant_file", "") or "").strip()
                existing_code = str(suggestion.get("existing_code", "") or "")
                matched = find_lines_in_new_hunk(patches_diff, relevant_file, existing_code)
                if matched is not None:
                    suggestion["relevant_lines_start"], suggestion["relevant_lines_end"] = matched
                else:
                    suggestion["relevant_lines_start"] = -1
                    suggestion["relevant_lines_end"] = -1
                    suggestion["score"] = 0

    @staticmethod
    def _truncate_if_needed(suggestion):
        max_code_suggestion_length = get_settings().get("PR_CODE_SUGGESTIONS.MAX_CODE_SUGGESTION_LENGTH", 0)
        suggestion_truncation_message = get_settings().get("PR_CODE_SUGGESTIONS.SUGGESTION_TRUNCATION_MESSAGE", "")
        if max_code_suggestion_length > 0:
            if len(suggestion['improved_code']) > max_code_suggestion_length:
                get_logger().info(f"Truncated suggestion from {len(suggestion['improved_code'])} "
                                  f"characters to {max_code_suggestion_length} characters")
                suggestion['improved_code'] = suggestion['improved_code'][:max_code_suggestion_length]
                suggestion['improved_code'] += f"\n{suggestion_truncation_message}"
        return suggestion

    def _prepare_pr_code_suggestions(self, predictions: str) -> Dict:
        data = load_yaml(predictions.strip(),
                         keys_fix_yaml=["relevant_file", "suggestion_content", "existing_code", "improved_code"],
                         first_key="code_suggestions", last_key="label")
        if isinstance(data, list):
            data = {'code_suggestions': data}
        if not isinstance(data, dict) or not isinstance(data.get("code_suggestions"), list):
            raise SuggestionOutputSchemaError("improve output must contain a code_suggestions array")

        # Strip trailing whitespace/newlines from all string fields.
        # YAML block scalars ('|') preserve trailing '\n' which breaks
        # downstream matching (e.g. self-reflect summary matching, file name
        # lookup in diff). Stripping here normalizes regardless of prompt style.
        for suggestion in data.get('code_suggestions', []):
            for key, val in suggestion.items():
                if isinstance(val, str):
                    suggestion[key] = val.strip()

            # Prefer score returned directly by improve prompts and keep
            # backward compatibility with legacy key names.
            raw_score = suggestion.get("score", suggestion.get("suggestion_score"))
            normalized_score = self._normalize_score(raw_score)
            if normalized_score is not None:
                suggestion["score"] = normalized_score

        # remove or edit invalid suggestions
        suggestion_list = []
        one_sentence_summary_list = []
        for i, suggestion in enumerate(data['code_suggestions']):
            try:
                needed_keys = ['one_sentence_summary', 'label', 'relevant_file']
                is_valid_keys = True
                for key in needed_keys:
                    if key not in suggestion:
                        is_valid_keys = False
                        get_logger().debug(
                            f"Skipping suggestion {i + 1}, because it does not contain '{key}':\n'{suggestion}")
                        break
                if not is_valid_keys:
                    continue

                if get_settings().get("pr_code_suggestions.focus_only_on_problems", False):
                    CRITICAL_LABEL = 'critical'
                    if CRITICAL_LABEL in suggestion['label'].lower(): # we want the published labels to be less declarative
                        suggestion['label'] = 'possible issue'

                if suggestion['one_sentence_summary'] in one_sentence_summary_list:
                    get_logger().debug(f"Skipping suggestion {i + 1}, because it is a duplicate: {suggestion}")
                    continue

                if 'const' in suggestion['suggestion_content'] and 'instead' in suggestion[
                    'suggestion_content'] and 'let' in suggestion['suggestion_content']:
                    get_logger().debug(
                        f"Skipping suggestion {i + 1}, because it uses 'const instead let': {suggestion}")
                    continue

                if ('existing_code' in suggestion) and ('improved_code' in suggestion):
                    suggestion = self._truncate_if_needed(suggestion)
                    one_sentence_summary_list.append(suggestion['one_sentence_summary'])
                    suggestion_list.append(suggestion)
                else:
                    get_logger().info(
                        f"Skipping suggestion {i + 1}, because it does not contain 'existing_code' or 'improved_code': {suggestion}")
            except Exception as e:
                get_logger().error(f"Error processing suggestion {i + 1}: {suggestion}, error: {e}")
        data['code_suggestions'] = suggestion_list

        return data

    async def push_inline_code_suggestions(self, data):
        code_suggestions = []

        if not data['code_suggestions']:
            get_logger().info('No suggestions found to improve this PR.')
            if self.progress_response:
                return self.git_provider.edit_comment(self.progress_response,
                                                      body='No suggestions found to improve this PR.')
            else:
                return self.git_provider.publish_comment('No suggestions found to improve this PR.')

        for d in data['code_suggestions']:
            try:
                if get_settings().config.verbosity_level >= 2:
                    get_logger().info(f"suggestion: {d}")
                relevant_file = d['relevant_file'].strip()
                relevant_lines_start = int(d['relevant_lines_start'])  # absolute position
                relevant_lines_end = int(d['relevant_lines_end'])
                content = self._strip_severity_line(d['suggestion_content'].rstrip())
                new_code_snippet = d['improved_code'].rstrip()
                label = d['label'].strip()

                if new_code_snippet:
                    new_code_snippet = self.dedent_code(relevant_file, relevant_lines_start, new_code_snippet)

                if d.get('score'):
                    body = f"**Suggestion:** {content} [{label}, importance: {d.get('score')}]\n```suggestion\n" + new_code_snippet + "\n```"
                else:
                    body = f"**Suggestion:** {content} [{label}]\n```suggestion\n" + new_code_snippet + "\n```"
                code_suggestions.append({'body': body, 'relevant_file': relevant_file,
                                         'relevant_lines_start': relevant_lines_start,
                                         'relevant_lines_end': relevant_lines_end,
                                         'original_suggestion': d})
            except Exception:
                get_logger().info(f"Could not parse suggestion: {d}")

        is_successful = self.git_provider.publish_code_suggestions(code_suggestions)
        if not is_successful:
            get_logger().info("Failed to publish code suggestions, trying to publish each suggestion separately")
            for code_suggestion in code_suggestions:
                self.git_provider.publish_code_suggestions([code_suggestion])

    def dedent_code(self, relevant_file, relevant_lines_start, new_code_snippet):
        try:  # dedent code snippet
            self.diff_files = self.git_provider.diff_files if self.git_provider.diff_files \
                else self.git_provider.get_diff_files()
            original_initial_line = None
            for file in self.diff_files:
                if file.filename.strip() == relevant_file:
                    if file.head_file:
                        file_lines = file.head_file.splitlines()
                        if relevant_lines_start > len(file_lines):
                            get_logger().warning(
                                "Could not dedent code snippet, because relevant_lines_start is out of range",
                                artifact={'filename': file.filename,
                                          'file_content': file.head_file,
                                          'relevant_lines_start': relevant_lines_start,
                                          'new_code_snippet': new_code_snippet})
                            return new_code_snippet
                        else:
                            original_initial_line = file_lines[relevant_lines_start - 1]
                    else:
                        get_logger().warning("Could not dedent code snippet, because head_file is missing",
                                             artifact={'filename': file.filename,
                                                       'relevant_lines_start': relevant_lines_start,
                                                       'new_code_snippet': new_code_snippet})
                        return new_code_snippet
                    break
            if original_initial_line:
                suggested_initial_line = new_code_snippet.splitlines()[0]
                original_initial_spaces = len(original_initial_line) - len(original_initial_line.lstrip()) # lstrip works both for spaces and tabs
                suggested_initial_spaces = len(suggested_initial_line) - len(suggested_initial_line.lstrip())
                delta_spaces = original_initial_spaces - suggested_initial_spaces
                if delta_spaces > 0:
                    # Detect indentation character from original line
                    indent_char = '\t' if original_initial_line.startswith('\t') else ' '
                    new_code_snippet = textwrap.indent(new_code_snippet, delta_spaces * indent_char).rstrip('\n')
        except Exception as e:
            get_logger().error(f"Error when dedenting code snippet for file {relevant_file}, error: {e}")

        return new_code_snippet

    def validate_one_liner_suggestion_not_repeating_code(self, suggestion):
        try:
            existing_code = suggestion.get('existing_code', '').strip()
            if '...' in existing_code:
                return suggestion
            new_code = suggestion.get('improved_code', '').strip()

            relevant_file = suggestion.get('relevant_file', '').strip()
            diff_files = self.git_provider.get_diff_files()
            for file in diff_files:
                if file.filename.strip() == relevant_file:
                    # protections
                    if not file.head_file:
                        get_logger().info(f"head_file is empty")
                        return suggestion
                    head_file = file.head_file
                    base_file = file.base_file
                    if existing_code in base_file and existing_code not in head_file and new_code in head_file:
                        suggestion["score"] = 0
                        get_logger().warning(
                            f"existing_code is in the base file but not in the head file, setting score to 0",
                            artifact={"suggestion": suggestion})
        except Exception as e:
            get_logger().exception(f"Error validating one-liner suggestion", artifact={"error": e})

        return suggestion

    def remove_line_numbers(self, patches_diff_list: List[str]) -> List[str]:
        # create a copy of the patches_diff_list, without line numbers for '__new hunk__' sections
        try:
            self.patches_diff_list_no_line_numbers = []
            for patches_diff in self.patches_diff_list:
                patches_diff_lines = patches_diff.splitlines()
                for i, line in enumerate(patches_diff_lines):
                    if line.strip():
                        if line.isnumeric():
                            patches_diff_lines[i] = ''
                        elif line[0].isdigit():
                            # find the first letter in the line that starts with a valid letter
                            for j, char in enumerate(line):
                                if not char.isdigit():
                                    patches_diff_lines[i] = line[j + 1:]
                                    break
                self.patches_diff_list_no_line_numbers.append('\n'.join(patches_diff_lines))
            return self.patches_diff_list_no_line_numbers
        except Exception as e:
            get_logger().error(f"Error removing line numbers from patches_diff_list, error: {e}")
            return patches_diff_list

    async def prepare_prediction_main(self, model: str) -> dict:
        self.related_files_context = self._get_related_files_context()
        # get PR diff
        map_reduce_enabled = bool(get_settings().get("large_mr_review.enabled", True))
        if map_reduce_enabled:
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
            self.patches_diff_list = [chunk.text for chunk in plan.chunks]
            self.patches_diff_list_no_line_numbers = [chunk.raw_text for chunk in plan.chunks]
            if not plan.is_complete_plan:
                self.review_coverage = coverage_for_results(plan, (), ())
                self.coverage_failure_message = self._format_map_reduce_coverage(plan.status)
                get_logger().warning(self.coverage_failure_message)
                update_review_run(
                    stage="failed",
                    status="failed",
                    error_code="incomplete_coverage",
                    error_message=self.coverage_failure_message,
                )
                self.data = None
                return None
        elif get_settings().pr_code_suggestions.decouple_hunks:
            self.patches_diff_list = get_pr_multi_diffs(self.git_provider,
                                                        self.token_handler,
                                                        model,
                                                        max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                        add_line_numbers=True)  # decouple hunk with line numbers
            self.patches_diff_list_no_line_numbers = self.remove_line_numbers(self.patches_diff_list)  # decouple hunk

        else:
            # non-decoupled hunks
            self.patches_diff_list_no_line_numbers = get_pr_multi_diffs(self.git_provider,
                                                                        self.token_handler,
                                                                        model,
                                                                        max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                                        add_line_numbers=False)
            self.patches_diff_list = await self.convert_to_decoupled_with_line_numbers(
                self.patches_diff_list_no_line_numbers, model)
            if not self.patches_diff_list:
                # fallback to decoupled hunks
                self.patches_diff_list = get_pr_multi_diffs(self.git_provider,
                                                            self.token_handler,
                                                            model,
                                                            max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                            add_line_numbers=True)  # decouple hunk with line numbers

        if self.patches_diff_list:
            get_logger().info(f"Number of PR chunk calls: {len(self.patches_diff_list)}")
            get_logger().debug(f"PR diff:", artifact=self.patches_diff_list)

            # parallelize calls to AI:
            if map_reduce_enabled:
                max_concurrency = max(1, int(get_settings().get("large_mr_review.max_concurrency", 4)))
                semaphore = asyncio.Semaphore(max_concurrency)

                async def predict(patches_diff, patches_diff_no_line_numbers):
                    async with semaphore:
                        return await self._get_prediction(model, patches_diff, patches_diff_no_line_numbers)

                if get_settings().pr_code_suggestions.parallel_calls:
                    raw_results = await asyncio.gather(
                        *(predict(patches_diff, patches_diff_no_line_numbers) for
                          patches_diff, patches_diff_no_line_numbers in
                          zip(self.patches_diff_list, self.patches_diff_list_no_line_numbers)),
                        return_exceptions=True,
                    )
                else:
                    raw_results = []
                    for patches_diff, patches_diff_no_line_numbers in zip(
                            self.patches_diff_list, self.patches_diff_list_no_line_numbers):
                        try:
                            raw_results.append(await predict(patches_diff, patches_diff_no_line_numbers))
                        except Exception as exc:
                            raw_results.append(exc)
                prediction_list = []
                successful_ids = []
                failed_ids = []
                for chunk, result in zip(self.review_chunk_plan.chunks, raw_results):
                    if isinstance(result, BaseException) or not isinstance(result, dict):
                        failed_ids.append(chunk.chunk_id)
                        get_logger().warning(f"Large MR improve chunk failed: {chunk.chunk_id[:12]}")
                        continue
                    successful_ids.append(chunk.chunk_id)
                    prediction_list.append(result)
                self.review_coverage = coverage_for_results(self.review_chunk_plan, successful_ids, failed_ids)
                self.prediction_list = prediction_list
                if self.review_coverage.status != "complete":
                    self.coverage_failure_message = self._format_map_reduce_coverage("chunk_failure")
                    get_logger().warning(self.coverage_failure_message)
                    update_review_run(
                        stage="failed",
                        status="failed",
                        error_code="incomplete_coverage",
                        error_message=self.coverage_failure_message,
                    )
                    if bool(get_settings().get("large_mr_review.fail_closed", True)):
                        self.data = None
                        return None
            elif get_settings().pr_code_suggestions.parallel_calls:
                prediction_list = await asyncio.gather(
                    *[self._get_prediction(model, patches_diff, patches_diff_no_line_numbers) for
                      patches_diff, patches_diff_no_line_numbers in
                      zip(self.patches_diff_list, self.patches_diff_list_no_line_numbers)])
                self.prediction_list = prediction_list
            else:
                prediction_list = []
                for patches_diff, patches_diff_no_line_numbers in zip(self.patches_diff_list, self.patches_diff_list_no_line_numbers):
                    prediction = await self._get_prediction(model, patches_diff, patches_diff_no_line_numbers)
                    prediction_list.append(prediction)

            data = {"code_suggestions": []}
            for j, predictions in enumerate(prediction_list):  # each call adds an element to the list
                if "code_suggestions" in predictions:
                    score_threshold = max(1, int(get_settings().pr_code_suggestions.suggestions_score_threshold))
                    for i, prediction in enumerate(predictions["code_suggestions"]):
                        try:
                            score = int(prediction.get("score", 1))
                            if score >= score_threshold:
                                data["code_suggestions"].append(prediction)
                            else:
                                get_logger().info(
                                    f"Removing suggestions {i} from call {j}, because score is {score}, and score_threshold is {score_threshold}",
                                    artifact=prediction)
                        except Exception as e:
                            get_logger().error(f"Error getting PR diff for suggestion {i} in call {j}, error: {e}",
                                               artifact={"prediction": prediction})
            data["code_suggestions"] = self._deduplicate_map_suggestions(data["code_suggestions"])
            if bool(get_settings().get("pr_code_suggestions.pipeline_v2_enabled", False)):
                repair_result = await self.run_repair_pipeline(
                    data["code_suggestions"],
                    patches_diff="\n\n".join(self.patches_diff_list) if self.patches_diff_list else "",
                )
                data["code_suggestions"] = repair_result["resolved"]
                self.pending_tier2_tasks = repair_result["pending_tier2"]

            self.data = data
        else:
            get_logger().warning(f"Empty PR diff list")
            self.data = data = None
        return data

    def _format_map_reduce_coverage(self, reason: str) -> str:
        coverage = getattr(self, "review_coverage", None)
        completed = len(coverage.completed_unit_ids) if coverage is not None else 0
        expected = len(coverage.expected_unit_ids) if coverage is not None else 0
        return (
            "Large MR improve did not cover the complete Diff: "
            f"processed {completed}/{expected} units, reason={reason}"
        )

    def _publish_map_reduce_coverage_failure(self) -> bool:
        """Publish an explicit coverage failure instead of a false no-suggestions result."""
        message = str(getattr(self, "coverage_failure_message", "") or "")
        if not message:
            return False
        try:
            get_settings().set("data", {"artifact": message, "large_mr_review_status": "incomplete"})
        except Exception:
            pass
        if bool(get_settings().config.publish_output):
            self.git_provider.remove_initial_comment()
            self.git_provider.publish_comment(f"⚠️ {message}")
        return True

    @staticmethod
    def _deduplicate_map_suggestions(suggestions: list) -> list:
        unique = []
        seen = set()
        for suggestion in suggestions:
            identity = (
                str(suggestion.get("relevant_file", "")).strip().casefold(),
                str(suggestion.get("relevant_lines_start", "")).strip(),
                str(suggestion.get("relevant_lines_end", "")).strip(),
                " ".join(str(suggestion.get("suggestion_content", "")).casefold().split()),
            )
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(suggestion)
        return unique

    async def run_repair_pipeline(self, code_suggestions: list, patches_diff: str = "") -> dict:
        """Pipeline v2 stages (3)+(4)+(5): deterministic fix, Tier-1 small-model retry,
        then final position normalization. Only called from prepare_prediction_main when
        pr_code_suggestions.pipeline_v2_enabled is true.

        Returns {"resolved": [...], "pending_tier2": [...]}: `resolved` is ready
        for stage (6) rendering (table + inline); `pending_tier2` are RepairTask
        dicts that still need Tier-2's heavy Copilot CLI channel (a later task
        in this plan) or, failing that, a final text-only fallback.
        """
        from pr_agent.suggestions.deterministic_fix import (
            _build_head_map,
            apply_final_normalization,
            run_deterministic_fix,
        )
        from pr_agent.suggestions.tier1_repair import run_tier1_repair

        try:
            head_map = _build_head_map(self.git_provider)
        except Exception as e:
            get_logger().warning(f"run_repair_pipeline: failed to build head_map: {e}")
            head_map = {}

        try:
            ordered_suggestions = [
                dict(suggestion, _pipeline_order=index)
                for index, suggestion in enumerate(code_suggestions)
            ]
            resolved, tasks = run_deterministic_fix(head_map, ordered_suggestions)
        except Exception as e:
            get_logger().warning(f"run_repair_pipeline: deterministic_fix failed, rejecting suggestions: {e}")
            return {"resolved": [], "pending_tier2": []}

        tier1_tasks = [t for t in tasks if not t.get("needs_tier2")]
        tier2_tasks = [t for t in tasks if t.get("needs_tier2")]

        if tier1_tasks:
            try:
                tier1_resolved, tier1_unresolved = await run_tier1_repair(
                    self.ai_handler, tier1_tasks, head_map,
                    project_prompt_rules=self._project_skill_context(
                        "tier1_repair",
                        getattr(self, "_improve_rule_languages", frozenset()),
                        tuple(task.get("file_path") or task.get("relevant_file") or "" for task in tier1_tasks),
                    ))
                resolved.extend(tier1_resolved)
                tier2_tasks.extend(tier1_unresolved)
            except Exception as e:
                get_logger().warning(f"run_repair_pipeline: tier1_repair failed, routing its tasks to tier2: {e}")
                tier2_tasks.extend(tier1_tasks)

        # Stage (5): final position normalization — discards model line numbers,
        # derives authoritative position from exact unique match in head_file, and
        # rejects any suggestion whose location falls outside the new-side diff hunks.
        try:
            resolved, _rejected = apply_final_normalization(resolved, head_map, patches_diff)
        except Exception as e:
            get_logger().warning(f"run_repair_pipeline: apply_final_normalization failed, rejecting suggestions: {e}")
            resolved = []

        resolved.sort(key=lambda suggestion: suggestion.get("_pipeline_order", len(code_suggestions)))
        for suggestion in resolved:
            suggestion.pop("_pipeline_order", None)
        tier2_tasks.sort(key=lambda task: min(
            (member.get("_pipeline_order", len(code_suggestions)) for member in task.get("members", [])),
            default=len(code_suggestions),
        ))
        for task in tier2_tasks:
            for member in task.get("members", []):
                member.pop("_pipeline_order", None)

        return {"resolved": resolved, "pending_tier2": tier2_tasks}

    async def convert_to_decoupled_with_line_numbers(self, patches_diff_list_no_line_numbers, model) -> List[str]:
        with get_logger().contextualize(sub_feature='convert_to_decoupled_with_line_numbers'):
            try:
                patches_diff_list = []
                for patch_prompt in patches_diff_list_no_line_numbers:
                    file_prefix = "## File: "
                    patches = patch_prompt.strip().split(f"\n{file_prefix}")
                    patches_new = copy.deepcopy(patches)
                    for i in range(len(patches_new)):
                        if i == 0:
                            prefix = patches_new[i].split("\n@@")[0].strip()
                        else:
                            prefix = file_prefix + patches_new[i].split("\n@@")[0]
                            prefix = prefix.strip()
                        patches_new[i] = prefix + '\n\n' + decouple_and_convert_to_hunks_with_lines_numbers(patches_new[i],
                                                                                                          file=None).strip()
                        patches_new[i] = patches_new[i].strip()
                    patch_final = "\n\n\n".join(patches_new)
                    if model in MAX_TOKENS:
                        max_tokens_full = MAX_TOKENS[
                            model]  # note - here we take the actual max tokens, without any reductions. we do aim to get the full documentation website in the prompt
                    else:
                        max_tokens_full = get_max_tokens(model)
                    delta_output = 2000
                    token_count = self.token_handler.count_tokens(patch_final)
                    if token_count > max_tokens_full - delta_output:
                        get_logger().warning(
                            f"Token count {token_count} exceeds the limit {max_tokens_full - delta_output}. clipping the tokens")
                        patch_final = clip_tokens(patch_final, max_tokens_full - delta_output)
                    patches_diff_list.append(patch_final)
                return patches_diff_list
            except Exception as e:
                get_logger().exception(f"Error converting to decoupled with line numbers",
                                       artifact={'patches_diff_list_no_line_numbers': patches_diff_list_no_line_numbers})
                return []

    def generate_summarized_suggestions(self, data: Dict) -> str:
        try:
            lang = str(get_settings().config.get("response_language", "en-US")).lower()
            is_zh = lang.startswith("zh")
            pr_body = "## PR 代码建议 ✨\n\n" if is_zh else "## PR Code Suggestions ✨\n\n"

            if len(data.get('code_suggestions', [])) == 0:
                pr_body += "未发现可改进建议。" if is_zh else "No suggestions found to improve this PR."
                return pr_body

            # Drop suggestions missing required rendering keys (e.g. when
            # self-reflection failed to assign relevant_lines_start/score).
            # Without this, a single bad item raises and triggers the outer
            # except, returning "" — which then makes GitLab reject the empty
            # comment body with HTTP 400.
            required_keys = ('relevant_file', 'relevant_lines_start', 'relevant_lines_end',
                             'existing_code', 'improved_code', 'one_sentence_summary', 'label')
            filtered = []
            for s in data['code_suggestions']:
                missing = [k for k in required_keys if k not in s or s.get(k) in (None, "")]
                if missing:
                    get_logger().warning(
                        f"Skipping suggestion missing keys {missing} during summarization: {s}")
                    continue
                if 'score' not in s:
                    s['score'] = 7
                filtered.append(s)
            data['code_suggestions'] = filtered
            if not filtered:
                pr_body += "未发现可改进建议。" if is_zh else "No suggestions found to improve this PR."
                return pr_body

            # Suggestions that Tier-2 (heavy_repair.py) resolved at multiple,
            # usually non-contiguous, code locations for the SAME original
            # issue share a "source_task_id" tag. GitLab can't offer one
            # one-click button spanning multiple locations (each location is
            # still published as its own independent inline suggestion
            # elsewhere), but this summary table isn't bound by that -- merge
            # them into a single row/details block so the table reflects
            # "one problem, N code locations" instead of N separate rows.
            grouped_suggestions = self._group_multi_location_suggestions(filtered)

            if get_settings().config.is_auto_command:
                pr_body += ("以下是可选的代码建议：\n\n" if is_zh else "Explore these optional code suggestions:\n\n")

            language_extension_map_org = get_settings().language_extension_map_org
            extension_to_language = {}
            for language, extensions in language_extension_map_org.items():
                for ext in extensions:
                    extension_to_language[ext] = language

            # Width is forced by padding the header cell's text with trailing
            # &nbsp; below (see `delta`), not by a table-level width
            # attribute: GitLab's own stylesheet applies
            # ".md table:not(.code) { width: auto }" to every rendered
            # markdown table, which always overrides an HTML "width"
            # attribute or inline "style" (also stripped by GitLab's
            # sanitizer) -- "width: auto" means "size to content", so the
            # only way to force a wider table is to widen its actual
            # content. See the matching comment in convert_to_markdown_v2
            # (algo/utils.py), which pads its own (otherwise narrower)
            # review guide table the same way for visual consistency.
            pr_body += '<table>'
            header = "建议" if is_zh else "Suggestion"
            delta = 66
            header += "&nbsp; " * delta
            th_category = "类别" if is_zh else "Category"
            th_impact = "影响" if is_zh else "Impact"
            pr_body += f"""<thead><tr><td align=center><strong>{th_impact}</strong></td><td><strong>{th_category}</strong></td><td align=left><strong>{header}</strong></td></tr>"""
            pr_body += """<tbody>"""
            suggestions_labels = dict()
            # add all suggestions related to each label
            for suggestion in grouped_suggestions:
                label = suggestion['label'].strip().strip("'").strip('"')
                if label not in suggestions_labels:
                    suggestions_labels[label] = []
                suggestions_labels[label].append(suggestion)

            # sort suggestions_labels by the suggestion with the highest score
            suggestions_labels = dict(
                sorted(suggestions_labels.items(), key=lambda x: max([s['score'] for s in x[1]]), reverse=True))
            # sort the suggestions inside each label group by score
            for label, suggestions in suggestions_labels.items():
                suggestions_labels[label] = sorted(suggestions, key=lambda x: x['score'], reverse=True)

            counter_suggestions = 0
            for label, suggestions in suggestions_labels.items():
                num_suggestions = len(suggestions)
                display_label = label.capitalize()
                if is_zh:
                    label_map = {
                        "security": "安全性",
                        "safety": "安全性",
                        "correctness": "正确性",
                        "possible bug": "可能缺陷",
                        "possible issue": "可能问题",
                        "performance": "性能",
                        "enhancement": "增强",
                        "best practice": "最佳实践",
                        "maintainability": "可维护性",
                        "readability": "可读性",
                        "tests": "测试",
                        "documentation": "文档",
                        "numeric stability": "数值稳定性",
                        "time&frame": "时间与帧",
                        "concurrency": "并发",
                        "real-time&perf": "实时与性能",
                        "api misuse": "API 误用",
                        "general": "一般",
                        "critical bug": "严重缺陷",
                    }
                    display_label = label_map.get(label.lower(), display_label)
                for i, suggestion in enumerate(suggestions):

                    multi_members = suggestion.get('_multi_location_members')
                    # One-click appliability: normal (non-Tier-2) suggestions
                    # are always appliable -- they've already been validated
                    # to sit inside this PR's diff. Tier-2's copy_patch
                    # results are the only kind that can't be one-click
                    # applied (the file is outside the diff; GitLab rejects
                    # inline suggestions there). For a multi-location group,
                    # ANY unappliable location makes the whole group
                    # unappliable -- there's no partial-credit "3 can be
                    # applied, 2 can't" display, just a single appliable/not
                    # verdict per the user's explicit requirement.
                    if multi_members:
                        appliable = all(m.get('resolved_by_stage') != 'tier2_copy_patch' for m in multi_members)
                    else:
                        appliable = suggestion.get('resolved_by_stage') != 'tier2_copy_patch'

                    relevant_file = suggestion['relevant_file'].strip()
                    relevant_lines_start = int(suggestion['relevant_lines_start'])
                    relevant_lines_end = int(suggestion['relevant_lines_end'])
                    range_str = ""
                    if relevant_lines_start < 0 or relevant_lines_end < 0:
                        range_str = ""
                    elif relevant_lines_start == relevant_lines_end:
                        range_str = f"[{relevant_lines_start}]"
                    else:
                        range_str = f"[{relevant_lines_start}-{relevant_lines_end}]"

                    try:
                        if relevant_lines_start > 0 and relevant_lines_end > 0:
                            code_snippet_link = self.git_provider.get_line_link(relevant_file, relevant_lines_start,
                                                                                relevant_lines_end)
                        else:
                            code_snippet_link = ""
                    except:
                        code_snippet_link = ""
                    # add html table for each suggestion

                    raw_suggestion_content = suggestion['suggestion_content'].rstrip()
                    suggestion_content = self._strip_severity_line(raw_suggestion_content)
                    CHAR_LIMIT_PER_LINE = 84
                    suggestion_content = insert_br_after_x_chars(suggestion_content, CHAR_LIMIT_PER_LINE)

                    if multi_members:
                        # One diff block per code location, each carrying its
                        # own file/line link, instead of a single top-level
                        # link (there is no single "the" location here).
                        location_blocks = []
                        for member in multi_members:
                            try:
                                m_file = member['relevant_file'].strip()
                                m_start = int(member['relevant_lines_start'])
                                m_end = int(member['relevant_lines_end'])
                            except Exception:
                                continue
                            if m_start < 0 or m_end < 0:
                                m_range = ""
                            elif m_start == m_end:
                                m_range = f"[{m_start}]"
                            else:
                                m_range = f"[{m_start}-{m_end}]"
                            try:
                                m_link = self.git_provider.get_line_link(m_file, m_start, m_end) \
                                    if m_start > 0 and m_end > 0 else ""
                            except:
                                m_link = ""
                            m_existing = str(member.get('existing_code', '')).rstrip() + "\n"
                            m_improved = str(member.get('improved_code', '')).rstrip() + "\n"
                            m_diff = difflib.unified_diff(m_existing.split('\n'), m_improved.split('\n'), n=999)
                            m_patch = "\n".join("\n".join(m_diff).splitlines()[5:]).strip('\n')
                            location_blocks.append(
                                f"[{m_file} {m_range}]({m_link})\n\n```diff\n{m_patch.rstrip()}\n```")
                        location_line = ""
                        example_code = "\n\n".join(location_blocks)
                        location_count_note = (f"（涉及 {len(multi_members)} 处代码位置）" if is_zh
                                               else f" (spans {len(multi_members)} code locations)")
                    else:
                        existing_code = suggestion['existing_code'].rstrip() + "\n"
                        improved_code = suggestion['improved_code'].rstrip() + "\n"

                        diff = difflib.unified_diff(existing_code.split('\n'),
                                                    improved_code.split('\n'), n=999)
                        patch_orig = "\n".join(diff)
                        patch = "\n".join(patch_orig.splitlines()[5:]).strip('\n')

                        example_code = ""
                        example_code += f"```diff\n{patch.rstrip()}\n```\n"
                        location_line = f"[{relevant_file} {range_str}]({code_snippet_link})\n\n"
                        location_count_note = ""

                    try:
                        score_int = int(suggestion.get('score', 0))
                    except Exception:
                        score_int = 0
                    impact_level = self._extract_impact_level(suggestion, raw_suggestion_content, is_zh)
                    impact_text = impact_level
                    if impact_level in {"阻断", "高", "Blocker", "High"}:
                        impact_text = f"<span style='color:red;'>{impact_level}</span>"
                    score_suffix = f"（{score_int}）" if is_zh else f" ({score_int})"
                    impact_cell = f"<strong>{impact_text}{score_suffix}</strong>"

                    if i == 0:
                        pr_body += f"""<tr><td align=center>{impact_cell}</td><td rowspan={num_suggestions}>{display_label}</td><td>\n\n"""
                    else:
                        pr_body += f"""<tr><td align=center>{impact_cell}</td><td>\n\n"""
                    applicability_note = (
                        ("（可一键应用修改）" if is_zh else " (can be applied with one click)") if appliable else
                        ("（文件不在本次改动范围内，需回本地修改）" if is_zh else
                         " (file outside this PR's diff; apply manually in your local checkout)")
                    )
                    suggestion_summary = (suggestion['one_sentence_summary'].strip().rstrip('.')
                                          + location_count_note + applicability_note)
                    if "'<" in suggestion_summary and ">'" in suggestion_summary:
                        # escape the '<' and '>' characters, otherwise they are interpreted as html tags
                        get_logger().info(f"Escaped suggestion summary: {suggestion_summary}")
                        suggestion_summary = suggestion_summary.replace("'<", "`<")
                        suggestion_summary = suggestion_summary.replace(">'", ">`")
                    if '`' in suggestion_summary:
                        suggestion_summary = replace_code_tags(suggestion_summary)

                    pr_body += f"""\n\n<details><summary>{suggestion_summary}</summary>\n\n___\n\n"""
                    pr_body += f"""
**{self._localize_suggestion_content(suggestion_content, is_zh)}**

{location_line}{example_code.rstrip()}
"""
                    pr_body += f"</details>"
                    inline_note_url = suggestion.get("inline_note_url")
                    if inline_note_url:
                        jump_text = "点击跳转至应用建议处" if is_zh else "Click to jump to the applied suggestion"
                        pr_body += f"\n\n[{jump_text}]({inline_note_url})"
                    pr_body += f"</td></tr>"
                    counter_suggestions += 1

                # pr_body += "</details>"
                # pr_body += """</td></tr>"""
            pr_body += """</tr></tbody></table>"""
            return pr_body
        except Exception as e:
            get_logger().info(f"Failed to publish summarized code suggestions, error: {e}")
            return ""

    @staticmethod
    def generate_pending_suggestions() -> str:
        lang = str(get_settings().config.get("response_language", "en-US")).lower()
        if lang.startswith("zh"):
            return "## PR 代码建议 ✨\n\n代码建议正在发布..."
        return "## PR Code Suggestions ✨\n\nPublishing code suggestions..."

    def get_score_str(self, score: int) -> str:
        th_high = get_settings().pr_code_suggestions.get('new_score_mechanism_th_high', 9)
        th_medium = get_settings().pr_code_suggestions.get('new_score_mechanism_th_medium', 7)
        lang = str(get_settings().config.get("response_language", "zh-CN")).lower()
        is_zh = lang.startswith("zh")
        if score >= th_high:
            return "高" if is_zh else "High"
        elif score >= th_medium:
            return "中" if is_zh else "Medium"
        else:
            return "低" if is_zh else "Low"

    @staticmethod
    def _extract_impact_level(suggestion: Dict, suggestion_content: str, is_zh: bool) -> str:
        # Impact/risk level is independent from score (score indicates likelihood).
        level_raw = ""
        for key in ("impact", "risk", "severity", "impact_level"):
            value = suggestion.get(key)
            if value:
                level_raw = str(value).strip()
                break

        if not level_raw and suggestion_content:
            for line in suggestion_content.splitlines():
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

    @staticmethod
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

    @staticmethod
    def _group_multi_location_suggestions(suggestions: List[Dict]) -> List[Dict]:
        """Merge suggestions that all originated from the SAME Tier-2 RepairTask
        (see heavy_repair.py's classify_heavy_repair_results -- they share a
        "source_task_id" tag) into a single renderable unit: one table row
        and one <details> block listing every code location together,
        instead of one row/details block per location.

        GitLab has no way to offer a single one-click "Apply" button
        spanning multiple, usually non-contiguous, code locations -- each
        location is still published as its own independent inline
        suggestion elsewhere (see tier2_scheduler.py's overview comment for
        that side of it). This summary table isn't bound by that platform
        limit, though, so it should present them as what they actually are:
        one problem, N code locations -- not N unrelated rows.

        Suggestions without a source_task_id (the normal, non-Tier-2 case,
        and the vast majority of suggestions) pass through completely
        unchanged, preserving today's one-row-per-suggestion behavior.

        Returns a new list; never mutates the input list or its dicts.
        """
        groups: "OrderedDict[str, List[Dict]]" = OrderedDict()
        singles: List[Dict] = []
        for s in suggestions or []:
            task_id = s.get("source_task_id")
            if not task_id:
                singles.append(s)
                continue
            groups.setdefault(task_id, []).append(s)

        merged: List[Dict] = []
        for members in groups.values():
            if len(members) == 1:
                merged.append(members[0])
                continue
            primary = dict(members[0])
            primary["_multi_location_members"] = members
            merged.append(primary)
        return merged + singles

    def _localize_suggestion_content(self, text: str, is_zh: bool) -> str:
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

    @staticmethod
    def _normalize_score(value):
        if value is None or value == "":
            return None
        try:
            score = int(value)
        except Exception:
            return None
        return max(0, min(10, score))

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "")
        return bool(value)

    async def self_reflect_on_suggestions(self,
                                          suggestion_list: List,
                                          patches_diff: str,
                                          model: str,
                                          prev_suggestions_str: str = "",
                                          dedicated_prompt: str = "",
                                          project_rule_languages: frozenset[str] | None = None) -> str:
        if not suggestion_list:
            return ""

        try:
            suggestion_str = ""
            for i, suggestion in enumerate(suggestion_list):
                suggestion_str += f"suggestion {i + 1}: " + str(suggestion) + '\n\n'

            lang = str(get_settings().config.get("response_language", "en-US")).lower()
            variables = {'suggestion_list': suggestion_list,
                         'suggestion_str': suggestion_str,
                         "diff": patches_diff,
                         'num_code_suggestions': len(suggestion_list),
                         'prev_suggestions_str': prev_suggestions_str,
                         "is_ai_metadata": get_settings().get("config.enable_ai_metadata", False),
                         'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
                         'response_language': lang,
                         'is_zh': lang.startswith("zh"),
                         'project_prompt_rules': self._project_skill_context(
                             "reflection",
                             project_rule_languages,
                             tuple(str(item.get("relevant_file") or "") for item in suggestion_list),
                         )}
            environment = Environment(undefined=StrictUndefined)

            if dedicated_prompt:
                system_prompt_reflect = environment.from_string(
                    get_settings().get(dedicated_prompt).system).render(variables)
                user_prompt_reflect = environment.from_string(
                    get_settings().get(dedicated_prompt).user).render(variables)
            else:
                system_prompt_reflect = environment.from_string(
                    get_settings().pr_code_suggestions_reflect_prompt.system).render(variables)
                user_prompt_reflect = environment.from_string(
                    get_settings().pr_code_suggestions_reflect_prompt.user).render(variables)

            user_prompt_reflect = append_project_skill_context(
                user_prompt_reflect,
                self._project_skill_effective(
                    "reflection",
                    project_rule_languages,
                    tuple(str(item.get("relevant_file") or "") for item in suggestion_list),
                ),
            )

            with get_logger().contextualize(command="self_reflect_on_suggestions"):
                response_reflect, finish_reason_reflect = await self.ai_handler.chat_completion(model=model,
                                                                                                system=system_prompt_reflect,
                                                                                                temperature=get_settings().config.temperature,
                                                                                                user=user_prompt_reflect)
        except Exception as e:
            get_logger().warning(f"Self-reflection failed with exception: {e}. All suggestions will default to score=7.", artifact={"error": str(e)})
            return ""
        return response_reflect
