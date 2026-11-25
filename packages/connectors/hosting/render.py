"""Hosting adapter stub for render — secrets from vault handles only."""

from __future__ import annotations

async def deploy(*, image: str, secrets: dict[str, str], dry_run: bool = True) -> dict:
    if dry_run:
        return {"ok": True, "dry_run": True, "provider": "render", "url": "https://example.local"}
    return {"ok": True, "provider": "render", "url": "https://app.example.com"}
