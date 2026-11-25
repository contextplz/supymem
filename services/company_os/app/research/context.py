"""Capture accepted owner context once, when a new research dossier is claimed."""
from __future__ import annotations


def _founder_override(session, venture_id, workspace_id, idea, revision):
    from sqlalchemy import select
    from app.db.supymem_models import IdeaSession
    from app.ventures.journey_overrides import read

    allowed = bool(read(venture_id, session=session)["skipped"]) and (
        idea is None or (idea.state == "open" and revision is None) or (
            idea.state in {"open", "reviewing"} and revision is not None and revision.source == "founder_override"))
    return allowed and not session.scalar(select(IdeaSession.id).where(
        IdeaSession.workspace_id == workspace_id, IdeaSession.venture_id == venture_id,
        IdeaSession.confirmed_revision_id.is_not(None)).limit(1))


def pending_steps(venture_id):
    from sqlalchemy import select
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.state import db
    from app.ventures.startup_review import require_complete

    steps = []
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:  # messaging helper: no venture row means nothing to list, never an error
            return steps
        try:
            require_complete(session, venture)
        except ValueError:
            steps.append(("startup.review", "Review your saved answers and priorities"))
        idea = session.scalar(select(IdeaSession).where(
            IdeaSession.workspace_id == venture.workspace_id, IdeaSession.venture_id == venture_id,
            IdeaSession.state != "abandoned").order_by(
                IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()).limit(1))
        revision = session.get(IdeaRevision, idea.current_revision_id) if idea and idea.current_revision_id else None
        if revision and (revision.workspace_id, revision.venture_id, revision.session_id) != (
                venture.workspace_id, venture_id, idea.id):
            raise PermissionError("research_scope_mismatch")
        if not _founder_override(session, venture_id, venture.workspace_id, idea, revision) and not (
                idea and idea.state == "confirmed" and idea.confirmed_revision_id
                and idea.confirmed_revision_id == idea.current_revision_id):
            latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
                .order_by(ClarifierTurn.turn.desc()).limit(1)) if idea else None
            if latest and latest.role == "agent" and latest.kind == "question" and idea.state in {"clarifying", "reviewing"}:
                steps.append(("idea.review", "Answer your open question"))
            from app.db.supymem_models import ValidationRun
            from app.idea.validation import METHODS
            runs = {run.method: run for run in session.scalars(select(ValidationRun).where(
                ValidationRun.session_id == idea.id, ValidationRun.revision_id == idea.current_revision_id))} if idea else {}
            # A finished validation is accepted by starting research, so it is not a separate step.
            if not all(method in runs and runs[method].state == "complete" for method in METHODS):
                steps.append(("idea.review", "Review and confirm your idea summary"))
    return steps


def current_input(session, venture_id: str):
    """Recheck the latest idea after locking: a concurrent correction may have forked it."""
    from sqlalchemy import select
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.idea.service import append_revision
    from app.research.errors import ResearchPreflightBlocked
    from app.ventures.current_idea import current_idea
    from app.ventures.journey_overrides import NOTE
    from app.tenancy import current_or_none
    from core.clock import utcnow
    from core.ids import new_id

    venture = session.get(Venture, venture_id)
    if not venture:
        raise KeyError("venture_not_found")
    scope = current_or_none()
    if scope and (scope.workspace_id != venture.workspace_id or scope.venture_id not in {"", venture_id}):
        raise PermissionError("research_scope_mismatch")
    latest_idea = select(IdeaSession).where(
        IdeaSession.workspace_id == venture.workspace_id, IdeaSession.venture_id == venture_id,
        IdeaSession.state != "abandoned").order_by(
            IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()).limit(1).with_for_update()
    idea = session.scalar(latest_idea)
    if idea is not None:
        idea = session.scalar(latest_idea)
    revision = session.get(IdeaRevision, idea.current_revision_id) if idea and idea.current_revision_id else None
    if revision and (revision.workspace_id, revision.venture_id, revision.session_id) != (
            venture.workspace_id, venture_id, idea.id):
        raise PermissionError("research_scope_mismatch")
    founder_override = _founder_override(session, venture_id, venture.workspace_id, idea, revision)
    if founder_override and revision is None:
        summary = current_idea(venture, session=session)
        if idea is None:
            now = utcnow()
            idea = IdeaSession(id=new_id("ids"), workspace_id=venture.workspace_id, venture_id=venture_id,
                               case="know", state="open", created_at=now, updated_at=now)
            session.add(idea)
            session.flush()
        revision = append_revision(session, idea, {"summary": summary, "limitations": [NOTE],
            "_missing_idea_summary": not bool(summary.strip())}, source="founder_override", summary=summary)
    if not idea or (not founder_override and (
            idea.state != "confirmed" or not idea.confirmed_revision_id
            or idea.confirmed_revision_id != idea.current_revision_id)):
        raise ResearchPreflightBlocked("research_confirmation_required")
    return idea, idea.current_revision_id


def capture(session, venture_id: str, workspace_id: str, snapshot: dict) -> dict:
    from app.core.jobs import FatalError
    from app.db.models import Venture
    venture = session.get(Venture, venture_id)
    if not venture or venture.workspace_id != workspace_id:
        raise FatalError("research_scope_mismatch")
    result = {}
    from app.connections.context_docs import research_context
    result.update(research_context(session, venture_id, workspace_id))
    try:
        from app.intake.assets import get_site
    except ModuleNotFoundError as exc:
        if exc.name != "app.intake.assets":
            raise
    else:
        # S04 owns site/stage derivation. These reports are context, never proof
        # of demand or permission for an effect. Do not mutate the idea revision.
        site = get_site(venture_id, session=session)
        result.update(founder_stage=site.get("founder_stage"), existing_site=site.get("site"))
    from app.config import settings
    if not settings.company_os_memory_facts_enabled:
        return result
    try:
        from app.memory.context import context_for
    except ModuleNotFoundError as exc:
        if exc.name != "app.memory.context":
            raise
    else:
        # S05's quoted facts are context only; approvals/current state stay live.
        result["institutional_context"] = context_for(session, workspace_id=workspace_id,
            venture_id=venture_id, purpose="research.deep", query=str(snapshot.get("problem") or snapshot.get("offer") or "prior attempts"),
            budget_bytes=3000)
    return result
