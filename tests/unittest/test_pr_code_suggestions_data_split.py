"""generate_suggestions_data() must run the same model-call + scenario-gate
pipeline run() uses internally, but return the structured data WITHOUT
rendering or publishing anything -- so callers (e.g. pr_mr_create.py) can do
extra work (like publishing inline suggestions) between generation and
rendering."""
import asyncio

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


def _make_instance(monkeypatch, prediction_data):
    instance = object.__new__(PRCodeSuggestions)
    instance.retry_options = None

    async def fake_retry(coro_func, model_type=None, **kwargs):
        instance.retry_options = kwargs
        return prediction_data

    async def fake_validate(data):
        return data

    monkeypatch.setattr(
        "pr_agent.tools.pr_code_suggestions.retry_with_fallback_models", fake_retry)
    instance.validate_suggestions_scenario_constraints = fake_validate
    return instance


def test_returns_structured_data_without_rendering(monkeypatch):
    prediction = {"code_suggestions": [{"relevant_file": "a.py", "score": 9}]}
    instance = _make_instance(monkeypatch, prediction)
    result = asyncio.run(instance.generate_suggestions_data())
    assert result == prediction
    assert instance.data == prediction
    assert instance.retry_options["retry_limit"] == 2
    assert instance.retry_options["include_independent"] is True
    assert instance.retry_options["on_failure"] == instance._record_model_attempt_failure


def test_defaults_to_empty_code_suggestions_when_prediction_is_falsy(monkeypatch):
    instance = _make_instance(monkeypatch, None)
    result = asyncio.run(instance.generate_suggestions_data())
    assert result == {"code_suggestions": []}
    assert instance.data == {"code_suggestions": []}
