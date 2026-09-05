from pr_agent.distributed.models import TriageCardState
from pr_agent.distributed.notifications import DirectFeishuNotificationSink
from pr_agent.feishu.triage_card import parse_mr_identity
from pr_agent.log import get_logger


class DummyComment:
    id = "dummy_id"

    def __init__(self, body: str):
        self.body = body


class FeishuGitProvider:
    """Proxy a Git provider while routing user-facing output through a notification sink."""

    def __init__(
        self,
        original_provider,
        feishu_sender_id,
        mr_url: str = "",
        notification_sink=None,
        task_id: str = "",
        correlate_triage: bool = False,
    ):
        self.original_provider = original_provider
        self.feishu_sender_id = feishu_sender_id
        self.mr_url = mr_url or getattr(original_provider, "pr_url", "") or ""
        self.notification_sink = notification_sink or DirectFeishuNotificationSink()
        self.task_id = task_id
        self.correlate_triage = correlate_triage
        self.project_id, self.mr_iid = self._mr_identity()

    def __getattr__(self, name):
        return getattr(self.original_provider, name)

    @staticmethod
    def _infer_card_title(pr_comment: str):
        first_line = pr_comment.lstrip().splitlines()[0].lower() if pr_comment else ""
        if any(keyword in first_line for keyword in ("review", "审阅", "审查")):
            return "PR Review 代码审阅", "blue"
        if any(keyword in first_line for keyword in ("describe", "description", "描述")):
            return "PR Description MR 描述", "wathet"
        if any(keyword in first_line for keyword in ("improve", "suggestion", "建议", "code suggestion")):
            return "PR Improve 代码建议", "green"
        return "PR-Agent 结果", "blue"

    def _mr_identity(self) -> tuple[str, int]:
        project_id = getattr(self.original_provider, "id_project", "")
        project_id = project_id if isinstance(project_id, str) else ""
        raw_iid = getattr(getattr(self.original_provider, "pr", None), "iid", 0)
        try:
            mr_iid = int(raw_iid)
        except (TypeError, ValueError):
            mr_iid = 0
        try:
            identity = parse_mr_identity(self.mr_url)
        except ValueError:
            return project_id, mr_iid
        self.mr_url = identity.mr_url
        return identity.project_id, identity.mr_iid

    def _identified_title(self, title: str) -> str:
        if title.startswith("【") or not self.project_id or not self.mr_iid:
            return title
        return f"【{self.project_id} !{self.mr_iid}】{title}"

    def _identified_content(self, content: str) -> str:
        if not self.mr_url or self.mr_url in content:
            return content
        label = f"{self.project_id} !{self.mr_iid}" if self.project_id and self.mr_iid else "MR"
        return f"**MR:** [{label}]({self.mr_url})\n\n{content}"

    def _publish_card_update(self, state: TriageCardState, status_markdown: str) -> bool:
        if not self.correlate_triage:
            return False
        publisher = getattr(self.notification_sink, "publish_card_update", None)
        if not callable(publisher):
            return False
        return bool(publisher(state=state, status_markdown=status_markdown))

    def _publish_triage_result(self, state: TriageCardState, status_markdown: str, details: dict) -> bool:
        if not self.correlate_triage:
            return False
        publisher = getattr(self.notification_sink, "publish_triage_result", None)
        if not callable(publisher):
            return False
        title, header_template = {
            TriageCardState.REPAIR_SUCCEEDED: ("修复成功", "green"),
            TriageCardState.REPAIR_PARTIAL: ("部分修复成功", "orange"),
            TriageCardState.REPAIR_BLOCKED: ("外部依赖阻塞", "orange"),
            TriageCardState.REPAIR_FAILED: ("修复失败", "red"),
        }[state]
        return bool(
            publisher(
                state=state,
                status_markdown=status_markdown,
                receive_id=self.feishu_sender_id,
                content=self._identified_content(status_markdown),
                title=self._identified_title(title),
                header_template=header_template,
                mr_url=self.mr_url,
                details=details,
            )
        )

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        try:
            if is_temporary and self.correlate_triage:
                self._publish_card_update(TriageCardState.REPAIR_RUNNING, pr_comment)
                return DummyComment(pr_comment)
            title, color = self._infer_card_title(pr_comment)
            self.notification_sink.publish_markdown(
                receive_id=self.feishu_sender_id,
                content=self._identified_content(pr_comment),
                title=self._identified_title(title),
                header_template=color,
                mr_url=self.mr_url,
            )
            return DummyComment(pr_comment)
        except Exception as error:
            get_logger().error(f"Error in FeishuGitProvider.publish_comment: {error}")
            return None

    def edit_comment(self, comment, body: str):
        return self.publish_comment(body)

    def remove_comment(self, comment):
        get_logger().info("Intercepted remove_comment, ignoring for Feishu")

    def publish_code_suggestions(self, code_suggestions: list):
        markdown_body = "### Code Suggestions\n\n"
        for suggestion in code_suggestions:
            body = suggestion.get("body", "")
            reason = suggestion.get("reason", "")
            file = suggestion.get("relevant_file", "")
            markdown_body += f"**File:** {file}\n**Suggestion:**\n{body}\n"
            if reason:
                markdown_body += f"**Reason:** {reason}\n"
            markdown_body += "---\n"
        self.notification_sink.publish_markdown(
            receive_id=self.feishu_sender_id,
            content=self._identified_content(markdown_body),
            title=self._identified_title("PR Improve 代码建议"),
            header_template="green",
            mr_url=self.mr_url,
        )

    def publish_labels(self, labels: list):
        self.notification_sink.publish_text(
            receive_id=self.feishu_sender_id,
            content=self._identified_content(f"Generated labels: {', '.join(labels)}"),
            mr_url=self.mr_url,
        )

    def publish_description(self, pr_title: str, pr_body: str):
        self.notification_sink.publish_markdown(
            receive_id=self.feishu_sender_id,
            content=self._identified_content(f"**Title:** {pr_title}\n\n**Description:**\n{pr_body}"),
            title=self._identified_title("PR Description MR 描述"),
            header_template="wathet",
            mr_url=self.mr_url,
        )

    def remove_initial_comment(self):
        get_logger().info("Intercepted remove_initial_comment, ignoring for Feishu")

    def publish_triage_result(self, content: str, *, success: bool, details: dict) -> None:
        status_markdown = self._format_triage_result(content, details)
        repair_outcome = str(details.get("repair_outcome") or "").strip().lower()
        state = {
            "success": TriageCardState.REPAIR_SUCCEEDED,
            "partial_success": TriageCardState.REPAIR_PARTIAL,
            "blocked": TriageCardState.REPAIR_BLOCKED,
            "failed": TriageCardState.REPAIR_FAILED,
        }.get(
            repair_outcome,
            TriageCardState.REPAIR_SUCCEEDED if success else TriageCardState.REPAIR_FAILED,
        )
        if self._publish_triage_result(state, status_markdown, details):
            return
        title, header_template = {
            TriageCardState.REPAIR_SUCCEEDED: ("修复成功", "green"),
            TriageCardState.REPAIR_PARTIAL: ("部分修复成功", "orange"),
            TriageCardState.REPAIR_BLOCKED: ("外部依赖阻塞", "orange"),
            TriageCardState.REPAIR_FAILED: ("修复失败", "red"),
        }[state]
        self.notification_sink.publish_markdown(
            receive_id=self.feishu_sender_id,
            content=self._identified_content(status_markdown),
            title=self._identified_title(title),
            header_template=header_template,
            mr_url=self.mr_url,
        )

    @staticmethod
    def _format_triage_result(content: str, details: dict) -> str:
        from pr_agent.triage.pipeline_coverage import coverage_label, coverage_unavailable_reason, normalize_coverage

        lines = [content.strip()]
        pushed_sha = str(details.get("pushed_sha") or "").strip()
        push_attempts = details.get("push_attempts") or []
        pipeline_groups = details.get("pipeline_groups") or []
        pipeline_status = str(details.get("final_pipeline_status") or "unknown").strip()
        coverage = details.get("final_coverage")
        coverage_source = str(details.get("coverage_source") or "").strip()
        coverage_status = str(details.get("coverage_status") or "").strip()
        error = str(details.get("error") or "").strip()
        duration_ms = details.get("processing_total_ms", details.get("duration_ms"))
        if push_attempts:
            for attempt in push_attempts:
                if not isinstance(attempt, dict) or not attempt.get("commit_sha"):
                    continue
                sequence = attempt.get("attempt_sequence") or "?"
                lines.append(f"- 修复提交 {sequence}: `{attempt['commit_sha']}`")
        elif pushed_sha:
            lines.append(f"- Commit: `{pushed_sha}`")
        for index, group in enumerate(pipeline_groups, start=1):
            if not isinstance(group, dict):
                continue
            root_id = group.get("root_pipeline_id") or "?"
            validation_id = group.get("validation_pipeline_id") or "?"
            status = group.get("status") or "unknown"
            lines.append(f"- 流水线 {index}: root `{root_id}` / validation `{validation_id}` / `{status}`")
        lines.append(f"- Pipeline: `{pipeline_status}`")
        coverage_value = normalize_coverage(str(coverage).strip().removesuffix("%") if coverage is not None else None)
        coverage_text = (
            f"{coverage_value:g}%"
            if coverage_value is not None
            else coverage_unavailable_reason(coverage_status)
        )
        lines.append(f"- {coverage_label(coverage_source)}：{coverage_text}")
        if duration_ms is not None:
            lines.append(f"- 处理总耗时: {FeishuGitProvider._format_duration(int(duration_ms))}")
        breakdown = details.get("duration_breakdown") or {}
        if isinstance(breakdown, dict):
            same_mr_wait = int(breakdown.get("same_mr_wait_ms") or 0)
            queue_ms = int(breakdown.get("queue_duration_ms") or 0)
            context_ms = int(breakdown.get("context_duration_ms") or 0)
            hermes_ms = int(breakdown.get("hermes_duration_ms") or 0)
            git_ms = int(breakdown.get("git_publish_duration_ms") or 0)
            pipeline_wait = int(breakdown.get("pipeline_wait_duration_ms") or 0)
            post_pipeline = int(breakdown.get("post_pipeline_duration_ms") or 0)
            if same_mr_wait:
                lines.append(f"- 同 MR 等待: {FeishuGitProvider._format_duration(same_mr_wait)}")
            repair_ms = context_ms + hermes_ms + git_ms
            if duration_ms is not None:
                repair_ms = max(
                    repair_ms,
                    int(duration_ms) - queue_ms - same_mr_wait - pipeline_wait - post_pipeline,
                )
            if repair_ms:
                lines.append(
                    f"- 诊断与修复: {FeishuGitProvider._format_duration(repair_ms)}"
                    f"（Hermes {FeishuGitProvider._format_duration(hermes_ms)}）"
                )
            if pipeline_wait:
                lines.append(f"- 流水线等待: {FeishuGitProvider._format_duration(pipeline_wait)}")
            if post_pipeline:
                lines.append(f"- 结果处理: {FeishuGitProvider._format_duration(post_pipeline)}")
        if pipeline_groups:
            remaining = (pipeline_groups[-1] or {}).get("failed_jobs") or []
            if remaining:
                lines.append(f"- 剩余失败 jobs: {', '.join(str(job) for job in remaining)}")
        if error and error not in content:
            lines.append(f"- 错误: {error}")
        return "\n\n".join(line for line in lines if line)

    @staticmethod
    def _format_duration(duration_ms: int) -> str:
        total_seconds = max(0, int(duration_ms) // 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h{minutes:02d}m{seconds:02d}s"
        if minutes:
            return f"{minutes}m{seconds:02d}s"
        return f"{seconds}s"

    def publish_persistent_comment(
        self,
        pr_comment: str,
        initial_header: str,
        update_header: bool = True,
        name: str = "review",
        final_update_message: bool = True,
    ):
        return self.publish_comment(pr_comment)
