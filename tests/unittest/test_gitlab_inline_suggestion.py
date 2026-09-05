from unittest.mock import MagicMock

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.gitlab_provider import GitLabProvider


class _FakeDiscussion:
    id = "disc-hash"
    attributes = {"notes": [{"id": 555}]}


class _FakeNote:
    id = 999


# ---------- _extract_discussion_ids ----------

def test_extract_ids_from_discussion():
    assert GitLabProvider._extract_discussion_ids(_FakeDiscussion()) == ("disc-hash", 555)


def test_extract_ids_from_note():
    assert GitLabProvider._extract_discussion_ids(_FakeNote()) == (None, 999)


def test_extract_ids_none():
    assert GitLabProvider._extract_discussion_ids(None) == (None, None)


# ---------- publish_inline_suggestions ----------

def _provider_with_mocks(created):
    gp = GitLabProvider.__new__(GitLabProvider)
    gp.id_mr = "10"
    gp.max_comment_chars = 65000
    tf = MagicMock()
    tf.filename = "src/a.go"
    tf.old_filename = None
    tf.head_file = "\n".join(f"l{i}" for i in range(1, 13)) + "\n"
    gp.get_diff_files = lambda: [tf]
    diff = MagicMock()
    diff.base_commit_sha = "b"
    diff.start_commit_sha = "s"
    diff.head_commit_sha = "h"
    gp.get_relevant_diff = lambda f, line: diff
    gp.mr = MagicMock()
    gp.mr.discussions.create.return_value = created
    return gp


def test_refresh_merge_request_diff_clears_caches_and_selects_latest_diff():
    gp = GitLabProvider.__new__(GitLabProvider)
    gp.pr_url = "https://gitlab/group/project/-/merge_requests/10"
    gp.diff_files = ["stale"]
    gp.git_files = ["stale.py"]
    gp._submodule_cache = {("a", "b", "c"): []}
    gp._parse_merge_request_url = lambda _url: ("group/project", "10")
    fresh_mr = MagicMock()
    old_diff = MagicMock(id=2)
    fresh_diff = MagicMock(id=9)
    fresh_mr.diffs.list.return_value = [fresh_diff, old_diff]
    gp._get_merge_request = lambda: fresh_mr

    gp.refresh_merge_request_diff()

    assert gp.diff_files is None
    assert gp.git_files is None
    assert gp._submodule_cache == {}
    assert gp.mr is fresh_mr
    assert gp.last_diff is fresh_diff


def test_create_native_inline_returns_position_and_sanitized_error():
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.create.side_effect = Exception(
        "position not part of the diff Authorization: Bearer secret-token"
    )
    target_file = gp.get_diff_files()[0]

    created, position, error = gp._create_native_inline(
        "body", "addition", "src/a.go", "l10", -1, target_file, 11,
    )

    assert created is None
    assert position == {
        "position_type": "text",
        "new_path": "src/a.go",
        "old_path": "src/a.go",
        "base_sha": "b",
        "start_sha": "s",
        "head_sha": "h",
        "new_line": 10,
    }
    assert error == "position not part of the diff Authorization: Bearer [REDACTED]"


def _payload():
    return {
        "suggestion_id": "SUG-001",
        "body": "text\n```suggestion\nnew\n```",
        "relevant_file": "src/a.go",
        "relevant_lines_start": 10,
        "relevant_lines_end": 12,
        "original_suggestion": {},
        "idempotency_marker": "pr-agent-suggestion:group/project:10:abc:SUG-001",
    }


def _fallback_payload():
    return {
        **_payload(),
        "fallback_body": (
            "### 代码建议（已降级为普通评论）\n\n"
            "<!-- pr-agent-suggestion:group/project:10:abc:SUG-001 -->"
        ),
    }


def test_publish_inline_suggestions_returns_ids():
    gp = _provider_with_mocks(_FakeDiscussion())
    results = gp.publish_inline_suggestions([_payload()])
    assert len(results) == 1
    assert results[0]["suggestion_id"] == "SUG-001"
    assert results[0]["discussion_id"] == "disc-hash"
    assert results[0]["note_id"] == 555
    assert results[0]["publish_status"] == "published"
    assert results[0]["attempt_count"] == 1
    assert results[0]["positions"][0]["position"]["head_sha"] == "h"


def test_publish_inline_suggestions_skips_when_file_not_in_diff():
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.get_diff_files = lambda: []
    results = gp.publish_inline_suggestions([_payload()])
    assert results[0]["publish_status"] == "failed"
    assert results[0]["skip_reason"] == "file_not_in_diff"


def test_publish_inline_suggestions_logs_file_not_in_diff(monkeypatch):
    import pr_agent.git_providers.gitlab_provider as gp_module
    logged = []
    fake_logger = MagicMock()
    fake_logger.info.side_effect = lambda msg: logged.append(msg)
    monkeypatch.setattr(gp_module, "get_logger", lambda: fake_logger)

    gp = _provider_with_mocks(_FakeDiscussion())
    gp.get_diff_files = lambda: []
    gp.publish_inline_suggestions([_payload()])
    assert any("file_not_in_diff" in m or "not found in MR" in m for m in logged)


