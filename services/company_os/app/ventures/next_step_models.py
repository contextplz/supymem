"""Data-only contracts for the next-action engine and its allowlisted tools."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ActionId = Literal[
    "prep_call_brief", "log_call_learnings", "follow_up_commitment", "synthesize_learnings",
    "decide_pivot_persevere", "run_pricing_test", "landing_page_waitlist", "mvp_scope",
    "collect_first_payment", "ask_for_referral", "reengage_outreach", "clarify", "validate",
    "research", "find_customers", "review_leads", "schedule_call", "review_progress",
]
Component = Literal["call_brief", "call_notes", "draft_editor", "learning_summary", "venture_decision",
                    "pricing_test", "landing_page", "mvp_scope", "payment_draft", "journey_entry", "call_schedule"]
Commitment = Literal["none", "follow_up", "intro", "loi", "pay"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Observations(StrictModel):
    pains_confirmed: list[str] = Field(default_factory=list, max_length=5)
    pains_refuted: list[str] = Field(default_factory=list, max_length=5)
    objections: list[str] = Field(default_factory=list, max_length=5)
    current_workaround: str = Field(default="", max_length=2000)
    money_or_time_spent: str = Field(default="", max_length=2000)
    willingness_to_pay: Literal["yes", "maybe", "no", "unknown"] = "unknown"
    commitment: Commitment = "none"

    @field_validator("pains_confirmed", "pains_refuted", "objections")
    @classmethod
    def short_quotes(cls, values):
        if any(not value.strip() or len(value) > 1000 for value in values):
            raise ValueError("Use non-empty quotes of at most 1000 characters.")
        return values


class Result(StrictModel):
    text: str = Field(default="", max_length=8000)
    viewed: bool = False
    decision: Literal["persevere", "narrow_segment", "pivot_problem"] | None = None
    price_response: Literal["yes", "maybe", "no"] | None = None
    commitment: Commitment = "none"
    payment_received: bool = False
    sent_by_founder: bool = False
    scheduled_at: datetime | None = None
    observations: Observations | None = None

    @field_validator("scheduled_at", mode="before")
    @classmethod
    def scheduled_time(cls, value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
            raise ValueError("Call time must include a timezone.")
        return value


class OutcomeBody(StrictModel):
    decision_id: str = Field(min_length=1, max_length=240)
    action_id: ActionId
    outcome: Literal["done", "skip", "not_relevant"]
    result: Result = Field(default_factory=Result)


class Evidence(StrictModel):
    id: str
    kind: str
    summary: str
    display_summary: str | None = None
    contact_id: str | None = None
    observations: Observations | None = None


class SavedFact(StrictModel):
    value: str | None = None
    source: str | None = None
    quote: str | None = None
    confidence: float = Field(default=0, ge=0, le=1)


class Contact(StrictModel):
    id: str
    name: str
    blocked: bool = False
    sent_at: datetime | None = None
    positive: bool = False
    paid: bool = False


class Call(StrictModel):
    contact_id: str
    evidence_id: str
    booked_at: datetime
    scheduled_at: datetime | None = None
    brief_id: str | None = None
    notes_id: str | None = None
    learning_id: str | None = None
    pains: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    price_response: Literal["yes", "maybe", "no", "unknown"] = "unknown"
    commitment: Commitment = "none"
    commitment_at: datetime | None = None
    paid: bool = False


class Outcome(StrictModel):
    action_id: ActionId
    outcome: Literal["done", "skip", "not_relevant"]
    evidence_hash: str
    contact_id: str | None = None
    cooldown_until: datetime | None = None
    result: Result = Field(default_factory=Result)


class Snapshot(StrictModel):
    venture_id: str
    workspace_id: str
    founder_facts: dict[str, SavedFact] = Field(default_factory=dict)
    review_questions: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    clarified: bool = False
    validated: bool = False
    research_id: str | None = None
    idea_revision_id: str | None = None
    lead_ids: list[str] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    calls: list[Call] = Field(default_factory=list)
    sent: int = 0
    replies: int = 0
    waitlist_signups: int = 0
    page_live: bool = False
    page_status: str = "absent"
    available_micro: int | None = None
    credit_status: str | None = None
    venture_decision: Literal["persevere", "narrow_segment", "pivot_problem"] | None = None
    price_responses: int = 0
    pricing_yeses: int = 0
    lois: int = 0
    pricing_supporters: list[str] = Field(default_factory=list)
    paid_customers: int = 0
    mvp_saved: bool = False
    learning_deadline: datetime | None = None
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    outcomes: list[Outcome] = Field(default_factory=list)


class ContactParams(StrictModel):
    contact_id: str = Field(min_length=1, max_length=200)


class DraftParams(StrictModel):
    contact_id: str | None
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4000)


class DocumentParams(StrictModel):
    text: str = Field(min_length=1, max_length=8000)


class EntryParams(StrictModel):
    context: str = Field(min_length=1, max_length=2000)


class Why(StrictModel):
    text: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class Pick(StrictModel):
    action_id: ActionId
    params: ContactParams | DraftParams | DocumentParams | EntryParams
    why: list[Why] = Field(min_length=1, max_length=3)


class Card(Pick):
    title: str
    component: Component
    risk: Literal["none", "sends", "spends", "publishes"]
    draft_only: bool
    required_evidence_ids: list[str]


class Decision(StrictModel):
    decision_id: str
    snapshot_hash: str
    venture_id: str
    workspace_id: str
    hero: Card | None
    alternatives: list[Card] = Field(max_length=2)
    evidence: dict[str, Evidence]
    source: Literal["rules", "llm"] = "rules"
    refining: bool = False
