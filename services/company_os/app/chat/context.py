"""Assemble ChatContext from DB (simplified)."""

from __future__ import annotations

from app.ventures.current_idea import current_idea

from dataclasses import dataclass, field

from sqlalchemy import select

from app.db.models import (
    ActivityLine,
    Venture,
    VentureDocument,
    VentureResource,
)
from app.planning.store import list_current_tasks
from app.state import MEM, db


@dataclass
class ChatContext:
    venture: dict
    charter: dict | None = None
    answers: list = field(default_factory=list)
    demand: dict | None = None
    resources: list = field(default_factory=list)
    team: list = field(default_factory=list)
    documents: list = field(default_factory=list)
    recent_steps: list = field(default_factory=list)
    tasks: list = field(default_factory=list)
    governance: dict = field(default_factory=dict)
    cost: dict = field(default_factory=dict)
    limits: list[str] = field(default_factory=list)
    website: dict = field(default_factory=dict)
    email: str | None = None


def assemble(venture_id: str) -> ChatContext:
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise KeyError(venture_id)
        resources = session.scalars(
            select(VentureResource).where(VentureResource.venture_id == venture_id)
        ).all()
        from app.ventures.startup_review import PRIVATE_KINDS
        docs = session.scalars(
            select(VentureDocument).where(VentureDocument.venture_id == venture_id, VentureDocument.kind.not_in(PRIVATE_KINDS))
        ).all()
        steps = session.scalars(
            select(ActivityLine)
            .where(ActivityLine.venture_id == venture_id)
            .order_by(ActivityLine.seq.desc())
            .limit(40)
        ).all()
        email = None
        for r in resources:
            if r.resource == "email":
                email = r.display
        charter = MEM.get(f"charter:{venture_id}")
        charter = charter if isinstance(charter, dict) else {}
        content = (charter or {}).get("content")
        content = content if isinstance(content, dict) else None
        org = MEM.get(f"org:{venture_id}") or {}
        org = org if isinstance(org, dict) else {}
        demand = MEM.get(f"demand:{venture_id}")
        demand = demand if isinstance(demand, dict) else None
        website = MEM.get(f"website:{venture_id}") or {}
        website = website if isinstance(website, dict) else {}
        from app.chat.recovery import external_site
        if site := external_site(venture_id):
            website = {**website, "url": site["url"], "status": "founder_reported"}
        limits: list[str] = []
        from app.ventures.journey_overrides import NOTE, read as journey_choices
        if journey_choices(venture_id, session=session)["skipped"]:
            limits.append(NOTE)
        if not current_idea(ven, session=session):
            limits.append("No idea summary is saved; use placeholders and ask for scope before external actions.")
        if not demand and not _has_research(venture_id):
            limits.append("No market read yet.")
        if not website.get("url") and not any(r.resource == "hosting" and not r.simulated for r in resources) \
                and not _site_is_live(session, ven.workspace_id, venture_id):
            limits.append("The site is a local preview only; there is no public traffic.")
        if (ven.autonomy_level or "L1") == "L1":
            limits.append("At Suggest level I have not performed any external action.")
        return ChatContext(
            venture={
                "id": ven.id,
                "workspace_id": ven.workspace_id,
                "name": ven.name,
                "stage": ven.stage,
                "raw_idea": current_idea(ven, session=session),
                "autonomy": ven.autonomy_level,
            },
            charter=content,
            answers=_answers(venture_id),
            demand=demand,
            resources=[
                {
                    "resource": r.resource,
                    "simulated": bool(r.simulated),
                    "display": r.display,
                    "url": r.url,
                }
                for r in resources
            ],
            team=org.get("agents") or [],
            documents=[{"id": d.id, "kind": d.kind, "title": d.title} for d in docs],
            recent_steps=[
                {"text": s.text, "status": s.status, "phase": s.phase} for s in steps
            ],
            tasks=list_current_tasks(venture_id),
            governance={"autonomy": ven.autonomy_level or "L1", "label": "Suggest"},
            cost={"revenue_tracked": False},
            limits=limits,
            website=website,
            email=email,
        )


def _site_is_live(session, workspace_id: str | None, venture_id: str) -> bool:
    """A live static deployment (what the Website page shows) means the site is public."""
    if not workspace_id:
        return False
    from app.planning.publication_evidence import public_site_evidence

    try:
        return public_site_evidence(session, workspace_id=workspace_id, venture_id=venture_id) is not None
    except Exception as exc:  # noqa: BLE001 - grounding must never break chat
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "context.public_site")
        return False


def _has_research(venture_id: str) -> bool:
    if MEM.get(f"research_dossier:{venture_id}"):
        return True
    try:
        from app.research.service import list_dossiers

        return bool(list_dossiers(venture_id))
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "context.research")
        return False


def _answers(venture_id: str) -> list[dict]:
    questions = MEM.get(f"questions:{venture_id}") or []
    answers = MEM.get(f"answers:{venture_id}") or {}
    questions = questions if isinstance(questions, list) else []
    answers = answers if isinstance(answers, dict) else {}
    out = []
    for q in questions:
        if not isinstance(q, dict) or not isinstance(q.get("id"), str):
            continue
        aid = answers.get(q["id"])
        opt = next((o for o in q.get("options") or [] if isinstance(o, dict) and o.get("id") == aid), None)
        out.append(
            {
                "question": q.get("prompt"),
                "slot": q.get("slot_key"),
                "chosen": (opt or {}).get("label") or aid,
            }
        )
    return out
