"""Bounded, platform-owned HTTP journey contracts for product diagnostics.

The contract describes expected behavior; it is never loaded from the
untrusted product archive. Hosted QA can reuse this validator when it accepts
a separately reviewed scenario.
"""
from __future__ import annotations

import hashlib
import json
import re

_NAME = re.compile(r"[a-z][a-z0-9_]{2,63}\Z")
_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_PATH = re.compile(r"/[A-Za-z0-9_./{}-]{0,160}\Z")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_OPTIONAL = frozenset({"body", "origin", "expect", "contains", "excludes", "capture"})
_REQUIRED = frozenset({"name", "actor", "method", "path", "status"})


def validate_product_journey(raw: bytes) -> tuple[dict, bytes, str]:
    """Return validated contract, canonical bytes and SHA-256 identity."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 65536:
        raise ValueError("qa_contract_too_large")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("qa_contract_invalid") from exc
    if (not isinstance(value, dict) or set(value) != {"schema", "steps"}
            or value["schema"] != "local_product_journey_v1"
            or not isinstance(value["steps"], list)
            or not 2 <= len(value["steps"]) <= 60):
        raise ValueError("qa_contract_invalid")
    names: set[str] = set()
    for step in value["steps"]:
        if (not isinstance(step, dict) or not _REQUIRED <= step.keys()
                or step.keys() - _REQUIRED - _OPTIONAL
                or not isinstance(step["name"], str) or not _NAME.fullmatch(step["name"])
                or step["name"] in names
                or (step["actor"] is not None
                    and (type(step["actor"]) is not int or step["actor"] not in (0, 1)))
                or step["method"] not in _METHODS
                or not isinstance(step["path"], str) or not _PATH.fullmatch(step["path"])
                or step["path"].startswith("//")
                or any(part in {".", ".."} for part in step["path"].split("/"))
                or type(step["status"]) is not int or not 200 <= step["status"] <= 499
                or step.get("origin", "same") not in {"same", "foreign"}
                or ("capture" in step and (not isinstance(step["capture"], str)
                    or not _FIELD.fullmatch(step["capture"])))
                or any(not isinstance(step[key], list)
                       or not all(isinstance(item, str) and len(item) <= 2048 for item in step[key])
                       for key in ("contains", "excludes") if key in step)):
            raise ValueError("qa_contract_invalid")
        names.add(step["name"])
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(canonical) > 65536:
        raise ValueError("qa_contract_too_large")
    return value, canonical, hashlib.sha256(canonical).hexdigest()
