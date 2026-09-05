from unittest.mock import MagicMock

import pr_agent.servers.gitlab_webhook as wh


def _resp(message: str, author_email: str, ok: bool = True):
    r = MagicMock()
    r.ok = ok
    r.json.return_value = {"message": message, "author_email": author_email}
    return r


def _patch_commit(monkeypatch, message, author_email, ok=True):
    monkeypatch.setattr(wh, "_gitlab_api_get", lambda path: _resp(message, author_email, ok))


def _patch_rollback_lookups(monkeypatch, pipelines, *, pipelines_ok=True):
    calls = []

    def api_get(path, *, params=None, timeout=15):
        calls.append((path, params, timeout))
        if "/repository/commits/" in path:
            return _resp(
                "revert: 撤回自动修复\n\n[pr-agent-rollback:repair-task-549:rollback-task-549]",
                "pr-agent@noreply.local",
            )
        response = MagicMock()
        response.ok = pipelines_ok
        response.json.return_value = pipelines
        return response

    monkeypatch.setattr(wh, "_gitlab_api_get", api_get)
    return calls


class TestLockKeyConsistency:
    """锁 key 必须用项目路径（path_with_namespace），不是数字 project_id。

    回归保护：UT Agent 的 _run_lock 用 git_provider.id_project（= MR URL 解析出的
    路径 eabot/cook），webhook 若错传数字 id（如 2），workspace_key 的 sha256 不同，
    会永远查不到锁导致压制失效。
    """

    def test_is_mr_being_fixed_called_with_path_not_numeric_id(self, monkeypatch):
        _patch_commit(monkeypatch, "[UT Agent] MR !5: 自动代码变更", "ut-agent@noreply.local")
        seen = {}
        monkeypatch.setattr(
            "ut_agent.agent.is_mr_being_fixed",
            lambda pid, iid: seen.update(project_id=pid, mr_iid=iid) or True,
        )
        # project_id=2（数字），path_with_namespace="eabot/cook"
        assert wh._should_suppress_pipeline_card("abc123", 2, 5, project_path="eabot/cook") is True
        assert seen["project_id"] == "eabot/cook", (
            f"锁 key 必须用路径，实际传了 {seen['project_id']!r}（数字 id 会导致查不到锁）"
        )

    def test_falls_back_to_numeric_id_when_no_path(self, monkeypatch):
        # 拿不到 path 时退回数字 id（兜底，不至于崩）
        _patch_commit(monkeypatch, "[UT Agent] MR !5: 自动代码变更", "ut-agent@noreply.local")
        seen = {}
        monkeypatch.setattr(
            "ut_agent.agent.is_mr_being_fixed",
            lambda pid, iid: seen.update(project_id=pid) or True,
        )
        assert wh._should_suppress_pipeline_card("abc123", 2, 5, project_path="") is True
        assert seen["project_id"] == "2"


