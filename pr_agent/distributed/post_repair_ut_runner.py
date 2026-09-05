"""Durable adapter between a Feishu post-repair task and the UT Agent."""

import os

from pr_agent.distributed.models import PipelineEvent, TaskEnvelope
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.tools.pr_ut import serialize_diff_files
from ut_agent.agent import UT_WORKSPACE, UTAgent
from ut_agent.tools.context import init_context, reset_context


class PostRepairUTRunner:
    def __init__(self, *, checkpointer=None):
        self.checkpointer = checkpointer

    def _context(self, task: TaskEnvelope) -> tuple[object, dict]:
        provider = get_git_provider_with_context(task.pr_url)
        provider = getattr(provider, "original_provider", provider)
        os.makedirs(UT_WORKSPACE, exist_ok=True)
        mr_info = {
            "trigger_type": "feishu_post_repair_ut",
            "pr_url": task.pr_url,
            "title": str(getattr(provider.pr, "title", "")),
            "author": self._author(provider),
            "mr_id": int(getattr(provider.pr, "iid", 0) or 0),
            "source_branch": provider.get_pr_branch(),
            "target_branch": str(getattr(provider.pr, "target_branch", "")),
            "diff_files": serialize_diff_files(provider),
            "project_id": str(getattr(provider, "id_project", "")),
            "pipeline_id": int(task.payload.get("baseline_pipeline_id") or 0),
            "commit_sha": str(task.payload.get("baseline_sha") or ""),
            "failed_jobs": None,
            "coverage_before": task.payload.get("coverage_before"),
            "coverage_status_before": str(task.payload.get("coverage_status_before") or ""),
        }
        return provider, mr_info

    @staticmethod
    def _author(provider) -> str:
        author = getattr(provider.pr, "author", None)
        return str(author.get("username") or "unknown") if isinstance(author, dict) else str(author or "unknown")

    async def run(self, task: TaskEnvelope) -> dict:
        provider, mr_info = self._context(task)
        token = init_context(git_provider=provider, output_dir=UT_WORKSPACE)
        try:
            return await UTAgent(checkpointer=self.checkpointer).run(mr_info)
        finally:
            reset_context(token)

    async def resume(self, task: TaskEnvelope, event: PipelineEvent) -> dict:
        provider, _ = self._context(task)
        token = init_context(git_provider=provider, output_dir=UT_WORKSPACE)
        try:
            return await UTAgent(checkpointer=self.checkpointer).resume(task.task_id, event)
        finally:
            reset_context(token)
