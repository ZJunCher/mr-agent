"""Tests for the Tier-2 heavy repair channel (heavy_repair.py)."""
import json
import os
import subprocess
import tempfile

from pr_agent.suggestions.heavy_repair import (
    build_heavy_repair_prompt,
    line_range_in_diff_hunks,
    parse_unified_diff,
    read_manifest,
)


SAMPLE_DIFF = """diff --git a/src/foo.cpp b/src/foo.cpp
index 1234567..89abcde 100644
--- a/src/foo.cpp
+++ b/src/foo.cpp
@@ -10,3 +10,4 @@ void Foo::bar() {
     int x = 1;
-    do_thing(x);
+    do_thing(x);
+    log_it(x);
     return;
diff --git a/src/foo.hpp b/src/foo.hpp
index 1234567..89abcde 100644
--- a/src/foo.hpp
+++ b/src/foo.hpp
@@ -5,2 +5,3 @@ class Foo {
 public:
+    void log_it(int x);
     void bar();
"""


# A real PR diff (base vs head): the '+' side hunk header says lines 10-13
# (inclusive) of the head file were touched by this PR.
PR_DIFF_ONE_HUNK = """diff --git a/src/foo.cpp b/src/foo.cpp
index 1234567..89abcde 100644
--- a/src/foo.cpp
+++ b/src/foo.cpp
@@ -10,3 +10,4 @@ void Foo::bar() {
     int x = 1;
-    do_thing(x);
+    do_thing(x);
+    log_it(x);
     return;
"""


class TestLineRangeInDiffHunks:
    def test_line_range_fully_inside_a_hunk_is_in_diff(self):
        assert line_range_in_diff_hunks(PR_DIFF_ONE_HUNK, 11, 12) is True

    def test_line_range_matching_the_whole_hunk_is_in_diff(self):
        assert line_range_in_diff_hunks(PR_DIFF_ONE_HUNK, 10, 13) is True

    def test_line_range_overlapping_a_hunk_boundary_is_in_diff(self):
        assert line_range_in_diff_hunks(PR_DIFF_ONE_HUNK, 13, 20) is True

    def test_line_range_entirely_outside_any_hunk_is_not_in_diff(self):
        # This is the exact bug scenario: Tier-2 repaired lines 34-39 of
        # ssm.cpp, a file that IS in the PR diff, but that specific line
        # range was never touched by this PR's own hunks.
        assert line_range_in_diff_hunks(PR_DIFF_ONE_HUNK, 34, 39) is False

    def test_no_hunks_at_all_is_not_in_diff(self):
        assert line_range_in_diff_hunks("", 1, 5) is False

    def test_multiple_hunks_checks_each_one(self):
        two_hunks = PR_DIFF_ONE_HUNK + (
            "@@ -50,2 +51,3 @@ void Foo::baz() {\n"
            "     int y = 2;\n"
            "+    log_it(y);\n"
            "     return;\n"
        )
        assert line_range_in_diff_hunks(two_hunks, 51, 53) is True
        assert line_range_in_diff_hunks(two_hunks, 100, 105) is False


