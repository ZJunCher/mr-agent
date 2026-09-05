import os
import sqlite3
import tempfile

import pytest

from pr_agent.feedback import store
from pr_agent.tools.pr_feedback import PRFeedback, REVIEW_ID_MARKER_RE
from pr_agent.tools.pr_reviewer import PRReviewer


class _FakeNote:
    def __init__(self, note_id, body, updated_at=None, created_at=None):
        self.id = note_id
        self.body = body
        self.updated_at = updated_at
        self.created_at = created_at


class _FakeNotes:
    def __init__(self, notes):
        self._notes = notes

    def list(self, get_all=True):
        return self._notes


class _FakeDiscussion:
    def __init__(self, discussion_id, notes):
        self.id = discussion_id
        self.attributes = {"notes": notes}


class _FakeDiscussions:
    def __init__(self, discussions):
        self._discussions = discussions

    def list(self, get_all=True):
        return self._discussions


class _FakeMR:
    def __init__(self, notes=None, discussions=None, author=None, sha=None, web_url=None):
        self.notes = _FakeNotes(notes or [])
        self.discussions = _FakeDiscussions(discussions or [])
        self.author = author
        self.sha = sha
        self.web_url = web_url


class _FakeProvider:
    def __init__(self, mr=None, id_project=None, id_mr=None):
        self.mr = mr
        self.id_project = id_project
        self.id_mr = id_mr
        self.published = []
        self.replied_to = []
        self.reactions = []

    def publish_comment(self, body, is_temporary=False):
        self.published.append(body)

    def reply_to_comment_from_comment_id(self, comment_id: int, body: str):
        """No-op in tests — simulate replying to a discussion thread."""
        self.published.append(body)
        self.replied_to.append(comment_id)

    def add_reaction(self, issue_comment_id: int, emoji_name: str):
        """No-op in tests."""
        self.reactions.append((issue_comment_id, emoji_name))


def _make_feedback(monkeypatch, args, provider, reviewer_user=None):
    monkeypatch.setattr(
        "pr_agent.tools.pr_feedback.get_git_provider_with_context",
        lambda pr_url: provider,
    )
    return PRFeedback("http://gitlab/x/-/merge_requests/1", args=args,
                      reviewer_user=reviewer_user)


# --- store ---------------------------------------------------------------

def test_store_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "feedback.db")  # parent dir auto-created
        ok = store.save_feedback({"score": 4, "comment": "good", "pr_url": "u"}, path=path)
        assert ok is True
        assert os.path.exists(path)
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT score, comment, pr_url FROM review_feedback"
            ).fetchone()
        finally:
            conn.close()
        assert row == (4, "good", "u")


def test_store_serializes_extra_dict():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "feedback.db")
        ok = store.save_feedback({"score": 3, "extra": {"k": "v"}}, path=path)
        assert ok is True
        conn = sqlite3.connect(path)
        try:
            extra = conn.execute("SELECT extra_json FROM review_feedback").fetchone()[0]
        finally:
            conn.close()
        assert "k" in extra and "v" in extra


# --- parse args ----------------------------------------------------------

@pytest.mark.parametrize(
    "args,score,comment,error",
    [
        (["5", "very", "helpful"], 5, "very helpful", None),
        (["1"], None, None, "comment_required"),
        ([], None, None, "missing_score"),
        (["abc"], None, None, "invalid_score"),
        (["0"], None, None, "out_of_range"),
        (["6"], None, None, "out_of_range"),
    ],
)
def test_parse_args(monkeypatch, args, score, comment, error):
    fb = _make_feedback(monkeypatch, args, _FakeProvider())
    assert fb._parse_args() == (score, comment, error)


def test_parse_structured_false_negative_case():
    comment, case, error = PRFeedback._parse_evolution_case_comment(
        "case=false_negative file=src/parser.py line=10-12 漏掉空指针检查"
    )

    assert error is None
    assert comment == "漏掉空指针检查"
    assert case == {
        "kind": "false_negative",
        "description": "漏掉空指针检查",
        "file_path": "src/parser.py",
        "line_start": 10,
        "line_end": 12,
        "suggestion_id": "",
    }


def test_parse_structured_bad_fix_case_requires_explicit_metadata():
    comment, case, error = PRFeedback._parse_evolution_case_comment(
        "case=bad_fix suggestion=s-1 修复代码会破坏空输入"
    )

    assert (comment, error) == ("修复代码会破坏空输入", None)
    assert case["suggestion_id"] == "s-1"


def test_unknown_case_kind_is_rejected():
    _, case, error = PRFeedback._parse_evolution_case_comment("case=timeout 网络超时")

    assert case is None
    assert error == "invalid_case_kind"


# --- review id linkage ---------------------------------------------------

def test_find_latest_review_id_picks_most_recent(monkeypatch):
    notes = [
        _FakeNote(10, "old review <!-- pr_agent_review_id: aaaaaaaaaaaa -->"),
        _FakeNote(25, "new review <!-- pr_agent_review_id: bbbbbbbbbbbb -->"),
        _FakeNote(30, "a normal comment without marker"),
    ]
    provider = _FakeProvider(mr=_FakeMR(notes=notes))
    fb = _make_feedback(monkeypatch, ["5"], provider)
    assert fb._find_latest_review_id() == ("bbbbbbbbbbbb", 25, None)


