import os

from redis import Redis
from redis.exceptions import RedisError


def redis_is_ready() -> bool:
    redis_url = os.getenv("PR_AGENT_REDIS_URL", "").strip()
    if not redis_url:
        return False
    try:
        client = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except (RedisError, ValueError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if redis_is_ready() else 1)
