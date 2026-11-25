"""Sandbox providers. Fargate is Stage 12 hosted (see doc 37 H-10); fake is the conformance default."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from fastapi import HTTPException

from app.config import data_root
from app.core.local_mode import is_local


class SandboxProvider(Protocol):
    name: str

    def start(self, run_id: str, spec: dict[str, Any]) -> dict[str, Any]: ...

    def status(self, ref: str) -> dict[str, Any]: ...

    def exec_allowed(self, command: str) -> tuple[bool, str]: ...


_TERMINAL_ALLOW = ("npm test", "pytest", "python -m pytest", "ls", "pwd")
CODING_TOOL_SETUP_MESSAGE = "In Settings > Keys and connections > Coding tools, connect your tool, sign in with your own subscription and choose Allow Charlie, then retry your build."
CODING_TOOL_RESULTS_MESSAGE = "Open Build activity from your venture chat to review your coding tool's checked results; if you have no build, connect your tool in Settings > Keys and connections > Coding tools and request your build in chat."


class FakeSandbox:
    name = "fake"

    def start(self, run_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        if is_local():
            raise HTTPException(409, detail={"code": "build_tool_required", "message": CODING_TOOL_SETUP_MESSAGE})
        root = data_root() / "sandboxes" / run_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "artifact.json").write_text(json.dumps({"run_id": run_id, "spec": spec}, sort_keys=True), encoding="utf-8")
        (root / "index.html").write_text("<!doctype html><title>preview</title><p>ok</p>", encoding="utf-8")
        return {"ref": str(root), "preview_url": f"file://{root / 'index.html'}"}

    def status(self, ref: str) -> dict[str, Any]:
        if is_local():
            return {"state": "unverified", "ref": ref, "message": CODING_TOOL_RESULTS_MESSAGE}
        path = Path(ref)
        return {"state": "succeeded" if path.exists() else "failed", "ref": ref}

    def exec_allowed(self, command: str) -> tuple[bool, str]:
        cmd = (command or "").strip()
        if cmd in _TERMINAL_ALLOW or cmd.startswith("npm test"):
            return True, "allowlisted"
        if cmd.startswith("curl ") or cmd.startswith("rm "):
            return False, "needs_approval"
        return False, "denied"


class DockerSandbox(FakeSandbox):
    """Local docker backend. Uses the fake layout unless DOCKER_HOST is explicitly wired later."""

    name = "docker"


def get_provider() -> SandboxProvider:
    mode = (os.getenv("BUILDER_SANDBOX") or "fake").lower()
    if mode == "docker":
        return DockerSandbox()
    return FakeSandbox()
