"""Phase 1 heuristic gate for inline suggestions (发布前门禁).

Blocks inline suggestions that are likely incomplete or unsafe to one-click
apply. All checks are local heuristics (no LLM). Each check returns a
skip_reason string when the suggestion should be blocked, or None when it
passes. The orchestrator gate_suggestions() never raises: any internal error
means the individual check is skipped (fail-open) so the publish chain is
never broken by the gate itself.

Checks:
- G1 new_symbol:      improved_code uses member/function symbols absent from the file
- G2 new_dependency:  improved_code adds #include / import not present in the file
- G3 cross_file:      suggestion text asks to modify another file
- G4 incomplete_patch/existing_mismatch: ellipsis markers, brace imbalance,
                      or existing_code not found in the file (LLM hallucination)
- G5 speculative:     label belongs to speculative categories (e.g. performance)
"""
from __future__ import annotations

import re

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

# Common language keywords / builtins that look like function calls but are not.
_CALL_KEYWORDS = {
    "if", "for", "while", "switch", "return", "sizeof", "catch", "throw",
    "new", "delete", "defined", "assert", "decltype", "noexcept", "alignof",
    "typeid", "static_assert", "co_await", "co_return", "co_yield",
    "def", "print", "len", "range", "isinstance", "super", "type", "str",
    "int", "float", "bool", "list", "dict", "set", "tuple", "except", "with",
}

# Markers that indicate the snippet is an abbreviated / incomplete patch.
_ELLIPSIS_PATTERNS = [
    r"^\s*(//|#|/\*)?\s*\.\.\.",  # a line starting with ... (optionally commented)
    r"\.\.\.\s*(其余|剩余|不变|省略)",
    r"(其余|剩余)代码(保持)?不变",
    r"(此处|以下|以上)省略",
    r"rest of (the )?(code|function|file)",
    r"remains? unchanged",
    r"unchanged code",
]

# Verbs that indicate the suggestion requires modifying something.
_MODIFY_VERBS = [
    "修改", "更新", "调整", "同时改", "也要改", "添加到", "新增到", "声明到",
    "update", "modify", "change", "edit", "add to", "declare in", "also add",
]

# label synonyms for speculative categories (config value -> match keywords)
_SPECULATIVE_SYNONYMS = {
    "performance": ["performance", "性能"],
}

_SRC_EXTENSIONS = (".h", ".hpp", ".hh", ".cpp", ".cc", ".cxx", ".c",
                   ".py", ".go", ".java", ".ts", ".js", ".rs", ".proto")


def _cfg(key: str, default=None):
    return get_settings().get(f"pr_code_suggestions.{key}", default)


# --------------------------------------------------------------------------- #
# G1: new symbol
# --------------------------------------------------------------------------- #
def check_new_symbol(suggestion: dict, head_file: str) -> str | None:
    if not _cfg("inline_gate_check_new_symbol", True):
        return None
    if not head_file:
        return None  # cannot judge without file content: don't block
    improved = suggestion.get("improved_code", "") or ""
    existing = suggestion.get("existing_code", "") or ""
    candidates: set[str] = set()

    # member-style variables: trailing underscore (C++), m_xxx, self.xxx
    for token in re.findall(r"\b[A-Za-z]\w*_(?=\b|[^\w])", improved):
        candidates.add(token)
    for token in re.findall(r"\bm_\w+\b", improved):
        candidates.add(token)
    for attr in re.findall(r"\bself\.(\w+)", improved):
        candidates.add(f"self.{attr}")

    # bare function calls: name( not preceded by . -> :: > or a type name
    for m in re.finditer(r"(?<![\w.>:])([A-Za-z_]\w*)\s*\(", improved):
        name = m.group(1)
        if name in _CALL_KEYWORDS or name.endswith("_cast"):
            continue
        prefix = improved[: m.start()].rstrip()
        # preceded by '>' (template type) or an identifier => variable declaration
        if prefix.endswith(">") or re.search(r"[\w>]\s*$", prefix[-3:] if prefix else ""):
            continue
        candidates.add(name)

    for sym in candidates:
        bare = sym.split(".", 1)[1] if sym.startswith("self.") else sym
        if bare in _CALL_KEYWORDS:
            continue
        if sym in existing or bare in existing:
            continue  # not newly introduced
        # declared inside the improved snippet itself (e.g. auto x = ...)
        if re.search(rf"(?:auto|[\w:<>,*&\]]+)\s+[*&]?{re.escape(bare)}\s*[=({{;]", improved):
            continue
        if sym.startswith("self."):
            if sym in head_file or re.search(rf"\bself\.{re.escape(bare)}\b", head_file):
                continue
        elif re.search(rf"\b{re.escape(bare)}\b", head_file):
            continue
        return "new_symbol"
    return None


# --------------------------------------------------------------------------- #
# G2: new dependency
# --------------------------------------------------------------------------- #
def check_new_dependency(suggestion: dict, head_file: str) -> str | None:
    if not _cfg("inline_gate_check_new_dependency", True):
        return None
    improved = suggestion.get("improved_code", "") or ""
    existing = suggestion.get("existing_code", "") or ""
    head_file = head_file or ""

    deps: list[str] = []
    deps += re.findall(r'#\s*include\s*[<"]([^>"]+)[>"]', improved)
    for m in re.finditer(r"^\s*(?:import\s+([\w.]+)|from\s+([\w.]+)\s+import)", improved, re.MULTILINE):
        deps.append(m.group(1) or m.group(2))

    for dep in deps:
        pattern = re.escape(dep)
        if re.search(pattern, existing):
            continue  # replacing an existing include/import line
        if re.search(pattern, head_file):
            continue
        return "new_dependency"
    return None


