"""Local log + incident debugger (docs/supymem/13 §8). CloudWatch Insights is H-18."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import data_root
from app.core.jobs import enqueue
from app.db.supymem_models import AppIncident, FounderApp, FounderRequest
from app.state import db
from app.worker.run_sync import run_kind
from core.clock import utcnow
from core.ids import new_id

_SYNC = "IDEA_SYNC"


def _reject_local_log_secrets(text: str) -> None:
    from app.core.local_mode import is_local
    from app.secrets.detect import looks_like_secret
    from fastapi import HTTPException

    if is_local() and looks_like_secret(text):
        raise HTTPException(409, detail={"code": "debug_request_contains_secret", "message":
            "Remove credentials from your error report. Save keys in Settings > Keys and connections, then retry with a sanitized summary and log line."})


def _sync() -> bool:
    import os

    return os.environ.get(_SYNC, "").strip() in {"1", "true", "yes"}


def log_dir(app_id: str) -> Path:
    path = data_root() / "apps" / app_id / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_log(app_id: str, line: str) -> Path:
    _reject_local_log_secrets(line)
    path = log_dir(app_id) / "app.log"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip() + "\n")
    return path


def read_logs(app_id: str, *, limit: int = 80) -> list[str]:
    path = log_dir(app_id) / "app.log"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[-limit:]


def open_incident(
    app_id: str,
    workspace_id: str,
    *,
    summary: str,
    kind: str = "5xx",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _reject_local_log_secrets(summary)
    factory = db()
    now = utcnow()
    with factory() as session:
        app = session.get(FounderApp, app_id)
        if app is None or app.deleted_at is not None:
            raise KeyError("app")
        row = AppIncident(
            id=new_id("inc"),
            app_id=app_id,
            workspace_id=workspace_id,
            kind=kind,
            state="open",
            summary=summary[:500],
            detail=json.dumps(detail or {}, default=str),
            opened_at=now,
        )
        session.add(row)
        payload = {
            "incident_id": row.id,
            "app_id": app_id,
            "workspace_id": workspace_id,
            "venture_id": app.venture_id,
        }
        job_id = enqueue(
            session,
            "app.debug",
            payload,
            workspace_id=workspace_id,
            venture_id=app.venture_id,
            dedupe_key=f"debug:{row.id}",
        )
        session.commit()
        incident_id = row.id
    if _sync():
        run_kind("app.debug", payload)
    return {"id": incident_id, "job_id": job_id, "state": "open"}


def list_incidents(app_id: str) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = list(
            session.scalars(
                select(AppIncident)
                .where(AppIncident.app_id == app_id)
                .order_by(AppIncident.opened_at.desc())
            )
        )
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "state": r.state,
                "summary": r.summary,
                "detail": json.loads(r.detail or "{}"),
                "opened_at": r.opened_at.isoformat() if r.opened_at else None,
                "debug_run_id": r.debug_run_id,
            }
            for r in rows
        ]


def get_incident(incident_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.get(AppIncident, incident_id)
        if row is None:
            raise KeyError("incident")
        return {
            "id": row.id,
            "app_id": row.app_id,
            "workspace_id": row.workspace_id,
            "kind": row.kind,
            "state": row.state,
            "summary": row.summary,
            "detail": json.loads(row.detail or "{}"),
            "opened_at": row.opened_at.isoformat() if row.opened_at else None,
            "debug_run_id": row.debug_run_id,
        }


def start_debug(incident_id: str) -> dict[str, Any]:
    from app.core.local_mode import is_local

    data = get_incident(incident_id)
    factory = db()
    with factory() as session:
        app = session.get(FounderApp, data["app_id"])
        if app is None:
            raise KeyError("app")
        payload = {
            "incident_id": incident_id,
            "app_id": data["app_id"],
            "workspace_id": data["workspace_id"],
            "venture_id": app.venture_id,
        }
        if is_local():
            from app.builder.ctl import request_local_build

            order = request_local_build(app.venture_id, data["summary"], repair=True)
            return {"device_work_order_id": order["id"], "review_path": order["review_path"], "status": "queued", "incident": data}
        job_id = enqueue(
            session,
            "app.debug",
            payload,
            workspace_id=data["workspace_id"],
            venture_id=app.venture_id,
            dedupe_key=f"debug:{incident_id}:rerun",
        )
        session.commit()
    if _sync():
        run_kind("app.debug", payload)
        data = get_incident(incident_id)
    return {"job_id": job_id, "incident": data}


def propose_fix(logs: list[str], summary: str) -> dict[str, Any]:
    blob = "\n".join(logs[-40:] + [summary]).lower()
    cause = "unknown"
    fix = "Add a health check and retry the last deploy."
    if "timeout" in blob or "timed out" in blob:
        cause = "upstream_timeout"
        fix = "Increase the request timeout and cache the slow path."
    elif "5xx" in blob or "internal server error" in blob or "traceback" in blob:
        cause = "unhandled_exception"
        fix = "Catch the exception on the failing route and return a 4xx with a founder-readable message."
    elif "oom" in blob or "killed" in blob:
        cause = "memory"
        fix = "Drop the infra tier one step or bound the in-memory cache."
    elif "secret" in blob or "credential" in blob:
        cause = "leaked_secret"
        fix = "Rotate the leaked secret slot and scrub it from logs."
    return {
        "symptom": summary,
        "likely_cause": cause,
        "fix_proposed": fix,
        "log_lines": len(logs),
        "source": "local_logs",
    }


def run_debug(payload: dict[str, Any]) -> dict[str, Any]:
    incident_id = payload["incident_id"]
    app_id = payload["app_id"]
    factory = db()
    now = utcnow()
    logs = read_logs(app_id)
    with factory() as session:
        row = session.get(AppIncident, incident_id)
        if row is None:
            raise KeyError("incident")
        detail = json.loads(row.detail or "{}")
        proposal = propose_fix(logs, row.summary)
        req = FounderRequest(
            id=new_id("frq"),
            workspace_id=row.workspace_id,
            venture_id=payload.get("venture_id") or "",
            source="iteration",
            text=row.summary,
            intent="debug_fix",
            intent_category="debug",
            classification="in",
            outcome="proposed",
            related_id=row.id,
            created_at=now,
        )
        session.add(req)
        session.flush()
        detail["proposal"] = proposal
        detail["founder_request_id"] = req.id
        row.detail = json.dumps(detail, default=str)
        row.debug_run_id = req.id
        row.state = "diagnosed"
        session.commit()
        return {"incident_id": row.id, "proposal": proposal, "founder_request_id": req.id}
