import base64
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any
from urllib.parse import quote

import redis
import redis.asyncio as async_redis
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)


def _encode(serde, value: Any) -> str:
    kind, data = serde.dumps_typed(value)
    return json.dumps(
        {"kind": kind, "data": base64.b64encode(data).decode("ascii")},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _decode(serde, payload: str) -> Any:
    raw = json.loads(payload)
    return serde.loads_typed((raw["kind"], base64.b64decode(raw["data"])))


_EMPTY_BLOB = json.dumps({"kind": "empty", "data": ""}, separators=(",", ":"))


class CoreRedisCheckpointSaver(BaseCheckpointSaver[int]):
    """LangGraph checkpoint saver backed only by Redis core commands."""

    def __init__(
        self,
        sync_client: redis.Redis,
        async_client: async_redis.Redis,
        *,
        prefix: str = "pr-agent:checkpoint",
        serde=None,
    ) -> None:
        super().__init__(serde=serde)
        self.sync_client = sync_client
        self.async_client = async_client
        self.prefix = prefix

    @classmethod
    def from_url(cls, redis_url: str, *, prefix: str = "pr-agent:checkpoint") -> "CoreRedisCheckpointSaver":
        options = {
            "decode_responses": True,
            "protocol": 2,
            "socket_connect_timeout": 2,
            "socket_timeout": 5,
            "health_check_interval": 10,
        }
        return cls(
            redis.Redis.from_url(redis_url, **options),
            async_redis.Redis.from_url(redis_url, **options),
            prefix=prefix,
        )

    @staticmethod
    def _escape(value: Any) -> str:
        return quote(str(value), safe="")

    @property
    def _threads_key(self) -> str:
        return f"{self.prefix}:threads"

    def _namespaces_key(self, thread_id: str) -> str:
        return f"{self.prefix}:{self._escape(thread_id)}:namespaces"

    def _base(self, thread_id: str, namespace: str) -> str:
        return f"{self.prefix}:{self._escape(thread_id)}:{self._escape(namespace)}"

    def _index_key(self, thread_id: str, namespace: str) -> str:
        return f"{self._base(thread_id, namespace)}:index"

    def _sequence_key(self, thread_id: str, namespace: str) -> str:
        return f"{self._base(thread_id, namespace)}:sequence"

    def _checkpoint_key(self, thread_id: str, namespace: str, checkpoint_id: str) -> str:
        return f"{self._base(thread_id, namespace)}:{self._escape(checkpoint_id)}"

    def _writes_key(self, thread_id: str, namespace: str, checkpoint_id: str) -> str:
        return f"{self._checkpoint_key(thread_id, namespace, checkpoint_id)}:writes"

    def _blob_key(self, thread_id: str, namespace: str, channel: str, version: Any) -> str:
        return f"{self._base(thread_id, namespace)}:blob:{self._escape(channel)}:{self._escape(version)}"

    @staticmethod
    def _config_parts(config) -> tuple[str, str, str | None]:
        configurable = config["configurable"]
        return (
            str(configurable["thread_id"]),
            str(configurable.get("checkpoint_ns", "")),
            get_checkpoint_id(config),
        )

    @staticmethod
    def _result_config(thread_id: str, namespace: str, checkpoint_id: str) -> dict:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put(self, config, checkpoint, metadata, new_versions):
        thread_id, namespace, parent_id = self._config_parts(config)
        checkpoint_id = str(checkpoint["id"])
        checkpoint_copy = checkpoint.copy()
        values = checkpoint_copy.pop("channel_values", {})
        sequence = self.sync_client.incr(self._sequence_key(thread_id, namespace))
        with self.sync_client.pipeline(transaction=True) as pipeline:
            for channel, version in new_versions.items():
                payload = _encode(self.serde, values[channel]) if channel in values else _EMPTY_BLOB
                pipeline.set(self._blob_key(thread_id, namespace, channel, version), payload)
            pipeline.hset(
                self._checkpoint_key(thread_id, namespace, checkpoint_id),
                mapping={
                    "checkpoint": _encode(self.serde, checkpoint_copy),
                    "metadata": _encode(self.serde, get_checkpoint_metadata(config, metadata)),
                    "parent": parent_id or "",
                },
            )
            pipeline.zadd(self._index_key(thread_id, namespace), {checkpoint_id: sequence})
            pipeline.sadd(self._threads_key, thread_id)
            pipeline.sadd(self._namespaces_key(thread_id), namespace)
            pipeline.execute()
        return self._result_config(thread_id, namespace, checkpoint_id)

    async def aput(self, config, checkpoint, metadata, new_versions):
        thread_id, namespace, parent_id = self._config_parts(config)
        checkpoint_id = str(checkpoint["id"])
        checkpoint_copy = checkpoint.copy()
        values = checkpoint_copy.pop("channel_values", {})
        sequence = await self.async_client.incr(self._sequence_key(thread_id, namespace))
        async with self.async_client.pipeline(transaction=True) as pipeline:
            for channel, version in new_versions.items():
                payload = _encode(self.serde, values[channel]) if channel in values else _EMPTY_BLOB
                pipeline.set(self._blob_key(thread_id, namespace, channel, version), payload)
            pipeline.hset(
                self._checkpoint_key(thread_id, namespace, checkpoint_id),
                mapping={
                    "checkpoint": _encode(self.serde, checkpoint_copy),
                    "metadata": _encode(self.serde, get_checkpoint_metadata(config, metadata)),
                    "parent": parent_id or "",
                },
            )
            pipeline.zadd(self._index_key(thread_id, namespace), {checkpoint_id: sequence})
            pipeline.sadd(self._threads_key, thread_id)
            pipeline.sadd(self._namespaces_key(thread_id), namespace)
            await pipeline.execute()
        return self._result_config(thread_id, namespace, checkpoint_id)

    def get_tuple(self, config) -> CheckpointTuple | None:
        thread_id, namespace, checkpoint_id = self._config_parts(config)
        checkpoint_id = checkpoint_id or self._latest_id_sync(thread_id, namespace)
        if not checkpoint_id:
            return None
        value = self.sync_client.hgetall(self._checkpoint_key(thread_id, namespace, checkpoint_id))
        return self._build_tuple_sync(thread_id, namespace, checkpoint_id, value) if value else None

    async def aget_tuple(self, config) -> CheckpointTuple | None:
        thread_id, namespace, checkpoint_id = self._config_parts(config)
        checkpoint_id = checkpoint_id or await self._latest_id_async(thread_id, namespace)
        if not checkpoint_id:
            return None
        value = await self.async_client.hgetall(self._checkpoint_key(thread_id, namespace, checkpoint_id))
        return await self._build_tuple_async(thread_id, namespace, checkpoint_id, value) if value else None

    def _latest_id_sync(self, thread_id: str, namespace: str) -> str | None:
        values = self.sync_client.zrevrange(self._index_key(thread_id, namespace), 0, 0)
        return str(values[0]) if values else None

    async def _latest_id_async(self, thread_id: str, namespace: str) -> str | None:
        values = await self.async_client.zrevrange(self._index_key(thread_id, namespace), 0, 0)
        return str(values[0]) if values else None

    def _build_tuple_sync(self, thread_id: str, namespace: str, checkpoint_id: str, value: dict) -> CheckpointTuple:
        checkpoint = _decode(self.serde, value["checkpoint"])
        channel_values = {}
        for channel, version in checkpoint.get("channel_versions", {}).items():
            payload = self.sync_client.get(self._blob_key(thread_id, namespace, channel, version))
            if payload and payload != _EMPTY_BLOB:
                channel_values[channel] = _decode(self.serde, payload)
        return self._tuple_from_parts(
            thread_id,
            namespace,
            checkpoint_id,
            checkpoint,
            channel_values,
            _decode(self.serde, value["metadata"]),
            value.get("parent", ""),
            self._writes_sync(thread_id, namespace, checkpoint_id),
        )

    async def _build_tuple_async(
        self, thread_id: str, namespace: str, checkpoint_id: str, value: dict
    ) -> CheckpointTuple:
        checkpoint = _decode(self.serde, value["checkpoint"])
        channel_values = {}
        for channel, version in checkpoint.get("channel_versions", {}).items():
            payload = await self.async_client.get(self._blob_key(thread_id, namespace, channel, version))
            if payload and payload != _EMPTY_BLOB:
                channel_values[channel] = _decode(self.serde, payload)
        return self._tuple_from_parts(
            thread_id,
            namespace,
            checkpoint_id,
            checkpoint,
            channel_values,
            _decode(self.serde, value["metadata"]),
            value.get("parent", ""),
            await self._writes_async(thread_id, namespace, checkpoint_id),
        )

    def _tuple_from_parts(
        self,
        thread_id: str,
        namespace: str,
        checkpoint_id: str,
        checkpoint: dict,
        channel_values: dict,
        metadata: dict,
        parent_id: str,
        pending_writes: list,
    ) -> CheckpointTuple:
        return CheckpointTuple(
            config=self._result_config(thread_id, namespace, checkpoint_id),
            checkpoint={**checkpoint, "channel_values": channel_values},
            metadata=metadata,
            parent_config=self._result_config(thread_id, namespace, parent_id) if parent_id else None,
            pending_writes=pending_writes,
        )

    def put_writes(self, config, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        thread_id, namespace, checkpoint_id = self._config_parts(config)
        if not checkpoint_id:
            raise ValueError("checkpoint_id is required for writes")
        key = self._writes_key(thread_id, namespace, checkpoint_id)
        with self.sync_client.pipeline(transaction=True) as pipeline:
            for index, (channel, value) in enumerate(writes):
                write_index = WRITES_IDX_MAP.get(channel, index)
                field = f"{self._escape(task_id)}:{write_index}"
                payload = self._write_payload(task_id, channel, value, task_path, write_index)
                if write_index < 0:
                    pipeline.hset(key, field, payload)
                else:
                    pipeline.hsetnx(key, field, payload)
            pipeline.execute()

    async def aput_writes(
        self, config, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        thread_id, namespace, checkpoint_id = self._config_parts(config)
        if not checkpoint_id:
            raise ValueError("checkpoint_id is required for writes")
        key = self._writes_key(thread_id, namespace, checkpoint_id)
        async with self.async_client.pipeline(transaction=True) as pipeline:
            for index, (channel, value) in enumerate(writes):
                write_index = WRITES_IDX_MAP.get(channel, index)
                field = f"{self._escape(task_id)}:{write_index}"
                payload = self._write_payload(task_id, channel, value, task_path, write_index)
                if write_index < 0:
                    pipeline.hset(key, field, payload)
                else:
                    pipeline.hsetnx(key, field, payload)
            await pipeline.execute()

    def _write_payload(self, task_id: str, channel: str, value: Any, task_path: str, index: int) -> str:
        return json.dumps(
            {
                "task_id": task_id,
                "channel": channel,
                "value": _encode(self.serde, value),
                "task_path": task_path,
                "index": index,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _writes_sync(self, thread_id: str, namespace: str, checkpoint_id: str) -> list:
        values = self.sync_client.hvals(self._writes_key(thread_id, namespace, checkpoint_id))
        return self._decode_writes(values)

    async def _writes_async(self, thread_id: str, namespace: str, checkpoint_id: str) -> list:
        values = await self.async_client.hvals(self._writes_key(thread_id, namespace, checkpoint_id))
        return self._decode_writes(values)

    def _decode_writes(self, values: list[str]) -> list:
        decoded = [json.loads(value) for value in values]
        decoded.sort(key=lambda item: (item["task_path"], item["task_id"], item["index"]))
        return [(item["task_id"], item["channel"], _decode(self.serde, item["value"])) for item in decoded]

    def list(self, config, *, filter=None, before=None, limit=None) -> Iterator[CheckpointTuple]:
        for thread_id, namespace, checkpoint_id in self._list_ids_sync(config, before):
            item = self.get_tuple(self._result_config(thread_id, namespace, checkpoint_id))
            if item is None or (filter and not all(item.metadata.get(key) == value for key, value in filter.items())):
                continue
            yield item
            if limit is not None:
                limit -= 1
                if limit <= 0:
                    return

    async def alist(self, config, *, filter=None, before=None, limit=None) -> AsyncIterator[CheckpointTuple]:
        async for thread_id, namespace, checkpoint_id in self._list_ids_async(config, before):
            item = await self.aget_tuple(self._result_config(thread_id, namespace, checkpoint_id))
            if item is None or (filter and not all(item.metadata.get(key) == value for key, value in filter.items())):
                continue
            yield item
            if limit is not None:
                limit -= 1
                if limit <= 0:
                    return

    def _list_ids_sync(self, config, before) -> Iterator[tuple[str, str, str]]:
        thread_ids = [self._config_parts(config)[0]] if config else list(self.sync_client.smembers(self._threads_key))
        configured_ns = self._config_parts(config)[1] if config else None
        configured_id = self._config_parts(config)[2] if config else None
        before_id = get_checkpoint_id(before) if before else None
        for thread_id in thread_ids:
            namespaces = [configured_ns] if configured_ns is not None else self.sync_client.smembers(
                self._namespaces_key(thread_id)
            )
            for namespace in namespaces:
                ids = self.sync_client.zrevrange(self._index_key(thread_id, str(namespace)), 0, -1)
                for checkpoint_id in ids:
                    checkpoint_id = str(checkpoint_id)
                    if configured_id and checkpoint_id != configured_id:
                        continue
                    if before_id and checkpoint_id >= before_id:
                        continue
                    yield str(thread_id), str(namespace), checkpoint_id

    async def _list_ids_async(self, config, before) -> AsyncIterator[tuple[str, str, str]]:
        thread_ids = (
            [self._config_parts(config)[0]]
            if config
            else list(await self.async_client.smembers(self._threads_key))
        )
        configured_ns = self._config_parts(config)[1] if config else None
        configured_id = self._config_parts(config)[2] if config else None
        before_id = get_checkpoint_id(before) if before else None
        for thread_id in thread_ids:
            namespaces = (
                [configured_ns]
                if configured_ns is not None
                else await self.async_client.smembers(self._namespaces_key(str(thread_id)))
            )
            for namespace in namespaces:
                ids = await self.async_client.zrevrange(self._index_key(str(thread_id), str(namespace)), 0, -1)
                for checkpoint_id in ids:
                    checkpoint_id = str(checkpoint_id)
                    if configured_id and checkpoint_id != configured_id:
                        continue
                    if before_id and checkpoint_id >= before_id:
                        continue
                    yield str(thread_id), str(namespace), checkpoint_id

    def delete_thread(self, thread_id: str) -> None:
        self._delete_thread_sync(thread_id)

    async def adelete_thread(self, thread_id: str) -> None:
        await self._delete_thread_async(thread_id)

    def _delete_thread_sync(self, thread_id: str) -> None:
        pattern = f"{self.prefix}:{self._escape(thread_id)}:*"
        keys = list(self.sync_client.scan_iter(match=pattern))
        if keys:
            self.sync_client.delete(*keys)
        self.sync_client.srem(self._threads_key, thread_id)

    async def _delete_thread_async(self, thread_id: str) -> None:
        pattern = f"{self.prefix}:{self._escape(thread_id)}:*"
        keys = [key async for key in self.async_client.scan_iter(match=pattern)]
        if keys:
            await self.async_client.delete(*keys)
        await self.async_client.srem(self._threads_key, thread_id)

    def delete_for_runs(self, run_ids: Sequence[str]) -> None:
        self._delete_matching_runs_sync(set(run_ids))

    async def adelete_for_runs(self, run_ids: Sequence[str]) -> None:
        await self._delete_matching_runs_async(set(run_ids))

    def _delete_matching_runs_sync(self, run_ids: set[str]) -> None:
        for item in list(self.list(None)):
            if str(item.metadata.get("run_id", "")) in run_ids:
                self._delete_checkpoint_sync(item.config)

    async def _delete_matching_runs_async(self, run_ids: set[str]) -> None:
        items = [item async for item in self.alist(None)]
        for item in items:
            if str(item.metadata.get("run_id", "")) in run_ids:
                await self._delete_checkpoint_async(item.config)

    def _delete_checkpoint_sync(self, config) -> None:
        thread_id, namespace, checkpoint_id = self._config_parts(config)
        if checkpoint_id:
            self.sync_client.delete(
                self._checkpoint_key(thread_id, namespace, checkpoint_id),
                self._writes_key(thread_id, namespace, checkpoint_id),
            )
            self.sync_client.zrem(self._index_key(thread_id, namespace), checkpoint_id)

    async def _delete_checkpoint_async(self, config) -> None:
        thread_id, namespace, checkpoint_id = self._config_parts(config)
        if checkpoint_id:
            await self.async_client.delete(
                self._checkpoint_key(thread_id, namespace, checkpoint_id),
                self._writes_key(thread_id, namespace, checkpoint_id),
            )
            await self.async_client.zrem(self._index_key(thread_id, namespace), checkpoint_id)

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        self._copy_thread_sync(source_thread_id, target_thread_id)

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        await self._copy_thread_async(source_thread_id, target_thread_id)

    def _copy_thread_sync(self, source_thread_id: str, target_thread_id: str) -> None:
        items = list(self.list({"configurable": {"thread_id": source_thread_id}}))
        for item in reversed(items):
            parent_id = get_checkpoint_id(item.parent_config) if item.parent_config else None
            config = {
                "configurable": {
                    "thread_id": target_thread_id,
                    "checkpoint_ns": item.config["configurable"]["checkpoint_ns"],
                }
            }
            if parent_id:
                config["configurable"]["checkpoint_id"] = parent_id
            saved = self.put(config, item.checkpoint, item.metadata, item.checkpoint["channel_versions"])
            if item.pending_writes:
                self.put_writes(saved, [(channel, value) for _, channel, value in item.pending_writes], "copied")

    async def _copy_thread_async(self, source_thread_id: str, target_thread_id: str) -> None:
        items = [item async for item in self.alist({"configurable": {"thread_id": source_thread_id}})]
        for item in reversed(items):
            parent_id = get_checkpoint_id(item.parent_config) if item.parent_config else None
            config = {
                "configurable": {
                    "thread_id": target_thread_id,
                    "checkpoint_ns": item.config["configurable"]["checkpoint_ns"],
                }
            }
            if parent_id:
                config["configurable"]["checkpoint_id"] = parent_id
            saved = await self.aput(config, item.checkpoint, item.metadata, item.checkpoint["channel_versions"])
            if item.pending_writes:
                await self.aput_writes(saved, [(channel, value) for _, channel, value in item.pending_writes], "copied")

    def prune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        if strategy == "delete":
            for thread_id in thread_ids:
                self.delete_thread(thread_id)
        elif strategy != "keep_latest":
            raise ValueError(f"unsupported prune strategy: {strategy}")

    async def aprune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        if strategy == "delete":
            for thread_id in thread_ids:
                await self.adelete_thread(thread_id)
        elif strategy != "keep_latest":
            raise ValueError(f"unsupported prune strategy: {strategy}")

    def close(self) -> None:
        self.sync_client.close()

    async def aclose(self) -> None:
        await self.async_client.aclose()
        self.sync_client.close()