def test_publish_inline_suggestions_logs_line_out_of_range(monkeypatch):
    import pr_agent.git_providers.gitlab_provider as gp_module
    logged = []
    fake_logger = MagicMock()
    fake_logger.info.side_effect = lambda msg: logged.append(msg)
    monkeypatch.setattr(gp_module, "get_logger", lambda: fake_logger)

    gp = _provider_with_mocks(_FakeDiscussion())
    payload = _payload()
    payload["relevant_lines_start"] = 9999
    payload["relevant_lines_end"] = 9999
    gp.publish_inline_suggestions([payload])
    assert any("line_out_of_range" in m or "out of range" in m for m in logged)


def test_publish_inline_suggestions_marks_failed_when_inline_rejected():
    # When GitLab rejects the inline position we must NOT fall back to a plain
    # verbose MR note, and the record must be 'failed' (not 'published').
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.create.side_effect = Exception("not a '+' line")
    results = gp.publish_inline_suggestions([_payload()])
    assert results[0]["publish_status"] == "failed"
    assert results[0]["skip_reason"] in ("inline_rejected", "native_inline_rejected", "no_id_returned")
    assert results[0]["discussion_id"] is None
    gp.mr.notes.create.assert_not_called()


def test_send_inline_comment_no_fallback_returns_none():
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.create.side_effect = Exception("not a '+' line")
    tf = gp.get_diff_files()[0]
    created = gp.send_inline_comment(
        "body```suggestion\nx\n```", "addition", True, "src/a.go", "l10",
        -1, tf, 11, original_suggestion={}, fallback=False)
    assert created is None
    gp.mr.notes.create.assert_not_called()


def test_send_inline_comment_logs_rejection_reason_and_position(monkeypatch):
    # Regression: previously the real GitLab exception (e.g. "position not
    # part of the diff") was swallowed, leaving only a generic
    # "no_id_returned" skip_reason with no clue why GitLab refused the
    # discussion. The rejection log must now include both the exception text
    # and the position payload that was sent, so root-causing a bad
    # relevant_lines_start/end no longer requires manual diff comparison.
    import pr_agent.git_providers.gitlab_provider as gp_module

    logged = []
    fake_logger = MagicMock()
    fake_logger.info.side_effect = lambda msg: logged.append(msg)
    monkeypatch.setattr(gp_module, "get_logger", lambda: fake_logger)

    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.create.side_effect = Exception("position not part of the diff")
    tf = gp.get_diff_files()[0]
    created = gp.send_inline_comment(
        "body```suggestion\nx\n```", "addition", True, "src/a.go", "l10",
        -1, tf, 11, original_suggestion={}, fallback=False)

    assert created is None
    assert len(logged) == 1
    assert "position not part of the diff" in logged[0]
    assert "position=" in logged[0]


def test_inline_rejection_refreshes_diff_and_retries_with_new_position():
    get_settings().set("pr_code_suggestions.inline_publish_retry_limit", 1)
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.list.return_value = []
    gp.mr.discussions.create.side_effect = [Exception("position not part of the diff"), _FakeDiscussion()]
    stale_diff = gp.get_relevant_diff("src/a.go", "l10")
    fresh_diff = MagicMock(base_commit_sha="new-base", start_commit_sha="new-start", head_commit_sha="new-head")

    def refresh():
        gp.get_relevant_diff = lambda _file, _line: fresh_diff

    gp.refresh_merge_request_diff = refresh

    result = gp.publish_inline_suggestions([_payload()])[0]

    assert gp.mr.discussions.create.call_count == 2
    assert result["publish_status"] == "published"
    assert result["attempt_count"] == 2
    assert result["positions"] == [
        {
            "attempt": 1,
            "position": {
                "position_type": "text", "new_path": "src/a.go", "old_path": "src/a.go",
                "base_sha": stale_diff.base_commit_sha, "start_sha": stale_diff.start_commit_sha,
                "head_sha": stale_diff.head_commit_sha, "new_line": 10,
            },
            "error": "position not part of the diff",
        },
        {
            "attempt": 2,
            "position": {
                "position_type": "text", "new_path": "src/a.go", "old_path": "src/a.go",
                "base_sha": "new-base", "start_sha": "new-start", "head_sha": "new-head", "new_line": 10,
            },
            "error": "",
        },
    ]
    assert result["provider_error"] == ""


def test_ambiguous_create_is_reconciled_by_marker_without_retry():
    get_settings().set("pr_code_suggestions.inline_publish_retry_limit", 1)
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.create.side_effect = Exception("connection reset")
    existing = _FakeDiscussion()
    existing.attributes = {
        "notes": [{"id": 555, "body": _payload()["idempotency_marker"]}],
    }
    gp.mr.discussions.list.side_effect = [[], [existing]]

    result = gp.publish_inline_suggestions([_payload()])[0]

    assert result["publish_status"] == "published"
    assert result["attempt_count"] == 1
    gp.mr.discussions.create.assert_called_once()


