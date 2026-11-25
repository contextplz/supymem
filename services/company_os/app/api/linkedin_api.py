"""LinkedIn people page and the Mac's Google-results hand-off. Supymem never fetches LinkedIn."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.admin.authn import verify_caller
from app.core.local_mode import is_local
from app.gtm import linkedin_people as people

router = APIRouter(tags=["linkedin-people"])


class MarkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sent: bool | None = None
    replied: bool | None = None


class ResultPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=600)
    final_url: str = Field(default="", max_length=2000)
    html: str = Field(default="", max_length=people.MAX_PAGE_BYTES)


class ResultsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pages: list[ResultPage] = Field(default_factory=list, max_length=5)
    captcha: bool = False


@router.get("/ventures/{venture_id}/linkedin")
def linkedin_people(venture_id: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    subject = verify_caller(request).sub
    return people.local_view(venture_id, subject) if is_local() else people.view(venture_id, subject)


@router.post("/ventures/{venture_id}/linkedin/find")
async def find_more(venture_id: str, request: Request):
    subject = verify_caller(request).sub
    return await people.local_find(venture_id, subject) if is_local() else people.request(venture_id, subject)


@router.patch("/ventures/{venture_id}/linkedin/people/{person_id}")
def mark_person(venture_id: str, person_id: str, body: MarkBody, request: Request):
    subject = verify_caller(request).sub
    if not is_local():
        return people.mark(venture_id, subject, person_id, sent=body.sent, replied=body.replied)
    return people.local_mark(venture_id, subject, person_id, sent=body.sent, replied=body.replied)


@router.get("/desktop/linkedin-searches/next")
def next_search(request: Request):
    # Founder rule: research is invisible. Only Mac builds that search headless send ?headless=1;
    # older builds (visible Chrome window) get no work.
    if request.query_params.get("headless") != "1":
        verify_caller(request, required=True)
        return {"order": None}
    return people.next_order(verify_caller(request, required=True))


@router.post("/ventures/{venture_id}/linkedin/searches/{order_id}/results")
async def search_results(venture_id: str, order_id: str, body: ResultsBody, request: Request):
    return await people.record_results(verify_caller(request, required=True), venture_id, order_id,
                                       [page.model_dump() for page in body.pages], body.captcha)