class TestParseUnifiedDiff:
    def test_parses_two_files_with_correct_line_ranges(self):
        result = parse_unified_diff(SAMPLE_DIFF)
        assert set(result.keys()) == {"src/foo.cpp", "src/foo.hpp"}
        cpp_hunk = result["src/foo.cpp"][0]
        assert cpp_hunk["old_start"] == 10 and cpp_hunk["old_end"] == 12
        assert cpp_hunk["existing_code"] == "    int x = 1;\n    do_thing(x);\n    return;"
        assert cpp_hunk["improved_code"] == "    int x = 1;\n    do_thing(x);\n    log_it(x);\n    return;"

    def test_parses_against_a_real_git_diff(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q"], cwd=d, check=True)
            subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=d, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
            path = os.path.join(d, "foo.cpp")
            with open(path, "w") as f:
                f.write("line1\nline2\nline3\nvoid Foo::bar() {\n    int x = 1;\n"
                        "    do_thing(x);\n    return;\n}\nline_end\n")
            subprocess.run(["git", "add", "foo.cpp"], cwd=d, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=d, check=True)
            with open(path, "w") as f:
                f.write("line1\nline2\nline3\nvoid Foo::bar() {\n    int x = 1;\n"
                        "    do_thing(x);\n    log_it(x);\n    return;\n}\nline_end\n")
            real_diff = subprocess.run(["git", "diff"], cwd=d, capture_output=True, text=True).stdout
            result = parse_unified_diff(real_diff)
            hunk = result["foo.cpp"][0]
            assert "log_it(x);" not in hunk["existing_code"]
            assert "log_it(x);" in hunk["improved_code"]

    def test_empty_diff_returns_empty_dict(self):
        assert parse_unified_diff("") == {}


class TestBuildHeavyRepairPrompt:
    def test_includes_task_id_and_file_and_issue(self):
        task = {
            "relevant_file": "src/foo.cpp", "structural_issue": "cross_file", "fix_note": "needs companion edit",
            "companion_head_file": "class Foo {};", "members": [{
                "suggestion_content": "add a null check", "relevant_file": "src/foo.cpp",
                "relevant_lines_start": 5, "relevant_lines_end": 6, "companion_file": "src/foo.hpp",
            }],
        }
        prompt = build_heavy_repair_prompt([task], ["SUG-001"])
        assert "SUG-001" in prompt
        assert "src/foo.cpp" in prompt
        assert "cross_file" in prompt
        assert "add a null check" in prompt
        assert "src/foo.hpp" in prompt
        assert "manifest.json" in prompt

    def test_handles_multiple_tasks(self):
        tasks = [
            {"relevant_file": "a.cpp", "structural_issue": "incomplete_patch", "fix_note": "x",
             "companion_head_file": None, "members": [{"suggestion_content": "fix a"}]},
            {"relevant_file": "b.cpp", "structural_issue": "existing_mismatch", "fix_note": "y",
             "companion_head_file": None, "members": [{"suggestion_content": "fix b"}]},
        ]
        prompt = build_heavy_repair_prompt(tasks, ["SUG-001", "SUG-002"])
        assert "SUG-001" in prompt and "SUG-002" in prompt
        assert "fix a" in prompt and "fix b" in prompt


class TestReadManifest:
    def test_reads_valid_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "manifest.json"), "w") as f:
                json.dump({"SUG-001": {"status": "done"}}, f)
            assert read_manifest(d) == {"SUG-001": {"status": "done"}}

    def test_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            assert read_manifest(d) == {}

    def test_malformed_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "manifest.json"), "w") as f:
                f.write("{not valid json")
            assert read_manifest(d) == {}

from pr_agent.suggestions.heavy_repair import run_copilot_cli


class TestRunCopilotCli:
    def test_success_returns_true(self):
        def fake_runner(cmd, cwd, timeout_seconds):
            return 0, "done", ""
        ok, msg = run_copilot_cli("/tmp/repo", "prompt", 60, runner=fake_runner)
        assert ok is True

    def test_nonzero_exit_returns_false(self):
        def fake_runner(cmd, cwd, timeout_seconds):
            return 1, "", "some error"
        ok, msg = run_copilot_cli("/tmp/repo", "prompt", 60, runner=fake_runner)
        assert ok is False

    def test_timeout_returns_false(self):
        def fake_runner(cmd, cwd, timeout_seconds):
            return -1, "", "timeout"
        ok, msg = run_copilot_cli("/tmp/repo", "prompt", 60, runner=fake_runner)
        assert ok is False
        assert "time" in msg.lower()

    def test_runner_exception_returns_false(self):
        def fake_runner(cmd, cwd, timeout_seconds):
            raise RuntimeError("boom")
        ok, msg = run_copilot_cli("/tmp/repo", "prompt", 60, runner=fake_runner)
        assert ok is False

    def test_deny_tools_present_in_command(self):
        captured = {}
        def fake_runner(cmd, cwd, timeout_seconds):
            captured["cmd"] = cmd
            return 0, "", ""
        run_copilot_cli("/tmp/repo", "prompt", 60, runner=fake_runner)
        assert "--deny-tool=shell(git push)" in captured["cmd"]
        assert "--deny-tool=shell(git commit)" in captured["cmd"]
        assert "--deny-tool=shell(rm)" in captured["cmd"]

