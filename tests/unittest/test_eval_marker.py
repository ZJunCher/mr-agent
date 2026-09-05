import base64
import json

from pr_agent.eval.marker import (build_eval_marker, build_eval_payload,
                                   encode_eval_marker, parse_eval_marker)


class _FakeProvider:
    def __init__(self, refs=None, id_project="group/repo", id_mr="42",
                 pr_url="https://gitlab.example.com/group/repo/-/merge_requests/42"):
        self._refs = refs
        self.id_project = id_project
        self.id_mr = id_mr
        self.pr_url = pr_url

    def get_diff_refs(self):
        return self._refs


class TestEvalMarker:
    def test_encode_then_parse_roundtrip(self):
        """A built marker can be parsed back into an equivalent payload."""
        refs = {"base_sha": "aaa", "head_sha": "bbb", "start_sha": "ccc"}
        provider = _FakeProvider(refs=refs)
        marker = build_eval_marker(provider, "rid123456789")
        assert marker.startswith("<!-- pr-agent-eval ")
        assert marker.endswith("-->")

        parsed = parse_eval_marker(marker)
        assert parsed is not None
        assert parsed["rid"] == "rid123456789"
        assert parsed["base_sha"] == "aaa"
        assert parsed["head_sha"] == "bbb"
        assert parsed["start_sha"] == "ccc"
        assert parsed["project"] == "group/repo"
        assert parsed["mr_iid"] == "42"

    def test_parse_marker_embedded_in_review_text(self):
        """The marker is found even when surrounded by other review text."""
        payload = {"v": 1, "rid": "abc", "base_sha": "1", "head_sha": "2"}
        marker = encode_eval_marker(payload)
        text = f"# Review\n\nSome findings...\n\n<!-- pr_agent_review_id: abc -->\n{marker}\n"
        parsed = parse_eval_marker(text)
        assert parsed["rid"] == "abc"
        assert parsed["head_sha"] == "2"

    def test_parse_returns_none_when_absent(self):
        assert parse_eval_marker("no marker here") is None
        assert parse_eval_marker("") is None
        assert parse_eval_marker(None) is None

    def test_parse_handles_corrupt_payload(self):
        """A malformed base64/JSON payload yields None rather than raising."""
        assert parse_eval_marker("<!-- pr-agent-eval @@@notb64@@@ -->") is None
        bad = base64.b64encode(b"not-json").decode("ascii")
        assert parse_eval_marker(f"<!-- pr-agent-eval {bad} -->") is None

    def test_build_payload_tolerates_missing_diff_refs(self):
        """If the provider returns no refs, the payload still builds with None shas."""
        provider = _FakeProvider(refs=None)
        payload = build_eval_payload(provider, "rid")
        assert payload["rid"] == "rid"
        assert payload["base_sha"] is None
        assert payload["head_sha"] is None

    def test_build_payload_tolerates_provider_error(self):
        """A provider raising in get_diff_refs must not break payload building."""
        class _Boom(_FakeProvider):
            def get_diff_refs(self):
                raise RuntimeError("boom")

        payload = build_eval_payload(_Boom(), "rid")
        assert payload is not None
        assert payload["rid"] == "rid"
        assert payload["base_sha"] is None

    def test_payload_is_compact_json(self):
        """Encoded payload decodes to valid JSON (compact separators)."""
        payload = {"v": 1, "rid": "x", "cfg": {"config.model": "gpt-4"}}
        marker = encode_eval_marker(payload)
        b64 = marker[len("<!-- pr-agent-eval "):-len(" -->")]
        decoded = json.loads(base64.b64decode(b64))
        assert decoded["cfg"]["config.model"] == "gpt-4"


class TestEvalMarkerInputSnapshot:
    def test_input_snapshot_roundtrips(self):
        """A small input snapshot survives encode/parse and stays plain base64."""
        provider = _FakeProvider(refs={"base_sha": "a", "head_sha": "b"})
        snap = {
            "title": "Fix bug",
            "description": "a short description",
            "commit_messages": "- fix\n- test",
            "related_tickets": ["PROJ-1"],
            "branch": "feature/x",
        }
        marker = build_eval_marker(provider, "rid", input_snapshot=snap)
        assert "z:" not in marker  # small payload stays uncompressed
        parsed = parse_eval_marker(marker)
        assert parsed["input"]["title"] == "Fix bug"
        assert parsed["input"]["commit_messages"] == "- fix\n- test"
        assert parsed["input"]["related_tickets"] == ["PROJ-1"]

    def test_empty_fields_dropped_from_input(self):
        """None/empty input fields are not persisted; all-empty -> no input key."""
        provider = _FakeProvider()
        snap = {"title": None, "description": "", "related_tickets": []}
        parsed = parse_eval_marker(build_eval_marker(provider, "rid", input_snapshot=snap))
        assert "input" not in parsed

    def test_large_input_is_compressed_and_roundtrips(self):
        """A large input snapshot is zlib-compressed (z: prefix) yet round-trips."""
        provider = _FakeProvider()
        big = "x" * 5000 + " line\n" * 500
        snap = {"title": "big", "description": big, "commit_messages": big}
        marker = build_eval_marker(provider, "rid", input_snapshot=snap)
        assert "pr-agent-eval z:" in marker
        parsed = parse_eval_marker(marker)
        assert parsed["input"]["description"] == big

    def test_oversized_input_is_dropped(self):
        """If even compressed the marker is too large, the input is dropped."""
        import os
        provider = _FakeProvider()
        # random-ish, low-compressibility payload well above the marker cap
        huge = os.urandom(80000).hex()
        snap = {"title": "t", "description": huge}
        marker = build_eval_marker(provider, "rid", input_snapshot=snap)
        parsed = parse_eval_marker(marker)
        assert parsed["rid"] == "rid"
        assert "input" not in parsed

    def test_legacy_uncompressed_marker_still_parses(self):
        """Markers produced before compression (plain base64) remain parseable."""
        import base64 as _b64
        import json as _json
        payload = {"v": 1, "rid": "legacy", "base_sha": "1"}
        token = _b64.b64encode(
            _json.dumps(payload, separators=(",", ":")).encode()).decode()
        parsed = parse_eval_marker(f"<!-- pr-agent-eval {token} -->")
        assert parsed["rid"] == "legacy"
