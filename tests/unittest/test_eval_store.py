import os
import tempfile

from pr_agent.eval.store import (find_replayable_runs_by_review_ids,
                                 get_benchmark_db_path, get_review_runs_db_path,
                                 list_replay_results, list_review_runs,
                                 save_replay_result, save_review_run)


class TestReviewRunsStore:
    def _tmp_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)  # let sqlite create it fresh
        return path

    def test_save_and_list_review_run(self):
        path = self._tmp_db()
        try:
            record = {
                "review_id": "rid1",
                "pr_url": "https://gitlab/x/-/merge_requests/1",
                "provider": "gitlab",
                "project": "group/repo",
                "mr_iid": "1",
                "base_sha": "aaa",
                "head_sha": "bbb",
                "start_sha": "ccc",
                "model": "gpt-4",
                "cfg": {"config.model": "gpt-4"},
                "review_output": "## Review\nlooks good",
                "note_id": 555,
                "discussion_id": "disc1",
                "marker_ts": "2026-01-01T00:00:00Z",
            }
            assert save_review_run(record, path=path) is True
            rows = list_review_runs(path=path)
            assert len(rows) == 1
            assert rows[0]["review_id"] == "rid1"
            assert rows[0]["base_sha"] == "aaa"
            assert rows[0]["cfg_json"] == '{"config.model": "gpt-4"}'
            assert rows[0]["mr_iid"] == "1"
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_save_review_run_is_idempotent(self):
        """A second save with the same review_id is ignored (keeps the first)."""
        path = self._tmp_db()
        try:
            save_review_run({"review_id": "rid1", "review_output": "first"}, path=path)
            save_review_run({"review_id": "rid1", "review_output": "second"}, path=path)
            rows = list_review_runs(path=path)
            assert len(rows) == 1
            assert rows[0]["review_output"] == "first"
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_save_review_run_requires_review_id(self):
        path = self._tmp_db()
        try:
            assert save_review_run({"review_output": "x"}, path=path) is False
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_only_replayable_filter(self):
        path = self._tmp_db()
        try:
            save_review_run({"review_id": "with", "base_sha": "a", "head_sha": "b"}, path=path)
            save_review_run({"review_id": "without"}, path=path)
            assert len(list_review_runs(path=path)) == 2
            replayable = list_review_runs(path=path, only_replayable=True)
            assert len(replayable) == 1
            assert replayable[0]["review_id"] == "with"
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_list_missing_db_returns_empty(self):
        assert list_review_runs(path="/nonexistent/path/to.db") == []

    def test_find_replayable_runs_by_exact_review_identity(self):
        path = self._tmp_db()
        try:
            save_review_run({
                "review_id": "rid-exact",
                "project": "group/repo",
                "mr_iid": "1",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "input": {"title": "frozen"},
            }, path=path)

            rows = find_replayable_runs_by_review_ids(("rid-exact", "missing"), path=path)

            assert [row["review_id"] for row in rows] == ["rid-exact"]
            assert rows[0]["input"] == {"title": "frozen"}
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestReplayResultsStore:
    def _tmp_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        return path

    def test_save_and_list_replay_result(self):
        path = self._tmp_db()
        try:
            rec = {
                "tag": "baseline",
                "review_id": "rid1",
                "review_output": "new review",
                "status": "ok",
                "duration_ms": 1234,
                "model": "gpt-4",
            }
            assert save_replay_result(rec, path=path) is True
            rows = list_replay_results("baseline", path=path)
            assert len(rows) == 1
            assert rows[0]["review_id"] == "rid1"
            assert rows[0]["status"] == "ok"
            assert rows[0]["duration_ms"] == 1234
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_replay_result_upsert_by_tag_and_review(self):
        """Re-running the same tag+review_id replaces the previous row."""
        path = self._tmp_db()
        try:
            save_replay_result({"tag": "exp", "review_id": "rid1", "review_output": "v1"}, path=path)
            save_replay_result({"tag": "exp", "review_id": "rid1", "review_output": "v2"}, path=path)
            rows = list_replay_results("exp", path=path)
            assert len(rows) == 1
            assert rows[0]["review_output"] == "v2"
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_replay_result_requires_tag_and_review_id(self):
        path = self._tmp_db()
        try:
            assert save_replay_result({"review_id": "rid1"}, path=path) is False
            assert save_replay_result({"tag": "t"}, path=path) is False
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestReviewRunsInputSnapshot:
    def _tmp_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        return path

    def test_input_json_is_persisted(self):
        path = self._tmp_db()
        try:
            rec = {
                "review_id": "rid-in",
                "input": {"title": "T", "commit_messages": "c1\nc2"},
            }
            assert save_review_run(rec, path=path) is True
            rows = list_review_runs(path=path)
            assert rows[0]["input_json"] == '{"title": "T", "commit_messages": "c1\\nc2"}'
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_migration_adds_input_json_to_old_db(self):
        """An existing review_runs table without input_json gets the column added."""
        import sqlite3
        path = self._tmp_db()
        try:
            conn = sqlite3.connect(path)
            # the v2.0 schema: every column EXCEPT input_json
            conn.execute(
                "CREATE TABLE review_runs (review_id TEXT PRIMARY KEY, "
                "created_at TEXT NOT NULL DEFAULT '', pr_url TEXT, provider TEXT, "
                "project TEXT, mr_iid TEXT, base_sha TEXT, head_sha TEXT, "
                "start_sha TEXT, model TEXT, cfg_json TEXT, review_output TEXT, "
                "note_id TEXT, discussion_id TEXT, marker_ts TEXT, extra_json TEXT)")
            conn.commit()
            conn.close()

            assert save_review_run(
                {"review_id": "rid-old", "input": {"title": "X"}}, path=path) is True
            cols = {r[1] for r in sqlite3.connect(path)
                    .execute("PRAGMA table_info(review_runs)").fetchall()}
            assert "input_json" in cols
            rows = list_review_runs(path=path)
            assert rows[0]["input_json"] == '{"title": "X"}'
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestReviewRunsScoreComment:
    def _tmp_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        return path

    def test_score_and_comment_persisted(self):
        path = self._tmp_db()
        try:
            rec = {"review_id": "rid-sc", "score": 4, "comment": "useful",
                   "review_output": "r"}
            assert save_review_run(rec, path=path) is True
            row = list_review_runs(path=path)[0]
            assert row["score"] == 4
            assert row["comment"] == "useful"
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_rescore_updates_score_but_keeps_frozen_content(self):
        """A second /feedback updates score/comment to the latest, but the frozen
        review_output/input stay as first captured."""
        path = self._tmp_db()
        try:
            save_review_run({"review_id": "rid-r", "score": 1, "comment": "bad",
                             "review_output": "first", "input": {"title": "T"}}, path=path)
            save_review_run({"review_id": "rid-r", "score": 5, "comment": "great",
                             "review_output": "second", "input": {"title": "CHANGED"}}, path=path)
            rows = list_review_runs(path=path)
            assert len(rows) == 1
            assert rows[0]["score"] == 5          # latest rating wins
            assert rows[0]["comment"] == "great"
            assert rows[0]["review_output"] == "first"          # content frozen
            assert rows[0]["input_json"] == '{"title": "T"}'    # input frozen
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_migration_adds_score_comment_to_old_db(self):
        import sqlite3
        path = self._tmp_db()
        try:
            conn = sqlite3.connect(path)
            # an older schema: with input_json but WITHOUT score/comment
            conn.execute(
                "CREATE TABLE review_runs (review_id TEXT PRIMARY KEY, "
                "created_at TEXT NOT NULL DEFAULT '', pr_url TEXT, provider TEXT, "
                "project TEXT, mr_iid TEXT, base_sha TEXT, head_sha TEXT, "
                "start_sha TEXT, model TEXT, cfg_json TEXT, review_output TEXT, "
                "note_id TEXT, discussion_id TEXT, marker_ts TEXT, input_json TEXT, "
                "extra_json TEXT)")
            conn.commit()
            conn.close()

            assert save_review_run(
                {"review_id": "rid-mig", "score": 3, "comment": "ok"}, path=path) is True
            cols = {r[1] for r in sqlite3.connect(path)
                    .execute("PRAGMA table_info(review_runs)").fetchall()}
            assert {"score", "comment"} <= cols
            row = list_review_runs(path=path)[0]
            assert row["score"] == 3
            assert row["comment"] == "ok"
        finally:
            if os.path.exists(path):
                os.unlink(path)
