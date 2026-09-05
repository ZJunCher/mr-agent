# Prompt Evolution 运维手册

Prompt Evolution 每周汇总 `/improve` 建议的真实结果，把反复出现的问题整理成受限的 Prompt 修改，并且最多创建一个待人工审核的 Draft MR。它不会自动合并、部署或热更新 Prompt。

## 示例部署配置

- 目标项目：`example-group/example-project`
- 目标分支：`main`
- 调度时间：每周一 03:00，`Asia/Shanghai`
- 独立服务：`pr-agent-prompt-evolution`，固定单副本
- Redis：只使用 `PR_AGENT_REDIS_URL`
- 证据库：复用 `/app/data` 中的建议反馈 SQLite
- 模型顺序：`anthropic/claude-sonnet-5` → `anthropic/claude-opus-4-8` → `anthropic/claude-opus-4-6`

Scheduler 在周一 03:00 后启动会补跑本周任务。Redis 租约和 SQLite 批次记录共同保证同一周不会重复创建分支、Commit 或 MR；本版本不迁移、不恢复上线前的旧 Prompt Evolution 任务。

## 证据和修改范围

只有带完整 `global_prompt_set_hash`、`project_rules_hash` 和 `prompt_bundle_hash` 的建议才会成为证据。进入聚类的结果只有：

- `accepted`：建议被应用；
- `rejected`：建议被明确解决/拒绝；
- `unhandled`：MR 已合并或建议超过观察期仍未处理。

`pending` 和 `invalid` 会保留在快照中，但不会影响 Prompt。

全局经验只允许修改 `/improve` 的白名单 Prompt TOML；项目级经验只允许修改对应项目的规则 TOML。项目规则可以用 `languages = ["python"]` 或 `languages = ["cpp"]` 隔离，混合语言 MR 的两次 Prompt 调用分别读取自己的规则。

## 上线前 dry-run

先确保 Redis 已启动且容器能读取生产 env 和 `/app/data`，然后执行：

```bash
docker compose run --rm --no-deps pr-agent-prompt-evolution \
  python scripts/prompt_evolution_weekly.py --dry-run
```

命令只输出批次 ID、状态、MR URL、base SHA 和有界错误码，不输出反馈正文或密钥。可接受的首次结果：

- `completed_no_change`：流程正常，但当前没有达到门槛的有效候选；这是安全成功，不是故障；
- `dry_run_validated`：已生成并通过确定性校验的方案，但没有写 GitLab；
- `failed_retryable`：查看 `error_code` 和容器日志，修复环境后可重试同一批次。

dry-run 不创建分支、Commit、MR，也不推进生产 watermark。

## 启动和检查

```bash
docker compose up -d --no-deps --force-recreate pr-agent-prompt-evolution
docker compose ps pr-agent-prompt-evolution
docker compose logs --since=10m --tail=200 pr-agent-prompt-evolution
docker compose exec -T pr-agent-prompt-evolution \
  python -m pr_agent.suggestions.prompt_evolution.scheduler --healthcheck
```

心跳每 30 秒更新一次，超过 120 秒视为不健康。心跳循环与模型任务相互独立，因此模型调用或 GitLab I/O 很慢时，健康检查仍应持续通过。

## Draft MR 人工验收

自然证据达到门槛后，确认：

1. 标题以 `Draft:` 开头，目标分支是 `main`；
2. 修改文件全部属于 Prompt 白名单；
3. 项目规则的 `project` 和 `languages` 与证据一致；
4. 描述包含证据 ID、静态校验和“离线行为评估未执行”的说明；
5. 服务没有调用 merge、deploy 或 Prompt 热更新。

MR 合并后仍需由人工部署 PR-Agent。新版本运行后，新 `/improve` 建议才会记录新的 Prompt 归因哈希。

## 状态和排障

| 状态/错误码 | 含义 | 处理 |
| --- | --- | --- |
| `completed_no_change` | 无新信号或有效候选为 0 | 无需处理，等待后续自然证据 |
| `dry_run_validated` | dry-run 方案校验通过 | 可以启动正式 Scheduler |
| `mr_open` | Draft MR 已创建或已找到 | 人工审核 MR |
| `target_prompt_version_mismatch` | 运行容器与目标分支 Prompt 不一致 | 先同步并重新部署目标分支 |
| `project_prompt_version_mismatch` | 项目证据来自旧规则版本 | 候选被安全忽略，不修改 Prompt |
| `clustering_models_unavailable` | 三个模型均暂时不可用 | Scheduler 15 分钟后重试 |
| `lease_lost` | Redis 续租失败 | 不会写 GitLab，检查 Redis 后重试 |
| `proposal_validation_failed` | 两次方案均未通过确定性边界 | 查看存储的校验错误，人工修正 |

## 停止与回滚

仅停止新服务即可关闭自动整理，不影响 Web、Agent、飞书、CI Triage 或反馈采集：

```bash
docker compose stop pr-agent-prompt-evolution
```

不要清空 Redis 或删除共享 SQLite。若已合并的 Prompt 修改效果不好，应在 GitLab revert 对应 MR，再人工部署；服务本身不会自动回滚已合并内容。

离线回放/Holdout 行为门禁仍延后到 2026 年 9 月或更晚，Draft MR 会明确标注该限制。
