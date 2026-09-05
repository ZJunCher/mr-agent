import redis
import redis.asyncio as async_redis


class RedisClientFactory:
    """Create process-local Redis clients with the protocol shared by every service."""

    def __init__(self, redis_url: str):
        if not redis_url:
            raise ValueError("redis_url is required")
        self.redis_url = redis_url

    def create_async(self) -> async_redis.Redis:
        return async_redis.Redis.from_url(
            self.redis_url,
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=2,
            socket_timeout=5,
            health_check_interval=10,
        )

    def create_sync(self) -> redis.Redis:
        return redis.Redis.from_url(
            self.redis_url,
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=2,
            socket_timeout=5,
            health_check_interval=10,
        )
