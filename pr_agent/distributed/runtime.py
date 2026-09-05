from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from pr_agent.distributed.broker import MrLease, RedisBroker, RepairManifestConflict, SyncRedisBroker
from pr_agent.distributed.models import PipelineEvent
from pr_agent.triage.repair_rollback import RepairCommitEntry, RepairCommitManifest


class TaskSuspended(RuntimeError):
    def __init__(self, task_id: str, wait_kind: str, wait_identity: str):
        super().__init__(f"task {task_id} suspended for {wait_kind}: {wait_identity}")
        self.task_id = task_id
        self.wait_kind = wait_kind
        self.wait_identity = wait_identity


class TaskCanceled(RuntimeError):
    def __init__(self, task_id: str):
        super().__init__(f"task {task_id} was canceled")
        self.task_id = task_id


def resolve_repair_manifest_base_tree(
    manifest: RepairCommitManifest | None,
    entry: RepairCommitEntry,
    parent_tree_sha: str,
) -> str:
    """Validate an entry's parent and return the manifest's immutable base tree."""
    if manifest is None:
        if entry.sequence != 1:
            raise RepairManifestConflict("commit sequence is not continuous")
        return parent_tree_sha
    if entry.sequence < 1 or entry.sequence > len(manifest.entries) + 1:
        raise RepairManifestConflict("commit sequence is not continuous")
    expected_parent_sha = manifest.base_commit_sha
    expected_parent_tree = manifest.base_tree_sha
    if entry.sequence > 1:
        previous = manifest.entries[entry.sequence - 2]
        expected_parent_sha = previous.commit_sha
        expected_parent_tree = previous.tree_sha
    if entry.parent_sha != expected_parent_sha:
        raise RepairManifestConflict("commit parent does not match")
    if parent_tree_sha != expected_parent_tree:
        raise RepairManifestConflict("commit parent tree does not match")
    return manifest.base_tree_sha


def reconcile_pushed_repair_commit(
    manifest: RepairCommitManifest | None,
    entry: RepairCommitEntry,
    *,
    expected_parent_sha: str,
    authoritative_parent_tree_sha: str,
    branch_head_sha: str,
) -> str:
    """Validate post-push GitLab facts and return the manifest's immutable base tree."""
    if entry.parent_sha != expected_parent_sha:
        raise RepairManifestConflict("pushed commit expected parent does not match")
    if branch_head_sha != entry.commit_sha:
        raise RepairManifestConflict("pushed commit branch head does not match")
    return resolve_repair_manifest_base_tree(manifest, entry, authoritative_parent_tree_sha)


