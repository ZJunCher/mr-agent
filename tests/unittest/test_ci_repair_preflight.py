import pytest

import pr_agent.config_loader  # noqa: F401 - initialize Dynaconf before eager ut_agent imports
from ut_agent.blocker_evidence import validate_blocker_record
from ut_agent.ci_repair_preflight import build_ci_environment_blocker, build_ci_repair_preflight


def test_upload_pack_user_cancellation_is_ci_environment_blocker():
    blocker = build_ci_environment_blocker(
        "build_release_arm64",
        ("remote: rpc error: code = Canceled desc = running upload-pack: user canceled the request",),
    )

    assert blocker is not None
    assert blocker["blocker_type"] == "ci_environment"
    assert blocker["job_name"] == "build_release_arm64"
    assert validate_blocker_record(blocker, "build_release_arm64") is None


@pytest.mark.parametrize(
    "line",
    [
        "fatal: repository not found",
        "permission denied while fetching dependency",
        "error: Request has no member named node_name",
        "generic network timeout",
        "upload-pack completed after user canceled another operation",
    ],
)
def test_non_approved_failures_do_not_short_circuit(line):
    assert build_ci_environment_blocker("build_release_arm64", (line,)) is None


def test_classifier_ignores_empty_or_non_string_evidence():
    assert build_ci_environment_blocker("build_release_arm64", ()) is None
    assert build_ci_environment_blocker("build_release_arm64", ("", None)) is None


def test_branch_not_found_suppresses_later_upload_pack_cancellation_shortcut():
    blocker = build_ci_repair_preflight(
        "build_release_arm64",
        (
            "[Build] ci_deps file not found or download failed (HTTP 404), using default deps.yml",
            "fatal: Remote branch joint/e2e/da_mini/830 not found in upstream origin",
            "remote: rpc error: code = Canceled desc = running upload-pack: user canceled the request",
        ),
    )

    assert blocker is not None
    assert blocker["blocker_type"] == "external_dependency"
    assert "joint/e2e/da_mini/830" in blocker["ci_evidence"][0]["observation"]


def test_ci_deps_fallback_with_missing_package_config_is_environment_blocker():
    blocker = build_ci_repair_preflight(
        "build_release_arm64",
        (
            "Could not find a package configuration file provided by \"eabot_cmake\"",
            "CMake Error at CMakeLists.txt:5 (find_package):",
            "1 package failed: eabot_topics_monitor",
            "[Build] ci_deps file not found or download failed (HTTP 404): "
            "ci_deps/chogori/main/deps.yml, using default deps.yml",
        ),
    )

    assert blocker is not None
    assert blocker["blocker_type"] == "ci_environment"
    assert "ci_deps" in blocker["root_cause"]
    assert validate_blocker_record(blocker, "build_release_arm64") is None


def test_real_multiline_cmake_error_lines_are_recognized():
    """CMake wraps its message across lines; only the Config.cmake line carries an error signal."""
    from ut_agent.ci_diagnostics import extract_diagnostic_candidates, primary_diagnostic

    trace = (
        "2026-08-19T10:34:34.699347Z 01O [Build] ci_deps file not found or download failed "
        "(HTTP 404): ci_deps/chogori/main/deps.yml, using default deps.yml\n"
        "2026-08-19T10:35:16.760782Z 01O CMake Error at CMakeLists.txt:5 (find_package):\n"
        "2026-08-19T10:35:16.760785Z 01O   By not providing \"Findeabot_cmake.cmake\" in "
        "CMAKE_MODULE_PATH this project\n"
        "2026-08-19T10:35:16.760788Z 01O   has asked CMake to find a package configuration file provided by\n"
        "2026-08-19T10:35:16.760790Z 01O   \"eabot_cmake\", but CMake did not find one.\n"
        "2026-08-19T10:35:16.760793Z 01O   Could not find eabot_cmakeConfig.cmake or eabot_cmake-config.cmake.\n"
        "2026-08-19T10:35:20.264674Z 01O   1 package failed: eabot_topics_monitor\n"
        "2026-08-19T10:35:21.278896Z 00O ERROR: Job failed: command terminated with exit code 1\n"
    )
    candidate_set = extract_diagnostic_candidates(trace, identity_key="t", limit=12)
    primary = primary_diagnostic(candidate_set.candidates)
    ranked = ([primary] if primary else []) + [c for c in candidate_set.candidates if c is not primary]

    blocker = build_ci_repair_preflight("build_release_arm64", [c.text for c in ranked])

    assert blocker is not None
    assert blocker["blocker_type"] == "ci_environment"
    assert "ci_deps" in blocker["root_cause"]


def test_missing_package_config_without_deps_fallback_is_not_blocked():
    blocker = build_ci_repair_preflight(
        "build_release_arm64",
        (
            "Could not find a package configuration file provided by \"eabot_cmake\"",
            "CMake Error at CMakeLists.txt:5 (find_package):",
        ),
    )

    assert blocker is None


def test_deps_fallback_alone_without_consequence_is_not_blocked():
    blocker = build_ci_repair_preflight(
        "build_release_arm64",
        (
            "[Build] ci_deps file not found or download failed (HTTP 404): "
            "ci_deps/chogori/main/deps.yml, using default deps.yml",
            "error: 'udp_pub_' was not declared in this scope",
        ),
    )

    assert blocker is None


def test_empty_git_revision_is_classified_as_ci_job_configuration():
    blocker = build_ci_repair_preflight(
        "code_format_check",
        ("ERROR: git diff failed: fatal: ambiguous argument '': unknown revision or path",),
    )

    assert blocker is not None
    assert blocker["blocker_type"] == "ci_environment"
    assert "基准版本为空" in blocker["root_cause"]
