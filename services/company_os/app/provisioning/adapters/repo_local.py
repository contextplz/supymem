"""Local git repo adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from coding_agents.env import child_env

from app.provisioning.protocol import Preflight, ProvisionResult


class RepoLocal:
    resource = "repo"
    provider = "local"
    capability = "proc:spawn"
    required_credentials: list[str] = []

    async def preflight(self, ctx: Any) -> Preflight:
        return Preflight(ok=True)

    async def provision(self, ctx: Any) -> ProvisionResult:
        root = Path(__file__).resolve().parents[5] / "data" / "ventures" / ctx.venture.id / "repo"
        root.mkdir(parents=True, exist_ok=True)
        git_dir = root / ".git"
        if not git_dir.exists():
            try:
                subprocess.run(
                    ["git", "init"],
                    cwd=str(root),
                    env=child_env(),
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                (root / ".gitkeep").write_text("", encoding="utf-8")
        return ProvisionResult(
            resource="repo",
            simulated=True,
            handle=f"local:repo:{ctx.venture.id}",
            display=f"{ctx.company}/local",
            provider="local",
            url=str(root),
            detail=[str(root)],
            teardown={"path": str(root), "archive": True},
        )

    async def teardown(self, handle: str, data: dict) -> None:
        return None
