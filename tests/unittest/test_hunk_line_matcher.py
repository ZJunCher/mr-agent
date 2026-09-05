from pr_agent.algo.hunk_line_matcher import find_lines_in_new_hunk

_DIFF = """## File: 'src/file1.py'

@@ ... @@ def func1():
__new hunk__
11  def func1():
12      x = 1
13 +    y = 2
14      return x + y
__old hunk__
 def func1():
     x = 1
     return x


## File: 'src/file2.py'

@@ ... @@ def func2():
__new hunk__
40  def func2():
41 +    z = compute()
42      return z
"""


def test_finds_single_added_line():
    result = find_lines_in_new_hunk(_DIFF, "src/file1.py", "    y = 2")
    assert result == (13, 13)


def test_finds_multi_line_span():
    result = find_lines_in_new_hunk(_DIFF, "src/file1.py", "    x = 1\n    y = 2")
    assert result == (12, 13)


def test_matches_in_second_file_only():
    result = find_lines_in_new_hunk(_DIFF, "src/file2.py", "    z = compute()")
    assert result == (41, 41)


def test_returns_none_when_file_not_in_diff():
    result = find_lines_in_new_hunk(_DIFF, "src/does_not_exist.py", "    y = 2")
    assert result is None


def test_returns_none_when_code_not_in_new_hunk():
    # "return x" only appears in the __old hunk__ section (it was removed),
    # not in __new hunk__ — must not match there.
    result = find_lines_in_new_hunk(_DIFF, "src/file1.py", "    return x")
    assert result is None


def test_returns_none_for_empty_existing_code():
    assert find_lines_in_new_hunk(_DIFF, "src/file1.py", "") is None


def test_tolerates_surrounding_whitespace_differences():
    # LLM-produced existing_code sometimes loses/gains indentation.
    result = find_lines_in_new_hunk(_DIFF, "src/file1.py", "y = 2")
    assert result == (13, 13)
