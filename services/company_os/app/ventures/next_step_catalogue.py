"""Generate, filter and rank pure actions. Nothing here calls a provider or writes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from pydantic import ConfigDict

from app.ventures.next_step_models import (
    ActionId, Card, Component, ContactParams, DocumentParams, DraftParams, EntryParams,
    Call, Pick, Snapshot, StrictModel, Why,
)

CATALOGUE_VERSION = "2"
PROMPT_VERSION = "2"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def calls_with_notes(snapshot):
    return [call for call in snapshot.calls if call.notes_id]


def contact_allowed(snapshot, contact_id):
    return any(contact.id == contact_id and not contact.blocked for contact in snapshot.contacts)


def targets(snapshot, action_id, now):
    now = aware(now)
    calls = [call for call in snapshot.calls if contact_allowed(snapshot, call.contact_id)]
    if action_id == "schedule_call":
        return [call for call in calls if not call.scheduled_at and not call.notes_id]
    if action_id == "prep_call_brief":
        return [call for call in calls if call.scheduled_at and now <= aware(call.scheduled_at) <= now + timedelta(hours=48) and not call.brief_id]
    if action_id == "log_call_learnings":
        return [call for call in calls if call.scheduled_at and aware(call.scheduled_at) < now and not call.notes_id]
    if action_id == "follow_up_commitment":
        return [call for call in calls if call.commitment in {"follow_up", "intro"} and not any(
            contact.id == call.contact_id and contact.sent_at and call.commitment_at
            and aware(contact.sent_at) > aware(call.commitment_at) for contact in snapshot.contacts)]
    if action_id == "collect_first_payment":
        return [call for call in calls if call.commitment in {"loi", "pay"} and not call.paid] if snapshot.paid_customers == 0 else []
    if action_id == "ask_for_referral":
        positive = [call for call in calls if call.paid or call.notes_id and (call.pains or call.price_response == "yes")]
        return positive + [contact for contact in snapshot.contacts if contact.paid and not contact.blocked
            and contact.id not in {call.contact_id for call in positive}]
    if action_id == "run_pricing_test":
        return [call for call in calls if call.notes_id and call.price_response == "unknown"]
    return calls


class Action(StrictModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    id: ActionId
    title: str
    preconditions: Callable[[Snapshot, datetime], bool]
    done_when: Callable[[Snapshot], bool]
    required_evidence_ids: Callable[[Snapshot, datetime], list[str]]
    risk: Literal["none", "sends", "spends", "publishes"]
    component: Component
    params_schema: type[StrictModel]
    cooldown: int = 86400
    priority: int


def relevant_evidence(snapshot, action_id, now):
    targeted = targets(snapshot, action_id, now)
    if action_id in {"schedule_call", "prep_call_brief", "log_call_learnings", "follow_up_commitment", "collect_first_payment", "ask_for_referral", "run_pricing_test"} and targeted:
        target = targeted[0]
        contact_id = target.contact_id if isinstance(target, Call) else target.id
        identifiers = [target.evidence_id, target.notes_id, target.learning_id] if isinstance(target, Call) else [target.id]
        if action_id == "prep_call_brief":
            identifiers += [contact_id, f"{snapshot.venture_id}:fact:problem", f"{snapshot.venture_id}:fact:audience"]
        identifiers += [item.id for item in snapshot.evidence.values() if item.contact_id == contact_id and item.kind in {"pricing", "payment"}]
        return list(dict.fromkeys(identifier for identifier in identifiers if identifier in snapshot.evidence))[-20:]
    if action_id in {"synthesize_learnings", "decide_pivot_persevere", "mvp_scope", "landing_page_waitlist"}:
        identifiers = [call.learning_id or call.notes_id for call in calls_with_notes(snapshot)]
        identifiers += [item.id for item in snapshot.evidence.values() if item.kind in {"venture_decision", "pricing", "payment"}]
    elif action_id in {"find_customers", "review_leads", "reengage_outreach"}:
        identifiers = [item.id for item in snapshot.evidence.values() if item.kind in {"lead", "sent", "reply", "journey", "venture_decision"}]
    else:
        identifiers = [item.id for item in snapshot.evidence.values() if item.kind in {"founder_fact", "idea", "review", "research"}]
    return sorted(set(identifier for identifier in identifiers if identifier in snapshot.evidence))[-20:] or [snapshot.venture_id]


def _action(action_id, title, priority, component, preconditions, done_when=lambda snapshot: False, risk="none", params_schema=EntryParams):
    return Action(id=action_id, title=title, priority=priority, component=component,
                  preconditions=preconditions, done_when=done_when, risk=risk, params_schema=params_schema,
                  required_evidence_ids=lambda snapshot, now: relevant_evidence(snapshot, action_id, now))


def _targeted(action_id, title, priority, component, *, risk="none", params_schema=ContactParams):
    return _action(action_id, title, priority, component, lambda snapshot, now: bool(targets(snapshot, action_id, now)),
                   risk=risk, params_schema=params_schema)


def learning_ready(snapshot):
    return snapshot.validated or snapshot.clarified or bool(snapshot.lead_ids or snapshot.sent or snapshot.calls)


CATALOGUE = (
    _targeted("prep_call_brief", "Prepare for your upcoming call", 1100, "call_brief"),
    _targeted("log_call_learnings", "Record what you learned from the call", 1000, "call_notes"),
    _targeted("schedule_call", "Record the time of your booked call", 950, "call_schedule"),
    _targeted("follow_up_commitment", "Follow up on their commitment", 900, "draft_editor", risk="sends", params_schema=DraftParams),
    _targeted("collect_first_payment", "Draft your first payment request", 850, "payment_draft", risk="spends", params_schema=DraftParams),
    _action("decide_pivot_persevere", "Decide: persevere, narrow the segment, or pivot", 800, "venture_decision",
            lambda snapshot, now: not snapshot.venture_decision and (len(calls_with_notes(snapshot)) >= 5 or bool(
                calls_with_notes(snapshot) and snapshot.learning_deadline and aware(snapshot.learning_deadline) <= aware(now))),
            lambda snapshot: bool(snapshot.venture_decision), params_schema=DocumentParams),
    _action("synthesize_learnings", "Synthesize your customer learnings", 700, "learning_summary",
            lambda snapshot, now: len(calls_with_notes(snapshot)) >= 3, params_schema=DocumentParams),
    _action("mvp_scope", "Scope the smallest useful MVP", 650, "mvp_scope",
            lambda snapshot, now: max(len(snapshot.pricing_supporters), snapshot.pricing_yeses, snapshot.lois) >= 2 and not snapshot.mvp_saved,
            lambda snapshot: snapshot.mvp_saved, params_schema=DocumentParams),
    _targeted("ask_for_referral", "Ask for a relevant introduction", 600, "draft_editor", risk="sends", params_schema=DraftParams),
    _action("run_pricing_test", "Test a price or ask for an LOI", 550, "pricing_test",
            lambda snapshot, now: snapshot.venture_decision == "persevere" and snapshot.price_responses == 0,
            lambda snapshot: snapshot.price_responses > 0, risk="sends", params_schema=DraftParams),
    _action("landing_page_waitlist", "Build your landing page and waitlist", 500, "landing_page",
            lambda snapshot, now: snapshot.venture_decision == "persevere" and not snapshot.page_live,
            lambda snapshot: snapshot.page_live, risk="publishes"),
    _action("reengage_outreach", "Revise your outreach or try a new segment", 400, "journey_entry",
            lambda snapshot, now: snapshot.sent > 0 and snapshot.replies / snapshot.sent < .2 and not any(
                not call.notes_id and (not call.scheduled_at or aware(call.scheduled_at) >= aware(now)) for call in snapshot.calls)),
    _action("clarify", "Clarify your customer and problem", 300, "journey_entry",
            lambda snapshot, now: not snapshot.clarified and not snapshot.sent and not snapshot.calls,
            lambda snapshot: snapshot.clarified),
    _action("validate", "Validate your current idea", 290, "journey_entry",
            lambda snapshot, now: snapshot.clarified and not snapshot.validated and not snapshot.sent and not snapshot.calls,
            lambda snapshot: snapshot.validated),
    _action("research", "Research your market and alternatives", 280, "journey_entry",
            lambda snapshot, now: snapshot.validated and not snapshot.research_id and not snapshot.lead_ids and not snapshot.sent,
            lambda snapshot: bool(snapshot.research_id), risk="spends"),
    _action("find_customers", "Find customers to learn from", 270, "journey_entry",
            lambda snapshot, now: learning_ready(snapshot) and not snapshot.lead_ids and not snapshot.sent,
            lambda snapshot: bool(snapshot.lead_ids or snapshot.sent), risk="spends"),
    _action("review_leads", "Review your saved prospects", 260, "journey_entry",
            lambda snapshot, now: bool(snapshot.lead_ids) and snapshot.sent == 0, lambda snapshot: snapshot.sent > 0),
    _action("review_progress", "Review your venture and choose what matters", 0, "journey_entry", lambda snapshot, now: True),
)
ACTIONS = {action.id: action for action in CATALOGUE}


def evidence_hash(snapshot, action, now):
    identifiers = action.required_evidence_ids(snapshot, now)
    return digest({identifier: snapshot.evidence[identifier].model_dump(exclude={"display_summary"}) for identifier in identifiers})


def filter_rank(snapshot: Snapshot, now: datetime) -> list[Action]:
    eligible = []
    for action in CATALOGUE:
        if not action.preconditions(snapshot, now) or action.done_when(snapshot):
            continue
        identifiers = action.required_evidence_ids(snapshot, now)
        if not identifiers or any(identifier not in snapshot.evidence for identifier in identifiers):
            continue
        fingerprint = evidence_hash(snapshot, action, now)
        if any(outcome.action_id == action.id and (outcome.outcome == "skip" and outcome.cooldown_until
                and aware(outcome.cooldown_until) > aware(now) or outcome.outcome in {"done", "not_relevant"}
                and outcome.evidence_hash == fingerprint) for outcome in snapshot.outcomes):
            continue
        eligible.append(action)
    return sorted(eligible, key=lambda action: (-action.priority, action.id))


def snapshot_hash(snapshot, now):
    # ponytail: SQL state plus time-gate transitions, replace with event invalidation if snapshot reads become expensive.
    return digest([snapshot.model_dump(mode="json"), CATALOGUE_VERSION, PROMPT_VERSION,
                   [[action.id, action.required_evidence_ids(snapshot, now)] for action in filter_rank(snapshot, now)]])


QUESTIONS = "\n".join([
    "Tell me about the last time this problem happened.", "What made that situation hard?",
    "What did you do to work around it?", "What did that cost in time or money?",
    "Who else is involved when you try to solve it?",
])


def rule_pick(snapshot, action, now):
    identifiers = action.required_evidence_ids(snapshot, now)
    selected = targets(snapshot, action.id, now)
    contact_id = (selected[0].contact_id if isinstance(selected[0], Call) else selected[0].id) if selected else None
    if action.params_schema is ContactParams:
        params = ContactParams(contact_id=contact_id)
    elif action.params_schema is DraftParams:
        body = {
            "follow_up_commitment": "Thanks for the conversation. Could we follow up on the next step you offered? Please confirm what works for you.",
            "collect_first_payment": "Thanks for committing to try this. Let’s confirm the scope and price before I send an invoice or payment link from my own account.",
            "ask_for_referral": "Thanks for sharing your experience. Is there someone with the same problem you would feel comfortable introducing me to? Only with their permission.",
            "run_pricing_test": "What did you pay to solve this last time? Let’s test a specific price for a defined outcome. Would a written LOI or small paid pilot be appropriate?",
        }[action.id]
        params = DraftParams(contact_id=contact_id, subject=action.title, body=body)
    elif action.params_schema is DocumentParams:
        params = DocumentParams(text="\n".join(snapshot.evidence[identifier].display_summary or "Saved evidence supports this step." for identifier in identifiers)[:8000])
    else:
        pains = list(dict.fromkeys(pain for call in calls_with_notes(snapshot) for pain in call.pains))
        params = EntryParams(context=(action.title + ("\nUse your customers' language: " + "; ".join(pains) if pains else ""))[:2000])
    return Pick(action_id=action.id, params=params, why=[Why(text=action.title + ", based on your saved evidence.", evidence_ids=identifiers)])


def validate_pick(raw, snapshot, eligible, now):
    pick = Pick.model_validate(raw)
    action = next((action for action in eligible if action.id == pick.action_id), None)
    if action is None:
        raise ValueError("Action is not in the eligible shortlist.")
    action.params_schema.model_validate(pick.params.model_dump())
    expected = rule_pick(snapshot, action, now)
    if getattr(pick.params, "contact_id", None) != getattr(expected.params, "contact_id", None):
        raise ValueError("Tool parameters changed the evidence-bound contact.")
    cited = {identifier for why in pick.why for identifier in why.evidence_ids}
    if not cited <= snapshot.evidence.keys() or not set(action.required_evidence_ids(snapshot, now)) <= cited:
        raise ValueError("Explanation must cite the required saved evidence.")
    return pick


def card(pick, snapshot, now):
    action = ACTIONS[pick.action_id]
    return Card(**pick.model_dump(), title=action.title, component=action.component, risk=action.risk,
                draft_only=action.risk != "none", required_evidence_ids=action.required_evidence_ids(snapshot, now))
