from unittest.mock import MagicMock

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_feedback import PRFeedback


def _fb(args):
    fb = PRFeedback.__new__(PRFeedback)  # bypass __init__/network
    fb.args = args
    return fb


def setup_function(_):
    get_settings().set("pr_feedback.min_score", 1)
    get_settings().set("pr_feedback.max_score", 5)
    get_settings().set("pr_feedback.comment_required_below", 5)


def test_score_five_without_comment_is_valid():
    score, comment, err = _fb(["5"])._parse_args()
    assert err is None and score == 5 and comment is None


def test_score_four_without_comment_requires_comment():
    score, comment, err = _fb(["4"])._parse_args()
    assert err == "comment_required"


def test_score_four_with_comment_is_valid():
    score, comment, err = _fb(["4", "needs", "work"])._parse_args()
    assert err is None and score == 4 and comment == "needs work"


def test_missing_score():
    _, _, err = _fb([])._parse_args()
    assert err == "missing_score"


def test_invalid_score():
    _, _, err = _fb(["abc"])._parse_args()
    assert err == "invalid_score"


def test_out_of_range():
    _, _, err = _fb(["9"])._parse_args()
    assert err == "out_of_range"
