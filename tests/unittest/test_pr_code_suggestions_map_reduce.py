import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


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


def _suggestion(path, start=1, score=8):
    return {
        "relevant_file": path,
        "relevant_lines_start": start,
        "relevant_lines_end": start,
        "existing_code": "old",
        "improved_code": "new",
        "one_sentence_summary": "修复问题",
        "suggestion_content": "Trigger: test\nFix: fix",
        "label": "正确性",
        "score": score,
    }


def _tool(files):
    tool = object.__new__(PRCodeSuggestions)
    tool.git_provider = Provider(files)
    tool.token_handler = Tokens()
    tool.related_files_context = ""
    tool._get_related_files_context = lambda: "related"
    tool._get_prediction = AsyncMock()
    tool.run_repair_pipeline = AsyncMock()
    tool.data = None
    return tool


def _settings(monkeypatch, *, fail_closed=True, parallel=True, max_chunks=20):
    values = {
        "large_mr_review.enabled": True,
        "large_mr_review.output_buffer_tokens": 20,
        "large_mr_review.chunk_metadata_tokens": 10,
        "large_mr_review.max_chunks": max_chunks,
        "large_mr_review.max_concurrency": 2,
        "large_mr_review.fail_closed": fail_closed,
        "pr_code_suggestions.pipeline_v2_enabled": False,
    }
    settings = SimpleNamespace(
        get=lambda key, default=None: values.get(key, default),
        pr_code_suggestions=SimpleNamespace(
            decouple_hunks=False,
            max_number_of_calls=1,
            parallel_calls=parallel,
            suggestions_score_threshold=1,
        ),
    )
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.get_settings", lambda: settings)


def test_improve_processes_all_chunks_beyond_legacy_call_limit(monkeypatch):
    _settings(monkeypatch, max_chunks=20)
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 230)
    tool = _tool([_file("a.py", "a" * 75), _file("b.py", "b" * 75)])
    tool._get_prediction.side_effect = [
        {"code_suggestions": [_suggestion("a.py")]},
        {"code_suggestions": [_suggestion("b.py")]},
    ]

    result = asyncio.run(tool.prepare_prediction_main("model"))

    assert tool._get_prediction.await_count == 2
    assert tool.review_coverage.status == "complete"
    assert [item["relevant_file"] for item in result["code_suggestions"]] == ["a.py", "b.py"]
    for call, chunk in zip(tool._get_prediction.await_args_list, tool.review_chunk_plan.chunks):
        assert call.args[1] == chunk.text
        assert call.args[2] == chunk.raw_text


def test_improve_parallel_and_sequential_modes_merge_equivalently(monkeypatch):
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 230)
    files = [_file("a.py", "a" * 75), _file("b.py", "b" * 75)]
    outputs = [
        {"code_suggestions": [_suggestion("a.py")]},
        {"code_suggestions": [_suggestion("b.py")]},
    ]
    _settings(monkeypatch, parallel=True)
    parallel = _tool(files)
    parallel._get_prediction.side_effect = outputs
    parallel_result = asyncio.run(parallel.prepare_prediction_main("model"))

    _settings(monkeypatch, parallel=False)
    sequential = _tool(files)
    sequential._get_prediction.side_effect = outputs
    sequential_result = asyncio.run(sequential.prepare_prediction_main("model"))

    assert parallel_result == sequential_result


def test_improve_failed_chunk_fails_closed(monkeypatch):
    _settings(monkeypatch, fail_closed=True)
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 230)
    tool = _tool([_file("a.py", "a" * 75), _file("b.py", "b" * 75)])
    tool._get_prediction.side_effect = [
        {"code_suggestions": [_suggestion("a.py")]},
        RuntimeError("model failed"),
    ]

    result = asyncio.run(tool.prepare_prediction_main("model"))

    assert result is None
    assert tool.review_coverage.status == "partial"
    assert tool.coverage_failure_message


def test_improve_partial_mode_keeps_successful_suggestions(monkeypatch):
    _settings(monkeypatch, fail_closed=False)
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 230)
    tool = _tool([_file("a.py", "a" * 75), _file("b.py", "b" * 75)])
    tool._get_prediction.side_effect = [
        {"code_suggestions": [_suggestion("a.py")]},
        RuntimeError("model failed"),
    ]

    result = asyncio.run(tool.prepare_prediction_main("model"))

    assert tool.review_coverage.status == "partial"
    assert len(result["code_suggestions"]) == 1


def test_improve_capacity_failure_calls_no_model(monkeypatch):
    _settings(monkeypatch, max_chunks=1)
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 230)
    tool = _tool([_file("a.py", "a" * 75), _file("b.py", "b" * 75)])

    result = asyncio.run(tool.prepare_prediction_main("model"))

    assert result is None
    tool._get_prediction.assert_not_awaited()
    assert tool.review_chunk_plan.status == "capacity_exceeded"


def test_improve_reduce_removes_duplicate_suggestions(monkeypatch):
    _settings(monkeypatch)
    monkeypatch.setattr("pr_agent.algo.review_chunking.get_max_tokens", lambda model: 230)
    tool = _tool([_file("a.py", "a" * 75), _file("b.py", "b" * 75)])
    duplicate = _suggestion("a.py")
    tool._get_prediction.side_effect = [
        {"code_suggestions": [duplicate]},
        {"code_suggestions": [dict(duplicate)]},
    ]

    result = asyncio.run(tool.prepare_prediction_main("model"))

    assert len(result["code_suggestions"]) == 1


def test_coverage_failure_is_not_published_as_no_suggestions(monkeypatch):
    published = []
    settings = SimpleNamespace(
        config=SimpleNamespace(publish_output=True),
        set=lambda key, value: None,
    )
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.get_settings", lambda: settings)
    tool = object.__new__(PRCodeSuggestions)
    tool.coverage_failure_message = "processed 1/2 units"
    tool.git_provider = SimpleNamespace(
        remove_initial_comment=lambda: published.append("removed"),
        publish_comment=lambda message: published.append(message),
    )

    handled = tool._publish_map_reduce_coverage_failure()

    assert handled
    assert published == ["removed", "⚠️ processed 1/2 units"]
