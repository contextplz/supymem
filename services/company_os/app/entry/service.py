"""Commit an entry decision: create the workspace and start the right flow."""

from __future__ import annotations

from typing import Any
import logging
from contextlib import contextmanager

from sqlalchemy import select, update

from app.audit.writer import audit
from app.db.models import Venture
from app.db.supymem_models import EntryChatProposal
from app.entry.agent_packs import pack_for_stage
from app.entry.product_context import product_context
from app.entry.route import EntryDecision, infer_project_intent, route, wants_gtm_only
from app.intake.naming import derive_name, unique_venture_name
from app.state import db
from app.workspaces import resolve, service as workspace_service
from core.clock import utcnow
from core.enums import VentureStage
from core.ids import new_id


class EntryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _assert_entry_actor(actor_id: str) -> None:
    from fastapi import HTTPException
    from app.authz.workspace import subject_person_id
    from app.tenancy import current_or_none

    incoming = current_or_none()
    if (incoming and incoming.actor not in {"local", "anonymous", "system"}
            and subject_person_id(incoming.actor) != actor_id):
        raise HTTPException(403, detail={"code": "actor_mismatch"})


@contextmanager
def venture_scope(venture_id: str, *, actor_id: str, workspace_id: str):
    """Derive post-entry tenancy from an owned, persisted venture, then restore it."""
    from fastapi import HTTPException
    from app.authz.workspace import assert_venture_access
    from app.tenancy import bind

    _assert_entry_actor(actor_id)
    owned_id, owned_workspace = assert_venture_access(venture_id, actor_id)
    with db()() as session:
        row = session.get(Venture, owned_id)
        if (row is None or row.archived_at is not None
                or row.workspace_id != owned_workspace or owned_workspace != workspace_id):
            raise HTTPException(404, detail={"code": "not_found"})
    with bind(workspace_id=owned_workspace, venture_id=owned_id, actor=actor_id) as scope:
        yield scope


def _link_chat_proposal(session, options: dict[str, Any], venture_id: str, actor_id: str) -> None:
    """Save the venture link in the same transaction as the venture itself."""
    proposal_id = options.get("entry_chat_proposal_id")
    if not proposal_id:
        return
    linked = session.execute(update(EntryChatProposal).where(
        EntryChatProposal.id == proposal_id,
        EntryChatProposal.actor_id == actor_id,
        EntryChatProposal.state == "committing",
    ).values(target_venture_id=venture_id, updated_at=utcnow()))
    if linked.rowcount != 1:
        raise EntryError("entry_proposal_conflict", "This chat proposal is no longer active.")


def first_run_reply(venture_id: str, *, personal: bool = False) -> dict[str, Any]:
    return {
        "kind": "workspace_welcome",
        "body_md": "Supymem helps you turn your idea into a product, find potential customers, and review outreach before anything is sent.",
        "actions": [
            ({"label": "Clarify my idea", "action": "idea.review", "style": "primary"} if personal else
             {"label": "Find my first customers", "action": "chat.reply", "payload": {"text": "Find my first customers"}, "style": "primary"}),
            {"label": "How to use Supymem", "href": f"/v/{venture_id}/guide", "style": "secondary"},
        ],
    }


def _welcome(venture_id: str, workspace_id: str, *, personal: bool = False) -> None:
    from app.chat.conversations import list_conversations
    from app.chat.store import append_message
    from app.db.models import ChatMessage

    with db()() as session:
        if session.scalar(select(ChatMessage.id).join(Venture, Venture.id == ChatMessage.venture_id).where(
            Venture.workspace_id == workspace_id, ChatMessage.kind == "workspace_welcome")):
            return
    reply = first_run_reply(venture_id, personal=personal)
    append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                   actions=reply["actions"], idempotency_key=f"welcome:{workspace_id}",
                   conversation_id=list_conversations(venture_id)["legacy_conversation_id"])


def _stage_from_options(options: dict[str, Any], mode: str) -> str:
    stage = str(options.get("stage") or "").strip().lower()
    if stage:
        return stage
    if mode == "idea":
        return "scratch"
    if mode == "existing_company":
        return "company"
    if mode == "product":
        return "product"
    return mode


