"""Shared deterministic factories for focused repair-memory tests.

Each helper writes through the public store API rather than direct production-table
SQL. Production SQL must never interpolate caller-provided table names; the
``count_rows`` helper here is test-only and uses a fixed allowlist of known tables.
"""

import sqlite3

import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before the eager ut_agent package import

# Table allowlist for the test-only ``count_rows`` helper. Production code must
# never interpolate caller-provided table names.
_ROW_COUNT_TABLES = frozenset(
    {
        "repair_memory_episodes",
        "repair_memories",
        "repair_memory_evidence",
        "repair_memory_hits",
        "repair_memory_retrieval_candidates",
        "repair_memory_retrieval_audits",
        "repair_memory_events",
        "repair_memory_embeddings",
    }
)


def count_rows(path: str, table: str) -> int:
    """Return the row count for a known repair-memory table.

    Test-only helper: production SQL must never interpolate caller-provided table
    names. This function rejects any table not in the fixed allowlist.
    """
    if table not in _ROW_COUNT_TABLES:
        raise ValueError(f"unknown repair-memory table: {table}")
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


@pytest.fixture
def memory_db(tmp_path) -> str:
    """Initialize a temporary repair-memory database and return its path."""
    # Imported here to avoid a circular import at module load time.
    from ut_agent.repair_memory.store import init_repair_memory_tables

    path = str(tmp_path / "repair-memory.db")
    init_repair_memory_tables(path)
    return path


def sample_episode(
    *,
    task_id: str = "task-1",
    project: str = "group/a",
    action_identity: str = "action-1",
) -> "object":
    """Return a ``RepairEpisode`` with fixed valid defaults for tests."""
    from ut_agent.repair_memory.models import RepairEpisode

    return RepairEpisode(
        episode_id=f"episode:{task_id}:{action_identity}",
        task_id=task_id,
        action_identity=action_identity,
        root_cause_group_id="root-1",
        project=project,
        mr_iid=1,
        source_pipeline_id=100,
        source_sha="a" * 40,
        final_pipeline_id=101,
        final_sha="b" * 40,
        categories=("build",),
        job_names=("build_release",),
        language_hints=("cpp",),
        build_system_hints=("cmake",),
        diagnostic_fingerprint="fingerprint-1",
        causal_tokens=("request", "member"),
        root_cause="The current request interface has no member named node_name.",
        solution_summary="Align the test with the current request interface.",
        measures=("Update the fixture",),
        changed_files=("tests/request_test.cpp",),
        report_input_digest="c" * 64,
        report_source="model",
    )


def sample_memory(memory_id: str = "mem-1", **overrides) -> "object":
    """Return a ``RepairMemory`` with fixed valid defaults for tests.

    Keyword overrides replace the default field values; unknown keys raise at
    dataclass construction time, which is the desired test failure mode.
    """
    from ut_agent.repair_memory.models import MemoryScope, MemoryStatus, RepairMemory

    values: dict[str, object] = {
        "memory_id": memory_id,
        "scope": MemoryScope.PROJECT,
        "scope_key": "group/a",
        "pattern_key": "pattern-1",
        "pattern_version": 1,
        "language": "cpp",
        "build_system": "cmake",
        "failure_family": "missing_member",
        "root_cause_class": "interface_drift",
        "repair_action_class": "align_current_interface",
        "diagnostic_fingerprint": "fingerprint-1",
        "causal_tokens": ("request", "member"),
        "problem_pattern": "请求对象缺少预期成员",
        "applicability": ("编译器报告当前请求类型缺少目标成员",),
        "anti_conditions": ("当前接口中仍存在该成员",),
        "repair_guidance": "按照当前接口调整测试代码",
        "validation_guidance": ("运行对应精确 SHA 的 Pipeline",),
        "confidence": 0.60,
        "support_episode_count": 1,
        "support_project_count": 1,
        "settled_attempts": 0,
        "immediate_successes": 0,
        "status": MemoryStatus.ACTIVE,
        "content_locale": "zh-CN",
        "created_at": "2026-08-15T00:00:00+00:00",
        "updated_at": "2026-08-15T00:00:00+00:00",
        "last_reinforced_at": "2026-08-15T00:00:00+00:00",
    }
    values.update(overrides)
    return RepairMemory(**values)


