from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import httpx
from playwright.async_api import Page


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the shopping catalog with a text query.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_product",
            "description": "Open a product detail page by product id.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "string"}},
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": "Click Add to Cart on the current product page.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_to_cart",
            "description": "Navigate to the cart page.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkout",
            "description": "Complete checkout from the cart/checkout page.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Stop after checkout is complete.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


@dataclass
class BrowserEnv:
    site_url: str
    api_url: str
    session_id: str
    page: Page


async def create_shopping_session(api_url: str, task_id: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{api_url}/api/sessions", json={"task_id": task_id})
        resp.raise_for_status()
        return resp.json()["session_id"]


async def observe_page(page: Page) -> str:
    title = await page.title()
    url = page.url
    # Collect visible product cards / key buttons for the model.
    texts = await page.evaluate(
        """() => {
          const nodes = Array.from(document.querySelectorAll('[data-testid], button, a, h1, h2, input'));
          return nodes.slice(0, 80).map(n => ({
            tag: n.tagName,
            testid: n.getAttribute('data-testid'),
            text: (n.innerText || n.value || '').trim().slice(0, 120),
            href: n.getAttribute('href')
          })).filter(x => x.text || x.testid);
        }"""
    )
    return json.dumps({"title": title, "url": url, "nodes": texts}, ensure_ascii=False)


async def execute_tool(env: BrowserEnv, name: str, args: dict[str, Any]) -> str:
    page = env.page
    site = env.site_url.rstrip("/")
    if name == "search":
        q = str(args.get("query", ""))
        await page.goto(f"{site}/search?q={quote_plus(q)}&session_id={env.session_id}", wait_until="networkidle")
        return await observe_page(page)
    if name == "open_product":
        pid = str(args.get("product_id", ""))
        await page.goto(f"{site}/products/{pid}?session_id={env.session_id}", wait_until="networkidle")
        return await observe_page(page)
    if name == "add_to_cart":
        btn = page.locator('[data-testid="add-to-cart"]')
        await btn.click()
        await page.wait_for_timeout(400)
        return await observe_page(page)
    if name == "go_to_cart":
        await page.goto(f"{site}/cart?session_id={env.session_id}", wait_until="networkidle")
        return await observe_page(page)
    if name == "checkout":
        # Prefer checkout page button; fall back to cart checkout.
        if "/checkout" not in page.url:
            await page.goto(f"{site}/checkout?session_id={env.session_id}", wait_until="networkidle")
        btn = page.locator('[data-testid="place-order"]')
        await btn.click()
        await page.wait_for_timeout(800)
        return await observe_page(page)
    if name == "finish":
        return "finished"
    return f"unknown tool {name}"


def parse_tool_calls(message: Any) -> list[tuple[str, dict[str, Any]]]:
    calls = []
    tool_calls = getattr(message, "tool_calls", None) or []
    for tc in tool_calls:
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append((name, args))
    if calls:
        return calls
    # Fallback: parse WebShop-style text actions from content.
    content = (message.content or "") if hasattr(message, "content") else str(message)
    for m in re.finditer(r"(search|click|add_to_cart|checkout)\[(.*?)\]", content):
        act, val = m.group(1), m.group(2)
        if act == "search":
            calls.append(("search", {"query": val}))
        elif act == "click":
            if val.lower() in {"buy now", "checkout", "place order"}:
                calls.append(("checkout", {}))
            else:
                calls.append(("open_product", {"product_id": val}))
        elif act == "add_to_cart":
            calls.append(("add_to_cart", {}))
        elif act == "checkout":
            calls.append(("checkout", {}))
    return calls
