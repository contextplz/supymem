"""Loopback surface for the Tauri shell (notices, no secrets)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.desktop_notices import ack, pending

router = APIRouter(tags=["desktop"])


class AckBody(BaseModel):
    ids: list[str] = Field(default_factory=list)


class BugReportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    contact: str | None = Field(default=None, max_length=200)
    app_version: str | None = Field(default=None, max_length=40)
    os: str | None = Field(default=None, max_length=120)
    include_account_id: bool = False


@router.post("/desktop/bug-report", status_code=201)
def submit_bug_report(body: BugReportBody, request: Request) -> dict[str, str]:
    from app.admin.authn import verify_caller
    from app.consumer.reports import ReportError, ingest

    caller = verify_caller(request, required=True)
    try:
        report = ingest(
            kind="user", title=body.title, body=body.body, contact=body.contact,
            app_version=body.app_version or "unknown", os_name=body.os or "unknown",
            account_id=caller.sub if body.include_account_id else None,
            anonymous=not body.include_account_id,
        )
    except ReportError as exc:
        raise HTTPException(
            503 if exc.code == "ingest_disabled" else 422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    from app.analytics.funnel import emit_for_venture
    emit_for_venture("feedback.submitted", None, key=f"report:{report['id']}", person_id=caller.sub if body.include_account_id else None,  # anonymous reports stay anonymous
                     props={"kind": "bug_report"})
    return {"id": report["id"], "status": "received"}


@router.get("/desktop/notices")
def get_notices(request: Request):
    return {"notices": pending(workspace_ids=_visible_workspaces(request), person_id=_recipient_person(request))}


@router.post("/desktop/notices/ack")
def ack_notices(body: AckBody, request: Request):
    return {"acked": ack(body.ids, workspace_ids=_visible_workspaces(request), person_id=_recipient_person(request))}


def _recipient_person(request: Request) -> str | None:
    from app.authz.workspace import subject_person_id

    subject = request.headers.get("X-Subject")
    return subject_person_id(subject) if subject else None


def _visible_workspaces(request: Request) -> set[str] | None:
    from app.admin.authn import require_signed_caller
    from app.authz.workspace import accessible_workspace_ids

    subject = request.headers.get("X-Subject")
    if not require_signed_caller() and not subject:
        return None  # The local tray may also receive machine-wide notices.
    return accessible_workspace_ids(subject)