class TestShouldSuppressPipelineCard:
    def test_suppress_first_automatic_rollback_pipeline(self, monkeypatch):
        calls = _patch_rollback_lookups(
            monkeypatch,
            [{"id": 34745, "source": "merge_request_event"}],
        )

        assert wh._should_suppress_pipeline_card(
            "rollback-sha",
            42,
            5,
            pipeline_id=34745,
            pipeline_source="merge_request_event",
        ) is True
        assert calls[-1][1] == {
            "sha": "rollback-sha",
            "source": "merge_request_event",
            "order_by": "id",
            "sort": "asc",
            "per_page": 1,
        }

    def test_do_not_suppress_later_rollback_pipeline_rerun(self, monkeypatch):
        _patch_rollback_lookups(
            monkeypatch,
            [{"id": 34745, "source": "merge_request_event"}],
        )

        assert wh._should_suppress_pipeline_card(
            "rollback-sha",
            42,
            5,
            pipeline_id=34782,
            pipeline_source="merge_request_event",
        ) is False

    def test_do_not_suppress_rollback_without_pipeline_identity(self, monkeypatch):
        calls = _patch_rollback_lookups(monkeypatch, [])

        assert wh._should_suppress_pipeline_card("rollback-sha", 42, 5) is False
        assert len(calls) == 1

    def test_do_not_suppress_rollback_when_pipeline_lookup_fails(self, monkeypatch):
        _patch_rollback_lookups(monkeypatch, [], pipelines_ok=False)

        assert wh._should_suppress_pipeline_card(
            "rollback-sha",
            42,
            5,
            pipeline_id=34745,
            pipeline_source="merge_request_event",
        ) is False

    def test_do_not_suppress_rollback_when_pipeline_list_is_empty(self, monkeypatch):
        _patch_rollback_lookups(monkeypatch, [])

        assert wh._should_suppress_pipeline_card(
            "rollback-sha",
            42,
            5,
            pipeline_id=34745,
            pipeline_source="merge_request_event",
        ) is False

    def test_do_not_suppress_rollback_when_pipeline_identity_is_invalid(self, monkeypatch):
        _patch_rollback_lookups(
            monkeypatch,
            [{"id": "invalid", "source": "merge_request_event"}],
        )

        assert wh._should_suppress_pipeline_card(
            "rollback-sha",
            42,
            5,
            pipeline_id=34745,
            pipeline_source="merge_request_event",
        ) is False

    def test_do_not_suppress_rollback_when_pipeline_source_differs(self, monkeypatch):
        _patch_rollback_lookups(
            monkeypatch,
            [{"id": 34745, "source": "push"}],
        )

        assert wh._should_suppress_pipeline_card(
            "rollback-sha",
            42,
            5,
            pipeline_id=34745,
            pipeline_source="merge_request_event",
        ) is False

    def test_invalid_rollback_marker_is_not_suppressed(self, monkeypatch):
        _patch_commit(
            monkeypatch,
            "revert: manual\n\n[pr-agent-rollback:missing-child]",
            "pr-agent@noreply.local",
        )
        monkeypatch.setattr("ut_agent.agent.is_mr_being_fixed", lambda pid, iid: False)

        assert wh._should_suppress_pipeline_card("rollback-sha", 42, 5) is False

    def test_suppress_when_ut_agent_commit_and_lock_held(self, monkeypatch):
        _patch_commit(monkeypatch, "[UT Agent] MR !5: 自动代码变更", "ut-agent@noreply.local")
        monkeypatch.setattr("ut_agent.agent.is_mr_being_fixed", lambda pid, iid: True)
        assert wh._should_suppress_pipeline_card("abc123", 42, 5) is True

    def test_not_suppress_when_user_commit(self, monkeypatch):
        _patch_commit(monkeypatch, "fix: my manual change", "jun.zhao@example.com")
        # 即使锁被持有，用户 commit 也必须照发
        monkeypatch.setattr("ut_agent.agent.is_mr_being_fixed", lambda pid, iid: True)
        assert wh._should_suppress_pipeline_card("abc123", 42, 5) is False

    def test_not_suppress_when_ut_agent_commit_but_lock_free(self, monkeypatch):
        _patch_commit(monkeypatch, "[UT Agent] MR !5: 自动代码变更", "ut-agent@noreply.local")
        monkeypatch.setattr("ut_agent.agent.is_mr_being_fixed", lambda pid, iid: False)
        assert wh._should_suppress_pipeline_card("abc123", 42, 5) is False

    def test_not_suppress_when_format_bot_commit(self, monkeypatch):
        # 格式 bot 的 commit 不是 [UT Agent]，不应压制
        _patch_commit(monkeypatch, "style: 自动修复代码格式 [format-bot]", "eabot_devops@example.com")
        monkeypatch.setattr("ut_agent.agent.is_mr_being_fixed", lambda pid, iid: True)
        assert wh._should_suppress_pipeline_card("abc123", 42, 5) is False

    def test_not_suppress_on_commit_lookup_error(self, monkeypatch):
        monkeypatch.setattr(wh, "_gitlab_api_get", lambda path: None)
        # 取不到 commit 时宁可照发，不误压
        assert wh._should_suppress_pipeline_card("abc123", 42, 5) is False

    def test_not_suppress_when_sha_empty(self, monkeypatch):
        monkeypatch.setattr("ut_agent.agent.is_mr_being_fixed", lambda pid, iid: True)
        assert wh._should_suppress_pipeline_card("", 42, 5) is False

    def test_suppress_via_author_email_fallback(self, monkeypatch):
        # message 不带前缀但作者邮箱是 bot，仍判定为 agent commit
        _patch_commit(monkeypatch, "some other message", "ut-agent@noreply.local")
        monkeypatch.setattr("ut_agent.agent.is_mr_being_fixed", lambda pid, iid: True)
        assert wh._should_suppress_pipeline_card("abc123", 42, 5) is True
