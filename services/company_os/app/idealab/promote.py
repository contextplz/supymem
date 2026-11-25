"""Promote an opportunity into a Venture with pre-filled slots (37 §7)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.audit.writer import audit
from app.db.models import CharterSlot, Opportunity, Venture
from app.idealab import slate as slate_mod
from app.idealab.slate import IdeaLabError, OpportunityView
from app.intake.naming import derive_name, unique_venture_name
from app.onboarding.narrator import interpolate, resolve_variant
from app.state import MEM, db
from app.workspaces import service as workspace_service
from app.workspaces.resolve import LOCAL_PERSON_ID
from core.clock import utcnow
from core.enums import VentureStage
from core.ids import new_id

# Doc 37 §7 mapping. `audience` is the live slot key; `target_user` is the
# doc-05 name kept as an alias so promotions stay readable either way.
SLOT_MAP: tuple[tuple[str, str, str], ...] = (
    ("audience", "first_customer", "Who is the first user?"),
    ("mvp_scope", "wedge", "What must be in the first usable version?"),
    ("goal_primary", "why_now", "What is the primary outcome you want in 90 days?"),
    ("validation_hypothesis", "kill_criteria", "What would make this false?"),
)


@dataclass(frozen=True)
class PrefillSlot:
    key: str
    value: str
    source_field: str
    prompt: str
    confirmable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source_field": self.source_field,
            "prompt": self.prompt,
            "confirmable": self.confirmable,
            "source": "idealab",
        }


@dataclass(frozen=True)
class PromoteResult:
    venture_id: str
    workspace_id: str
    opportunity_id: str
    prefilled_slots: list[PrefillSlot]
    teaser_path: str | None
    company: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "venture_id": self.venture_id,
            "workspace_id": self.workspace_id,
            "opportunity_id": self.opportunity_id,
            "prefilled_slots": [s.to_dict() for s in self.prefilled_slots],
            "teaser_path": self.teaser_path,
            "company": self.company,
            "message": interpolate(
                resolve_variant("idealab.promoted"), {"company": self.company}
            ),
        }


def _slots_from(view: OpportunityView) -> list[PrefillSlot]:
    values = {
        "first_customer": view.first_customer,
        "wedge": view.wedge,
        "why_now": view.why_now,
        "kill_criteria": view.kill_criteria,
    }
    slots: list[PrefillSlot] = []
    for key, field, prompt in SLOT_MAP:
        value = (values.get(field) or "").strip()
        if not value:
            continue
        slots.append(
            PrefillSlot(key=key, value=value, source_field=field, prompt=prompt)
        )
    # Doc requires ≥ 3. If somehow thin, synthesize from title/one_liner.
    if len(slots) < 3 and view.one_liner:
        slots.append(
            PrefillSlot(
                key="non_goals",
                value="Anything beyond the wedge until the kill criteria clear.",
                source_field="derived",
                prompt="What are you explicitly not building yet?",
            )
        )
    return slots[:5]


def promote(
    opportunity_id: str,
    *,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
    start: bool = False,
) -> PromoteResult:
    """Create a Venture and carry evidence in as confirmable pre-filled slots."""
    view = slate_mod.get_opportunity(opportunity_id)
    if view.status == "promoted" and view.promoted_venture_id:
        raise IdeaLabError(
            "already_promoted",
            "That opening is already a venture.",
        )
    if view.status == "rejected":
        raise IdeaLabError(
            "rejected",
            "You turned that one down, pick another, or un-reject it first.",
        )

    slots = _slots_from(view)
    if len(slots) < 3:
        raise IdeaLabError(
            "too_thin",
            "That opening doesn't carry enough to start a venture from.",
        )

    idea = f"{view.title}. {view.one_liner}".strip()
    base_name = derive_name(None, view.title or idea)
    name = unique_venture_name(actor_id, base_name)
    workspace = workspace_service.create_workspace(
        kind="venture", name=name, entry_mode="idea", owner_id=actor_id
    )
    workspace_service.merge_settings(workspace.id, {"venture_name_origin": {"name": name, "base": base_name}})
    now = utcnow()
    venture_id = new_id("ven")
    ven = Venture(
        id=venture_id,
        workspace_id=workspace.id,
        name=name,
        raw_idea=idea[:2000],
        stage=VentureStage.DRAFT.value,
        autonomy_level="L1",
        created_at=now,
        updated_at=now,
    )

    factory = db()
    with factory() as session:
        session.add(ven)
        from app.ventures.startup_review import pin_at_creation
        pin_at_creation(session, ven)
        session.flush()
        for slot in slots:
            session.add(
                CharterSlot(
                    id=new_id("slt"),
                    venture_id=venture_id,
                    key=slot.key,
                    kind="input",
                    criticality="important",
                    value_json=json.dumps({"text": slot.value, "confirmable": True}),
                    source="idealab",
                    confidence=0.7,
                    decision_impact=0.8,
                    filled_at=now,
                )
            )
        opp = session.get(Opportunity, opportunity_id)
        if opp is None:
            raise IdeaLabError("opportunity_not_found", "That opening doesn't exist.")
        opp.status = "promoted"
        opp.promoted_venture_id = venture_id
        session.commit()

    from app.entry.service import _welcome, venture_scope

    with venture_scope(venture_id, actor_id=actor_id, workspace_id=workspace.id):
        _welcome(venture_id, workspace.id)

    # Confirmable answers the validate phase can skip after the founder nods.
    answers = {s.key: s.value for s in slots}
    # Doc alias
    if "audience" in answers:
        answers["target_user"] = answers["audience"]
    MEM[f"answers:{venture_id}"] = answers
    MEM[f"answers_source:{venture_id}"] = "idealab"
    MEM[f"prefilled_slots:{venture_id}"] = [s.to_dict() for s in slots]
    if view.evidence:
        MEM[f"evidence_appendix:{venture_id}"] = view.evidence
    if view.kill_criteria:
        MEM[f"validation_hypothesis:{venture_id}"] = view.kill_criteria
    if view.teaser_path:
        MEM[f"first_look_path:{venture_id}"] = view.teaser_path

    audit(
        "idealab.opportunity.promoted",
        workspace_id=view.workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=opportunity_id,
        payload={
            "venture_id": venture_id,
            "workspace_id": workspace.id,
            "prefilled": [s.key for s in slots],
            "count": len(slots),
        },
    )

    if start:
        from app.onboarding.orchestrator import run_onboarding

        # Fire-and-forget style: caller may await via the ventures path later.
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                run_onboarding(
                    venture_id,
                    mode="guided",
                    workspace_id=workspace.id,
                    entry_mode="idea",
                )
            )
        except RuntimeError:
            pass

    return PromoteResult(
        venture_id=venture_id,
        workspace_id=workspace.id,
        opportunity_id=opportunity_id,
        prefilled_slots=slots,
        teaser_path=view.teaser_path,
        company=name,
    )


__all__ = ["PrefillSlot", "PromoteResult", "SLOT_MAP", "promote"]
