from unittest.mock import MagicMock

import pr_agent.servers.gitlab_webhook as wh


def _payload(old_title, new_title):
    return {
        "object_kind": "merge_request",
        "project": {"id": 42},
        "object_attributes": {
            "action": "update",
            "iid": 5,
            "url": "http://gl/group/proj/-/merge_requests/5",
        },
        "changes": {
            "title": {"previous": old_title, "current": new_title},
        },
    }


def test_noop_when_changes_has_no_title_key(monkeypatch):
    payload = {
        "object_kind": "merge_request",
        "project": {"id": 42},
        "object_attributes": {"action": "update", "iid": 5, "url": "http://gl/group/proj/-/merge_requests/5"},
        "changes": {"labels": {"previous": [], "current": ["bug"]}},
    }
    called = {"n": 0}
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: called.__setitem__("n", called["n"] + 1))
    wh._handle_title_issue_link_refresh(payload)
    assert called["n"] == 0


def test_noop_when_old_and_new_ids_are_the_same(monkeypatch):
    payload = _payload("m-111 feat: x", "m-111 feat: x renamed")
    called = {"n": 0}
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: called.__setitem__("n", called["n"] + 1))
    wh._handle_title_issue_link_refresh(payload)
    assert called["n"] == 0


def test_noop_when_neither_old_nor_new_title_has_an_id(monkeypatch):
    payload = _payload("feat: x", "feat: x renamed")
    called = {"n": 0}
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: called.__setitem__("n", called["n"] + 1))
    wh._handle_title_issue_link_refresh(payload)
    assert called["n"] == 0


_ISSUE_LINE_RE = "### 需求链接：https://project.feishu.cn/eabot/issue/detail/"


def _make_mr(description):
    mr = MagicMock()
    mr.description = description
    return mr


def test_replaces_placeholder_when_new_id_found(monkeypatch):
    payload = _payload("feat: x", "m-7050198506 feat: x")
    mr = _make_mr(
        "## 变更说明\n内容\n\n"
        "### 需求链接：https://project.feishu.cn/eabot/issue/detail/[在此填入问题ID]\n\n"
        "## 测试说明\n- [x] 单元测试\n"
    )
    fake_gp = MagicMock()
    fake_gp.mr = mr
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)

    wh._handle_title_issue_link_refresh(payload)

    assert "### 需求链接：https://project.feishu.cn/eabot/issue/detail/7050198506" in mr.description
    assert "[在此填入问题ID]" not in mr.description
    # Everything else must be untouched.
    assert "## 变更说明\n内容" in mr.description
    assert "## 测试说明\n- [x] 单元测试" in mr.description
    mr.save.assert_called_once()


def test_replaces_old_id_with_new_id_when_current_value_matches_old_id(monkeypatch):
    payload = _payload("m-1111 feat: x", "m-2222 feat: x")
    mr = _make_mr(
        "## 变更说明\n内容\n\n"
        "### 需求链接：https://project.feishu.cn/eabot/issue/detail/1111\n\n"
        "## 测试说明\n- [x] 单元测试\n"
    )
    fake_gp = MagicMock()
    fake_gp.mr = mr
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)

    wh._handle_title_issue_link_refresh(payload)

    assert "### 需求链接：https://project.feishu.cn/eabot/issue/detail/2222" in mr.description
    mr.save.assert_called_once()


def test_falls_back_to_placeholder_when_new_title_has_no_id(monkeypatch):
    payload = _payload("m-1111 feat: x", "feat: x renamed, id removed")
    mr = _make_mr("### 需求链接：https://project.feishu.cn/eabot/issue/detail/1111\n")
    fake_gp = MagicMock()
    fake_gp.mr = mr
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)

    wh._handle_title_issue_link_refresh(payload)

    assert "### 需求链接：https://project.feishu.cn/eabot/issue/detail/[在此填入问题ID]" in mr.description
    mr.save.assert_called_once()


def test_skips_when_current_value_was_hand_edited(monkeypatch):
    payload = _payload("m-1111 feat: x", "m-2222 feat: x")
    original_description = "### 需求链接：https://project.feishu.cn/eabot/issue/detail/9999999999\n"
    mr = _make_mr(original_description)
    fake_gp = MagicMock()
    fake_gp.mr = mr
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)

    wh._handle_title_issue_link_refresh(payload)

    assert mr.description == original_description
    mr.save.assert_not_called()


def test_skips_when_issue_link_line_not_found_in_description(monkeypatch):
    payload = _payload("feat: x", "m-7050198506 feat: x")
    original_description = "## 变更说明\n内容，没有需求链接这一行\n"
    mr = _make_mr(original_description)
    fake_gp = MagicMock()
    fake_gp.mr = mr
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)

    wh._handle_title_issue_link_refresh(payload)

    assert mr.description == original_description
    mr.save.assert_not_called()


def test_never_raises_when_git_provider_lookup_fails(monkeypatch):
    payload = _payload("feat: x", "m-7050198506 feat: x")
    def _boom(url):
        raise RuntimeError("network down")
    monkeypatch.setattr(wh, "get_git_provider_with_context", _boom)

    wh._handle_title_issue_link_refresh(payload)  # must not raise


from starlette.testclient import TestClient


def test_webhook_route_calls_title_issue_link_refresh_on_update(monkeypatch):
    """Integration-style: POST through the real FastAPI app (TestClient) so the
    starlette_context middleware (required by gitlab_webhook()) is properly set
    up -- calling the route function directly without it raises
    ContextDoesNotExistError. BackgroundTasks run synchronously within
    TestClient's request/response cycle, so the patched fake below is expected
    to have been called by the time `client.post` returns.
    """
    payload = _payload("feat: x", "m-7050198506 feat: x")

    # Other update-branch handlers reach out to GitLab for a real MR/provider;
    # neutralize them so this test only exercises the wiring under test.
    monkeypatch.setattr(wh, "_handle_feedback_gate_push", lambda request_json: None)
    monkeypatch.setattr(wh, "_handle_inline_gate_push", lambda request_json: None)

    called = {"n": 0, "payload": None}

    def fake_refresh(request_json):
        called["n"] += 1
        called["payload"] = request_json

    monkeypatch.setattr(wh, "_handle_title_issue_link_refresh", fake_refresh)

    client = TestClient(wh.app)
    response = client.post("/webhook", json=payload)

    assert response.status_code == 200
    assert called["n"] == 1
    assert called["payload"] == payload
