import pytest

from pr_agent.algo.model_resilience import (
    ModelFailureKind,
    classify_model_failure,
    is_transient_model_failure,
    sanitize_model_error,
)


@pytest.mark.parametrize(
    ("error", "kind", "transient"),
    [
        (TimeoutError("timed out"), ModelFailureKind.TIMEOUT, True),
        (RuntimeError("HTTP 429 rate limit"), ModelFailureKind.RATE_LIMIT, True),
        (RuntimeError("HTTP 503 unavailable"), ModelFailureKind.SERVER, True),
        (ConnectionError("connection reset"), ModelFailureKind.CONNECTION, True),
        (RuntimeError("HTTP 401 invalid api key"), ModelFailureKind.AUTHORIZATION, False),
        (RuntimeError("model deployment not found"), ModelFailureKind.CONFIGURATION, False),
        (RuntimeError("HTTP 400 context length"), ModelFailureKind.REQUEST, False),
    ],
)
def test_classify_model_failure(error, kind, transient):
    failure = classify_model_failure(error)

    assert failure is kind
    assert is_transient_model_failure(failure) is transient


def test_sanitize_model_error_redacts_credentials_and_truncates():
    error = RuntimeError(
        "Authorization: Bearer secret-token api_key=secret password=hunter2 " + "long-body " * 100
    )

    message = sanitize_model_error(error)

    assert "secret-token" not in message
    assert "api_key=secret" not in message
    assert "hunter2" not in message
    assert message.count("[REDACTED]") == 3
    assert len(message) <= 300
