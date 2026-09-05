# Repair Memory 的 BGE-M3 混合检索

UT-Agent 使用一份内部 BGE-M3 模型，为项目经验和通用经验生成向量。在线检索将语义相似度与报错指纹、
失败类型、关键报错词、语言、构建系统、项目范围及经验质量共同评分，最多向修复上下文注入 3 条中文经验。
模型只负责召回候选，不负责修改代码、提交 Git 或决定 Pipeline 结果。

## 部署边界

- 全部 Agent Worker 共用一个 `bge-m3-service`，不会在每个 Worker 内分别加载模型。
- 服务只加入 Compose 内部网络，不配置 `ports`，外部不能直接访问。
- Agent 不强依赖模型服务启动；服务超时、停止或版本不匹配时自动退回规则检索。
- `retrieval_mode = "off"` 是总开关，关闭后不请求模型、不检索经验，也不影响 CI 修复主流程。
- 模型输入来自已清洗的当前 CI 证据和经验正文，不发送项目 Token，也不读取生产数据库。

服务器需要为模型缓存预留 5–8GiB 磁盘，为单个 CPU 模型服务预留约 3–5GiB 内存。模型目录固定为
`${MR_AGENT_HOME:-./runtime}/data/models`，目录应由部署用户创建并授予 Docker 读写权限。

## 上线步骤

执行资源操作前，先备份 Repair Memory 数据库：

~~~bash
cp ${MR_AGENT_HOME:-./runtime}/data/feedback/repair_memory.db \
  ${MR_AGENT_HOME:-./runtime}/data/feedback/repair_memory.db.$(date +%Y%m%d%H%M%S).bak
~~~

构建镜像并使用一次性初始化容器下载固定 revision。初始化容器完成后立即退出，不会成为第二个常驻模型服务：

~~~bash
docker compose build bge-m3-service bge-m3-model-init pr-agent-memory pr-agent-web pr-agent-agent
docker compose --profile model-init run --rm bge-m3-model-init
docker compose up -d bge-m3-service
docker compose ps bge-m3-service
~~~

模型下载完成后，检查 `${MR_AGENT_HOME:-./runtime}/data/models` 可读。BGE readiness 正常后创建新增表、补算历史
active 经验的向量，再启动业务服务：

~~~bash
docker compose exec pr-agent-web python -m ut_agent.repair_memory.cli embeddings backfill --limit 500
docker compose up -d pr-agent-memory pr-agent-web pr-agent-agent
~~~

初始化和补算命令均可重复执行：固定 revision 已完整下载时不会再次下载；正文、模板或模型 revision 没有变化
的 ready 向量不会重复计算。不要删除原有 SQLite 数据库、经验正文或审计记录。

## 上线前验收

先运行不需要真实模型的回归测试：

~~~bash
PYTHONPATH=. ./.venv/bin/pytest \
  tests/unittest/test_repair_memory_integration.py \
  tests/unittest/test_repair_memory_evaluation.py -q
~~~

生产 Compose 不公开模型端口。仅在运维建立临时端口转发后运行真实 Smoke：

~~~bash
PYTHONPATH=. ./.venv/bin/python scripts/repair_memory_embedding_smoke.py \
  --url http://127.0.0.1:18080
~~~

Smoke 会检查固定模型和 revision、1024 维向量、单条与 16 条批量请求、同义报错相似度及预热后单条查询
P95。它只发送内置脱敏文本，不读取 Repair Memory 数据库。验收完成后立即关闭临时端口转发。

还应在一条脱敏任务上确认：

1. 项目经验和通用经验同时进入候选池，最终审计的 `scoring_mode` 为 `hybrid`。
2. 修复 Prompt 最多注入 3 条中文经验，Hit 不保存原始查询文本或向量。
3. 看板禁用经验后，新任务不再召回；恢复后可重新参与检索。
4. 停止 BGE 服务后，Agent 使用 `rule_fallback` 继续修复，而不是让任务失败。

## 回滚

需要立即停止经验检索时，将配置改为：

~~~toml
[repair_memory]
retrieval_mode = "off"
~~~

重启 `pr-agent-agent` 即可。关闭检索不会删除经验、向量、命中记录或审计数据；BGE 容器也可以单独停止。
恢复时改回 `inject`，未发生变化的经验不需要重新生成向量。
