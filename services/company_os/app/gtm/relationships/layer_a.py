"""OT-48: public-document Layer A ingest. Never writes named Layer B emails."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from core.clock import utcnow
from core.ids import new_id

from app.db.models import GtmPublicDocument
from app.state import db

_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_LINKEDIN_RE = re.compile(
    r"https?://([a-z0-9.-]*\.)?linkedin\.com/[^\s\"']+", re.IGNORECASE
)
ALLOWED_KINDS = frozenset({"job", "hn", "issue", "other"})


class LayerAError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def strip_layer_a_pii(payload: dict[str, Any]) -> dict[str, Any]:
    banned = {
        "email",
        "emails",
        "contact_email",
        "linkedin_url",
        "linkedin",
        "first_name",
        "last_name",
        "full_name",
    }
    out: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if key.lower() in banned:
            continue
        if isinstance(value, str):
            value = _EMAIL_RE.sub("[redacted]", value)
            value = _LINKEDIN_RE.sub("", value).strip()
        if isinstance(value, dict):
            value = strip_layer_a_pii(value)
        out[key] = value
    out.pop("email", None)
    out.pop("linkedin_url", None)
    return out


def ingest_public_documents(
    workspace_id: str | None,
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    """Persist Layer A public docs. ``workspace_id`` None = shared catalog."""

    if not documents:
        raise LayerAError("documents_required")
    now = utcnow()
    created: list[dict[str, Any]] = []
    with db()() as session:
        for doc in documents:
            kind = str(doc.get("kind") or "other")
            if kind not in ALLOWED_KINDS:
                kind = "other"
            raw_payload = dict(doc.get("payload") or {})
            raw_payload.setdefault("title", doc.get("title"))
            raw_payload.setdefault("url", doc.get("url") or doc.get("evidence_url"))
            cleaned = strip_layer_a_pii(raw_payload)
            blob = json.dumps(cleaned, sort_keys=True)
            body_hash = hashlib.sha256(blob.encode()).hexdigest()
            row = GtmPublicDocument(
                id=new_id("gtmpub"),
                workspace_id=workspace_id,
                kind=kind,
                title=doc.get("title"),
                evidence_url=doc.get("url") or doc.get("evidence_url"),
                payload_json=json.dumps(cleaned),
                body_hash=body_hash,
                created_at=now,
            )
            session.add(row)
            created.append(
                {
                    "id": row.id,
                    "kind": kind,
                    "payload": cleaned,
                    "email": None,
                    "linkedin_url": None,
                    "workspace_id": workspace_id,
                }
            )
        session.commit()
    return {"ok": True, "documents": created, "layer": "A"}