def test_find_latest_review_id_prefers_updated_at_over_note_id(monkeypatch):
    notes = [
        _FakeNote(
            10,
            "persistent review <!-- pr_agent_review_id: aaaaaaaaaaaa -->",
            updated_at="2026-06-23T06:00:00.000Z",
        ),
        _FakeNote(
            25,
            "older non-persistent review <!-- pr_agent_review_id: bbbbbbbbbbbb -->",
            updated_at="2026-06-23T05:00:00.000Z",
        ),
    ]
    provider = _FakeProvider(mr=_FakeMR(notes=notes))
    fb = _make_feedback(monkeypatch, ["5"], provider)
    assert fb._find_latest_review_id() == ("aaaaaaaaaaaa", 10, None)


def test_find_latest_review_id_returns_discussion_id(monkeypatch):
    discussions = [
        _FakeDiscussion(
            "abc123discussion",
            [
                {
                    "id": 40,
                    "body": "review <!-- pr_agent_review_id: cccccccccccc -->",
                    "updated_at": "2026-06-23T06:00:00.000Z",
                },
            ],
        ),
    ]
    provider = _FakeProvider(mr=_FakeMR(discussions=discussions))
    fb = _make_feedback(monkeypatch, ["5"], provider)
    assert fb._find_latest_review_id() == ("cccccccccccc", 40, "abc123discussion")


def test_find_latest_review_id_none_when_absent(monkeypatch):
    provider = _FakeProvider(mr=_FakeMR(notes=[_FakeNote(1, "no marker here")]))
    fb = _make_feedback(monkeypatch, ["5"], provider)
    assert fb._find_latest_review_id() == (None, None, None)


# --- full run persists + confirms ---------------------------------------

@pytest.mark.asyncio
async def test_run_persists_and_confirms(monkeypatch):
    provider = _FakeProvider(
        mr=_FakeMR(notes=[_FakeNote(1, "review <!-- pr_agent_review_id: abc123abc123 -->")],
                   author={"username": "alice"}, sha="deadbeef"),
        id_project=42, id_mr=7,
    )
    fb = _make_feedback(monkeypatch, ["2", "too", "noisy"], provider, reviewer_user="bob")

    captured = {}

    def fake_save(record, path=None):
        captured.update(record)
        return True

    monkeypatch.setattr("pr_agent.tools.pr_feedback.save_feedback", fake_save)
    await fb.run()

    assert captured["score"] == 2
    assert captured["comment"] == "too noisy"
    assert captured["reviewer_user"] == "bob"
    assert captured["review_id"] == "abc123abc123"
    assert captured["mr_author"] == "alice"
    assert captured["commit_sha"] == "deadbeef"
    assert captured["project"] == 42
    assert captured["mr_iid"] == 7
    assert provider.published  # a confirmation comment was published


@pytest.mark.asyncio
async def test_run_persists_structured_evolution_case(monkeypatch):
    provider = _FakeProvider(
        mr=_FakeMR(
            notes=[_FakeNote(1, "review <!-- pr_agent_review_id: abc123abc123 -->")],
            sha="deadbeef",
        ),
        id_project="group/repo",
        id_mr=7,
    )
    fb = _make_feedback(
        monkeypatch,
        ["2", "case=false_negative", "file=src/parser.py", "line=10", "漏掉空指针检查"],
        provider,
    )
    captured = {}
    monkeypatch.setattr("pr_agent.tools.pr_feedback.save_feedback", lambda _record: True)
    monkeypatch.setattr(
        "pr_agent.tools.pr_feedback.save_evolution_case",
        lambda record: captured.update(record) is None,
    )

    await fb.run()

    assert captured["kind"] == "false_negative"
    assert captured["review_id"] == "abc123abc123"
    assert captured["head_sha"] == "deadbeef"
    assert captured["file_path"] == "src/parser.py"


@pytest.mark.asyncio
async def test_run_invalid_score_shows_help_and_skips_save(monkeypatch):
    provider = _FakeProvider(mr=_FakeMR())
    fb = _make_feedback(monkeypatch, ["nope"], provider)

    called = {"saved": False}

    def fake_save(record, path=None):
        called["saved"] = True
        return True

    monkeypatch.setattr("pr_agent.tools.pr_feedback.save_feedback", fake_save)
    await fb.run()

    assert called["saved"] is False
    assert provider.published  # help message published


# --- reviewer appends marker + hint -------------------------------------

def test_reviewer_appends_marker_and_hint():
    reviewer = object.__new__(PRReviewer)  # bypass heavy __init__
    out = PRReviewer._append_feedback_section(reviewer, "BODY")
    assert "BODY" in out
    match = REVIEW_ID_MARKER_RE.search(out)
    assert match is not None
    assert len(match.group(1)) == 12
    assert "/feedback" in out
