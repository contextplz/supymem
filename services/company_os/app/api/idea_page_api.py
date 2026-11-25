from fastapi import APIRouter, Header, Response

from app.ventures import idea_page

router = APIRouter(tags=["idea page"])


@router.get("/ventures/{venture_id}/idea-page")
def read(venture_id: str, response: Response, x_subject: str | None = Header(default=None, alias="X-Subject")):
    response.headers["Cache-Control"] = "private, no-store"
    return idea_page.read_for(venture_id, x_subject)
