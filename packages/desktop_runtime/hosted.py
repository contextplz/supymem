"""window.__SUPYMEM__ payload for hosted vs local desktop (docs/supymem/18)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


def _hosts_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "hosts.json"


def load_hosts() -> dict[str, Any]:
    path = _hosts_path()
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _is_local() -> bool:
    try:
        from app.core.local_mode import is_local
    except ImportError:
        return os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local"
    return is_local()


def validate_local_state() -> None:
    if not _is_local():
        return
    from core.local_security import _local_hostname

    unsafe = []
    for name in ("REDIS_URL", "COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "RDS_PROXY_ENDPOINT"):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            parsed = urlsplit("//" + raw if name == "RDS_PROXY_ENDPOINT" else raw)
            if name not in {"REDIS_URL", "RDS_PROXY_ENDPOINT"} and not parsed.scheme.startswith("postgres"):
                continue
            hosts = [parsed.hostname]
            if parsed.scheme.startswith("postgres"):
                query = parse_qs(parsed.query, keep_blank_values=True)
                overrides = [host for field in ("host", "hostaddr") for value in query.get(field, []) for host in value.split(",")]
                if parsed.hostname is None and overrides:
                    hosts = []
                hosts.extend(overrides)
                hosts = [host for host in hosts if not (host and host.startswith("/"))]
            if any(not _local_hostname(host) for host in hosts):
                unsafe.append(name)
        except ValueError:
            unsafe.append(name)
    if unsafe:
        message = ("Your app cannot start until these data settings use an explicit local address: " + ", ".join(unsafe)
                   + ". Remove these settings from your local .env and launch environment, or set them to localhost, then restart the app.")
        logging.getLogger("desktop_runtime.hosted").error(message)
        raise RuntimeError(message) from None


def current_mode() -> str:
    if _is_local():
        validate_local_state()
        return "local"
    raw = (os.environ.get("SUPYMEM_MODE") or os.environ.get("COMPANY_OS_HOSTED") or "").strip().lower()
    if raw in {"1", "true", "yes", "hosted"}:
        return "hosted"
    env = (os.environ.get("APP_ENV") or "local").strip().lower()
    if env in {"staging", "production"}:
        return "hosted"
    return "local"


def gateway_url() -> str:
    explicit = (
        os.environ.get("COMPANY_OS_PUBLIC_URL")
        or os.environ.get("GATEWAY_URL")
        or ""
    ).rstrip("/")
    if explicit:
        return explicit
    hosts = load_hosts()
    mode = current_mode()
    env = "production" if mode == "hosted" and (os.environ.get("APP_ENV") or "") == "production" else (
        "staging" if (os.environ.get("APP_ENV") or "") == "staging" else "local"
    )
    if mode == "hosted" and env == "local":
        env = "staging"
    block = hosts.get(env) or hosts.get("local") or {}
    return str(block.get("gateway") or "http://127.0.0.1:3200").rstrip("/")


def window_supymem(*, bridge_port: int = 3500, version: str = "0.1.0") -> dict[str, Any]:
    token = os.environ.get("BRIDGE_TOKEN") or ""
    return {
        "mode": current_mode(),
        "gatewayUrl": gateway_url(),
        "bridgeUrl": f"http://127.0.0.1:{int(os.environ.get('BRIDGE_PORT') or bridge_port)}",
        "bridgeToken": token,
        "version": os.environ.get("SUPYMEM_VERSION") or version,
    }


def inject_javascript(payload: dict[str, Any] | None = None) -> str:
    data = payload or window_supymem()
    encoded = json.dumps(data, separators=(",", ":"))
    return (
        f"window.__SUPYMEM__={encoded};"
        'try{sessionStorage.setItem("cos_gateway_url",window.__SUPYMEM__.gatewayUrl);}catch(e){}'
        'try{sessionStorage.setItem("cos_bridge_url",window.__SUPYMEM__.bridgeUrl);}catch(e){}'
    )
