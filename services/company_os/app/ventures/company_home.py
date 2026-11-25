"""Private company-home projections and reversible navigation pins."""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import select

from app.authz.workspace import assert_venture_access
from app.core.jobs import ACTIVE_JOB_STATUSES
from app.db.models import GtmContact, GtmTouch, HumanTask, Job, PlannedTask, Venture, VentureDocument
from app.db.session import serialize_writes
from app.db.supymem_models import BuildRun, ExecutionPlan, FounderApp, ResearchDossier
from app.db.task_models import Task
from app.intake.evidence import KIND as EVIDENCE_KIND
from app.state import db
from core.clock import as_utc, utcnow

DOCUMENT_FOLDERS = {
    "validation_dossier": "research", "market": "research",
    "external_site": "website", "gtm_journey": "outreach", "outreach_draft": "outreach", "task_report": "tasks",
    EVIDENCE_KIND: "files", "mission": "knowledge", "roadmap": "knowledge", "recap": "knowledge",
}
JOB_DESTINATIONS = {
    "idea.clarify_turn": ("research", "Clarification", "idea"),
    "idea.validate": ("research", "Idea validation", "idea"),
    "research.deep": ("research", "Research", "research"),
    "plan.compile": ("knowledge", "Execution plan", "plan"),
    "pagebuilder.build": ("website", "Landing page", "site"),
    "gtm.journey.prepare": ("outreach", "Customer research", None),
}


def _scope(venture_id, subject):
    if not subject or subject.strip() in {"", "anonymous", "system"}:
        raise HTTPException(401, detail="Sign in to open your company.")
    return assert_venture_access(venture_id, subject)[1]


def _pins(session, venture_id):
    row = session.get(VentureDocument, f"company_ui_pins:{venture_id}")
    if row is None:
        return []
    try:
        pins = json.loads(row.body_md)
        if row.venture_id != venture_id or row.kind != "company_ui_pins" or not isinstance(pins, list) or not all(isinstance(pin, str) for pin in pins):
            raise ValueError()
        return pins
    except (TypeError, ValueError):
        raise HTTPException(409, detail="Saved pins could not be read. Your work is safe; use the page list instead.")


def read_pins(venture_id, subject):
    _scope(venture_id, subject)
    with db()() as session:
        return {"venture_id": venture_id, "pins": _pins(session, venture_id)}


def change_pin(venture_id, subject, department, pinned):
    _scope(venture_id, subject)
    with db()() as session:
        serialize_writes(session, key=f"company-ui-pins:{venture_id}")
        session.get(Venture, venture_id, with_for_update=True)
        pins = _pins(session, venture_id)
        if pinned and department not in pins:
            if len(pins) >= 32:
                raise HTTPException(409, detail="Remove a pin before adding another (32 maximum).")
            pins.append(department)
        elif not pinned:
            pins = [pin for pin in pins if pin != department]
        now = utcnow()
        row = session.get(VentureDocument, f"company_ui_pins:{venture_id}")
        if row is None:
            row = VentureDocument(id=f"company_ui_pins:{venture_id}", venture_id=venture_id,
                                  kind="company_ui_pins", title="Quick access", created_at=now, updated_at=now)
            session.add(row)
        row.body_md, row.updated_at = json.dumps(pins), now
        session.commit()
        return {"venture_id": venture_id, "pins": pins}


