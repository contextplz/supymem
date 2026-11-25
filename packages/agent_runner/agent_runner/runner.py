from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx
from playwright.async_api import async_playwright

from agent_runner.actions import TOOLS, create_shopping_session, execute_tool, observe_page, parse_tool_calls, BrowserEnv
from agent_runner.provider import chat_completion, load_provider_config_from_env, redacted_status


def _urls() -> tuple[str, str]:
    api = os.getenv("ENVIRONMENT_API_URL", "http://localhost:8000").rstrip("/")
    site = os.getenv("SHOPPING_SITE_URL", "http://localhost:3000").rstrip("/")
    # Inside docker compose, runner uses service DNS; browser may need host mapping.
    if site.startswith("http://web:"):
        site = os.getenv("PUBLIC_SHOPPING_SITE_URL", "http://localhost:3000").rstrip("/")
    return api, site


async def _fetch_task(api: str, task_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{api}/api/tasks/{task_id}")
        r.raise_for_status()
        return r.json()


async def _fetch_order_reward(api: str, order_id: str) -> Optional[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{api}/api/orders/{order_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def run_oracle_task(task_id: str, *, artifacts_dir: Optional[Path] = None) -> dict[str, Any]:
    """Deterministic scripted browser path: search gold product title → open → cart → checkout."""
    api, site = _urls()
    task = await _fetch_task(api, task_id)
    session_id = await create_shopping_session(api, task_id)
    events: list[dict[str, Any]] = []
    artifacts: list[str] = []
    headless = os.getenv("AGENT_HEADLESS", "true").lower() != "false"
    artifacts_dir = artifacts_dir or Path(os.getenv("ARTIFACTS_DIR", "data/artifacts"))
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()
        # Search using a distinctive token from the gold product id / instruction.
        query = task.get("product_type") or task["instruction"].split()[0]
        # Better: look up gold product title via API.
        async with httpx.AsyncClient(timeout=30) as client:
            prod = (await client.get(f"{api}/api/products/{task['gold_product_id']}")).json()
        query = " ".join(prod["title"].split()[:4])
        await page.goto(f"{site}/search?q={quote_plus(query)}&session_id={session_id}", wait_until="networkidle")
        events.append({"type": "action", "name": "search", "args": {"query": query}})
        await page.goto(
            f"{site}/products/{task['gold_product_id']}?session_id={session_id}",
            wait_until="networkidle",
        )
        events.append({"type": "action", "name": "open_product", "args": {"product_id": task["gold_product_id"]}})
        await page.locator('[data-testid="add-to-cart"]').click()
        events.append({"type": "action", "name": "add_to_cart", "args": {}})
        await page.goto(f"{site}/checkout?session_id={session_id}", wait_until="networkidle")
        await page.locator('[data-testid="place-order"]').click()
        await page.wait_for_timeout(500)
        events.append({"type": "action", "name": "checkout", "args": {}})
        shot = artifacts_dir / f"oracle_{task_id}_{session_id}.png"
        await page.screenshot(path=str(shot), full_page=True)
        artifacts.append(str(shot))
        # Extract order id from URL if possible.
        order_id = None
        if "/order/" in page.url:
            order_id = page.url.rstrip("/").split("/")[-1].split("?")[0]
        await browser.close()

    reward = None
    score = 0.0
    success = False
    if order_id:
        payload = await _fetch_order_reward(api, order_id)
        if payload and payload.get("reward"):
            reward = payload["reward"]
            score = float(reward.get("score") or 0)
            success = bool(reward.get("success"))

    return {
        "task_id": task_id,
        "session_id": session_id,
        "order_id": order_id,
        "score": score,
        "success": success,
        "steps": len(events),
        "events": events,
        "artifacts": artifacts,
        "reward": reward,
        "mode": "oracle",
    }


async def run_agent_task(
    task_id: str,
    *,
    cancel_event: Optional[asyncio.Event] = None,
    artifacts_dir: Optional[Path] = None,
) -> dict[str, Any]:
    api, site = _urls()
    task = await _fetch_task(api, task_id)
    session_id = await create_shopping_session(api, task_id)
    max_steps = int(os.getenv("AGENT_MAX_STEPS", "25"))
    headless = os.getenv("AGENT_HEADLESS", "true").lower() != "false"
    artifacts_dir = artifacts_dir or Path(os.getenv("ARTIFACTS_DIR", "data/artifacts"))
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    events: list[dict[str, Any]] = []
    artifacts: list[str] = []
    order_id = None

    system = (
        "You are a shopping agent evaluating a deterministic e-commerce site. "
        "Use tools to search, open a product, add to cart, and checkout. "
        "Prefer products that match the instruction constraints (attributes, price ceiling, rating)."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Instruction: {task['instruction']}\nSession: {session_id}"},
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()
        await page.goto(f"{site}/?session_id={session_id}", wait_until="networkidle")
        env = BrowserEnv(site_url=site, api_url=api, session_id=session_id, page=page)
        obs = await observe_page(page)
        messages.append({"role": "user", "content": f"Current page observation JSON:\n{obs}"})

        steps = 0
        finished = False
        error = None
        try:
            while steps < max_steps and not finished:
                if cancel_event and cancel_event.is_set():
                    error = "cancelled"
                    break
                resp = await chat_completion(messages, tools=TOOLS)
                msg = resp.choices[0].message
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in (msg.tool_calls or [])
                        ]
                        if msg.tool_calls
                        else None,
                    }
                )
                calls = parse_tool_calls(msg)
                if not calls:
                    # Force a textual nudge once.
                    messages.append(
                        {
                            "role": "user",
                            "content": "Please call a tool: search, open_product, add_to_cart, go_to_cart, or checkout.",
                        }
                    )
                    steps += 1
                    continue
                for name, args in calls:
                    steps += 1
                    events.append(
                        {
                            "type": "action",
                            "name": name,
                            "args": args,
                            "ts": datetime.utcnow().isoformat(),
                        }
                    )
                    if name == "finish":
                        finished = True
                        break
                    result = await execute_tool(env, name, args)
                    if name == "checkout" and "/order/" in page.url:
                        order_id = page.url.rstrip("/").split("/")[-1].split("?")[0]
                        finished = True
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": (msg.tool_calls[0].id if msg.tool_calls else "manual"),
                            "content": result[:6000],
                        }
                    )
                    if finished:
                        break
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        finally:
            shot = artifacts_dir / f"agent_{task_id}_{session_id}.png"
            try:
                await page.screenshot(path=str(shot), full_page=True)
                artifacts.append(str(shot))
            except Exception:
                pass
            await browser.close()

    reward = None
    score = 0.0
    success = False
    if order_id:
        payload = await _fetch_order_reward(api, order_id)
        if payload and payload.get("reward"):
            reward = payload["reward"]
            score = float(reward.get("score") or 0)
            success = bool(reward.get("success"))

    return {
        "task_id": task_id,
        "session_id": session_id,
        "order_id": order_id,
        "score": score,
        "success": success,
        "steps": steps if "steps" in locals() else len(events),
        "events": events,
        "artifacts": artifacts,
        "reward": reward,
        "error": error,
        "mode": "agent",
        "provider": redacted_status(),
    }


async def run_evaluation(run_id: str, cancel_event: Optional[asyncio.Event] = None) -> dict[str, Any]:
    """Called by the environment API for a queued AgentRun."""
    api, _ = _urls()
    async with httpx.AsyncClient(timeout=30) as client:
        run = (await client.get(f"{api}/api/runs/{run_id}")).json()
    task_id = run["task_id"]
    mode = run.get("mode") or "agent"
    if mode == "oracle":
        return await run_oracle_task(task_id)
    return await run_agent_task(task_id, cancel_event=cancel_event)
