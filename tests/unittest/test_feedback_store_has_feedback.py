import os
import tempfile

from pr_agent.feedback.store import (
    get_project_skill_usages,
    has_feedback,
    save_feedback,
    save_project_skill_usage,
)


def _record(project, mr_iid, score=5):
    return {"project": project, "mr_iid": mr_iid, "score": score, "comment": "ok"}


def test_has_feedback_true_after_save():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fb.db")
        assert has_feedback("group/proj", "10", path=path) is False
        assert save_feedback(_record("group/proj", "10"), path=path) is True
        assert has_feedback("group/proj", "10", path=path) is True


def test_has_feedback_isolated_by_mr():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fb.db")
        save_feedback(_record("group/proj", "10"), path=path)
        assert has_feedback("group/proj", "11", path=path) is False
        assert has_feedback("other/proj", "10", path=path) is False


def test_has_feedback_accepts_int_mr_iid():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fb.db")
        save_feedback(_record("group/proj", 10), path=path)
        assert has_feedback("group/proj", 10, path=path) is True


def test_project_skill_usage_is_idempotent_and_joinable_by_review_id():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fb.db")
        usage = {
            "review_id": "review-1",
            "command": "review",
            "project": "group/proj",
            "mr_iid": "10",
            "target_branch": "main",
            "target_sha": "sha-1",
            "skill_hash": "skill-1",
            "manifest_hash": "manifest-1",
            "load_status": "loaded",
            "selected_rule_ids": ["api"],
            "matched_files": {"api": ["src/api.cc"]},
            "reference_hashes": {"references/api.md": "ref-1"},
            "global_prompt_set_hash": "global-1",
            "prompt_bundle_hash": "bundle-1",
        }

        assert save_project_skill_usage(usage, path=path) is True
        assert save_project_skill_usage(usage, path=path) is True
        rows = get_project_skill_usages("group/proj", "10", path=path)

        assert len(rows) == 1
        assert rows[0]["review_id"] == "review-1"
        assert rows[0]["skill_hash"] == "skill-1"
        assert rows[0]["selected_rule_ids_json"] == '["api"]'
        assert rows[0]["global_prompt_set_hash"] == "global-1"
        assert rows[0]["prompt_bundle_hash"] == "bundle-1"