def archive(venture_id, subject):
    workspace_id = _scope(venture_id, subject)
    base = f"/v/{quote(venture_id, safe='')}"
    records = []
    open_jobs = 0

    def add(row, folder, title, href, status=None, created=None, updated=None, document=False):
        records.append({"id": row.id, "folder": folder, "title": title, "href": href, "status": status,
                        "created_at": as_utc(created).isoformat() if created else None,
                        "updated_at": as_utc(updated).isoformat() if updated else None, "document": document,
                        "kind": getattr(row, "kind", None) or row.__tablename__,
                        "agent_id": getattr(row, "owner_agent_id", None),
                        "agent_role": getattr(row, "agent", None), "integration": getattr(row, "provider", None)})

    with db()() as session:
        for row in session.scalars(select(Job).where(Job.workspace_id == workspace_id, Job.venture_id == venture_id, Job.kind.in_(JOB_DESTINATIONS))):
            folder, title, page = JOB_DESTINATIONS[row.kind]
            add(row, folder, title, f"{base}/{page}" if page else f"/gtm?venture={quote(venture_id, safe='')}", row.status, row.created_at, row.finished_at)
            open_jobs += row.status in ACTIVE_JOB_STATUSES
        for row in session.scalars(select(ExecutionPlan).where(ExecutionPlan.workspace_id == workspace_id, ExecutionPlan.venture_id == venture_id)):
            add(row, "knowledge", f"Execution plan · version {row.version}", f"{base}/plan", row.state, row.created_at, row.approved_at)
        for row in session.scalars(select(ResearchDossier).where(ResearchDossier.workspace_id == workspace_id, ResearchDossier.venture_id == venture_id)):
            add(row, "research", row.kind.replace("_", " "), f"{base}/research?dossier={quote(row.id, safe='')}", row.state, row.created_at, row.completed_at)
        apps = list(session.scalars(select(FounderApp).where(FounderApp.workspace_id == workspace_id, FounderApp.venture_id == venture_id, FounderApp.deleted_at.is_(None))))
        names = {row.id: row.name for row in apps}
        for row in apps:
            add(row, "website", row.name, f"{base}/builds?app={quote(row.id, safe='')}", row.state, row.created_at, row.updated_at)
        for row in session.scalars(select(BuildRun).where(BuildRun.workspace_id == workspace_id, BuildRun.venture_id == venture_id, BuildRun.app_id.in_(names))):
            add(row, "website", names[row.app_id], f"{base}/builds?app={quote(row.app_id, safe='')}&run={quote(row.id, safe='')}", row.state, row.created_at or row.started_at, row.finished_at)
        for row in session.scalars(select(PlannedTask).where(PlannedTask.venture_id == venture_id, (PlannedTask.workspace_id == workspace_id) | PlannedTask.workspace_id.is_(None))):
            add(row, "tasks", row.title, f"{base}/tasks", row.status, row.created_at, row.updated_at)
        for row in session.scalars(select(Task).where(Task.workspace_id == workspace_id, Task.venture_id == venture_id)):
            add(row, "tasks", row.title, f"{base}/tasks", row.state, row.created_at, row.updated_at)
        for row in session.scalars(select(HumanTask).where(HumanTask.venture_id == venture_id)):
            add(row, "tasks", row.title, f"{base}/tasks", row.status, row.created_at, row.updated_at)
        for row in session.scalars(select(GtmContact).where(GtmContact.workspace_id == workspace_id, GtmContact.source.startswith(f"venture:{venture_id}:", autoescape=True))):
            add(row, "outreach", " ".join(part for part in (row.first_name, row.last_name) if part) or row.email or row.linkedin_url or row.id,
                f"/gtm?venture={quote(venture_id, safe='')}", row.relationship_stage, row.created_at, row.stage_changed_at)
        for row in session.scalars(select(GtmTouch).where(GtmTouch.workspace_id == workspace_id, GtmTouch.venture_id == venture_id, GtmTouch.simulated == 0)):
            add(row, "outreach", row.subject or row.channel, f"/gtm?venture={quote(venture_id, safe='')}", row.outcome, row.created_at, row.sent_at)
        seen_evidence = set()
        for row in session.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture_id, VentureDocument.kind.in_(DOCUMENT_FOLDERS)).order_by(VentureDocument.created_at, VentureDocument.id)):
            if row.kind == EVIDENCE_KIND:
                try:
                    provenance = json.loads(row.provenance_json or "{}")
                except ValueError:
                    continue
                if not isinstance(provenance, dict) or not provenance.get("evidence_id") or provenance.get("source_kind") != "file" or provenance.get("evidence_id") in seen_evidence:
                    continue
                seen_evidence.add(provenance.get("evidence_id"))
            folder = DOCUMENT_FOLDERS[row.kind]
            href = f"/gtm?venture={quote(venture_id, safe='')}" if folder == "outreach" else f"{base}/" + {"research": "research", "website": "site", "tasks": "tasks", "files": "context", "knowledge": "context"}[folder]
            add(row, folder, row.title, href, created=row.created_at, updated=row.updated_at,
                document=row.kind not in {"gtm_journey", "outreach_draft"})
    open_states = {"open", "running", "submitted", "working", "input_required", "auth_required", "approval_required", "paused"}
    return {"venture_id": venture_id, "open_work": open_jobs + sum(item["folder"] == "tasks" and item["status"] in open_states for item in records),
            "items": sorted(records, key=lambda item: (item["updated_at"] or item["created_at"] or "", item["id"]), reverse=True)}


def document(venture_id, subject, document_id):
    _scope(venture_id, subject)
    with db()() as session:
        row = session.get(VentureDocument, document_id)
        if row is None or row.venture_id != venture_id or row.kind not in DOCUMENT_FOLDERS or row.kind in {"gtm_journey", "outreach_draft"}:
            raise HTTPException(404, detail="This saved document is unavailable. Open its page or reload archives.")
        text = row.body_md or ""
        if row.kind == EVIDENCE_KIND:
            try:
                provenance = json.loads(row.provenance_json or "{}")
            except ValueError:
                raise HTTPException(409, detail="Attachment metadata could not be read. Reload archives or open its original page.")
            identifier = provenance.get("evidence_id") if isinstance(provenance, dict) else None
            if not identifier:
                raise HTTPException(404, detail="Reload archives to open this attachment.")
            chunks = session.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture_id,
                                     VentureDocument.kind == EVIDENCE_KIND, VentureDocument.id.startswith(identifier + ":", autoescape=True))).all()
            chunks = sorted(chunks, key=lambda chunk: json.loads(chunk.provenance_json)["chunk"])
            text = "\n\n".join(chunk.body_md or "" for chunk in chunks)
        return {"id": row.id, "title": row.title, "text": text}
