import shlex
from functools import partial
from typing import Optional, Tuple

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.cli_args import CliArgs
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.distributed.broker import LostLeaseError
from pr_agent.distributed.runtime import TaskSuspended
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import get_logger
from pr_agent.tools.pr_add_docs import PRAddDocs
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_config import PRConfig
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_doc_drift import PRDocDrift
from pr_agent.tools.pr_generate_labels import PRGenerateLabels
from pr_agent.tools.pr_help_docs import PRHelpDocs
from pr_agent.tools.pr_help_message import PRHelpMessage
from pr_agent.tools.pr_line_questions import PR_LineQuestions
from pr_agent.tools.pr_questions import PRQuestions
from pr_agent.tools.pr_reviewer import PRReviewer
from pr_agent.tools.pr_similar_issue import PRSimilarIssue
from pr_agent.tools.pr_update_changelog import PRUpdateChangelog
from pr_agent.tools.pr_mr_create import PRMrCreate
from pr_agent.tools.pr_fix_format import PRFixFormat
from pr_agent.tools.pr_ut import PRUT
from pr_agent.tools.pr_triage import PRTriage
from pr_agent.tools.pr_feedback import PRFeedback

#用户命令和对应的类
command2class = {
    "auto_review": PRReviewer,
    "answer": PRReviewer,
    "review": PRReviewer,
    "review_pr": PRReviewer,
    "describe": PRDescription,
    "describe_pr": PRDescription,
    "improve": PRCodeSuggestions,
    "improve_code": PRCodeSuggestions,
    "ask": PRQuestions,
    "ask_question": PRQuestions,
    "ask_line": PR_LineQuestions,
    "update_changelog": PRUpdateChangelog,
    "config": PRConfig,
    "settings": PRConfig,
    "help": PRHelpMessage,
    "similar_issue": PRSimilarIssue,
    "add_docs": PRAddDocs,
    "generate_labels": PRGenerateLabels,
    "help_docs": PRHelpDocs,
    "doc_drift": PRDocDrift,
    "mr_create": PRMrCreate,
    "fix_format": PRFixFormat,
    "fix-format": PRFixFormat,
    "ut": PRUT,
    "triage": PRTriage,
}

#命令列表，/help的时候会输出这个
commands = list(command2class.keys())