def sample_report_input(
    *,
    repair_task_id: str = "task-1",
    project_id: str = "group/a",
    mr_iid: int = 1,
    final_pipeline_id: int = 101,
    final_pipeline_status: str = "success",
    final_sha: str = "b" * 40,
    source_pipeline_id: int = 100,
    source_sha: str = "a" * 40,
    selected_categories: tuple[str, ...] = ("build",),
    failed_jobs: tuple[str, ...] = ("build_release",),
    causal_lines: tuple[str, ...] = (
        "error: no member named 'node_name' in 'Request'",
    ),
) -> "object":
    """Return a ``FinalRepairReportInput`` with fixed valid successful defaults."""
    from pr_agent.triage.final_repair_report import FinalRepairDiff, FinalRepairReportInput

    diffs = (
        FinalRepairDiff(
            path="tests/request_test.cpp",
            change_type="modified",
            additions=2,
            deletions=1,
            patch="@@ -1,2 +1,3 @@\n-old\n+new\n+extra\n",
        ),
    )
    return FinalRepairReportInput(
        repair_task_id=repair_task_id,
        project_id=project_id,
        mr_iid=mr_iid,
        pr_url="https://gitlab.example.com/group/a/-/merge_requests/1",
        source_pipeline_id=source_pipeline_id,
        source_sha=source_sha,
        base_sha="0" * 40,
        final_sha=final_sha,
        final_pipeline_id=final_pipeline_id,
        final_pipeline_status=final_pipeline_status,
        final_coverage=None,
        selected_categories=selected_categories,
        failed_jobs=failed_jobs,
        causal_lines=causal_lines,
        diffs=diffs,
    )


def sample_report_state(
    *,
    input_digest: str = "c" * 64,
    source: str = "model",
) -> "object":
    """Return a ``FinalRepairReportState`` reflecting a completed model report."""
    from pr_agent.triage.final_repair_report import (
        FinalRepairReport,
        FinalRepairReportState,
        RepairReportStatus,
    )

    report = FinalRepairReport(
        root_cause_summary="The current request interface has no member named node_name.",
        solution_summary="Align the test with the current request interface.",
        rationale="The dependency interface changed; the test must follow.",
        file_explanations=(),
        source=source,
    )
    return FinalRepairReportState(
        RepairReportStatus.MODEL_GENERATED,
        report_task_id="report-1",
        input_digest=input_digest,
        report=report,
        model="test-model",
        attempted_models=("test-model",),
        created_at="2026-08-15T00:00:00+00:00",
        updated_at="2026-08-15T00:00:00+00:00",
    )


def sample_manifest(action_shas: tuple[str, ...] = ("b" * 40,)) -> "object":
    """Return a frozen, statically valid ``RepairCommitManifest``.

    Builds a continuous commit chain from ``base_commit_sha`` through each
    action SHA, ending at ``sample_report_input().final_sha``.
    """
    from pr_agent.triage.repair_rollback import RepairCommitEntry, RepairCommitManifest

    base = "0" * 40
    entries: list[RepairCommitEntry] = []
    parent = base
    for sequence, sha in enumerate(action_shas, start=1):
        entries.append(
            RepairCommitEntry(
                sequence=sequence,
                commit_sha=sha,
                parent_sha=parent,
                tree_sha="1" * 40,
                effect_id=f"effect-{sequence}",
                task_marker=f"task-{sequence}",
                pushed_at="2026-08-15T00:00:00+00:00",
            )
        )
        parent = sha
    return RepairCommitManifest(
        repair_task_id="task-1",
        project_id="group/a",
        mr_iid=1,
        source_branch="feature/test",
        base_commit_sha=base,
        base_tree_sha="2" * 40,
        authorized_actor_id="user-1",
        entries=tuple(entries),
        frozen=True,
        frozen_at="2026-08-15T00:00:00+00:00",
    )


