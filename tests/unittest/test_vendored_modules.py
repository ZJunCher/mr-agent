"""Task 3a: vendored Hermes 模块独立可用性测试。

验证：
1. 三个 vendored 模块能独立导入（无 pr_agent 依赖）。
2. fuzzy_match 的核心策略在典型场景下工作。
3. path_security 的路径逃逸检测工作。
4. patch_parser 的 V4A 解析工作。
5. PatchResult 数据类可用。
"""
import pytest

import pr_agent.config_loader  # noqa: F401  # Initialize settings before importing the eager ut_agent package.
from ut_agent.tools._vendored.fuzzy_match import (
    fuzzy_find_and_replace,
    is_already_applied,
    format_no_match_hint,
    find_closest_lines,
)
from ut_agent.tools._vendored.path_security import (
    validate_within_dir,
    has_traversal_component,
)
from ut_agent.tools._vendored.patch_parser import (
    parse_v4a_patch,
    PatchResult,
    OperationType,
    PatchOperation,
)


class TestFuzzyMatchExact:
    """策略 1：精确匹配。"""

    def test_exact_match_replaces_text(self):
        content = "def foo():\n    pass\n"
        new, count, strategy, err = fuzzy_find_and_replace(content, "def foo():", "def bar():")
        assert err is None
        assert count == 1
        assert strategy == "exact"
        assert "def bar():" in new
        assert "def foo():" not in new

    def test_exact_match_rejects_identical_strings(self):
        content = "hello world"
        new, count, strategy, err = fuzzy_find_and_replace(content, "hello", "hello")
        assert err is not None
        assert count == 0

    def test_exact_match_rejects_empty_old_string(self):
        content = "hello"
        _, count, _, err = fuzzy_find_and_replace(content, "", "x")
        assert count == 0
        assert err is not None

    def test_exact_match_rejects_whitespace_only_old_string(self):
        content = "hello"
        _, count, _, err = fuzzy_find_and_replace(content, "   ", "x")
        assert count == 0
        assert err is not None


class TestFuzzyMatchWhitespaceNormalized:
    """策略 3：空白归一化匹配。"""

    def test_matches_with_extra_spaces_in_file(self):
        content = "def    foo():\n    pass\n"
        new, count, strategy, err = fuzzy_find_and_replace(content, "def foo():", "def bar():")
        assert err is None
        assert count == 1
        assert "def bar():" in new

    def test_matches_with_tabs_vs_spaces(self):
        content = "def\tfoo():\n\tpass\n"
        new, count, strategy, err = fuzzy_find_and_replace(content, "def foo():", "def bar():")
        assert err is None
        assert count == 1


class TestFuzzyMatchIndentationFlexible:
    """策略 4：缩进灵活匹配。"""

    def test_matches_different_indentation(self):
        content = "    def foo():\n        pass\n"
        new, count, strategy, err = fuzzy_find_and_replace(
            content, "def foo():\n    pass", "def bar():\n    pass"
        )
        assert err is None
        assert count == 1
        assert "def bar():" in new


class TestIsAlreadyApplied:
    """已应用检测：避免重复发送已落地的编辑。"""

    def test_detects_new_string_already_present(self):
        content = "def bar():\n    pass\n"
        assert is_already_applied(content, "def foo():", "def bar():\n    pass") is True

    def test_rejects_short_new_string(self):
        content = "x"
        assert is_already_applied(content, "y", "x") is False

    def test_rejects_when_old_string_still_present(self):
        content = "def foo():\ndef bar():\n"
        assert is_already_applied(content, "def foo():", "def bar():") is False


class TestFormatNoMatchHint:
    """失败提示：给出"你是不是想…"建议。"""

    def test_returns_hint_for_similar_lines(self):
        content = "def foo_bar():\n    pass\n\ndef baz():\n    pass\n"
        hint = format_no_match_hint("Could not find a match", 0, "def foo_baz():", content)
        assert "Did you mean" in hint or "foo_bar" in hint

    def test_returns_empty_for_non_no_match_error(self):
        assert format_no_match_hint("Found 3 matches", 0, "x", "y") == ""


class TestPathSecurity:
    """路径逃逸检测。"""

    def test_validate_within_dir_allows_inside_path(self, tmp_path):
        safe = tmp_path / "safe.txt"
        safe.write_text("ok")
        assert validate_within_dir(safe, tmp_path) is None

    def test_validate_within_dir_rejects_escape(self, tmp_path):
        outside = tmp_path.parent / "outside.txt"
        err = validate_within_dir(outside, tmp_path)
        assert err is not None
        assert "escapes" in err.lower()

    def test_has_traversal_component_detects_dot_dot(self):
        assert has_traversal_component("../outside") is True
        assert has_traversal_component("safe/path") is False
        assert has_traversal_component("../../etc/passwd") is True


class TestParseV4aPatch:
    """V4A 补丁解析。"""

    def test_parse_update_file(self):
        patch = """*** Begin Patch
*** Update File: src/example.py
@@ def foo() @@
 def foo()
-    pass
+    return 42
*** End Patch"""
        operations, error = parse_v4a_patch(patch)
        assert error is None
        assert len(operations) == 1
        assert operations[0].operation == OperationType.UPDATE
        assert operations[0].file_path == "src/example.py"
        assert len(operations[0].hunks) == 1
        assert operations[0].hunks[0].context_hint == "def foo()"

    def test_parse_add_file(self):
        patch = """*** Begin Patch
*** Add File: new.py
+print("hello")
+print("world")
*** End Patch"""
        operations, error = parse_v4a_patch(patch)
        assert error is None
        assert operations[0].operation == OperationType.ADD
        assert operations[0].file_path == "new.py"
        # ADD 操作的内容在 hunks[0].lines 里，每行是 HunkLine(prefix='+', content=...)
        add_lines = [l.content for l in operations[0].hunks[0].lines if l.prefix == "+"]
        assert add_lines == ['print("hello")', 'print("world")']

    def test_parse_delete_file(self):
        patch = """*** Begin Patch
*** Delete File: old.py
*** End Patch"""
        operations, error = parse_v4a_patch(patch)
        assert error is None
        assert operations[0].operation == OperationType.DELETE

    def test_parse_malformed_patch_returns_empty(self):
        # parse_v4a_patch 对不含 *** 标记的文本不报错，返回空 operations
        patch = "not a valid patch"
        operations, error = parse_v4a_patch(patch)
        assert operations == []


class TestPatchResult:
    """本地 PatchResult 数据类。"""

    def test_to_dict_includes_success(self):
        result = PatchResult(success=True, diff="+hello")
        d = result.to_dict()
        assert d["success"] is True
        assert d["diff"] == "+hello"

    def test_to_dict_omits_empty_fields(self):
        result = PatchResult(success=False)
        d = result.to_dict()
        assert "diff" not in d
        assert "files_modified" not in d

    def test_to_dict_includes_error(self):
        result = PatchResult(success=False, error="something failed")
        d = result.to_dict()
        assert d["error"] == "something failed"