async def commit(
    decision: EntryDecision,
    *,
    text: str = "",
    mode_options: dict[str, Any] | None = None,
    actor_id: str | None = None,
    surface: str = "api",
) -> dict[str, Any]:
    """Create/return the workspace and kick the mode-specific flow."""
    actor = actor_id or resolve.local_person().id
    _assert_entry_actor(actor)
    options = mode_options or {}
    body = (text or "").strip()
    from app.ventures.quota import VentureLimitReached, assert_can_create_venture

    try:
        assert_can_create_venture(actor)
    except VentureLimitReached as exc:
        raise EntryError(exc.code, exc.message) from exc

    if decision.mode == "idea":
        return await _commit_idea(body, options=options, actor_id=actor, surface=surface)
    if decision.mode == "surprise":
        return await _commit_surprise(
            body, options=options, actor_id=actor, surface=surface
        )
    if decision.mode == "engagement":
        return await _commit_engagement(
            body, options=options, actor_id=actor, surface=surface
        )
    if decision.mode in {"existing_company", "company"}:
        return await _commit_company(body, options=options, actor_id=actor, surface=surface)
    if decision.mode == "product":
        return await _commit_product(body, options=options, actor_id=actor, surface=surface)
    raise EntryError("unknown_mode", f"Unknown entry mode: {decision.mode}")


async def _commit_idea(
    text: str,
    *,
    options: dict[str, Any],
    actor_id: str,
    surface: str,
) -> dict[str, Any]:
    idea = text.strip()
    if idea and len(idea) < 12:
        raise EntryError(
            "idea_too_short", "Tell me a bit more, at least a short sentence."
        )
    # The founder writes an idea; there is no mandatory startup/hobby selector.
    # Keep explicit API values for existing clients and older personal ventures.
    project_intent = options.get("project_intent") or infer_project_intent(idea)
    if project_intent not in {"startup", "personal"}:
        raise EntryError("invalid_project_intent", "Unsupported project intent.")
    base_name = options.get("founder_chosen_name") or derive_name(options.get("name"), idea)
    name = unique_venture_name(actor_id, base_name)
    workspace = workspace_service.create_workspace(
        kind="venture", name=name, entry_mode="idea", owner_id=actor_id
    )
    stage = _stage_from_options(options, "idea")
    answers = options.get("answers") if isinstance(options.get("answers"), dict) else {}
    agent_pack = pack_for_stage(stage, answers)
    # Save the founder's intent before the initial idea job can be queued.
    workspace_service.merge_settings(workspace.id, {
        "agent_pack": agent_pack, "stage": stage, "answers": answers,
        "project_intent": project_intent,
        "venture_name_origin": {"name": name, "base": base_name},
        **({"chat_entry_defer_clarification": True} if options.get("defer_initial_clarification") else {}),
    })
    now = utcnow()
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
    factory = db()
    with factory() as session:
        session.add(ven)
        from app.ventures.startup_review import pin_at_creation
        pin_at_creation(session, ven)
        _link_chat_proposal(session, options, ven.id, actor_id)
        session.commit()
        session.refresh(ven)
        venture_id = ven.id

    with venture_scope(venture_id, actor_id=actor_id, workspace_id=workspace.id):
        # The front-door /entry route is the production path used by the Mac app.
        # Provision credits before clarification is queued, just as the legacy
        # POST /ventures route does. The worker also self-heals a missing account,
        # but doing this here keeps the normal journey atomic from the founder's
        # point of view.
        try:
            from app.credits.accounts import ensure_account

            ensure_account(
                venture_id,
                workspace_id=workspace.id,
                owner_person_id=actor_id,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "entry_credit_account_failed", extra={"venture_id": venture_id}
            )

        # Brandable name from the idea (flash tier, credit-metered). The founder can
        # rename in Settings; a founder-supplied name is never overridden.
        suggested_name = None
        if not options.get("name") and idea:
            from app.intake.naming import suggest_name

            suggested_name = await suggest_name(idea, workspace.id)
            if suggested_name:
                base_name = suggested_name
                suggested_name = unique_venture_name(actor_id, suggested_name)
                with factory() as session:
                    row = session.get(Venture, venture_id)
                    row.name = suggested_name
                    session.commit()
                workspace_service.rename_workspace(workspace.id, suggested_name)
                workspace_service.merge_settings(workspace.id, {"venture_name_origin": {"name": suggested_name, "base": base_name}})
                ven.name = suggested_name

        if not options.get("defer_initial_clarification"):
            try:
                from app.idea.service import open_session

                open_session(
                    venture=ven,
                    case="explore" if not idea else "know",
                    actor_subject=actor_id,
                )
            except Exception as exc:
                logging.getLogger(__name__).error("entry_idea_session_failed", extra={
                    "venture_id": venture_id, "error_kind": type(exc).__name__})

        audit(
            "entry.idea",
            workspace_id=workspace.id,
            actor_id=actor_id,
            actor_kind="person",
            surface=surface,
            target_ref=venture_id,
            payload={"mode": "idea", "project_intent": project_intent},
        )
        from app.org.summary import persist_pack_org

        persist_pack_org(venture_id, agent_pack)

        onboarding = None
        start = options.get("start", True)
        from app.worker.handlers import hosted_mode
        if start and not hosted_mode():
            from app.onboarding.spawn import start_onboarding

            # Product UX: return venture_id immediately; onboarding continues in background
            # when ONBOARDING_SYNC!=1. Tests keep the sync await.
            run = await start_onboarding(
                venture_id,
                mode=str(options.get("mode") or "guided"),
                research_founder=bool(options.get("research_founder")),
                skip_research=bool(options.get("skip_research")),
                workspace_id=workspace.id,
                entry_mode="idea",
            )
            onboarding = {
                "run_id": run.id,
                "status": run.status,
                "phase": run.phase,
                "phases_done": run.phases_done,
                "phases_total": run.phases_total,
            }

        return {
            "mode": "idea",
            "stage": stage,
            "workspace": workspace_service.get_workspace(workspace.id).to_dict()
            if workspace_service.get_workspace(workspace.id)
            else workspace.to_dict(),
            "venture_id": venture_id,
            "next_step": "idea",
            "onboarding": onboarding,
            "agent_pack": agent_pack,
            "suggested_name": suggested_name,
        }


