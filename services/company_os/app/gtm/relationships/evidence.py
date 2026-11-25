"""OT-39: record Slack/GitHub/Gmail evidence sources without inventing contacts."""

from __future__ import annotations

from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import GtmEmailAccount, GtmEvidenceSource
from app.connections import mailboxes
from app.state import db

PROVIDERS = frozenset({"slack", "github", "gmail"})
STARTED_OR_CONNECTED = frozenset({"started", "connected"})


class EvidenceError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _gmail_connected(session, workspace_id: str) -> GtmEmailAccount | None:
    from app.connections.mailboxes import list_read_mailboxes
    refs = list_read_mailboxes(session, workspace_id)
    return session.get(GtmEmailAccount, refs[0]["mailbox_id"]) if refs else None


def list_evidence_sources(workspace_id: str) -> list[dict[str, Any]]:
    with db()() as session:
        rows = list(
            session.scalars(
                select(GtmEvidenceSource).where(GtmEvidenceSource.workspace_id == workspace_id)
            )
        )
        gmail = _gmail_connected(session, workspace_id)
        out: list[dict[str, Any]] = []
        by_provider = {r.provider: r for r in rows}
        for provider in ("slack", "github", "gmail"):
            row = by_provider.get(provider)
            status = row.status if row else "absent"
            connected_account_id = row.connected_account_id if row else None
            if provider == "gmail" and gmail is not None:
                status = "connected"
                connected_account_id = gmail.connected_account_id
            out.append(
                {
                    "provider": provider,
                    "status": status,
                    "connected_account_id": connected_account_id,
                    "fake": False,
                }
            )
            if provider == "gmail" and mailboxes.enabled():
                out[-1]["mailboxes"] = [{"mailbox_id": ref["mailbox_id"], "status": ref["status"]}
                    for ref in mailboxes.list_read_mailboxes(session, workspace_id)]
                if not gmail and status == "connected":
                    out[-1].update(status="absent", connected_account_id=None)
        return out


def evidence_present(workspace_id: str) -> bool:
    return any(s["status"] in STARTED_OR_CONNECTED for s in list_evidence_sources(workspace_id))


def record_evidence_source(
    workspace_id: str,
    *,
    provider: str,
    action: str,
) -> dict[str, Any]:
    """Record intent only. ``connected`` is allowed for Gmail only when an account exists."""

    provider = (provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise EvidenceError("unknown_provider")
    action = (action or "").strip().lower()
    if action not in {"connect_later", "start", "connected"}:
        raise EvidenceError("unknown_action")

    now = utcnow()
    with db()() as session:
        row = session.scalar(
            select(GtmEvidenceSource).where(
                GtmEvidenceSource.workspace_id == workspace_id,
                GtmEvidenceSource.provider == provider,
            )
        )
        if row is None:
            row = GtmEvidenceSource(
                id=new_id("gtmes"),
                workspace_id=workspace_id,
                provider=provider,
                status="deferred",
                created_at=now,
                updated_at=now,
            )
            session.add(row)

        if action == "connect_later":
            row.status = "deferred"
            row.deferred_at = now
            row.connected_at = None
            row.connected_account_id = None
        elif action == "start":
            row.status = "started"
            row.started_at = now
            row.connected_at = None
            row.connected_account_id = None
        else:
            if provider != "gmail":
                raise EvidenceError("oauth_not_connected", 409)
            acct = _gmail_connected(session, workspace_id)
            if acct is None:
                raise EvidenceError("oauth_not_connected", 409)
            row.status = "connected"
            row.connected_at = now
            row.connected_account_id = acct.connected_account_id

        row.updated_at = now
        session.commit()
        return {
            "provider": row.provider,
            "status": row.status,
            "connected_account_id": row.connected_account_id,
            "fake": False,
        }
