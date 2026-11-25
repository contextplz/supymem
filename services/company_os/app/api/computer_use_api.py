"""Desktop claim surface for durable Computer Use build jobs."""

from __future__ import annotations

from app.build import computer_use as _service

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.build.computer_use import claim_job, list_jobs, record_status

router = APIRouter(tags=["computer-use"])


class ComputerUseStatusBody(BaseModel):
    status: str
    detail: str = Field(default="", max_length=1000)


def _venture(venture_id: str) -> None:
    return _service.computer_venture(venture_id)


@router.get("/ventures/{venture_id}/computer-use/jobs")
def pending_computer_use_jobs(venture_id: str):
    _venture(venture_id)
    return {"items": list_jobs(venture_id, statuses={"pending"})}


@router.post("/ventures/{venture_id}/computer-use/jobs/{job_id}/claim")
def claim_computer_use_job(venture_id: str, job_id: str):
    _venture(venture_id)
    job = claim_job(venture_id, job_id)
    if not job:
        raise HTTPException(404, "not_found")
    if job.get("status") != "claimed" or not job.get("claimed_now"):
        raise HTTPException(409, "computer_use_job_already_claimed")
    job.pop("claimed_now", None)
    return job


@router.post("/ventures/{venture_id}/computer-use/jobs/{job_id}/status")
def update_computer_use_job(
    venture_id: str, job_id: str, payload: ComputerUseStatusBody
):
    _venture(venture_id)
    try:
        job = record_status(
            venture_id, job_id, status=payload.status, detail=payload.detail
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if not job:
        raise HTTPException(404, "not_found")
    return job