def sample_action(
    *,
    action_id: str = "action-1",
    root_cause_group_id: str = "root-1",
    status: str = "verified",
    validation_pipeline_id: int = 101,
    commit_sha: str = "b" * 40,
    categories: tuple[str, ...] = ("build",),
    job_names: tuple[str, ...] = ("build_release",),
    root_cause: str = "The current request interface has no member named node_name.",
    solution_summary: str = "Align the test with the current request interface.",
    measures: tuple[str, ...] = ("Update the fixture",),
    changed_files: tuple[str, ...] = ("tests/request_test.cpp",),
) -> "object":
    """Return a ``RepairAction`` with fixed valid verified defaults."""
    from pr_agent.triage.repair_details import RepairAction

    return RepairAction(
        action_id=action_id,
        root_cause_group_id=root_cause_group_id,
        categories=categories,
        job_names=job_names,
        root_cause=root_cause,
        evidence="compiler diagnostic",
        confidence="high",
        measures=measures,
        changed_files=changed_files,
        solution_summary=solution_summary,
        rationale="The dependency interface changed.",
        commit_sha=commit_sha,
        validation_pipeline_id=validation_pipeline_id,
        validation_status="success",
        status=status,
        started_at="2026-08-15T00:00:00+00:00",
        completed_at="2026-08-15T00:00:01+00:00",
    )


def sample_pipeline_repair_state(
    *,
    actions: tuple["object", ...] | None = None,
) -> "object":
    """Return a ``PipelineRepairState`` with one verified action by default."""
    from pr_agent.triage.pipeline_repair import PipelineRepairPhase, PipelineRepairState

    return PipelineRepairState(
        phase=PipelineRepairPhase.TERMINAL,
        root_pipeline_id=100,
        latest_pipeline_id=101,
        latest_pipeline_sha="b" * 40,
        final_pipeline_status="success",
        selected_categories=("build",),
        effective_categories=("build",),
        repair_actions=actions if actions is not None else (sample_action(),),
    )


def enabled_memory_settings(**overrides) -> "object":
    """Return ``RepairMemorySettings`` with capture and inject enabled for group/a."""
    from ut_agent.repair_memory.config import parse_repair_memory_settings
    from ut_agent.repair_memory.models import RetrievalMode

    defaults = {
        "capture_enabled": True,
        "retrieval_mode": RetrievalMode.INJECT,
        "promotion_enabled": True,
        "project_allowlist": ("group/a",),
    }
    defaults.update(overrides)
    return parse_repair_memory_settings(defaults)


def valid_candidate_payload(**overrides) -> dict:
    """Return a valid consolidation candidate JSON payload for tests."""
    payload = {
        "schema_version": 1,
        "language": "cpp",
        "build_system": "cmake",
        "failure_family": "missing_member",
        "root_cause_class": "interface_drift",
        "repair_action_class": "align_current_interface",
        "problem_pattern": "测试代码仍使用当前依赖接口中已删除的成员",
        "applicability": ["编译器报告依赖请求类型缺少目标成员"],
        "anti_conditions": ["当前依赖快照中仍声明该成员"],
        "repair_guidance": "按照当前依赖接口调整测试夹具",
        "validation_guidance": ["重新运行受影响的编译目标和精确 SHA 的 Pipeline"],
    }
    payload.update(overrides)
    return payload


