import asyncio

from pr_agent.distributed.broker import LostLeaseError, MrLease, RedisBroker
from pr_agent.distributed.models import PipelineEvent, TaskEnvelope
from pr_agent.distributed.runtime import TaskSuspended
from pr_agent.log import get_logger


def is_triage_task(task: TaskEnvelope) -> bool:
    return bool(task.command) and task.command.split()[0].lower() in {
        "/triage",
        "/fix-format",
        "/fix_format",
        "/repair-pipeline",
    }


class MrSession:
    def __init__(self, lease: MrLease, executor, broker=None) -> None:
        self.lease = lease
        self.executor = executor
        self.broker = broker or getattr(executor, "broker", None)
        self._condition = asyncio.Condition()
        self._normal_running = 0
        self._exclusive_task_id: str | None = None
        self._triage_waiting_task_id: str | None = None

    async def run(self, task: TaskEnvelope) -> None:
        triage = is_triage_task(task)
        if triage:
            await self._begin_triage(task.task_id, "initial")
        else:
            await self._begin_normal(task.task_id, "initial")
        keep_exclusive = False
        try:
            await self.executor.execute(task, self.lease)
        except TaskSuspended:
            keep_exclusive = triage
            raise
        finally:
            if triage and not keep_exclusive:
                await self._end_triage(task.task_id)
            elif not triage:
                await self._end_normal()

    async def resume_pipeline(self, task: TaskEnvelope, event: PipelineEvent) -> None:
        triage = is_triage_task(task)
        segment_id = f"resume:{event.pipeline_id}"
        if triage:
            await self._begin_triage_resume(task.task_id, segment_id)
        else:
            await self._begin_normal(task.task_id, segment_id)
        keep_exclusive = False
        try:
            await self.executor.resume_pipeline(task, self.lease, event)
        except TaskSuspended:
            keep_exclusive = triage
            raise
        finally:
            if triage and not keep_exclusive:
                await self._end_triage(task.task_id)
            elif not triage:
                await self._end_normal()

    async def _record_wait(self, task_id: str, kind: str, segment_id: str) -> None:
        from pr_agent.distributed.lifecycle import LifecycleEvent

        recorder = getattr(self.broker, "record_lifecycle_event", None)
        if not callable(recorder):
            return
        await recorder(
            LifecycleEvent.new(task_id, "same_mr_wait", kind, segment_id=segment_id)
        )

    async def _begin_normal(self, task_id: str, segment_id: str) -> None:
        await self._record_wait(task_id, "start", segment_id)
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._exclusive_task_id is None and self._triage_waiting_task_id is None
            )
            self._normal_running += 1
        await self._record_wait(task_id, "end", segment_id)

    async def _end_normal(self) -> None:
        async with self._condition:
            self._normal_running -= 1
            self._condition.notify_all()

    async def _begin_triage(self, task_id: str, segment_id: str) -> None:
        await self._record_wait(task_id, "start", segment_id)
        async with self._condition:
            if self._triage_waiting_task_id not in {None, task_id}:
                raise ValueError(f"another triage task is already waiting: {self._triage_waiting_task_id}")
            self._triage_waiting_task_id = task_id
            try:
                await self._condition.wait_for(
                    lambda: self._exclusive_task_id == task_id
                    or (self._exclusive_task_id is None and self._normal_running == 0)
                )
            except BaseException:
                if self._triage_waiting_task_id == task_id:
                    self._triage_waiting_task_id = None
                    self._condition.notify_all()
                raise
            self._exclusive_task_id = task_id
            self._triage_waiting_task_id = None
        await self._record_wait(task_id, "end", segment_id)

    async def _end_triage(self, task_id: str) -> None:
        async with self._condition:
            if self._exclusive_task_id == task_id:
                self._exclusive_task_id = None
                self._condition.notify_all()
            if self._triage_waiting_task_id == task_id:
                self._triage_waiting_task_id = None
                self._condition.notify_all()

    async def _begin_triage_resume(self, task_id: str, segment_id: str) -> None:
        async with self._condition:
            if self._exclusive_task_id is not None and self._exclusive_task_id != task_id:
                raise ValueError(f"pipeline resume task does not own MR exclusivity: {task_id}")
        await self._begin_triage(task_id, segment_id)


