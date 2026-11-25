"""Local hosting with port allocation 4173–4273."""

from __future__ import annotations

import threading
from typing import Any

from app.provisioning.protocol import Preflight, ProvisionResult

_PORT_LOCK = threading.Lock()
_NEXT = 4173
_IN_USE: set[int] = set()
_PORT_MAX = 4273


def allocate_port() -> int:
    global _NEXT
    with _PORT_LOCK:
        for _ in range(_PORT_MAX - 4173 + 1):
            p = _NEXT
            _NEXT = 4173 if _NEXT >= _PORT_MAX else _NEXT + 1
            if p not in _IN_USE:
                _IN_USE.add(p)
                return p
        raise RuntimeError("no ports available")


def release_port(port: int) -> None:
    with _PORT_LOCK:
        _IN_USE.discard(port)


class HostLocal:
    resource = "hosting"
    provider = "local"
    capability = "net:egress"
    required_credentials: list[str] = []

    async def preflight(self, ctx: Any) -> Preflight:
        return Preflight(ok=True)

    async def provision(self, ctx: Any) -> ProvisionResult:
        port = allocate_port()
        url = f"http://127.0.0.1:{port}"
        return ProvisionResult(
            resource="hosting",
            simulated=True,
            handle=f"local:host:{port}",
            display=f"Local preview :{port}",
            provider="local",
            url=url,
            detail=[url],
            teardown={"port": port},
        )

    async def teardown(self, handle: str, data: dict) -> None:
        if data.get("port"):
            release_port(int(data["port"]))
