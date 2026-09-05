import pytest

from ut_agent.config import _parse_validation_profile


def test_profile_parses_lint_build_and_test_stages():
    profile = _parse_validation_profile("group/repo", {
        "lint_argv": ["ruff", "check", "."],
        "build_argv": ["cmake", "--build", "build"],
        "test_argv": ["pytest", "-q"],
        "working_directory": "src",
        "timeout_seconds": 120,
    })

    assert profile.configured_checks == ("lint_check", "build_check", "test_check")
    assert profile.effective_test_argv == ("pytest", "-q")
    assert profile.working_directory == "src"


def test_profile_preserves_legacy_unit_test_command():
    profile = _parse_validation_profile("group/repo", {"unit_test_argv": ["pytest", "-q"]})

    assert profile.configured_checks == ("unit_test_check",)
    assert profile.effective_test_argv == ("pytest", "-q")


@pytest.mark.parametrize("value", [
    {},
    {"test_argv": []},
    {"test_argv": ["pytest"], "unit_test_argv": ["pytest"]},
    {"build_argv": ["cmake"], "working_directory": "../outside"},
])
def test_profile_rejects_missing_ambiguous_or_unsafe_configuration(value):
    with pytest.raises(ValueError):
        _parse_validation_profile("group/repo", value)
