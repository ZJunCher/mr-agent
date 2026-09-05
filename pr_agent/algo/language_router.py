"""
Language router: detect primary language from diff file extensions and select
language-specific prompt/context strategies.

Routing rules:
- pure python  → Python-specific prompt only
- pure cpp     → C++ (default) prompt only
- mixed        → TWO calls (C++ prompt + Python prompt), results merged before output
- other        → C++ (default) prompt only

Each tool always produces ONE unified comment output.
"""
import os
from typing import List, Optional, Tuple

# Extension → language mapping (lowercase)
_PYTHON_EXTENSIONS = {".py", ".pyw", ".pyi"}
_CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx", ".hh"}

# ---------------------------------------------------------------------------
# Python description supplement — appended to C++ base prompt for mixed PRs.
# Description uses a single LLM call (format overlaps, no need for two).
# ---------------------------------------------------------------------------
_PYTHON_DESCRIPTION_SUPPLEMENT = """

【Python 文件描述要点（本 PR 包含 Python 文件，描述中应覆盖）】
- 明确指出影响的 Python 模块/包/入口点
- 对于新增类/函数，简要说明其职责
- 对于依赖变更（requirements/pyproject），列出新增/移除的包
- 对于配置变更，说明新增/修改的配置项
- 对于测试文件，说明覆盖的场景
"""


def language_scope_for_file(filename: str) -> str | None:
    """Return the supported project-rule language for one file, if any."""
    ext = os.path.splitext(str(filename or ""))[1].lower()
    if ext in _PYTHON_EXTENSIONS:
        return "python"
    if ext in _CPP_EXTENSIONS:
        return "cpp"
    return None


def detect_language_from_files(filenames: List[str]) -> str:
    """
    Determine the primary language of a PR based on diff file extensions.

    Returns:
        "python" | "cpp" | "mixed" | "other"
    """
    py_count = 0
    cpp_count = 0

    for f in filenames:
        language = language_scope_for_file(f)
        if language == "python":
            py_count += 1
        elif language == "cpp":
            cpp_count += 1

    if py_count == 0 and cpp_count == 0:
        return "other"
    if py_count > 0 and cpp_count == 0:
        return "python"
    if cpp_count > 0 and py_count == 0:
        return "cpp"
    return "mixed"


def language_scopes_for_mode(detected_lang: str) -> frozenset[str]:
    """Map a routed Prompt mode to the project-rule languages it may consume."""
    if detected_lang == "python":
        return frozenset({"python"})
    if detected_lang == "cpp":
        return frozenset({"cpp"})
    if detected_lang == "mixed":
        return frozenset({"python", "cpp"})
    return frozenset()


def improve_prompt_pair_languages(detected_lang: str) -> tuple[frozenset[str], ...]:
    """Return language scopes aligned one-to-one with improve Prompt pairs."""
    if detected_lang == "mixed":
        return (frozenset({"cpp"}), frozenset({"python"}))
    return (language_scopes_for_mode(detected_lang),)


def get_review_prompt_pairs(detected_lang: str, use_v2: bool = False) -> List[Tuple[str, str]]:
    """
    Return a list of (system_template, user_template) pairs for review.
    - python → [Python prompt]
    - mixed  → [C++ prompt, Python prompt]  (two clean calls, merged later)
    - cpp/other → [C++ prompt]

    `use_v2` selects the Related-Files-aware prompt variants (the
    `_v2`-suffixed Dynaconf sections) instead of the original diff-only
    prompts. Callers pass `use_v2=True` only when
    `pr_reviewer.code_graph.enabled` is true.
    """
    from pr_agent.config_loader import get_settings
    default_key = "pr_review_prompt_v3" if use_v2 else "pr_review_prompt"
    python_key = "pr_review_prompt_python_v3" if use_v2 else "pr_review_prompt_python"

    default_settings = get_settings().get(default_key)
    default_pair = (default_settings.system, default_settings.user)

    if detected_lang == "python" and hasattr(get_settings(), python_key):
        python_settings = get_settings().get(python_key)
        return [(python_settings.system, python_settings.user)]
    if detected_lang == "mixed" and hasattr(get_settings(), python_key):
        python_settings = get_settings().get(python_key)
        python_pair = (python_settings.system, python_settings.user)
        return [default_pair, python_pair]
    return [default_pair]


def get_improve_prompt_pairs(detected_lang: str, base_sys: str, base_usr: str, use_v2: bool = False) -> List[Tuple[str, str]]:
    """
    Return a list of (system_template, user_template) pairs for code suggestions (improve).
    - python → [Python prompt]
    - mixed  → [C++ prompt, Python prompt]  (two clean calls, merged later)
    - cpp/other → [C++ prompt]

    `base_sys`/`base_usr` are already resolved by the caller (which also
    picks the decoupled/not-decoupled and v1/v2 base pair). `use_v2` here
    only controls which Python-specific section (`pr_code_suggestions_prompt_python`
    vs. `_v2`) is used for the Python/mixed-language leg.
    """
    from pr_agent.config_loader import get_settings
    base_pair = (base_sys, base_usr)
    python_key = "pr_code_suggestions_prompt_python_v3" if use_v2 else "pr_code_suggestions_prompt_python"

    if detected_lang == "python" and hasattr(get_settings(), python_key):
        python_settings = get_settings().get(python_key)
        return [(python_settings.system, python_settings.user)]
    if detected_lang == "mixed" and hasattr(get_settings(), python_key):
        python_settings = get_settings().get(python_key)
        python_pair = (python_settings.system, python_settings.user)
        return [base_pair, python_pair]
    return [base_pair]


