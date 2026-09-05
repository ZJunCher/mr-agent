from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from pr_agent.distributed.checkpoint import CoreRedisCheckpointSaver, _decode, _encode


class _MemoryPipeline:
    def __init__(self, client):
        self.client = client
        self.operations = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.operations.append((name, args, kwargs))
            return self

        return queue

    def execute(self):
        for name, args, kwargs in self.operations:
            getattr(self.client, name)(*args, **kwargs)


class _MemoryRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.sorted_sets = {}
        self.sets = {}

    def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    def pipeline(self, transaction=True):
        return _MemoryPipeline(self)

    def set(self, key, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def hset(self, key, mapping=None, *args):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hvals(self, key):
        return list(self.hashes.get(key, {}).values())

    def zadd(self, key, mapping):
        self.sorted_sets.setdefault(key, {}).update(mapping)

    def zrevrange(self, key, start, end):
        values = sorted(self.sorted_sets.get(key, {}), key=self.sorted_sets.get(key, {}).get, reverse=True)
        return values[start:] if end == -1 else values[start:end + 1]

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)


def test_checkpoint_serialization_round_trip_uses_typed_json():
    serde = JsonPlusSerializer()
    value = {"messages": ["中文", 1], "nested": {"ok": True}}

    payload = _encode(serde, value)

    assert _decode(serde, payload) == value
    assert "pickle" not in payload.lower()


def test_checkpoint_keys_escape_user_controlled_identity():
    saver = CoreRedisCheckpointSaver(sync_client=None, async_client=None)

    key = saver._checkpoint_key("task/536", "triage namespace", "checkpoint:1")

    assert key == "pr-agent:checkpoint:task%2F536:triage%20namespace:checkpoint%3A1"


def test_keep_latest_prune_is_safe_noop_for_delta_channels():
    saver = CoreRedisCheckpointSaver(sync_client=None, async_client=None)

    assert saver.prune(["task-1"], strategy="keep_latest") is None


def test_repair_plan_events_round_trip_through_core_redis_checkpoint():
    redis_client = _MemoryRedis()
    saver = CoreRedisCheckpointSaver(sync_client=redis_client, async_client=None)
    repair_plans = [
        {"schema_version": 1, "plan_id": "a" * 64, "version": 1},
        {"schema_version": 1, "plan_id": "b" * 64, "version": 2},
    ]
    repair_verifications = [{
        "plan_id": "b" * 64,
        "plan_version": 2,
        "verdict": "pass",
    }]
    repair_memory_contexts = [{
        "schema_version": 1,
        "plan_id": "b" * 64,
        "plan_version": 2,
        "work_item_id": "root-parser",
        "status": "injected",
        "attempt_id": "attempt-1",
        "memory_ids": ["memory-1"],
        "prompt_block": "bounded historical hint",
        "error_code": "",
        "created_at": "2026-08-27T00:00:00+00:00",
    }]
    compression_state = {
        "context_summary": "summary",
        "context_summary_covered_messages": 12,
        "context_compression_ineffective_count": 1,
        "context_compression_cooldown_until": 1787800000.0,
        "context_compression_last_input_hash": "c" * 64,
    }
    checkpoint = {
        "v": 1,
        "ts": "2026-08-26T00:00:00+00:00",
        "id": "checkpoint-1",
        "channel_values": {
            "repair_plans": repair_plans,
            "repair_verifications": repair_verifications,
            "repair_memory_contexts": repair_memory_contexts,
            **compression_state,
        },
        "channel_versions": {
            "repair_plans": 1,
            "repair_verifications": 1,
            "repair_memory_contexts": 1,
            **{key: 1 for key in compression_state},
        },
        "versions_seen": {},
        "updated_channels": [
            "repair_plans",
            "repair_verifications",
            "repair_memory_contexts",
            *compression_state,
        ],
    }
    config = {"configurable": {"thread_id": "repair-task", "checkpoint_ns": ""}}

    saved = saver.put(
        config,
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        {
            "repair_plans": 1,
            "repair_verifications": 1,
            "repair_memory_contexts": 1,
            **{key: 1 for key in compression_state},
        },
    )
    restored = saver.get_tuple(saved)

    assert restored.checkpoint["channel_values"]["repair_plans"] == repair_plans
    assert restored.checkpoint["channel_values"]["repair_verifications"] == repair_verifications
    assert restored.checkpoint["channel_values"]["repair_memory_contexts"] == repair_memory_contexts
    for key, value in compression_state.items():
        assert restored.checkpoint["channel_values"][key] == value
