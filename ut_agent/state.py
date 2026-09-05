import operator
from typing import Annotated, Optional, TypedDict

from langchain_core.messages import AnyMessage


class AgentState(TypedDict):
    """ReAct Agent 的状态。

    状态是对话历史的涌现，不是预定义容器。
    Agent 运行时的全部状态就是 messages——工具调用的历史。
    如果 Agent 调了 clone_source_branch，返回值在 messages 里；
    如果没调，对话里就没有这个信息——它不是"空字段"，而是根本不存在。

    trigger_* 字段是入口注入的只读数据，不是 Agent 运行时产生的状态。
    """
    # ── 对话历史（核心状态）──
    messages: Annotated[list[AnyMessage], operator.add]

    # ── 元数据 ──
    iteration: int
    max_iterations: int
    active_model: Optional[str]
    attempted_models: list[str]
    model_failover_count: int
    last_model_failure_code: Optional[str]
    model_terminal_error: Optional[str]
    model_terminal_failure_kind: Optional[str]
    # Task-scoped context compression control, persisted by LangGraph Checkpoint.
    context_summary: str
    context_summary_covered_messages: int
    context_compression_ineffective_count: int
    context_compression_cooldown_until: float
    context_compression_last_input_hash: str

    # ── 触发上下文（入口注入，只读）──
    trigger_type: str          # "mr_created" | "pipeline_failed" | "manual_triage"
    pr_url: str
    mr_id: int
    title: str
    author: str
    source_branch: str
    target_branch: str
    project_id: str
    pipeline_id: Optional[int]
    commit_sha: Optional[str]
    failed_jobs: Optional[list[dict]]
    selected_categories: list[str]
    diff_files: list[dict]
    workspace_snapshot: dict
    require_workspace_snapshot: bool
    # 验证与修复
    verification_verdict: Optional[str]  # Verifier 输出 JSON (PASS/FAIL + 详情)
    fix_plan: Optional[str]          # Planner 修复计划 JSON
    fix_iterations: int              # 修复迭代次数
    fix_history: Optional[str]       # 修复历史 JSON: [{plan: ..., result: "FAIL"/"PASS", evidence: ...}, ...]
    fix_patches: Optional[list[str]] # 修复模式产生的 patch 文件列表（与正常流程的 generated_patches 隔离）
    # Native hybrid repair: append-only checkpointed intent and semantic verdict events.
    repair_plans: Annotated[list[dict], operator.add]
    repair_verifications: Annotated[list[dict], operator.add]
    repair_memory_contexts: Annotated[list[dict], operator.add]
    # 输出
    response: Optional[str]
