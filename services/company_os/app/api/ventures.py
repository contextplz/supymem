"""Ventures CRUD + stage transitions + soft-delete lifecycle."""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from app.api.schemas import CreateVenture, StageBody
from app.authz.workspace import (
    accessible_workspace_ids,
    assert_venture_access,
    ensure_subject_person,
)
from app.core.stages import transition
from app.core.local_mode import is_local
from app.db.models import Venture, VentureDocument
from app.intake.naming import VENTURE_NAME_MAX_CHARS, derive_name, looks_like_id, unique_venture_name
from app.secrets.detect import looks_like_secret
from app.state import MEM, db
from app.ventures import lifecycle
from app.workspaces import service as workspace_service
from core.clock import utcnow
from core.enums import VentureStage
from core.errors import IllegalTransition
from core.ids import new_id

router = APIRouter(tags=["ventures"])


class DeleteVentureBody(BaseModel):
    confirmation: str = Field(
        ...,
        description="Must match DELETE (any capitalization).",
    )


class PauseVentureBody(BaseModel):
    reason: str | None = None


class ResumeVentureBody(BaseModel):
    pass


class RenameVentureBody(BaseModel):
    name: str


class VentureCardBody(BaseModel):
    card_type: Literal[
        "software-ai",
        "marketplace",
        "consumer-brand",
        "service-business",
        "media-community",
        "hardware",
    ]
    card_color: Literal["blue", "green", "pink", "orange"]
    card_art: Literal["3", "4", "5", "6", "7", "8", "9", "10"] | None = None


class ManagerNameBody(BaseModel):
    manager_name: str = Field(..., max_length=40)


class AutonomyBody(BaseModel):
    level: Literal["L0", "L1", "L2", "L3", "L4"]


class GtmOrderBody(BaseModel):
    gtm_order: Literal["product_first", "market_first"]


class PageChoiceBody(BaseModel):
    choice: Literal["skip", "build"]


def _lifecycle_http(exc: lifecycle.VentureLifecycleError) -> HTTPException:
    status = {
        "not_found": 404,
        "confirmation_mismatch": 422,
        "already_deleted": 409,
        "not_deleted": 409,
        "already_paused": 409,
        "not_paused": 409,
        "retention_expired": 410,
    }.get(exc.code, 422)
    return HTTPException(
        status, detail={"error": {"code": exc.code, "message": exc.message}}
    )


@router.get("/ventures")
def list_ventures(
    x_subject: str | None = Header(default=None, alias="X-Subject"),
    summary: bool = False,
):
    return {
        "items": lifecycle.list_active(
            workspace_ids=accessible_workspace_ids(x_subject), summary=summary
        )
    }


@router.get("/ventures/deleted")
def list_deleted_ventures(
    x_subject: str | None = Header(default=None, alias="X-Subject"),
    summary: bool = False,
):
    """Soft-deleted ventures still inside the 7-day recovery window."""
    return {
        "items": lifecycle.list_deleted(
            workspace_ids=accessible_workspace_ids(x_subject), summary=summary
        ),
        "retention_days": lifecycle.RETENTION_DAYS,
        "confirmation_phrase": lifecycle.confirmation_phrase(),
    }


@router.get("/ventures/delete-phrase")
def get_delete_phrase():
    return {
        "phrase": lifecycle.confirmation_phrase(),
        "retention_days": lifecycle.RETENTION_DAYS,
        "instruction": "Type DELETE to confirm.",
    }