import asyncio
import types

from pr_agent.suggestions.heavy_repair import classify_heavy_repair_results, run_heavy_repair


def _patch_covering(new_start: int, new_count: int) -> str:
    """Build a minimal valid unified-diff patch whose single hunk's new-side
    ('+') range is [new_start, new_start + new_count - 1]."""
    lines = "\n".join(" context" for _ in range(new_count))
    return f"@@ -{new_start},{new_count} +{new_start},{new_count} @@\n{lines}\n"


class TestClassifyHeavyRepairResults:
    def test_done_in_diff_becomes_one_click(self):
        manifest = {"SUG-001": {"status": "done", "files": ["src/foo.cpp"], "note": "fixed"}}
        file_hunks = {"src/foo.cpp": [{"old_start": 10, "old_end": 12, "existing_code": "a", "improved_code": "b"}]}
        result = classify_heavy_repair_results(
            manifest, file_hunks, {"src/foo.cpp": _patch_covering(10, 3)},
            {"SUG-001": {"relevant_file": "src/foo.cpp",
                         "members": [{"label": "possible bug", "score": 8}]}})
        assert len(result["one_click"]) == 1 and not result["copy_patch"] and not result["failed"]
        assert result["one_click"][0]["resolved_by_stage"] == "tier2_heavy"

    def test_one_click_label_and_score_come_from_task_members_not_task_itself(self):
        # Regression test: a RepairTask (see deterministic_fix.py's _new_task)
        # never carries its own "label"/"score"/"one_sentence_summary" --
        # those live on the ORIGINAL suggestion(s) in task["members"]. Before
        # this fix, classify_heavy_repair_results read them straight off
        # `task`, which always missed and silently fell back to the
        # "possible issue"/score=7 placeholders.
        manifest = {"SUG-001": {"status": "done", "files": ["src/foo.cpp"], "note": "fixed"}}
        file_hunks = {"src/foo.cpp": [{"old_start": 10, "old_end": 12, "existing_code": "a", "improved_code": "b"}]}
        task_by_id = {"SUG-001": {
            "relevant_file": "src/foo.cpp",
            "members": [{"label": "并发性", "score": 9, "one_sentence_summary": "锁竞争风险",
                        "suggestion_content": "why...\nfix..."}],
        }}
        result = classify_heavy_repair_results(
            manifest, file_hunks, {"src/foo.cpp": _patch_covering(10, 3)}, task_by_id)
        sugg = result["one_click"][0]
        assert sugg["label"] == "并发性"
        assert sugg["score"] == 9
        assert sugg["one_sentence_summary"] == "锁竞争风险"

    def test_one_click_preserves_original_suggestion_content_and_severity(self):
        # Regression test: the manifest's `note` (a short one-line recap from
        # the Copilot CLI session) was being used as the PRIMARY
        # suggestion_content, discarding the original suggestion's own
        # content -- which is where "Severity: High" lives, the marker
        # _extract_impact_level/_impact_label parse to render the real
        # impact level. That silently produced "Unspecified"/"未标注" impact
        # on every Tier-2-resolved suggestion. The original content (and the
        # direct `severity` field, when present) must win; `note` is only a
        # fallback for when the original has nothing.
        manifest = {"SUG-001": {"status": "done", "files": ["src/foo.cpp"], "note": "Fixed a data race"}}
        file_hunks = {"src/foo.cpp": [{"old_start": 10, "old_end": 12, "existing_code": "a", "improved_code": "b"}]}
        task_by_id = {"SUG-001": {
            "relevant_file": "src/foo.cpp",
            "members": [{"label": "并发性", "score": 9, "severity": "High",
                        "suggestion_content": "Severity: High\nWhy: real data race\nFix: add a mutex"}],
        }}
        result = classify_heavy_repair_results(
            manifest, file_hunks, {"src/foo.cpp": _patch_covering(10, 3)}, task_by_id)
        sugg = result["one_click"][0]
        assert sugg["suggestion_content"] == "Severity: High\nWhy: real data race\nFix: add a mutex"
        assert sugg["severity"] == "High"

    def test_one_click_falls_back_to_manifest_note_when_original_content_missing(self):
        manifest = {"SUG-001": {"status": "done", "files": ["src/foo.cpp"], "note": "Fixed a data race"}}
        file_hunks = {"src/foo.cpp": [{"old_start": 10, "old_end": 12, "existing_code": "a", "improved_code": "b"}]}
        task_by_id = {"SUG-001": {"relevant_file": "src/foo.cpp", "members": [{"label": "并发性"}]}}
        result = classify_heavy_repair_results(
            manifest, file_hunks, {"src/foo.cpp": _patch_covering(10, 3)}, task_by_id)
        assert result["one_click"][0]["suggestion_content"] == "Fixed a data race"

    def test_done_not_in_diff_becomes_copy_patch(self):
        manifest = {"SUG-002": {"status": "done", "files": ["src/foo.hpp"], "note": "added decl"}}
        file_hunks = {"src/foo.hpp": [{"old_start": 5, "old_end": 6, "existing_code": "a", "improved_code": "b"}]}
        result = classify_heavy_repair_results(
            manifest, file_hunks, {"src/foo.cpp": _patch_covering(1, 5)},
            {"SUG-002": {"relevant_file": "src/foo.hpp"}})
        assert len(result["copy_patch"]) == 1 and not result["one_click"] and not result["failed"]
        assert result["copy_patch"][0]["resolved_by_stage"] == "tier2_copy_patch"

    def test_done_in_diff_file_but_line_range_outside_any_hunk_becomes_copy_patch(self):
        # This is the exact bug scenario reported against gitlab.example.com/
        # eabot/cook MR !492: the file IS in the PR's diff, but Tier-2's
        # repair landed on a line range this PR never touched. Previously
        # this was misclassified as one_click, GitLab rejected the resulting
        # inline suggestion with a 500/no_id_returned, and it silently
        # vanished. It must now be classified as copy_patch instead, so it
        # still reaches the user (as a "not one-click appliable" table row)
        # rather than disappearing.
        manifest = {"SUG-001": {"status": "done", "files": ["src/foo.cpp"], "note": "fixed"}}
        file_hunks = {"src/foo.cpp": [{"old_start": 34, "old_end": 39, "existing_code": "a", "improved_code": "b"}]}
        result = classify_heavy_repair_results(
            manifest, file_hunks, {"src/foo.cpp": _patch_covering(10, 3)},  # PR only touched lines 10-12
            {"SUG-001": {"relevant_file": "src/foo.cpp", "members": [{"label": "possible bug", "score": 8}]}})
        assert len(result["copy_patch"]) == 1 and not result["one_click"] and not result["failed"]
        assert result["copy_patch"][0]["resolved_by_stage"] == "tier2_copy_patch"

    def test_copy_patch_carries_label_score_and_summary_for_table_rendering(self):
        # Regression test: generate_summarized_suggestions silently drops any
        # suggestion missing label/one_sentence_summary/suggestion_content
        # (see its required_keys filter). copy_patch results now feed the
        # /improve table as a "not one-click appliable" row (no longer a
        # standalone comment), so they must carry these fields exactly like
        # one_click does, or they'd vanish from the table entirely.
        manifest = {"SUG-002": {"status": "done", "files": ["src/foo.hpp"], "note": "added decl"}}
        file_hunks = {"src/foo.hpp": [{"old_start": 5, "old_end": 6, "existing_code": "a", "improved_code": "b"}]}
        task_by_id = {"SUG-002": {
            "relevant_file": "src/foo.hpp",
            "members": [{"label": "数值稳定性", "score": 9, "one_sentence_summary": "缺少 NaN/Inf 防护",
                        "suggestion_content": "why...\nfix..."}],
        }}
        result = classify_heavy_repair_results(
            manifest, file_hunks, {"src/foo.cpp": _patch_covering(1, 5)}, task_by_id)
        sugg = result["copy_patch"][0]
        assert sugg["label"] == "数值稳定性"
        assert sugg["score"] == 9
        assert sugg["one_sentence_summary"] == "缺少 NaN/Inf 防护"
        assert sugg["suggestion_content"] == "why...\nfix..."

    def test_failed_status_recorded(self):
        manifest = {"SUG-003": {"status": "failed", "note": "could not determine fix"}}
        result = classify_heavy_repair_results(manifest, {}, {}, {"SUG-003": {"relevant_file": "x.cpp"}})
        assert result["failed"] == [("SUG-003", "could not determine fix")]

    def test_done_but_no_hunk_found_recorded_as_failed(self):
        manifest = {"SUG-004": {"status": "done", "files": ["src/missing.cpp"], "note": "fixed"}}
        result = classify_heavy_repair_results(manifest, {}, {"src/missing.cpp": _patch_covering(1, 5)},
                                               {"SUG-004": {"relevant_file": "src/missing.cpp"}})
        assert len(result["failed"]) == 1 and result["failed"][0][0] == "SUG-004"

    def test_two_files_one_in_diff_one_not_split_across_lists(self):
        manifest = {"SUG-005": {"status": "done", "files": ["src/foo.cpp", "src/foo.hpp"], "note": "n"}}
        file_hunks = {
            "src/foo.cpp": [{"old_start": 1, "old_end": 1, "existing_code": "a", "improved_code": "b"}],
            "src/foo.hpp": [{"old_start": 2, "old_end": 2, "existing_code": "c", "improved_code": "d"}],
        }
        result = classify_heavy_repair_results(
            manifest, file_hunks, {"src/foo.cpp": _patch_covering(1, 3)},
            {"SUG-005": {"relevant_file": "src/foo.cpp"}})
        assert len(result["one_click"]) == 1 and len(result["copy_patch"]) == 1


