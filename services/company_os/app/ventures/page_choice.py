"""One reversible page-or-customers decision per venture."""
import json

from app.db.models import VentureDocument
from core.clock import utcnow


def save(session, venture_id: str, choice: str) -> None:
    if choice not in {"build", "skip"}:
        raise ValueError("invalid_page_choice")
    identity, now = f"page_choice:{venture_id}", utcnow()
    row = session.get(VentureDocument, identity)
    if row is None:
        row = VentureDocument(id=identity, venture_id=venture_id, kind="page_choice",
            title="Page choice", created_at=now, updated_at=now)
        session.add(row)
    row.body_md, row.updated_at = json.dumps({"choice": choice}), now


def building(session, venture_id: str) -> None:
    # Only update an existing choice. Ordinary builds do not need another document.
    if session.get(VentureDocument, f"page_choice:{venture_id}"):
        save(session, venture_id, "build")