async def _commit_surprise(
    text: str,
    *,
    options: dict[str, Any],
    actor_id: str,
    surface: str,
) -> dict[str, Any]:
    from app.idealab import slate as slate_mod

    desk = resolve.ensure_desk(actor_id)
    seed = options.get("seed") if isinstance(options.get("seed"), dict) else {}
    if text and "interests" not in seed:
        seed = {**seed, "interests": [text[:80]]} if len(text) >= 3 else seed

    view = await slate_mod.create_slate(
        workspace_id=desk.id,
        seed_input=seed,
        actor_id=actor_id,
        surface=surface,
    )
    audit(
        "entry.surprise",
        workspace_id=desk.id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=view.id,
        payload={"mode": "surprise", "size": len(view.opportunities)},
    )
    ws = workspace_service.get_workspace(desk.id)
    return {
        "mode": "surprise",
        "workspace": ws.to_dict() if ws else {"id": desk.id, "kind": "desk"},
        "slate": view.to_dict(),
    }


async def _commit_engagement(
    text: str,
    *,
    options: dict[str, Any],
    actor_id: str,
    surface: str,
) -> dict[str, Any]:
    from app.engagements import service as eng

    desk = resolve.ensure_desk(actor_id)
    workspace_id = str(options.get("workspace_id") or desk.id)
    if workspace_service.get_workspace(workspace_id) is None:
        raise EntryError("workspace_not_found", "That workspace doesn't exist.")

    body = text.strip()
    if len(body) < 3:
        raise EntryError(
            "request_too_short", "Tell me a bit more about what you need."
        )

    view = await eng.create_engagement(
        workspace_id=workspace_id,
        text=body,
        origin=str(options.get("origin") or "composer"),
        requested_by=actor_id,
        surface=surface,
    )
    if view.status != "awaiting_goahead":
        view = await eng.advance(view.id)

    audit(
        "entry.engagement",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=view.id,
        payload={"mode": "engagement", "size": view.size, "status": view.status},
    )
    ws = workspace_service.get_workspace(workspace_id)
    return {
        "mode": "engagement",
        "workspace": ws.to_dict() if ws else {"id": workspace_id},
        "engagement": view.to_dict(),
    }


