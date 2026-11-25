"""Charter amendment diff."""
from __future__ import annotations
from typing import Any

def charter_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    changed = {k: {"from": old.get(k), "to": new.get(k)} for k in set(old)|set(new) if old.get(k) != new.get(k)}
    return {"changed": changed}
