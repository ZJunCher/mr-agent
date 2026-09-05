# CI Blocker Verification Task

你负责验证当前失败 job 是否确实无法通过当前仓库的安全修改解决。本操作只读，不得修改文件。

## 强制规则

1. 重新检查当前 CI 日志、源码使用点、构建配置、依赖来源、子模块和仓库内替代方案。
2. 不接受“包不在仓库”作为单独证据；必须说明为什么删除、替换或调整该依赖会破坏实际源码。
3. 禁止创建、修改、删除或格式化文件；禁止执行 git add、commit、push、reset 或 checkout。
4. 不查询 Revert 历史、不获取旧提交 diff、不寻找或复用以前的修复答案。
5. 不输出或记录密钥、Token、认证 URL 等敏感信息。
6. 如果存在任何合理的仓库内修复，不能输出 blocked；报告该修复方向并说明 blocker 验证失败。
7. 流水线修复期间 `.git` 元数据会被外层系统临时隔离；这是正常安全措施，不能作为 blocker 证据。

## 最终输出格式

只有确认仓库内没有安全修复时，最终响应末尾才能输出下面两个标记及其中的一个 JSON 对象。不要使用
Markdown 代码围栏。JSON 保持紧凑，所有字段必须有具体证据：

BEGIN_TRIAGE_BLOCKER_JSON
{
  "schema_version": 1,
  "outcome": "blocked",
  "job_name": "当前失败 job 的精确名称",
  "blocker_type": "external_dependency | ci_environment | provider_outage | permissions | missing_required_input | unsupported_repository_state",
  "root_cause": "确定的根因",
  "ci_evidence": [
    {"job_name": "当前失败 job 的精确名称", "observation": "当前 CI 日志中的具体证据"}
  ],
  "repository_evidence": [
    {"kind": "证据类型", "locator": "具体路径、行号、构建项或搜索", "observation": "具体发现"}
  ],
  "attempted_repairs": ["已实际评估但不可行的仓库内修复方案"],
  "why_no_safe_repo_change": "为什么所有合理仓库修改都不安全或不能解决当前失败",
  "suggested_action": "作者可以执行的最小人工处理；不能建议不安全的仓库修改"
}
END_TRIAGE_BLOCKER_JSON

若证据不完整或仍存在仓库内修复方向，不要输出上述标记；直接报告还缺什么证据或应尝试什么修复。
