"""Idea Lab service: slates, rerolls, save/reject memory, teasers (37 §2, §6).

Routes call this; this owns the ORM. Nothing here is founder-facing copy — the
stamp and the activity lines come from the narration deck.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from app.audit.writer import audit
from app.db.models import Opportunity, OpportunitySlate
from app.idealab import corpus as corpus_mod
from app.idealab import generate as generate_mod
from app.idealab.filters import Candidate
from app.idealab.generate import COST_CAP_USD, SLATE_SIZE, WALL_CLOCK_S, Seed
from app.onboarding.narrator import resolve_variant
from app.state import db
from app.workspaces.resolve import LOCAL_PERSON_ID
from core.clock import utcnow
from core.ids import new_id

REJECT_REASONS = frozenset(
    {"not_interesting", "too_crowded", "too_hard", "wrong_for_me", "other"}
)
STATUSES = frozenset({"proposed", "saved", "rejected", "promoted"})


class IdeaLabError(ValueError):
    """Invalid Idea Lab request. Carries a founder-readable message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OpportunityView:
    id: str
    slate_id: str
    workspace_id: str
    title: str
    one_liner: str
    first_customer: str
    wedge: str
    why_now: str
    kill_criteria: str
    evidence: list[dict[str, Any]]
    shape_tags: list[str]
    effort_days: int
    confidence: float
    researched: bool
    status: str
    reject_reason: str | None
    teaser_path: str | None
    promoted_venture_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slate_id": self.slate_id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "one_liner": self.one_liner,
            "first_customer": self.first_customer,
            "wedge": self.wedge,
            "why_now": self.why_now,
            "kill_criteria": self.kill_criteria,
            "evidence": self.evidence,
            "shape_tags": self.shape_tags,
            "effort_days": self.effort_days,
            "confidence": self.confidence,
            "researched": self.researched,
            "evidence_stamp": stamp(self.researched),
            "status": self.status,
            "reject_reason": self.reject_reason,
            "teaser_path": self.teaser_path,
            "promoted_venture_id": self.promoted_venture_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SlateView:
    id: str
    workspace_id: str
    round: int
    seed: dict[str, Any]
    seed_hash: str
    cost_usd: float
    duration_s: float
    researched: bool
    opportunities: list[OpportunityView]
    dropped: list[dict[str, str]]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "round": self.round,
            "seed": self.seed,
            "seed_hash": self.seed_hash,
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
            "researched": self.researched,
            "evidence_stamp": stamp(self.researched),
            "size": len(self.opportunities),
            "opportunities": [o.to_dict() for o in self.opportunities],
            "dropped": self.dropped,
            "reason": self.reason,
        }


def stamp(researched: bool) -> str:
    """Honesty suffix (27 §5): priors are never passed off as research."""
    return resolve_variant("idealab.researched" if researched else "idealab.reasoned")


def _view(row: Opportunity) -> OpportunityView:
    return OpportunityView(
        id=row.id,
        slate_id=row.slate_id,
        workspace_id=row.workspace_id,
        title=row.title,
        one_liner=row.one_liner,
        first_customer=row.first_customer,
        wedge=row.wedge,
        why_now=row.why_now,
        kill_criteria=row.kill_criteria,
        evidence=_json_list(row.evidence_json),
        shape_tags=[str(t) for t in _json_list(row.shape_tags_json)],
        effort_days=row.effort_days,
        confidence=row.confidence,
        researched=bool(row.researched),
        status=row.status,
        reject_reason=row.reject_reason,
        teaser_path=row.teaser_path,
        promoted_venture_id=row.promoted_venture_id,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def _json_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def rejected_shapes(workspace_id: str) -> list[frozenset[str]]:
    """Anti-repeat memory: every shape this workspace has turned down (37 §5.2)."""
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(Opportunity).where(
                Opportunity.workspace_id == workspace_id,
                Opportunity.status == "rejected",
            )
        ).all()
        shapes: list[frozenset[str]] = []
        for row in rows:
            tags = {str(t).strip().lower() for t in _json_list(row.shape_tags_json)}
            if tags:
                shapes.append(frozenset(tags))
        return shapes


