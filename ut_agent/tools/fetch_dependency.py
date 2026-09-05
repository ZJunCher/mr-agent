"""
fetch_dependency 工具 - 通过 GitLab API 获取依赖文件并落盘到 workspace。

此工具供 LLM 在生成 UT 时调用，一步完成：获取文件内容 → 写入本地 workspace。

=== LLM 调用说明 ===

工具名称: fetch_dependency_file

参数:
    file_path (str, 必填): 要获取的文件路径，相对于仓库根目录。
        示例: "src/modules/acc/acc_controller.h"
              "include/common/types.h"
              "CMakeLists.txt"

    branch (str, 可选): 从哪个分支获取，默认使用 MR 的源分支。

返回:
    成功: 返回落盘后的本地文件路径。
    失败: 返回以 "ERROR:" 开头的错误描述。

使用场景:
    1. 变更文件中 #include 了某个头文件，需要查看其接口定义
    2. 变更的类继承了基类，需要获取基类源码
    3. 需要查看 CMakeLists.txt 了解构建配置
    4. 需要查看已有测试文件，复用 fixture/helper

调用示例:
    fetch_dependency_file(file_path="src/modules/acc/acc_controller.h")
    fetch_dependency_file(file_path="test/modules/acc/test_acc.cpp", branch="develop")

=== 实现 ===
"""
import os
import re
from typing import Annotated, Optional

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from ut_agent.tools.context import (get_git_provider, get_output_dir,
                                    workspace_path)


def _write_dependency_file(
    content: str,
    file_path_in_repo: str,
    output_dir: str,
    mr_id: int,
    project_id: str = "",
) -> str:
    """将单个依赖文件内容落盘到 workspace/mr_{id}/deps/ 目录。"""
    deps_dir = workspace_path(output_dir, project_id, mr_id, "deps")
    os.makedirs(deps_dir, exist_ok=True)

    safe_name = file_path_in_repo.replace("/", os.sep).replace("\\", os.sep)
    file_path = os.path.join(deps_dir, safe_name)
    file_dir = os.path.dirname(file_path)
    if file_dir:
        os.makedirs(file_dir, exist_ok=True)

    with open(file_path, "w", encoding="utf-8") as fp:
        fp.write(content)

    return file_path


def extract_includes(source_content: str) -> list[str]:
    """
    从 C/C++ 源文件内容中提取 #include 的项目内文件路径。

    参数:
        source_content: 源文件的完整文本内容

    返回:
        include 路径列表（不含系统头文件如 <iostream>）
    """
    includes = []
    pattern = re.compile(r'#include\s+"([^"]+)"')
    for match in pattern.finditer(source_content):
        includes.append(match.group(1))
    return includes


def extract_imports(source_content: str, language: str) -> list[str]:
    """
    从源文件内容中提取 import/include 的模块路径。

    参数:
        source_content: 源文件的完整文本内容
        language: 文件语言 ("python", "cpp", 等)

    返回:
        依赖路径列表
    """
    if language == "python":
        imports = []
        pattern1 = re.compile(r'from\s+([\w.]+)\s+import')
        pattern2 = re.compile(r'^import\s+([\w.]+)', re.MULTILINE)
        for match in pattern1.finditer(source_content):
            imports.append(match.group(1))
        for match in pattern2.finditer(source_content):
            imports.append(match.group(1))
        return imports
    elif language in ("cpp", "c", "c++"):
        return extract_includes(source_content)
    return []


def resolve_include_path(include_path: str, source_file: str, search_dirs: Optional[list[str]] = None) -> list[str]:
    """
    根据 include 路径和源文件位置，推断可能的仓库内文件路径。

    参数:
        include_path: #include 中的路径（如 "acc_controller.h"）
        source_file: 包含该 include 的源文件路径
        search_dirs: 额外的搜索目录列表

    返回:
        可能的仓库相对路径列表（优先级从高到低）
    """
    candidates = []

    # 候选1：相对于源文件所在目录
    source_dir = os.path.dirname(source_file)
    if source_dir:
        candidates.append(os.path.join(source_dir, include_path).replace("\\", "/"))
    else:
        candidates.append(include_path)

    # 候选2：直接作为仓库根目录的相对路径
    candidates.append(include_path)

    # 候选3：在常见搜索目录下查找
    default_search_dirs = search_dirs or ["include", "src", "lib"]
    for d in default_search_dirs:
        candidates.append(f"{d}/{include_path}")

    # 去重保序
    seen = set()
    result = []
    for c in candidates:
        normalized = c.replace("\\", "/")
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)

    return result


