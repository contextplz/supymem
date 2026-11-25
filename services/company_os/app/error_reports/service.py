"""Durable error fingerprints with new-fingerprint and spike alerts.

Fed by the one place every error already passes: ``analytics.error_events.log_error_event`` (server 5xx, worker
job failures, model rejections, and console/desktop ``ui.error`` telemetry). Ids and closed labels only; request
bodies, exception messages and tracebacks never enter a row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Person
from app.core.local_mode import is_local
from app.db.supymem_models import ErrorReport
from app.state import db
from core.clock import utcnow
from core.ids import new_id
from core.runtime_diagnostics import closed_runtime_diagnostic

log = logging.getLogger("app.error_event")
_FRAME = re.compile(r"[A-Za-z0-9_./-]{1,80}:[A-Za-z0-9_<>]{1,60}\Z")
SPIKE_WINDOW = timedelta(minutes=10)
MAX_AFFECTED = 50  # ponytail: JSON list on the row, not a join table; enough for a 20-founder beta
_CLIENT_CRASHES = frozenset({"render_error", "runtime_error", "native_error", "integrity_error",
                            "cu_browser_decision_unavailable"})


def spike_threshold() -> int:
    try:
        return max(1, int(os.environ.get("ERROR_SPIKE_THRESHOLD", "5")))
    except ValueError:
        return 5


def release() -> str:
    value = os.environ.get("APP_VERSION") or os.environ.get("IMAGE_TAG") or os.environ.get("GIT_SHA") or "unknown"
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value) else "unknown"


def top_frame(exc: BaseException | None) -> str:
    """``api/chat_api.py:post_chat``: the innermost frame in our own code; no line numbers, so it is stable across deploys."""
    frame = ""
    tb = exc.__traceback__ if exc else None
    while tb is not None:
        path = tb.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/app/" in path:
            frame = f"{path.rsplit('/app/', 1)[1]}:{tb.tb_frame.f_code.co_name}"
        tb = tb.tb_next
    return frame if _FRAME.fullmatch(frame) else ""


def fingerprint(source: str, feature: str, code: str, place: str, frame: str) -> str:
    return hashlib.sha256("|".join((source, feature, code, place, frame)).encode()).hexdigest()[:16]


def severity_for(source: str, code: str, level: int) -> str | None:
    """``error`` alerts the founder, ``warning`` is only counted, None (below WARNING) is not stored."""
    if level >= logging.ERROR or (source == "client" and code in _CLIENT_CRASHES):
        return "error"
    return "warning" if level >= logging.WARNING else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def record(*, source: str, feature: str, code: str, place: str = "", frame: str = "", severity: str = "error",
           person_id: str | None = None, workspace_id: str | None = None, venture_id: str | None = None,
           status: int | None = None, runtime_diagnostic: dict | None = None) -> None:
    """Upsert one occurrence. Never raises: error recording must not break the failing path."""
    if is_local():
        return
    try:
        fp = fingerprint(source, feature, code, place, frame)
        now = utcnow()
        sample = {k: v for k, v in {"person": person_id, "workspace": workspace_id, "venture": venture_id,
                                    "status": status}.items() if v is not None}
        diagnostic = closed_runtime_diagnostic(runtime_diagnostic)
        if diagnostic:
            sample["runtime_diagnostic"] = diagnostic
        alert: tuple[str, dict[str, Any]] | None = None
        for _ in (0, 1):
            alert = None
            with db()() as session:
                row = session.scalar(select(ErrorReport).where(ErrorReport.fingerprint == fp).with_for_update())
                kind: str | None = None
                if row is None:
                    row = ErrorReport(id=new_id("err"), fingerprint=fp, source=source, feature=feature, code=code,
                                      place=place, frame=frame, severity=severity, status="open", count=1,
                                      first_seen=now, last_seen=now, release=release(), window_start=now,
                                      window_count=1, sample_json=json.dumps(sample, sort_keys=True),
                                      affected_json=json.dumps([person_id] if person_id else []))
                    session.add(row)
                    kind = "new" if severity == "error" else None
                else:
                    if row.status == "resolved":
                        row.status, row.resolved_at, row.resolved_by = "open", None, None
                        row.window_start, row.window_count = now, 0
                        kind = "regression" if severity == "error" else None
                    if now - _aware(row.window_start) > SPIKE_WINDOW:
                        row.window_start, row.window_count = now, 0
                    row.count += 1
                    row.window_count += 1
                    row.last_seen, row.release = now, release()
                    row.sample_json = json.dumps(sample, sort_keys=True)
                    people = json.loads(row.affected_json or "[]")
                    if person_id and person_id not in people and len(people) < MAX_AFFECTED:
                        row.affected_json = json.dumps(people + [person_id])
                    if (kind is None and severity == "error" and row.window_count > spike_threshold()
                            and (row.alerted_at is None or _aware(row.alerted_at) < _aware(row.window_start))):
                        kind = "spike"
                if kind:
                    row.alerted_at = now
                    alert = (kind, {"fingerprint": fp, "source": source, "feature": feature, "code": code,
                                    "place": place, "count": row.count, "window_count": row.window_count,
                                    "release": row.release, "window": _aware(row.window_start).isoformat()})
                try:
                    session.commit()
                    break
                except IntegrityError:  # two workers saw a new fingerprint at once: the second pass updates it
                    session.rollback()
                    alert = None
        if alert:
            send_alert(*alert)
    except Exception:  # noqa: BLE001
        pass


def send_alert(kind: str, info: dict[str, Any]) -> None:
    """Log line for CloudWatch plus an email to every founder admin (queued; delivered once Resend is configured)."""
    if is_local():
        return
    log.error("error_alert %s", json.dumps({"kind": kind, **{k: info[k] for k in
              ("fingerprint", "source", "feature", "code", "count")}}, sort_keys=True))
    from app.consumer.founder import founder_emails
    from app.email.service import send_email

    emails = sorted(founder_emails())
    if not emails:
        return
    with db()() as session:
        ids = session.scalars(select(Person.id).where(Person.email.in_(emails))).all()
    for pid in ids:
        try:
            send_email(pid, "error_alert", {"kind": kind, **info}, ref=f"{info['fingerprint']}:{kind}:{info['window']}")
        except Exception:  # noqa: BLE001
            log.warning("error alert email not queued")


def recent_for_person(person_id: str, hours: int = 24, limit: int = 5) -> list[dict[str, Any]]:
    """Fingerprints this person hit lately (auto-attached to a problem report)."""
    since = utcnow() - timedelta(hours=hours)
    with db()() as session:
        rows = session.scalars(select(ErrorReport).where(ErrorReport.last_seen >= since)
                               .order_by(ErrorReport.last_seen.desc()).limit(200)).all()
    out = [{"fingerprint": r.fingerprint, "code": r.code, "feature": r.feature, "place": r.place}
           for r in rows if person_id in json.loads(r.affected_json or "[]")]
    return out[:limit]

import base64
import binascii
from fastapi import Depends, Header, HTTPException, Response
from sqlalchemy import func
from app.admin.deps import AdminContext, require_admin
from app.authz.workspace import assert_venture_access, subject_person_id
from app.db.supymem_models import ProblemReport
from app.surfaces.ratelimit import Limiter

_IMAGE_MAGIC = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg"}

MAX_SCREENSHOT = 1_500_000

_LIMIT = Limiter(capacity=5, per_seconds=300.0)

def problems_report_problem(body, x_subject: str | None = Header(default=None, alias="X-Subject")):
    if not x_subject:
        raise HTTPException(401, detail={"error": {"code": "unauthenticated"}})
    from app.core.local_mode import is_local

    if is_local():
        raise HTTPException(404, detail={"error": {"code": "not_found",
            "message": "You can check your setup at /local/health."}})
    person_id = subject_person_id(x_subject)
    if not _LIMIT.check(person_id).allowed:
        raise HTTPException(429, detail={"error": {"code": "rate_limited"}})
    wid = None
    if body.venture_id:
        _, wid = assert_venture_access(body.venture_id, x_subject)
    shot, shot_type = None, None
    if body.screenshot_b64:
        if not body.screenshot_consent:
            raise HTTPException(422, detail={"error": {"code": "screenshot_consent_required"}})
        try:
            shot = base64.b64decode(body.screenshot_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, detail={"error": {"code": "screenshot_invalid"}}) from None
        shot_type = next((t for magic, t in _IMAGE_MAGIC.items() if shot.startswith(magic)), None)
        if shot_type is None or len(shot) > MAX_SCREENSHOT:
            raise HTTPException(422, detail={"error": {"code": "screenshot_invalid"}})
    attached = recent_for_person(person_id)
    rid = new_id("prb")
    with db()() as session:
        session.add(ProblemReport(id=rid, person_id=person_id, workspace_id=wid, venture_id=body.venture_id,
                                  message=body.message.strip(), page=body.page, app_version=body.app_version,
                                  fingerprints_json=json.dumps([a["fingerprint"] for a in attached]),
                                  screenshot=shot, screenshot_type=shot_type, status="open", created_at=utcnow()))
        session.commit()
    return {"id": rid, "attached_errors": len(attached), "screenshot_saved": shot is not None}

def problems_emails(session, ids: set[str]) -> dict[str, str]:
    return dict(session.execute(select(Person.id, Person.email).where(Person.id.in_(ids))).all()) if ids else {}

def problems_admin_errors(status: str = "open", limit: int = 100, ctx: AdminContext = Depends(require_admin("audit.view"))):
    _ = ctx
    if status not in {"open", "resolved", "all"} or not 1 <= limit <= 200:
        raise HTTPException(422, detail={"error": {"code": "invalid_query"}})
    with db()() as session:
        q = select(ErrorReport).order_by(ErrorReport.last_seen.desc()).limit(limit)
        if status != "all":
            q = q.where(ErrorReport.status == status)
        rows = session.scalars(q).all()
        reports = session.scalars(select(ProblemReport).order_by(ProblemReport.created_at.desc()).limit(200)).all()
        linked: dict[str, list[str]] = {}
        for rep in reports:
            for fp in json.loads(rep.fingerprints_json or "[]"):
                linked.setdefault(fp, []).append(rep.id)
        emails = problems_emails(session, {p for r in rows for p in json.loads(r.affected_json or "[]")})
        day = utcnow() - timedelta(hours=24)
        open_errors = session.scalar(select(func.count()).select_from(ErrorReport).where(
            ErrorReport.status == "open", ErrorReport.severity == "error")) or 0
        new_24h = session.scalar(select(func.count()).select_from(ErrorReport).where(ErrorReport.first_seen >= day)) or 0
        open_reports = session.scalar(select(func.count()).select_from(ProblemReport).where(ProblemReport.status == "open")) or 0
    items = [{"id": r.id, "fingerprint": r.fingerprint, "source": r.source, "feature": r.feature, "code": r.code,
              "place": r.place, "frame": r.frame, "severity": r.severity, "status": r.status, "count": r.count,
              "window_count": r.window_count, "first_seen": r.first_seen.isoformat(),
              "last_seen": r.last_seen.isoformat(), "release": r.release, "sample": json.loads(r.sample_json or "{}"),
              "affected": [{"person_id": p, "email": emails.get(p)} for p in json.loads(r.affected_json or "[]")],
              "problem_reports": linked.get(r.fingerprint, [])} for r in rows]
    return {"items": items, "badge": {"open_errors": open_errors, "new_24h": new_24h, "open_reports": open_reports}}

def problems_admin_resolve(error_id: str, ctx: AdminContext = Depends(require_admin("ops.flags"))):
    with db()() as session:
        row = session.get(ErrorReport, error_id)
        if row is None:
            raise HTTPException(404, detail={"error": {"code": "not_found"}})
        row.status, row.resolved_at = "resolved", utcnow()
        row.resolved_by = ctx.admin.email
        row.window_count = 0
        session.commit()
    return {"id": error_id, "status": "resolved"}

def problems_admin_problem_reports(limit: int = 50, ctx: AdminContext = Depends(require_admin("audit.view"))):
    _ = ctx
    if not 1 <= limit <= 200:
        raise HTTPException(422, detail={"error": {"code": "invalid_limit"}})
    with db()() as session:
        rows = session.scalars(select(ProblemReport).order_by(ProblemReport.created_at.desc()).limit(limit)).all()
        emails = problems_emails(session, {r.person_id for r in rows})
    return {"items": [{"id": r.id, "person_id": r.person_id, "email": emails.get(r.person_id), "venture_id": r.venture_id,
                       "message": r.message, "page": r.page, "app_version": r.app_version, "status": r.status,
                       "fingerprints": json.loads(r.fingerprints_json or "[]"), "has_screenshot": r.screenshot is not None,
                       "created_at": r.created_at.isoformat()} for r in rows]}

def problems_admin_screenshot(report_id: str, ctx: AdminContext = Depends(require_admin("audit.view"))):
    _ = ctx
    with db()() as session:
        row = session.get(ProblemReport, report_id)
        if row is None or row.screenshot is None:
            raise HTTPException(404, detail={"error": {"code": "not_found"}})
        return Response(row.screenshot, media_type=row.screenshot_type or "image/png",
                        headers={"Cache-Control": "no-store", "Content-Disposition": "attachment"})