def _provider_with_repo(diff_files):
    provider = types.SimpleNamespace()
    provider.get_diff_files = lambda: diff_files
    provider.get_pr_branch = lambda: "feature/x"
    provider.id_mr = 42
    provider.pr_url = "https://gitlab.example.com/group/repo/-/merge_requests/42"
    provider.get_git_repo_url = lambda url: "https://gitlab.example.com/group/repo.git"
    provider._prepare_clone_url_with_token = lambda url: "https://oauth2:token@gitlab.example.com/group/repo.git"
    return provider


class TestRunHeavyRepair:
    def test_empty_tasks_returns_empty_result(self):
        provider = _provider_with_repo([])
        result = asyncio.run(run_heavy_repair(provider, []))
        assert result == {"one_click": [], "copy_patch": [], "failed": []}

    def test_clone_failure_marks_all_tasks_failed(self, monkeypatch):
        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "_clone_source_branch", lambda *a, **k: "ERROR: no access")
        provider = _provider_with_repo([])
        task = {"relevant_file": "a.cpp", "structural_issue": "none", "fix_note": "", "members": [],
                "companion_head_file": None, "needs_tier2": True}
        result = asyncio.run(run_heavy_repair(provider, [task]))
        assert not result["one_click"] and not result["copy_patch"]
        assert len(result["failed"]) == 1 and "ERROR" in result["failed"][0][1]

    def test_copilot_failure_marks_all_tasks_failed(self, monkeypatch):
        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "_clone_source_branch", lambda *a, **k: "/tmp/fake_repo")
        monkeypatch.setattr(hr, "run_copilot_cli", lambda *a, **k: (False, "Copilot CLI timed out"))
        provider = _provider_with_repo([])
        task = {"relevant_file": "a.cpp", "structural_issue": "none", "fix_note": "", "members": [],
                "companion_head_file": None, "needs_tier2": True}
        result = asyncio.run(run_heavy_repair(provider, [task]))
        assert len(result["failed"]) == 1 and "timed out" in result["failed"][0][1]

    def test_full_success_path_produces_one_click_result(self, monkeypatch):
        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "_clone_source_branch", lambda *a, **k: "/tmp/fake_repo")
        monkeypatch.setattr(hr, "run_copilot_cli", lambda *a, **k: (True, "ok"))
        monkeypatch.setattr(hr, "read_working_tree_diff", lambda repo_dir: SAMPLE_DIFF)
        monkeypatch.setattr(hr, "read_manifest", lambda repo_dir: {
            "SUG-001": {"status": "done", "files": ["src/foo.cpp"], "note": "fixed"}})
        diff_file = types.SimpleNamespace(filename="src/foo.cpp", head_file="...",
                                          patch="@@ -10,3 +10,4 @@ void Foo::bar() {\n context\n")
        provider = _provider_with_repo([diff_file])
        task = {"relevant_file": "src/foo.cpp", "structural_issue": "existing_mismatch", "fix_note": "x",
                "members": [{"relevant_file": "src/foo.cpp"}], "companion_head_file": None, "needs_tier2": False}
        result = asyncio.run(run_heavy_repair(provider, [task]))
        assert len(result["one_click"]) == 1
        assert result["one_click"][0]["resolved_by_stage"] == "tier2_heavy"

    def test_never_raises_when_provider_methods_raise(self, monkeypatch):
        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "_clone_source_branch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        provider = _provider_with_repo([])
        task = {"relevant_file": "a.cpp", "structural_issue": "none", "fix_note": "", "members": [],
                "companion_head_file": None, "needs_tier2": True}
        result = asyncio.run(run_heavy_repair(provider, [task]))
        assert len(result["failed"]) == 1

