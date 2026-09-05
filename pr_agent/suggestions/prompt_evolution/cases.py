"""Strict, deduplicated failure cases for Prompt and Project Skill evolution."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvolutionCaseKind(StrEnum):
    FALSE_POSITIVE = "false_positive"
    FALSE_NEGATIVE = "false_negative"
    BAD_FIX = "bad_fix"
    OUTPUT_SCHEMA_ERROR = "output_schema_error"
    PARSER_ERROR = "parser_error"
    INCOMPLETE_COVERAGE = "incomplete_coverage"


ATTRIBUTABLE_ERROR_CODES = {
    "output_schema_error": EvolutionCaseKind.OUTPUT_SCHEMA_ERROR,
    "schema_validation_error": EvolutionCaseKind.OUTPUT_SCHEMA_ERROR,
    "tool_schema_error": EvolutionCaseKind.OUTPUT_SCHEMA_ERROR,
    "parser_error": EvolutionCaseKind.PARSER_ERROR,
    "yaml_parse_error": EvolutionCaseKind.PARSER_ERROR,
    "incomplete_coverage": EvolutionCaseKind.INCOMPLETE_COVERAGE,
}


class EvolutionCase(BaseModel):
    """One reproducible expected behavior tied to an immutable review identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    kind: EvolutionCaseKind
    project: str = Field(min_length=1, max_length=300)
    mr_iid: str = Field(min_length=1, max_length=80)
    review_id: str = Field(min_length=1, max_length=128)
    head_sha: str = Field(pattern=r"(?i)^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    command: Literal["review", "improve"]
    description: str = Field(min_length=1, max_length=2_000)
    expected_action: Literal["emit", "suppress", "revise"]
    source: Literal["manual", "automatic"]
    file_path: str = Field(default="", max_length=500)
    line_start: int = Field(default=0, ge=0, le=10_000_000)
    line_end: int = Field(default=0, ge=0, le=10_000_000)
    suggestion_id: str = Field(default="", max_length=200)
    error_code: str = Field(default="", max_length=100)
    global_prompt_set_hash: str = Field(default="", max_length=128)
    prompt_bundle_hash: str = Field(default="", max_length=128)
    project_skill_hash: str = Field(default="", max_length=128)
    created_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def _validate_identity_and_kind(self):
        if self.file_path:
            path = PurePosixPath(self.file_path.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
                raise ValueError("evolution case file_path must stay inside the repository")
        if self.line_end and (not self.line_start or self.line_end < self.line_start):
            raise ValueError("evolution case line range is invalid")
        if self.kind is EvolutionCaseKind.FALSE_NEGATIVE and (not self.file_path or self.line_start <= 0):
            raise ValueError("false_negative requires file_path and line_start")
        if self.kind is EvolutionCaseKind.BAD_FIX and not self.suggestion_id:
            raise ValueError("bad_fix requires suggestion_id")
        if self.kind is EvolutionCaseKind.FALSE_POSITIVE and not self.suggestion_id:
            raise ValueError("false_positive requires suggestion_id")
        execution_kinds = {
            EvolutionCaseKind.OUTPUT_SCHEMA_ERROR,
            EvolutionCaseKind.PARSER_ERROR,
            EvolutionCaseKind.INCOMPLETE_COVERAGE,
        }
        if self.kind in execution_kinds:
            mapped = ATTRIBUTABLE_ERROR_CODES.get(self.error_code)
            if self.source != "automatic" or mapped is not self.kind:
                raise ValueError("execution case requires an attributable allowlisted error_code")
        elif self.error_code:
            raise ValueError("human feedback cases cannot carry an execution error_code")
        return self

    @property
    def case_hash(self) -> str:
        payload = {
            "kind": self.kind.value,
            "project": self.project.casefold(),
            "mr_iid": self.mr_iid,
            "head_sha": self.head_sha.casefold(),
            "command": self.command,
            "file_path": self.file_path.casefold(),
            "line_start": self.line_start,
            "line_end": self.line_end,
            "suggestion_id": self.suggestion_id,
            "description": " ".join(self.description.casefold().split()),
            "error_code": self.error_code,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def case_id(self) -> str:
        return f"case:{self.case_hash[:24]}"


def expected_action_for(kind: EvolutionCaseKind) -> str:
    if kind is EvolutionCaseKind.FALSE_NEGATIVE:
        return "emit"
    if kind is EvolutionCaseKind.BAD_FIX:
        return "revise"
    return "suppress"


def build_evolution_case(record: dict) -> EvolutionCase:
    values = dict(record)
    kind = EvolutionCaseKind(str(values.get("kind") or ""))
    values["kind"] = kind
    values.setdefault("expected_action", expected_action_for(kind))
    return EvolutionCase.model_validate(values)


def attributable_case_kind(error_code: str) -> EvolutionCaseKind | None:
    return ATTRIBUTABLE_ERROR_CODES.get(str(error_code or "").strip().casefold())