@dataclass(frozen=True)
class ExecutionRuntime:
    task_id: str
    worker_id: str
    lease: MrLease | None
    mode: str
    broker: RedisBroker
    sync_broker: SyncRedisBroker
    checkpointer: Any = None
    pipeline_event: PipelineEvent | None = None

    def register_pipeline_wait_sync(
        self,
        project_id: str,
        commit_sha: str,
        attempt_id: str = "",
        pipeline_id: int | None = None,
    ) -> PipelineEvent | None:
        self.raise_if_canceled()
        from pr_agent.distributed.lifecycle import pipeline_wait_segment

        segment_id = pipeline_wait_segment(attempt_id, commit_sha, pipeline_id)
        self.record_lifecycle_sync("pipeline_wait", "start", segment_id=segment_id)
        self.record_repair_progress_sync(
            "waiting_pipeline",
            "修复提交已推送，正在等待新流水线",
            metadata={"commit_sha": commit_sha, "pipeline_id": pipeline_id or 0},
        )
        event = self.sync_broker.register_pipeline_wait(
            self.task_id,
            project_id,
            commit_sha,
            attempt_id=attempt_id,
            pipeline_id=pipeline_id,
        )
        if event is not None and event.terminal:
            self.record_lifecycle_sync("pipeline_wait", "end", segment_id=segment_id)
        return event

    def assert_fence_sync(self) -> None:
        if self.lease is not None:
            self.sync_broker.assert_fence(self.lease)

    def record_repair_commit_sync(
        self,
        entry: RepairCommitEntry,
        *,
        parent_tree_sha: str,
        source_branch: str,
    ) -> RepairCommitManifest:
        """Record one remotely confirmed repair commit before execution may continue."""
        self.assert_fence_sync()
        binding = self.sync_broker.get_task_triage_card(self.task_id)
        if binding is None or not binding.receive_id:
            raise RepairManifestConflict("repair card owner is unavailable")
        getter = getattr(self.sync_broker, "get_repair_commit_manifest", None)
        manifest = getter(self.task_id) if callable(getter) else None
        base_tree_sha = resolve_repair_manifest_base_tree(manifest, entry, parent_tree_sha)
        return self.sync_broker.append_repair_commit(
            self.task_id,
            entry,
            base_tree_sha=base_tree_sha,
            source_branch=source_branch,
            authorized_actor_id=binding.receive_id,
            lease=self.lease,
        )

    def record_reconciled_repair_commit_sync(
        self,
        entry: RepairCommitEntry,
        *,
        expected_parent_sha: str,
        authoritative_parent_tree_sha: str,
        branch_head_sha: str,
        source_branch: str,
    ) -> RepairCommitManifest:
        """Atomically append a commit after validating authoritative post-push GitLab facts."""
        self.assert_fence_sync()
        binding = self.sync_broker.get_task_triage_card(self.task_id)
        if binding is None or not binding.receive_id:
            raise RepairManifestConflict("repair card owner is unavailable")
        getter = getattr(self.sync_broker, "get_repair_commit_manifest", None)
        manifest = getter(self.task_id) if callable(getter) else None
        base_tree_sha = reconcile_pushed_repair_commit(
            manifest,
            entry,
            expected_parent_sha=expected_parent_sha,
            authoritative_parent_tree_sha=authoritative_parent_tree_sha,
            branch_head_sha=branch_head_sha,
        )
        return self.sync_broker.append_repair_commit(
            self.task_id,
            entry,
            base_tree_sha=base_tree_sha,
            source_branch=source_branch,
            authorized_actor_id=binding.receive_id,
            lease=self.lease,
        )

    def next_repair_commit_sequence_sync(self) -> int:
        getter = getattr(self.sync_broker, "get_repair_commit_manifest", None)
        manifest = getter(self.task_id) if callable(getter) else None
        return len(manifest.entries) + 1 if manifest is not None else 1

    def raise_if_canceled(self) -> None:
        checker = getattr(self.sync_broker, "is_cancel_requested", None)
        if callable(checker) and checker(self.task_id) is True:
            raise TaskCanceled(self.task_id)

    async def raise_if_canceled_async(self) -> None:
        checker = getattr(self.broker, "is_cancel_requested", None)
        if callable(checker) and await checker(self.task_id) is True:
            raise TaskCanceled(self.task_id)

    def record_lifecycle_sync(
        self,
        phase: str,
        kind: str,
        *,
        segment_id: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        from pr_agent.distributed.lifecycle import LifecycleEvent

        recorder = getattr(self.sync_broker, "record_lifecycle_event", None)
        if not callable(recorder):
            return False
        return recorder(
            LifecycleEvent.new(
                self.task_id,
                phase,
                kind,
                segment_id=segment_id,
                metadata=metadata,
            )
        )

    def record_repair_progress_sync(
        self,
        phase: str,
        summary: str,
        *,
        categories: tuple[str, ...] = (),
        job_names: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Publish owner-visible progress without making Redis telemetry part of repair correctness."""
        from pr_agent.triage.repair_details import RepairProgressEvent

        recorder = getattr(self.sync_broker, "append_repair_progress", None)
        if not callable(recorder):
            return False
        try:
            recorder(
                RepairProgressEvent.new(
                    self.task_id,
                    phase,
                    summary,
                    categories=categories,
                    job_names=job_names,
                    metadata=metadata,
                )
            )
            return True
        except Exception:
            return False

    def lifecycle_summary_sync(self):
        from pr_agent.distributed.lifecycle import summarize_lifecycle

        getter = getattr(self.sync_broker, "get_lifecycle_events", None)
        return summarize_lifecycle(getter(self.task_id) if callable(getter) else [])


_CURRENT_RUNTIME: ContextVar[ExecutionRuntime | None] = ContextVar("pr_agent_execution_runtime", default=None)


def get_execution_runtime(*, required: bool = False) -> ExecutionRuntime | None:
    runtime = _CURRENT_RUNTIME.get()
    if required and runtime is None:
        raise RuntimeError("distributed execution runtime is not active")
    return runtime


@contextmanager
def execution_context(runtime: ExecutionRuntime) -> Iterator[ExecutionRuntime]:
    token = _CURRENT_RUNTIME.set(runtime)
    try:
        yield runtime
    finally:
        _CURRENT_RUNTIME.reset(token)
