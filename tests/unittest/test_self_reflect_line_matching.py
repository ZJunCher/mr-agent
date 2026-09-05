"""analyze_self_reflection_response must override relevant_lines_start/end
with a deterministic, diff-scoped match when `patches_diff` is provided,
instead of trusting the self-reflect LLM's own line-number guess -- and must
drop (score=0) any suggestion whose existing_code can't be found in the
diff's own __new hunk__ text. Without `patches_diff`, behavior is unchanged
(back-compat with existing callers/tests)."""
import asyncio

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

_DIFF = """## File: 'src/a.py'

@@ ... @@ def f():
__new hunk__
20  def f():
21 +    return None
"""


def _make_instance():
    instance = object.__new__(PRCodeSuggestions)
    return instance


def _suggestion(**kw):
    base = {
        "one_sentence_summary": "fix bug",
        "relevant_file": "src/a.py",
        "existing_code": "    return None",
        "improved_code": "    return 0",
        "label": "bug",
    }
    base.update(kw)
    return base


def test_overrides_wrong_llm_line_numbers_with_deterministic_match():
    instance = _make_instance()
    data = {"code_suggestions": [_suggestion()]}
    # Self-reflect claims lines 999-999 (wrong / outside the diff); the real
    # location per the diff text is line 21.
    response_reflect = (
        "code_suggestions:\n"
        "  - suggestion_summary: fix bug\n"
        "    relevant_file: src/a.py\n"
        "    relevant_lines_start: 999\n"
        "    relevant_lines_end: 999\n"
        "    suggestion_score: 8\n"
        "    why: because\n"
    )
    asyncio.run(instance.analyze_self_reflection_response(data, response_reflect, patches_diff=_DIFF))
    sugg = data["code_suggestions"][0]
    assert (sugg["relevant_lines_start"], sugg["relevant_lines_end"]) == (21, 21)


def test_drops_suggestion_whose_existing_code_is_not_in_the_diff():
    instance = _make_instance()
    data = {"code_suggestions": [_suggestion(existing_code="this code does not exist in the diff at all")]}
    response_reflect = (
        "code_suggestions:\n"
        "  - suggestion_summary: fix bug\n"
        "    relevant_file: src/a.py\n"
        "    relevant_lines_start: 21\n"
        "    relevant_lines_end: 21\n"
        "    suggestion_score: 8\n"
        "    why: because\n"
    )
    asyncio.run(instance.analyze_self_reflection_response(data, response_reflect, patches_diff=_DIFF))
    sugg = data["code_suggestions"][0]
    assert sugg["score"] == 0
    assert (sugg["relevant_lines_start"], sugg["relevant_lines_end"]) == (-1, -1)


def test_without_patches_diff_behavior_is_unchanged():
    instance = _make_instance()
    data = {"code_suggestions": [_suggestion()]}
    response_reflect = (
        "code_suggestions:\n"
        "  - suggestion_summary: fix bug\n"
        "    relevant_file: src/a.py\n"
        "    relevant_lines_start: 999\n"
        "    relevant_lines_end: 999\n"
        "    suggestion_score: 8\n"
        "    why: because\n"
    )
    asyncio.run(instance.analyze_self_reflection_response(data, response_reflect))
    sugg = data["code_suggestions"][0]
    # No patches_diff passed -> no deterministic override -> keeps the (wrong) LLM line numbers.
    assert (sugg["relevant_lines_start"], sugg["relevant_lines_end"]) == (999, 999)
