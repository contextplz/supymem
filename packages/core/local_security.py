"""Network and browser boundary for single-user local services."""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def _loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def _local_hostname(host: str | None) -> bool:
    return bool(host and (host.lower() == "localhost" or _loopback(host)))


OWNER_COOKIE = "supymem_owner"


def owner_secret_path() -> Path:
    return Path(os.environ.get("SUPYMEM_LOCAL_PROXY_SECRET_FILE") or
                Path(os.environ.get("COMPANY_OS_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
                / "owner-session.secret").expanduser()


def console_origin() -> str:
    return (os.environ.get("CONSOLE_ORIGIN") or os.environ.get("CONSOLE_URL") or
            f"http://127.0.0.1:{os.environ.get('CONSOLE_PORT', '3100')}").rstrip("/")


def local_browser_origins() -> set[str]:
    allowed = {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
    console = console_origin()
    try:
        address = urlsplit(console)
        if (address.scheme in {"http", "https"} and _local_hostname(address.hostname)
                and address.username is None and address.password is None
                and (address.port is None or address.port > 0)
                and console == f"{address.scheme}://{address.netloc}"):
            allowed.add(console)
    except ValueError:
        pass
    return allowed


def local_readiness_request(connection: HTTPConnection) -> bool:
    scope = connection.scope
    return (scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == "/local-ready"
            and connection.client is not None and _loopback(connection.client.host)
            and not any(name in connection.headers for name in ("x-forwarded-host", "origin", "referer")))


def public_request(connection: HTTPConnection) -> bool:
    if connection.scope["type"] != "http":
        return False
    path, method = connection.scope["path"], connection.scope["method"]
    if path.startswith("/api/v1/"):
        path = path[len("/api/v1"):]
    if method in {"GET", "POST"} and path in {"/public/unsubscribe", "/public/email/unsubscribe"}:
        return True
    if method in {"GET", "POST"} and re.fullmatch(r"/public/waitlists/[A-Za-z0-9_-]{1,128}", path):
        return True
    if method == "GET" and re.fullmatch(
            r"/public/waitlists/[A-Za-z0-9_-]{1,128}/pricing/exp_[a-f0-9]{24}/[a-z][a-z0-9_]{0,19}", path):
        return True
    if method in {"GET", "POST"} and re.fullmatch(
            r"/public/waitlists/[A-Za-z0-9_-]{1,128}/surveys/exp_[a-f0-9]{24}/[A-Za-z0-9_-]{20,128}", path):
        return True
    if method == "GET" and path == "/ai-gtm/gmail/callback":
        return True
    if method == "GET" and re.fullmatch(r"/content/oauth/[A-Za-z0-9_-]+/callback", path):
        return True
    return method == "POST" and path == "/e"


def _proxy_secret() -> bytes:
    try:
        if os.name == "nt":
            from app.core.local_windows import private_open

            descriptor = private_open(owner_secret_path(), os.O_RDONLY)
        else:
            descriptor = os.open(owner_secret_path(), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or (os.name != "nt" and (
                    metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid())):
                raise ValueError()
            secret = source.read(129).strip()
        if not re.fullmatch(rb"[0-9a-f]{64}", secret):
            raise ValueError()
        from core.logging import register_local_secret

        register_local_secret(secret.decode("ascii"))
        return secret
    except (OSError, ValueError):
        raise HTTPException(503, detail={"code": "local_proxy_not_ready",
            "message": "Check your app data-folder permissions, then restart your app. If this keeps happening, close your app and remove its owner-session.secret file before reopening."}) from None


def check_request(connection: HTTPConnection) -> None:
    allow_remote = os.environ.get("SUPYMEM_LOCAL_ALLOW_REMOTE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    try:
        for name in ("host", "x-forwarded-host", "origin", "referer", "sec-fetch-site", "x-local-proxy-secret"):
            values = connection.headers.getlist(name)
            if len(values) > 1 or any(any(ord(character) <= 32 or ord(character) == 127 or character == "\\" for character in value) for value in values):
                raise ValueError
            if name in {"host", "x-forwarded-host"} and any(any(character in "/@?#" for character in value) for value in values):
                raise ValueError
        credential = connection.headers.get("x-local-proxy-secret", "")
        cookie = connection.cookies.get(OWNER_COOKIE, "")
        if credential and cookie and credential != cookie:
            raise ValueError
        credential = credential or cookie
        if sum(part.strip().startswith(OWNER_COOKIE + "=") for value in connection.headers.getlist("cookie")
               for part in value.split(";")) > 1:
            raise ValueError
        secret = _proxy_secret() if credential else b""
        authenticated = bool(secret and secrets.compare_digest(secret, credential.encode()))
        trusted_proxy = authenticated and connection.headers.get("x-forwarded-host") == urlsplit(console_origin()).netloc
        if not allow_remote and not trusted_proxy and (not connection.client or not _loopback(connection.client.host)):
            raise HTTPException(403, detail={
                "code": "local_only",
                "message": "Open your app on this machine and try again. Keep its services private.",
            })
        host = urlsplit(f"http://{connection.headers.get('host', '')}")
        hosts = [host]
        forwarded_host = connection.headers.get("x-forwarded-host")
        if forwarded_host is not None:
            hosts.append(urlsplit(f"http://{forwarded_host}"))
        origin_raw = connection.headers.get("origin")
        origin = urlsplit(origin_raw) if origin_raw is not None else None
        if origin is not None and origin_raw != f"{origin.scheme}://{origin.netloc}":
            raise ValueError
        valid_host = all(candidate.hostname and candidate.username is None and candidate.password is None
                         and not candidate.path and not candidate.query and not candidate.fragment
                         and (candidate.port is None or candidate.port > 0) for candidate in hosts)
        local_hosts = hosts[1:] if trusted_proxy else hosts
        public_host = urlsplit(os.environ.get("COMPANY_OS_PUBLIC_URL", "")).netloc
        valid_host = valid_host and bool(local_hosts) and all(
            _local_hostname(candidate.hostname) or (allow_remote and candidate.netloc == public_host)
            for candidate in local_hosts)
        origins = [origin] if origin is not None else []
        referer = connection.headers.get("referer")
        if referer is not None:
            origins.append(urlsplit(referer)._replace(path="", query="", fragment=""))
        public = public_request(connection)
        allowed_origins = local_browser_origins()
        valid_origin = all(
            candidate.scheme in {"http", "https", "tauri"} and bool(candidate.hostname)
            and (candidate.port is None or candidate.port > 0)
            and candidate.username is None and candidate.password is None
            and not candidate.path and not candidate.query and not candidate.fragment
            and (public or f"{candidate.scheme}://{candidate.netloc}" in allowed_origins)
            for candidate in origins)
        valid_origin = valid_origin and (public or connection.headers.get("sec-fetch-site") in {None, "none", "same-origin", "same-site"})
        if not public and connection.headers.get("sec-fetch-site") == "same-site" and not origins:
            valid_origin = False
        if connection.scope["type"] == "http" and connection.scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            content_type = connection.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            explicit_bearer = connection.headers.get("authorization", "").lower().startswith("bearer ")
            if not public and not origins and not explicit_bearer and connection.headers.get("sec-fetch-site") != "same-origin" and content_type in {"text/plain", "application/x-www-form-urlencoded", "multipart/form-data"}:
                valid_origin = False
        if connection.scope["type"] == "websocket":
            valid_origin = bool(valid_origin and origin is not None)
    except ValueError:
        valid_host = valid_origin = False
    if not valid_host or not valid_origin:
        raise HTTPException(403, detail={
            "code": "local_origin_rejected",
            "message": "Open your local app directly and try again.",
        })
    if not public and not authenticated and not local_readiness_request(connection):
        raise HTTPException(403, detail={
            "code": "local_owner_required",
            "message": "Open your desktop app, then reopen your workspace from the app and retry.",
        })


class LocalNetworkMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if os.environ.get("SUPYMEM_MODE", "").strip().lower() != "local" or scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        try:
            check_request(HTTPConnection(scope))
        except HTTPException as exc:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "Open your local app directly and try again."})
            else:
                await JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                    headers={"Cache-Control": "no-store"})(scope, receive, send)
            return

        async def local_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}
                if b"content-security-policy" not in names and any(name.lower() == b"content-type" and value.lower().startswith(b"text/html") for name, value in headers):
                    headers.append((b"content-security-policy", b"sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"))
                if b"x-content-type-options" not in names:
                    headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, local_send)
