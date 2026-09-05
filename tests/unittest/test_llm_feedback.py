import asyncio

from pr_agent.algo.llm_feedback import format_llm_feedback_markdown, record_llm_feedback
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_mr_create import PRMrCreate


class TestLLMFeedback:
    def setup_method(self):
        get_settings().data = {}
        get_settings().config.response_language = "zh-CN"

    def test_record_rate_limit_feedback(self):
        record_llm_feedback(RuntimeError("429 Too Many Requests: rate limit exceeded"), context="review")

        markdown = format_llm_feedback_markdown(get_settings().data["llm_feedback"])

        assert "LLM 调用状态提示" in markdown
        assert "review" in markdown
        assert "限流" in markdown

    def test_record_feedback_redacts_credentials(self):
        record_llm_feedback(RuntimeError("Authorization: Bearer secret-token api_key=secret"))

        message = next(
            item["message"]
            for item in get_settings().data["llm_feedback"]
            if item["context"] == "LLM inference"
        )

        assert "secret-token" not in message
        assert "api_key=secret" not in message
        assert message == "Authorization: Bearer [REDACTED] api_key=[REDACTED]"

    def test_mr_create_collects_feedback_from_subtool(self):
        mr_create = PRMrCreate.__new__(PRMrCreate)
        mr_create.llm_feedback = []

        async def failing_tool():
            record_llm_feedback(TimeoutError("request timed out"))

        result = asyncio.run(mr_create._safe_tool_run("improve", failing_tool))

        assert result == ""
        assert mr_create.llm_feedback == [
            {
                "context": "improve",
                "type": "timeout",
                "message": "request timed out",
            }
        ]
