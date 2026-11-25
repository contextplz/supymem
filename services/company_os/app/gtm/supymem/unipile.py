"""Thin Unipile REST client for LinkedIn messaging + invitations.

Uses the documented ``/api/v1`` surface (same style as existing chat sends).
Never logs API keys. Callers must treat missing credentials as NeedsYou /
human_queue — do not fake success.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

DEFAULT_DSN = "https://api.unipile.com"


class UnipileError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def configured() -> bool:
    return bool(
        (os.environ.get("UNIPILE_API_KEY") or "").strip()
        and (os.environ.get("UNIPILE_ACCOUNT_ID") or "").strip()
    )


def credentials() -> tuple[str, str, str]:
    key = (os.environ.get("UNIPILE_API_KEY") or "").strip()
    account_id = (os.environ.get("UNIPILE_ACCOUNT_ID") or "").strip()
    dsn = (os.environ.get("UNIPILE_DSN") or DEFAULT_DSN).rstrip("/")
    if not key or not account_id:
        raise UnipileError("UNIPILE_API_KEY + UNIPILE_ACCOUNT_ID required")
    return key, account_id, dsn


def public_identifier_from_url(value: str) -> str:
    """Extract LinkedIn vanity slug from URL or return raw identifier."""
    raw = (value or "").strip()
    if not raw:
        return ""
    m = re.search(r"linkedin\.com/in/([^/?#]+)", raw, re.I)
    if m:
        return m.group(1).rstrip("/")
    if raw.startswith("http"):
        return raw.rstrip("/").rsplit("/", 1)[-1]
    return raw.lstrip("@")


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    key, account_id, dsn = credentials()
    url = f"{dsn}{path}"
    headers = {"X-API-KEY": key, "Accept": "application/json"}
    q = dict(params or {})
    if "account_id" not in q and method.upper() == "GET":
        q["account_id"] = account_id
    with httpx.Client(timeout=timeout) as client:
        res = client.request(
            method.upper(),
            url,
            headers=headers,
            params=q or None,
            json=json_body,
        )
    if res.status_code >= 400:
        raise UnipileError(
            f"Unipile HTTP {res.status_code}: {res.text[:400]}",
            status=res.status_code,
            body=res.text[:800],
        )
    if not res.content:
        return {}
    data = res.json()
    return data if isinstance(data, dict) else {"data": data}


def get_profile(identifier: str) -> dict[str, Any]:
    """Resolve public LinkedIn slug / provider id → Unipile UserProfile."""
    slug = public_identifier_from_url(identifier)
    if not slug:
        raise UnipileError("profile identifier required")
    return _request("GET", f"/api/v1/users/{quote(slug, safe='')}")


def send_invitation(*, provider_id: str, message: str = "") -> dict[str, Any]:
    """Send LinkedIn connection request (optional note)."""
    _, account_id, _ = credentials()
    pid = (provider_id or "").strip()
    if not pid:
        raise UnipileError("provider_id required")
    body: dict[str, Any] = {"provider_id": pid, "account_id": account_id}
    note = (message or "").strip()
    if note:
        body["message"] = note[:300]
    return _request("POST", "/api/v1/users/invite", json_body=body)


def list_invitations_received(*, limit: int = 20) -> dict[str, Any]:
    return _request(
        "GET",
        "/api/v1/users/invite/received",
        params={"limit": max(1, min(int(limit), 100))},
    )


def list_invitations_sent(*, limit: int = 20) -> dict[str, Any]:
    return _request(
        "GET",
        "/api/v1/users/invite/sent",
        params={"limit": max(1, min(int(limit), 100))},
    )


def handle_invitation(
    *,
    invitation_id: str,
    action: str = "accept",
    shared_secret: str,
) -> dict[str, Any]:
    """Accept or decline a received invitation (LinkedIn requires shared_secret)."""
    _, account_id, _ = credentials()
    inv_id = (invitation_id or "").strip()
    secret = (shared_secret or "").strip()
    if not inv_id or not secret:
        raise UnipileError("invitation_id + shared_secret required")
    act = (action or "accept").strip().lower()
    if act not in {"accept", "decline"}:
        raise UnipileError("action must be accept or decline")
    return _request(
        "POST",
        f"/api/v1/users/invite/received/{quote(inv_id, safe='')}",
        json_body={
            "provider": "LINKEDIN",
            "shared_secret": secret,
            "account_id": account_id,
            "action": act,
        },
    )


def start_chat(*, attendees_ids: list[str], text: str) -> dict[str, Any]:
    """Start (or continue) a LinkedIn chat with provider ids or profile URLs."""
    _, account_id, _ = credentials()
    ids = [a.strip() for a in attendees_ids if (a or "").strip()]
    body_text = (text or "").strip()
    if not ids or not body_text:
        raise UnipileError("attendees_ids + text required")
    return _request(
        "POST",
        "/api/v1/chats",
        json_body={
            "account_id": account_id,
            "attendees_ids": ids,
            "text": body_text,
        },
    )


def list_chats(*, limit: int = 25) -> dict[str, Any]:
    return _request(
        "GET",
        "/api/v1/chats",
        params={"limit": max(1, min(int(limit), 100))},
    )


def list_chat_messages(chat_id: str, *, limit: int = 30) -> dict[str, Any]:
    cid = (chat_id or "").strip()
    if not cid:
        raise UnipileError("chat_id required")
    return _request(
        "GET",
        f"/api/v1/chats/{quote(cid, safe='')}/messages",
        params={"limit": max(1, min(int(limit), 100))},
    )


def send_message_in_chat(chat_id: str, text: str) -> dict[str, Any]:
    cid = (chat_id or "").strip()
    body_text = (text or "").strip()
    if not cid or not body_text:
        raise UnipileError("chat_id + text required")
    return _request(
        "POST",
        f"/api/v1/chats/{quote(cid, safe='')}/messages",
        json_body={"text": body_text},
    )


def summarize_invitation(item: dict[str, Any]) -> dict[str, Any]:
    """Safe projection of a received invitation (includes note text)."""
    inviter = item.get("inviter") if isinstance(item.get("inviter"), dict) else {}
    specifics = item.get("specifics") if isinstance(item.get("specifics"), dict) else {}
    return {
        "id": item.get("id"),
        "invitation_text": item.get("invitation_text") or item.get("message"),
        "inviter_name": inviter.get("inviter_name"),
        "inviter_id": inviter.get("inviter_id"),
        "inviter_public_identifier": inviter.get("inviter_public_identifier"),
        "inviter_description": inviter.get("inviter_description"),
        "shared_secret_present": bool(specifics.get("shared_secret")),
        "shared_secret": specifics.get("shared_secret"),
        "date": item.get("date") or item.get("parsed_datetime"),
    }
