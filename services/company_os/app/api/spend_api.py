"""Founder spend view and per-experiment caps (flag ``spend_view_v1``; 404 while off)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.routing import APIRoute

from app.authz.roles import role_allows
from app.authz.workspace import assert_venture_access, subject_person_id
from app.workspaces.service import role_of
from app.credits import spend
from app.core.local_mode import is_local

_NOT_FOUND = {"error": {"code": "not_found", "message": "Not found."}}


class _SpendRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def gated_handler(request):
            if not spend.enabled():
                raise HTTPException(404, detail=_NOT_FOUND)
            return await handler(request)

        return gated_handler


router = APIRouter(tags=["credits"], route_class=_SpendRoute)


def _scope(venture_id: str, subject: str | None) -> str:
    return assert_venture_access(venture_id, subject)[1]


@router.get("/local/spend")
def local_spend_view():
    if not is_local():
        raise HTTPException(404, detail=_NOT_FOUND)
    from app.credits.errors import CreditsUnavailable
    from app.credits.local_meter import snapshot

    try:
        report = snapshot()
    except CreditsUnavailable:
        raise HTTPException(503, "Open Keys and connections, check Your monthly spend cap and your data folder permissions, then choose Refresh spending.") from None
    return {name: report[name] for name in ("month", "month_cost_usd_micro", "monthly_cap_usd_micro")}


@router.get("/ventures/{venture_id}/spend")
def spend_view(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return spend.view(_scope(venture_id, x_subject), venture_id)


@router.put("/ventures/{venture_id}/spend/experiments/{experiment_id}/cap")
def set_experiment_cap(venture_id: str, experiment_id: str, body: dict = Body(...),
                       x_subject: str | None = Header(default=None, alias="X-Subject")):
    workspace_id = _scope(venture_id, x_subject)
    role = role_of(workspace_id, subject_person_id(x_subject))  # None only for the owner; access is checked above
    if role is not None and not role_allows(role, "change_autonomy"):
        raise HTTPException(403, detail={"error": {"code": "change_autonomy_required"}})
    cap = body.get("cap_micro")
    try:
        return spend.set_cap(workspace_id, venture_id, experiment_id, cap, stop=body.get("stop") is True)
    except KeyError:
        detail = {"error": {"code": "not_found", "message": "Open your venture's Experiments screen, choose a saved experiment, then set its spending limit again."}} if is_local() else _NOT_FOUND
        raise HTTPException(404, detail=detail) from None
    except ValueError:
        raise HTTPException(422, detail={"error": {"code": "spend_cap_invalid",
            "message": "Set a cap above $0 and up to $100,000."}}) from None
