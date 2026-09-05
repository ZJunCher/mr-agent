import os
import tempfile

from pr_agent.algo.code_graph.python_resolver import extract_python_imports


def _write(root, relpath, content):
    abs_path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as f:
        f.write(content)


def test_absolute_import_resolves_to_submodule_file():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "pkg/__init__.py", "")
        _write(root, "pkg/mod.py", "VALUE = 1\n")
        deps = extract_python_imports("main.py", "import pkg.mod\n", root)
        assert deps == ["pkg/mod.py"]


def test_from_import_resolves_concrete_submodule():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "pkg/__init__.py", "")
        _write(root, "pkg/mod.py", "VALUE = 1\n")
        deps = extract_python_imports("main.py", "from pkg import mod\n", root)
        assert deps == ["pkg/mod.py"]


def test_relative_import_single_dot_same_package():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "pkg/__init__.py", "")
        _write(root, "pkg/sibling.py", "VALUE = 1\n")
        deps = extract_python_imports("pkg/file_a.py", "from . import sibling\n", root)
        assert deps == ["pkg/sibling.py"]


def test_relative_import_double_dot_parent_package():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "pkg/__init__.py", "")
        _write(root, "pkg/helper.py", "VALUE = 1\n")
        _write(root, "pkg/sub/__init__.py", "")
        deps = extract_python_imports("pkg/sub/file_a.py", "from .. import helper\n", root)
        assert deps == ["pkg/helper.py"]


def test_init_py_reexport_is_chased_to_submodule():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "pkg/__init__.py", "from .pr_reviewer import PRReviewer\n")
        _write(root, "pkg/pr_reviewer.py", "class PRReviewer: pass\n")
        deps = extract_python_imports("main.py", "from pkg import PRReviewer\n", root)
        assert deps == ["pkg/pr_reviewer.py"]


def test_init_py_reexport_falls_back_to_init_when_not_found():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "pkg/__init__.py", "SOMETHING_ELSE = 1\n")
        deps = extract_python_imports("main.py", "from pkg import Unresolvable\n", root)
        assert deps == ["pkg/__init__.py"]


def test_from_import_resolves_subpackage_init_file():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "pkg/__init__.py", "")
        _write(root, "pkg/subpkg/__init__.py", "")
        deps = extract_python_imports("main.py", "from pkg import subpkg\n", root)
        assert deps == ["pkg/subpkg/__init__.py"]


def test_package_init_does_not_emit_self_edge_for_unresolved_import():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "pkg/__init__.py", "from . import missing\n")
        deps = extract_python_imports("pkg/__init__.py", "from . import missing\n", root)
        assert deps == []


def test_dynamic_import_is_never_resolved():
    with tempfile.TemporaryDirectory() as root:
        _write(root, "pkg/mod.py", "VALUE = 1\n")
        content = "import importlib\nmodule_name = 'pkg.mod'\nm = importlib.import_module(module_name)\n"
        deps = extract_python_imports("main.py", content, root)
        assert deps == []


def test_file_with_syntax_error_returns_empty_list():
    deps = extract_python_imports("broken.py", "def f(:\n", "/tmp")
    assert deps == []


def test_import_of_missing_module_is_skipped():
    with tempfile.TemporaryDirectory() as root:
        deps = extract_python_imports("main.py", "import does_not_exist\n", root)
        assert deps == []
