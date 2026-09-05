import os
import tempfile

from pr_agent.suggestions.store import (get_published_suggestions,
                                        get_suggestion_threads,
                                        save_suggestion_thread)


def _rec(suggestion_id="SUG-001", project="group/cook", mr_iid="10", **kw):
    base = {
        "suggestion_id": suggestion_id,
        "review_id": "run-1",
        "project": project,
        "mr_iid": mr_iid,
        "commit_sha": "abc123",
        "file_path": "src/a.go",
        "line_start": 10,
        "line_end": 12,
        "label": "possible bug",
        "severity": "High",
        "score": 9,
        "one_sentence_summary": "fix off-by-one",
        "suggestion_content": "why...\nfix...",
        "existing_code": "old",
        "improved_code": "new",
        "gitlab_discussion_id": "d1",
        "gitlab_note_id": 101,
        "publish_status": "published",
        "skip_reason": "",
        "state": "published",
    }
    base.update(kw)
    return base


def test_save_then_get_returns_record():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        assert get_published_suggestions("group/cook", "10", path=path) == []
        assert save_suggestion_thread(_rec(), path=path) is True
        rows = get_published_suggestions("group/cook", "10", path=path)
        assert len(rows) == 1
        assert rows[0]["suggestion_id"] == "SUG-001"
        assert rows[0]["gitlab_discussion_id"] == "d1"
        assert rows[0]["score"] == 9


def test_get_isolated_by_mr_and_project():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        save_suggestion_thread(_rec(mr_iid="10"), path=path)
        assert get_published_suggestions("group/cook", "11", path=path) == []
        assert get_published_suggestions("other/proj", "10", path=path) == []


def test_save_accepts_int_mr_iid_and_extra():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        assert save_suggestion_thread(_rec(mr_iid=10, extra={"k": "v"}), path=path) is True
        rows = get_published_suggestions("group/cook", 10, path=path)
        assert len(rows) == 1


def test_published_suggestion_persists_project_skill_provenance():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        record = _rec(
            project_skill_hash="skill-v2",
            project_skill_manifest_hash="manifest-v2",
            project_skill_target_sha="target-sha",
            project_skill_status="loaded",
            project_skill_rule_ids_json='["api"]',
            project_skill_matched_files_json='{"api":["src/a.go"]}',
            project_skill_reference_hashes_json='{"references/api.md":"ref-hash"}',
        )

        assert save_suggestion_thread(record, path=path) is True
        row = get_published_suggestions("group/cook", "10", path=path)[0]

        assert row["project_skill_hash"] == "skill-v2"
        assert row["project_skill_target_sha"] == "target-sha"
        assert row["project_skill_rule_ids_json"] == '["api"]'


def test_save_never_raises_on_bad_path():
    # parent path goes through a regular file, so the directory cannot be created
    with tempfile.NamedTemporaryFile() as f:
        bad = os.path.join(f.name, "nested", "s.db")
        assert save_suggestion_thread(_rec(), path=bad) is False
