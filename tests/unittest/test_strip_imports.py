import pytest

from pr_agent.algo.pr_processing import _strip_third_party_import_lines


def test_strip_python_third_party_imports():
    patch = "\n".join([
        "@@ -1,0 +1,5 @@",
        "+import numpy as np",
        "+from pandas import DataFrame",
        "+from .local import helper",
        "+import myproj.utils",
        "+print('done')",
    ])
    local_roots = {"myproj", "local"}
    out = _strip_third_party_import_lines(patch, "module.py", local_roots)
    lines = out.splitlines()
    assert any("numpy" in l for l in lines) is False
    assert any("pandas" in l for l in lines) is False
    assert any("from .local import helper" in l for l in lines)
    assert any("import myproj.utils" in l for l in lines)


def test_strip_js_third_party_imports():
    patch = "\n".join([
        "@@ -1,0 +1,6 @@",
        "+import _ from 'lodash'",
        "+const express = require(\"express\")",
        "+import localMod from './local/mod'",
        "+import internal from 'src/utils'",
        "+console.log('x')",
    ])
    local_roots = {"src", "local"}
    out = _strip_third_party_import_lines(patch, "file.ts", local_roots)
    lines = out.splitlines()
    assert any("lodash" in l for l in lines) is False
    assert any("express" in l for l in lines) is False
    assert any("import localMod from './local/mod'" in l for l in lines)
    assert any("import internal from 'src/utils'" in l for l in lines)
