"""Signed HTTP client for QM POST /v1/turns + GET /v1/runs/:id (doc 55 §4)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.qm.scope import ScopeRef
from app.qm import settings as qm_settings

log = logging.getLogger(__name__)

TERMINAL_TURN_STATUSES = frozenset({"ok", "failed", "refused", "silent", "react"})
TERMINAL_RUN_STATUSES = frozenset({"done", "failed"})


@dataclass(frozen=True)
class TurnResult:
    status: str
    reply: str | None = None
    run_id: str | None = None
    reason: str | None = None
    raw: dict[str, Any] | None = None


def canonical_payload(method: str, path_with_query: str, body: str) -> str:
    """Byte-for-byte match of qm/src/auth/source-auth-sign.ts canonicalPayload."""
    return f"{method}\n{path_with_query}\n{body}"


def sign_request(secret: str, timestamp_sec: int, canonical: str) -> str:
    """Byte-for-byte match of qm/src/auth/source-auth-sign.ts signRequest."""
    digest = hmac.new(
        secret.encode("utf-8"),
        f"v0:{timestamp_sec}:{canonical}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


def signed_headers(
    secret: str,
    method: str,
    path_with_query: str,
    body: str = "",
    *,
    now_sec: int | None = None,
) -> dict[str, str]:
    ts = int(time.time()) if now_sec is None else now_sec
    canonical = canonical_payload(method, path_with_query, body)
    return {
        "x-timestamp": str(ts),
        "x-signature": sign_request(secret, ts, canonical),
        "content-type": "application/json",
        "accept": "application/json",
    }


class QmClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _http_json(
    method: str,
    path_with_query: str,
    *,
    body_obj: dict[str, Any] | None = None,
    secret: str,
    base_url: str,
    timeout_s: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    body = "" if body_obj is None else json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
    headers = signed_headers(secret, method, path_with_query, body)
    url = f"{base_url}{path_with_query}"
    req = Request(url, data=body.encode("utf-8") if body else None, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — local QM sidecar URL
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200) or 200
    except HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise QmClientError(
            f"QM {method} {path_with_query} -> HTTP {exc.code}: {err_body[:500]}",
            status_code=exc.code,
            body=err_body,
        ) from exc
    except URLError as exc:
        raise QmClientError(f"QM unreachable at {base_url}: {exc.reason}") from exc

    if not raw.strip():
        return status, {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QmClientError(f"QM returned non-JSON: {raw[:200]}") from exc
    if not isinstance(parsed, dict):
        raise QmClientError(f"QM returned non-object JSON: {type(parsed)}")
    return status, parsed


async def run_turn(
    *,
    scope: ScopeRef,
    text: str,
    idempotency_key: str,
    timeout_s: float | None = None,
    read_only: bool = False,
    person_id: str | None = None,
    base_url: str | None = None,
    secret: str | None = None,
) -> TurnResult:
    """Start an async QM turn and poll until terminal or timeout (doc 55 §4.2–4.3)."""
    base = (base_url or qm_settings.qm_base_url()).rstrip("/")
    signing = (secret if secret is not None else qm_settings.qm_source_signing_secret()).strip()
    if not signing:
        raise QmClientError("QM_SOURCE_SIGNING_SECRET is not set")
    if len(signing) < 32:
        raise QmClientError("QM_SOURCE_SIGNING_SECRET must be at least 32 characters")

    budget = timeout_s if timeout_s is not None else qm_settings.qm_ops_turn_timeout_s()
    payload: dict[str, Any] = {
        "surface": "companyos",
        "actor": {"externalId": qm_settings.qm_actor_external_id(person_id)},
        "conversation": scope.conversation_dict(),
        "text": text,
        "async": True,
        "idempotencyKey": idempotency_key,
    }
    if read_only:
        payload["readOnly"] = True

    http_status, immediate = await asyncio.to_thread(
        _http_json,
        "POST",
        "/v1/turns",
        body_obj=payload,
        secret=signing,
        base_url=base,
        timeout_s=min(30.0, budget),
    )

    status = str(immediate.get("status") or "")
    run_id = immediate.get("runId") or immediate.get("run_id")
    if isinstance(run_id, str) is False:
        run_id = None

    # Already terminal (unlikely with async=true, but handle it).
    if status in TERMINAL_TURN_STATUSES and not run_id:
        return TurnResult(
            status=status,
            reply=immediate.get("reply") if isinstance(immediate.get("reply"), str) else None,
            run_id=None,
            reason=immediate.get("reason") if isinstance(immediate.get("reason"), str) else None,
            raw=immediate,
        )

    if not run_id:
        # 202 queued should always include runId; treat missing as failure.
        raise QmClientError(
            f"QM turn response missing runId (HTTP {http_status}): {immediate!r}"
        )

    if status in TERMINAL_TURN_STATUSES:
        return TurnResult(
            status=status,
            reply=immediate.get("reply") if isinstance(immediate.get("reply"), str) else None,
            run_id=run_id,
            reason=immediate.get("reason") if isinstance(immediate.get("reason"), str) else None,
            raw=immediate,
        )

    return await _poll_run(run_id, budget_s=budget, secret=signing, base_url=base)


async def _poll_run(
    run_id: str,
    *,
    budget_s: float,
    secret: str,
    base_url: str,
) -> TurnResult:
    deadline = time.monotonic() + budget_s
    delay = 1.0
    path = f"/v1/runs/{run_id}"
    last: dict[str, Any] = {}

    while time.monotonic() < deadline:
        _status_code, last = await asyncio.to_thread(
            _http_json,
            "GET",
            path,
            body_obj=None,
            secret=secret,
            base_url=base_url,
            timeout_s=15.0,
        )
        run_status = str(last.get("status") or "")
        if run_status in TERMINAL_RUN_STATUSES:
            result = last.get("result") if isinstance(last.get("result"), dict) else {}
            turn_status = str(result.get("status") or ("failed" if run_status == "failed" else "ok"))
            return TurnResult(
                status=turn_status,
                reply=result.get("reply") if isinstance(result.get("reply"), str) else None,
                run_id=run_id,
                reason=result.get("reason") if isinstance(result.get("reason"), str) else None,
                raw=last,
            )
        await asyncio.sleep(delay)
        delay = min(10.0, delay * 2)

    raise QmClientError(
        f"QM run {run_id} timed out after {budget_s:.0f}s (last status={last.get('status')!r})"
    )


async def signal_run(
    run_id: str,
    *,
    kind: str,
    text: str | None = None,
    base_url: str | None = None,
    secret: str | None = None,
) -> dict[str, Any]:
    """POST /v1/runs/:id/signal with kind abort|steer (doc 55 §4.4)."""
    if kind not in {"abort", "steer"}:
        raise ValueError("kind must be 'abort' or 'steer'")
    base = (base_url or qm_settings.qm_base_url()).rstrip("/")
    signing = (secret if secret is not None else qm_settings.qm_source_signing_secret()).strip()
    if not signing:
        raise QmClientError("QM_SOURCE_SIGNING_SECRET is not set")
    body: dict[str, Any] = {"kind": kind}
    if text is not None:
        body["text"] = text
    _code, parsed = await asyncio.to_thread(
        _http_json,
        "POST",
        f"/v1/runs/{run_id}/signal",
        body_obj=body,
        secret=signing,
        base_url=base,
        timeout_s=15.0,
    )
    return parsed