def fetch_dependency(git_provider, file_path: str, output_dir: str, mr_id: int, branch: Optional[str] = None) -> str:
    """
    获取指定依赖文件并落盘到 workspace/mr_{id}/deps/ 目录。

    完整流程：通过 git provider API 获取文件内容 → 写入本地 workspace。

    参数:
        git_provider: pr-agent 的 git provider 实例
        file_path: 文件路径（相对于仓库根目录）
        output_dir: workspace 根目录
        mr_id: MR 编号
        branch: 分支名，默认使用 MR 源分支

    返回:
        成功: 本地文件路径
        失败: 以 "ERROR:" 开头的错误信息
    """
    if branch is None:
        branch = git_provider.get_pr_branch()

    try:
        content = git_provider.get_pr_file_content(file_path, branch)
        if not content:
            return f"ERROR: 文件不存在或为空: {file_path} (branch: {branch})"

        # 直接落盘
        project_id = getattr(git_provider, "id_project", "")
        local_path = _write_dependency_file(content, file_path, output_dir, mr_id, project_id)
        return local_path

    except Exception as e:
        return f"ERROR: 获取文件失败: {file_path} (branch: {branch}), 原因: {e}"


def fetch_dependencies_batch(git_provider, file_paths: list[str], output_dir: str, mr_id: int, branch: Optional[str] = None) -> dict[str, str]:
    """
    批量获取多个依赖文件并落盘。

    参数:
        git_provider: pr-agent 的 git provider 实例
        file_paths: 文件路径列表
        output_dir: workspace 根目录
        mr_id: MR 编号
        branch: 分支名

    返回:
        {repo_path: local_path_or_error} 字典
    """
    results = {}
    for fp in file_paths:
        results[fp] = fetch_dependency(git_provider, fp, output_dir, mr_id, branch)
    return results


def auto_fetch_dependencies(git_provider, source_content: str, source_file: str, language: str, output_dir: str, mr_id: int, branch: Optional[str] = None) -> dict[str, str]:
    """
    自动解析源文件中的依赖引用，获取并落盘。

    完整流程：解析 #include/import → 推断路径 → 逐个尝试获取 → 落盘。

    参数:
        git_provider: pr-agent 的 git provider 实例
        source_content: 源文件的完整文本内容
        source_file: 源文件路径（用于推断相对路径）
        language: 文件语言
        output_dir: workspace 根目录
        mr_id: MR 编号
        branch: 分支名

    返回:
        {resolved_path: local_path_or_error} 字典
    """
    if language in ("cpp", "c", "c++"):
        includes = extract_includes(source_content)
    else:
        includes = extract_imports(source_content, language)

    results = {}
    for inc in includes:
        candidates = resolve_include_path(inc, source_file)
        found = False
        for candidate in candidates:
            result = fetch_dependency(git_provider, candidate, output_dir, mr_id, branch)
            if not result.startswith("ERROR:"):
                results[candidate] = result
                found = True
                break
        if not found:
            results[inc] = f"ERROR: 无法在任何候选路径中找到文件: {inc}"

    return results


@tool
def fetch_dependency_file(file_path: str, state: Annotated[dict, InjectedState]) -> str:
    """从 GitLab 获取指定依赖文件并落盘到 workspace。

    通过 GitLab API 获取文件内容，写入 workspace/mr_{id}/deps/ 目录。
    用于获取变更文件所依赖的头文件、基类、CMakeLists.txt 等。

    Args:
        file_path: 要获取的文件路径，相对于仓库根目录。
            示例: "src/modules/acc/acc_controller.h"
                  "CMakeLists.txt"
                  "test/modules/acc/test_acc.cpp"

    返回: 成功时返回本地文件路径，失败时返回错误描述。
    """
    git_provider = get_git_provider()
    output_dir = get_output_dir()
    mr_id = state["mr_id"]
    branch = state["source_branch"]

    return fetch_dependency(git_provider, file_path, output_dir, mr_id, branch)


@tool
def auto_fetch_deps(source_file: str, state: Annotated[dict, InjectedState]) -> str:
    """自动解析指定源文件的依赖并全部获取落盘。

    解析源文件中的 #include / import 语句，推断仓库内路径，
    逐个获取并落盘到 workspace/mr_{id}/deps/ 目录。

    Args:
        source_file: 源文件路径（仓库相对路径），需要是 changed_files 中存在的文件。
            示例: "src/modules/acc/acc_controller.cpp"

    返回: 每个依赖的获取结果（路径或错误），换行分隔。
    """
    git_provider = get_git_provider()
    output_dir = get_output_dir()
    mr_id = state["mr_id"]
    branch = state["source_branch"]
    diff_files = state["diff_files"]

    # 从 diff_files 中找到对应文件的 head_file 内容
    source_content = None
    language = "unknown"
    for f in diff_files:
        if f["filename"] == source_file:
            source_content = f.get("head_file", "")
            language = f.get("language", "unknown")
            break

    if not source_content:
        return f"ERROR: 未找到文件 {source_file} 的源码内容"

    results = auto_fetch_dependencies(
        git_provider, source_content, source_file, language, output_dir, mr_id, branch
    )

    lines = []
    for path, result in results.items():
        lines.append(f"{path} -> {result}")
    return "\n".join(lines) if lines else "未检测到依赖文件。"
