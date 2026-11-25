from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx

from agent_runner.runner import run_agent_task, run_oracle_task


async def _list_task_ids(limit: int) -> list[str]:
    api = os.getenv("ENVIRONMENT_API_URL", "http://localhost:8000").rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{api}/api/tasks", params={"limit": limit})
        r.raise_for_status()
        return [t["id"] for t in r.json()["tasks"][:limit]]


async def _run_batch(mode: str, limit: int) -> dict:
    task_ids = await _list_task_ids(limit)
    results = []
    for tid in task_ids:
        if mode == "oracle":
            result = await run_oracle_task(tid)
        else:
            result = await run_agent_task(tid)
        results.append(result)
        print(json.dumps({"task_id": tid, "success": result.get("success"), "score": result.get("score")}))
    successes = sum(1 for r in results if r.get("success"))
    summary = {
        "mode": mode,
        "attempted": len(results),
        "completed": sum(1 for r in results if r.get("order_id")),
        "successes": successes,
        "success_rate": (successes / len(results)) if results else 0.0,
        "results": results,
    }
    print(json.dumps({"summary": {k: summary[k] for k in ("mode", "attempted", "completed", "successes", "success_rate")}}, indent=2))
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rl-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("oracle", "agent"):
        p = sub.add_parser(name)
        p.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    summary = asyncio.run(_run_batch(args.cmd, args.limit))
    # Non-zero exit only if oracle fails entirely (stack broken).
    if args.cmd == "oracle" and summary["successes"] == 0 and summary["attempted"] > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
