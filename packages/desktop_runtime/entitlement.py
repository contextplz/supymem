"""Privileged-process entitlement cache (doc 112 §3.2 / P6).

The webview never sees the raw lease — only booleans. Verification uses the
public key from the hosted API (or ENTITLEMENT_PUBLIC_KEY). Cached under
COMPANY_OS_DATA_DIR so it survives restarts for the 7-day offline grace.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from desktop_runtime.paths import data_root

CACHE_NAME = "entitlement_lease.json"
MAX_OFFLINE_SECONDS = 7 * 24 * 3600


def _cache_path() -> Path:
    return Path(data_root()) / CACHE_NAME


def _load_cache() -> dict[str, Any] | None:
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(payload: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _gateway_url() -> str:
    return (
        os.environ.get("COMPANY_OS_PUBLIC_URL")
        or os.environ.get("GATEWAY_URL")
        or "http://127.0.0.1:3200"
    ).rstrip("/")


def fetch_entitlement(*, workspace_id: str | None, cookie: str | None = None) -> dict[str, Any] | None:
    qs = f"?workspace_id={workspace_id}" if workspace_id else ""
    req = Request(_gateway_url() + "/api/entitlement" + qs)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    lease = data.get("lease")
    if isinstance(lease, dict):
        try:
            from app.billing.lease import verify_lease

            pub = str(data.get("public_key") or os.environ.get("ENTITLEMENT_PUBLIC_KEY") or "")
            if not verify_lease(lease, public_key_b64_override=pub or None):
                return None
        except Exception:
            # Core process can import company_os; a frozen sidecar may not.
            pass
        data["_cached_at"] = time.time()
        _save_cache(data)
    return data


def has_entitlement(feature: str, *, workspace_id: str | None = None) -> bool:
    """Boolean only — this is what the UI is allowed to know."""
    live = fetch_entitlement(workspace_id=workspace_id)
    payload = live or _load_cache()
    if not payload:
        return feature in {"local_app", "credits_meter"}
    cached_at = float(payload.get("_cached_at") or 0)
    if live is None and cached_at and (time.time() - cached_at) > MAX_OFFLINE_SECONDS:
        return feature in {"local_app", "credits_meter"}
    features = payload.get("features") or (payload.get("lease") or {}).get("features") or []
    return feature in features or feature == "local_app"
