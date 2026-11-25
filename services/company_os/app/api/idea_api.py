"""Idea sessions (docs/supymem/25 § ideation)."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.authz.workspace import assert_venture_access
from app.authz.workspace import subject_person_id
from app.chat.idea_bridge import load_venture, session_belongs_to, list_sessions as list_idea_sessions
from app.idea.candidates import decide_validation
from app.idea.service import (
    confirm,
    get_session,
    list_revisions,
    open_session,
    patch_revision,
    post_message,
    retry_failed_clarification,
    review_clarification_manually,
    set_case,
    start_direction_correction,
    start_confirmed_answer_correction,
    reopen_assumed_questions,
)

router = APIRouter(tags=["idea"])


class MessageBody(BaseModel):
    text: str = ""
    chip: str | None = None
    client_ts: str | None = None
    correction_question_id: str | None = None


class CaseBody(BaseModel):
    case: str


class ReopenQuestionsBody(BaseModel):
    revision_id: str


class DirectionCorrectionBody(BaseModel):
    case: str
    revision_id: str


class ConfirmedAnswerCorrectionBody(BaseModel):
    revision_id: str
    question_id: str
    text: str
    patch: dict


class RevisionPatch(BaseModel):
    patch: dict
    answer_correction: dict | None = None


class ManualReviewBody(BaseModel):
    revision_id: str
    patch: dict


class ConfirmBody(BaseModel):
    revision_id: str | None = None
    name: str | None = None
    slug: str | None = None
    start_research: bool = Field(default=True, strict=True)
    accept_validation: bool = Field(default=False, strict=True)


class ValidateBody(BaseModel):
    methods: list[str] | None = None
    revision_id: str | None = None


class DecideBody(BaseModel):
    decision: str
    note: str | None = None
    patch: dict | None = None


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, detail={"error": {"code": "not_found", "message": str(exc)}})
    if isinstance(exc, ValueError):
        return HTTPException(422, detail={"error": {"code": "invalid", "message": str(exc)}})
    if isinstance(exc, RuntimeError):
        return HTTPException(409, detail={"error": {"code": str(exc), "message": str(exc)}})
    return HTTPException(500, detail={"error": {"code": "internal", "message": "failed"}})


def _load_venture(venture_id: str, subject: str | None):
    assert_venture_access(venture_id, subject)
    try:
        return load_venture(venture_id)
    except KeyError as exc:
        raise HTTPException(404, detail={"error": {"code": "not_found", "message": "venture"}}) from exc


@router.get("/ventures/{venture_id}/journey")
def read_journey(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.ventures.journey_overrides import read
    assert_venture_access(venture_id, x_subject)
    return read(venture_id)


@router.post("/ventures/{venture_id}/idea/sessions", status_code=201)
def create_session(
    venture_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    ven = _load_venture(venture_id, x_subject)
    return open_session(venture=ven, actor_subject=subject_person_id(x_subject))


@router.get("/ventures/{venture_id}/idea/sessions/{session_id}")
def read_session(
    venture_id: str,
    session_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    try:
        data = get_session(session_id)
    except KeyError as exc:
        raise _http(exc) from exc
    if data["venture_id"] != venture_id:
        raise HTTPException(404, detail={"error": {"code": "not_found", "message": "session"}})
    return data


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/messages")
def post_session_message(
    venture_id: str,
    session_id: str,
    body: MessageBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    try:
        return post_message(
            session_id,
            body.text,
            chip=body.chip,
            actor_subject=subject_person_id(x_subject),
            correction_question_id=body.correction_question_id,
        )
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _http(exc) from exc


class ClarificationRetryBody(BaseModel):
    failed_job_id: str | None = Field(default=None, min_length=1, max_length=128)


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/retry-clarification")
def retry_session_clarification(
    venture_id: str,
    session_id: str,
    body: ClarificationRetryBody | None = None,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    try:
        data = retry_failed_clarification(
            session_id,
            venture_id=venture_id,
            actor_subject=subject_person_id(x_subject),
            failed_job_id=body.failed_job_id if body else None,
            allow_uncertain=True,
        )
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _http(exc) from exc
    return data


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/manual-review")
def manual_review_session(
    venture_id: str,
    session_id: str,
    body: ManualReviewBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    try:
        return review_clarification_manually(session_id, body.patch, venture_id=venture_id,
            workspace_id=workspace_id, revision_id=body.revision_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _http(exc) from exc


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/case")
def set_session_case(
    venture_id: str,
    session_id: str,
    body: CaseBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    try:
        return set_case(session_id, body.case, venture_id=venture_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _http(exc) from exc


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/correct-direction", status_code=201)
def correct_direction(
    venture_id: str,
    session_id: str,
    body: DirectionCorrectionBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    try:
        return start_direction_correction(session_id, body.case,
            venture_id=venture_id, revision_id=body.revision_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _http(exc) from exc


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/reopen-questions")
def reopen_questions(
    venture_id: str,
    session_id: str,
    body: ReopenQuestionsBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    try:
        return reopen_assumed_questions(session_id, venture_id=venture_id, revision_id=body.revision_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _http(exc) from exc


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/correct-answer", status_code=201)
def correct_confirmed_answer(
    venture_id: str,
    session_id: str,
    body: ConfirmedAnswerCorrectionBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    try:
        return start_confirmed_answer_correction(session_id, venture_id=venture_id,
            revision_id=body.revision_id, question_id=body.question_id,
            text=body.text, patch=body.patch)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _http(exc) from exc


@router.patch("/ventures/{venture_id}/idea/sessions/{session_id}/revision")
def patch_session_revision(
    venture_id: str,
    session_id: str,
    body: RevisionPatch,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    try:
        revision = patch_revision(session_id, body.patch, venture_id=venture_id, workspace_id=workspace_id,
                                  answer_correction=body.answer_correction)
        from app.chat.idea_corrections import correction_receipt
        from app.chat.store import append_message
        receipt = correction_receipt(venture_id, body.patch)
        append_message(venture_id, role="assistant", kind="idea_correction_saved", body_md=receipt["body_md"],
                       actions=receipt["actions"], idempotency_key=f"idea_correction:{revision['id']}")
        return revision
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _http(exc) from exc


@router.get("/ventures/{venture_id}/idea/sessions/{session_id}/revisions")
def session_revisions(
    venture_id: str,
    session_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    return {"items": list_revisions(session_id)}


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/propose")
def force_propose(
    venture_id: str,
    session_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    return post_message(
        session_id,
        "just propose",
        actor_subject=subject_person_id(x_subject),
    )


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/validate")
def validate(
    venture_id: str,
    session_id: str,
    body: ValidateBody | None = None,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    from app.idea.validation import start_validation
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    body = body or ValidateBody()
    try:
        return start_validation(session_id, revision_id=body.revision_id, methods=body.methods,
                                venture_id=venture_id, workspace_id=workspace_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _http(exc) from exc


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/validations/{run_id}/decide")
def decide(
    venture_id: str,
    session_id: str,
    run_id: str,
    body: DecideBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    from app.worker.handlers import require_capability_http
    require_capability_http("idea.validate")
    assert_venture_access(venture_id, x_subject)
    try:
        return decide_validation(session_id, run_id, body.decision, note=body.note, patch=body.patch)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _http(exc) from exc


@router.post("/ventures/{venture_id}/idea/sessions/{session_id}/confirm")
def confirm_session(
    venture_id: str,
    session_id: str,
    body: ConfirmBody | None = None,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    if not session_belongs_to(session_id, venture_id, workspace_id):
        raise HTTPException(404, "not_found")
    body = body or ConfirmBody()
    try:
        return confirm(session_id, revision_id=body.revision_id, name=body.name, slug=body.slug, person_id=x_subject,
                       start_research=body.start_research, accept_validation=body.accept_validation, venture_id=venture_id)
    except (KeyError, RuntimeError) as exc:
        raise _http(exc) from exc


@router.get("/ventures/{venture_id}/idea/sessions")
def list_sessions(
    venture_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    return list_idea_sessions(venture_id)
