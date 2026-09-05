"""
/triage 命令 - 收集 MR + 流水线失败信息并转发给 UT Agent（ReAct 模式），
让 Agent 自主诊断失败原因并尝试修复。
"""
import time
from functools import partial
from typing import Optional

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.config_loader import get_settings
from pr_agent.distributed.models import PipelineEvent
from pr_agent.distributed.runtime import TaskSuspended
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger
from pr_agent.tools.pr_ut import serialize_diff_files
from pr_agent.triage.store import save_triage_run
from ut_agent import UTAgent


class PRTriage:
    """
    PRTriage 类负责收集 MR + 流水线失败信息，委托给 UT Agent（ReAct 模式）
    进行自主诊断和修复，然后将 Agent 的响应作为评论发布到 MR 上。

    与 PRUT 的区别：
    - trigger_type = "pipeline_failed"（Agent 知道是修复场景）
    - 注入 failed_jobs 和 pipeline_id（Agent 能看到失败 job 信息）
    - 注入 diff_files 和流水线上下文，避免 Agent 猜测仓库路径
    """

    def __init__(
        self,
        pr_url: str,
        ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler,
        args: list = None,
        *,
        pipeline_id: int | None = None,
        pipeline_sha: str = "",
        selected_categories: tuple[str, ...] = (),
    ):
        self.pr_url = pr_url
        self.git_provider = get_git_provider_with_context(pr_url)
        self.args = args
        self.pipeline_id = pipeline_id
        self.pipeline_sha = pipeline_sha
        self.selected_categories = selected_categories

    async def run(self, *, publish_result: bool = True, persist_result: bool = True):
        t0 = time.monotonic()
        triage_info = {}
        agent_result = None
        caught_error = None
        suspended = False
        try:
            get_logger().info(f"Triage Agent: 正在处理 MR {self.pr_url}")
            runtime = self._runtime()
            if runtime is not None:
                runtime.record_repair_progress_sync("preparing", "正在读取 MR 和流水线信息")

            # 发布临时加载提示
            if publish_result and get_settings().config.publish_output:
                self.git_provider.publish_comment(
                    "Triage Agent 正在诊断并尝试修复流水线失败（可能需要几分钟到半小时，请耐心等待）...",
                    is_temporary=True,
                )

            # 收集 MR + 流水线失败信息
            triage_info = self._collect_triage_info()

            # 初始化 UT Agent 工作目录和上下文
            import os

            from ut_agent.agent import UT_WORKSPACE
            from ut_agent.tools.context import init_context
            os.makedirs(UT_WORKSPACE, exist_ok=True)
            init_context(git_provider=self.git_provider, output_dir=UT_WORKSPACE)

            from ut_agent.workspace import prepare_workspace

            self._record_lifecycle("context", "start", "workspace")
            try:
                workspace_snapshot = prepare_workspace(
                    self.git_provider,
                    UT_WORKSPACE,
                    int(triage_info.get("mr_id") or 0),
                    str(triage_info.get("source_branch") or ""),
                )
            finally:
                self._record_lifecycle("context", "end", "workspace")
            triage_info["workspace_snapshot"] = workspace_snapshot.to_dict()
            triage_info["require_workspace_snapshot"] = True
            if workspace_snapshot.status != "ready":
                raise RuntimeError(
                    f"工作区准备失败（{workspace_snapshot.error_code or workspace_snapshot.status}）："
                    f"{workspace_snapshot.message}"
                )

            if runtime is not None:
                runtime.record_repair_progress_sync(
                    "diagnosing",
                    "工作区已准备完成，正在分析失败原因",
                    categories=tuple(self.selected_categories),
                    job_names=tuple(
                        str(job.get("name") or "")
                        for job in triage_info.get("failed_jobs") or ()
                        if isinstance(job, dict) and job.get("name")
                    ),
                    metadata={
                        "pipeline_id": int(triage_info.get("pipeline_id") or 0),
                        "commit_sha": str(triage_info.get("commit_sha") or ""),
                    },
                )

            # 运行 UT Agent（同一个 Agent，不同触发类型）
            agent = UTAgent()
            agent_result = await agent.run(triage_info)

            response = agent_result["response"] if isinstance(agent_result, dict) else str(agent_result)
            self._finalize_timing(agent_result, t0)
            # 发布响应评论
            if publish_result and get_settings().config.publish_output:
                self._publish_result(response, agent_result)
                get_logger().info("Triage Agent: 评论发布成功")

            if publish_result:
                self.git_provider.remove_initial_comment()

        except TaskSuspended:
            suspended = True
            get_logger().info(f"Triage Agent 正在等待流水线事件: {self.pr_url}")
            raise
        except Exception as e:
            caught_error = str(e)
            get_logger().error(f"Triage Agent 失败: {e}")
            agent_result = agent_result or {"result": {"success": False, "error": str(e)}}
            self._finalize_timing(agent_result, t0)
            if publish_result and get_settings().config.publish_output:
                self._publish_result(
                    f"## Triage Agent 错误\n\n诊断/修复失败: {e}",
                    agent_result,
                )
        finally:
            if persist_result and not suspended:
                self._finalize_timing(agent_result, t0)
                self._persist_result(triage_info, agent_result, t0, caught_error)

        return agent_result

    async def resume(
        self,
        task_id: str,
        pipeline_event: PipelineEvent,
        *,
        publish_result: bool = True,
        persist_result: bool = True,
    ):
        t0 = time.monotonic()
        triage_info = {}
        agent_result = None
        caught_error = None
        suspended = False
        try:
            import os

            from ut_agent.agent import UT_WORKSPACE
            from ut_agent.tools.context import init_context

            os.makedirs(UT_WORKSPACE, exist_ok=True)
            init_context(git_provider=self.git_provider, output_dir=UT_WORKSPACE)
            self._record_lifecycle("post_pipeline", "start", f"pipeline:{pipeline_event.pipeline_id}")
            try:
                agent_result = await UTAgent().resume(task_id, pipeline_event)
            finally:
                self._record_lifecycle("post_pipeline", "end", f"pipeline:{pipeline_event.pipeline_id}")
            triage_info = agent_result.get("state", {}) if isinstance(agent_result, dict) else {}
            response = agent_result["response"] if isinstance(agent_result, dict) else str(agent_result)
            self._finalize_timing(agent_result, t0)
            if publish_result and get_settings().config.publish_output:
                self._publish_result(response, agent_result)
                get_logger().info("Triage Agent: 流水线恢复结果发布成功")
            if publish_result:
                self.git_provider.remove_initial_comment()
        except TaskSuspended:
            suspended = True
            get_logger().info(f"Triage Agent 恢复后继续等待流水线事件: {self.pr_url}")
            raise
        except Exception as error:
            caught_error = str(error)
            get_logger().error(f"Triage Agent 恢复失败: {error}")
            agent_result = agent_result or {"result": {"success": False, "error": str(error)}}
            self._finalize_timing(agent_result, t0)
            if publish_result and get_settings().config.publish_output:
                self._publish_result(
                    f"## Triage Agent 错误\n\n恢复流水线等待失败: {error}",
                    agent_result,
                )
            raise
        finally:
            if persist_result and not suspended:
                self._finalize_timing(agent_result, t0)
                self._persist_result(triage_info, agent_result, t0, caught_error)

        return agent_result

    @staticmethod
    def _runtime():
        try:
            from pr_agent.distributed.runtime import get_execution_runtime

            return get_execution_runtime()
        except Exception:
            return None

    def _record_lifecycle(self, phase: str, kind: str, segment_id: str = "default") -> None:
        runtime = self._runtime()
        if runtime is not None:
            runtime.record_lifecycle_sync(phase, kind, segment_id=segment_id)

    def _finalize_timing(self, agent_result, t0: float) -> None:
        if not isinstance(agent_result, dict):
            return
        result = agent_result.setdefault("result", {})
        if not isinstance(result, dict):
            return
        fallback_ms = int((time.monotonic() - t0) * 1000)
        runtime = self._runtime()
        if runtime is None:
            result.setdefault("processing_total_ms", fallback_ms)
            result.setdefault("duration_ms", fallback_ms)
            result.setdefault("duration_breakdown", {"processing_total_ms": fallback_ms})
            return
        runtime.record_lifecycle_sync("terminal", "point")
        summary = runtime.lifecycle_summary_sync()
        processing_total_ms = summary.processing_total_ms if summary.processing_total_ms is not None else fallback_ms
        result["processing_total_ms"] = processing_total_ms
        result["duration_ms"] = processing_total_ms
        result["duration_breakdown"] = summary.to_dict()

    def _publish_result(self, response: str, agent_result) -> None:
        result = (agent_result or {}).get("result", {}) if isinstance(agent_result, dict) else {}
        details = dict(result) if isinstance(result, dict) else {}
        details.setdefault("duration_ms", details.get("processing_total_ms", 0))
        success_value = details.get("success", False)
        if isinstance(success_value, str):
            success = success_value.strip().lower() in {"1", "true", "yes"}
        else:
            success = bool(success_value)
        publisher = getattr(self.git_provider, "publish_triage_result", None)
        if callable(publisher):
            publisher(response, success=success, details=details)
        else:
            self.git_provider.publish_comment(response)

    def _persist_result(self, triage_info: dict, agent_result, t0: float, caught_error: str = None) -> None:
        """落盘 triage 结果（never raises，存储故障不阻断主流程）。"""
        try:
            result = (agent_result or {}).get("result", {}) if isinstance(agent_result, dict) else {}
            # 异常路径优先用 caught_error；Agent 内部错误优先用 result.error
            error = caught_error or result.get("error")
            record = {
                "pr_url": self.pr_url,
                "project": triage_info.get("project_id"),
                "mr_iid": triage_info.get("mr_id"),
                "mr_author": triage_info.get("author"),
                "source_branch": triage_info.get("source_branch"),
                "target_branch": triage_info.get("target_branch"),
                "commit_sha": triage_info.get("commit_sha"),
                "pipeline_id": triage_info.get("pipeline_id"),
                "trigger_type": triage_info.get("trigger_type", "manual_triage"),
                "failed_job_names": [
                    job.get("name") if isinstance(job, dict) else job
                    for job in triage_info.get("failed_jobs", [])
                ],
                "failure_categories": self._infer_categories(triage_info),
                "success": result.get("success", 0),
                "finish_reason": result.get("finish_reason", ""),
                "iterations": result.get("iterations"),
                "max_iterations": result.get("max_iterations"),
                "pushed_sha": result.get("pushed_sha"),
                "final_pipeline_status": result.get("final_pipeline_status", "unknown"),
                "final_coverage": result.get("final_coverage"),
                "failure_signatures": result.get("failure_signatures", []),
                "fix_duration_ms": int(result.get("processing_total_ms") or result.get("duration_ms") or 0),
                "model": None,
                "error": error,
                "extra": {
                    "selected_categories": list(getattr(self, "selected_categories", ())),
                    "duration_breakdown": result.get("duration_breakdown", {}),
                    "push_attempts": result.get("push_attempts", []),
                    "pipeline_groups": result.get("pipeline_groups", []),
                    "coverage_source": result.get("coverage_source", ""),
                    "coverage_status": result.get("coverage_status", ""),
                },
            }
            save_triage_run(record)
        except Exception as e:
            get_logger().error(f"Triage 结果落盘失败（不影响主流程）: {e}")

    def _infer_categories(self, triage_info: dict) -> list:
        """从 failed_job_names 启发式推断失败类型。"""
        names = [j.get("name", "") if isinstance(j, dict) else str(j) for j in triage_info.get("failed_jobs", [])]
        categories = []
        for name in names:
            low = name.lower()
            if "format" in low or "clang-format" in low:
                categories.append("format")
            elif "clang" in low:
                categories.append("clang")
            elif "build" in low or "compile" in low:
                categories.append("build")
            else:
                categories.append("unknown")
        return sorted(set(categories)) or ["unknown"]

    def _collect_triage_info(self) -> dict:
        """收集 MR + 流水线失败信息，传递给 UT Agent。"""
        # 获取 MR 基本信息
        mr_title = self.git_provider.pr.title
        mr_author = "unknown"
        author = getattr(self.git_provider.pr, "author", None)
        if isinstance(author, dict):
            mr_author = author.get("username", "unknown")
        elif author:
            mr_author = str(author)

        mr_id = getattr(self.git_provider.pr, "iid", 0)
        source_branch = self.git_provider.get_pr_branch()
        target_branch = getattr(self.git_provider.pr, "target_branch", "") if hasattr(self.git_provider, "pr") else ""
        project_id = getattr(self.git_provider, "id_project", "")

        # 尝试获取最近的流水线失败信息
        failed_jobs = []
        pipeline_id = None
        commit_sha = None

        try:
            failed_jobs, pipeline_id, commit_sha = self._fetch_failed_pipeline_info()
        except Exception as e:
            get_logger().warning(f"Triage: 获取流水线失败信息失败: {e}")

        try:
            diff_files = serialize_diff_files(self.git_provider)
        except Exception as e:
            get_logger().warning(f"Triage: 获取 MR diff 失败: {e}")
            diff_files = []

        return {
            "trigger_type": "pipeline_failed",
            "pr_url": self.pr_url,
            "title": mr_title,
            "author": mr_author,
            "mr_id": mr_id,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "project_id": project_id,
            "pipeline_id": pipeline_id,
            "commit_sha": commit_sha,
            "failed_jobs": failed_jobs,
            "diff_files": diff_files,
            "selected_categories": list(getattr(self, "selected_categories", ()) or ()),
        }

    def _fetch_failed_pipeline_info(self) -> tuple[list[dict], Optional[int], Optional[str]]:
        """从 GitLab API 获取最近的流水线失败 job 信息。"""
        project_id = getattr(self.git_provider, "id_project", "")
        source_branch = self.git_provider.get_pr_branch()

        if not project_id:
            return [], None, None

        project = self.git_provider.gl.projects.get(project_id)
        explicit_pipeline_id = getattr(self, "pipeline_id", None)
        if explicit_pipeline_id:
            pipeline = project.pipelines.get(explicit_pipeline_id)
            actual_sha = str(getattr(pipeline, "sha", None) or "")
            expected_sha = str(getattr(self, "pipeline_sha", "") or "")
            if expected_sha and actual_sha and actual_sha != expected_sha:
                raise ValueError(
                    f"指定 Pipeline #{explicit_pipeline_id} 的 SHA 已变化: {actual_sha[:12]} != {expected_sha[:12]}"
                )
        else:
            mr_id = getattr(self.git_provider.pr, "iid", 0)
            pipelines = []
            if mr_id:
                merge_request = project.mergerequests.get(mr_id)
                pipelines = merge_request.pipelines.list(get_all=True)
            if not pipelines:
                pipelines = project.pipelines.list(
                    ref=source_branch, order_by="id", sort="desc", per_page=5, get_all=False
                )
            if not pipelines:
                return [], None, None
            pipelines = sorted(pipelines, key=lambda candidate: candidate.id, reverse=True)
            pipeline = pipelines[0]

        from pr_agent.servers.gitlab_webhook import _get_failed_pipeline_jobs
        jobs_data = _get_failed_pipeline_jobs(project.id, pipeline.id)
        failed_jobs = [{
            "name": job.get("name", ""),
            "status": job.get("status", ""),
            "stage": job.get("stage", ""),
            "web_url": job.get("web_url", ""),
        } for job in jobs_data]

        selected_categories = set(getattr(self, "selected_categories", ()) or ())
        if selected_categories:
            from pr_agent.triage.failure_categories import categorize_failed_job

            failed_jobs = [
                job for job in failed_jobs if categorize_failed_job(job).value in selected_categories
            ]

        return failed_jobs, pipeline.id, getattr(pipeline, "sha", None)
