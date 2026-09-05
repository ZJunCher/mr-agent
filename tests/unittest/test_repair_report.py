import json
import subprocess

import pr_agent.config_loader  # noqa: F401 - initialize settings before ut_agent package
from ut_agent.repair_report import (
    REPAIR_REPORT_END,
    REPAIR_REPORT_START,
    capture_repair_diff,
    parse_repair_report,
)
from ut_agent.tools.generate_code import _generate_result


def _report(**overrides) -> str:
    payload = {
        "schema_version": 1,
        "root_cause_summary": "请求类型中不存在 node_name 字段。",
        "solution_summary": "移除对不存在字段的访问。",
        "rationale": "使实现与接口的真实字段定义保持一致。",
        "file_explanations": [
            {
                "path": "src/a.cpp",
                "summary": "删除 request->node_name 的无效访问。",
            }
        ],
        **overrides,
    }
    return f"{REPAIR_REPORT_START}\n{json.dumps(payload, ensure_ascii=False)}\n{REPAIR_REPORT_END}"


def test_parse_repair_report_ignores_prompt_echo_and_keeps_real_files():
    text = "修改 src/a.cpp 前必须检查，不得猜测字段。\n" + _report()

    report = parse_repair_report(text, ["src/a.cpp"])

    assert report is not None
    assert report.solution_summary == "移除对不存在字段的访问。"
    assert report.rationale == "使实现与接口的真实字段定义保持一致。"
    assert tuple(item.path for item in report.file_explanations) == ("src/a.cpp",)
    assert "不得猜测" not in report.solution_summary


def test_parse_repair_report_uses_the_final_complete_record():
    text = _report(solution_summary="旧方案") + "\nnoise\n" + _report(solution_summary="最终方案")

    report = parse_repair_report(text, ["src/a.cpp"])

    assert report is not None
    assert report.solution_summary == "最终方案"


def test_parse_repair_report_rejects_invalid_or_incomplete_records():
    assert parse_repair_report("普通诊断文本", ["src/a.cpp"]) is None
    assert parse_repair_report(f"{REPAIR_REPORT_START}\n{{bad json}}\n{REPAIR_REPORT_END}", ["src/a.cpp"]) is None
    assert parse_repair_report(_report(solution_summary=""), ["src/a.cpp"]) is None
    assert parse_repair_report(_report(schema_version=2), ["src/a.cpp"]) is None


def test_parse_repair_report_intersects_paths_with_real_changes():
    text = _report(file_explanations=[
        {"path": "src/a.cpp", "summary": "修改真实文件。"},
        {"path": "src/not-changed.cpp", "summary": "模型声称修改。"},
        {"path": "../secret", "summary": "越界路径。"},
    ])

    report = parse_repair_report(text, ["src/a.cpp", "src/b.cpp"])

    assert report is not None
    assert [item.path for item in report.file_explanations] == ["src/a.cpp"]


def test_parse_repair_report_sanitizes_secrets_and_bounds_text():
    text = _report(
        root_cause_summary="authorization=very-secret " + "x" * 1000,
        file_explanations=[{"path": "src/a.cpp", "summary": "token=secret-value 修改字段"}],
    )

    report = parse_repair_report(text, ["src/a.cpp"])

    assert report is not None
    assert "very-secret" not in report.root_cause_summary
    assert len(report.root_cause_summary) <= 500
    assert "secret-value" not in report.file_explanations[0].summary


def test_generate_result_attaches_only_a_valid_structured_repair_report(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    diagnostic = "过程文字不能成为方案\n" + _report()

    result = json.loads(_generate_result(
        "changed",
        "repair",
        "build_release_arm64",
        str(repo),
        [str(repo / "src/a.cpp")],
        diagnostic,
        "Hermes 已完成修复。",
    ))

    assert result["repair_report"]["solution_summary"] == "移除对不存在字段的访问。"
    assert result["repair_report"]["file_explanations"][0]["path"] == "src/a.cpp"


def test_generate_result_omits_malformed_repair_report(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    result = json.loads(_generate_result(
        "changed",
        "repair",
        "build_release_arm64",
        str(repo),
        [str(repo / "src/a.cpp")],
        "修改 src/a.cpp，不得猜测字段。",
        "Hermes 已完成修复。",
    ))

    assert "repair_report" not in result


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def test_capture_repair_diff_reads_real_modified_and_added_files(tmp_path):
    repo = _repo(tmp_path)
    source = repo / "src"
    source.mkdir()
    existing = source / "a.cpp"
    existing.write_text("int value = 1;\nreturn value;\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    existing.write_text("int value = 2;\nreturn value;\n", encoding="utf-8")
    added = source / "b.cpp"
    added.write_text("int added = 1;\n", encoding="utf-8")

    changes = capture_repair_diff(str(repo), [str(existing), str(added)])

    assert [item["path"] for item in changes] == ["src/a.cpp", "src/b.cpp"]
    modified = changes[0]
    assert modified["change_type"] == "modified"
    assert modified["additions"] == 1
    assert modified["deletions"] == 1
    assert [line["kind"] for line in modified["hunks"][0]["lines"]] == ["deletion", "addition", "context"]
    assert changes[1]["change_type"] == "added"
    assert changes[1]["additions"] == 1


def test_capture_repair_diff_marks_deleted_and_binary_files(tmp_path):
    repo = _repo(tmp_path)
    text_file = repo / "remove.txt"
    binary_file = repo / "image.bin"
    text_file.write_text("remove me\n", encoding="utf-8")
    binary_file.write_bytes(b"\x00\x01before")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    text_file.unlink()
    binary_file.write_bytes(b"\x00\x02after")

    changes = capture_repair_diff(str(repo), [str(text_file), str(binary_file)])
    by_path = {item["path"]: item for item in changes}

    assert by_path["remove.txt"]["change_type"] == "deleted"
    assert by_path["remove.txt"]["deletions"] == 1
    assert by_path["image.bin"]["binary"] is True
    assert by_path["image.bin"]["hunks"] == []


def test_capture_repair_diff_bounds_lines_and_rejects_paths_outside_repo(tmp_path):
    repo = _repo(tmp_path)
    source = repo / "large.cpp"
    source.write_text("old\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    source.write_text("\n".join(f"line {index} " + "x" * 100 for index in range(20)) + "\n", encoding="utf-8")

    changes = capture_repair_diff(
        str(repo),
        [str(source), str(tmp_path / "outside.cpp"), "../secret"],
        max_lines_per_file=5,
        max_line_chars=32,
    )

    assert len(changes) == 1
    assert changes[0]["path"] == "large.cpp"
    assert changes[0]["truncated"] is True
    assert changes[0]["omitted_lines"] > 0
    assert all(
        len(line["content"]) <= 32
        for hunk in changes[0]["hunks"]
        for line in hunk["lines"]
    )


def test_generate_result_attaches_git_file_changes(tmp_path):
    repo = _repo(tmp_path)
    source = repo / "src"
    source.mkdir()
    changed = source / "a.cpp"
    changed.write_text("int value = 1;\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    changed.write_text("int value = 2;\n", encoding="utf-8")

    result = json.loads(_generate_result(
        "changed",
        "repair",
        "build_release_arm64",
        str(repo),
        [str(changed)],
        _report(),
        "Hermes 已完成修复。",
    ))

    assert result["file_changes"][0]["path"] == "src/a.cpp"
    assert result["file_changes"][0]["additions"] == 1
    assert result["file_changes"][0]["deletions"] == 1
