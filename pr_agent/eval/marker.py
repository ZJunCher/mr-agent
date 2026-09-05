"""Hidden ``pr-agent-eval`` marker: freeze review context for later replay.

A review published by PR-Agent already carries a ``pr_agent_review_id`` marker
used by ``/feedback`` to link a rating back to the review. For the offline
evaluation benchmark we add a *second*, independent marker that freezes the
exact inputs the review was produced against::

    <!-- pr-agent-eval <base64-json> -->          # small payloads (legacy)
    <!-- pr-agent-eval z:<base64-zlib-json> -->   # larger payloads (compressed)

The payload is a JSON object with two parts:

- frozen *code* state: base/head/start sha, model, project, a tiny config
  snapshot — lets us rebuild the historical diff via the GitLab Compare API.
- frozen *non-code* inputs (``input``): title, description, commit messages,
  branches and related tickets as of review time — lets a replay reproduce the
  exact same prompt inputs even if the MR is later edited.

Larger payloads are zlib-compressed and tagged with a ``z:`` prefix; small ones
stay plain base64 for readability and backward compatibility. Both building and
parsing are best-effort: any failure returns an empty result and must never
break review publishing or feedback handling.
"""

import base64
import json
import re
import zlib
from typing import Optional

from pr_agent.config_loader import get_settings
from pr_agent.feedback.timez import now_cn_iso
from pr_agent.log import get_logger

# Matches the (optionally ``z:``-prefixed) base64 payload of the eval marker.
EVAL_MARKER_RE = re.compile(r"<!--\s*pr-agent-eval\s+((?:z:)?[A-Za-z0-9+/=]+)\s*-->")

# Payloads whose raw JSON is at most this many bytes are stored as plain base64;
# larger ones are zlib-compressed. Keeps small markers human-inspectable while
# stopping full input snapshots from bloating the published review note.
_COMPRESS_THRESHOLD = 800

# Hard upper bound on the encoded token length. If a payload (typically because
# of a huge description / commit list) still exceeds this after compression, the
# ``input`` snapshot is dropped so the review note can never be bloated.
_MARKER_MAX_LEN = 60000

# A small snapshot of config keys that influence review output. Captured so a
# later experiment can record/compare "what configuration produced this review".
_CFG_SNAPSHOT_KEYS = (
    "config.model",
    "config.git_provider",
    "config.allow_dynamic_context",
    "config.patch_extra_lines_before",
    "config.patch_extra_lines_after",
    "pr_reviewer.require_ticket_analysis_review",
    "pr_reviewer.num_max_findings",
    "pr_reviewer.extra_instructions",
)


def _cfg_snapshot() -> dict:
    snapshot = {}
    for key in _CFG_SNAPSHOT_KEYS:
        try:
            value = get_settings().get(key, None)
        except Exception:
            value = None
        if value is not None and value != "":
            snapshot[key] = value
    return snapshot


def _str_or_none(value) -> Optional[str]:
    return str(value) if value is not None else None


def _clean_input_snapshot(input_snapshot: Optional[dict]) -> Optional[dict]:
    """Drop empty fields so an absent input is encoded as nothing, not noise."""
    if not input_snapshot or not isinstance(input_snapshot, dict):
        return None
    cleaned = {}
    for key, value in input_snapshot.items():
        if value is None:
            continue
        if isinstance(value, (str, list, dict)) and len(value) == 0:
            continue
        cleaned[key] = value
    return cleaned or None


def build_eval_payload(git_provider, review_id: str,
                       input_snapshot: Optional[dict] = None) -> Optional[dict]:
    """Build the frozen-context payload for a review, or None on failure."""
    try:
        refs = {}
        try:
            refs = git_provider.get_diff_refs() or {}
        except Exception:
            refs = {}
        payload = {
            "v": 1,
            "rid": review_id,
            "provider": get_settings().get("config.git_provider", None),
            "project": _str_or_none(getattr(git_provider, "id_project", None)),
            "mr_iid": _str_or_none(getattr(git_provider, "id_mr", None)),
            "pr_url": _str_or_none(getattr(git_provider, "pr_url", None)),
            "base_sha": refs.get("base_sha"),
            "head_sha": refs.get("head_sha"),
            "start_sha": refs.get("start_sha"),
            "model": get_settings().get("config.model", None),
            "ts": now_cn_iso(),
            "cfg": _cfg_snapshot(),
        }
        cleaned_input = _clean_input_snapshot(input_snapshot)
        if cleaned_input:
            payload["input"] = cleaned_input
        return payload
    except Exception as e:
        get_logger().warning(f"Failed to build eval payload: {e}")
        return None


def encode_eval_marker(payload: dict) -> str:
    """Encode a payload dict into the full ``<!-- pr-agent-eval ... -->`` string.

    Small payloads are stored as plain base64 (backward compatible); larger ones
    are zlib-compressed and tagged with a ``z:`` prefix.
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) <= _COMPRESS_THRESHOLD:
        token = base64.b64encode(raw).decode("ascii")
    else:
        token = "z:" + base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    return f"<!-- pr-agent-eval {token} -->"


def build_eval_marker(git_provider, review_id: str,
                      input_snapshot: Optional[dict] = None) -> str:
    """Build the full eval marker string for a review (``""`` on failure).

    If the encoded marker (usually due to a very large input snapshot) would
    exceed ``_MARKER_MAX_LEN``, the ``input`` snapshot is dropped and only the
    compact code/config state is kept, so the review note is never bloated.
    """
    payload = build_eval_payload(git_provider, review_id, input_snapshot)
    if not payload:
        return ""
    try:
        marker = encode_eval_marker(payload)
        if len(marker) > _MARKER_MAX_LEN and "input" in payload:
            get_logger().info(
                "Eval marker too large; dropping input snapshot for this review",
                artifact={"review_id": review_id, "marker_len": len(marker)})
            payload.pop("input", None)
            marker = encode_eval_marker(payload)
        return marker
    except Exception as e:
        get_logger().warning(f"Failed to encode eval marker: {e}")
        return ""


def _decode_token(token: str) -> bytes:
    if token.startswith("z:"):
        return zlib.decompress(base64.b64decode(token[2:]))
    data = base64.b64decode(token)
    # tolerate a compressed payload that lost its prefix
    if data[:2] == b"\x78\x9c" or data[:1] == b"\x78":
        try:
            return zlib.decompress(data)
        except Exception:
            return data
    return data


def parse_eval_marker(text: str) -> Optional[dict]:
    """Parse the eval marker out of a piece of text. Returns dict or None."""
    if not text:
        return None
    try:
        match = EVAL_MARKER_RE.search(text)
        if not match:
            return None
        raw = _decode_token(match.group(1))
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        get_logger().warning(f"Failed to parse eval marker: {e}")
        return None
