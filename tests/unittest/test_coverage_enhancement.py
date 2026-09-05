import json

import ut_agent.coverage_enhancement as module
from ut_agent.coverage_enhancement import (
    CoverageEnhancementRequest,
    run_coverage_enhancement,
    validate_coverage_changed_paths,
)


def _request():
    return CoverageEnhancementRequest(17, "x86_64_ut_coverage_check", 63.04, 80.0, 549, "feature")


def _report():
    return {
        "available": True,
        "report_text": "文件: src/a.cpp\n- L10: branch",
        "summary": {"coverage_pct": 63.04, "uncovered": 1},
        "files": [{"path": "src/a.cpp", "uncovered": [{"line": 10, "code": "branch"}]}],
    }


def test_enhancement_fetches_report_generates_tests_and_pushes(monkeypatch):
    generate = lambda **_kwargs: json.dumps({
        "status": "changed", "changed_files": ["tests/unit/a_test.cpp", "tests/unit/CMakeLists.txt"],
    })
    push = lambda **_kwargs: json.dumps({
        "status": "success", "changed": True, "commit_sha": "b" * 40,
    })

    result = run_coverage_enhancement(
        _request(),
        {"mr_id": 549, "source_branch": "feature"},
        fetch_report=lambda _job_id: _report(),
        generate=generate,
        push=push,
    )

    assert result.status == "pushed"
    assert result.commit_sha == "b" * 40
    assert result.uncovered_line_count == 1


def test_enhancement_skips_empty_report(monkeypatch):
    report = _report()
    report["files"] = []
    report["summary"] = {"uncovered": 0}
    assert run_coverage_enhancement(_request(), {}, fetch_report=lambda _job_id: report).status == "skipped"


def test_enhancement_discards_production_changes(monkeypatch):
    discarded = []
    generate = lambda **_kwargs: json.dumps({
        "status": "changed", "changed_files": ["src/engine.cpp"],
    })

    result = run_coverage_enhancement(
        _request(),
        {"mr_id": 549},
        fetch_report=lambda _job_id: _report(),
        generate=generate,
        discard=lambda **kwargs: discarded.append(kwargs) or json.dumps({"status": "success"}),
    )

    assert result.status == "unsafe_changes"
    assert len(discarded) == 1


def test_path_policy_accepts_only_test_code_and_test_registration():
    assert validate_coverage_changed_paths((
        "tests/unit/a_test.cpp",
        "tests/unit/CMakeLists.txt",
        "pkg/test_math.py",
    )).ok
    assert not validate_coverage_changed_paths(("src/engine.cpp",)).ok
    assert not validate_coverage_changed_paths((".gitlab-ci.yml",)).ok
    assert not validate_coverage_changed_paths(("../tests/a_test.cpp",)).ok
