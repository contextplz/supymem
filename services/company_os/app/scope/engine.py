"""Scope in/out/conditional with founder-facing messages (doc 12 §2)."""

from __future__ import annotations

import json
from typing import Any

from app.db.supymem_models import ExecutionPlan, ScopeDecision
from app.scope.catalogue import load_catalogue
from app.state import db
from core.clock import utcnow
from core.ids import new_id

_NEVER_HINTS = {
    "native ios": "mobile.native_ios",
    "ios app": "mobile.native_ios",
    "iphone app": "mobile.native_ios",
    "react native": "mobile.react_native",
    "hipaa": "compliance.hipaa",
    "video transcode": "media.video_processing",
    "iot": "hardware.iot",
    "firmware": "hardware.iot",
}


def classify_request(text: str) -> dict[str, Any]:
    blob = (text or "").lower()
    catalogue = load_catalogue()
    for needle, cap_id in _NEVER_HINTS.items():
        if needle in blob:
            cap = next(c for c in catalogue["capabilities"] if c["id"] == cap_id)
            maturity = cap.get("maturity")
            classification = "out" if maturity == "never" else "conditional" if maturity == "planned" else "in"
            message = cap.get("message") or _default_message(text, classification, cap_id)
            return {
                "classification": classification,
                "capability_id": cap_id,
                "founder_message": message,
                "echo": text.strip(),
                "alternatives": _alternatives(cap_id),
                "reason": f"capability {cap_id} maturity={maturity}",
            }
    if any(k in blob for k in ("waitlist", "landing", "next.js", "web app", "dashboard")):
        return {
            "classification": "in",
            "capability_id": "web_app.nextjs",
            "founder_message": f"We can include this: {text.strip()}",
            "echo": text.strip(),
            "alternatives": [],
            "reason": "matches ga web_app.nextjs",
        }
    return {
        "classification": "conditional",
        "capability_id": None,
        "founder_message": (
            f"I heard: {text.strip()}. I don't have an exact capability id yet"
            "I'll park it as conditional until research/plan review."
        ),
        "echo": text.strip(),
        "alternatives": ["Describe the workflow as a web screen instead."],
        "reason": "unknown capability",
    }


def _default_message(text: str, classification: str, cap_id: str) -> str:
    return (
        f"I heard you want: {text.strip()}. That maps to {cap_id}, which is {classification} "
        "for Supymem v1."
    )


def _alternatives(cap_id: str) -> list[str]:
    if cap_id == "mobile.native_ios":
        return ["Ship a responsive web app", "Import an existing iOS repo later"]
    if cap_id == "media.video_processing":
        return ["Integrate Mux", "Integrate Cloudinary"]
    if cap_id == "compliance.hipaa":
        return ["Do not store PHI on Supymem", "Use a HIPAA vendor and keep us out of the data path"]
    return ["Narrow the request to a web workflow"]


def record_decision(plan_id: str, workspace_id: str, venture_id: str, text: str) -> dict[str, Any]:
    result = classify_request(text)
    factory = db()
    now = utcnow()
    with factory() as session:
        plan = session.get(ExecutionPlan, plan_id)
        if plan is None:
            raise KeyError(plan_id)
        row = ScopeDecision(
            id=new_id("scd"),
            plan_id=plan_id,
            workspace_id=workspace_id,
            venture_id=venture_id,
            request_text=text,
            classification=result["classification"],
            reasons=json.dumps({"reason": result["reason"], "capability_id": result.get("capability_id")}),
            founder_message=result["founder_message"],
            alternatives=json.dumps(result["alternatives"]),
            decided_by="agent",
            created_at=now,
        )
        session.add(row)
        session.commit()
        result["id"] = row.id
    return result