# --------------------------------------------------------------------------- #
# G3: cross file
# --------------------------------------------------------------------------- #
def check_cross_file(suggestion: dict) -> str | None:
    if not _cfg("inline_gate_check_cross_file", True):
        return None
    text = " ".join(
        str(suggestion.get(k, "") or "")
        for k in ("suggestion_content", "one_sentence_summary")
    )
    relevant_file = suggestion.get("relevant_file", "") or ""
    own_basename = relevant_file.rsplit("/", 1)[-1].lower()

    mentioned = re.findall(r"\b[\w./-]+(?:\.(?:h|hpp|hh|cpp|cc|cxx|c|py|go|java|ts|js|rs|proto))\b",
                           text, re.IGNORECASE)
    other_files = [f for f in mentioned if f.rsplit("/", 1)[-1].lower() != own_basename]
    if not other_files:
        return None
    lowered = text.lower()
    if any(verb in lowered for verb in _MODIFY_VERBS):
        return "cross_file"
    return None


# --------------------------------------------------------------------------- #
# G4: incomplete patch / existing mismatch
# --------------------------------------------------------------------------- #
def check_incomplete_patch(suggestion: dict, head_file: str) -> str | None:
    if not _cfg("inline_gate_check_incomplete", True):
        return None
    improved = suggestion.get("improved_code", "") or ""
    existing = suggestion.get("existing_code", "") or ""

    # 1) ellipsis / omission markers
    for line in improved.splitlines():
        for pat in _ELLIPSIS_PATTERNS:
            if re.search(pat, line, re.IGNORECASE):
                return "incomplete_patch"

    # 2) brace delta must match between existing and improved (structure preserved)
    def _delta(code: str) -> int:
        stripped = re.sub(r'//[^\n]*|#[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "", code)
        return stripped.count("{") - stripped.count("}")

    if _delta(improved) != _delta(existing):
        return "incomplete_patch"

    # 3) existing_code must actually appear in the file (guard LLM hallucination)
    if head_file:
        head_lines = {ln.strip() for ln in head_file.splitlines() if ln.strip()}
        existing_lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]
        if existing_lines:
            found = sum(1 for ln in existing_lines if ln in head_lines)
            if found / len(existing_lines) < 0.7:
                return "existing_mismatch"
    return None


# --------------------------------------------------------------------------- #
# G5: speculative
# --------------------------------------------------------------------------- #
def check_speculative(suggestion: dict) -> str | None:
    if not _cfg("inline_gate_check_speculative", True):
        return None
    labels = _cfg("inline_gate_speculative_labels", ["performance"]) or []
    label = str(suggestion.get("label", "") or "").lower()
    for configured in labels:
        keywords = _SPECULATIVE_SYNONYMS.get(str(configured).lower(), [str(configured).lower()])
        if any(kw in label for kw in keywords):
            return "speculative"
    return None


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def _build_head_map(git_provider) -> dict:
    files = None
    try:
        files = git_provider.get_diff_files()
    except Exception:
        files = getattr(git_provider, "diff_files", None)
    head_map = {}
    for f in files or []:
        try:
            filename = getattr(f, "filename", None)
            head = getattr(f, "head_file", None)
            if filename and head:
                head_map[filename] = head
        except Exception:
            continue
    return head_map


def _head_for(head_map: dict, relevant_file: str) -> str:
    if not relevant_file:
        return ""
    if relevant_file in head_map:
        return head_map[relevant_file]
    for filename, head in head_map.items():
        if filename.endswith(relevant_file) or relevant_file.endswith(filename):
            return head
    return ""


def check_suggestion(suggestion: dict, head_file: str) -> str | None:
    """Run G1~G5 against a single suggestion given its file content.

    Returns the first skip_reason that fires, or None when the suggestion
    passes every enabled check. Never raises: a failing individual check is
    skipped (fail-open) so it can only block, never crash the caller. This is
    the single-suggestion re-check entry reused by Phase 2 (2B rewrite
    products must re-pass the heuristic gate before publishing).
    """
    for check, args in (
        (check_new_symbol, (suggestion, head_file)),
        (check_new_dependency, (suggestion, head_file)),
        (check_cross_file, (suggestion,)),
        (check_incomplete_patch, (suggestion, head_file)),
        (check_speculative, (suggestion,)),
    ):
        try:
            reason = check(*args)
        except Exception as e:
            get_logger().warning(f"inline gate check {check.__name__} failed: {e}")
            reason = None
        if reason:
            return reason
    return None


def gate_suggestions(git_provider, suggestions: list) -> tuple[list, list]:
    """Split suggestions into (passed, blocked). blocked items are (suggestion, skip_reason).

    Never raises; an unexpected error in a single check skips that check only.
    """
    if not _cfg("inline_gate_enabled", True):
        return list(suggestions or []), []

    try:
        head_map = _build_head_map(git_provider)
    except Exception:
        head_map = {}

    passed, blocked = [], []
    for suggestion in suggestions or []:
        try:
            head_file = _head_for(head_map, suggestion.get("relevant_file", "") or "")
        except Exception:
            head_file = ""
        reason = check_suggestion(suggestion, head_file)
        if reason:
            blocked.append((suggestion, reason))
        else:
            passed.append(suggestion)
    return passed, blocked
