"""generate_summarized_suggestions() renders the /improve table with an
opening tag of `<table width="100%">` (see PRCodeSuggestions.run(),
which forces full comment width so the table doesn't shrink-wrap to its
content and mismatch the review guide table's width). The history-folding
logic in publish_persistent_comment_with_history locates that table by
searching for the tag textually, so it must match "<table" as a prefix, not
require the exact literal "<table>" -- otherwise every second+ /improve run
would silently fail to find/fold the previous table and instead blow away
history (see the `if table_index == -1` fallback)."""
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

INITIAL_HEADER = "## PR Code Suggestions ✨"


class _FakeComment:
    def __init__(self, body, id=1):
        self.body = body
        self.id = id


class _FakeGitProvider:
    def __init__(self, prev_comments):
        self._prev_comments = prev_comments
        self.edited = []
        self.removed = []
        self._next_id = 1000

    def get_latest_commit_url(self):
        return "https://gl/commit/abcdef1234567"

    def get_comment_url(self, comment):
        return f"https://gl/mr/1#note_{comment.id}"

    def get_issue_comments(self):
        return list(self._prev_comments)

    def edit_comment(self, comment, body):
        self.edited.append((comment, body))

    def remove_comment(self, comment):
        self.removed.append(comment)

    def publish_comment(self, body):
        self._next_id += 1
        return _FakeComment(body, id=self._next_id)


def test_finds_table_with_width_style_attribute_no_history_section_yet():
    """First fold: previous comment's table tag carries the width style
    attribute produced by the real renderer -- must still be located and
    folded into a 'previous suggestions' history section, not treated as
    'table not found' (which would silently overwrite instead of archiving)."""
    old_body = (
        f'{INITIAL_HEADER}\n<!-- aaaaaaa -->\n\n'
        '<table width="100%"><tbody><tr><td>old row</td></tr></tbody></table>\n'
    )
    old_comment = _FakeComment(old_body)
    gp = _FakeGitProvider([old_comment])

    new_pr_comment = f'{INITIAL_HEADER}\n\n<table width="100%"><tbody><tr><td>new row</td></tr></tbody></table>'

    comment, body = PRCodeSuggestions.publish_persistent_comment_with_history(
        gp, new_pr_comment, INITIAL_HEADER, name="suggestions", publish_as_new_comment=True)

    # the old table content was captured and archived under history, not lost
    assert "old row" in body
    assert "new row" in body


def test_finds_table_with_width_style_attribute_when_history_section_exists():
    """Second+ fold: the CURRENT ("latest") table inside a comment that
    already has a history section also carries the width-style tag and must
    still be located correctly to extract its content and commit link."""
    history_header = "#### Previous suggestions\n"
    old_body = (
        f'{INITIAL_HEADER}\n<!-- bbbbbbb -->\n\n'
        'Latest suggestions up to bbbbbbb\n'
        '<table width="100%"><tbody><tr><td>latest row</td></tr></tbody></table>\n\n'
        '___\n\n'
        f'{history_header}\n'
        '<details><summary>Suggestions up to commit aaaaaaa</summary>\n<br>'
        '<table width="100%"><tbody><tr><td>archived row</td></tr></tbody></table>\n\n</details>\n'
    )
    old_comment = _FakeComment(old_body)
    gp = _FakeGitProvider([old_comment])

    new_pr_comment = f'{INITIAL_HEADER}\n\n<table width="100%"><tbody><tr><td>newest row</td></tr></tbody></table>'

    comment, body = PRCodeSuggestions.publish_persistent_comment_with_history(
        gp, new_pr_comment, INITIAL_HEADER, name="suggestions", publish_as_new_comment=True)

    # all three generations of table content survive: newest, previous-latest, and the older archived one
    assert "newest row" in body
    assert "latest row" in body
    assert "archived row" in body
