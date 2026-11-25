"""Analytics connector stub — dry-run safe."""

from __future__ import annotations


def track(event: str, props: dict | None = None) -> dict:
    return {"ok": True, "dry_run": True, "event": event, "props": props or {}}
