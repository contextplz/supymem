"""Durable, single-claim Computer Use jobs for the founder's Mac.

The hosted service records intent; only the authenticated desktop for that
venture may claim it. Claimed work is never automatically reclaimed because
GUI side effects are ambiguous after a disconnect.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.db.models import Venture, VentureDocument
from app.state import db
from core.clock import utcnow
from core.cu_task_classification import classify_cu_task
from core.ids import new_id

JOB_KIND = "computer_use_job"
VISIBLE_STATES = {"pending", "claimed", "running", "completed", "failed"}


def enqueue_computer_use(
    venture_id: str, *, prompt: str, title: str = "Build this product"
) -> dict[str, Any]:
    text = prompt.strip()
    if not text:
        raise ValueError("computer_use_prompt_required")
    factory = db()
    now = utcnow()
    job_id = new_id("cuj")
    body = {
        "id": job_id,
        "status": "pending",
        "prompt": text[:12000],
        "title": title.strip()[:120] or "Build this product",
        "created_at": now.isoformat(),
    }
    body["cu_task_class"], body["cu_task_class_reason"] = classify_cu_task(body)
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise KeyError(venture_id)
        session.add(VentureDocument(
            id=job_id, venture_id=venture_id, kind=JOB_KIND,
            title=body["title"], path=None, body_md=body["prompt"],
            provenance_json=json.dumps(body), created_at=now, updated_at=now,
        ))
        session.commit()
    return body


def list_jobs(venture_id: str, *, statuses: set[str] | None = None) -> list[dict[str, Any]]:
    wanted = statuses or VISIBLE_STATES
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(VentureDocument).where(
                VentureDocument.venture_id == venture_id,
                VentureDocument.kind == JOB_KIND,
            ).order_by(VentureDocument.created_at.desc())
        ).all()
        return [record for row in rows if (record := _record(row)).get("status") in wanted]


def claim_job(venture_id: str, job_id: str) -> dict[str, Any] | None:
    factory = db()
    now = utcnow()
    with factory() as session:
        row = session.get(VentureDocument, job_id, with_for_update=True)
        if row is None or row.venture_id != venture_id or row.kind != JOB_KIND:
            return None
        rec = _record(row)
        if rec.get("status") != "pending":
            rec["claimed_now"] = False
            return rec
        rec["status"] = "claimed"
        rec["claimed_at"] = now.isoformat()
        row.provenance_json = json.dumps(rec)
        row.updated_at = now
        session.commit()
        rec["claimed_now"] = True
        return rec


def record_status(
    venture_id: str, job_id: str, *, status: str, detail: str = ""
) -> dict[str, Any] | None:
    if status not in {"running", "completed", "failed"}:
        raise ValueError("computer_use_status_invalid")
    factory = db()
    now = utcnow()
    with factory() as session:
        row = session.get(VentureDocument, job_id, with_for_update=True)
        if row is None or row.venture_id != venture_id or row.kind != JOB_KIND:
            return None
        rec = _record(row)
        current = str(rec.get("status") or "")
        allowed = {
            "claimed": {"running", "failed"},
            "running": {"completed", "failed"},
        }
        if status != current and status not in allowed.get(current, set()):
            raise RuntimeError("computer_use_transition_invalid")
        rec["status"] = status
        rec["detail"] = detail.strip()[:1000]
        rec[f"{status}_at"] = now.isoformat()
        row.provenance_json = json.dumps(rec)
        row.updated_at = now
        session.commit()
        return rec


def _record(row: VentureDocument) -> dict[str, Any]:
    try:
        data = json.loads(row.provenance_json or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("id", row.id)
    data.setdefault("prompt", row.body_md or "")
    data.setdefault("title", row.title)
    data.setdefault("status", "pending")
    data.setdefault("cu_task_class", None)
    data.setdefault("cu_task_class_reason", None)
    return data

from fastapi import HTTPException

def computer_venture(venture_id: str) -> None:
    with db()() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
