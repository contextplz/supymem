"""Hosting plan — Fly/Render/Vercel."""
def plan_hosting(provider: str = "fly") -> dict:
    return {"provider": provider, "dry_run": True}