def shown_keys(workspace_id: str) -> list[str]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(Opportunity).where(Opportunity.workspace_id == workspace_id)
        ).all()
        return [
            Candidate(
                title=r.title,
                one_liner=r.one_liner,
                first_customer=r.first_customer,
                wedge=r.wedge,
                why_now=r.why_now,
                kill_criteria=r.kill_criteria,
            ).key
            for r in rows
        ]


def seed_of(slate_id: str, *, workspace_id: str) -> tuple[dict[str, Any], int]:
    factory = db()
    with factory() as session:
        row = session.get(OpportunitySlate, slate_id)
        if row is None or row.workspace_id != workspace_id:
            raise IdeaLabError("slate_not_found", "That slate doesn't exist.")
        try:
            seed = json.loads(row.seed_json or "{}")
        except json.JSONDecodeError:
            seed = {}
        return (seed if isinstance(seed, dict) else {}), int(row.round or 1)


async def create_slate(
    *,
    workspace_id: str,
    seed_input: Mapping[str, Any] | None = None,
    round: int = 1,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
    budget_usd: float = COST_CAP_USD,
) -> SlateView:
    """One click, no form: an empty seed still produces a slate (37 §3)."""
    seed = Seed.from_input(seed_input)
    generated = await generate_mod.generate_slate(
        seed,
        round=round,
        rejected_shapes=rejected_shapes(workspace_id),
        seen_keys=shown_keys(workspace_id),
        budget_usd=budget_usd,
    )

    now = utcnow()
    slate_id = new_id("ops")
    factory = db()
    with factory() as session:
        session.add(
            OpportunitySlate(
                id=slate_id,
                workspace_id=workspace_id,
                seed_json=json.dumps(seed.to_dict(), sort_keys=True),
                seed_hash=seed.hash,
                round=round,
                cost_usd=generated.cost_usd,
                created_at=now,
            )
        )
        session.flush()
        for cand in generated.opportunities:
            session.add(
                Opportunity(
                    id=new_id("opp"),
                    slate_id=slate_id,
                    workspace_id=workspace_id,
                    title=cand.title,
                    one_liner=cand.one_liner,
                    first_customer=cand.first_customer,
                    wedge=cand.wedge,
                    why_now=cand.why_now,
                    evidence_json=json.dumps(cand.evidence),
                    kill_criteria=cand.kill_criteria,
                    shape_tags_json=json.dumps(sorted(cand.shape)),
                    effort_days=cand.effort_days,
                    confidence=cand.confidence,
                    researched=1 if cand.researched else 0,
                    status="proposed",
                    created_at=now,
                )
            )
        session.commit()

    audit(
        "idealab.slate.created",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=slate_id,
        outcome="ok" if generated.opportunities else "partial",
        payload={
            "round": round,
            "seed_hash": seed.hash,
            "size": len(generated.opportunities),
            "dropped": [{"rule": d.rule, "detail": d.detail} for d in generated.dropped],
            "cost_usd": generated.cost_usd,
            "duration_s": float(f"{generated.duration_s:.3f}"),
            "over_budget": generated.cost_usd > budget_usd,
            "over_wall_clock": generated.duration_s > WALL_CLOCK_S,
            "researched": generated.researched,
        },
    )
    return replace(
        get_slate(slate_id),
        duration_s=float(f"{generated.duration_s:.3f}"),
        dropped=[{"rule": d.rule, "detail": d.detail} for d in generated.dropped],
        reason=generated.reason,
    )


async def reroll(
    *,
    workspace_id: str,
    previous_slate_id: str,
    reject_reasons: Sequence[Mapping[str, Any]] = (),
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
    budget_usd: float = COST_CAP_USD,
) -> SlateView:
    """Rerolls are memoried: the reason steers the next slate (37 §1)."""
    seed, previous_round = seed_of(previous_slate_id, workspace_id=workspace_id)
    rejections = []
    for entry in reject_reasons or ():
        if not isinstance(entry, Mapping):
            raise IdeaLabError("invalid_rejection", "Each rejection needs an opening.")
        opportunity_id = entry.get("opportunity_id")
        reason_code = entry.get("reason_code") or "other"
        note = entry.get("note")
        if (
            not isinstance(opportunity_id, str) or not opportunity_id.strip()
            or not isinstance(reason_code, str)
            or (note is not None and not isinstance(note, str))
        ):
            raise IdeaLabError("invalid_rejection", "Each rejection needs an opening and a text reason.")
        opportunity_id = opportunity_id.strip()
        get_opportunity(opportunity_id, workspace_id=workspace_id)
        rejections.append((opportunity_id, reason_code, note))
    for opportunity_id, reason_code, note in rejections:
        reject_opportunity(
            opportunity_id,
            workspace_id=workspace_id,
            reason_code=reason_code,
            note=note,
            actor_id=actor_id,
            surface=surface,
        )
    return await create_slate(
        workspace_id=workspace_id,
        seed_input=seed,
        round=previous_round + 1,
        actor_id=actor_id,
        surface=surface,
        budget_usd=budget_usd,
    )


