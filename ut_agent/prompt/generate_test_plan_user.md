# Generate Test Plan - User Prompt

## MR 信息

- **标题**: {title}
- **作者**: {author}
- **MR ID**: !{mr_id}
- **源分支**: {source_branch}
- **目标分支**: {target_branch}

## 仓库本地路径

{repo_path}

## Diff 分析结果

以下是上一步对本 MR diff 的结构化分析结果：

```json
{diff_analysis}
```

## 项目上下文（如有）

{project_context}

## 要求

请基于以上分析结果，生成完整的单元测试实现计划。确保：
1. 每个 testable_unit 的每个 branch 都有对应的测试用例
2. 每个 edge_case 都有对应的测试用例
3. Mock 策略针对 mock_targets 中列出的每个对象给出具体方案
4. test_fixtures_needed 中的每个 fixture 都体现在计划中
5. 输出严格遵循 system prompt 中定义的 JSON 格式
