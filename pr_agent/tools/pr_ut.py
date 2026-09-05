"""
/ut 命令 - 收集 MR 信息并转发给 UT Agent，将其响应作为评论发布到 MR 上。
"""
from functools import partial

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger


def serialize_diff_files(git_provider) -> list[dict]:
    """将 provider diff 对象转换为 UT Agent state。"""
    return [{
        "filename": file.filename,
        "patch": file.patch,
        "head_file": file.head_file,
        "edit_type": file.edit_type.name if file.edit_type else "UNKNOWN",
        "language": file.language or "unknown",
    } for file in git_provider.get_diff_files()]


class PRUT:
    """
    PRUT 类负责收集 MR 信息，委托给基于 LangGraph 的 UT Agent 进行分析，
    然后将 Agent 的响应作为评论发布到 MR 上。
    """

    def __init__(self, pr_url: str, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler, args: list = None):
        self.pr_url = pr_url
        self.git_provider = get_git_provider_with_context(pr_url)
        self.args = args

    async def run(self) -> None:
        try:
            get_logger().info(f"UT Agent: 正在处理 MR {self.pr_url}")

            # 发布临时加载提示
            if get_settings().config.publish_output:
                self.git_provider.publish_comment("UT Agent 正在分析本 MR...", is_temporary=True)

            # 收集 MR 信息
            mr_info = self._collect_mr_info()

            # 初始化 UT Agent 工作目录和上下文
            import os

            from ut_agent.agent import UT_WORKSPACE
            from ut_agent.tools.context import init_context
            os.makedirs(UT_WORKSPACE, exist_ok=True)
            init_context(git_provider=self.git_provider, output_dir=UT_WORKSPACE)

            # 运行 UT Agent
            from ut_agent import UTAgent
            agent = UTAgent()
            response = await agent.run(mr_info)

            # 发布响应评论
            if get_settings().config.publish_output:
                self.git_provider.publish_comment(response)
                get_logger().info("UT Agent: 评论发布成功")

            self.git_provider.remove_initial_comment()

        except Exception as e:
            get_logger().error(f"UT Agent 失败: {e}")
            if get_settings().config.publish_output:
                self.git_provider.publish_comment(
                    f"## UT Agent 错误 ❌\n\n分析本 MR 失败: {e}"
                )

    def _collect_mr_info(self) -> dict:
        """收集 MR 信息，传递给 UT Agent。"""
        diff_files = serialize_diff_files(self.git_provider)

        return {
            "trigger_type": "mr_created",
            "pr_url": self.pr_url,
            "title": self.git_provider.pr.title,
            "author": getattr(self.git_provider.pr, "author", {}).get("username", "unknown") if isinstance(getattr(self.git_provider.pr, "author", None), dict) else str(getattr(self.git_provider.pr, "author", "unknown")),
            "mr_id": getattr(self.git_provider.pr, "iid", 0),
            "source_branch": self.git_provider.get_pr_branch(),
            "target_branch": getattr(self.git_provider.pr, "target_branch", "") if hasattr(self.git_provider, "pr") else "",
            "diff_files": diff_files,
            "project_id": getattr(self.git_provider, "id_project", ""),
            "pipeline_id": None,
            "failed_jobs": None,
        }
