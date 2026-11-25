#!/usr/bin/env python3
"""Minimal OpenEnv-compatible client smoke test."""

from __future__ import annotations

import json
import os
import sys

import httpx

API = os.getenv("ENVIRONMENT_API_URL", "http://127.0.0.1:8000").rstrip("/")


def main() -> int:
    with httpx.Client(timeout=30) as client:
        tasks = client.get(f"{API}/api/tasks", params={"limit": 1}).json()["tasks"]
        if not tasks:
            print("No tasks", file=sys.stderr)
            return 1
        task = tasks[0]
        reset = client.post(f"{API}/openenv/reset", json={"task_id": task["id"]}).json()
        episode = reset["episode_id"]
        pid = task["gold_product_id"]
        client.post(
            f"{API}/openenv/step",
            json={"episode_id": episode, "action": {"tool": "add_to_cart", "args": {"product_id": pid}}},
        )
        done = client.post(
            f"{API}/openenv/step",
            json={"episode_id": episode, "action": {"tool": "checkout", "args": {}}},
        ).json()
        print(json.dumps({"episode_id": episode, "done": done["done"], "reward": done["reward"]}, indent=2))
        return 0 if done["done"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
