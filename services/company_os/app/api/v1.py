"""Public REST surface at /api/v1 (41 §4).

Versioned separately from the console's internal routes so the console can
evolve without breaking integrators. Every handler is: authenticate, check the
four-way minimum, call a service, audit. No logic lives here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.audit.writer import audit, use_actor
from app.surfaces import idempotency
from app.surfaces.auth import Principal, resolve_principal
from app.surfaces.errors import SurfaceError, error_body
from app.surfaces.ratelimit import check_rate

router = APIRouter(prefix="/api/v1", tags=["v1"])

MAX_LIMIT = 100


class CreateEngagement(BaseModel):
    workspace_id: str
    request: str = Field(min_length=3, max_length=4000)


class AnswerBody(BaseModel):
    question_id: str
    option_id: str | None = None
    other_text: str | None = None


class DecideBody(BaseModel):
    decision: str
    note: str | None = None


def _fail(exc: SurfaceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())


def _auth(authorization: str | None) -> Principal:
    return resolve_principal(authorization)


def _rate(principal: Principal, response: Response) -> None:
    verdict = check_rate(f"token:{principal.token_id}")
    response.headers["X-RateLimit-Limit"] = str(verdict.limit)
    response.headers["X-RateLimit-Remaining"] = str(verdict.remaining)
    response.headers["X-RateLimit-Reset"] = str(verdict.reset_after_s)
    if not verdict.allowed:
        raise SurfaceError(
            "rate_limited",
            "You're sending requests faster than this token allows.",
            status=429,
            hint=f"Retry in {verdict.retry_after_s}s.",
            retryable=True,
        )


def _deny(principal: Principal, capability: str, workspace_id: str) -> None:
    perm = principal.may(capability, workspace_id=workspace_id)
    if perm.allowed:
        return
    audit(
        "surface.denied",
        workspace_id=workspace_id,
        surface="api",
        target_ref=workspace_id,
        outcome="denied",
        denied_reason=perm.audit_reason(),
        payload={"capability": capability, "token": principal.token_id},
    )
    raise SurfaceError(
        "forbidden",
        perm.reason,
        status=403,
        hint="This token's scopes or your role don't allow that.",
    )


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


@router.get("/workspaces")
def list_workspaces(response: Response, authorization: str | None = Header(default=None)):
    try:
        principal = _auth(authorization)
        _rate(principal, response)
    except SurfaceError as exc:
        return _fail(exc)

    from app.workspaces.service import list_workspaces as svc

    with use_actor(principal.actor):
        items = [w.to_dict() for w in svc(principal.person_id) if not w.settings.get("blocked")]
        if principal.workspace_id:
            items = [w for w in items if w["id"] == principal.workspace_id]
    return {"items": items}


@router.get("/workspaces/{workspace_id}/status")
def get_status(
    workspace_id: str, response: Response, authorization: str | None = Header(default=None)
):
    try:
        principal = _auth(authorization)
        _rate(principal, response)
        _deny(principal, "read", workspace_id)
    except SurfaceError as exc:
        return _fail(exc)

    from app.surfaces.status import workspace_status

    with use_actor(principal.actor):
        return workspace_status(workspace_id)


@router.get("/workspaces/{workspace_id}/activity")
def get_activity(
    workspace_id: str,
    response: Response,
    limit: int = 50,
    cursor: int | None = None,
    authorization: str | None = Header(default=None),
):
    try:
        principal = _auth(authorization)
        _rate(principal, response)
        _deny(principal, "read", workspace_id)
    except SurfaceError as exc:
        return _fail(exc)

    from app.surfaces.status import workspace_activity

    with use_actor(principal.actor):
        return workspace_activity(
            workspace_id, limit=min(limit, MAX_LIMIT), cursor=cursor
        )


@router.get("/workspaces/{workspace_id}/approvals")
def list_approvals(
    workspace_id: str,
    response: Response,
    status: str = "pending",
    authorization: str | None = Header(default=None),
):
    try:
        principal = _auth(authorization)
        _rate(principal, response)
        _deny(principal, "read", workspace_id)
    except SurfaceError as exc:
        return _fail(exc)

    from app.approvals import service as approvals

    with use_actor(principal.actor):
        return {
            "items": [a.to_dict() for a in approvals.list_approvals(workspace_id, status=status)]
        }


@router.get("/engagements/{engagement_id}")
def get_engagement(
    engagement_id: str,
    response: Response,
    authorization: str | None = Header(default=None),
):
    try:
        principal = _auth(authorization)
        _rate(principal, response)
    except SurfaceError as exc:
        return _fail(exc)

    from app.surfaces.work import read_engagement

    with use_actor(principal.actor):
        found = read_engagement(engagement_id)
    if found is None:
        return JSONResponse(
            status_code=404,
            content=error_body(
                "not_found", "No engagement with that id.", hint="Check the id."
            ),
        )
    try:
        _deny(principal, "read", found["workspace_id"])
    except SurfaceError as exc:
        return _fail(exc)
    return found


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


@router.post("/engagements", status_code=201)
async def create_engagement(
    body: CreateEngagement,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        principal = _auth(authorization)
        _rate(principal, response)
        _deny(principal, "request_work", body.workspace_id)
        if not idempotency_key:
            # Required, not optional: a client retry must not create a second
            # engagement (41 §4).
            raise SurfaceError(
                "idempotency_key_required",
                "Creating work needs an Idempotency-Key header.",
                status=400,
                hint="Send a unique Idempotency-Key per logical request.",
            )
    except SurfaceError as exc:
        return _fail(exc)

    scope = f"v1.engagements:{principal.token_id}"
    replay = idempotency.replay_or_reserve(scope, idempotency_key)
    if replay.in_flight:
        return _fail(
            SurfaceError(
                "in_flight",
                "That request is still being processed.",
                status=409,
                hint="Poll the engagement instead of retrying.",
                retryable=True,
            )
        )
    if replay.found:
        response.headers["Idempotency-Replayed"] = "true"
        return JSONResponse(
            status_code=replay.status_code, content=replay.body or {}
        )

    from app.surfaces.work import start_engagement

    try:
        with use_actor(principal.actor):
            result = await start_engagement(
                workspace_id=body.workspace_id,
                request_text=body.request,
                requested_by=principal.person_id,
                origin="api",
                origin_ref=principal.token_id,
            )
    except SurfaceError as exc:
        idempotency.release(scope, idempotency_key)
        return _fail(exc)
    except Exception:
        idempotency.release(scope, idempotency_key)
        raise

    idempotency.store_response(scope, idempotency_key, result, status_code=201)
    return result


@router.post("/approvals/{approval_id}/decide")
def decide(
    approval_id: str,
    body: DecideBody,
    response: Response,
    authorization: str | None = Header(default=None),
):
    try:
        principal = _auth(authorization)
        _rate(principal, response)
    except SurfaceError as exc:
        return _fail(exc)

    from app.approvals import service as approvals

    view = approvals.get_approval(approval_id)
    if view is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", "No approval with that id."),
        )
    try:
        _deny(principal, "approve", view.workspace_id)
    except SurfaceError as exc:
        return _fail(exc)

    try:
        with use_actor(principal.actor):
            decided = approvals.decide(
                approval_id,
                decision=body.decision,
                decided_by=principal.person_id,
                note=body.note,
                surface="api",
            )
    except approvals.ApprovalError as exc:
        return JSONResponse(
            status_code=409 if exc.code in {"already_decided", "expired"} else 422,
            content=error_body(exc.code, exc.message),
        )
    return decided.to_dict()


# ---------------------------------------------------------------------------
# capabilities a token may never hold (41 §8)
# ---------------------------------------------------------------------------

_HUMAN_ONLY: dict[str, str] = {
    "autonomy": "changing autonomy",
    "connections": "connecting a system",
    "offboard": "offboarding a workspace",
}


@router.post("/workspaces/{workspace_id}/{human_only}")
def refuse_human_only(
    workspace_id: str,
    human_only: str,
    authorization: str | None = Header(default=None),
) -> Any:
    """Explicit 403 rather than a 404, so integrators learn the boundary."""
    what = _HUMAN_ONLY.get(human_only)
    if what is None:
        return JSONResponse(
            status_code=404, content=error_body("not_found", "No such endpoint.")
        )
    try:
        principal = _auth(authorization)
    except SurfaceError as exc:
        return _fail(exc)
    audit(
        "surface.denied",
        workspace_id=workspace_id,
        surface="api",
        target_ref=workspace_id,
        outcome="denied",
        denied_reason=f"{what} needs a signed-in human, not an API token",
        payload={"token": principal.token_id, "attempted": human_only},
    )
    return JSONResponse(
        status_code=403,
        content=error_body(
            "human_required",
            f"{what.capitalize()} needs a signed-in person, not an API token.",
            hint="Do this in the console.",
        ),
    )
