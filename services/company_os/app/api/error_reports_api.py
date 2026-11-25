"""Founder "Report a problem" (signed in) and the operator error triage views (admin)."""

from __future__ import annotations

from app.error_reports import service as _service


from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.admin.deps import AdminContext, require_admin

router = APIRouter(tags=["error-reports"])
# ponytail: in-process bucket per task; move to the gateway/Redis limiter if core scales out.
_LIMIT = _service._LIMIT
MAX_SCREENSHOT = _service.MAX_SCREENSHOT
_IMAGE_MAGIC = _service._IMAGE_MAGIC


class ProblemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=3, max_length=2000)
    page: str = Field(default="", max_length=120, pattern=r"^(/[A-Za-z0-9_{}./-]{0,119})?$")
    app_version: str = Field(default="unknown", max_length=32, pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    venture_id: str | None = Field(default=None, max_length=128)
    # The screenshot is only ever stored when the founder explicitly attached it and ticked consent.
    screenshot_b64: str | None = Field(default=None, max_length=2_100_000)
    screenshot_consent: bool = False


@router.post("/problem-reports")
def report_problem(body: ProblemBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return _service.problems_report_problem(body, x_subject)


@router.get("/local/support-bundle")
def download_local_support_bundle():
    from app.core.local_mode import is_local

    if not is_local():
        raise HTTPException(404, "Open your local desktop app to download a support bundle.")
    from desktop_runtime.crash_report import local_crash_summaries

    try:
        crashes = local_crash_summaries()
    except OSError:
        raise HTTPException(503, "Check your app data folder permissions, then choose Download support bundle again.") from None
    return JSONResponse({"desktop_crashes": crashes}, headers={
        "Cache-Control": "no-store",
        "Content-Disposition": 'attachment; filename="supymem-support.json"',
    })


def _emails(session, ids: set[str]) -> dict[str, str]:
    return _service.problems_emails(session, ids)


@router.get("/admin/errors")
def admin_errors(status: str = "open", limit: int = 100, ctx: AdminContext = Depends(require_admin("audit.view"))):
    return _service.problems_admin_errors(status, limit, ctx)


@router.post("/admin/errors/{error_id}/resolve")
def admin_resolve(error_id: str, ctx: AdminContext = Depends(require_admin("ops.flags"))):
    return _service.problems_admin_resolve(error_id, ctx)


@router.get("/admin/problem-reports")
def admin_problem_reports(limit: int = 50, ctx: AdminContext = Depends(require_admin("audit.view"))):
    return _service.problems_admin_problem_reports(limit, ctx)


@router.get("/admin/problem-reports/{report_id}/screenshot")
def admin_screenshot(report_id: str, ctx: AdminContext = Depends(require_admin("audit.view"))):
    return _service.problems_admin_screenshot(report_id, ctx)
