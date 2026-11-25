"""Authenticated workbook reference browsing; never initiates venture actions."""
from __future__ import annotations

from app.knowledge import pmf as _service


from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.authz.workspace import assert_venture_access, subject_person_id
from app.core.local_mode import is_local
from app.knowledge import pmf
from app.knowledge.local_data import ReferenceDataError

router = APIRouter(tags=["knowledge"])


def _load_reference(loader, *args, **kwargs):
    try:
        data = loader(*args, **kwargs)
    except (ReferenceDataError, OSError) as error:
        raise HTTPException(503, detail=str(error)) from error
    if data.get("message"):
        raise HTTPException(503, detail=data["message"])
    return data


@router.get("/ventures/{venture_id}/knowledge")
def references(
    venture_id: str,
    q: str | None = Query(default=None, max_length=2000),
    kind: str | None = Query(default=None, max_length=80),
    stage: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=6, ge=1, le=20),
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    inferred = q is None
    if inferred:
        q = venture_query(venture_id, workspace_id)
        if stage is None:
            from app.ventures.next_step import founder_next_step
            stage = pmf.journey_stage(founder_next_step(venture_id))
    data = _load_reference(pmf.corpus)
    return {"venture_id": venture_id, "dataset_id": data["dataset_id"], "sha256": data["sha256"],
            "policy": pmf.POLICY, "inferred_from_idea": inferred,
            "items": (pmf.recommend(q or "", kind=kind, stage=stage, limit=limit)
                      if kind in ("historical_claim", "playbook_guidance") else
                      pmf.search(q or "", kind=kind, stage=stage, limit=limit)),
            "records_available": len(data["records"])}


@router.get("/ventures/{venture_id}/knowledge/coaching")
def coaching_references(venture_id: str, response: Response, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    from app.ventures.next_step import founder_next_step
    from app.ventures.run_status import snapshot

    step = founder_next_step(venture_id)
    status = snapshot(venture_id, workspace_id) if step.get("key") == "work.running" else None
    if step.get("key", "").startswith("gtm."):
        replies = _service.knowledge_reply_count(venture_id, workspace_id)
        if replies:
            step = {**step, "key": "learning.replies", "title": "Learn from customer replies"}
    response.headers["Cache-Control"] = "private, no-store"
    return {"venture_id": venture_id, **_load_reference(pmf.coaching, venture_query(venture_id, workspace_id),
                                                       next_step=step, run_status=status)}


@router.get("/ventures/{venture_id}/knowledge/records/{record_id}")
def reference(venture_id: str, record_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    _load_reference(pmf.corpus)
    record = pmf.get_record(record_id)
    if not record:
        raise HTTPException(404, detail={"error": {"code": "reference_not_found"}})
    return {"venture_id": venture_id, "item": record}


def venture_query(venture_id, workspace_id):
    return _service.knowledge_venture_query(venture_id, workspace_id)


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str | None = Field(default=None, min_length=1, max_length=2000)


@router.post("/ventures/{venture_id}/knowledge/review")
def review_references(venture_id: str, body: ReviewBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    _load_reference(pmf.corpus)
    from app.knowledge import jev
    query = body.query if body.query is not None else venture_query(venture_id, workspace_id)
    # The optional model may order close analogies or abstain, but it must not
    # reintroduce cases rejected by the deterministic context boundary.
    stage = None
    if body.query is None:
        from app.ventures.next_step import founder_next_step
        stage = pmf.journey_stage(founder_next_step(venture_id))
    candidates = pmf.recommend(query, limit=6, kind="historical_claim", stage=stage)
    advisory = (jev.review(query, candidates, workspace_id=workspace_id, venture_id=venture_id,
                           actor=subject_person_id(x_subject)) if candidates else
                {"assessment": "no_close_match", "selected_id": None, "founder_credits_charged": 0})
    if is_local():
        advisory.pop("founder_credits_charged", None)
    selected = advisory.get("selected_id")
    if selected and selected in {r["id"] for r in candidates}:
        candidates.sort(key=lambda row: row["id"] != selected)
    return {"venture_id": venture_id, "items": candidates[:3], "advisory": advisory,
            "policy": pmf.POLICY, "inferred_from_idea": body.query is None}