def seed_pending_episode(
    db_path: str,
    *,
    episode_id: str = "episode:task-1:action-1",
    project: str = "group/a",
    task_id: str | None = None,
    action_identity: str | None = None,
    changed_files: tuple[str, ...] = ("tests/request_test.cpp",),
) -> "object":
    """Write one pending episode through the public store API."""
    from ut_agent.repair_memory.models import RepairEpisode
    from ut_agent.repair_memory.store import save_episode

    # Derive unique task_id/action_identity from episode_id so multiple episodes
    # can coexist without tripping the (task_id, action_identity) unique index.
    suffix = episode_id.rsplit(":", 1)[-1]
    tid = task_id or f"task-{suffix}"
    aid = action_identity or f"action-{suffix}"
    episode = RepairEpisode(
        episode_id=episode_id,
        task_id=tid,
        action_identity=aid,
        root_cause_group_id=f"root-{suffix}",
        project=project,
        mr_iid=1,
        source_pipeline_id=100,
        source_sha="a" * 40,
        final_pipeline_id=101,
        final_sha="b" * 40,
        categories=("build",),
        job_names=("build_release",),
        language_hints=("cpp",),
        build_system_hints=("cmake",),
        diagnostic_fingerprint=f"fingerprint-{suffix}",
        causal_tokens=("request", "member"),
        root_cause="The current request interface has no member named node_name.",
        solution_summary="Align the test with the current request interface.",
        measures=("Update the fixture",),
        changed_files=changed_files,
        report_input_digest="c" * 64,
        report_source="model",
    )
    assert save_episode(episode, db_path) is True
    return episode


def seed_project_memory(
    db_path: str,
    *,
    project: str = "group/a",
    pattern_key: str = "pattern-1",
    episode_id: str = "episode:task-1:action-1",
    memory_id: str | None = None,
    confidence: float = 0.60,
    changed_files: tuple[str, ...] = ("tests/request_test.cpp",),
    **overrides,
) -> "object":
    """Write one active project memory with evidence through the public store API."""
    from ut_agent.repair_memory.models import MemoryScope
    from ut_agent.repair_memory.store import save_memory_with_evidence

    # Ensure the episode exists so the evidence link is valid.
    try:
        seed_pending_episode(db_path, episode_id=episode_id, project=project, changed_files=changed_files)
    except Exception:
        pass

    mid = memory_id or f"mem:{project}:{pattern_key}"
    memory = sample_memory(
        memory_id=mid,
        scope=MemoryScope.PROJECT,
        scope_key=project,
        pattern_key=pattern_key,
        confidence=confidence,
        **overrides,
    )
    assert save_memory_with_evidence(memory, episode_id, db_path) is True
    return memory


def seed_two_projects(
    db_path: str,
    *,
    pattern_key: str = "pattern-1",
    failure_family: str = "missing_member",
    changed_file: str = "tests/request_test.cpp",
) -> tuple["object", "object"]:
    """Seed active project memories for the same pattern in two distinct projects."""
    mem_a = seed_project_memory(
        db_path,
        project="group/a",
        pattern_key=pattern_key,
        episode_id="episode:task-a:action-a",
        memory_id="mem:group/a",
        failure_family=failure_family,
        changed_files=(changed_file,),
    )
    mem_b = seed_project_memory(
        db_path,
        project="group/b",
        pattern_key=pattern_key,
        episode_id="episode:task-b:action-b",
        memory_id="mem:group/b",
        failure_family=failure_family,
        changed_files=(changed_file,),
    )
    return mem_a, mem_b


def seed_promoted_global_memory(
    db_path: str,
    *,
    projects: tuple[str, ...] = ("group/a", "group/b"),
    pattern_key: str = "pattern-1",
) -> "object":
    """Seed an active global memory plus its supporting project memories."""
    from ut_agent.repair_memory.models import MemoryScope
    from ut_agent.repair_memory.store import save_memory

    for index, project in enumerate(projects):
        seed_project_memory(
            db_path,
            project=project,
            pattern_key=pattern_key,
            episode_id=f"episode:task-{project}:action-{index}",
            memory_id=f"mem:{project}:{pattern_key}",
        )
    global_memory = sample_memory(
        memory_id=f"mem:global:{pattern_key}",
        scope=MemoryScope.GLOBAL,
        scope_key="*",
        pattern_key=pattern_key,
        support_project_count=len(projects),
        support_episode_count=len(projects),
        confidence=0.70,
    )
    assert save_memory(global_memory, db_path) is True
    return global_memory


