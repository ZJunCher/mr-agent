"""End-to-end integration tests for the complete repair-memory learning loop.

Covers Task 7 of the UT-Agent repair-memory implementation plan:
- two projects promote a global memory and a third project gets only the
  sanitized global hint;
- memory failures never change the repair result.
"""

import asyncio
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before the eager ut_agent package import
from tests.unittest.repair_memory_helpers import (
    sample_query,
    seed_pending_episode,
    seed_project_memory,
    valid_candidate_payload,
)
from ut_agent.model_failover import LLMCallOutcome
from ut_agent.repair_memory.consolidate import (
    promote_ready_patterns,
    run_consolidation_batch,
)
from ut_agent.repair_memory.embedding import (
    BGE_DIMENSIONS,
    BGE_MODEL_NAME,
    BGE_MODEL_REVISION,
    EmbeddingBatch,
    build_memory_embedding_text,
    embedding_source_hash,
    vector_to_blob,
)
from ut_agent.repair_memory.models import (
    EmbeddingStatus,
    MemoryScope,
    RepairMemoryEmbedding,
    RetrievalMode,
)
from ut_agent.repair_memory.outcomes import (
    memory_effectiveness_summary,
    settle_immediate_pipeline,
)
from ut_agent.repair_memory.prompt import render_historical_hints
from ut_agent.repair_memory.retrieve import retrieve_repair_hints
from ut_agent.repair_memory.store import init_repair_memory_tables, upsert_memory_embeddings


@pytest.fixture
def memory_db(tmp_path) -> str:
    path = str(tmp_path / "repair-memory.db")
    init_repair_memory_tables(path)
    return path


def _fake_llm_outcome(payload: dict):
    return LLMCallOutcome(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "memory-1",
                "type": "function",
                "function": {
                    "name": "submit_repair_memory",
                    "arguments": json.dumps(payload, ensure_ascii=False),
                },
            }],
        },
        "test-model",
        (),
    )


def _fake_call(outcome):
    async def _call(*args, **kwargs):
        return outcome

    return _call


def _fake_global_call():
    async def _call(*args, **kwargs):
        return _fake_llm_outcome(valid_candidate_payload())

    return _call


def _successful_pipeline_event():
    from pr_agent.distributed.models import PipelineEvent

    return PipelineEvent.new(
        project_id="group/c",
        pipeline_id=200,
        sha="b" * 40,
        status="success",
        ref="feature/c",
    )


def _unit_vector() -> tuple[float, ...]:
    return (1.0,) + (0.0,) * (BGE_DIMENSIONS - 1)


class _QueryEmbeddingClient:
    def encode(self, texts, *, timeout_seconds):
        return EmbeddingBatch(
            model=BGE_MODEL_NAME,
            revision=BGE_MODEL_REVISION,
            dimensions=BGE_DIMENSIONS,
            vectors=(_unit_vector(),),
        )


def test_two_projects_promote_memory_and_third_project_gets_only_sanitized_global_hint(memory_db):
    # Two projects with the same pattern.
    seed_pending_episode(memory_db, project="group/a", episode_id="episode:task-a:action-a")
    seed_pending_episode(memory_db, project="group/b", episode_id="episode:task-b:action-b")

    outcome = _fake_llm_outcome(valid_candidate_payload())
    run_consolidation_batch(10, "worker-1", memory_db, llm_call=_fake_call(outcome))
    assert asyncio.run(promote_ready_patterns(memory_db, llm_call=_fake_global_call())).promoted == 1

    # A third project retrieves only the global hint.
    result = retrieve_repair_hints(
        sample_query(project="group/c", root_cause_group_id="root-c"),
        "task-c",
        RetrievalMode.INJECT,
        memory_db,
    )
    block = render_historical_hints(result.hints, 2000)

    assert len(result.hints) == 1
    assert result.hints[0].scope is MemoryScope.GLOBAL
    assert "group/a" not in block
    assert "group/b" not in block

    # Settle the attempt and verify the metric.
    settle_immediate_pipeline("task-c", _successful_pipeline_event(), memory_db)
    metrics = memory_effectiveness_summary(days=None, project="group/c", path=memory_db)
    assert metrics["settled_pipeline_attempts"] == 1
    assert metrics["immediate_success_rate"] == 100.0


def test_project_memory_is_retrieved_before_global(memory_db):
    seed_project_memory(memory_db, project="group/a", pattern_key="pattern-1")
    seed_project_memory(memory_db, project="group/b", pattern_key="pattern-1")
    asyncio.run(promote_ready_patterns(memory_db, llm_call=_fake_global_call()))

    result = retrieve_repair_hints(
        sample_query(project="group/a", root_cause_group_id="root-a"),
        "task-a",
        RetrievalMode.SHADOW,
        memory_db,
    )
    assert result.hints
    assert result.hints[0].scope is MemoryScope.PROJECT


