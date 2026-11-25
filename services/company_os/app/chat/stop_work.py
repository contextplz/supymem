"""'Cancel the build / Mac task / research' from chat: stop what can be stopped, say plainly what cannot."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard

_BUILD = re.compile(r"\bbuild\b", re.I)
_MAC = re.compile(r"\b(?:mac|device)\b", re.I)
_RESEARCH = re.compile(r"\bresearch\b", re.I)
_MAC_ACTIVE = {"pending", "reviewed", "approved", "claimed"}


def named(text: str) -> str | None:
    """Which long-running work the founder named, if any."""
    if _MAC.search(text or ""):
        return "mac"
    if _BUILD.search(text or ""):
        return "build"
    if _RESEARCH.search(text or ""):
        return "research"
    return None


def _reply(body: str, actions: list | None = None, kind: str = "text") -> dict[str, Any]:
    return {"kind": kind, "body_md": body, "actions": actions or [], "citations": []}


def active_build(venture_id: str) -> tuple[str, str] | None:
    from sqlalchemy import select

    from app.core.fsm import BUILD_ACTIVE
    from app.db.supymem_models import BuildRun
    from app.state import db

    with db()() as session:
        run = session.scalar(select(BuildRun).where(
            BuildRun.venture_id == venture_id, BuildRun.state.in_(BUILD_ACTIVE), BuildRun.closed_at.is_(None),
            BuildRun.runtime_version == 1).order_by(BuildRun.created_at.desc()).limit(1))
        return (run.id, run.app_id) if run else None


def _active_mac_href(venture_id: str) -> str | None:
    from app.ventures.state_brief import get_venture_state_brief

    try:
        recent = (get_venture_state_brief(venture_id).get("device_work") or {}).get("recent") or []
    except Exception as exc:  # noqa: BLE001 - a brief failure must not break the answer
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "stop.mac_status")
        return f"/v/{venture_id}/settings#settings-computer-use"
    row = next((r for r in recent if isinstance(r, dict) and r.get("status") in _MAC_ACTIVE), None)
    return str(row.get("review_href")) if row and row.get("review_href") else None


def _research_running(venture_id: str) -> bool:
    from sqlalchemy import select

    from app.db.models import Job
    from app.state import db
    from app.ventures.run_status import ACTIVE

    with db()() as session:
        return session.scalar(select(Job.id).where(
            Job.venture_id == venture_id, Job.kind == "research.deep", Job.status.in_(ACTIVE)).limit(1)) is not None


def route(venture_id: str, text: str) -> dict[str, Any] | None:
    """A reply for a named build / Mac task / research, else None (plain 'cancel that' keeps parking tasks)."""
    kind = named(text)
    if kind == "build":
        found = active_build(venture_id)
        if found is None:
            return _reply("No build is running for this venture, so there is nothing to cancel.",
                          [{"label": "Open builds", "href": f"/v/{venture_id}/builds"}])
        run_id, app_id = found
        card = ConfirmationCard.for_action(
            get_action("build.cancel"), interpretation="Cancel the build that is running now.",
            consequences=["The build stops and its sandbox is cleaned up.",
                          "Nothing is published. You can start a new build later."],
            resulting_state={"run_id": run_id})
        return _reply("A build is running. Cancelling stops it, so please confirm.",
                      [replace(card, confirm_label="Cancel the build").as_action()], "confirmation")
    if kind == "mac":
        href = _active_mac_href(venture_id)
        if href is None:
            return _reply("No Mac task is open right now, so there is nothing to cancel.")
        return _reply("I can't cancel a Mac task from chat: stopping one needs your signed approval on its review "
                      "page. Nothing was changed. Open it and use Cancel there.",
                      [{"label": "Open the Mac task", "href": href, "style": "primary"}], kind="navigation")
    if kind == "research":
        if not _research_running(venture_id):
            return _reply("No research is running for this venture, so there is nothing to cancel.",
                          [{"label": "Open research", "href": f"/v/{venture_id}/research"}])
        from sqlalchemy import select

        from app.core.jobs import ACTIVE_JOB_STATUSES, request_cancel
        from app.db.models import Job
        from app.db.session import serialize_writes
        from app.state import db

        with db()() as session:
            serialize_writes(session, key="research-stop:" + venture_id)
            job_ids = session.scalars(select(Job.id).where(Job.venture_id == venture_id,
                Job.kind == "research.deep", Job.status.in_(ACTIVE_JOB_STATUSES)).order_by(Job.id)).all()
            for job_id in job_ids:
                request_cancel(session, job_id)
            session.commit()
        return _reply("Stopping research now. It stops before its next step; anything already in progress "
                      "finishes, and the sources found so far stay saved.",
                      [{"label": "Open research", "href": f"/v/{venture_id}/research", "style": "primary"}])
    return None


def _run_scope(run_id: str) -> tuple[str, str] | None:
    from app.db.supymem_models import BuildRun
    from app.state import db

    with db()() as session:
        run = session.get(BuildRun, run_id) if run_id else None
        return (run.venture_id, run.app_id) if run else None


def run_build_cancel(venture_id: str, payload: dict[str, Any], actor: str | None) -> dict[str, Any]:
    from app.builder import execution as managed_builds

    if not actor:
        raise PermissionError("build_chat_authentication_required")
    run_id = str(payload.get("run_id") or "")
    scope = _run_scope(run_id)
    if scope is None or scope[0] != venture_id:
        raise KeyError("build")
    result = managed_builds.cancel(run_id, actor=actor)
    where = [{"label": "Open builds", "href": f"/v/{venture_id}/builds?app={scope[1]}&run={run_id}", "style": "primary"}]
    if str(result.get("state") or "") == "cancelled":
        return _reply("The build is cancelled. Nothing was published.", where)
    return _reply("Cancellation was requested. The build is stopping and its cleanup is still finishing; "
                  "open the build to see when it is done. Nothing was published.", where)