def disable_project_memory(db_path: str, *, project: str, pattern_key: str) -> bool:
    """Disable the active project memory for ``project`` and ``pattern_key``."""
    from ut_agent.repair_memory.models import MemoryStatus
    from ut_agent.repair_memory.store import list_memories, update_memory_status

    memories = list_memories(
        scope="project", scope_key=project, pattern_key=pattern_key, status="active", path=db_path
    )
    if not memories:
        return False
    return update_memory_status(memories[0].memory_id, MemoryStatus.DISABLED, "test disable", path=db_path)


def project_memory_for(db_path: str, project: str, pattern_key: str) -> "object | None":
    """Return the active project memory for ``project`` and ``pattern_key``."""
    from ut_agent.repair_memory.store import list_memories

    memories = list_memories(
        scope="project", scope_key=project, pattern_key=pattern_key, status="active", path=db_path
    )
    return memories[0] if memories else None


def supporting_episodes(db_path: str, pattern_key: str) -> tuple:
    """Return all episodes linked to active memories with ``pattern_key``."""
    from pr_agent.feedback.store import _connect

    conn = _connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT e.episode_id, e.changed_files_json, e.project "
            "FROM repair_memory_evidence ev "
            "JOIN repair_memory_episodes e ON e.episode_id = ev.episode_id "
            "JOIN repair_memories m ON m.memory_id = ev.memory_id "
            "WHERE m.pattern_key = ? AND m.status = 'active'",
            (pattern_key,),
        ).fetchall()
        return tuple(
            {
                "episode_id": row["episode_id"],
                "changed_files": tuple(__import__("json").loads(row["changed_files_json"])),
                "project": row["project"],
            }
            for row in rows
        )
    finally:
        conn.close()


def seed_old_episode(
    db_path: str,
    *,
    episode_id: str = "episode:old",
    linked_to_memory: bool = False,
) -> "object":
    """Seed an episode with an old ``created_at`` for retention tests."""
    from ut_agent.repair_memory.models import RepairEpisode
    from ut_agent.repair_memory.store import save_episode, save_memory_with_evidence

    # Derive a unique task_id/action_identity from episode_id so multiple old
    # episodes can coexist without tripping the (task_id, action_identity)
    # unique index.
    suffix = episode_id.rsplit(":", 1)[-1]
    episode = RepairEpisode(
        episode_id=episode_id,
        task_id=f"task-{suffix}",
        action_identity=f"action-{suffix}",
        root_cause_group_id=f"root-{suffix}",
        project="group/a",
        mr_iid=1,
        source_pipeline_id=100,
        source_sha="a" * 40,
        final_pipeline_id=101,
        final_sha="b" * 40,
        categories=("build",),
        job_names=("build_release",),
        language_hints=("cpp",),
        build_system_hints=("cmake",),
        diagnostic_fingerprint=f"fingerprint-{suffix}",
        causal_tokens=("request",),
        root_cause="old root cause",
        solution_summary="old solution",
        measures=("old measure",),
        changed_files=("tests/old.cpp",),
        report_input_digest="d" * 64,
        report_source="model",
        created_at="2024-01-01T00:00:00+00:00",
    )
    assert save_episode(episode, db_path) is True
    if linked_to_memory:
        memory = sample_memory(memory_id=f"mem:{episode_id}", pattern_key=f"pattern-{suffix}")
        save_memory_with_evidence(memory, episode_id, db_path)
    return episode


