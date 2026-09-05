"""
ut_agent prompt 模板管理。
"""
import os


PROMPT_DIR = os.path.dirname(__file__)


def load_prompt(name: str) -> str:
    """加载指定名称的 prompt 文件内容。"""
    path = os.path.join(PROMPT_DIR, f"{name}.md")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
