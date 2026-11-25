"""Durable, exact chat proposals; model output never grants execution authority.

Start with reviewed plan approval. Other actions must register a deterministic
preview, admission and reconciliation contract before using this service.
"""
from __future__ import annotations

import json
from datetime import timedelta, timezone

from sqlalchemy import and_, or_, select, update

from app.authz.workspace import assert_venture_access, subject_person_id
from app.db.supymem_models import ChatProposal, ExecutionPlan
from app.planning import compiler
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def _scope(venture_id, actor):
    _, wid = assert_venture_access(venture_id, actor)
    return wid, subject_person_id(actor)


def _where(venture_id, wid, actor, proposal_id):
    return (ChatProposal.id == proposal_id, ChatProposal.venture_id == venture_id,
            ChatProposal.workspace_id == wid, ChatProposal.actor_id == actor)


def _serialize(row):
    expiry = row.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return {"id": row.id, "venture_id": row.venture_id, "action": row.action,
            "state": row.state, "review": json.loads(row.review_json),
            "expires_at": expiry.astimezone(timezone.utc).isoformat(),
            "receipt": json.loads(row.receipt_json) if row.receipt_json else None}


def prepare_plan_approval(venture_id, actor, plan_id):
    wid, actor = _scope(venture_id, actor)
    # Do not rely on ambient ORM filtering for exact resource containment.
    with db()() as session:
        plan_row = session.scalar(select(ExecutionPlan).where(ExecutionPlan.id == plan_id,
            ExecutionPlan.venture_id == venture_id, ExecutionPlan.workspace_id == wid))
        if plan_row is None:
            raise KeyError("chat_plan_not_found")
    plan = compiler.get_plan(plan_id)
    if not plan["reviewable"] or plan.get("review_findings"):
        raise ValueError("chat_plan_not_reviewable")
    review = {"title": "Approve this execution plan", "plan_id": plan_id,
        "version": plan["version"], "edit_version": plan["edit_version"],
        "proposal": plan["proposal"], "sources": plan.get("sources", []),
        "precedents": plan.get("precedents"), "credit_ceiling_micro": 0,
        "effects": ["Record this exact reviewed plan as approved and add its next work items.",
                    "Approval does not start a build, publish a landing page, send messages or send messages."],
        "review_href": f"/v/{venture_id}/plan#plan-review"}
    now = utcnow()
    with db()() as session:
        row = ChatProposal(id=new_id("cpr"), workspace_id=wid, venture_id=venture_id,
            actor_id=actor, action="plan.approve", state="pending", created_at=now,
            expires_at=now + timedelta(minutes=15), updated_at=now,
            arguments_json=json.dumps({"plan_id": plan_id, "review_token": plan["review_token"]}),
            review_json=json.dumps(review))
        session.add(row)
        session.commit()
        return _serialize(row)


def list_recent(venture_id, actor, before=None):
    """Recover exact cards after reconnect without exposing another actor's review."""
    wid, actor = _scope(venture_id, actor)
    scope = (ChatProposal.venture_id == venture_id,
             ChatProposal.workspace_id == wid, ChatProposal.actor_id == actor)
    with db()() as session:
        query = select(ChatProposal).where(*scope)
        if before:
            cursor = session.scalar(select(ChatProposal).where(*scope, ChatProposal.id == before))
            if cursor is None:
                raise KeyError("chat_proposal_not_found")
            query = query.where(or_(ChatProposal.created_at < cursor.created_at,
                and_(ChatProposal.created_at == cursor.created_at, ChatProposal.id < cursor.id)))
        rows = list(session.scalars(query.order_by(
            ChatProposal.created_at.desc(), ChatProposal.id.desc()).limit(51)))
        return {"items": [_serialize(row) for row in rows[:50]],
                "next_cursor": rows[49].id if len(rows) > 50 else None}


def get(venture_id, actor, proposal_id):
    wid, actor = _scope(venture_id, actor)
    with db()() as session:
        row = session.scalar(select(ChatProposal).where(*_where(venture_id, wid, actor, proposal_id)))
        if row is None:
            raise KeyError("chat_proposal_not_found")
        return _serialize(row)