class MrSessionManager:
    def __init__(self, broker: RedisBroker, executor, worker_id: str, *, lease_seconds: int) -> None:
        self.broker = broker
        self.executor = executor
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self._sessions: dict[str, MrSession] = {}
        self._sessions_lock = asyncio.Lock()

    @property
    def owned_mr_count(self) -> int:
        return len(self._sessions)

    async def submit(self, task: TaskEnvelope) -> None:
        session = await self._session_for(task)
        if session is None:
            await self.executor.execute(task, None)
        else:
            await session.run(task)

    async def resume_pipeline(self, task: TaskEnvelope, event: PipelineEvent) -> None:
        session = await self._session_for(task)
        if session is None:
            await self.executor.resume_pipeline(task, None, event)
        else:
            await session.resume_pipeline(task, event)

    async def resume_auto(self, task: TaskEnvelope) -> None:
        await self.submit(task)

    async def _session_for(self, task: TaskEnvelope) -> MrSession | None:
        if task.mr is None:
            return None
        stored_task = await self.broker.get_task(task.task_id)
        if stored_task is None or stored_task.worker_id != self.worker_id or stored_task.fencing_token is None:
            raise LostLeaseError(task.task_id)
        lease = MrLease(task.mr, self.worker_id, stored_task.fencing_token)
        async with self._sessions_lock:
            session = self._sessions.get(task.mr.redis_id)
            if session is None:
                session = MrSession(lease, self.executor, self.broker)
                self._sessions[task.mr.redis_id] = session
            elif session.lease.fencing_token != lease.fencing_token:
                session.lease = lease
            return session

    async def renew_leases(self) -> None:
        lost_sessions: list[str] = []
        for redis_id, session in list(self._sessions.items()):
            renewed = await self.broker.renew_mr(
                session.lease.mr,
                session.lease.worker_id,
                session.lease.fencing_token,
                self.lease_seconds,
            )
            if not renewed:
                lost_sessions.append(redis_id)
                async with self._sessions_lock:
                    if self._sessions.get(redis_id) is session:
                        self._sessions.pop(redis_id, None)
        if lost_sessions:
            raise LostLeaseError(",".join(lost_sessions))

    async def fallback_pipeline_scan(self) -> None:
        tasks = await self.broker.list_stale_pipeline_waits(
            age_seconds=self.broker.settings.pipeline_fallback_scan_seconds,
            limit=32,
        )
        for task in tasks:
            try:
                event = await asyncio.to_thread(self._fetch_terminal_pipeline, task)
                if event is not None:
                    await self.broker.publish_pipeline_event(event)
                else:
                    await self.broker.defer_pipeline_fallback(task.task_id)
            except Exception:
                get_logger().exception(f"Pipeline fallback scan failed: task_id={task.task_id}")
                await self.broker.defer_pipeline_fallback(task.task_id)

    @staticmethod
    def _fetch_terminal_pipeline(task) -> PipelineEvent | None:
        from pr_agent.git_providers.gitlab_provider import GitLabProvider

        provider = GitLabProvider(task.envelope.pr_url)
        project = provider.gl.projects.get(task.pipeline_project_id)
        if task.pipeline_id is not None:
            pipeline = project.pipelines.get(task.pipeline_id)
        else:
            pipelines = project.pipelines.list(sha=task.pipeline_sha, order_by="id", sort="desc", per_page=1)
            if not pipelines:
                return None
            pipeline = project.pipelines.get(pipelines[0].id)
        if str(pipeline.status) not in {"success", "failed", "canceled", "skipped"}:
            return None
        return PipelineEvent.new(
            project_id=task.pipeline_project_id,
            pipeline_id=int(pipeline.id),
            sha=str(pipeline.sha),
            status=str(pipeline.status),
            ref=str(getattr(pipeline, "ref", "") or ""),
            source=str(getattr(pipeline, "source", "") or ""),
        )
