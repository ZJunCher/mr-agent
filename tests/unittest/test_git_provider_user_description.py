from pr_agent.algo.utils import process_description
from pr_agent.git_providers.git_provider import GitProvider


class DummyGitProvider(GitProvider):
    def __init__(self, description: str):
        self._description = description
        self.user_description = None

    def is_supported(self, capability: str) -> bool:
        return True

    def get_files(self) -> list:
        return []

    def get_diff_files(self) -> list:
        return []

    def publish_description(self, pr_title: str, pr_body: str):
        return None

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        return True

    def get_languages(self):
        return {}

    def get_pr_branch(self):
        return "main"

    def get_user_id(self):
        return "test-user"

    def get_pr_description_full(self) -> str:
        return self._description

    def get_repo_settings(self):
        return ""

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        return None

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        return None

    def publish_inline_comments(self, comments: list[dict]):
        return None

    def remove_initial_comment(self):
        return None

    def remove_comment(self, comment):
        return None

    def get_issue_comments(self):
        return []

    def publish_labels(self, labels):
        return None

    def get_pr_labels(self, update=False):
        return []

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False):
        return None

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return True

    def get_commit_messages(self):
        return ""


def test_get_user_description_returns_empty_for_generated_chinese_description_without_user_block():
    provider = DummyGitProvider(
        "### **描述**\n"
        "## 变更说明\n"
        "- 更新描述模板\n"
    )

    assert provider.get_user_description() == ""


def test_get_user_description_extracts_only_original_user_text_before_chinese_generated_sections():
    provider = DummyGitProvider(
        "### **User Description**\n"
        "用户手写说明\n\n"
        "___\n\n"
        "### **描述**\n"
        "## 变更说明\n"
        "- 更新描述模板\n\n"
        "___\n\n"
        "### 图示详解\n\n"
        "```mermaid\nflowchart LR\n```\n"
    )

    assert provider.get_user_description() == "用户手写说明"


def test_generated_chinese_header_is_recognized_as_pr_agent_output():
    provider = DummyGitProvider("")

    assert provider._is_generated_by_pr_agent("### **描述**\n内容") is True


def test_process_description_supports_new_chinese_file_walkthrough_heading_format():
    description, files = process_description(
        "### **描述**\n"
        "## 变更说明\n"
        "- 更新描述模板\n\n"
        "___\n\n"
        "### **文件详解**\n\n"
        "<details> <summary>展开查看文件列表</summary>\n\n"
        "<table><tbody><tr><td>data</td></tr></tbody></table>\n\n"
        "</details>\n"
    )

    assert "文件详解" not in description
    assert files == []