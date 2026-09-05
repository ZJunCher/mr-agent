#!/usr/bin/env python3
import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def fetch_json(url: str, timeout: float) -> dict:
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check PR-Agent distributed health without printing credentials")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-agents", type=int, default=3)
    parser.add_argument("--max-oldest-seconds", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    try:
        ready = fetch_json(f"{base_url}/health/ready", args.timeout)
        snapshot = fetch_json(f"{base_url}/health/distributed", args.timeout)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        print(f"health_check_failed={type(error).__name__}")
        return 1

    agents = snapshot.get("agent_workers") or {}
    queue = snapshot.get("queue") or {}
    feishu = snapshot.get("feishu") or {}
    live_agents = int(agents.get("live") or 0)
    oldest = float(queue.get("oldest_ingress_seconds") or 0)
    redis_status = str(snapshot.get("redis") or "unavailable")
    web_status = str(ready.get("status") or "unavailable")
    feishu_status = "ok" if feishu.get("alive") else "unavailable"
    print(
        f"redis={redis_status} web={web_status} agents={live_agents}/{args.expected_agents} "
        f"feishu={feishu_status} queue_oldest_seconds={oldest:.3f}"
    )
    healthy = (
        redis_status == "ok"
        and web_status == "ok"
        and snapshot.get("status") == "ok"
        and live_agents == args.expected_agents
        and feishu_status == "ok"
        and oldest < args.max_oldest_seconds
    )
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