def cancel(venture_id, actor, proposal_id):
    wid, actor = _scope(venture_id, actor)
    with db()() as session:
        session.execute(update(ChatProposal).where(*_where(venture_id, wid, actor, proposal_id),
            ChatProposal.state == "pending").values(state="cancelled", updated_at=utcnow()))
        session.commit()
    return get(venture_id, actor, proposal_id)


def confirm(venture_id, actor, proposal_id):
    wid, actor = _scope(venture_id, actor)
    now = utcnow()
    with db()() as session:
        row = session.scalar(select(ChatProposal).where(*_where(venture_id, wid, actor, proposal_id)))
        if row is None:
            raise KeyError("chat_proposal_not_found")
        if row.state == "succeeded":
            return _serialize(row)
        # Atomic claim prevents parallel confirmation dispatch. An interrupted
        # claim remains executing; never automatically replay unknown outcomes.
        claimed = session.execute(update(ChatProposal).where(*_where(venture_id, wid, actor, proposal_id),
            ChatProposal.state == "pending", ChatProposal.expires_at > now)
            .values(state="executing", updated_at=now).execution_options(synchronize_session=False)).rowcount
        if not claimed:
            raise ValueError("chat_proposal_not_confirmable")
        action, args = row.action, json.loads(row.arguments_json)
        session.commit()
    try:
        if action == "research.update_and_rerun":
            from app.chat.research_revision import apply
            receipt = apply(venture_id, actor, args, proposal_id)
        elif action == "plan.approve":
            result = compiler.approve_plan(args["plan_id"], person_id=actor, review_token=args["review_token"])
            receipt = {"plan_id": args["plan_id"], "plan_state": result["plan"]["state"],
                       "execution_started": result["execution_started"]}
        else:
            raise ValueError("chat_action_not_supported")
    except (ValueError, KeyError, PermissionError) as exc:
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "proposal.execute")
        # Even a post-commit projection error can leave a durable approval. Keep
        # an uncertain outcome frozen for reconciliation; never blindly retry.
        _finish(proposal_id, "needs_reconciliation", {"code": "chat_action_not_completed"})
        raise
    except Exception as exc:
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "proposal.execute")
        _finish(proposal_id, "needs_reconciliation", {"code": "chat_action_outcome_unknown"})
        raise
    _finish(proposal_id, "succeeded", receipt)
    return get(venture_id, actor, proposal_id)


def _finish(proposal_id, state, receipt):
    with db()() as session:
        session.execute(update(ChatProposal).where(ChatProposal.id == proposal_id,
            ChatProposal.state == "executing").values(state=state, receipt_json=json.dumps(receipt), updated_at=utcnow()))
        session.commit()


def reconcile(venture_id, actor, proposal_id):
    """Observe durable plan approval only. Never dispatch or reset an action."""
    wid, actor = _scope(venture_id, actor)
    with db()() as session:
        row = session.scalar(select(ChatProposal).where(*_where(venture_id, wid, actor, proposal_id)))
        if row is None:
            raise KeyError("chat_proposal_not_found")
        if row.state not in {"executing", "needs_reconciliation"} or row.action != "plan.approve":
            return _serialize(row)
        args, review = json.loads(row.arguments_json), json.loads(row.review_json)
        plan = session.scalar(select(ExecutionPlan).where(ExecutionPlan.id == args["plan_id"],
            ExecutionPlan.venture_id == venture_id, ExecutionPlan.workspace_id == wid))
        doc = json.loads(plan.structured) if plan else {}
        if (plan and plan.approved_at and plan.approved_by == actor
                and doc.get("edit_version") == review["edit_version"]
                and doc.get("proposal") == review["proposal"]):
            receipt = {"plan_id": plan.id, "plan_state": plan.state, "execution_started": False,
                       "reconciled": True, "approved_at": plan.approved_at.isoformat()}
            session.execute(update(ChatProposal).where(*_where(venture_id, wid, actor, proposal_id),
                ChatProposal.state.in_(["executing", "needs_reconciliation"]))
                .values(state="succeeded", receipt_json=json.dumps(receipt), updated_at=utcnow()))
            session.commit()
    return get(venture_id, actor, proposal_id)
