from unittest.mock import MagicMock

import pr_agent.servers.gitlab_webhook as wh
from pr_agent.config_loader import get_settings


def _allow(allowlist=("group/proj",), enabled=True):
    get_settings().set("pr_inline_suggestion_gate.gate_enabled", enabled)
    get_settings().set("pr_inline_suggestion_gate.gate_project_allowlist", list(allowlist))


def _note_payload():
    return {
        "object_kind": "note",
        "object_attributes": {
            "noteable_type": "MergeRequest",
            "note": "looks good",
            "url": "http://gl/group/proj/-/merge_requests/7#note_1",
            "action": "create",
        },
        "merge_request": {"iid": 7, "url": "http://gl/group/proj/-/merge_requests/7"},
        "project": {"path_with_namespace": "group/proj"},
        "user": {"username": "alice"},
    }


def _mr_update_payload():
    return {
        "object_kind": "merge_request",
        "project": {"id": 42, "path_with_namespace": "group/proj"},
        "object_attributes": {
            "action": "update",
            "iid": 7,
            "url": "http://gl/group/proj/-/merge_requests/7",
        },
    }


def test_handle_inline_gate_note_triggers_sync(monkeypatch):
    _allow()
    fake_gp = MagicMock()
    fake_gp.id_project = "group/proj"
    fake_gp.id_mr = 7
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)
    calls = []
    monkeypatch.setattr(
        wh.inline_thread_sync, "sync_mr_threads",
        lambda gp, project, mr_iid, fetch_fn, path=None: calls.append((project, mr_iid)),
    )
    wh._handle_inline_gate_note(_note_payload())
    assert calls == [("group/proj", 7)]


def test_handle_inline_gate_note_noop_for_non_allowlisted_project(monkeypatch):
    _allow(allowlist=("some-other-project",))
    fake_gp = MagicMock()
    fake_gp.id_project = "group/proj"
    fake_gp.id_mr = 7
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)
    calls = []
    monkeypatch.setattr(
        wh.inline_thread_sync, "sync_mr_threads",
        lambda *a, **k: calls.append(1),
    )
    wh._handle_inline_gate_note(_note_payload())
    assert calls == []


def test_handle_inline_gate_note_noop_when_master_switch_off(monkeypatch):
    # Even for an allowlisted project, no sync (and therefore no possible
    # commit-status write, not even a harmless "success") happens while the
    # master switch is off. Mirrors feedback/gate.py's restamp_on_push.
    _allow(enabled=False)
    fake_gp = MagicMock()
    fake_gp.id_project = "group/proj"
    fake_gp.id_mr = 7
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)
    calls = []
    monkeypatch.setattr(
        wh.inline_thread_sync, "sync_mr_threads",
        lambda *a, **k: calls.append(1),
    )
    wh._handle_inline_gate_note(_note_payload())
    assert calls == []


def test_handle_inline_gate_note_noop_without_mr_url(monkeypatch):
    _allow()
    calls = []
    monkeypatch.setattr(
        wh.inline_thread_sync, "sync_mr_threads",
        lambda *a, **k: calls.append(1),
    )
    payload = _note_payload()
    payload["object_attributes"]["url"] = ""
    payload["merge_request"]["url"] = ""
    wh._handle_inline_gate_note(payload)
    assert calls == []


def test_handle_inline_gate_push_triggers_sync(monkeypatch):
    _allow()
    fake_gp = MagicMock()
    fake_gp.id_project = "group/proj"
    fake_gp.id_mr = 7
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)
    calls = []
    monkeypatch.setattr(
        wh.inline_thread_sync, "sync_mr_threads",
        lambda gp, project, mr_iid, fetch_fn, path=None: calls.append((project, mr_iid)),
    )
    wh._handle_inline_gate_push(_mr_update_payload())
    assert calls == [("group/proj", 7)]


def test_handle_inline_gate_push_noop_for_non_allowlisted_project(monkeypatch):
    _allow(allowlist=("some-other-project",))
    fake_gp = MagicMock()
    fake_gp.id_project = "group/proj"
    fake_gp.id_mr = 7
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)
    calls = []
    monkeypatch.setattr(
        wh.inline_thread_sync, "sync_mr_threads",
        lambda *a, **k: calls.append(1),
    )
    wh._handle_inline_gate_push(_mr_update_payload())
    assert calls == []


def test_resolve_mr_iids_for_push_uses_source_branch(monkeypatch):
    class _Resp:
        ok = True
        def json(self):
            return [{"iid": 3}, {"iid": 4}]

    monkeypatch.setattr(wh._requests, "get", lambda *a, **k: _Resp())
    iids = wh._resolve_mr_iids_for_push(42, "refs/heads/feature-x")
    assert iids == [3, 4]


def test_sync_inline_gate_for_push_calls_sync_per_mr(monkeypatch):
    _allow()
    monkeypatch.setattr(wh, "_resolve_mr_iids_for_push", lambda project_id, ref: [3, 4])
    fake_gp = MagicMock()
    fake_gp.id_project = "group/proj"
    fake_gp.id_mr = 3
    monkeypatch.setattr(wh, "get_git_provider_with_context", lambda url: fake_gp)
    calls = []
    monkeypatch.setattr(
        wh.inline_thread_sync, "sync_mr_threads",
        lambda gp, project, mr_iid, fetch_fn, path=None: calls.append(mr_iid),
    )
    payload = {"ref": "refs/heads/x", "project": {"path_with_namespace": "group/proj"}}
    wh._sync_inline_gate_for_push(payload, 42)
    assert calls == [3, 3]  # fake_gp.id_mr is fixed at 3 for both calls in this stub


def test_sync_inline_gate_for_push_noop_for_non_allowlisted_project(monkeypatch):
    _allow(allowlist=("some-other-project",))
    monkeypatch.setattr(wh, "_resolve_mr_iids_for_push", lambda project_id, ref: [3, 4])
    calls = []
    monkeypatch.setattr(
        wh.inline_thread_sync, "sync_mr_threads",
        lambda *a, **k: calls.append(1),
    )
    payload = {"ref": "refs/heads/x", "project": {"path_with_namespace": "group/proj"}}
    wh._sync_inline_gate_for_push(payload, 42)
    assert calls == []
