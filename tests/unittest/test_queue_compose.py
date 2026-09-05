from pathlib import Path

import yaml


def test_compose_runs_exactly_one_persistent_repair_memory_worker():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    memory = compose["services"]["pr-agent-memory"]
    assert memory["command"] == ["python", "-m", "ut_agent.repair_memory.worker"]
    assert memory["volumes"] == ["${MR_AGENT_HOME:-./runtime}/data:/app/data"]
    assert memory["restart"] == "unless-stopped"
    assert "deploy" not in memory or memory["deploy"].get("replicas", 1) == 1
    assert memory["depends_on"]["pr-agent-redis"]["condition"] == "service_healthy"


def test_queue_dockerfile_has_repair_memory_worker_target():
    dockerfile = Path("docker/Dockerfile.queue").read_text(encoding="utf-8")

    assert "FROM queue_runtime AS repair_memory_worker" in dockerfile
    assert 'CMD ["python", "-m", "ut_agent.repair_memory.worker"]' in dockerfile
