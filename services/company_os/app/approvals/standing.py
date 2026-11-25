"""Founder-private standing grants, using the existing approval queue and audit log."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from app.approvals.service import DEFAULT_WINDOW_HOURS, STANDING_DECISION_KIND, STANDING_KIND
from app.db.models import Approval, ChatConversation, ChatMessage, EventLog, Venture
from app.db.session import serialize_writes
from app.credits.policy import get_policy
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id


def enabled() -> bool:
    from app.core.local_mode import is_local
    from app.core.local_capabilities import enabled as capability_enabled
    if is_local():
        return capability_enabled("routines")
    try:
        flags = get_policy("feature_flags")
    except Exception:  # noqa: BLE001 - an unreadable flag means off
        return False
    return isinstance(flags, dict) and flags.get("approvals_policy_v1") is True


@dataclass(frozen=True)
class StandingAction:
    action_type: str
    scope: str
    credit_cost_micro: int | None = None
    task_allowance_micro: int | None = None
    first_send: bool = True
    sequence_approved: bool = False
    sent_today: int | None = None
    daily_cap: int | None = None

    def eligible(self) -> bool:
        if self.action_type not in {"documents.read", "browse.public", "outreach.followup",
                                    "openworker_search", "research.start", "outreach.draft"}:
            return False
        if not isinstance(self.scope, str) or not self.scope.strip() or len(self.scope) > 4096:
            return False
        if (type(self.credit_cost_micro) is not int or type(self.task_allowance_micro) is not int
                or not 0 <= self.credit_cost_micro <= self.task_allowance_micro):
            return False
        if self.action_type == "outreach.followup":
            return (self.first_send is False and self.sequence_approved is True
                    and type(self.sent_today) is int and type(self.daily_cap) is int
                    and 0 <= self.sent_today < self.daily_cap)
        return True


def card_action(action: str, state: dict) -> StandingAction:
    scope = hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()
    return StandingAction(action, scope, state.get("ceiling_micro"), state.get("task_allowance_micro"),
        state.get("first_send", True), state.get("sequence_approved", False),
        state.get("sent_today"), state.get("daily_cap"))


def _identity(session, workspace_id, venture_id, founder_id, *, require_approval=True):
    from app.approvals.service import ApprovalError, _can_approve
    from app.authz.workspace import LOCAL_PERSON_ID, subject_person_id
    from app.tenancy import current_or_none

    founder = subject_person_id(founder_id)
    if founder in {LOCAL_PERSON_ID, "anonymous", "system"}:
        raise ApprovalError("cannot_approve", "You need a verified account to save a standing approval.")
    tenant = current_or_none()
    if tenant and (tenant.workspace_id != workspace_id or subject_person_id(tenant.actor) != founder
                   or tenant.venture_id and tenant.venture_id != venture_id):
        raise ApprovalError("cannot_approve", "You cannot change another person's approvals.")
    venture = session.get(Venture, venture_id)
    if not venture or venture.workspace_id != workspace_id or require_approval and venture.archived_at is not None:
        raise ApprovalError("not_found", "That venture is not available.")
    if require_approval and not _can_approve(session, workspace_id, founder):
        raise ApprovalError("cannot_approve", "You cannot approve work in this workspace.")
    return founder


def _key(founder, venture, action):
    return "standing:" + hashlib.sha256(json.dumps(
        [founder, venture, action.action_type, action.scope], separators=(",", ":")
    ).encode()).hexdigest()


def _audit(session, topic, row, founder, venture_id, **payload):
    automatic = topic == "approval.auto_approved"
    actor = "standing_policy" if automatic else founder
    session.add(EventLog(id=new_id("evt"), topic=topic, workspace_id=row.workspace_id,
        venture_id=venture_id, actor=actor, actor_id=actor, actor_kind="system" if automatic else "person",
        on_behalf_of=founder, surface="api", target_ref=row.subject_ref,
        outcome="ok", approval_id=row.id, payload_json=json.dumps(payload), ts=utcnow()))


def _grant(session, workspace_id, venture_id, founder, action, label):
    from app.approvals.service import ApprovalError

    if not enabled() or not action.eligible():
        raise ApprovalError("always_ask", "You need to approve this action each time.")
    key = _key(founder, venture_id, action)
    serialize_writes(session, key=key)
    row = session.scalar(select(Approval).where(Approval.workspace_id == workspace_id,
        Approval.subject_ref == key, Approval.kind == STANDING_KIND, Approval.status == "standing").with_for_update())
    moment = utcnow()
    if row is None:
        row = Approval(id=new_id("apr"), workspace_id=workspace_id, kind=STANDING_KIND,
            subject_ref=key, requested_by=founder, assigned_to=founder, decided_by=founder,
            risk="low", status="standing", notified_count=0, created_at=moment)
        session.add(row)
    row.summary = label
    row.decided_at = moment
    row.expires_at = moment + timedelta(days=30)
    row.detail_json = json.dumps({"standing_policy_v1": True, "founder_id": founder,
        "venture_id": venture_id, "action_type": action.action_type, "scope": action.scope,
        "scope_label": label})
    _audit(session, "approval.standing_granted", row, founder, venture_id,
           action_type=action.action_type, scope=action.scope, expires_at=row.expires_at.isoformat())
    return row


def grant(*, workspace_id, venture_id, founder_id, action: StandingAction, label):
    """Internal callers must supply verified action identity and current safety limits."""
    from app.authz.workspace import subject_person_id

    founder_id = subject_person_id(founder_id)
    with db()() as session:
        serialize_writes(session, key=_key(founder_id, venture_id, action))
        founder = _identity(session, workspace_id, venture_id, founder_id)
        row = _grant(session, workspace_id, venture_id, founder, action, label)
        session.commit()
        session.refresh(row)
        return row


def choose_card(*, workspace_id, founder_id, detail):
    from app.approvals.service import ApprovalError

    if not enabled() or not isinstance(detail, dict) or detail.get("choice") not in {"always", "deny"}:
        raise ApprovalError("invalid_decision", "That approval choice is not available.")
    venture_id, message_id, action_type = (detail.get(key) for key in ("venture_id", "message_id", "action"))
    if not all(isinstance(value, str) and value for value in (venture_id, message_id, action_type)):
        raise ApprovalError("invalid_decision", "Choose an action from its saved confirmation card.")
    with db()() as session:
        serialize_writes(session, key="chat-confirm:" + message_id)
        founder = _identity(session, workspace_id, venture_id, founder_id, require_approval=detail["choice"] == "always")
        message = session.scalar(select(ChatMessage).where(ChatMessage.id == message_id,
            ChatMessage.venture_id == venture_id, ChatMessage.workspace_id == workspace_id,
            ChatMessage.role == "assistant").with_for_update())
        conversation = session.get(ChatConversation, message.conversation_id) if message and message.conversation_id else None
        if (not message or not conversation or conversation.workspace_id != workspace_id
                or conversation.venture_id != venture_id or conversation.visibility != "private"
                or conversation.creator_person_id != founder):
            raise ApprovalError("not_found", "That saved confirmation is not available to you.")
        try:
            actions = json.loads(message.actions_json or "[]")
        except (TypeError, ValueError) as exc:
            raise ApprovalError("invalid_decision", "That confirmation card is invalid.") from exc
        if not isinstance(actions, list):
            raise ApprovalError("invalid_decision", "That confirmation card is invalid.")
        confirmation = next((item["confirmation"] for item in actions if isinstance(item, dict)
            and isinstance(item.get("confirmation"), dict)
            and item["confirmation"].get("action") == action_type), None)
        if not confirmation or confirmation.get("state"):
            raise ApprovalError("already_decided", "That action is no longer awaiting your decision.")
        state = confirmation.get("resulting_state") or {}
        if not isinstance(state, dict):
            raise ApprovalError("invalid_decision", "That confirmation card is invalid.")
        action = card_action(action_type, state)
        if detail["choice"] == "always":
            label = confirmation.get("interpretation")
            row = _grant(session, workspace_id, venture_id, founder, action,
                         label if isinstance(label, str) and label.strip() else action_type)
        else:
            confirmation["state"] = "denied"
            message.actions_json = json.dumps(actions)
            moment = utcnow()
            row = Approval(id=new_id("apr"), workspace_id=workspace_id, kind=STANDING_DECISION_KIND,
                subject_ref="chat-confirm:" + message_id, summary="You denied this saved action.",
                requested_by=founder, assigned_to=founder, decided_by=founder,
                status="rejected", risk="low", notified_count=0, created_at=moment,
                decided_at=moment, expires_at=moment, detail_json=json.dumps({"venture_id": venture_id,
                    "action_type": action_type, "message_id": message_id}))
            session.add(row)
            _audit(session, "approval.card_denied", row, founder, venture_id, action_type=action_type,
                   message_id=message_id)
        session.commit()
        session.refresh(row)
        return row


def list_grants(workspace_id):
    from app.authz.workspace import subject_person_id
    from app.tenancy import current_or_none

    tenant = current_or_none()
    if not enabled() or not tenant or tenant.workspace_id != workspace_id:
        return []
    founder = subject_person_id(tenant.actor)
    with db()() as session:
        rows = session.scalars(select(Approval).where(Approval.workspace_id == workspace_id,
            Approval.kind == STANDING_KIND, Approval.status == "standing",
            Approval.decided_by == founder, Approval.expires_at > utcnow()).order_by(Approval.created_at)).all()
        return rows


def revoke(approval_id, founder_id):
    from app.approvals.service import ApprovalError

    with db()() as session:
        serialize_writes(session, key="standing-revoke:" + approval_id)
        row = session.scalar(select(Approval).where(Approval.id == approval_id).with_for_update())
        if row is None or row.kind != STANDING_KIND or row.status != "standing":
            raise ApprovalError("not_found", "That standing approval is not available.")
        detail = json.loads(row.detail_json)
        founder = _identity(session, row.workspace_id, detail["venture_id"], founder_id, require_approval=False)
        if row.decided_by != founder or detail.get("founder_id") != founder:
            raise ApprovalError("cannot_approve", "You cannot revoke another person's approval.")
        row.status = "withdrawn"
        _audit(session, "approval.standing_revoked", row, founder, detail["venture_id"],
               action_type=detail["action_type"], scope=detail["scope"])
        session.commit()
        session.refresh(row)
        return row


def _active_grant(session, workspace_id, venture_id, founder_id, action, *, grant_id=None):
    if not enabled() or not action.eligible():
        return None
    founder = _identity(session, workspace_id, venture_id, founder_id)
    row = session.scalar(select(Approval).where(Approval.workspace_id == workspace_id,
        Approval.subject_ref == _key(founder, venture_id, action), Approval.kind == STANDING_KIND,
        Approval.status == "standing", Approval.decided_by == founder).with_for_update())
    if (not row or grant_id is not None and row.id != grant_id
            or row.expires_at is None or as_utc(row.expires_at) <= utcnow()):
        return None
    try:
        saved = json.loads(row.detail_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(saved, dict) or (
            saved.get("founder_id"), saved.get("venture_id"), saved.get("action_type"), saved.get("scope")) != (
            founder, venture_id, action.action_type, action.scope):
        return None
    return row


def matching_grant(*, workspace_id, venture_id, founder_id, action):
    from app.approvals.service import ApprovalError

    with db()() as session:
        try:
            row = _active_grant(session, workspace_id, venture_id, founder_id, action)
        except ApprovalError:
            return None
        return row.id if row else None


def auto_approve(*, workspace_id, venture_id, founder_id, action: StandingAction,
                 kind, subject_ref, summary, requested_by, detail=None, window_hours=DEFAULT_WINDOW_HOURS,
                 grant_id=None, session=None):
    """Recheck the live grant and audit the decision before ordinary guarded execution."""
    if not enabled() or not action.eligible() or kind != "workflow_write" or window_hours <= 0:
        return None
    from app.authz.workspace import subject_person_id

    founder_id = subject_person_id(founder_id)
    owns_session = session is None
    with (db()() if owns_session else nullcontext(session)) as session:
        serialize_writes(session, key=_key(founder_id, venture_id, action))
        grant_row = _active_grant(session, workspace_id, venture_id, founder_id, action, grant_id=grant_id)
        if grant_row is None:
            return None
        founder = founder_id
        moment = utcnow()
        row = Approval(id=new_id("apr"), workspace_id=workspace_id, kind=STANDING_DECISION_KIND,
            subject_ref=subject_ref, summary=summary, requested_by=requested_by,
            assigned_to=founder, decided_by=founder, decided_at=moment, status="approved",
            risk="low", notified_count=0, expires_at=min(moment + timedelta(hours=window_hours), as_utc(grant_row.expires_at)), created_at=moment,
            detail_json=json.dumps({**(detail or {}), "standing_approval_id": grant_row.id, "requested_kind": kind}, default=str))
        session.add(row)
        _audit(session, "approval.auto_approved", row, founder, venture_id,
               standing_approval_id=grant_row.id, requested_by=requested_by,
               action_type=action.action_type, scope=action.scope)
        if owns_session:
            session.commit()
            session.refresh(row)
        return row
