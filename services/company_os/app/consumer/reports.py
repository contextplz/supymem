"""Persist desktop crash / user bug reports in Company OS RDS."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.credits._txn import naive_utc, session_factory
from app.db.models import DesktopBugReport
from core.ids import new_id  # type: ignore[import-not-found]
from core.redact import redact_string

KINDS = frozenset({"crash", "user"})
STATUSES = frozenset({"new", "ack", "closed"})
MAX_STACK = 64_000
MAX_LOGS = 32_000
MAX_BODY = 8_000
MAX_TITLE = 200
MAX_CONTACT = 200


class ReportError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def ingest_enabled() -> bool:
    raw = (os.environ.get("BUG_REPORTS_INGEST_ENABLED") or "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _clip(value: str | None, limit: int) -> str | None:
    text = redact_string((value or "").strip())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _parse_occurred(raw: str | None) -> datetime:
    text = (raw or "").strip()
    if not text:
        return naive_utc()
    try:
        cleaned = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return naive_utc()
    return naive_utc(dt)


def _expunge(session: Any, row: Any) -> Any:
    session.expunge(row)
    return row


def ingest(
    *,
    kind: str,
    app_version: str,
    os_name: str,
    occurred_at: str | None = None,
    stack: str | None = None,
    title: str | None = None,
    body: str | None = None,
    logs: str | None = None,
    contact: str | None = None,
    install_id: str | None = None,
    account_id: str | None = None,
    anonymous: bool = True,
) -> dict[str, Any]:
    if not ingest_enabled():
        raise ReportError("ingest_disabled", "Bug report ingest is turned off.")
    if kind not in KINDS:
        raise ReportError("invalid_kind", "kind must be crash or user")
    now = naive_utc()
    # Crash reports never store a typed contact. Account id is attached only
    # when the caller proved a session and opted in.
    stored_contact = _clip(contact, MAX_CONTACT) if kind == "user" else None
    stored_account = (account_id or "").strip() or None
    if kind == "crash" and not stored_account:
        stored_contact = None
    row = DesktopBugReport(
        id=new_id("bug"),
        kind=kind,
        status="new",
        app_version=_clip(app_version, 40) or "unknown",
        os_name=_clip(os_name, 120) or "unknown",
        occurred_at=_parse_occurred(occurred_at),
        received_at=now,
        title=_clip(title, MAX_TITLE),
        body=_clip(body, MAX_BODY),
        stack_text=_clip(stack, MAX_STACK),
        logs_text=_clip(logs, MAX_LOGS),
        contact=stored_contact,
        account_id=stored_account,
        anonymous=bool(anonymous or stored_account is None),
        install_id=_clip(install_id, 80),
        created_at=now,
        updated_at=now,
    )
    if stored_account:
        row.anonymous = False
    factory = session_factory()
    with factory() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return as_dict(_expunge(session, row), detail=False)


def as_dict(row: DesktopBugReport, *, detail: bool = False) -> dict[str, Any]:
    stack = row.stack_text or ""
    payload: dict[str, Any] = {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "app_version": row.app_version,
        "os": row.os_name,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "title": row.title,
        "body": row.body,
        "contact": row.contact,
        "account_id": row.account_id,
        "anonymous": bool(row.anonymous),
        "install_id": row.install_id,
        "stack_preview": stack[:500] if stack else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if detail:
        payload["stack"] = row.stack_text
        payload["logs"] = row.logs_text
    return payload


def list_reports(
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    cap = max(1, min(int(limit), 200))
    factory = session_factory()
    with factory() as session:
        stmt = select(DesktopBugReport).order_by(DesktopBugReport.received_at.desc())
        if kind:
            if kind not in KINDS:
                raise ReportError("invalid_kind", "kind must be crash or user")
            stmt = stmt.where(DesktopBugReport.kind == kind)
        if status:
            if status not in STATUSES:
                raise ReportError("invalid_status", "status must be new, ack, or closed")
            stmt = stmt.where(DesktopBugReport.status == status)
        rows = list(session.scalars(stmt.limit(cap)))
        items = [as_dict(_expunge(session, row), detail=False) for row in rows]
    return {"items": items, "count": len(items)}


def get_report(report_id: str) -> dict[str, Any] | None:
    factory = session_factory()
    with factory() as session:
        row = session.get(DesktopBugReport, report_id)
        if row is None:
            return None
        return as_dict(_expunge(session, row), detail=True)


def set_status(report_id: str, status: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise ReportError("invalid_status", "status must be new, ack, or closed")
    factory = session_factory()
    with factory() as session:
        row = session.get(DesktopBugReport, report_id)
        if row is None:
            raise ReportError("not_found", "report not found")
        row.status = status
        row.updated_at = naive_utc()
        session.commit()
        session.refresh(row)
        return as_dict(_expunge(session, row), detail=True)
