from fastapi import APIRouter, Header, Response

from app.ventures.next_step_models import Decision, OutcomeBody

router = APIRouter(tags=["next step"])


@router.get("/ventures/{venture_id}/next-step", response_model=Decision)
def next_step(venture_id: str, response: Response, x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.ventures import next_step_engine

    response.headers["Cache-Control"] = "private, no-store"
    return next_step_engine.get_next_step(venture_id, x_subject)


@router.post("/ventures/{venture_id}/next-step/outcome")
def outcome(venture_id: str, body: OutcomeBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.ventures import next_step_engine

    return next_step_engine.post_outcome(venture_id, x_subject, body)