class PRAgent:
    def __init__(self, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        self.ai_handler = ai_handler  # will be initialized in run_action

    @staticmethod
    def _has_gitlab_improve_output(tool_instance) -> Optional[bool]:
        """
        Return True if an improve result comment already exists on the GitLab MR.
        Returns None when the provider is not GitLab or when the check cannot be performed.

        Looks specifically for the improve header (not total note count) so parallel
        tools (review / describe / help) running at the same time cannot cause false results.
        """
        try:
            git_provider = getattr(tool_instance, "git_provider", None)
            if not isinstance(git_provider, GitLabProvider):
                return None

            lang = str(get_settings().config.get("response_language", "en-US")).lower()
            is_zh = lang.startswith("zh")
            header = "## PR 代码建议 ✨" if is_zh else "## PR Code Suggestions ✨"

            notes = git_provider.mr.notes.list(get_all=True)
            return any(note.body.startswith(header) for note in notes)
        except Exception as e:
            get_logger().warning(f"Failed to check GitLab improve output: {e}")
            return None

    async def _handle_request(self, pr_url, request, notify=None, reviewer_user=None) -> bool:
        # 先查一下用户有没有自定义输入的system prompt和user prompt
        apply_repo_settings(pr_url)

        custom_system_prompt = None
        custom_user_prompt = None
        if isinstance(request, str):
            import re, hashlib, html
            normalized = html.unescape(request)
            normalized = normalized.replace("\\<", "<").replace("\\>", ">")
            sys_pat = r"<custom_system_prompt>(.*?)</custom_system_prompt>"
            user_pat = r"<custom_user_prompt>(.*?)</custom_user_prompt>"
            sys_match = re.search(sys_pat, normalized, re.DOTALL)
            user_match = re.search(user_pat, normalized, re.DOTALL)
            if sys_match:
                custom_system_prompt = sys_match.group(1).strip()
                normalized = normalized.replace(sys_match.group(0), "")
                prompt_len = len(custom_system_prompt)
                prompt_hash = hashlib.sha256(custom_system_prompt.encode("utf-8")).hexdigest()[:12]
                get_logger().info(f"Custom system prompt detected (len={prompt_len}, sha12={prompt_hash})")
            if user_match:
                custom_user_prompt = user_match.group(1).strip()
                normalized = normalized.replace(user_match.group(0), "")
                prompt_len_u = len(custom_user_prompt)
                prompt_hash_u = hashlib.sha256(custom_user_prompt.encode("utf-8")).hexdigest()[:12]
                get_logger().info(f"Custom user prompt detected (len={prompt_len_u}, sha12={prompt_hash_u})")
            request = normalized

        # 格式化处理用户输入的prompt
        if isinstance(request, str):
            request = request.replace("'", "\\'")
            lexer = shlex.shlex(request, posix=True)
            lexer.whitespace_split = True
            action, *args = list(lexer)
        else:
            action, *args = request

        # 拦截不能让用户自定义的参数，如改动模型之类的。毕竟容器内只设置了deepseek的token api
        is_valid, arg = CliArgs.validate_user_args(args)
        if not is_valid:
            get_logger().error(
                f"CLI argument for param '{arg}' is forbidden. Use instead a configuration file."
            )
            return False

        # Update settings from args
        args = update_settings_from_args(args)

        response_language = get_settings().config.get('response_language', 'zh-cn')
        if response_language.lower() != 'en-us':
            get_logger().info(f'User has set the response language to: {response_language}')
            for key in get_settings():
                setting = get_settings().get(key)
                if str(type(setting)) == "<class 'dynaconf.utils.boxing.DynaBox'>":
                    if hasattr(setting, 'extra_instructions'):
                        current_extra_instructions = setting.extra_instructions
                        lang_instruction_text = f"Your response MUST be written in the language corresponding to locale code: '{response_language}'. This is crucial."
                        separator_text = "\n======\n\nIn addition, "

                        if lang_instruction_text not in str(current_extra_instructions):
                            if current_extra_instructions:
                                setting.extra_instructions = str(current_extra_instructions) + separator_text + lang_instruction_text
                            else:
                                setting.extra_instructions = lang_instruction_text
        # 强制命令以'/'开头
        if isinstance(action, str):
            if not action.startswith("/"):
                get_logger().warning("Command must start with '/'. Ignoring request.")
                return False
            action = action[1:].lower()
        # 新增：写一个函数用来提取system prompt中的yaml block
        def _extract_yaml_enforcer(s: str) -> str:
            try:
                import re
                m = re.search(r"The output must be a YAML.*", s, re.DOTALL)
                if m:
                    return m.group(0).strip()
            except:
                pass
            return ""

        # 新增：如果用户自定义了system prompt，需要检查是否包含yaml block
        if custom_system_prompt or custom_user_prompt:
            if action in ("review", "review_pr", "auto_review", "answer"):
                if custom_system_prompt:
                    base_sys = str(global_settings.pr_review_prompt.system)
                    yaml_block = _extract_yaml_enforcer(base_sys) # 抽取base_sys中的yaml部分
                    composed = custom_system_prompt + ("\n\n" + yaml_block if yaml_block else "") # 合并用户自定义的system prompt和base_sys中的yaml部分
                    get_settings().set("pr_review_prompt.system", composed) # 暂时性写入配置（只在当前运行周期内生效）
                    get_logger().info("Applied custom system prompt for review pipeline")  #日志返回信息，成功运用用户prompts
                if custom_user_prompt:
                    get_settings().set("pr_review_prompt.user", custom_user_prompt)
                    get_logger().info("Applied custom user prompt for review pipeline")
            elif action in ("improve", "improve_code"):
                if custom_system_prompt:
                    base_sys = str(global_settings.pr_code_suggestions_prompt.system)
                    yaml_block = _extract_yaml_enforcer(base_sys)
                    composed = custom_system_prompt + ("\n\n" + yaml_block if yaml_block else "")
                    get_settings().set("pr_code_suggestions_prompt.system", composed)
                    get_settings().set("pr_code_suggestions_prompt_not_decoupled.system", composed)
                    get_logger().info("Applied custom system prompt for improve pipeline")
                if custom_user_prompt:
                    get_settings().set("pr_code_suggestions_prompt.user", custom_user_prompt)
                    get_settings().set("pr_code_suggestions_prompt_not_decoupled.user", custom_user_prompt)
                    get_logger().info("Applied custom user prompt for improve pipeline")
            elif action in ("describe", "describe_pr"):
                if custom_system_prompt:
                    base_sys = str(global_settings.pr_description_prompt.system)
                    yaml_block = _extract_yaml_enforcer(base_sys)
                    composed = custom_system_prompt + ("\n\n" + yaml_block if yaml_block else "")
                    get_settings().set("pr_description_prompt.system", composed)
                    get_logger().info("Applied custom system prompt for describe pipeline")
                if custom_user_prompt:
                    get_settings().set("pr_description_prompt.user", custom_user_prompt)
                    get_logger().info("Applied custom user prompt for describe pipeline")
            elif action in ("ask", "ask_question"):
                if custom_system_prompt:
                    get_settings().set("pr_questions_prompt.system", custom_system_prompt)
                    get_logger().info("Applied custom system prompt for ask pipeline")
                if custom_user_prompt:
                    get_settings().set("pr_questions_prompt.user", custom_user_prompt)
                    get_logger().info("Applied custom user prompt for ask pipeline")

        if action not in command2class:
            get_logger().warning(f"Unknown command: {action}")
            return False
        with get_logger().contextualize(command=action, pr_url=pr_url): # with可以自动try/finally，相当于帮你自动收尾
            get_logger().info("PR-Agent request handler started", analytics=True)
            if action == "answer": # 这个判断基本上走不进，我没提供这个
                if notify:
                    notify()
                await PRReviewer(pr_url, is_answer=True, args=args, ai_handler=self.ai_handler).run()
            elif action == "auto_review": # 这个判断也走不进，我内提供这个
                await PRReviewer(pr_url, is_auto=True, args=args, ai_handler=self.ai_handler).run()
            elif action in ("feedback", "rate"): # 用户对 review 结果评分/评论
                if notify:
                    notify()
                await PRFeedback(pr_url, args=args, ai_handler=self.ai_handler,
                                 reviewer_user=reviewer_user).run()
            elif action in command2class: # 一般都进这个
                if notify:
                    notify()

                tool_instance = command2class[action](pr_url, ai_handler=self.ai_handler, args=args)
                should_check_gitlab_output = (
                    action in ("improve", "improve_code") and bool(get_settings().config.publish_output)
                )

                await tool_instance.run() # await的作用是等待命令完成，但在等的过程中不把程序卡死，允许其他任务运行

                # 工程兜底：improve 在 GitLab 上没有实际输出时，再重试一次。
                # 这里精确查找 improve 自身的 header 评论是否存在，而不是统计全局 notes 数，
                # 避免 review / describe / help 等并行工具的评论干扰判断，也避免重复输出。
                if should_check_gitlab_output:
                    has_output = self._has_gitlab_improve_output(tool_instance)
                    if has_output is False:  # 确认没有 improve 结果评论
                        get_logger().warning(
                            "Improve finished but no GitLab output comment was found; retrying improve once.",
                        )
                        await tool_instance.run()
                    elif has_output is None:
                        get_logger().warning("Could not check GitLab improve output; skipping retry.")
            else:
                return False
            return True

    # 捕获异常，对外只传递一个bool值。这样不会因为发生异常导致崩溃，而是只传出一个false
    async def handle_request(self, pr_url, request, notify=None, reviewer_user=None) -> bool:
        try:
            return await self._handle_request(pr_url, request, notify, reviewer_user)
        except (TaskSuspended, LostLeaseError):
            raise
        except:
            get_logger().exception("Failed to process the command.")
            return False
