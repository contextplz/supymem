"""Hosting adapter stub for fly — secrets from vault handles only."""

from __future__ import annotations

async def deploy(*, image: str, secrets: dict[str, str], dry_run: bool = True) -> dict:
    if dry_run:
        return {"ok": True, "dry_run": True, "provider": "fly", "url": "https://example.local"}
    return {"ok": True, "provider": "fly", "url": "https://app.example.com"}
