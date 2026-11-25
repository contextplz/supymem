from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from app.ventures import company_home

router = APIRouter(tags=["company home"])


class PinBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    department: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00-\x1f\x7f]+$")
    pinned: bool


@router.get("/ventures/{venture_id}/ui-pins")
def pins(venture_id: str, response: Response, x_subject: str | None = Header(default=None, alias="X-Subject")):
    response.headers["Cache-Control"] = "private, no-store"
    return company_home.read_pins(venture_id, x_subject)


@router.post("/ventures/{venture_id}/ui-pins")
def pin(venture_id: str, body: PinBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return company_home.change_pin(venture_id, x_subject, body.department, body.pinned)


@router.get("/ventures/{venture_id}/ui-archive")
def archive(venture_id: str, response: Response, x_subject: str | None = Header(default=None, alias="X-Subject")):
    response.headers["Cache-Control"] = "private, no-store"
    return company_home.archive(venture_id, x_subject)


@router.get("/ventures/{venture_id}/ui-archive/{document_id}")
def document(venture_id: str, document_id: str, response: Response, x_subject: str | None = Header(default=None, alias="X-Subject")):
    response.headers["Cache-Control"] = "private, no-store"
    return company_home.document(venture_id, x_subject, document_id)
