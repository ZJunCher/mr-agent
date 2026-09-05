"""Vendored pure-stdlib algorithm modules from Hermes Agent (MIT).

Source: https://github.com/NousResearch/hermes-agent
License: MIT — Copyright (c) 2025 Nous Research

These modules are vendored (not installed as a dependency) because:
1. They are pure standard library (re, difflib, dataclasses, pathlib) — no
   transitive dependencies pulled into pr-agent.
2. They encode production-grade edge-case handling (Unicode NFC/NFD, BOM,
   escape drift, already-applied detection) that would be risky to rewrite
   from scratch.
3. Vendoring isolates the native repair backend from Hermes' release cadence;
   we sync deliberately, not automatically.

Sync policy:
- Update only when a Hermes release fixes a bug affecting our use cases.
- Record the upstream commit SHA in the module header when syncing.
- Run tests/unittest/test_vendored_fuzzy_match.py after every sync.

Current vendored modules:
- fuzzy_match: 9-strategy fuzzy find-and-replace for patch application.
- path_security: path traversal validation for tool inputs.
- patch_parser: V4A multi-file patch parsing (parse_v4a_patch + data classes).
  The apply_v4a_operations() function is NOT vendored — it depends on
  Hermes' ShellFileOperations backend. We reimplement a thin adapter in
  ut_agent/tools/apply_repo_patch.py that uses git apply for the actual
  filesystem writes.
"""