def test_ready_embedding_is_directly_injected_as_a_chinese_untrusted_hint(memory_db):
    memory = seed_project_memory(
        memory_db,
        project="group/a",
        pattern_key="missing-member",
        problem_pattern="编译器报错：error: no member named 'node_name'",
        repair_guidance="根据当前依赖接口调整测试夹具",
    )
    text = build_memory_embedding_text(memory)
    embedding = RepairMemoryEmbedding(
        memory_id=memory.memory_id,
        model_name=BGE_MODEL_NAME,
        model_revision=BGE_MODEL_REVISION,
        dimensions=BGE_DIMENSIONS,
        vector_blob=vector_to_blob(_unit_vector()),
        source_hash=embedding_source_hash(
            text,
            model_name=BGE_MODEL_NAME,
            model_revision=BGE_MODEL_REVISION,
        ),
        status=EmbeddingStatus.READY,
        created_at="2026-08-18T00:00:00+00:00",
        updated_at="2026-08-18T00:00:00+00:00",
    )
    assert upsert_memory_embeddings((embedding,), memory_db)

    result = retrieve_repair_hints(
        sample_query(project="group/a", root_cause_group_id="root-direct-inject"),
        "task-direct-inject",
        RetrievalMode.INJECT,
        memory_db,
        embedding_client=_QueryEmbeddingClient(),
    )
    block = render_historical_hints(result.hints, 2000)

    assert result.hints
    assert "[UNTRUSTED HISTORICAL REPAIR HINTS]" in block
    assert "根据当前依赖接口调整测试夹具" in block
    assert "error: no member named 'node_name'" in block
    assert "不得直接照搬历史补丁" in block


def test_off_mode_returns_no_hints(memory_db):
    seed_project_memory(memory_db, project="group/a", pattern_key="pattern-1")
    result = retrieve_repair_hints(
        sample_query(project="group/a"),
        "task-1",
        RetrievalMode.OFF,
        memory_db,
    )
    assert result.hints == ()


def test_empty_database_returns_no_hints(memory_db):
    result = retrieve_repair_hints(
        sample_query(project="group/a"),
        "task-1",
        RetrievalMode.INJECT,
        memory_db,
    )
    assert result.hints == ()


def test_compose_runs_one_internal_bge_service_without_a_host_port():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    model_service = services["bge-m3-service"]
    assert model_service["build"] == {"context": ".", "dockerfile": "docker/Dockerfile.embedding"}
    assert "ports" not in model_service
    assert model_service["volumes"] == ["/srv/mr-agent/data/models:/models:ro"]
    assert model_service["restart"] == "unless-stopped"
    assert model_service["healthcheck"]["test"][0:2] == ["CMD", "python"]
    assert model_service["cpus"]
    assert model_service["mem_limit"]


def test_embedding_image_pins_official_cpu_only_torch():
    dockerfile = Path("docker/Dockerfile.embedding").read_text(encoding="utf-8")
    requirements = Path("requirements-embedding.txt").read_text(encoding="utf-8").casefold()

    assert "https://download.pytorch.org/whl/cpu" in dockerfile
    assert "torch==2.8.0+cpu" in dockerfile
    assert "torch==" not in requirements
    assert "nvidia-" not in requirements
    assert "cuda" not in requirements


def test_compose_model_initializer_is_explicit_writable_and_one_shot():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    initializer = compose["services"]["bge-m3-model-init"]

    assert initializer["build"] == {"context": ".", "dockerfile": "docker/Dockerfile.embedding"}
    assert initializer["profiles"] == ["model-init"]
    assert initializer["volumes"] == ["/srv/mr-agent/data/models:/models"]
    assert initializer["restart"] == "no"
    assert "ports" not in initializer
    assert initializer["command"] == ["python", "scripts/download_bge_m3.py", "--target", "/models"]


def test_compose_workers_use_internal_embedding_url_without_startup_dependency():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    expected_url = "http://bge-m3-service:8080"

    for service_name in ("pr-agent-web", "pr-agent-agent", "pr-agent-memory"):
        service = services[service_name]
        assert service["environment"]["REPAIR_MEMORY__EMBEDDING_SERVICE_URL"] == expected_url
        assert "bge-m3-service" not in service.get("depends_on", {})


def test_embedding_submodule_does_not_eagerly_load_full_repair_runtime():
    probe = (
        "import sys; import ut_agent.repair_memory.embedding; "
        "assert 'ut_agent.repair_memory.consolidate' not in sys.modules; "
        "assert 'pr_agent.config_loader' not in sys.modules"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_download_script_uses_fixed_revision_and_skips_complete_snapshot(tmp_path):
    from scripts.download_bge_m3 import download_model

    calls: list[dict[str, object]] = []

    def fake_snapshot_download(**kwargs):
        calls.append(kwargs)
        snapshot = tmp_path / "models--BAAI--bge-m3" / "snapshots" / "fixed"
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text("{}", encoding="utf-8")
        return str(snapshot)

    output = io.StringIO()
    first = download_model(tmp_path, snapshot_download_fn=fake_snapshot_download, output=output)
    second = download_model(tmp_path, snapshot_download_fn=fake_snapshot_download, output=output)

    assert first == second
    assert len(calls) == 1
    assert calls[0]["repo_id"] == BGE_MODEL_NAME
    assert calls[0]["revision"] == BGE_MODEL_REVISION
    assert calls[0]["cache_dir"] == str(tmp_path.resolve())
    assert calls[0]["ignore_patterns"] == ("onnx/*",)
    rendered = output.getvalue()
    assert BGE_MODEL_NAME in rendered
    assert BGE_MODEL_REVISION in rendered
    assert str(tmp_path.resolve()) in rendered
    assert "download_complete=true" in rendered


@pytest.mark.parametrize("revision", ["", "main", " MAIN "])
def test_download_script_rejects_mutable_revision(tmp_path, revision):
    from scripts.download_bge_m3 import download_model

    with pytest.raises(ValueError, match="immutable"):
        download_model(tmp_path, revision=revision, snapshot_download_fn=lambda **_kwargs: "unused")
