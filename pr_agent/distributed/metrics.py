import json
import time
from urllib.parse import quote

OBSERVE_METRIC_LUA = """
local value = tonumber(ARGV[1])
local current_max = tonumber(redis.call('HGET', KEYS[1], 'max') or '-1')
redis.call('HINCRBY', KEYS[1], 'count', 1)
redis.call('HINCRBYFLOAT', KEYS[1], 'sum', value)
redis.call('HSET', KEYS[1], 'last', value, 'updated_at', ARGV[2], 'display', ARGV[3])
if value > current_max then
  redis.call('HSET', KEYS[1], 'max', value)
end
redis.call('SADD', KEYS[2], KEYS[1])
return 1
"""


class DistributedMetrics:
    def __init__(self, redis_client, *, prefix: str = "pr-agent:metrics") -> None:
        self.redis = redis_client
        self.prefix = prefix

    @property
    def registry_key(self) -> str:
        return f"{self.prefix}:registry"

    @staticmethod
    def _display(name: str, labels: dict[str, str] | None) -> str:
        if not labels:
            return name
        values = "|".join(f"{key}={labels[key]}" for key in sorted(labels))
        return f"{name}|{values}"

    def _metric_key(self, name: str, labels: dict[str, str] | None) -> tuple[str, str]:
        display = self._display(name, labels)
        return f"{self.prefix}:{quote(display, safe='')}", display

    async def increment(self, name: str, value: int = 1, labels: dict[str, str] | None = None) -> None:
        key, display = self._metric_key(name, labels)
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.hincrby(key, "count", value)
            pipeline.hset(key, mapping={"updated_at": time.time(), "display": display})
            pipeline.sadd(self.registry_key, key)
            await pipeline.execute()

    async def observe_ms(self, name: str, value: int | float, labels: dict[str, str] | None = None) -> None:
        key, display = self._metric_key(name, labels)
        await self.redis.eval(
            OBSERVE_METRIC_LUA,
            2,
            key,
            self.registry_key,
            float(value),
            time.time(),
            display,
        )

    async def snapshot(self) -> dict[str, dict]:
        result = {}
        for key in await self.redis.smembers(self.registry_key):
            value = await self.redis.hgetall(str(key))
            if not value:
                continue
            display = str(value.pop("display", key))
            result[display] = self._public_value(value)
        return result

    @staticmethod
    def _public_value(value: dict) -> dict:
        output = {}
        for key, item in value.items():
            if key == "count":
                output[key] = int(item)
            elif key in {"sum", "max", "last", "updated_at"}:
                output[key] = float(item)
        return output

    def to_json(self, snapshot: dict[str, dict]) -> str:
        return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
