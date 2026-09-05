# CI Triage 结果统计与看板

CI 自动修复 triage 每次运行的结果（成功/失败/异常）会落盘到 `review_feedback.db` 的 `triage_runs` 表。

## 看板访问

看板部署在服务器上，与 inline suggestion 看板同一个 FastAPI app，刷新自动更新（实时查 SQLite，无需 sync/重新生成）。

**访问地址**（生产）：
- `http://localhost:8080/dashboard` — 看板索引页
- `http://localhost:8080/dashboard/triage` — CI triage 结果看板

> 注意：看板路由部署在 GitLab webhook 服务里，需要重新构建并部署 Docker 镜像后才能访问。

## 看板维度

- 总修复次数、成功率、平均修复轮数、平均修复耗时
- 按失败类型（format/clang/build/unknown）成功率柱状图
- 周成功率趋势折线图
- 最近 50 条运行明细表（时间/项目/MR链接/失败类型/成功✅❌/轮数/耗时）

## 数据流

1. pipeline 失败 → 第一张飞书卡 → 用户点按钮；Web 把 `/triage` 任务持久化到 Redis 后立即确认回调
2. Agent worker 读取任务，记录根因分组、Hermes 调用、push attempt 和 root/validation pipeline group
3. 等待流水线时任务写入 `waiting_pipeline` 并释放子进程执行槽；GitLab terminal Webhook 只唤醒精确匹配的 task/attempt/pipeline
4. `PRTriage` 到达终态后按 `task_id` 幂等写入 `triage_runs`；worker 重启或多次 resume 会更新同一行，不会新增重复记录
5. 第一张卡更新为终态，同时只发送一张新的完整终态卡；通知成功时间继续记录在 Redis lifecycle 中
6. 看板路由 `/api/triage/summary` 实时查 `triage_runs` 返回 JSON，浏览器加载 `/dashboard/triage` 后渲染

## 字段说明

| 字段 | 说明 |
|---|---|
| `success` | 0/1，来自 `validate_finish` 判定 |
| `iterations` | Agent 实际迭代轮数 |
| `failure_categories` | 失败类型 JSON 数组 |
| `failure_signatures` | 失败签名 JSON 数组 |
| `pushed_sha` | Agent 最后推送的 commit；完整多轮 commit 列表在 `extra_json.push_attempts` |
| `final_pipeline_status` | validation child pipeline 的最终状态，不使用只有 `generate_joblist` 的父流水线代替 |
| `final_coverage` | validation pipeline 报告的覆盖率；无值时看板显示 `—` |
| `fix_duration_ms` | 从飞书点击任务创建到 triage 终态持久化的总处理时间，不是最后一次 resume 的耗时 |
| `task_id` | Redis 任务 ID，也是 SQLite 幂等更新键 |
| `extra_json.duration_breakdown` | queue、同 MR 等待、Hermes、git publish、pipeline wait、post-pipeline 等分段耗时 |
| `extra_json.pipeline_groups` | 每次尝试对应的 root pipeline、validation child pipeline、状态与 coverage 来源 |
| `extra_json.coverage_status` | 覆盖率缺失原因，例如未配置 coverage Job、Job 失败或报告缺失 |

`final_coverage` 为空不等于“覆盖率为 0”。只有 GitLab pipeline 或 coverage Job 真正提供数值时才写入百分比；否则终态卡和
`coverage_status` 会明确说明为什么未提供，避免把“流水线绿灯”错误解释为“已得到覆盖率数值”。
