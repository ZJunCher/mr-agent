# Plan Fix - User Prompt

## 原始测试目标

本次 UT Agent 的任务是为 MR !{mr_id} 生成单元测试。测试计划摘要：
```
{test_plan_summary}
```

## 当前失败信息

**失败类型:** {failure_type}
**失败原因:** {failure_reason}
**当前修复轮次:** 第 {iteration} 轮（最多 {max_iterations} 轮）

### CI 错误日志
```
{evidence}
```

## 历史修复记录

{fix_history_section}

## 任务

请根据以上信息，生成本轮的修复计划 JSON。

要求：
1. 如果有历史修复记录，必须分析之前为什么失败，并采用不同的修复策略
2. 如果判定为业务源码问题且无法通过测试侧规避，标注 unfixable=true
3. fix_steps 中的文件路径必须基于实际仓库结构
4. instructions 字段要足够具体，让 coding agent 能直接执行