@router.post("/ventures", status_code=201)
async def create_venture(
    payload: CreateVenture,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    idea = (payload.raw_idea or "").strip()
    if idea and len(idea) < 12:
        raise HTTPException(
            422,
            detail={
                "error": {
                    "code": "idea_too_short",
                    "message": "Tell me a bit more, at least a short sentence.",
                }
            },
        )
    if len(idea) > 2000:
        raise HTTPException(
            422,
            detail={
                "error": {
                    "code": "idea_too_long",
                    "message": "That's too long, keep the idea under 2000 characters.",
                }
            },
        )

    owner_id = ensure_subject_person(x_subject)
    from app.ventures.quota import VentureLimitReached, assert_can_create_venture

    try:
        assert_can_create_venture(owner_id)
    except VentureLimitReached as exc:
        raise HTTPException(
            403, detail={"error": {"code": exc.code, "message": exc.message}}
        ) from exc
    base_name = derive_name(payload.name, idea)
    name = unique_venture_name(owner_id, base_name)
    factory = db()
    now = utcnow()
    # Every venture is a workspace (35 §3); the venture row stays as the legacy
    # key so /ventures/{id}/... keeps working.
    workspace = workspace_service.create_workspace(
        kind="venture",
        name=name,
        entry_mode="idea",
        owner_id=owner_id,
    )
    workspace_service.merge_settings(workspace.id, {"venture_name_origin": {"name": name, "base": base_name}})
    from app.core.local_mode import sample_view
    sample = workspace.settings.get("sample") is True
    ven = Venture(
        id=new_id("ven"),
        workspace_id=workspace.id,
        name=name,
        raw_idea=idea,
        stage=VentureStage.DRAFT.value,
        autonomy_level="L1",
        created_at=now,
        updated_at=now,
    )
    with factory() as session:
        session.add(ven)
        from app.ventures.startup_review import pin_at_creation
        pin_at_creation(session, ven)
        session.commit()
        session.refresh(ven)

    # Legacy A1–A7 pass name and expect no auto-onboard; new-style omits name → start.
    should_start = (
        bool(payload.start)
        if payload.start is not None
        else (payload.name is None)
    )
    if payload.start is True:
        from app.entry.service import _welcome, venture_scope

        with venture_scope(ven.id, actor_id=owner_id, workspace_id=workspace.id):
            _welcome(ven.id, workspace.id)

    try:
        from app.credits.accounts import ensure_account

        ensure_account(
            ven.id,
            workspace_id=ven.workspace_id,
            owner_person_id=workspace.owner_id,
        )
    except Exception:
        pass
    try:
        from app.analytics.events import emit

        emit(
            "venture.created",
            person_id=workspace.owner_id,
            workspace_id=ven.workspace_id,
            venture_id=ven.id,
        )
    except Exception:
        pass
    try:
        from app.idea.service import open_session

        if should_start or not is_local():
            open_session(
                venture=ven,
                case="explore" if not idea else "know",
                actor_subject=workspace.owner_id,
            )
    except Exception:
        pass

    # Back-compat flat fields for A1–A7
    result = {
        "id": ven.id,
        "workspace_id": ven.workspace_id,
        "name": ven.name,
        "stage": ven.stage,
        "raw_idea": ven.raw_idea,
        "venture": {
            "id": ven.id,
            "name": ven.name,
            "stage": ven.stage,
            "raw_idea": ven.raw_idea,
            "created_at": ven.created_at.isoformat() + ("" if ven.created_at.tzinfo else "Z"),
        },
        "onboarding": None,
    }
    sample_view(result, sample)
    sample_view(result["venture"], sample)

    if should_start:
        from app.onboarding.spawn import start_onboarding

        run = await start_onboarding(
            ven.id,
            mode=payload.mode or "guided",
            research_founder=bool(payload.research_founder),
            workspace_id=workspace.id,
            entry_mode="idea",
        )
        result["onboarding"] = {
            "run_id": run.id,
            "status": run.status,
            "phase": run.phase,
            "phases_done": run.phases_done,
            "phases_total": run.phases_total,
        }
        # Move stage forward
        with factory() as session:
            row = session.get(Venture, ven.id)
            if row and row.stage == VentureStage.DRAFT.value:
                try:
                    row.stage = VentureStage.CLASSIFYING.value
                except AttributeError:
                    row.stage = "classifying"
                row.updated_at = utcnow()
                session.commit()
                result["stage"] = row.stage
                result["venture"]["stage"] = row.stage

    return result


@router.patch("/ventures/{venture_id}")
def rename_venture(
    venture_id: str,
    body: RenameVentureBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Persist a founder-chosen product name. Reflected on every surface that reads venture.name."""
    try:
        assert_venture_access(venture_id, x_subject)
    except HTTPException:
        raise
    if looks_like_secret(body.name):
        raise HTTPException(422, "Remove the key or password and try again in Manage Ventures. Save credentials in Settings > Keys and connections.")
    if not 2 <= len(body.name.strip()) <= VENTURE_NAME_MAX_CHARS:
        raise HTTPException(422, f"Enter a name with 2 to {VENTURE_NAME_MAX_CHARS} characters in Manage Ventures, then choose Save name.")
    name = derive_name(body.name, "")
    if name.lower() in {"untitled venture", "untitled"} or looks_like_id(name):
        raise HTTPException(422, "Enter a different name in Manage Ventures, then choose Save name.")
    factory = db()
    with factory() as session:
        row = session.get(Venture, venture_id)
        if not row or row.archived_at is not None or row.stage == "purged":
            raise HTTPException(404, "not_found")
        row.name = name
        row.updated_at = utcnow()
        workspace_id = row.workspace_id
        session.commit()
        session.refresh(row)
        result = {
            "id": row.id,
            "name": row.name,
            "workspace_id": workspace_id,
        }
    if workspace_id:
        try:
            workspace_service.rename_workspace(workspace_id, name)
        except Exception:
            pass
    return result


@router.patch("/ventures/{venture_id}/card")
def patch_venture_card(
    venture_id: str,
    body: VentureCardBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Remember which startup card and color this venture uses."""
    assert_venture_access(venture_id, x_subject)
    factory = db()
    with factory() as session:
        row = session.get(Venture, venture_id)
        if not row or row.archived_at is not None or row.stage == "purged":
            raise HTTPException(404, "not_found")
        workspace_id = row.workspace_id
    if not workspace_id:
        raise HTTPException(404, "not_found")
    updated = workspace_service.merge_settings(
        workspace_id,
        body.model_dump(exclude_none=True),
    )
    if updated is None:
        raise HTTPException(404, "not_found")
    return {"venture_id": venture_id, **body.model_dump(exclude_none=True)}


@router.patch("/ventures/{venture_id}/settings")
def patch_venture_settings(
    venture_id: str,
    body: ManagerNameBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Rename the manager persona (default Charlie). Empty resets the default."""
    assert_venture_access(venture_id, x_subject)
    from app.org.manager import set_manager_name

    try:
        return {"manager_name": set_manager_name(venture_id, body.manager_name)}
    except ValueError:
        raise HTTPException(422, "invalid_name") from None
    except KeyError:
        raise HTTPException(404, "not_found") from None


@router.post("/ventures/{venture_id}/autonomy")
def set_venture_autonomy(
    venture_id: str,
    body: AutonomyBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Founder-scoped autonomy level for this venture only (not the operator /gov global)."""
    assert_venture_access(venture_id, x_subject)
    from app.audit.writer import audit

    with db()() as session:
        row = session.get(Venture, venture_id)
        if not row or row.archived_at is not None or row.stage == "purged":
            raise HTTPException(404, "not_found")
        before = row.autonomy_level
        row.autonomy_level = body.level
        row.updated_at = utcnow()
        workspace_id = row.workspace_id
        session.commit()
    if workspace_id:
        workspace_service.set_autonomy(workspace_id, body.level)
    audit(
        "governance.autonomy.changed",
        workspace_id=workspace_id,
        venture_id=venture_id,
        surface="api",
        target_ref=venture_id,
        payload={"from": before, "to": body.level},
    )
    return {"venture_id": venture_id, "autonomy_level": body.level}


@router.get("/ventures/{venture_id}")
def get_venture(venture_id: str):
    from app.org.manager import manager_name
    from app.ventures.current_idea import idea_summary

    factory = db()
    with factory() as session:
        row = session.get(Venture, venture_id)
        if not row or row.archived_at is not None or row.stage == "purged":
            raise HTTPException(404, "not_found")
        if lifecycle._heal_names(session, [row]):
            session.commit()
            session.refresh(row)
        ser = lifecycle._ser_active(row)
        ser["current_idea"] = ser["idea_summary"] = idea_summary(row, session=session)
        workspace = workspace_service.get_workspace(row.workspace_id) if row.workspace_id else None
        intake_path = None
        saved_intake = session.get(VentureDocument, f"intake_decision:{row.id}")
        if saved_intake and saved_intake.venture_id == row.id:
            try:
                intake = json.loads(saved_intake.body_md or "{}")
                if isinstance(intake, dict) and isinstance(intake.get("path"), str):
                    intake_path = intake["path"]
            except (TypeError, ValueError):
                pass
        entry_goal = (
            "gtm"
            if workspace and workspace.settings.get("intake_venture_id") in {None, row.id} and (
                workspace.settings.get("entry_goal") == "gtm"
                or (isinstance(workspace.settings.get("answers"), dict)
                    and workspace.settings["answers"].get("want_first") == "gtm")
            )
            else None
        )
        return {
            "id": row.id,
            "name": ser["name"],
            **({"sample": ser["sample"]} if "sample" in ser else {}),
            "stage": row.stage,
            "status": ser.get("status"),
            "raw_idea": row.raw_idea,
            "created_at": ser["created_at"],
            "current_idea": ser["current_idea"],
            "idea_summary": ser["idea_summary"],
            "gtm_order": row.gtm_order,
            "workspace_id": row.workspace_id,
            "card_type": ser.get("card_type"),
            "card_color": ser.get("card_color"),
            "card_art": ser.get("card_art"),
            "entry_mode": workspace.entry_mode if workspace else None,
            "entry_goal": entry_goal,
            "intake_path": intake_path,
            "manager_name": manager_name(venture_id),
            "active_charter_id": row.active_charter_id,
            "autonomy_level": row.autonomy_level,
            "has_org": bool(MEM.get(f"org:{venture_id}")),
            "has_questions": bool(MEM.get(f"questions:{venture_id}")),
            "paused": bool(ser.get("paused")),
            "paused_reason": ser.get("paused_reason"),
            "paused_at": ser.get("paused_at"),
        }


@router.put("/ventures/{venture_id}/gtm-order")
def save_gtm_order(
    venture_id: str,
    body: GtmOrderBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Persist the founder's route choice without creating a campaign."""
    assert_venture_access(venture_id, x_subject)
    factory = db()
    with factory() as session:
        row = session.get(Venture, venture_id)
        if not row or row.archived_at is not None or row.stage == "purged":
            raise HTTPException(404, "not_found")
        row.gtm_order = body.gtm_order
        row.updated_at = utcnow()
        session.commit()
        return {"venture_id": row.id, "gtm_order": row.gtm_order}


@router.post("/ventures/{venture_id}/page-choice")
def save_page_choice(venture_id: str, body: PageChoiceBody,
    x_subject: str | None = Header(default=None, alias="X-Subject")):
    from sqlalchemy import select
    from app.core.jobs import ACTIVE_JOB_STATUSES
    from app.db.models import Job
    from app.db.session import serialize_writes
    from app.ventures import page_choice
    from app.authz.roles import role_allows
    from app.authz.workspace import subject_person_id
    from app.workspaces.service import role_of

    _, workspace_id = assert_venture_access(venture_id, x_subject)
    if not role_allows(role_of(workspace_id, subject_person_id(x_subject)), "manage_apps"):
        raise HTTPException(403, "forbidden")
    with db()() as session:
        serialize_writes(session, key="research-page-choice:" + venture_id)
        row = session.get(Venture, venture_id, with_for_update=True)
        if not row or row.workspace_id != workspace_id or row.archived_at is not None or row.stage in {"archived", "purged"}:
            raise HTTPException(404, "not_found")
        if session.scalar(select(Job.id).where(Job.workspace_id == row.workspace_id,
                Job.venture_id == row.id, Job.status.in_(ACTIVE_JOB_STATUSES)).limit(1)):
            raise HTTPException(409, "Wait for the current work to finish before choosing your next step.")
        page_choice.save(session, venture_id, body.choice)
        session.commit()
    return {"choice": body.choice}


class FounderStageBody(BaseModel):
    stage: Literal["exploring", "idea", "building", "page_live", "outreach", "traction"]
    confirmed: Literal[True] = True


@router.get("/ventures/{venture_id}/founder-stage")
def get_founder_stage(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.ventures.stage import get_stage
    assert_venture_access(venture_id, x_subject)
    return get_stage(venture_id)


@router.patch("/ventures/{venture_id}/founder-stage")
def update_founder_stage(venture_id: str, body: FounderStageBody,
    x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.authz.roles import role_allows
    from app.authz.workspace import subject_person_id
    from app.ventures.stage import confirm_stage
    from app.workspaces.service import role_of
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    if not role_allows(role_of(workspace_id, subject_person_id(x_subject)), "manage_apps"):
        raise HTTPException(403, "forbidden")
    return confirm_stage(venture_id, body.stage)


@router.post("/ventures/{venture_id}/delete")
def delete_venture(
    venture_id: str,
    body: DeleteVentureBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Soft-delete with typed confirmation. Recoverable for 7 days."""
    from app.authz.roles import role_allows
    from app.authz.workspace import subject_person_id

    _, workspace_id = assert_venture_access(venture_id, x_subject)
    if not role_allows(workspace_service.role_of(workspace_id, subject_person_id(x_subject)), "offboard"):
        raise HTTPException(403, "forbidden")
    try:
        return lifecycle.soft_delete(
            venture_id,
            confirmation=body.confirmation,
            actor_id=(x_subject or "founder"),
        )
    except lifecycle.VentureLifecycleError as exc:
        raise _lifecycle_http(exc) from exc


@router.post("/ventures/{venture_id}/pause")
def pause_venture(
    venture_id: str,
    body: PauseVentureBody | None = None,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Halt onboarding, agents, and tasks. Recoverable via resume."""
    try:
        assert_venture_access(venture_id, x_subject)
    except HTTPException:
        raise
    body = body or PauseVentureBody()
    try:
        return lifecycle.pause(
            venture_id,
            reason=body.reason,
            actor_id=(x_subject or "founder"),
        )
    except lifecycle.VentureLifecycleError as exc:
        raise _lifecycle_http(exc) from exc


@router.post("/ventures/{venture_id}/resume")
async def resume_venture(
    venture_id: str,
    body: ResumeVentureBody | None = None,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Clear pause and restart an onboarding run this pause halted."""
    try:
        assert_venture_access(venture_id, x_subject)
    except HTTPException:
        raise
    try:
        return await lifecycle.resume(
            venture_id,
            actor_id=(x_subject or "founder"),
        )
    except lifecycle.VentureLifecycleError as exc:
        raise _lifecycle_http(exc) from exc


@router.post("/ventures/{venture_id}/restore")
def restore_venture(
    venture_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Restore a soft-deleted venture within the 7-day window."""
    # Ownership: resolve workspace even while archived.
    factory = db()
    with factory() as session:
        row = session.get(Venture, venture_id)
        if not row or not row.workspace_id:
            raise HTTPException(404, detail={"error": {"code": "not_found"}})
        wid = row.workspace_id
    from app.authz.workspace import assert_workspace_access

    assert_workspace_access(wid, x_subject)
    try:
        return lifecycle.restore(venture_id, actor_id=(x_subject or "founder"))
    except lifecycle.VentureLifecycleError as exc:
        raise _lifecycle_http(exc) from exc


@router.get("/ventures/{venture_id}/slate")
def get_slate(venture_id: str):
    """Return the current next-five. Mint only the first queue, never a silent refill."""
    factory = db()
    with factory() as session:
        row = session.get(Venture, venture_id)
        if not row or row.archived_at is not None:
            raise HTTPException(404, "not_found")
        idea = row.raw_idea
    from app.planning.store import has_any_tasks, list_open_tasks, save_tasks
    from app.will.slate import generate_slate, latest_slate

    tasks = list_open_tasks(venture_id)
    slate = latest_slate(venture_id)
    if not tasks and not has_any_tasks(venture_id):
        slate = slate or generate_slate(
            venture_id,
            because="slate requested",
            idea=idea,
        )
        planned = [
            {
                "title": item.title,
                "detail": item.expected_outcome,
                "why_now": item.why_now,
                "tags": [item.origin, item.capability_id],
                "est_minutes": {"minutes": 20, "hours": 60, "days": 240}.get(item.effort, 60),
                "runnable": not item.blocked_by,
                "rank": item.rank,
            }
            for item in slate.items
        ]
        tasks = save_tasks(venture_id, planned, source="plan")
    if slate is None:
        slate = generate_slate(venture_id, because="slate requested", idea=idea)
    dump = slate.model_dump(mode="json")
    by_title = {
        str(item.get("title") or ""): item
        for item in (dump.get("items") or [])
        if isinstance(item, dict)
    }
    overlaid: list[dict] = []
    for index, task in enumerate(tasks[:5]):
        base = dict(by_title.get(str(task.get("title") or "")) or {})
        runnable = bool(task.get("runnable", True))
        base.update(
            {
                "id": task["id"],
                "rank": index + 1,
                "title": task["title"],
                "why_now": task.get("why_now") or base.get("why_now"),
                "runnable": runnable,
                "blocked_by": [] if runnable else ["needs_founder"],
            }
        )
        if task.get("owner_name"):
            base["owner_name"] = task["owner_name"]
            base["assignee_agent_id"] = task.get("owner_id")
        overlaid.append(base)
    dump["items"] = overlaid
    return dump


@router.get("/ventures/{venture_id}/money")
def get_money(venture_id: str):
    factory = db()
    with factory() as session:
        row = session.get(Venture, venture_id)
        if not row or row.archived_at is not None:
            raise HTTPException(404, "not_found")
    from app.money.summary import money_summary

    return money_summary(venture_id)


@router.get("/ventures/{venture_id}/agents/live")
def get_live_agents(venture_id: str):
    factory = db()
    with factory() as session:
        row = session.get(Venture, venture_id)
        if not row or row.archived_at is not None:
            raise HTTPException(404, "not_found")
    from app.org.heartbeat import list_live_status

    return {"items": list_live_status(venture_id)}


@router.post("/ventures/{venture_id}/stage")
def set_stage(venture_id: str, payload: StageBody):
    factory = db()
    with factory() as session:
        row = session.get(Venture, venture_id)
        if not row or row.archived_at is not None:
            raise HTTPException(404, "not_found")
        try:
            nxt = transition(row.stage, payload.stage)
        except IllegalTransition as e:
            raise HTTPException(409, e.to_dict()) from e
        row.stage = nxt.value
        row.updated_at = utcnow()
        session.commit()
        return {"id": row.id, "stage": row.stage}


@router.get("/ventures/{venture_id}/memories")
def list_venture_memories(
    venture_id: str,
    kinds: str | None = None,
    limit: int = 50,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """List founder preferences / decisions for this venture (boundary-scoped)."""
    assert_venture_access(venture_id, x_subject)
    kind_list = [k.strip() for k in (kinds or "").split(",") if k.strip()] or None
    from app.ventures.founder_memory import list_founder_memories

    status = {}
    items = list_founder_memories(venture_id, kinds=kind_list, limit=min(max(limit, 1), 100), status=status)
    return {"items": items, **status}


@router.delete("/ventures/{venture_id}/memories/{mem_id}")
def forget_venture_memory(
    venture_id: str,
    mem_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Tombstone a memory for this venture. Cannot touch another venture's rows."""
    assert_venture_access(venture_id, x_subject)
    from app.ventures.founder_memory import forget_founder_memory

    try:
        forget_founder_memory(venture_id, mem_id, reason="founder settings forget")
    except KeyError as exc:
        raise HTTPException(404, "not_found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "id": mem_id}
