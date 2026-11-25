"""Authorization-code flow: start, callback, refresh, revoke.

Access and refresh tokens go into the same encrypted vault the connectors use.
Only the ``vault:`` reference is ever written to the database, logged, or
returned from the API.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets as pysecrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse, urlencode

import httpx
from core.clock import as_utc, utcnow
from core.ids import new_id
from sqlalchemy import select

from app.connectors.secrets import store as secret_store
from app.content.oauth.providers import OAuthProvider, get_provider
from app.db.models import ContentAccount
from app.content.oauth import attempts
from app.content.oauth.attempts import OAuthError
from app.state import db

log = logging.getLogger(__name__)

STATE_TTL_SECONDS = 900
#: Refresh this far before actual expiry so a tick never posts with a dead token.
REFRESH_SKEW_SECONDS = 300


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def _origin(value: str, *, setting: str) -> str:
    from app.admin.authn import require_signed_caller

    parsed = urlparse(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or "\\" in value
            or parsed.scheme not in ({"https"} if require_signed_caller() else {"https", "http"})):
        raise OAuthError("invalid_public_url", f"{setting} must be a configured HTTPS origin.")
    return f"{parsed.scheme}://{parsed.netloc}"


def public_base_url() -> str:
    from app.connections import local_config
    from app.core.local_mode import is_local

    value = os.environ.get("CONTENT_OAUTH_BASE_URL") or os.environ.get("COMPANY_OS_PUBLIC_URL")
    if is_local():
        try:
            return local_config.public_url(value or console_origin())
        except ValueError as exc:
            raise OAuthError("invalid_public_url", str(exc)) from None

    return _origin(value or "http://127.0.0.1:3300", setting="CONTENT_OAUTH_BASE_URL")


def console_origin() -> str:
    from app.connections import local_config
    from app.core.local_mode import is_local

    value = os.environ.get("CONSOLE_URL") or os.environ.get("NEXT_PUBLIC_APP_URL")
    if is_local():
        try:
            return local_config.public_url(value or "http://127.0.0.1:3100")
        except ValueError as exc:
            raise OAuthError("invalid_public_url", str(exc)) from None

    return _origin(value or "http://127.0.0.1:3100", setting="CONSOLE_URL")


def _safe_return_to(return_to: str | None, *, workspace_id: str | None = None) -> str:
    # Connector callbacks land on the reviewed status page. A workspace selector
    # may survive only when it matches the signed authorization attempt.
    origin = console_origin()
    if return_to:
        parsed = urlparse(return_to)
        from urllib.parse import parse_qs, urlencode

        query = parse_qs(parsed.query, keep_blank_values=True)
        if ("\\" in return_to or parsed.fragment
                or parsed.path not in {"/content", "/gtm", "/connections"}
                or (parsed.netloc and f"{parsed.scheme}://{parsed.netloc}" != origin)
                or (parsed.scheme and not parsed.netloc)
                or set(query) - {"workspace", "venture"}
                or any(len(values) != 1 or not values[0] for values in query.values())
                or (query and (not workspace_id or query.get("workspace") != [workspace_id]))):
            raise OAuthError("invalid_return_to", "Return to a Supymem connection screen.")
        venture_id = query.get("venture", [None])[0]
        if venture_id:
            from app.db.models import Venture
            from app.state import db

            with db()() as session:
                venture = session.get(Venture, venture_id)
                if venture is None or venture.workspace_id != workspace_id:
                    raise OAuthError("invalid_return_to", "Return to a Supymem connection screen.")
        if query:
            return origin + "/connections?" + urlencode({key: query[key][0] for key in ("workspace", "venture") if key in query})
    return origin + "/connections"


def default_redirect_uri(platform: str) -> str:
    expected = f"{public_base_url()}/api/v1/content/oauth/{platform}/callback"
    override = os.environ.get(f"{platform.upper()}_REDIRECT_URI", "").strip()
    if override and override != expected:
        raise OAuthError("invalid_redirect_uri", "Provider redirect must match the configured Supymem callback.")
    return expected


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _basic_header(provider: OAuthProvider) -> str:
    raw = f"{provider.client_id()}:{provider.client_secret()}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _headers(provider: OAuthProvider, *, token: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if provider.user_agent:
        headers["User-Agent"] = provider.user_agent
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ---------------------------------------------------------------------------
# Step 1 — send the founder to the platform
# ---------------------------------------------------------------------------


def start_authorization(
    *,
    workspace_id: str,
    platform: str,
    scopes: tuple[str, ...] | None = None,
    return_to: str | None = None,
    prefer_composio: bool = True,
) -> dict[str, str]:
    return_to = _safe_return_to(return_to, workspace_id=workspace_id)
    attempts.initiating_actor(workspace_id)
    if prefer_composio:
        from app.content import composio_social
        from app.content.oauth.callbacks import start_managed

        if composio_social.connectable(platform):
            return start_managed(workspace_id=workspace_id, platform=platform, return_to=return_to)

    provider = get_provider(platform)
    if not provider.configured():
        raise OAuthError("not_configured", f"Configure {provider.label} before connecting.")
    from app.admin.authn import require_signed_caller

    if require_signed_caller() and not provider.use_pkce:
        raise OAuthError("managed_auth_required", "Use managed authorization for this provider in hosted mode.")
    redirect_uri = default_redirect_uri(provider.name)
    verifier, challenge = _pkce_pair() if provider.use_pkce else (None, None)
    state = attempts.create(workspace_id=workspace_id, platform=provider.name,
                            redirect_uri=redirect_uri, return_to=return_to,
                            auth_method="native", verifier=verifier)

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": provider.client_id(),
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": provider.scope_value(scopes),
        **dict(provider.extra_authorize_params),
    }
    if challenge:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"

    return {
        "authorize_url": f"{provider.authorize_url}?{urlencode(params)}",
        "state": state,
        "platform": provider.name,
        "redirect_uri": redirect_uri,
    }


# ---------------------------------------------------------------------------
# Step 2 — the platform sends them back
# ---------------------------------------------------------------------------


def _exchange(
    provider: OAuthProvider,
    *,
    form: dict[str, str],
) -> dict[str, Any]:
    headers = _headers(provider)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    if provider.token_auth == "basic":
        headers["Authorization"] = _basic_header(provider)
    else:
        form = {
            **form,
            "client_id": provider.client_id(),
            "client_secret": provider.client_secret(),
        }
    with httpx.Client(timeout=30.0) as client:
        res = client.post(provider.token_url, data=form, headers=headers)
    try:
        payload = res.json()
    except Exception:
        payload = {"raw": res.text[:400]}
    if res.status_code >= 400 or not isinstance(payload, dict):
        detail = ""
        if isinstance(payload, dict):
            detail = str(
                payload.get("error_description")
                or payload.get("error")
                or payload.get("message")
                or ""
            )
        raise OAuthError(
            "token_exchange_failed",
            detail or f"{provider.label} token endpoint returned {res.status_code}.",
        )
    if not payload.get("access_token"):
        raise OAuthError(
            "token_exchange_failed",
            f"{provider.label} did not return an access token.",
        )
    return payload


def _fetch_identity(provider: OAuthProvider, access_token: str) -> dict[str, str]:
    with httpx.Client(timeout=30.0) as client:
        res = client.get(
            provider.identity_url, headers=_headers(provider, token=access_token)
        )
    if res.status_code >= 400:
        raise OAuthError(
            "identity_failed",
            f"{provider.label} identity lookup returned {res.status_code}.",
        )
    data = res.json() if res.content else {}
    if not isinstance(data, dict):
        data = {}

    if provider.name == "linkedin":
        sub = str(data.get("sub") or "")
        return {
            "external_account": sub,
            "handle": str(data.get("name") or data.get("given_name") or ""),
            "author_urn": f"urn:li:person:{sub}" if sub else "",
        }
    if provider.name == "reddit":
        name = str(data.get("name") or "")
        return {
            "external_account": str(data.get("id") or name),
            "handle": f"u/{name}" if name else "",
            "author_urn": "",
        }
    if provider.name == "x":
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        username = str(inner.get("username") or "")
        return {
            "external_account": str(inner.get("id") or username),
            "handle": f"@{username}" if username else "",
            "author_urn": "",
        }
    return {"external_account": "", "handle": "", "author_urn": ""}


def complete_authorization(*, state: str, code: str, platform: str) -> dict[str, Any]:
    return complete_attempt(attempts.consume(state, platform), code=code)


def complete_attempt(attempt: dict[str, Any], *, code: str) -> dict[str, Any]:
    if attempt["auth_method"] != "native":
        raise OAuthError("invalid_method", "Authorization method does not match.")
    workspace_id, platform = attempt["workspace_id"], attempt["platform"]
    redirect_uri, return_to = attempt["redirect_uri"], attempt["return_to"]
    verifier = attempts.verifier(attempt)
    provider = get_provider(platform)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if verifier:
        form["code_verifier"] = verifier
    # X requires client_id in the body even when authenticating with Basic.
    if provider.use_pkce and provider.token_auth == "basic":
        form["client_id"] = provider.client_id()

    payload = _exchange(provider, form=form)
    access = str(payload["access_token"])
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    granted = payload.get("scope") or provider.scope_value()

    identity = _fetch_identity(provider, access)
    account = identity["external_account"]
    if not account:
        raise OAuthError("identity_missing", "Provider did not verify an account identity.")

    view = _upsert_account(
        workspace_id=workspace_id,
        provider=provider,
        external_account=account,
        handle=identity["handle"],
        author_urn=identity["author_urn"],
        access_token=access,
        refresh_token=str(refresh) if refresh else None,
        expires_in=int(expires_in) if isinstance(expires_in, (int, float)) else None,
        scopes=granted,
    )
    view["return_to"] = return_to or ""
    return view


def _upsert_account(
    *,
    workspace_id: str,
    provider: OAuthProvider,
    external_account: str,
    handle: str,
    author_urn: str,
    access_token: str,
    refresh_token: str | None,
    expires_in: int | None,
    scopes: str,
) -> dict[str, Any]:
    import json

    vault = secret_store()
    new_refs: list[str] = []
    try:
        access_ref = vault.put(workspace_id=workspace_id, provider=f"content:{provider.name}",
                               key="access_token", value=access_token)
        new_refs.append(access_ref)
        refresh_ref = None
        if refresh_token:
            refresh_ref = vault.put(workspace_id=workspace_id, provider=f"content:{provider.name}",
                                    key="refresh_token", value=refresh_token)
            new_refs.append(refresh_ref)
    except Exception:
        for ref in new_refs:
            vault.forget(ref)
        raise

    now = utcnow()
    expires_at = now + timedelta(seconds=expires_in) if expires_in else None
    scope_list = [s for s in scopes.replace(",", " ").split() if s]

    old_refs: list[str] = []
    committed = False
    try:
        factory = db()
        with factory() as session:
            row = session.scalars(
                select(ContentAccount).where(
                    ContentAccount.workspace_id == workspace_id,
                    ContentAccount.platform == provider.name,
                    ContentAccount.external_account == external_account,
                )
            ).first()
            if row is None:
                row = ContentAccount(
                    id=new_id("cac"),
                    workspace_id=workspace_id,
                    platform=provider.name,
                    external_account=external_account,
                    daily_cap=provider.daily_cap_default,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                old_refs = [ref for ref in (row.access_secret_ref, row.refresh_secret_ref) if ref]
            row.handle = handle or row.handle
            row.author_urn = author_urn or row.author_urn
            row.status = "active"
            row.access_secret_ref = access_ref
            row.refresh_secret_ref = refresh_ref
            row.scopes_json = json.dumps(scope_list)
            row.expires_at = expires_at
            row.health = "ok"
            row.paused_reason = None
            row.last_error = None
            row.updated_at = now
            session.commit()
            committed = True
            session.refresh(row)
            view = account_view(row)
    except Exception:
        for ref in ([] if committed else new_refs):
            try:
                vault.forget(ref)
            except Exception:
                log.warning("connector.oauth.vault_cleanup_failed")
        raise
    # Preserve the old working credentials until the replacement commits.
    for ref in old_refs:
        try:
            vault.forget(ref)
        except Exception:
            log.warning("connector.oauth.vault_cleanup_failed")
    return view


# ---------------------------------------------------------------------------
# Token access with transparent refresh
# ---------------------------------------------------------------------------


def access_token(account_id: str, *, requester: str = "content-engine") -> str:
    factory = db()
    with factory() as session:
        row = session.get(ContentAccount, account_id)
        if row is None:
            raise OAuthError("no_account", "That publishing account is not connected.")
        if row.status not in {"active", "expired"}:
            raise OAuthError(
                "account_inactive", f"That account is {row.status}, not active."
            )
        needs_refresh = bool(
            row.expires_at
            and as_utc(row.expires_at) <= utcnow() + timedelta(seconds=REFRESH_SKEW_SECONDS)
        )
        ref = row.access_secret_ref
        platform = row.platform

    if needs_refresh:
        refreshed = refresh_account(account_id)
        if refreshed:
            with factory() as session:
                row2 = session.get(ContentAccount, account_id)
                ref = row2.access_secret_ref if row2 else ref

    if not ref:
        raise OAuthError("no_token", f"No stored {platform} token for that account.")
    try:
        return secret_store().get(ref, requester=requester)
    except KeyError as exc:
        raise OAuthError("no_token", "The stored token is gone; reconnect.") from exc


def refresh_account(account_id: str) -> bool:
    """Swap a refresh token for a fresh access token. False when not possible."""
    factory = db()
    with factory() as session:
        row = session.get(ContentAccount, account_id)
        if row is None or not row.refresh_secret_ref:
            return False
        provider = get_provider(row.platform)
        workspace_id = row.workspace_id
        refresh_ref = row.refresh_secret_ref
        old_access_ref = row.access_secret_ref

    vault = secret_store()
    try:
        refresh_token = vault.get(refresh_ref, requester="content-engine")
    except KeyError:
        return False

    try:
        payload = _exchange(
            provider,
            form={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
    except OAuthError as exc:
        _mark_account_error(account_id, f"refresh_failed: {exc.message}")
        return False

    access = str(payload["access_token"])
    expires_in = payload.get("expires_in")
    new_refresh = payload.get("refresh_token")

    access_ref = vault.put(
        workspace_id=workspace_id,
        provider=f"content:{provider.name}",
        key="access_token",
        value=access,
    )
    new_refresh_ref = None
    if new_refresh and str(new_refresh) != refresh_token:
        new_refresh_ref = vault.put(
            workspace_id=workspace_id,
            provider=f"content:{provider.name}",
            key="refresh_token",
            value=str(new_refresh),
        )

    now = utcnow()
    with factory() as session:
        row = session.get(ContentAccount, account_id)
        if row is None:
            return False
        row.access_secret_ref = access_ref
        if new_refresh_ref:
            row.refresh_secret_ref = new_refresh_ref
        row.expires_at = (
            now + timedelta(seconds=int(expires_in))
            if isinstance(expires_in, (int, float))
            else None
        )
        row.status = "active"
        row.health = "ok"
        row.last_error = None
        row.updated_at = now
        session.commit()

    for stale in (old_access_ref, refresh_ref if new_refresh_ref else None):
        if stale:
            try:
                vault.forget(stale)
            except Exception:
                pass
    return True


def _mark_account_error(account_id: str, reason: str) -> None:
    factory = db()
    with factory() as session:
        row = session.get(ContentAccount, account_id)
        if row is None:
            return
        row.status = "expired"
        row.health = "down"
        row.last_error = reason[:500]
        row.updated_at = utcnow()
        session.commit()


def revoke_account(account_id: str, *, workspace_id: str, expected_connection_id: str | None = None) -> dict[str, Any]:
    # Credential mutation uses the same signed administrator boundary as
    # connection creation, including callers outside the HTTP route.
    attempts.initiating_actor(workspace_id)
    from app.content import composio_social
    from app.credits import metered
    from app.db.session import serialize_writes

    factory = db()
    with metered.charge_sync(workspace_id=workspace_id, purpose="connections.disconnect", provider="composio", estimate_micro=0, payer="founder") as meter:
        with factory() as session:
            serialize_writes(session, key="connection-revoke:" + account_id)
            row = session.scalar(select(ContentAccount).where(
                ContentAccount.id == account_id, ContentAccount.workspace_id == workspace_id,
            ).with_for_update())
            if row is None:
                from app.connections import toolkits

                revoked = toolkits.revoke_account(session, workspace_id, account_id, expected_connection_id=expected_connection_id)
                if revoked is not None:
                    meter.record_micro(0)
                    return revoked
                raise OAuthError("no_account", "That publishing account is not connected.")
            if row.status == "revoked":
                meter.record_micro(0)
                return account_view(row)
            vault = secret_store()
            try:
                extra = json.loads(row.extra_json or "{}")
            except Exception:
                extra = {}
            composio_ca = ""
            if isinstance(extra, dict) and extra.get("auth_via") == "composio":
                composio_ca = str(extra.get("connected_account_id") or "")
            if expected_connection_id is not None and expected_connection_id != (composio_ca or f"native:{row.auth_generation}"):
                raise OAuthError("connection_changed_review_again", "Your connection changed. Review it again before disconnecting.")
            if composio_ca:
                composio_social.delete_connection(composio_ca, revoke=True)
            for ref in (row.access_secret_ref, row.refresh_secret_ref):
                if ref:
                    try:
                        vault.forget(ref)
                    except Exception:
                        pass
            row.status = "revoked"
            row.access_secret_ref = None
            row.refresh_secret_ref = None
            row.health = "down"
            row.extra_json = "{}"
            row.author_urn = None
            row.handle = None
            row.last_error = None
            row.paused_reason = "revoked by founder"
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            view = account_view(row)
        meter.record_micro(0)
    return view


def forget_all(workspace_id: str) -> dict[str, Any]:
    """Full reset: revoke every content account and wipe Composio links."""
    from app.content import composio_social

    attempts.initiating_actor(workspace_id)
    factory = db()
    revoked: list[str] = []
    with factory() as session:
        rows = session.scalars(
            select(ContentAccount).where(
                ContentAccount.workspace_id == workspace_id,
                ContentAccount.status != "revoked",
            )
        ).all()
        ids = [r.id for r in rows]
    for account_id in ids:
        try:
            revoke_account(account_id, workspace_id=workspace_id)
            revoked.append(account_id)
        except OAuthError:
            continue

    composio_removed: list[str] = []
    if composio_social.configured():
        try:
            composio_removed = composio_social.forget_workspace_connections(workspace_id)
        except Exception as exc:
            log.warning("composio workspace forget failed: %s", exc)

    return {
        "revoked_accounts": revoked,
        "composio_removed": composio_removed,
        "accounts": list_accounts(workspace_id),
    }


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def account_view(row: ContentAccount) -> dict[str, Any]:
    try:
        scopes = json.loads(row.scopes_json or "[]")
    except Exception:
        scopes = []
    try:
        extra = json.loads(row.extra_json or "{}")
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    return {
        "id": row.id,
        "platform": row.platform,
        "handle": row.handle or row.external_account,
        "external_account": row.external_account,
        "status": row.status,
        "health": row.health,
        "scopes": scopes if isinstance(scopes, list) else [],
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "daily_cap": row.daily_cap,
        "posts_today": row.posts_today,
        "paused_reason": row.paused_reason,
        "last_error": row.last_error,
        "last_post_at": row.last_post_at.isoformat() if row.last_post_at else None,
        "auth_via": extra.get("auth_via") or "native",
        "author_urn": row.author_urn,
    }


def list_accounts(workspace_id: str) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ContentAccount)
            .where(
                ContentAccount.workspace_id == workspace_id,
                ContentAccount.status != "revoked",
            )
            .order_by(ContentAccount.platform)
        ).all()
        return [account_view(r) for r in rows]


def active_account(workspace_id: str, platform: str) -> dict[str, Any] | None:
    """The connected account for a platform, as a plain dict.

    Callers read these fields long after the session closes, so nothing
    detached is handed back.
    """
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(ContentAccount)
            .where(
                ContentAccount.workspace_id == workspace_id,
                ContentAccount.platform == platform,
                ContentAccount.status == "active",
            )
            .order_by(ContentAccount.updated_at.desc())
        ).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "platform": row.platform,
            "handle": row.handle,
            "author_urn": row.author_urn,
            "daily_cap": row.daily_cap,
            "posts_today": row.posts_today,
            "day_key": row.day_key,
            "last_post_at": row.last_post_at,
            "extra_json": row.extra_json or "{}",
        }
