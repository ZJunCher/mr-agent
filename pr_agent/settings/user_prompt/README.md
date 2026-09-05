# 用户自定义 Prompt 指南

请将您自定义的 `.toml` prompt 文件放置在此目录 (`pr_agent/settings/user_prompt/`) 下。
这些文件将在默认配置文件之后加载，从而覆盖任何默认设置。

## 工作原理

1.  **文件命名**：您可以随意命名文件，只要以 `.toml` 结尾即可。
    -   示例：`my_custom_review.toml`、`team_guidelines.toml`。
2.  **配置节匹配 (Section Matching)**：要覆盖特定工具的 prompt，您必须使用与默认设置中定义相同的**配置节名称 (Section Name)**。
    -   虽然如果文件名与默认文件（如 `pr_reviewer_prompts.toml`）匹配也能直接覆盖，但为了安全起见，建议依赖明确的配置节名称。
    -   加载机制会扫描此目录下的*所有* `.toml` 文件。

## 常用配置节名称

以下是常用工具使用的配置节名称。请在您的自定义 TOML 文件中使用这些标题来覆盖其行为：

-   **/review**: `[pr_reviewer_prompt]`
-   **/describe**: `[pr_description_prompt]`
-   **/improve**: `[pr_code_suggestions_prompt]`
-   **/ask**: `[pr_questions_prompt]`
-   **/generate_labels**: `[pr_custom_labels_prompt]`

## ⚠️ 重要提示：保留关键占位符

在自定义 prompt 时，**务必保留原始 prompt 中的关键 Jinja2 占位符**（如 `{{ diff }}`、`{{ title }}` 等）。如果缺少这些占位符，模型将无法获取 PR 的代码变更或元数据，导致无法正常工作。

请参考默认的 `pr_agent/settings/*_prompts.toml` 文件来确认需要保留哪些变量。

## 示例

假设您想自定义 `/review` 的 prompt，可以在此目录下创建一个名为 `my_review_prompt.toml` 的文件，内容如下：

```toml
[pr_reviewer_prompt]
system = """
你是一位专注于安全漏洞的高级代码审查员。
请重点检查代码中的 SQL 注入、XSS 和权限绕过风险。

...
"""
user = """
PR 分析：
标题：{{ title }}
分支：{{ branch }}

PR Diff：
======
{{ diff }}
======

请基于上述 Diff 进行审查。
"""
```

*(注意：上面的 `{{ title }}`, `{{ branch }}`, `{{ diff }}` 是必须保留的占位符，否则 Agent 无法读取代码)*

## 加载顺序

1.  默认配置 `pr_agent/settings/*.toml`
2.  `pr_agent/settings/user_prompt/*.toml` (本目录)
3.  仓库根目录 `.pr_agent.toml` (优先级最高)
