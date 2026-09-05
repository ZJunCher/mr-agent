from pr_agent.config_loader import get_settings


def test_code_graph_config_defaults():
    cfg = get_settings().get("pr_reviewer.code_graph", {})
    assert cfg.get("enabled", None) is True
    assert cfg.get("supported_languages", None) == ["python", "cpp"]
    assert cfg.get("max_hops", None) == 2
    assert cfg.get("token_budget", None) == 8000
    assert cfg.get("stale_graph_ttl_days", None) == 15
    assert cfg.get("storage_root", None) == "/app/data/code_graph"
