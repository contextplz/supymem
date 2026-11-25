"""Minimal Company OS Python SDK — thin HTTP client over core (port 3300)."""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE = "http://127.0.0.1:3300"


class CompanyOS:
    def __init__(self, base_url: str = DEFAULT_BASE, token: str | None = None) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CompanyOS":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def health(self) -> dict[str, Any]:
        return self._client.get("/health").json()

    def enter(self, text: str | None = None, *, mode: str | None = None, **kw: Any) -> dict[str, Any]:
        body = {"text": text, "explicit_mode": mode, **kw}
        r = self._client.post("/entry", json=body)
        r.raise_for_status()
        return r.json()

    def site(self, workspace_id: str) -> dict[str, Any]:
        r = self._client.get(f"/workspaces/{workspace_id}/site")
        r.raise_for_status()
        return r.json()

    def mcp(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self._client.post(
            f"/mcp/tools/{tool}",
            json={"name": tool, "arguments": arguments or {}},
        )
        r.raise_for_status()
        return r.json()

    def ops_agents(self) -> dict[str, Any]:
        r = self._client.get("/api/v1/agents/status")
        r.raise_for_status()
        return r.json()