def test_file_missing_from_stale_cache_is_retried_after_refresh():
    get_settings().set("pr_code_suggestions.inline_publish_retry_limit", 1)
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.list.return_value = []
    target_file = gp.get_diff_files()[0]
    state = {"fresh": False}
    gp.get_diff_files = lambda: [target_file] if state["fresh"] else []
    gp.refresh_merge_request_diff = lambda: state.update(fresh=True)

    result = gp.publish_inline_suggestions([_payload()])[0]

    assert result["publish_status"] == "published"
    assert result["attempt_count"] == 2
    assert result["positions"][0] == {
        "attempt": 1, "position": {}, "error": "file_not_in_diff",
    }


def test_native_failures_fall_back_to_one_ordinary_note():
    get_settings().set("pr_code_suggestions.inline_publish_retry_limit", 1)
    get_settings().set("pr_code_suggestions.inline_publish_fallback_comment", True)
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.list.return_value = []
    gp.mr.notes.list.return_value = []
    gp.mr.discussions.create.side_effect = Exception("position not part of the diff")
    gp.mr.notes.create.return_value = _FakeNote()
    gp.refresh_merge_request_diff = MagicMock()

    result = gp.publish_inline_suggestions([_fallback_payload()])[0]

    assert result["publish_status"] == "fallback_published"
    assert result["discussion_id"] is None
    assert result["note_id"] == 999
    assert result["skip_reason"] == "native_inline_rejected"
    assert result["provider_error"] == "position not part of the diff"
    assert result["attempt_count"] == 2
    assert len(result["positions"]) == 2
    gp.mr.notes.create.assert_called_once()


def test_existing_fallback_note_is_reused_without_create():
    get_settings().set("pr_code_suggestions.inline_publish_retry_limit", 0)
    get_settings().set("pr_code_suggestions.inline_publish_fallback_comment", True)
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.list.return_value = []
    gp.mr.discussions.create.side_effect = Exception("position rejected")
    existing = _FakeNote()
    existing.body = _fallback_payload()["fallback_body"]
    gp.mr.notes.list.return_value = [existing]

    result = gp.publish_inline_suggestions([_fallback_payload()])[0]

    assert result["publish_status"] == "fallback_published"
    assert result["note_id"] == 999
    gp.mr.notes.create.assert_not_called()


def test_repeated_payload_reuses_fallback_without_another_native_attempt():
    get_settings().set("pr_code_suggestions.inline_publish_retry_limit", 0)
    get_settings().set("pr_code_suggestions.inline_publish_fallback_comment", True)
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.list.return_value = []
    gp.mr.discussions.create.side_effect = Exception("position rejected")
    note = _FakeNote()
    note.body = _fallback_payload()["fallback_body"]
    gp.mr.notes.list.side_effect = [[], [], [note]]
    gp.mr.notes.create.return_value = note

    first = gp.publish_inline_suggestions([_fallback_payload()])[0]
    second = gp.publish_inline_suggestions([_fallback_payload()])[0]

    assert first["publish_status"] == second["publish_status"] == "fallback_published"
    gp.mr.discussions.create.assert_called_once()
    gp.mr.notes.create.assert_called_once()


def test_ambiguous_fallback_create_is_reconciled_by_marker():
    get_settings().set("pr_code_suggestions.inline_publish_retry_limit", 0)
    get_settings().set("pr_code_suggestions.inline_publish_fallback_comment", True)
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.list.return_value = []
    gp.mr.discussions.create.side_effect = Exception("position rejected")
    existing = _FakeNote()
    existing.body = _fallback_payload()["fallback_body"]
    gp.mr.notes.list.side_effect = [[], [], [existing]]
    gp.mr.notes.create.side_effect = Exception("connection reset")

    result = gp.publish_inline_suggestions([_fallback_payload()])[0]

    assert result["publish_status"] == "fallback_published"
    assert result["note_id"] == 999
    gp.mr.notes.create.assert_called_once()


def test_native_and_fallback_failure_preserve_last_error():
    get_settings().set("pr_code_suggestions.inline_publish_retry_limit", 0)
    get_settings().set("pr_code_suggestions.inline_publish_fallback_comment", True)
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.list.return_value = []
    gp.mr.notes.list.return_value = []
    gp.mr.discussions.create.side_effect = Exception("position rejected")
    gp.mr.notes.create.side_effect = Exception("note create failed")

    result = gp.publish_inline_suggestions([_fallback_payload()])[0]

    assert result["publish_status"] == "failed"
    assert result["provider_error"] == "position rejected; fallback: note create failed"


def test_fallback_comment_can_be_disabled():
    get_settings().set("pr_code_suggestions.inline_publish_retry_limit", 0)
    get_settings().set("pr_code_suggestions.inline_publish_fallback_comment", False)
    gp = _provider_with_mocks(_FakeDiscussion())
    gp.mr.discussions.list.return_value = []
    gp.mr.discussions.create.side_effect = Exception("position rejected")

    result = gp.publish_inline_suggestions([_fallback_payload()])[0]

    assert result["publish_status"] == "failed"
    assert result["provider_error"] == "position rejected"
    gp.mr.notes.create.assert_not_called()
