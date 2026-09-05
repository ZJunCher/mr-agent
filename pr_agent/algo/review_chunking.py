"""Deterministic token- and hunk-aware planning for large PR reviews."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable

from pr_agent.algo.git_patch_processing import decouple_and_convert_to_hunks_with_lines_numbers
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import get_max_tokens

_HUNK_HEADER = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@")


@dataclass(frozen=True)
class DiffReviewUnit:
    unit_id: str
    parent_unit_id: str
    filename: str
    edit_type: str
    hunk_index: int
    part_index: int
    part_count: int
    raw_hunk: str
    raw_text: str
    text: str
    tokens: int


@dataclass(frozen=True)
class ReviewChunk:
    chunk_id: str
    unit_ids: tuple[str, ...]
    text: str
    raw_text: str
    tokens: int


@dataclass(frozen=True)
class ReviewChunkPlan:
    units: tuple[DiffReviewUnit, ...]
    chunks: tuple[ReviewChunk, ...]
    plan_hash: str
    usable_tokens: int
    status: str
    unplanned_unit_ids: tuple[str, ...] = ()
    unreviewable_files: tuple[tuple[str, str], ...] = ()
    error: str = ""

    @property
    def is_complete_plan(self) -> bool:
        return self.status == "ready" and not self.unplanned_unit_ids


@dataclass(frozen=True)
class ReviewCoverage:
    status: str
    expected_unit_ids: tuple[str, ...]
    completed_unit_ids: tuple[str, ...]
    missing_unit_ids: tuple[str, ...]
    duplicate_unit_ids: tuple[str, ...]
    failed_chunk_ids: tuple[str, ...]
    completion_ratio: float


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _edit_type_name(edit_type: EDIT_TYPE | object) -> str:
    if isinstance(edit_type, EDIT_TYPE):
        return edit_type.name.lower()
    return str(edit_type or "unknown").lower()


def _split_hunks(patch: str) -> list[str]:
    lines = patch.splitlines()
    starts = [index for index, line in enumerate(lines) if _HUNK_HEADER.match(line)]
    if not starts:
        return [patch.strip()] if patch.strip() else []
    hunks = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        hunks.append("\n".join(lines[start:end]).strip())
    return [hunk for hunk in hunks if hunk]


def _render_raw(filename: str, raw_hunk: str) -> str:
    return f"\n\n## File: '{filename.strip()}'\n\n{raw_hunk.strip()}\n"


def _render_numbered(file: FilePatchInfo, raw_hunk: str) -> str:
    if file.edit_type == EDIT_TYPE.DELETED:
        return _render_raw(file.filename, raw_hunk)
    return decouple_and_convert_to_hunks_with_lines_numbers(raw_hunk, file)


def _render(file: FilePatchInfo, raw_hunk: str, add_line_numbers: bool) -> tuple[str, str]:
    raw_text = _render_raw(file.filename, raw_hunk)
    text = _render_numbered(file, raw_hunk) if add_line_numbers else raw_text
    return text.strip(), raw_text.strip()


def _make_unit(
    file: FilePatchInfo,
    raw_hunk: str,
    hunk_index: int,
    part_index: int,
    part_count: int,
    parent_unit_id: str,
    token_handler,
    add_line_numbers: bool,
) -> DiffReviewUnit:
    text, raw_text = _render(file, raw_hunk, add_line_numbers)
    unit_id = _stable_hash({
        "parent": parent_unit_id,
        "part_index": part_index,
        "part_count": part_count,
        "content": raw_hunk,
    })
    return DiffReviewUnit(
        unit_id=unit_id,
        parent_unit_id=parent_unit_id,
        filename=file.filename,
        edit_type=_edit_type_name(file.edit_type),
        hunk_index=hunk_index,
        part_index=part_index,
        part_count=part_count,
        raw_hunk=raw_hunk,
        raw_text=raw_text,
        text=text,
        tokens=token_handler.count_tokens(text),
    )


def _split_oversized_hunk(
    file: FilePatchInfo,
    raw_hunk: str,
    hunk_index: int,
    usable_tokens: int,
    token_handler,
    add_line_numbers: bool,
) -> tuple[DiffReviewUnit, ...] | None:
    lines = raw_hunk.splitlines()
    header = lines[0] if lines and _HUNK_HEADER.match(lines[0]) else ""
    body = lines[1:] if header else lines
    if not body:
        return None
    parent_unit_id = _stable_hash({
        "filename": file.filename,
        "hunk_index": hunk_index,
        "raw_hunk": raw_hunk,
    })
    raw_parts: list[str] = []
    current: list[str] = []
    for line in body:
        candidate_lines = [*current, line]
        candidate = "\n".join(([header] if header else []) + candidate_lines)
        candidate_text, _ = _render(file, candidate, add_line_numbers)
        if token_handler.count_tokens(candidate_text) <= usable_tokens:
            current = candidate_lines
            continue
        if not current:
            return None
        raw_parts.append("\n".join(([header] if header else []) + current))
        current = [line]
        single = "\n".join(([header] if header else []) + current)
        single_text, _ = _render(file, single, add_line_numbers)
        if token_handler.count_tokens(single_text) > usable_tokens:
            return None
    if current:
        raw_parts.append("\n".join(([header] if header else []) + current))
    count = len(raw_parts)
    return tuple(
        _make_unit(
            file,
            part,
            hunk_index,
            part_index=index,
            part_count=count,
            parent_unit_id=parent_unit_id,
            token_handler=token_handler,
            add_line_numbers=add_line_numbers,
        )
        for index, part in enumerate(raw_parts, start=1)
    )


def _chunk(units: Iterable[DiffReviewUnit], token_handler) -> ReviewChunk:
    members = tuple(units)
    text = "\n\n".join(unit.text for unit in members)
    raw_text = "\n\n".join(unit.raw_text for unit in members)
    unit_ids = tuple(unit.unit_id for unit in members)
    return ReviewChunk(
        chunk_id=_stable_hash({"unit_ids": unit_ids, "text": text}),
        unit_ids=unit_ids,
        text=text,
        raw_text=raw_text,
        tokens=token_handler.count_tokens(text),
    )


def _plan_hash(units: tuple[DiffReviewUnit, ...], chunks: tuple[ReviewChunk, ...], usable_tokens: int) -> str:
    return _stable_hash({
        "schema_version": 1,
        "usable_tokens": usable_tokens,
        "units": [unit.unit_id for unit in units],
        "chunks": [{"id": chunk.chunk_id, "units": chunk.unit_ids} for chunk in chunks],
    })


def build_review_chunk_plan(
    git_provider,
    token_handler,
    model: str,
    *,
    add_line_numbers: bool = True,
    max_chunks: int = 20,
    output_buffer_tokens: int = 1500,
    metadata_tokens: int = 256,
) -> ReviewChunkPlan:
    """Create a stable plan that never clips or silently drops a reviewable hunk."""
    max_chunks = int(max_chunks)
    output_buffer_tokens = int(output_buffer_tokens)
    metadata_tokens = int(metadata_tokens)
    prompt_tokens = int(getattr(token_handler, "prompt_tokens", 0) or 0)
    usable_tokens = int(get_max_tokens(model)) - prompt_tokens - output_buffer_tokens - metadata_tokens
    if max_chunks <= 0 or output_buffer_tokens < 0 or metadata_tokens < 0 or usable_tokens <= 0:
        return ReviewChunkPlan((), (), _stable_hash({"error": "invalid_budget"}), usable_tokens,
                               "invalid_budget", error="large MR review budget is invalid")

    units: list[DiffReviewUnit] = []
    unreviewable: list[tuple[str, str]] = []
    for file in git_provider.get_diff_files():
        patch = str(getattr(file, "patch", "") or "")
        if not patch.strip():
            unreviewable.append((str(getattr(file, "filename", "") or ""), "missing_patch"))
            continue
        for hunk_index, raw_hunk in enumerate(_split_hunks(patch)):
            parent_unit_id = _stable_hash({
                "filename": file.filename,
                "hunk_index": hunk_index,
                "raw_hunk": raw_hunk,
            })
            unit = _make_unit(file, raw_hunk, hunk_index, 1, 1, parent_unit_id, token_handler, add_line_numbers)
            if unit.tokens <= usable_tokens:
                units.append(unit)
                continue
            parts = _split_oversized_hunk(
                file, raw_hunk, hunk_index, usable_tokens, token_handler, add_line_numbers,
            )
            if parts is None:
                return ReviewChunkPlan(
                    tuple([*units, unit]), (),
                    _stable_hash({"error": "unit_too_large", "parent": parent_unit_id}),
                    usable_tokens, "unit_too_large", unreviewable_files=tuple(unreviewable),
                    error=f"a diff line in {file.filename} exceeds the usable token budget",
                )
            units.extend(parts)

    all_units = tuple(units)
    if not all_units:
        return ReviewChunkPlan(
            (), (), _stable_hash({"schema_version": 1, "units": []}), usable_tokens, "empty",
            unreviewable_files=tuple(unreviewable),
        )

    groups: list[list[DiffReviewUnit]] = []
    current: list[DiffReviewUnit] = []
    for unit in all_units:
        candidate = [*current, unit]
        if current and _chunk(candidate, token_handler).tokens > usable_tokens:
            groups.append(current)
            current = [unit]
        else:
            current = candidate
    if current:
        groups.append(current)

    all_chunks = tuple(_chunk(group, token_handler) for group in groups)
    if any(chunk.tokens > usable_tokens for chunk in all_chunks):
        return ReviewChunkPlan(
            all_units, (), _stable_hash({"error": "chunk_too_large"}), usable_tokens,
            "unit_too_large", unreviewable_files=tuple(unreviewable),
            error="a planned chunk exceeds the usable token budget",
        )
    chunks = all_chunks[:max_chunks]
    planned_ids = {unit_id for chunk in chunks for unit_id in chunk.unit_ids}
    unplanned_ids = tuple(unit.unit_id for unit in all_units if unit.unit_id not in planned_ids)
    status = "capacity_exceeded" if unplanned_ids else "ready"
    return ReviewChunkPlan(
        all_units,
        chunks,
        _plan_hash(all_units, chunks, usable_tokens),
        usable_tokens,
        status,
        unplanned_unit_ids=unplanned_ids,
        unreviewable_files=tuple(unreviewable),
        error="large MR review exceeds max_chunks" if unplanned_ids else "",
    )


def coverage_for_results(
    plan: ReviewChunkPlan,
    successful_chunk_ids: Iterable[str],
    failed_chunk_ids: Iterable[str],
) -> ReviewCoverage:
    expected = tuple(unit.unit_id for unit in plan.units)
    successful = tuple(successful_chunk_ids)
    failed = tuple(dict.fromkeys(failed_chunk_ids))
    success_set = set(successful)
    completed_all = [unit_id for chunk in plan.chunks if chunk.chunk_id in success_set for unit_id in chunk.unit_ids]
    counts: dict[str, int] = {}
    for unit_id in completed_all:
        counts[unit_id] = counts.get(unit_id, 0) + 1
    completed = tuple(unit_id for unit_id in expected if counts.get(unit_id, 0) > 0)
    missing = tuple(unit_id for unit_id in expected if counts.get(unit_id, 0) == 0)
    duplicates = tuple(unit_id for unit_id in expected if counts.get(unit_id, 0) > 1)
    if plan.is_complete_plan and not missing and not duplicates and not failed:
        status = "complete"
    elif completed:
        status = "partial"
    else:
        status = "failed"
    ratio = len(completed) / len(expected) if expected else 0.0
    return ReviewCoverage(status, expected, completed, missing, duplicates, failed, ratio)
