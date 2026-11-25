"""Credential arming checks — always False in local mode."""

from __future__ import annotations

import os
from typing import Any


def provisioning_mode() -> str:
    return (os.environ.get("PROVISIONING_MODE") or "local").lower()


def credentials_armed(ctx: Any, keys: list[str]) -> bool:
    """In local mode, never report credentials as armed."""
    if provisioning_mode() == "local":
        return False
    if not keys:
        return True
    # Real mode would check gateway vault status; stub always False without vault.
    return False
