"""Immutable records and safety policy for reverting one CI repair task."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from pr_agent.config_loader import get_settings

_SCHEMA_VERSION = 1
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class RepairRollbackStatus(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    REVERTING = "reverting"
    COMMITTING = "committing"
    PUSHING = "pushing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RollbackFailureCode(StrEnum):
    UNAUTHORIZED_ACTOR = "unauthorized_actor"
    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_INCOMPLETE = "manifest_incomplete"
    COMMIT_CHAIN_MISMATCH = "commit_chain_mismatch"
    REMOTE_HEAD_CHANGED = "remote_head_changed"
    MR_NOT_OPEN = "mr_not_open"
    SOURCE_BRANCH_CHANGED = "source_branch_changed"
    SOURCE_BRANCH_MISSING = "source_branch_missing"
    ALREADY_ROLLED_BACK = "already_rolled_back"
    REVERT_CONFLICT = "revert_conflict"
    TREE_MISMATCH = "tree_mismatch"
    ROLLBACK_COMMIT_INVALID = "rollback_commit_invalid"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


@dataclass(frozen=True)
class ManifestValidation:
    ok: bool
    failure_code: RollbackFailureCode | None = None
    message: str = ""


@dataclass(frozen=True)
class RepairCommitEntry:
    sequence: int
    commit_sha: str
    parent_sha: str
    tree_sha: str
    effect_id: str
    task_marker: str
    pushed_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairCommitEntry":
        return cls(
            sequence=int(value["sequence"]),
            commit_sha=str(value["commit_sha"]),
            parent_sha=str(value["parent_sha"]),
            tree_sha=str(value["tree_sha"]),
            effect_id=str(value["effect_id"]),
            task_marker=str(value["task_marker"]),
            pushed_at=str(value["pushed_at"]),
        )


@dataclass(frozen=True)
class RepairCommitManifest:
    repair_task_id: str
    project_id: str
    mr_iid: int
    source_branch: str
    base_commit_sha: str
    base_tree_sha: str
    authorized_actor_id: str
    entries: tuple[RepairCommitEntry, ...] = ()
    frozen: bool = False
    frozen_at: str = ""
    schema_version: int = _SCHEMA_VERSION

    @property
    def final_repair_sha(self) -> str:
        return self.entries[-1].commit_sha if self.entries else ""

    def validate_static(self) -> ManifestValidation:
        if self.schema_version != _SCHEMA_VERSION:
            return _invalid(RollbackFailureCode.MANIFEST_INCOMPLETE, "unsupported manifest schema")
        if not all((self.repair_task_id, self.project_id, self.source_branch, self.authorized_actor_id)):
            return _invalid(RollbackFailureCode.MANIFEST_INCOMPLETE, "manifest identity is incomplete")
        if self.mr_iid <= 0 or not _valid_branch(self.source_branch):
            return _invalid(RollbackFailureCode.MANIFEST_INCOMPLETE, "MR identity or source branch is invalid")
        if not _valid_sha(self.base_commit_sha) or not _valid_sha(self.base_tree_sha):
            return _invalid(RollbackFailureCode.MANIFEST_INCOMPLETE, "base commit or tree is invalid")
        if not self.frozen or not self.frozen_at or not self.entries:
            return _invalid(RollbackFailureCode.MANIFEST_INCOMPLETE, "manifest is not frozen and complete")
        seen_shas: set[str] = set()
        expected_parent = self.base_commit_sha
        for expected_sequence, entry in enumerate(self.entries, start=1):
            if entry.sequence != expected_sequence or entry.commit_sha in seen_shas:
                return _invalid(
                    RollbackFailureCode.COMMIT_CHAIN_MISMATCH,
                    "commit sequence is not unique and continuous",
                )
            if not all((_valid_sha(entry.commit_sha), _valid_sha(entry.parent_sha), _valid_sha(entry.tree_sha))):
                return _invalid(RollbackFailureCode.MANIFEST_INCOMPLETE, "commit identity is invalid")
            if not all((entry.effect_id, entry.task_marker, entry.pushed_at)):
                return _invalid(RollbackFailureCode.MANIFEST_INCOMPLETE, "commit evidence is incomplete")
            if entry.parent_sha != expected_parent:
                return _invalid(RollbackFailureCode.COMMIT_CHAIN_MISMATCH, "commit parent chain does not match")
            seen_shas.add(entry.commit_sha)
            expected_parent = entry.commit_sha
        return ManifestValidation(ok=True)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["entries"] = [entry.to_dict() for entry in self.entries]
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairCommitManifest":
        return cls(
            schema_version=int(value.get("schema_version", _SCHEMA_VERSION)),
            repair_task_id=str(value["repair_task_id"]),
            project_id=str(value["project_id"]),
            mr_iid=int(value["mr_iid"]),
            source_branch=str(value["source_branch"]),
            base_commit_sha=str(value["base_commit_sha"]),
            base_tree_sha=str(value["base_tree_sha"]),
            authorized_actor_id=str(value["authorized_actor_id"]),
            entries=tuple(RepairCommitEntry.from_dict(entry) for entry in value.get("entries") or ()),
            frozen=bool(value.get("frozen", False)),
            frozen_at=str(value.get("frozen_at") or ""),
        )

    @classmethod
    def from_json(cls, value: str) -> "RepairCommitManifest":
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("repair commit manifest must be a JSON object")
        return cls.from_dict(decoded)


@dataclass(frozen=True)
class RepairRollbackState:
    rollback_task_id: str
    repair_task_id: str
    status: RepairRollbackStatus
    trigger: str
    requested_by: str
    expected_remote_head: str
    manifest_digest: str
    rollback_commit_sha: str = ""
    failure_code: RollbackFailureCode | None = None
    failure_message: str = ""
    retryable: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        value["failure_code"] = self.failure_code.value if self.failure_code else ""
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepairRollbackState":
        failure_code = str(value.get("failure_code") or "")
        return cls(
            rollback_task_id=str(value["rollback_task_id"]),
            repair_task_id=str(value["repair_task_id"]),
            status=RepairRollbackStatus(value["status"]),
            trigger=str(value.get("trigger") or ""),
            requested_by=str(value.get("requested_by") or ""),
            expected_remote_head=str(value.get("expected_remote_head") or ""),
            manifest_digest=str(value.get("manifest_digest") or ""),
            rollback_commit_sha=str(value.get("rollback_commit_sha") or ""),
            failure_code=RollbackFailureCode(failure_code) if failure_code else None,
            failure_message=str(value.get("failure_message") or ""),
            retryable=bool(value.get("retryable", False)),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
        )

    @classmethod
    def from_json(cls, value: str) -> "RepairRollbackState":
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("repair rollback state must be a JSON object")
        return cls.from_dict(decoded)


def _valid_sha(value: str) -> bool:
    return bool(_SHA_RE.fullmatch(value))


def _valid_branch(value: str) -> bool:
    return bool(
        value
        and value == value.strip()
        and not value.startswith(("/", "refs/"))
        and not value.endswith(("/", "."))
        and "//" not in value
        and ".." not in value
        and "@{" not in value
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _invalid(code: RollbackFailureCode, message: str) -> ManifestValidation:
    return ManifestValidation(ok=False, failure_code=code, message=message)


def _enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return str(value or "").strip().lower() in {"true", "1", "yes", "on"}


def _flag(env_name: str, setting_name: str, default: bool) -> bool:
    env_value = os.getenv(env_name)
    value = env_value if env_value is not None else get_settings().get(setting_name, default)
    return _enabled(value)


def repair_rollback_enabled() -> bool:
    return _flag("PR_AGENT_REPAIR_ROLLBACK_ENABLED", "REPAIR_ROLLBACK.ENABLED", False)


def cancel_reverts_pushed_commits() -> bool:
    return _flag(
        "PR_AGENT_CANCEL_REVERTS_PUSHED_COMMITS",
        "REPAIR_ROLLBACK.CANCEL_REVERTS_PUSHED_COMMITS",
        True,
    )


def rollback_success_notification_enabled() -> bool:
    return _flag(
        "PR_AGENT_ROLLBACK_NOTIFY_ON_SUCCESS",
        "REPAIR_ROLLBACK.NOTIFY_ON_SUCCESS",
        True,
    )
