"""Natural-language iteration (docs/supymem/13 §9)."""

from __future__ import annotations

import os
from typing import Any

from app.core.jobs import enqueue
from app.db.supymem_models import FounderApp, FounderRequest
from app.scope.engine import classify_request
from app.state import db
from app.worker.run_sync import run_kind
from core.clock import utcnow
from core.ids import new_id

AUTO_CREDIT_CAP = 100


def interpret(text: str) -> dict[str, Any]:
    blob = (text or "").lower()
    areas: list[str] = []
    if any(k in blob for k in ("pric", "annual", "plan", "billing")):
        areas.append("pricing")
    if any(k in blob for k in ("landing", "hero", "cta", "copy")):
        areas.append("landing")
    if any(k in blob for k in ("auth", "login", "signup")):
        areas.append("auth")
    if any(k in blob for k in ("email", "notif")):
        areas.append("email")
    if not areas:
        areas.append("web")
    risk = "low"
    if any(k in blob for k in ("delete", "payment", "refund", "password", "admin")):
        risk = "high"
    elif any(k in blob for k in ("pric", "auth", "email", "schema")):
        risk = "medium"
    words = max(1, len((text or "").split()))
    estimate = min(400, max(8, words * 2))
    scope = classify_request(text)
    caps = [c for c in ([scope.get("capability_id")] if scope.get("capability_id") else [])]
    return {
        "change_summary": (text or "").strip()[:400],
        "affected_areas": areas,
        "needs_capabilities": caps,
        "risk": risk,
        "estimate_hint": estimate,
        "scope": scope,
    }


def request_iteration(app_id: str, text: str) -> dict[str, Any]:
    from app.core.local_mode import is_local

    factory = db()
    now = utcnow()
    parsed = interpret(text)
    scope = parsed["scope"]
    with factory() as session:
        app = session.get(FounderApp, app_id)
        if app is None or app.deleted_at is not None:
            raise KeyError("app")
        order = None
        if is_local():
            from app.builder.ctl import request_local_build

            order = request_local_build(app.venture_id, text.strip(), repair=True)
        req = FounderRequest(
            id=new_id("frq"),
            workspace_id=app.workspace_id,
            venture_id=app.venture_id,
            source="iteration",
            text=text.strip(),
            intent="iterate",
            intent_category="iteration",
            classification=str(scope.get("classification") or "unknown"),
            outcome="pending",
            related_id=app_id,
            created_at=now,
        )
        session.add(req)
        session.flush()
        payload = {
            "app_id": app_id,
            "workspace_id": app.workspace_id,
            "venture_id": app.venture_id,
            "request_id": req.id,
            "text": text.strip(),
            "interpret": parsed,
        }
        if order is not None:
            req.outcome = "accepted"
            req.related_id = order["id"]
            session.commit()
            return {"request_id": req.id, "device_work_order_id": order["id"], "review_path": order["review_path"],
                    "status": "queued", "workspace_id": app.workspace_id, "venture_id": app.venture_id}
        job_id = enqueue(
            session,
            "app.iterate",
            payload,
            workspace_id=app.workspace_id,
            venture_id=app.venture_id,
            dedupe_key=f"iterate:{req.id}",
        )
        session.commit()
        request_id = req.id
        workspace_id = app.workspace_id
        venture_id = app.venture_id
    if os.environ.get("IDEA_SYNC", "").strip() in {"1", "true", "yes"}:
        run_kind("app.iterate", payload)
    return {
        "request_id": request_id,
        "job_id": job_id,
        "interpret": parsed,
        "workspace_id": workspace_id,
        "venture_id": venture_id,
    }


def run_iterate(payload: dict[str, Any]) -> dict[str, Any]:
    from app.core.local_mode import is_local

    if is_local():
        from app.builder.ctl import run_build

        return {"status": "building", "build": run_build({**payload, "reason": "iteration"}),
                "request_id": payload["request_id"]}
    parsed = payload.get("interpret") or interpret(payload.get("text") or "")
    scope = parsed.get("scope") or {}
    classification = scope.get("classification") or "unknown"
    estimate = int(parsed.get("estimate_hint") or 8)
    risk = parsed.get("risk") or "low"
    auto = classification == "in" and risk == "low" and estimate <= AUTO_CREDIT_CAP
    factory = db()
    now = utcnow()
    with factory() as session:
        req = session.get(FounderRequest, payload["request_id"])
        if req is None:
            raise KeyError("request")
        if classification in {"out", "conditional"}:
            req.outcome = "declined"
            req.failure_reason = str(scope.get("reason") or classification)
            session.commit()
            return {
                "status": "out_of_scope",
                "founder_message": scope.get("founder_message"),
                "request_id": req.id,
            }
        if auto:
            req.outcome = "accepted"
            from app.builder.ctl import run_build

            session.commit()
        else:
            req.outcome = "pending_approval"
            session.commit()
    if auto:
        built = run_build(
            {
                "workspace_id": payload["workspace_id"],
                "venture_id": payload["venture_id"],
                "reason": "iteration",
                "request_id": payload["request_id"],
            }
        )
        return {"status": "building", "build": built, "request_id": payload["request_id"]}
    from app.approvals.service import ApprovalError, request_approval

    try:
        approval = request_approval(
            workspace_id=payload["workspace_id"],
            kind="credit_reserve_large",
            subject_ref=payload["request_id"],
            summary=parsed.get("change_summary") or "Iteration needs approval",
            requested_by="system",
            risk=risk if risk in {"low", "medium", "high"} else "medium",
            detail={"estimate_credits": estimate, "interpret": parsed},
        )
        return {
            "status": "needs_approval",
            "approval": approval.to_dict(),
            "request_id": payload["request_id"],
        }
    except ApprovalError as exc:
        return {
            "status": "needs_approval",
            "approval_error": exc.code,
            "request_id": payload["request_id"],
            "created_at": now.isoformat(),
        }