class TestTier2DurationThreading:
    def test_one_click_result_carries_tier2_duration_ms(self, monkeypatch):
        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "_clone_source_branch", lambda *a, **k: "/tmp/fake_repo")
        monkeypatch.setattr(hr, "run_copilot_cli", lambda *a, **k: (True, "ok"))
        monkeypatch.setattr(hr, "read_working_tree_diff", lambda repo_dir: SAMPLE_DIFF)
        monkeypatch.setattr(hr, "read_manifest", lambda repo_dir: {
            "SUG-001": {"status": "done", "files": ["src/foo.cpp"], "note": "fixed"}})
        diff_file = types.SimpleNamespace(filename="src/foo.cpp", head_file="...",
                                          patch="@@ -10,3 +10,4 @@ void Foo::bar() {\n context\n")
        provider = _provider_with_repo([diff_file])
        task = {"relevant_file": "src/foo.cpp", "structural_issue": "existing_mismatch", "fix_note": "x",
                "members": [{"relevant_file": "src/foo.cpp"}], "companion_head_file": None, "needs_tier2": False}
        result = asyncio.run(run_heavy_repair(provider, [task]))
        assert len(result["one_click"]) == 1
        assert isinstance(result["one_click"][0]["tier2_duration_ms"], int)
        assert result["one_click"][0]["tier2_duration_ms"] >= 0

    def test_failed_results_have_no_duration_entries(self, monkeypatch):
        import pr_agent.suggestions.heavy_repair as hr
        monkeypatch.setattr(hr, "_clone_source_branch", lambda *a, **k: "ERROR: no access")
        provider = _provider_with_repo([])
        task = {"relevant_file": "a.cpp", "structural_issue": "none", "fix_note": "", "members": [],
                "companion_head_file": None, "needs_tier2": True}
        result = asyncio.run(run_heavy_repair(provider, [task]))
        assert not result["one_click"] and not result["copy_patch"]
        assert len(result["failed"]) == 1
