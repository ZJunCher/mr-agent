"""Ordered, non-authoritative diagnostic candidates from sanitized CI traces."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from pr_agent.triage.failure_explanations import sanitize_failure_text

_SIGNAL_PATTERNS = (
    (
        "fatal",
        re.compile(
            r"\bfatal(?:\s+error)?\b|\bpanic\b|\bsig(?:segv|abrt)\b|\bsegmentation fault\b",
            re.IGNORECASE,
        ),
    ),
    (
        "error",
        re.compile(
            r"\berror\s*:|\bcmake error\b|\bundefined reference\b|\bcould not find\b|\bno such file\b|"
            r"\bnot found\b|\bpermission denied\b|错误[:：]|无法访问|连接检测失败",
            re.IGNORECASE,
        ),
    ),
    ("warning", re.compile(r"\bwarning\s*:|\[[\w.-]*clang[\w.-]*\]", re.IGNORECASE)),
    ("exception", re.compile(r"\bexception\b|\btraceback\b", re.IGNORECASE)),
    ("failure", re.compile(r"\bfail(?:ed|ure)?\b|\bassert(?:ion)?\b|失败", re.IGNORECASE)),
    (
        "termination",
        re.compile(
            r"\bcancel(?:ed|led|lation)?\b|\bterminated\b|\bexited with (?:code|status)\b|"
            r"\bcommand terminated\b|\bkilled by signal\b",
            re.IGNORECASE,
        ),
    ),
)

_PRIORITY_PATTERNS = (
    (
        0,
        re.compile(
            r"\bfatal:\s+remote branch\b.*\bnot found\b|\bfatal:\s+repository\b.*\bnot found\b|"
            r"\bauthentication failed\b|\bpermission denied\b",
            re.IGNORECASE,
        ),
    ),
    (
        5,
        re.compile(
            r"\bcould not find a package configuration file provided by\b|"
            r"\bcould not find\b.*\bconfig\.cmake\b",
            re.IGNORECASE,
        ),
    ),
    (
        10,
        re.compile(
            r"\bcmake error\b|\bfatal error\s*:|\bundefined reference\b|\berror\s*:|"
            r"\bassert(?:ion)?\b|\bsegmentation fault\b",
            re.IGNORECASE,
        ),
    ),
    (
        20,
        re.compile(r"\bambiguous argument\s+['\"]{2}|\bbad revision\b", re.IGNORECASE),
    ),
    (
        40,
        re.compile(r"\brunning\s+upload-pack\s*:\s*user\s+canceled\s+the\s+request\b", re.IGNORECASE),
    ),
    (
        80,
        re.compile(
            r"\busing default deps\.yml\b|\bfalling back\b|\bno files to upload\b|"
            r"\bjob failed\b|\buploading artifacts\b",
            re.IGNORECASE,
        ),
    ),
)

_CODE_LOCATION_RE = re.compile(
    r"(?:^|\s)(?:/builds/[^\s:]+/)?[^\s:]+\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx|py):\d+(?::\d+)?",
    re.IGNORECASE,
)
_CLANG_CHECK_RE = re.compile(r"\bclang-(?:analyzer|diagnostic|tidy)-[\w.-]+\b|\[[\w.-]*clang[\w.-]*\]", re.IGNORECASE)
_CODE_DIAGNOSTIC_RE = re.compile(
    r"\b(?:error|warning)\s*:|\bno member named\b|\buse of undeclared identifier\b|"
    r"\bundefined reference\b|\bfatal error\s*:",
    re.IGNORECASE,
)
_WEAK_ONLY_RE = re.compile(
    r"^\s*[\"']?(?:failure|failures|failed|error|errors)[\"']?\s*[:=]\s*\d+\s*[,}]?\s*$",
    re.IGNORECASE,
)
_DIAGNOSTIC_SUBJECT_PATTERNS = (
    re.compile(r"['‘](?P<subject>[^'’]+)['’]\s+is private within this context", re.IGNORECASE),
    re.compile(r"no member named\s+['‘](?P<subject>[^'’]+)['’]", re.IGNORECASE),
    re.compile(r"fatal error\s*:\s*(?P<subject>[^:]+?)\s*:\s*no such file", re.IGNORECASE),
    re.compile(r"undefined reference to\s+[`'‘](?P<subject>[^`'’]+)", re.IGNORECASE),
    re.compile(r"use of undeclared identifier\s+['‘](?P<subject>[^'’]+)['’]", re.IGNORECASE),
)
_DIAGNOSTIC_LOCATION_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?(?:/[^\s:]+)*?/?[^/\s:]+\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx|py))"
    r":\d+(?::\d+)?",
    re.IGNORECASE,
)
_DIAGNOSTIC_TIMESTAMP_RE = re.compile(
    r"(?:"
    r"(?<!\d)\d{4}-\d{2}-\d{2}[T ](?:\d{2}:\d{2}:\d{2}|\d{6})"
    r"(?:\.\d+)?(?:[zZ]|[+-]\d{2}:?\d{2})?(?![\w.])"
    r"|"
    r"(?<!\d)\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[zZ]|[+-]\d{2}:?\d{2})?(?![\w.])"
    r")"
)
_TRACE_TIMESTAMP_PREFIX_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[T ]\S+\s+")


@dataclass(frozen=True)
class DiagnosticCandidate:
    candidate_id: str
    line_number: int
    signal: str
    text: str
    diagnostic_identity: str = ""
    priority: int = 50

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DiagnosticCandidateSet:
    candidates: tuple[DiagnosticCandidate, ...]
    total_matches: int
    truncated: bool
    identity_count: int = 0
    omitted_identity_count: int = 0


def _signal_for(line: str) -> str:
    return next((name for name, pattern in _SIGNAL_PATTERNS if pattern.search(line)), "")


def _priority_for(line: str, signal: str) -> int:
    lowered = line.lower()
    fallback_markers = ("using default deps.yml", "falling back", "no files to upload", "job failed")
    if any(marker in lowered for marker in fallback_markers):
        return 80
    if "rpc error" in lowered and "cancel" in lowered:
        return 60
    return next((priority for priority, pattern in _PRIORITY_PATTERNS if pattern.search(line)), {
        "fatal": 15,
        "error": 25,
        "warning": 35,
        "exception": 30,
        "failure": 50,
        "termination": 60,
    }.get(signal, 70))


def primary_diagnostic(candidates: Iterable[DiagnosticCandidate]) -> DiagnosticCandidate | None:
    """Select the strongest scheduling observation without changing serialized trace order."""
    values = tuple(item for item in candidates if item.priority < 80)
    return min(values, key=lambda item: (item.priority, item.line_number)) if values else None


def is_diagnostic_line(line: str) -> bool:
    return bool(_signal_for(str(line or "")))


def credible_code_diagnostic(candidate: DiagnosticCandidate | dict | str, job_name: str = "") -> bool:
    """Return whether one observation can safely anchor a code-level Clang repair."""
    if isinstance(candidate, DiagnosticCandidate):
        text = candidate.text
    elif isinstance(candidate, dict):
        text = str(candidate.get("text") or "")
    else:
        text = str(candidate or "")
    normalized = sanitize_failure_text(text, 1000).strip()
    if not normalized or _WEAK_ONLY_RE.fullmatch(normalized):
        return False
    lowered = normalized.lower()
    if lowered in {"job failed", "error", "failed", "failure"} or "no files to upload" in lowered:
        return False
    return bool(
        (_CODE_LOCATION_RE.search(normalized) and _CODE_DIAGNOSTIC_RE.search(normalized))
        or _CLANG_CHECK_RE.search(normalized)
    )


def _candidate_id(identity_key: str, line_number: int, text: str) -> str:
    value = f"{identity_key}:{line_number}:{text}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:20]


def _normalized_source_path(text: str) -> str:
    match = _DIAGNOSTIC_LOCATION_RE.search(text.replace("\\", "/"))
    if match is None:
        return ""
    path = match.group("path").lower()
    for marker in ("/src/", "/include/", "/tests/", "/test/"):
        if marker in path:
            return f"{marker.strip('/')}/{path.rsplit(marker, 1)[1]}"
    return "/".join(path.split("/")[-2:])


def diagnostic_identity_for(text: str, signal: str = "") -> str:
    """Return a stable identity that preserves distinct compiler subjects."""
    normalized = sanitize_failure_text(text, 1000).replace("\\", "/").lower()
    source = _normalized_source_path(normalized)
    subject = ""
    for pattern in _DIAGNOSTIC_SUBJECT_PATTERNS:
        match = pattern.search(normalized)
        if match is not None:
            subject = re.sub(r"\s+", " ", match.group("subject")).strip()
            break
    if not subject:
        fallback = _DIAGNOSTIC_TIMESTAMP_RE.sub("<time>", normalized)
        fallback = _DIAGNOSTIC_LOCATION_RE.sub(lambda match: match.group("path"), fallback)
        subject = re.sub(r"\s+", " ", fallback).strip()
    value = "|".join(part for part in (source, signal.lower(), subject) if part)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _positional_priority_sample(
    values: list[DiagnosticCandidate],
    limit: int,
) -> list[DiagnosticCandidate]:
    bound = max(1, int(limit))
    if len(values) <= bound:
        return list(values)
    # 优先级保底：最强的因果行（如日志中段的 fatal error）不能被头尾位置采样排除。
    reserve_count = max(1, bound // 3)
    reserved_ids = {
        id(item)
        for item in sorted(values, key=lambda item: (item.priority, item.line_number))[:reserve_count]
    }
    remaining = [item for item in values if id(item) not in reserved_ids]
    remaining_slots = bound - len(reserved_ids)
    head_count = (remaining_slots + 1) // 2
    tail_count = remaining_slots - head_count
    picked_ids = {id(item) for item in remaining[:head_count]}
    if tail_count:
        picked_ids |= {id(item) for item in remaining[-tail_count:]}
    selected = reserved_ids | picked_ids
    return [item for item in values if id(item) in selected]


def _bounded_candidates(values: list[DiagnosticCandidate], limit: int) -> tuple[DiagnosticCandidate, ...]:
    bound = max(1, int(limit))
    if len(values) <= bound:
        return tuple(values)
    groups: dict[str, list[DiagnosticCandidate]] = {}
    for item in values:
        groups.setdefault(item.diagnostic_identity, []).append(item)
    representatives = [
        min(items, key=lambda item: (item.priority, item.line_number))
        for items in groups.values()
    ]
    selected = _positional_priority_sample(representatives, bound)
    if len(selected) < bound:
        selected_ids = {id(item) for item in selected}
        remaining = [item for item in values if id(item) not in selected_ids]
        selected.extend(_positional_priority_sample(remaining, bound - len(selected)))
    selected_ids = {id(item) for item in selected}
    return tuple(item for item in values if id(item) in selected_ids)


def extract_diagnostic_candidates(
    trace: str,
    *,
    identity_key: str = "",
    limit: int = 12,
) -> DiagnosticCandidateSet:
    matches: list[DiagnosticCandidate] = []
    for line_number, raw_line in enumerate(str(trace or "").splitlines(), start=1):
        text = sanitize_failure_text(_TRACE_TIMESTAMP_PREFIX_RE.sub("", raw_line), 1000)
        signal = _signal_for(text)
        if not text or not signal:
            continue
        matches.append(DiagnosticCandidate(
            candidate_id=_candidate_id(identity_key, line_number, text),
            line_number=line_number,
            signal=signal,
            text=text,
            diagnostic_identity=diagnostic_identity_for(text, signal),
            priority=_priority_for(text, signal),
        ))
    candidates = _bounded_candidates(matches, limit)
    identities = {item.diagnostic_identity for item in matches}
    selected_identities = {item.diagnostic_identity for item in candidates}
    return DiagnosticCandidateSet(
        candidates,
        len(matches),
        len(matches) > len(candidates),
        len(identities),
        len(identities - selected_identities),
    )
