"""Regression test for a header-corruption bug in
convert_to_decoupled_with_line_numbers: when a multi-file diff is split back
apart on the "## File: " delimiter and reassembled, every file EXCEPT the
first lost the leading single-quote of its filename (producing e.g.
"## File: b.py'" instead of "## File: 'b.py'").

This was silent/harmless before because nothing strictly parsed that header
format -- but it corrupts the header text sent to the self-reflect prompt for
every non-first file, and (as of the deterministic hunk-line-matcher work)
makes pr_agent.algo.hunk_line_matcher.find_lines_in_new_hunk unable to locate
ANY suggestion whose file isn't first in the diff, silently zeroing their
score and dropping them entirely from /improve's output -- exactly the "no
suggestions at all" regression reported against real multi-file PRs.
"""
import asyncio

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


class _FakeTokenHandler:
    prompt_tokens = 0

    def count_tokens(self, text):
        return len(text) // 4


def _make_instance():
    instance = object.__new__(PRCodeSuggestions)
    instance.token_handler = _FakeTokenHandler()
    return instance


def test_non_first_file_headers_keep_both_quotes():
    instance = _make_instance()
    patch_prompt = (
        "\n\n## File: 'a.py'\n\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+line2_new\n"
        " line3\n"
        "\n\n## File: 'b.py'\n\n"
        "@@ -10,3 +10,3 @@\n"
        " line10\n"
        "-line11\n"
        "+line11_new\n"
        " line12\n"
    )
    result = asyncio.run(instance.convert_to_decoupled_with_line_numbers([patch_prompt], "gpt-3.5-turbo"))
    assert len(result) == 1
    assert "## File: 'a.py'" in result[0]
    assert "## File: 'b.py'" in result[0]
    # The exact corruption this regresses against: a trailing-quote-only header.
    assert "## File: b.py'" not in result[0]
