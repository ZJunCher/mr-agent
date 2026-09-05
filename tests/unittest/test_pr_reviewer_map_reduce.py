import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.tools.pr_reviewer import PRReviewer


class Provider:
    def __init__(self, files):
        self._files = files

    def get_diff_files(self):
        return self._files


class Tokens:
    prompt_tokens = 10

    @staticmethod
    def count_tokens(text):
        return len(text)


def _file(path, payload):
    return FilePatchInfo("old", "new", f"@@ -1 +1 @@\n-old\n+{payload}", path, edit_type=EDIT_TYPE.MODIFIED)


def _prediction(path, line):
    return f"""review:
  score: 80
  relevant_tests: 'No'
  estimated_effort_to_review_[1-5]: 2
  key_issues_to_review:
    - relevant_file: {path}
      relevant_line: '{line}'
      suggestion: fix it
  security_concerns: 'No'
"""


def _reviewer(files):
    reviewer = object.__new__(PRReviewer)
    reviewer.git_provider = Provider(files)
    reviewer.token_handler = Tokens()
    reviewer.patches_diff = None
    reviewer.prediction = None
    reviewer.related_files_context = ""
    reviewer._get_related_files_context = lambda: "related"
    reviewer.pr_url = "https://gitlab/repo/-/merge_requests/1"
    return reviewer


def _settings(monkeypatch, *, fail_closed=True, max_chunks=20, concurrency=4):
    values = {
        "large_mr_review.enabled": True,
        "large_mr_review.output_buffer_tokens": 20,
        "large_mr_review.chunk_metadata_tokens": 10,
        "large_mr_review.max_chunks": max_chunks,
        "large_mr_review.max_concurrency": concurrency,
        "large_mr_review.fail_closed": fail_closed,
    }
    monkeypatch.setattr(
        "pr_agent.tools.pr_reviewer.get_settings",
        lambda: SimpleNamespace(get=lambda key, default=None: values.get(key, default)),
    )


def test_review_small_diff_uses_one_existing_prediction_call(monkeypatch):
    _settings(monkeypatch)
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 300)
    reviewer = _reviewer([_file("a.py", "ok")])
    reviewer._get_prediction = AsyncMock(return_value=_prediction("a.py", 1))

    asyncio.run(reviewer._prepare_prediction("model"))

    reviewer._get_prediction.assert_awaited_once()
    assert reviewer.review_coverage.status == "complete"
    assert reviewer.prediction == _prediction("a.py", 1)


def test_review_maps_every_chunk_and_reduces_predictions(monkeypatch):
    _settings(monkeypatch, concurrency=2)
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 230)
    reviewer = _reviewer([_file("a.py", "a" * 75), _file("b.py", "b" * 75)])
    reviewer._get_prediction = AsyncMock(side_effect=[_prediction("a.py", 1), _prediction("b.py", 1)])

    asyncio.run(reviewer._prepare_prediction("model"))

    assert reviewer._get_prediction.await_count == 2
    assert reviewer.review_coverage.status == "complete"
    assert "a.py" in reviewer.prediction and "b.py" in reviewer.prediction


def test_review_failed_chunk_is_not_complete_and_fails_closed(monkeypatch):
    _settings(monkeypatch, fail_closed=True)
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 230)
    reviewer = _reviewer([_file("a.py", "a" * 75), _file("b.py", "b" * 75)])
    reviewer._get_prediction = AsyncMock(side_effect=[_prediction("a.py", 1), RuntimeError("model failed")])

    asyncio.run(reviewer._prepare_prediction("model"))

    assert reviewer.review_coverage.status == "partial"
    assert reviewer.prediction is None
    assert "未完整覆盖" in reviewer.coverage_failure_message


def test_review_partial_mode_keeps_successful_results_with_notice(monkeypatch):
    _settings(monkeypatch, fail_closed=False)
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 230)
    reviewer = _reviewer([_file("a.py", "a" * 75), _file("b.py", "b" * 75)])
    reviewer._get_prediction = AsyncMock(side_effect=[_prediction("a.py", 1), RuntimeError("model failed")])

    asyncio.run(reviewer._prepare_prediction("model"))

    assert reviewer.review_coverage.status == "partial"
    assert "a.py" in reviewer.prediction
    assert reviewer.coverage_partial_notice


def test_capacity_failure_calls_no_model_and_reports_missing_units(monkeypatch):
    _settings(monkeypatch, max_chunks=1)
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 230)
    reviewer = _reviewer([_file("a.py", "a" * 75), _file("b.py", "b" * 75)])
    reviewer._get_prediction = AsyncMock(return_value=_prediction("a.py", 1))

    asyncio.run(reviewer._prepare_prediction("model"))

    reviewer._get_prediction.assert_not_awaited()
    assert reviewer.review_chunk_plan.status == "capacity_exceeded"
    assert reviewer.review_coverage.status == "failed"
    assert reviewer.review_coverage.missing_unit_ids