async def _commit_company(
    text: str, *, options: dict[str, Any], actor_id: str, surface: str
) -> dict[str, Any]:
    name = "Company"
    if text.strip():
        snippet = text.strip().split()[0]
        name = derive_name(
            None, snippet.replace("https://", "").replace("http://", "")
        )
    answers = options.get("answers") if isinstance(options.get("answers"), dict) else {}
    if answers.get("company_name"):
        name = str(answers["company_name"])[:120]
    agent_pack = pack_for_stage("company", answers)
    workspace = workspace_service.create_workspace(
        kind="company",
        name=name[:120],
        entry_mode="existing_company",
        owner_id=actor_id,
        autonomy_level=str(agent_pack.get("autonomy_level") or "L1"),
    )
    assert workspace.observe_level == "O0"
    workspace_service.merge_settings(
        workspace.id, {"agent_pack": agent_pack, "stage": "company", "answers": answers}
    )
    workspace = workspace_service.get_workspace(workspace.id) or workspace
    now = utcnow()
    ven = Venture(
        id=new_id("ven"),
        workspace_id=workspace.id,
        name=name[:120],
        raw_idea=(text.strip() or "Existing company workspace")[:2000],
        stage=VentureStage.DRAFT.value,
        autonomy_level=str(agent_pack.get("autonomy_level") or "L1"),
        created_at=now,
        updated_at=now,
    )
    factory = db()
    with factory() as session:
        session.add(ven)
        from app.ventures.startup_review import pin_at_creation
        pin_at_creation(session, ven)
        _link_chat_proposal(session, options, ven.id, actor_id)
        session.commit()
        session.refresh(ven)
        venture_id = ven.id
    with venture_scope(venture_id, actor_id=actor_id, workspace_id=workspace.id):
        audit(
            "entry.existing_company",
            workspace_id=workspace.id,
            actor_id=actor_id,
            actor_kind="person",
            surface=surface,
            target_ref=venture_id,
            payload={
                "mode": "existing_company",
                "observe_level": "O0",
                "verbatim": text[:200],
                "answers": answers,
                "agent_pack": agent_pack,
            },
        )
        from app.org.summary import persist_pack_org

        persist_pack_org(venture_id, agent_pack)

        onboarding = None
        if options.get("start", True):
            from app.onboarding.spawn import start_onboarding

            run = await start_onboarding(
                venture_id,
                mode=str(options.get("mode") or "guided"),
                research_founder=bool(options.get("research_founder")),
                workspace_id=workspace.id,
                entry_mode="existing_company",
            )
            onboarding = {
                "run_id": run.id,
                "status": run.status,
                "phase": run.phase,
                "phases_done": run.phases_done,
                "phases_total": run.phases_total,
            }

        return {
            "mode": "existing_company",
            "stage": "company",
            "workspace": workspace.to_dict(),
            "venture_id": venture_id,
            "observe_level": "O0",
            "agent_pack": agent_pack,
            "onboarding": onboarding,
        }


