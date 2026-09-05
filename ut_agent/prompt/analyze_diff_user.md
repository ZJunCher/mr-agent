# Analyze Diff - User Prompt Template

## MR 信息

- **标题**: {title}
- **作者**: {author}
- **MR 编号**: !{mr_id}
- **源分支**: {source_branch}
- **目标分支**: {target_branch}

## 变更文件列表

共 {file_count} 个文件变更：

{file_list}

## Diff 详情

{diff_content}

## 已有测试文件（豁免参考）

以下是本次 MR 中同时提交的测试文件变更。这些测试已覆盖的源代码行**无需再生成额外测试**，请在分析时将其标记为已覆盖并降低优先级。

{existing_test_context}