def get_slate(slate_id: str) -> SlateView:
    factory = db()
    with factory() as session:
        row = session.get(OpportunitySlate, slate_id)
        if row is None:
            raise IdeaLabError("slate_not_found", "That slate doesn't exist.")
        opportunities = session.scalars(
            select(Opportunity)
            .where(Opportunity.slate_id == slate_id)
            .order_by(Opportunity.created_at, Opportunity.id)
        ).all()
        try:
            seed = json.loads(row.seed_json or "{}")
        except json.JSONDecodeError:
            seed = {}
        views = [_view(o) for o in opportunities]
        return SlateView(
            id=row.id,
            workspace_id=row.workspace_id,
            round=int(row.round or 1),
            seed=seed if isinstance(seed, dict) else {},
            seed_hash=row.seed_hash or "",
            cost_usd=row.cost_usd,
            duration_s=0.0,
            researched=bool(views) and all(v.researched for v in views),
            opportunities=views,
            dropped=[],
            reason="" if views else "no_evidence",
        )


def latest_slate(workspace_id: str) -> SlateView | None:
    """Most recent slate for a workspace, or None when the bench is empty."""
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(OpportunitySlate)
            .where(OpportunitySlate.workspace_id == workspace_id)
            .order_by(OpportunitySlate.created_at.desc(), OpportunitySlate.id.desc())
        ).first()
        if row is None:
            return None
        slate_id = row.id
    return get_slate(slate_id)


def get_opportunity(opportunity_id: str, *, workspace_id: str | None = None) -> OpportunityView:
    factory = db()
    with factory() as session:
        row = session.get(Opportunity, opportunity_id)
        if row is None or (workspace_id is not None and row.workspace_id != workspace_id):
            raise IdeaLabError("opportunity_not_found", "That opening doesn't exist.")
        return _view(row)


def list_opportunities(
    workspace_id: str, *, status: str | None = None
) -> list[OpportunityView]:
    factory = db()
    with factory() as session:
        stmt = select(Opportunity).where(Opportunity.workspace_id == workspace_id)
        if status:
            stmt = stmt.where(Opportunity.status == status)
        rows = session.scalars(
            stmt.order_by(Opportunity.created_at.desc(), Opportunity.id)
        ).all()
        return [_view(r) for r in rows]


def _set_status(
    opportunity_id: str,
    status: str,
    *,
    workspace_id: str,
    reject_reason: str | None = None,
) -> OpportunityView:
    factory = db()
    with factory() as session:
        row = session.get(Opportunity, opportunity_id)
        if row is None or row.workspace_id != workspace_id:
            raise IdeaLabError("opportunity_not_found", "That opening doesn't exist.")
        row.status = status
        if reject_reason is not None:
            row.reject_reason = reject_reason
        session.commit()
        session.refresh(row)
        return _view(row)


def save_opportunity(
    opportunity_id: str, *, workspace_id: str, actor_id: str = LOCAL_PERSON_ID, surface: str = "api"
) -> OpportunityView:
    view = _set_status(opportunity_id, "saved", workspace_id=workspace_id)
    audit(
        "idealab.opportunity.saved",
        workspace_id=view.workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=view.id,
        payload={"title": view.title},
    )
    return view


def reject_opportunity(
    opportunity_id: str,
    *,
    workspace_id: str,
    reason_code: str,
    note: str | None = None,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
) -> OpportunityView:
    """The highest-value signal we collect: it steers every later slate."""
    code = reason_code if reason_code in REJECT_REASONS else "other"
    reason = f"{code}: {note.strip()}" if note and note.strip() else code
    view = _set_status(opportunity_id, "rejected", workspace_id=workspace_id, reject_reason=reason[:500])
    audit(
        "idealab.opportunity.rejected",
        workspace_id=view.workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=view.id,
        payload={"reason_code": code, "shape_tags": view.shape_tags},
    )
    return view