async def _commit_product(
    text: str,
    *,
    options: dict[str, Any],
    actor_id: str,
    surface: str,
) -> dict[str, Any]:
    """Path B — already have a product: venture workspace + agent pack, light onboarding."""
    answers = options.get("answers") if isinstance(options.get("answers"), dict) else {}
    idea, naming_source = product_context(text, answers)
    fallback_names = {"Existing product", "Existing app", "Existing website", "Existing app and website", "Existing internal tool"}
    base_name = options.get("founder_chosen_name") or derive_name(options.get("name") or (naming_source if naming_source in fallback_names else None), naming_source)
    name = unique_venture_name(actor_id, base_name)
    gtm_first = str(answers.get("want_first") or "") == "gtm" or wants_gtm_only(text)
    if gtm_first:
        answers = {**answers, "want_first": "gtm"}
    agent_pack = pack_for_stage("product", answers)
    workspace = workspace_service.create_workspace(
        kind="venture", name=name, entry_mode="product", owner_id=actor_id
    )
    workspace_service.merge_settings(workspace.id, {"venture_name_origin": {"name": name, "base": base_name}})
    now = utcnow()
    ven = Venture(
        id=new_id("ven"),
        workspace_id=workspace.id,
        name=name,
        raw_idea=idea[:2000],
        stage=VentureStage.DRAFT.value,
        autonomy_level=str(agent_pack.get("autonomy_level") or "L1"),
        created_at=now,
        updated_at=now,
    )
    factory = db()
    with factory() as session:
        session.add(ven)
        from app.ventures.startup_review import pin_at_creation
        pin_at_creation(session, ven)
        _link_chat_proposal(session, options, ven.id, actor_id)
        session.commit()
        session.refresh(ven)
        venture_id = ven.id

    with venture_scope(venture_id, actor_id=actor_id, workspace_id=workspace.id):
        # GTM-first uses the same venture credit account as an idea-first journey.
        # Without it, the first reviewed diagnosis cannot quote platform credits.
        credit_ready = True
        try:
            from app.credits.accounts import ensure_account

            ensure_account(venture_id, workspace_id=workspace.id, owner_person_id=actor_id)
        except Exception:
            credit_ready = False
            logging.getLogger(__name__).exception(
                "entry_product_credit_account_failed", extra={"venture_id": venture_id}
            )

        audit(
            "entry.product",
            workspace_id=workspace.id,
            actor_id=actor_id,
            actor_kind="person",
            surface=surface,
            target_ref=venture_id,
            payload={"mode": "product", "answers": answers, "agent_pack": agent_pack},
        )
        workspace_service.merge_settings(
            workspace.id, {"agent_pack": agent_pack, "stage": "product", "answers": answers}
        )
        from app.org.summary import persist_pack_org

        persist_pack_org(venture_id, agent_pack)

        onboarding = None
        if options.get("start", True) and not gtm_first and credit_ready:
            from app.onboarding.spawn import start_onboarding

            run = await start_onboarding(
                venture_id,
                mode=str(options.get("mode") or "guided"),
                research_founder=bool(options.get("research_founder")),
                workspace_id=workspace.id,
                entry_mode="product",
            )
            onboarding = {
                "run_id": run.id,
                "status": run.status,
                "phase": run.phase,
                "phases_done": run.phases_done,
                "phases_total": run.phases_total,
            }

        ws_view = workspace_service.get_workspace(workspace.id) or workspace
        return {
            "mode": "product",
            "stage": "product",
            "workspace": ws_view.to_dict(),
            "venture_id": venture_id,
            # The workspace is already saved. Expose an actionable setup state
            # instead of starting a second venture or promising a GTM quote that
            # cannot be metered. Settings retries the idempotent account creation.
            "next_step": "credits" if not credit_ready else "gtm" if gtm_first else "cockpit",
            "setup_issue": "credit_account_unavailable" if not credit_ready else None,
            "onboarding": onboarding,
            "agent_pack": agent_pack,
        }


async def enter(
    *,
    text: str | None = None,
    explicit_mode: str | None = None,
    confirm: bool = False,
    active_workspace_id: str | None = None,
    options: dict[str, Any] | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Route, and only create when confidence is high or the founder confirmed."""
    active_kind = None
    if active_workspace_id:
        ws = workspace_service.get_workspace(active_workspace_id)
        active_kind = ws.kind if ws else None
        opts = dict(options or {})
        opts.setdefault("workspace_id", active_workspace_id)
        options = opts

    # The scratch composer is a single free-text entrance. A founder's explicit
    # statement that a product already exists and only GTM is wanted overrides
    # that page's default "idea" mode; do not queue idea clarification/build.
    if explicit_mode in {"idea", "scratch"} and wants_gtm_only(text):
        explicit_mode = "product"

    decision = route(
        text,
        explicit_mode=explicit_mode,
        active_workspace_kind=active_kind,
    )
    result: dict[str, Any] = {"decision": decision.to_dict(), "committed": False}

    if decision.confirm_needed and not confirm and not explicit_mode:
        return result

    committed = await commit(
        decision,
        text=text or "",
        mode_options=options,
        actor_id=actor_id,
    )
    result["committed"] = True
    result.update(committed)
    return result


def modes_payload() -> dict[str, Any]:
    from app.entry.route import list_modes

    return {"modes": list_modes()}


__all__ = ["EntryError", "commit", "enter", "modes_payload", "route"]
