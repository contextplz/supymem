"""Local preview handle surfaced after gates pass."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Preview:
    port: int
    url: str
    status: str = "ready"


def start_preview(port: int) -> Preview:
    from app.core.local_mode import is_local

    return Preview(port=int(port), url=f"http://127.0.0.1:{int(port)}", status="unverified" if is_local() else "ready")
