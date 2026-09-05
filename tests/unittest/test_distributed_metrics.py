import asyncio
from collections import defaultdict

from pr_agent.distributed.metrics import DistributedMetrics


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def hincrby(self, *args):
        self.calls.append(("hincrby", args))

    def hset(self, *args, **kwargs):
        self.calls.append(("hset", args, kwargs))

    def sadd(self, *args):
        self.calls.append(("sadd", args))

    async def execute(self):
        for call in self.calls:
            if call[0] == "hincrby":
                key, field, value = call[1]
                self.redis.hashes[key][field] = int(self.redis.hashes[key].get(field, 0)) + value
            elif call[0] == "hset":
                key = call[1][0]
                self.redis.hashes[key].update(call[2]["mapping"])
            elif call[0] == "sadd":
                key, value = call[1]
                self.redis.sets[key].add(value)


class FakeRedis:
    def __init__(self):
        self.hashes = defaultdict(dict)
        self.sets = defaultdict(set)

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    async def eval(self, _script, _key_count, key, registry, value, updated_at, display):
        metric = self.hashes[key]
        number = float(value)
        metric["count"] = int(metric.get("count", 0)) + 1
        metric["sum"] = float(metric.get("sum", 0)) + number
        metric["max"] = max(float(metric.get("max", -1)), number)
        metric.update({"last": number, "updated_at": updated_at, "display": display})
        self.sets[registry].add(key)
        return 1

    async def smembers(self, key):
        return self.sets[key]

    async def hgetall(self, key):
        return dict(self.hashes[key])


def test_metrics_record_counter_and_pipeline_latency():
    async def run_test():
        metrics = DistributedMetrics(FakeRedis())

        await metrics.increment("dedup_rejected", labels={"source": "gitlab"})
        await metrics.observe_ms("pipeline_wakeup_ms", 713, labels={"status": "success"})
        snapshot = await metrics.snapshot()

        assert snapshot["dedup_rejected|source=gitlab"]["count"] == 1
        assert snapshot["pipeline_wakeup_ms|status=success"]["max"] == 713
        assert snapshot["pipeline_wakeup_ms|status=success"]["last"] == 713

    asyncio.run(run_test())
