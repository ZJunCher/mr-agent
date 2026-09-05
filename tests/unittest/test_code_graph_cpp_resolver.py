import os
import tempfile

from pr_agent.algo.code_graph.cpp_resolver import extract_cpp_includes


def _write(root, relpath, content):
    abs_path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as f:
        f.write(content)


def test_quoted_include_relative_to_including_file_dir():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "src/helper.h", "")
        content = '#include "helper.h"\n'
        deps = extract_cpp_includes("src/main.cpp", content, root)
        assert deps == ["src/helper.h"]


def test_quoted_include_prefers_including_file_dir_over_project_root():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "helper.h", "// root\n")
        _write(root, "src/helper.h", "// source dir\n")
        deps = extract_cpp_includes("src/main.cpp", '#include "helper.h"\n', root)
        assert deps == ["src/helper.h"]


def test_quoted_include_relative_to_project_root():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "include/api.h", "")
        content = '#include "include/api.h"\n'
        deps = extract_cpp_includes("src/main.cpp", content, root)
        assert deps == ["include/api.h"]


def test_system_include_is_skipped():
    with tempfile.TemporaryDirectory() as root:
        content = "#include <vector>\n"
        deps = extract_cpp_includes("src/main.cpp", content, root)
        assert deps == []


def test_include_escaping_project_root_is_rejected():
    with tempfile.TemporaryDirectory() as root:
        outside_dir = tempfile.mkdtemp()
        _write(outside_dir, "secret.h", "")
        content = '#include "../../../../../../../../etc/secret.h"\n'
        deps = extract_cpp_includes("src/main.cpp", content, root)
        assert deps == []


def test_missing_include_target_is_skipped():
    with tempfile.TemporaryDirectory() as root:
        content = '#include "does_not_exist.h"\n'
        deps = extract_cpp_includes("src/main.cpp", content, root)
        assert deps == []


def test_multiple_includes_deduplicated_and_ordered():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "src/a.h", "")
        _write(root, "src/b.h", "")
        content = '#include "a.h"\n#include "b.h"\n#include "a.h"\n'
        deps = extract_cpp_includes("src/main.cpp", content, root)
        assert deps == ["src/a.h", "src/b.h"]
