import subprocess

import pr_agent.config_loader  # noqa: F401 - initialize settings before ut_agent package
from ut_agent.repair_safety import _changed_member_pairs, validate_member_substitutions


def _init_repo(tmp_path, content: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "handler.cpp").write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "handler.cpp"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    return repo


def test_unproven_member_substitution_is_unsafe(tmp_path):
    repo = _init_repo(tmp_path, "void handle(Request *request) { use(request->node_name); }\n")
    (repo / "handler.cpp").write_text(
        "void handle(Request *request) { use(request->target); }\n",
        encoding="utf-8",
    )

    safe, reason = validate_member_substitutions(str(repo))

    assert safe is False
    assert "node_name" in reason
    assert "target" in reason


def test_external_contract_without_new_member_keeps_substitution_unsafe(tmp_path):
    repo = _init_repo(tmp_path, "void handle(Request *request) { use(request->node_name); }\n")
    (repo / "handler.cpp").write_text(
        "void handle(Request *request) { use(request->target); }\n",
        encoding="utf-8",
    )
    contract = "int64 timestamp_ns\nuint32 command\nstring trace_id\nstring optional\n"

    safe, reason = validate_member_substitutions(str(repo), [contract])

    assert safe is False
    assert "target" in reason


def test_external_contract_can_prove_new_member(tmp_path):
    repo = _init_repo(tmp_path, "void handle(Request *request) { use(request->node_name); }\n")
    (repo / "handler.cpp").write_text(
        "void handle(Request *request) { use(request->target); }\n",
        encoding="utf-8",
    )

    assert validate_member_substitutions(str(repo), ["string target\n"]) == (True, "")


def test_existing_head_declaration_allows_member_substitution(tmp_path):
    repo = _init_repo(
        tmp_path,
        "struct Request { std::string target; };\n"
        "void handle(Request *request) { use(request->node_name); }\n",
    )
    (repo / "handler.cpp").write_text(
        "struct Request { std::string target; };\n"
        "void handle(Request *request) { use(request->target); }\n",
        encoding="utf-8",
    )

    assert validate_member_substitutions(str(repo)) == (True, "")


def test_removing_obsolete_member_access_without_replacement_is_allowed(tmp_path):
    repo = _init_repo(tmp_path, "void handle(Request *request) { use(request->node_name); }\n")
    (repo / "handler.cpp").write_text("void handle(Request *request) {}\n", encoding="utf-8")

    assert validate_member_substitutions(str(repo)) == (True, "")


def test_reformatted_member_removal_is_not_a_substitution():
    diff = """diff --git a/handler.cpp b/handler.cpp
--- a/handler.cpp
+++ b/handler.cpp
@@ -1,2 +1 @@
-log(request->command,
-    request->node_name, request->trace_id);
+log(request->command, request->trace_id);
"""

    assert _changed_member_pairs(diff) == []


def test_gbk_head_source_does_not_abort_validation(tmp_path):
    repo = _init_repo(
        tmp_path,
        "struct Request { int target; };\n"
        "void handle(Request *request) { use(request->node_name); }\n",
    )
    (repo / "legacy.h").write_bytes("// 节点配置\n".encode("gb18030"))
    subprocess.run(["git", "add", "legacy.h"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "legacy"], cwd=repo, check=True, capture_output=True)
    (repo / "handler.cpp").write_text(
        "struct Request { int target; };\n"
        "void handle(Request *request) { use(request->target); }\n",
        encoding="utf-8",
    )

    assert validate_member_substitutions(str(repo)) == (True, "")


def test_member_name_inside_head_string_literal_is_not_evidence(tmp_path):
    repo = _init_repo(
        tmp_path,
        'const char *help = "request->target";\n'
        "void handle(Request *request) { use(request->node_name); }\n",
    )
    (repo / "handler.cpp").write_text(
        'const char *help = "request->target";\n'
        "void handle(Request *request) { use(request->target); }\n",
        encoding="utf-8",
    )

    safe, _reason = validate_member_substitutions(str(repo))

    assert safe is False
