from fastapi import APIRouter, Header, Response
from pydantic import BaseModel

from app.coding_agents_consent import check_connection, set_consent
from app.ui_config import ui_config

router = APIRouter(tags=["founder UI"])


@router.get("/ui-config")
def get_config(response: Response, x_subject: str | None = Header(default=None, alias="X-Subject")):
    response.headers["Cache-Control"] = "no-store"
    return ui_config(x_subject)


class CodingToolBody(BaseModel):
    provider: str
    candidate: str | None = None


class CodingConsentBody(CodingToolBody):
    allow: bool


@router.post("/ui-config/coding-agents/consent")
def post_coding_consent(body: CodingConsentBody, response: Response,
                        x_subject: str | None = Header(default=None, alias="X-Subject")):
    response.headers["Cache-Control"] = "no-store"
    return {"consent": set_consent(x_subject, body.provider, body.allow, body.candidate or "")}


@router.post("/ui-config/coding-agents/check")
def post_coding_check(body: CodingToolBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    check_connection(x_subject, body.provider, body.candidate)
    return {"ok": True}