def resolve_description_prompt_key(detected_lang: str) -> Tuple[str, Optional[str]]:
    """
    Return (prompt_settings_key, supplement_text) for description.
    Description always does ONE call (output format overlaps across languages).
    - python → ("pr_description_prompt_python", None)
    - mixed  → ("pr_description_prompt", supplement)
    - cpp/other → ("pr_description_prompt", None)
    """
    from pr_agent.config_loader import get_settings
    if detected_lang == "python":
        py_settings = get_settings().get("pr_description_prompt_python", {})
        if py_settings and py_settings.get("system"):
            return ("pr_description_prompt_python", None)
    if detected_lang == "mixed":
        return ("pr_description_prompt", _PYTHON_DESCRIPTION_SUPPLEMENT)
    return ("pr_description_prompt", None)


def merge_review_predictions(predictions: List[str]) -> str:
    """
    Merge multiple review YAML prediction strings into one.
    Strategy:
    - key_issues_to_review: concatenate all
    - security_concerns: combine non-'No' entries
    - score: take the minimum (most conservative)
    - estimated_effort_to_review_[1-5]: take the max
    - relevant_tests: 'Yes' if any is 'Yes'
    - other fields: take from first prediction
    """
    from pr_agent.algo.utils import load_yaml

    if len(predictions) == 1:
        return predictions[0]

    first_key = 'review'
    last_key = 'security_concerns'
    keys_fix = ["ticket_compliance_check", "estimated_effort_to_review_[1-5]:",
                "security_concerns:", "key_issues_to_review:",
                "relevant_file:", "relevant_line:", "suggestion:"]

    parsed_list = []
    for pred in predictions:
        data = load_yaml(pred.strip(), keys_fix_yaml=keys_fix,
                         first_key=first_key, last_key=last_key)
        if data and 'review' in data:
            parsed_list.append(data)

    if not parsed_list:
        return predictions[0]
    if len(parsed_list) == 1:
        return predictions[0] if parsed_list[0] == load_yaml(predictions[0].strip(), keys_fix_yaml=keys_fix, first_key=first_key, last_key=last_key) else predictions[-1]

    merged = parsed_list[0]
    for extra in parsed_list[1:]:
        extra_review = extra.get('review', {})

        # Merge key_issues_to_review
        if 'key_issues_to_review' in extra_review:
            merged_issues = merged.get('review', {}).get('key_issues_to_review', [])
            if not isinstance(merged_issues, list):
                merged_issues = []
            extra_issues = extra_review.get('key_issues_to_review', [])
            if isinstance(extra_issues, list):
                merged_issues.extend(extra_issues)
            merged['review']['key_issues_to_review'] = merged_issues

        # Merge security_concerns
        if 'security_concerns' in extra_review:
            base_sec = str(merged['review'].get('security_concerns', 'No')).strip()
            extra_sec = str(extra_review.get('security_concerns', 'No')).strip()
            if extra_sec.lower() != 'no':
                if base_sec.lower() == 'no':
                    merged['review']['security_concerns'] = extra_sec
                else:
                    merged['review']['security_concerns'] = base_sec + "\n\n" + extra_sec

        # Merge score (take minimum = most conservative)
        if 'score' in extra_review:
            try:
                base_score = int(str(merged['review'].get('score', '100')).strip())
                extra_score = int(str(extra_review['score']).strip())
                merged['review']['score'] = str(min(base_score, extra_score))
            except (ValueError, TypeError):
                pass

        # Merge effort (take max = hardest)
        for key in list(extra_review.keys()):
            if 'estimated_effort' in key:
                try:
                    base_val = int(str(merged['review'].get(key, '1')).strip())
                    extra_val = int(str(extra_review[key]).strip())
                    merged['review'][key] = str(max(base_val, extra_val))
                except (ValueError, TypeError):
                    pass

        # Merge relevant_tests (Yes if any says Yes)
        if 'relevant_tests' in extra_review:
            if str(extra_review['relevant_tests']).strip().lower() == 'yes':
                merged['review']['relevant_tests'] = 'Yes'

        # Merge todo_sections
        if 'todo_sections' in extra_review:
            base_todo = merged['review'].get('todo_sections', 'No')
            extra_todo = extra_review.get('todo_sections', 'No')
            if isinstance(extra_todo, list) and extra_todo:
                if isinstance(base_todo, list):
                    base_todo.extend(extra_todo)
                elif str(base_todo).strip().lower() == 'no':
                    base_todo = extra_todo
                merged['review']['todo_sections'] = base_todo

    # Serialize back to YAML string
    import yaml
    issues = merged.get('review', {}).get('key_issues_to_review', [])
    if isinstance(issues, list):
        unique_issues = []
        seen = set()
        for issue in issues:
            if not isinstance(issue, dict):
                unique_issues.append(issue)
                continue
            identity = (
                str(issue.get('relevant_file', '')).strip().casefold(),
                str(issue.get('relevant_line', '')).strip(),
                ' '.join(str(issue.get('suggestion', '')).casefold().split()),
            )
            if identity in seen:
                continue
            seen.add(identity)
            unique_issues.append(issue)
        merged['review']['key_issues_to_review'] = unique_issues

    return yaml.dump(merged, default_flow_style=False, allow_unicode=True, sort_keys=False)
