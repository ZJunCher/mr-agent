import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load(path):
    return tomllib.loads((ROOT / path).read_text(encoding="utf-8"))


def test_large_mr_review_defaults_and_repository_mirror_match():
    defaults = _load("pr_agent/settings/configuration.toml")["large_mr_review"]
    repository = _load(".pr_agent.toml")["large_mr_review"]
    expected = {
        "enabled": True,
        "output_buffer_tokens": 1500,
        "chunk_metadata_tokens": 256,
        "max_chunks": 20,
        "max_concurrency": 4,
        "fail_closed": True,
    }

    assert defaults == expected
    assert repository == expected


def test_invalid_large_mr_limits_fail_closed(monkeypatch):
    from pr_agent.algo import review_chunking

    monkeypatch.setattr(review_chunking, "get_max_tokens", lambda model: 100)
    provider = type("Provider", (), {"get_diff_files": lambda self: []})()
    tokens = type("Tokens", (), {"prompt_tokens": 10, "count_tokens": staticmethod(len)})()

    plan = review_chunking.build_review_chunk_plan(provider, tokens, "model", max_chunks=0)

    assert plan.status == "invalid_budget"
    assert not plan.is_complete_plan