def seed_old_settled_hit(db_path: str, *, attempt_id: str = "old-attempt") -> None:
    """Seed a settled hit row with an old ``settled_at`` for retention tests."""
    from pr_agent.feedback.store import _connect
    from ut_agent.repair_memory.models import _json_dumps

    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO repair_memory_hits "
                "(attempt_id, task_id, root_cause_group_id, current_project, "
                "source_pipeline_id, source_sha, memory_id, memory_scope, rank, "
                "score_json, mode, immediate_pipeline_id, immediate_pipeline_sha, "
                "immediate_pipeline_status, outcome, created_at, settled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id, "task-old", "root-old", "group/a",
                    100, "a" * 40, "mem-old", "project", 1,
                    _json_dumps({"total": 90}), "injected",
                    101, "b" * 40, "success", "success",
                    "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00",
                ),
            )
    finally:
        conn.close()


def sample_query(
    *,
    project: str = "group/a",
    root_cause_group_id: str = "root-1",
    source_pipeline_id: int = 100,
    source_sha: str = "a" * 40,
    failure_category: str = "build",
    job_family: str = "build_release",
    failure_family: str = "missing_member",
    language: str = "cpp",
    build_system: str = "cmake",
    diagnostic_fingerprint: str = "fingerprint-1",
    causal_tokens: tuple[str, ...] = ("request", "member"),
) -> "object":
    """Return a ``RepairQuery`` with fixed valid defaults for retrieval tests."""
    from ut_agent.repair_memory.models import RepairQuery

    return RepairQuery(
        project=project,
        root_cause_group_id=root_cause_group_id,
        source_pipeline_id=source_pipeline_id,
        source_sha=source_sha,
        failure_category=failure_category,
        job_family=job_family,
        failure_family=failure_family,
        language=language,
        build_system=build_system,
        diagnostic_fingerprint=diagnostic_fingerprint,
        causal_tokens=causal_tokens,
    )


def sample_hint(
    *,
    memory_id: str = "mem-1",
    score: int = 90,
    confidence: float = 0.65,
) -> "object":
    """Return a ``RepairMemoryHint`` with fixed valid defaults for prompt tests."""
    from ut_agent.repair_memory.models import MemoryScope, RepairMemoryHint

    return RepairMemoryHint(
        memory_id=memory_id,
        scope=MemoryScope.PROJECT,
        pattern_key="pattern-1",
        score=score,
        match_reasons=("exact_fingerprint", "failure_family"),
        problem_pattern="编译器报告 std::unique_ptr 未定义",
        applicability=("当前 C++ 代码使用 std::unique_ptr",),
        anti_conditions=("代码已经正确包含 <memory> 头文件",),
        repair_guidance="检查并补充 #include <memory>",
        validation_guidance=("重新运行对应精确 SHA 的 Pipeline",),
        support_episode_count=1,
        support_project_count=1,
        confidence=confidence,
    )


def seed_memory(
    db_path: str,
    memory_id: str,
    *,
    scope: str = "project",
    scope_key: str = "group/a",
    pattern_key: str = "pattern-1",
    score_fixture: int = 90,
    **overrides,
) -> "object":
    """Seed a memory row for retrieval tests with optional score-relevant overrides."""
    from ut_agent.repair_memory.models import MemoryScope

    overrides.setdefault("confidence", 0.65)
    scope_enum = MemoryScope.GLOBAL if scope == "global" else MemoryScope.PROJECT
    memory = sample_memory(
        memory_id=memory_id,
        scope=scope_enum,
        scope_key=scope_key,
        pattern_key=pattern_key,
        **overrides,
    )
    from ut_agent.repair_memory.store import save_memory

    assert save_memory(memory, db_path) is True
    return memory


def seed_matching_memory(db_path: str) -> "object":
    """Seed one active project memory that matches ``sample_query()`` exactly."""
    return seed_memory(
        db_path,
        "mem-match",
        scope="project",
        scope_key="group/a",
        pattern_key="pattern-1",
        diagnostic_fingerprint="fingerprint-1",
        failure_family="missing_member",
        language="cpp",
        build_system="cmake",
        causal_tokens=("request", "member"),
        confidence=0.95,
    )


def raising_store_error(*args, **kwargs):
    """Raise a locked-database error for fail-open retrieval tests."""
    import sqlite3

    raise sqlite3.OperationalError("database is locked")
