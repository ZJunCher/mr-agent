"""convert_to_markdown_v2's PR Reviewer Guide table is a narrow, single-column
table (unlike the /improve suggestions table, which is padded wide via
literal &nbsp; characters stuffed into its header cell). Left unpadded, the
review guide table renders visibly narrower than the /improve table in the
same MR (both tables shrink-wrap to their own content -- GitLab's own
stylesheet forces `width: auto` on every markdown table, which overrides any
HTML/CSS width attribute, verified via GitLab's own POST /api/v4/markdown
render endpoint). The only technique that survives GitLab's sanitizer AND
actually widens the rendered table is padding real (visible-position, even if
visually blank) content -- literal &nbsp; characters appended to the last
cell -- mirroring the exact technique pr_code_suggestions.py already uses for
its own table header.
"""
from pr_agent.algo.utils import convert_to_markdown_v2


def test_last_table_cell_is_padded_with_nbsp():
    data = {"review": {
        "estimated_effort_to_review_[1-5]": "3",
        "security_concerns": "No",
    }}
    out = convert_to_markdown_v2(data)

    assert "<table>" in out
    # padding lands inside the LAST row's <td>, right before its closing tag
    last_row_start = out.rfind("<tr><td>")
    last_td_close = out.rfind("</td>")
    last_row = out[last_row_start:last_td_close]
    assert last_row.count("&nbsp;") >= 60


def test_padding_does_not_leak_into_a_new_row():
    """The padding must be appended INSIDE the existing last cell, not as a
    separate trailing row (a separate row would render as a visible near-
    empty gap at the bottom of the table)."""
    data = {"review": {
        "estimated_effort_to_review_[1-5]": "3",
        "security_concerns": "No",
    }}
    out = convert_to_markdown_v2(data)

    # exactly 2 rows -- effort + security -- not 3 (no extra padding-only row)
    assert out.count("<tr><td>") == 2
    assert out.count("</td></tr>") == 2


def test_no_padding_when_table_has_no_rows():
    """publish_no_suggestions-equivalent edge case: an empty review dict
    still returns without a table (existing early-return), so the padding
    logic (which searches for a "</td>" that doesn't exist) must not raise."""
    out = convert_to_markdown_v2({"review": {}})
    assert isinstance(out, str)