# --- "See it real" (T10.42) ----------------------------------------------


def teaser_dir() -> Path:
    return corpus_mod.data_dir() / "teasers"


def teaser_files() -> list[Path]:
    folder = teaser_dir()
    return sorted(folder.glob("*.html")) if folder.is_dir() else []


def generate_teaser(
    opportunity_id: str, *, actor_id: str = LOCAL_PERSON_ID, surface: str = "api"
) -> OpportunityView | dict[str, Any]:
    """On demand only: three unopened pages per slate would triple the cost."""
    view = get_opportunity(opportunity_id)
    from app.core.local_mode import is_local

    if is_local():
        from app.landing.service import prepare_page_build

        brief = {"title": view.title, "one_liner": view.one_liner, "first_customer": view.first_customer,
            "wedge": view.wedge, "why_now": view.why_now, "kill_criteria": view.kill_criteria,
            "evidence": view.evidence, "purpose": "Idea teaser, not a launched product"}
        order = prepare_page_build(view.promoted_venture_id, brief, actor_id=actor_id)
        return view.to_dict() | {"build": order,
            "message": "Open Builds to follow your page build and review its checks before publishing."}
    folder = teaser_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{view.id}.html"
    path.write_text(_teaser_html(view), encoding="utf-8")

    factory = db()
    with factory() as session:
        row = session.get(Opportunity, opportunity_id)
        if row is not None:
            row.teaser_path = str(path)
            session.commit()
    audit(
        "idealab.teaser.generated",
        workspace_id=view.workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=view.id,
        payload={"path": str(path)},
    )
    return get_opportunity(opportunity_id)


def _teaser_html(view: OpportunityView) -> str:
    """Self-contained, no external requests, and only this opening's own words."""
    evidence = "".join(
        f"<li>{html.escape(str(item.get('claim') or ''))}"
        f"<span>{html.escape(str(item.get('source') or ''))}</span></li>"
        for item in view.evidence
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(view.title)}</title>"
        "<style>body{font:16px/1.6 system-ui,sans-serif;margin:0;padding:3rem 1.5rem;"
        "max-width:44rem;margin-inline:auto;color:#111}h1{font-size:2rem;margin:0 0 .5rem}"
        "p.lead{font-size:1.15rem;color:#333}dl{display:grid;grid-template-columns:auto 1fr;"
        "gap:.35rem 1rem;margin:2rem 0}dt{font-weight:600}ul{padding-left:1.1rem}"
        "li span{display:block;color:#666;font-size:.85rem}footer{margin-top:3rem;"
        "color:#666;font-size:.9rem;border-top:1px solid #eee;padding-top:1rem}</style>"
        "</head><body>"
        f"<h1>{html.escape(view.title)}</h1>"
        f"<p class=\"lead\">{html.escape(view.one_liner)}</p>"
        "<dl>"
        f"<dt>First customer</dt><dd>{html.escape(view.first_customer)}</dd>"
        f"<dt>Smallest sellable thing</dt><dd>{html.escape(view.wedge)}</dd>"
        f"<dt>Time to that</dt><dd>about {view.effort_days} days</dd>"
        f"<dt>Why now</dt><dd>{html.escape(view.why_now)}</dd>"
        "</dl>"
        f"<h2>Evidence</h2><ul>{evidence}</ul>"
        f"<h2>Would be wrong if…</h2><p>{html.escape(view.kill_criteria)}</p>"
        f"<footer>{html.escape(stamp(view.researched))}</footer>"
        "</body></html>"
    )


__all__ = [
    "COST_CAP_USD",
    "IdeaLabError",
    "OpportunityView",
    "REJECT_REASONS",
    "SLATE_SIZE",
    "SlateView",
    "create_slate",
    "generate_teaser",
    "get_opportunity",
    "get_slate",
    "latest_slate",
    "list_opportunities",
    "reject_opportunity",
    "rejected_shapes",
    "reroll",
    "save_opportunity",
    "stamp",
    "teaser_files",
]
