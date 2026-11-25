"""Ops API workspace membership and API-key gates (doc 130 OT-05 / OT-06 / OT-33)."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute
from app.authz.workspace import person_for_principal, workspace_for_person
from app.ops import config as ops_config
from app.tenancy import current_or_none
from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

_authorization: ContextVar[str | None] = ContextVar("ops_authorization", default=None)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


def bind_authorization(value: str | None):
    return _authorization.set(value or "")


def bound_ops_person() -> str | None:
    return resolve_ops_person() if _authorization.get() is not None else None


def reset_authorization(token) -> None:
    _authorization.reset(token)


class OpsAuthRoute(APIRoute):
    """Capture Authorization so hosted membership does not trust X-Workspace-Id alone."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            tok = bind_authorization(request.headers.get("authorization"))
            try:
                return await original(request)
            finally:
                reset_authorization(tok)

        return wrapped


def person_from_hosted_token(authorization: str | None) -> str | None:
    """Session JWT (post-Cognito) → Person. Fail closed: unknown token is None.

    Remaining hook: this does not call Cognito JWKS. Production hosted login still
    issues ``ACCOUNT_SESSION_SECRET`` HS256 after Cognito (see ``app.consumer.session``).
    Wire IdP JWT verification here when the consumer Cognito app client is configured.
    """
    presented = (authorization or "").strip()
    if not presented:
        return None
    try:
        from app.consumer.session import SessionError, parse_session

        principal = parse_session(presented)
    except (SessionError, ValueError, KeyError):
        return None
    return person_for_principal(principal)


def resolve_ops_person(authorization: str | None = None) -> str:
    scope = current_or_none()
    if scope is not None and scope.workspace_id != "none" and scope.actor != "anonymous":
        from app.authz.workspace import subject_person_id

        return subject_person_id(scope.actor)
    token = authorization if authorization is not None else _authorization.get()
    if not token:
        return LOCAL_PERSON_ID
    person = person_from_hosted_token(token)
    if person is None:
        raise _not_found()
    return person


def require_workspace(
    person_id: str,
    workspace_id: str | None,
    x_workspace_id: str | None,
) -> Any:
    """Membership-checked workspace. Unknown or non-member → 404 (not 403)."""
    person = (person_id or "").strip() or LOCAL_PERSON_ID
    selectors = {value.strip() for value in (workspace_id, x_workspace_id) if value and value.strip()}
    if len(selectors) > 1:
        raise HTTPException(status_code=400, detail={"code": "workspace_mismatch"})
    requested = next(iter(selectors), None)
    scope = current_or_none()
    if scope is not None and scope.workspace_id != "none":
        if requested and requested != scope.workspace_id:
            raise _not_found()
        requested = scope.workspace_id
    if requested is None:
        return ensure_desk(person)

    workspace = workspace_for_person(requested, person)
    if workspace is None:
        raise _not_found()
    return workspace


def require_ops_auth(x_api_key: str | None, tenancy: str) -> None:
    """Accept verified gateway scope; preserve local/legacy managed-key checks."""
    scope = current_or_none()
    if scope is not None and scope.workspace_id != "none" and scope.actor != "anonymous":
        return
    expected = ops_config.ops_api_key()
    if expected is None:
        if (tenancy or "").strip().lower() == "managed":
            raise HTTPException(
                status_code=401, detail="Invalid or missing X-API-Key"
            )
        return
    if not x_api_key or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
