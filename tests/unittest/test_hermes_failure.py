import pytest

from ut_agent.hermes_failure import extract_hermes_control_failure


@pytest.mark.parametrize(
    "line",
    [
        "⚠️  API call failed (attempt 3/3): InternalServerError [HTTP 503]",
        "❌ API failed after 3 retries — HTTP 503: No available channel for model claude-opus-4-8",
        "💀 Final error: HTTP 429: rate limited",
    ],
)
def test_extracts_current_hermes_provider_failures(line):
    assert extract_hermes_control_failure([line], [])


def test_extracts_current_failure_with_ansi_and_wrapped_summary():
    lines = [
        "\x1b[31m❌ API failed after 3 retries — HTTP 503: No available channel\x1b[0m",
        " API call failed after 3 retries: HTTP 503: No available channel for model",
    ]

    result = extract_hermes_control_failure(lines, [])

    assert result
    assert "\x1b" not in result
    assert "HTTP 503" in result


def test_ignores_auxiliary_title_failure_without_main_request_failure():
    lines = ["⚠ Auxiliary title generation failed: HTTP 503: unavailable"]

    assert extract_hermes_control_failure(lines, []) is None


def test_does_not_classify_compiler_prose_as_provider_failure():
    lines = [
        "test.cpp:42: error: expected HTTP 503 response",
        "the API call failed because the mocked application returned an error",
    ]

    assert extract_hermes_control_failure(lines, []) is None


def test_redacts_request_ids_and_honors_limit():
    lines = [
        "❌ API failed after 3 retries — HTTP 503: unavailable "
        "(request id: 202608220547164275322848268d9d6QDB8XTxQ) "
        "(request_id=req-secret-value) "
        + "detail " * 500
    ]

    result = extract_hermes_control_failure(lines, [], limit=180)

    assert result
    assert len(result) <= 180
    assert "202608220547164275322848268d9d6QDB8XTxQ" not in result
    assert "req-secret-value" not in result
    assert "[REDACTED]" in result
