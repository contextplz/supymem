"""Marketing execution adapters — fulfill jobs through concrete backends.

The marketing system stays generic (create_content / publish_content / …).
Adapters decide whether that means Composio, the content engine, GTM, or ads.
Hermes research/browser adapters are operator fallbacks only.
"""

from __future__ import annotations

from typing import Any, Protocol


class AdapterResult(dict):
    """Typed-ish dict: ok, status (done|failed|blocked), result, proof, error, block_reason."""


class JobAdapter(Protocol):
    name: str

    def supports(self, kind: str) -> bool: ...

    async def run(self, job: dict[str, Any]) -> dict[str, Any]: ...
