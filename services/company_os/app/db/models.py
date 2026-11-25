"""SQLAlchemy 2.0 models for every Company OS table in 04_DATA_MODEL.md."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 2.1 ventures
# ---------------------------------------------------------------------------


class Venture(Base):
    __tablename__ = "ventures"
    __fsm__ = "venture"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text)  # Phase 10 (35 §4)
    default_gmail_mailbox_id: Mapped[str | None] = mapped_column(Text, deferred=True)  # loaded on use, so pre-s056 reads keep working
    name: Mapped[str] = mapped_column(Text, nullable=False)
    raw_idea: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    gtm_order: Mapped[str | None] = mapped_column(Text)
    autonomy_level: Mapped[str] = mapped_column(Text, nullable=False, default="L1")
    active_charter_id: Mapped[str | None] = mapped_column(Text)
    daily_token_budget: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (Index("ix_ventures_stage", "stage"),)


# ---------------------------------------------------------------------------
# 2.2 idea_briefs
# ---------------------------------------------------------------------------


class IdeaBrief(Base):
    __tablename__ = "idea_briefs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ventures.id"), nullable=False
    )
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    business_model: Mapped[str] = mapped_column(Text, nullable=False)
    build_archetype: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    monetization: Mapped[str | None] = mapped_column(Text)
    regulatory_surface: Mapped[str] = mapped_column(Text, nullable=False)
    novelty: Mapped[str] = mapped_column(Text, nullable=False)
    complexity_score: Mapped[float] = mapped_column(Float, nullable=False)
    uncertainty_score: Mapped[float] = mapped_column(Float, nullable=False)
    build_feasibility: Mapped[float] = mapped_column(Float, nullable=False)
    complexity_factors_json: Mapped[str] = mapped_column(Text, nullable=False)
    research_notes_json: Mapped[str] = mapped_column(Text, nullable=False)
    classifier_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


# ---------------------------------------------------------------------------
# 2.3 charter_slots
# ---------------------------------------------------------------------------


class CharterSlot(Base):
    __tablename__ = "charter_slots"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ventures.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    criticality: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    decision_impact: Mapped[float] = mapped_column(Float, nullable=False)
    filled_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (UniqueConstraint("venture_id", "key"),)


# ---------------------------------------------------------------------------
# 2.4 clarification_rounds / questions / answers
# ---------------------------------------------------------------------------


class ClarificationRound(Base):
    __tablename__ = "clarification_rounds"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ventures.id"), nullable=False
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    planned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_reason_json: Mapped[str] = mapped_column(Text, nullable=False)
    residual_uncertainty: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (UniqueConstraint("venture_id", "round_number"),)


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    round_id: Mapped[str] = mapped_column(
        Text, ForeignKey("clarification_rounds.id"), nullable=False
    )
    slot_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    why_asked: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False)
    allow_other: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    evpi: Mapped[float] = mapped_column(Float, nullable=False)
    ask_cost: Mapped[float] = mapped_column(Float, nullable=False)
    answerability: Mapped[float] = mapped_column(Float, nullable=False)
    depends_on_answer_of: Mapped[str | None] = mapped_column(Text)


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("questions.id"), nullable=False, unique=True
    )
    chosen_option_id: Mapped[str | None] = mapped_column(Text)
    other_text: Mapped[str | None] = mapped_column(Text)
    parsed_json: Mapped[str | None] = mapped_column(Text)
    parse_confidence: Mapped[float | None] = mapped_column(Float)
    is_dissent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    answered_at: Mapped[datetime] = mapped_column(nullable=False)


# ---------------------------------------------------------------------------
# 2.5 charters
# ---------------------------------------------------------------------------


class Charter(Base):
    __tablename__ = "charters"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ventures.id"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    parent_version: Mapped[str | None] = mapped_column(Text)
    locked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    slots_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    dissent_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (UniqueConstraint("venture_id", "version"),)


# ---------------------------------------------------------------------------
# 2.6 org_plans, agent_specs, agent_instances
# ---------------------------------------------------------------------------


class OrgPlan(Base):
    __tablename__ = "org_plans"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ventures.id"), nullable=False
    )
    charter_id: Mapped[str] = mapped_column(
        Text, ForeignKey("charters.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rationale_json: Mapped[str] = mapped_column(Text, nullable=False)
    rejected_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class AgentSpec(Base):
    __tablename__ = "agent_specs"

    id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    venture_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ventures.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    charge: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_tier: Mapped[str] = mapped_column(Text, nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    autonomy_overrides_json: Mapped[str | None] = mapped_column(Text)
    schedule_cron: Mapped[str | None] = mapped_column(Text)
    fitness_metric: Mapped[str] = mapped_column(Text, nullable=False)
    retire_when: Mapped[str] = mapped_column(Text, nullable=False)
    parent_version: Mapped[str | None] = mapped_column(Text)
    spec_checksum: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (PrimaryKeyConstraint("id", "version"),)


class AgentInstance(Base):
    __tablename__ = "agent_instances"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ventures.id"), nullable=False
    )
    spec_id: Mapped[str] = mapped_column(Text, nullable=False)
    spec_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    quarantine_cycles_passed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    fitness_ewma: Mapped[float | None] = mapped_column(Float)
    last_active_at: Mapped[datetime | None] = mapped_column()
    retired_at: Mapped[datetime | None] = mapped_column()
    retired_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_agent_instances_venture_status", "venture_id", "status"),
    )


# ---------------------------------------------------------------------------
# 2.7 agent_runs
# ---------------------------------------------------------------------------


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agent_instances.id"), nullable=False
    )
    spec_version: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    shadow: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        Index("ix_agent_runs_agent_started", "agent_id", "started_at"),
    )


# ---------------------------------------------------------------------------
# 2.8 ADR / work_orders / gate_results
# ---------------------------------------------------------------------------


class ArchitectureDecisionRecord(Base):
    __tablename__ = "architecture_decision_records"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    charter_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    locked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stack_json: Mapped[str] = mapped_column(Text, nullable=False)
    schema_json: Mapped[str] = mapped_column(Text, nullable=False)
    api_contract_json: Mapped[str] = mapped_column(Text, nullable=False)
    auth_flow_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_strategy_json: Mapped[str] = mapped_column(Text, nullable=False)
    deploy_topology_json: Mapped[str] = mapped_column(Text, nullable=False)
    component_inventory_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column()


class WorkOrder(Base):
    __tablename__ = "work_orders"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    adr_id: Mapped[str] = mapped_column(
        Text, ForeignKey("architecture_decision_records.id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    files_expected_json: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_json: Mapped[str] = mapped_column(Text, nullable=False)
    depends_on_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    adapter: Mapped[str | None] = mapped_column(Text)
    adapter_session_id: Mapped[str | None] = mapped_column(Text)
    worktree_path: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    rework_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_json: Mapped[str | None] = mapped_column(Text)
    diff_stat_json: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class GateResult(Base):
    __tablename__ = "gate_results"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    work_order_id: Mapped[str] = mapped_column(
        Text, ForeignKey("work_orders.id"), nullable=False
    )
    gate: Mapped[str] = mapped_column(Text, nullable=False)
    passed: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    findings_json: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_gate_results_wo", "work_order_id"),)


# ---------------------------------------------------------------------------
# 2.9 human_tasks
# ---------------------------------------------------------------------------


class HumanTask(Base):
    __tablename__ = "human_tasks"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    why: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    blocks_json: Mapped[str] = mapped_column(Text, nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    steps_json: Mapped[str] = mapped_column(Text, nullable=False)
    expects_json: Mapped[str] = mapped_column(Text, nullable=False)
    verification_json: Mapped[str | None] = mapped_column(Text)
    submitted_json: Mapped[str | None] = mapped_column(Text)
    vault_refs_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        Index("ix_human_tasks_venture_status", "venture_id", "status", "priority"),
    )


# ---------------------------------------------------------------------------
# 2.10 GTM
# ---------------------------------------------------------------------------


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    armed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    prereqs_json: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    budget_usd_month: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    spent_usd_month: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    armed_at: Mapped[datetime | None] = mapped_column()


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    success_metric: Mapped[str] = mapped_column(Text, nullable=False)
    success_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    strategy_json: Mapped[str] = mapped_column(Text, nullable=False)
    channels_json: Mapped[str] = mapped_column(Text, nullable=False)
    projected_cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    owner_agent_id: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime | None] = mapped_column()
    ends_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        Text, ForeignKey("campaigns.id"), nullable=False
    )
    channel_id: Mapped[str] = mapped_column(
        Text, ForeignKey("channels.id"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    media_json: Mapped[str | None] = mapped_column(Text)
    contains_url: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projected_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    actual_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    scheduled_for: Mapped[datetime | None] = mapped_column()
    status: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text)
    external_url: Mapped[str | None] = mapped_column(Text)
    metrics_json: Mapped[str | None] = mapped_column(Text)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    published_at: Mapped[datetime | None] = mapped_column()


# ---------------------------------------------------------------------------
# 2.11 feedback + metrics
# ---------------------------------------------------------------------------


class FeedbackEvent(Base):
    __tablename__ = "feedback_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    trust: Mapped[str] = mapped_column(Text, nullable=False, default="untrusted")
    sentiment: Mapped[float | None] = mapped_column(Float)
    topic: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[int | None] = mapped_column(Integer)
    about_json: Mapped[str | None] = mapped_column(Text)
    classified_by: Mapped[str | None] = mapped_column(Text)
    classified_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_feedback_venture_created", "venture_id", "created_at"),
    )


class Metric(Base):
    __tablename__ = "metrics"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    window: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(nullable=False)
    attributed_to_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_metrics_lookup", "venture_id", "key", "window", "observed_at"),
    )


# ---------------------------------------------------------------------------
# 2.12 evolution
# ---------------------------------------------------------------------------


class EvolutionCycle(Base):
    __tablename__ = "evolution_cycles"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    diagnosis_json: Mapped[str | None] = mapped_column(Text)
    proposals_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposals_promoted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    cycle_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("evolution_cycles.id")
    )
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    risk: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    predicted_metric: Mapped[str] = mapped_column(Text, nullable=False)
    predicted_delta: Mapped[float] = mapped_column(Float, nullable=False)
    spec_json: Mapped[str | None] = mapped_column(Text)
    diff_text: Mapped[str | None] = mapped_column(Text)
    simulation_id: Mapped[str | None] = mapped_column(Text)
    validation_json: Mapped[str | None] = mapped_column(Text)
    peer_review_json: Mapped[str | None] = mapped_column(Text)
    authored_by_agent_id: Mapped[str | None] = mapped_column(Text)
    reviewed_by_agent_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        Index("ix_proposals_venture_status", "venture_id", "status", "created_at"),
    )


class Promotion(Base):
    __tablename__ = "promotions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_id: Mapped[str] = mapped_column(
        Text, ForeignKey("proposals.id"), nullable=False
    )
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_version: Mapped[str | None] = mapped_column(Text)
    to_version: Mapped[str | None] = mapped_column(Text)
    git_ref: Mapped[str | None] = mapped_column(Text)
    baseline_json: Mapped[str] = mapped_column(Text, nullable=False)
    observe_until: Mapped[datetime] = mapped_column(nullable=False)
    observed_json: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    rolled_back_at: Mapped[datetime | None] = mapped_column()
    rollback_reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class ArchiveEntry(Base):
    __tablename__ = "archive_entries"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    parent_version: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Quality-diversity fields; archive variants are search parents, not merely
    # a promotion log.
    descriptors_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    novelty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fitness_scalar: Mapped[float | None] = mapped_column(Float)
    sample_n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(Text, nullable=False, default="never_promoted")
    created_at: Mapped[datetime] = mapped_column(nullable=False)


# ---------------------------------------------------------------------------
# 2.13 world model
# ---------------------------------------------------------------------------


class WorldStateSnapshot(Base):
    __tablename__ = "world_state_snapshots"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    derived_from_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class SimulationRun(Base):
    __tablename__ = "simulation_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_id: Mapped[str | None] = mapped_column(Text)
    snapshot_id: Mapped[str] = mapped_column(
        Text, ForeignKey("world_state_snapshots.id"), nullable=False
    )
    action_json: Mapped[str] = mapped_column(Text, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    predictions_json: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    method_mix_json: Mapped[str] = mapped_column(Text, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class CalibrationRecord(Base):
    __tablename__ = "calibration_records"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    simulation_id: Mapped[str] = mapped_column(
        Text, ForeignKey("simulation_runs.id"), nullable=False
    )
    quantity: Mapped[str] = mapped_column(Text, nullable=False)
    predicted: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_lo: Mapped[float | None] = mapped_column(Float)
    predicted_hi: Mapped[float | None] = mapped_column(Float)
    actual: Mapped[float] = mapped_column(Float, nullable=False)
    abs_error: Mapped[float] = mapped_column(Float, nullable=False)
    in_interval: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_calibration_quantity", "venture_id", "quantity", "resolved_at"),
    )


# ---------------------------------------------------------------------------
# 2.14 infrastructure (jobs, event_log, token_ledger)
# ---------------------------------------------------------------------------


class Job(Base):
    __tablename__ = "jobs"
    __fsm__ = "job"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    venture_id: Mapped[str | None] = mapped_column(Text)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    token_budget: Mapped[int | None] = mapped_column(Integer)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    effect_key: Mapped[str | None] = mapped_column(Text)
    run_after: Mapped[datetime | None] = mapped_column()
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    scheduled_for: Mapped[datetime | None] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_jobs_claim", "status", "priority", "scheduled_for"),
        UniqueConstraint("id", "workspace_id", "venture_id", name="uq_job_import_scope"),
    )


class EventLog(Base):
    """Append-only event tail. Updates/deletes blocked by DB trigger."""

    __tablename__ = "event_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    venture_id: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(nullable=False)
    # Phase 10 per-actor audit (42 §5). Every row must answer: who, on whose
    # authority, what, to what, and what happened.
    workspace_id: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(Text)
    actor_kind: Mapped[str | None] = mapped_column(Text)  # person|agent|token|system
    on_behalf_of: Mapped[str | None] = mapped_column(Text)
    surface: Mapped[str | None] = mapped_column(Text)  # ui|api|mcp|cli|sdk|internal
    approval_id: Mapped[str | None] = mapped_column(Text)
    target_ref: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)  # ok|denied|error
    denied_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_event_log_topic", "topic", "seq"),
        Index("ix_event_log_venture", "venture_id", "seq"),
    )


class TokenLedger(Base):
    __tablename__ = "token_ledger"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_token_ledger_venture_day", "venture_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# Phase 9 — onboarding / activity / provisioning / planning / chat
# ---------------------------------------------------------------------------


class OnboardingRun(Base):
    __tablename__ = "onboarding_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ventures.id"), nullable=False
    )
    workspace_id: Mapped[str | None] = mapped_column(Text)  # Phase 10 (35 §4)
    entry_mode: Mapped[str] = mapped_column(Text, nullable=False, default="idea")
    mode: Mapped[str] = mapped_column(Text, nullable=False)  # guided | auto
    status: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    phases_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    phases_total: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    paused_reason: Mapped[str | None] = mapped_column(Text)
    resume_token: Mapped[str | None] = mapped_column(Text)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    research_founder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_up_headline: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("venture_id", "started_at"),
        Index("ix_onboarding_runs_venture", "venture_id", "started_at"),
    )


class OnboardingStep(Base):
    __tablename__ = "onboarding_steps"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("onboarding_runs.id"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    output_json: Mapped[str | None] = mapped_column(Text)
    error_class: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("run_id", "step_id", "attempt"),
        Index("ix_onboarding_steps_run", "run_id", "started_at"),
    )


class ActivityLine(Base):
    """Append-only activity log. Updates/deletes blocked by DB trigger."""

    __tablename__ = "activity_lines"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(Text)  # Phase 10 (35 §4)
    run_id: Mapped[str | None] = mapped_column(Text)
    step_id: Mapped[str | None] = mapped_column(Text)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    detail_json: Mapped[str | None] = mapped_column(Text)
    query: Mapped[str | None] = mapped_column(Text)
    evidence_url: Mapped[str | None] = mapped_column(Text)
    undo_json: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_activity_venture_seq", "venture_id", "seq"),)


class VentureResource(Base):
    __tablename__ = "venture_resources"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ventures.id"), nullable=False
    )
    workspace_id: Mapped[str | None] = mapped_column(Text)  # Phase 10 (35 §4)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    simulated: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    handle: Mapped[str] = mapped_column(Text, nullable=False)
    display: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    detail_json: Mapped[str | None] = mapped_column(Text)
    secrets_json: Mapped[str | None] = mapped_column(Text)
    teardown_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("venture_id", "resource"),
        Index("ix_venture_resources_venture", "venture_id"),
    )


class TaskFingerprint(Base):
    __tablename__ = "task_fingerprints"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    dismiss_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("venture_id", "fingerprint"),
        Index("ix_task_fingerprints_venture", "venture_id"),
    )


class PlannedTask(Base):
    """Next-up agent tasks (distinct from human_tasks / concierge)."""

    __tablename__ = "planned_tasks"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(Text)  # Phase 10 (35 §4)
    engagement_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    why_now: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    est_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    runnable: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="planner")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fingerprint: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        Index("ix_planned_tasks_venture_status", "venture_id", "status", "rank"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(Text)  # Phase 10 (35 §4)
    conversation_id: Mapped[str | None] = mapped_column(Text, ForeignKey("chat_conversations.id"))
    author_person_id: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False)  # user | assistant | system
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="text")
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    citations_json: Mapped[str | None] = mapped_column(Text)
    actions_json: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_chat_messages_venture", "venture_id", "created_at"),
        Index("ix_chat_message_conversation", "conversation_id", "created_at", "id"),
    )


class ChatConversation(Base):
    __tablename__ = "chat_conversations"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    creator_person_id: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    pinned_at: Mapped[datetime | None] = mapped_column(nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)
    folder: Mapped[str | None] = mapped_column(Text)
    creation_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        CheckConstraint("visibility IN ('private', 'legacy_shared')"),
        CheckConstraint("visibility != 'private' OR creator_person_id IS NOT NULL"),
        CheckConstraint("length(folder) <= 60", name="ck_chat_conversation_folder_length"),
        Index("ix_chat_conversation_owner", "workspace_id", "venture_id", "creator_person_id"),
    )


class ChatContextCheckpoint(Base):
    __tablename__ = "chat_context_checkpoints"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, ForeignKey("chat_conversations.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    watermark_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_digest: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (UniqueConstraint("conversation_id", "version"),)


class InstitutionalMemory(Base):
    __tablename__ = "institutional_memories"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    record_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __table_args__ = (UniqueConstraint("venture_id", "dedupe_key"), Index("ix_institutional_scope", "workspace_id", "venture_id"))


class MemoryWorkingEntry(Base):
    __tablename__ = "memory_working_entries"
    scope_key: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    venture_id: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class VentureDocument(Base):
    __tablename__ = "venture_documents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str | None] = mapped_column(Text)
    body_md: Mapped[str | None] = mapped_column(Text)
    provenance_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (Index("ix_venture_documents_venture", "venture_id"),
                      Index("ix_venture_documents_lease", "kind", "lease_expires_at"))


# ---------------------------------------------------------------------------
# Phase 10 — workspaces, entry modes, boundary, engagements, connectors
# Docs 35–47. Ordered by owning doc.
# ---------------------------------------------------------------------------


class Person(Base):
    """A human. Distinct from an Agent. Every mutation names one (42 §1)."""

    __tablename__ = "people"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class Workspace(Base):
    """Top-level container: one narrated log, one governance scope (35 §3)."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # venture|company|desk
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # idea | surprise | engagement | existing_company
    entry_mode: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str] = mapped_column(Text, ForeignKey("people.id"), nullable=False)
    tenancy: Mapped[str] = mapped_column(Text, nullable=False, default="local")
    autonomy_level: Mapped[str] = mapped_column(Text, nullable=False, default="L1")
    observe_level: Mapped[str] = mapped_column(Text, nullable=False, default="O0")
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    default_gmail_mailbox_id: Mapped[str | None] = mapped_column(Text, deferred=True)  # loaded on use, so pre-s056 reads keep working
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (Index("ix_workspaces_owner", "owner_id", "created_at"),)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id"), nullable=False
    )
    person_id: Mapped[str] = mapped_column(Text, ForeignKey("people.id"), nullable=False)
    # owner | admin | maintainer | contributor | viewer
    role: Mapped[str] = mapped_column(Text, nullable=False)
    can_approve: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("workspace_id", "person_id"),)


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    invited_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (UniqueConstraint("workspace_id", "email"),)


class BoundaryLedgerEntry(Base):
    """Append-only record of everything that left this machine (43 §6)."""

    __tablename__ = "boundary_ledger"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    classes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    bytes_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    redactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    block_reason: Mapped[str | None] = mapped_column(Text)
    no_train_asserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trace_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_boundary_ledger_ws", "workspace_id", "at"),)


class Engagement(Base):
    """Bounded work with a deliverable and a done-definition (36 §3)."""

    __tablename__ = "engagements"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id"), nullable=False
    )
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[str] = mapped_column(Text, nullable=False)  # errand|task|project
    title: Mapped[str] = mapped_column(Text, nullable=False)
    request_verbatim: Mapped[str] = mapped_column(Text, nullable=False)
    definition_of_done: Mapped[str] = mapped_column(Text, nullable=False)
    not_doing_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    target_ref: Mapped[str | None] = mapped_column(Text)
    deliverables_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    plan_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    revert_hint: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_cap_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.15)
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="composer")
    origin_ref: Mapped[str | None] = mapped_column(Text)
    parent_workflow_id: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str | None] = mapped_column(Text)
    clarification_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        Index("ix_engagements_ws", "workspace_id", "created_at"),
        Index("ix_engagements_status", "workspace_id", "status"),
    )


class OpportunitySlate(Base):
    __tablename__ = "opportunity_slates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id"), nullable=False
    )
    seed_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    seed_hash: Mapped[str] = mapped_column(Text, nullable=False, default="")
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_slates_ws", "workspace_id", "created_at"),)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    slate_id: Mapped[str] = mapped_column(
        Text, ForeignKey("opportunity_slates.id"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    one_liner: Mapped[str] = mapped_column(Text, nullable=False)
    first_customer: Mapped[str] = mapped_column(Text, nullable=False)
    wedge: Mapped[str] = mapped_column(Text, nullable=False)
    why_now: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    kill_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    shape_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    effort_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    researched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed")
    reject_reason: Mapped[str | None] = mapped_column(Text)
    teaser_path: Mapped[str | None] = mapped_column(Text)
    promoted_venture_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_opps_ws", "workspace_id", "status", "created_at"),)


class SiteVersion(Base):
    """One rendered first-look / refined landing page (45 §5)."""

    __tablename__ = "site_versions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    venture_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)  # first_look|refined
    template: Mapped[str] = mapped_column(Text, nullable=False)
    archetype: Mapped[str] = mapped_column(Text, nullable=False)
    brandkit_hash: Mapped[str] = mapped_column(Text, nullable=False)
    copy_hash: Mapped[str] = mapped_column(Text, nullable=False)
    copy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    brandkit_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    fallback: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "version"),
        Index("ix_site_versions_ws", "workspace_id", "version"),
        Index("ix_site_versions_venture", "workspace_id", "venture_id", "version"),
    )


class InterestSignal(Base):
    """A real demand signal from the generated page (45 §4).

    ``email`` is third_party_pii: local only, never sent to a model.
    """

    __tablename__ = "interest_signals"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    site_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    email: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    referrer: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="direct")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_interest_ws", "workspace_id", "created_at"),)


class Connection(Base):
    """A link to a system THEY own. Never torn down by us (39 §2)."""

    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_account: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    auth_kind: Mapped[str] = mapped_column(Text, nullable=False, default="pat")
    secret_ref: Mapped[str | None] = mapped_column(Text)
    scopes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    resource_grants_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    declined_scopes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    webhook_ref: Mapped[str | None] = mapped_column(Text)
    cursors_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    simulated: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_sync_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    health: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", "external_account"),
        Index("ix_connections_ws", "workspace_id", "status"),
    )


class FounderLLMSetting(Base):
    """A founder's BYOK selection; the API key remains in the credential broker."""

    __tablename__ = "founder_llm_settings"

    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_ref: Mapped[str] = mapped_column(Text, nullable=False)


class ConnectionEvent(Base):
    """Append-only: everything we touched in their systems (39 §2)."""

    __tablename__ = "connection_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    connection_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)  # read|write|webhook
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    engagement_id: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    bytes_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    simulated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)  # ok|denied|error
    denied_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_conn_events", "connection_id", "at"),)


class CompanyMap(Base):
    __tablename__ = "company_maps"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    confidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    corrections_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    house_style_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    corrected_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("workspace_id", "version"),)


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    trigger_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    steps_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    sla_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    on_failure_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    autonomy: Mapped[str] = mapped_column(Text, nullable=False, default="propose")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    paused_reason: Mapped[str | None] = mapped_column(Text)
    cost_cap_run_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.15)
    cost_cap_month_usd: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="described")
    unmapped_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_run_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_workflows_ws", "workspace_id", "status"),)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    engagement_id: Mapped[str | None] = mapped_column(Text)
    trigger_payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    shadow: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verdict: Mapped[str | None] = mapped_column(Text)  # right|wrong
    verdict_note: Mapped[str | None] = mapped_column(Text)
    simulated_writes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    outcome_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("workflow_id", "idempotency_key"),
        Index("ix_workflow_runs", "workflow_id", "started_at"),
    )


class Approval(Base):
    """One pending human decision, whatever raised it (42 §3)."""

    __tablename__ = "approvals"
    __fsm__ = "approval"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_ref: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    risk: Mapped[str] = mapped_column(Text, nullable=False, default="low")
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column()
    decision_note: Mapped[str | None] = mapped_column(Text)
    notified_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_approvals_queue", "workspace_id", "status", "risk", "created_at"),
    )


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    person_id: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    hash: Mapped[str] = mapped_column(Text, nullable=False)
    scopes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    surface: Mapped[str] = mapped_column(Text, nullable=False, default="api")
    last_used_at: Mapped[datetime | None] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_api_tokens_person", "person_id"),)


class IdempotencyRecord(Base):
    """Stored responses for Idempotency-Key replay (41 §4)."""

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    events_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    subscription_id: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (Index("ix_webhook_deliveries", "subscription_id", "status"),)


class SuspiciousContentEvent(Base):
    """Prompt-injection attempts observed in third-party content (47 R13)."""

    __tablename__ = "suspicious_content_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(nullable=False)


# ---------------------------------------------------------------------------
# Polsia ops supersets (doc 49) — workspace-scoped, SQLite-safe Text JSON
# ---------------------------------------------------------------------------


class OpsCompanyConfig(Base):
    __tablename__ = "ops_company_config"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    mission: Mapped[str | None] = mapped_column(Text)
    vision: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    target_market: Mapped[str | None] = mapped_column(Text)
    value_prop: Mapped[str | None] = mapped_column(Text)
    pricing_model_json: Mapped[str | None] = mapped_column(Text)
    goals_json: Mapped[str | None] = mapped_column(Text)
    kpis_json: Mapped[str | None] = mapped_column(Text)
    website_url: Mapped[str | None] = mapped_column(Text)
    github_repo: Mapped[str | None] = mapped_column(Text)
    product_type: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")
    daily_cycle_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("workspace_id"),)


class OpsTask(Base):
    __tablename__ = "ops_tasks"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    agent_type: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    source: Mapped[str] = mapped_column(Text, nullable=False, default="orchestrator")
    scheduled_date: Mapped[datetime | None] = mapped_column()
    result_summary: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_ops_tasks_status", "workspace_id", "status"),
        Index("ix_ops_tasks_agent_type", "workspace_id", "agent_type"),
    )


class OpsAgentRun(Base):
    __tablename__ = "ops_agent_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("ops_tasks.id"))
    agent_type: Mapped[str] = mapped_column(Text, nullable=False)
    run_type: Mapped[str] = mapped_column(Text, nullable=False, default="task")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    input_context_json: Mapped[str | None] = mapped_column(Text)
    output_json: Mapped[str | None] = mapped_column(Text)
    raw_log: Mapped[str | None] = mapped_column(Text)
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_secs: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column()


class OpsActivity(Base):
    __tablename__ = "ops_activity"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    agent_type: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail_json: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str] = mapped_column(Text, nullable=False, default="info")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_ops_activity_created_at", "workspace_id", "created_at"),
        Index("ix_ops_activity_agent_type", "workspace_id", "agent_type"),
    )


class OpsMemory(Base):
    __tablename__ = "ops_memory"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    tags_json: Mapped[str | None] = mapped_column(Text)
    chroma_id: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_ops_memory_category", "workspace_id", "category"),)


class OpsSocialPost(Base):
    __tablename__ = "ops_social_posts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False, default="twitter")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    tweet_id: Mapped[str | None] = mapped_column(Text)
    scheduled_for: Mapped[datetime | None] = mapped_column()
    published_at: Mapped[datetime | None] = mapped_column()
    engagement_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class OpsSocialEngagement(Base):
    __tablename__ = "ops_social_engagements"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    mention_id: Mapped[str | None] = mapped_column(Text)
    author_handle: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    our_reply: Mapped[str | None] = mapped_column(Text)
    reply_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "mention_id", name="uq_ops_social_engagement_mention"),
    )


class OpsProspect(Base):
    __tablename__ = "ops_prospects"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("workspace_id", "email", name="uq_ops_prospect_email"),)


class OpsEmailCampaign(Base):
    __tablename__ = "ops_email_campaigns"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    target_segment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    total_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_opened: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_replied: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class OpsEmailLog(Base):
    __tablename__ = "ops_email_logs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    prospect_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ops_prospects.id"), nullable=False
    )
    campaign_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("ops_email_campaigns.id")
    )
    sequence_step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    sendgrid_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="sent")
    sent_at: Mapped[datetime | None] = mapped_column()
    opened_at: Mapped[datetime | None] = mapped_column()
    replied_at: Mapped[datetime | None] = mapped_column()
    follow_up_due: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class OpsAdCampaign(Base):
    __tablename__ = "ops_ad_campaigns"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    daily_budget_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_spent_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class OpsAdMetric(Base):
    __tablename__ = "ops_ad_metrics"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ops_ad_campaigns.id"), nullable=False
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conversions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spend_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ctr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cpc: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    roas: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class OpsCompetitor(Base):
    __tablename__ = "ops_competitors"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    website: Mapped[str | None] = mapped_column(Text)
    pricing_info_json: Mapped[str | None] = mapped_column(Text)
    positioning: Mapped[str | None] = mapped_column(Text)
    strengths_json: Mapped[str | None] = mapped_column(Text)
    weaknesses_json: Mapped[str | None] = mapped_column(Text)
    last_researched: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class OpsStripeEvent(Base):
    __tablename__ = "ops_stripe_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    stripe_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str | None] = mapped_column(Text)
    amount_cents: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="processed")
    raw_payload_json: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "stripe_event_id", name="uq_ops_stripe_event"),
        Index("ix_ops_stripe_events_event_type", "workspace_id", "event_type"),
    )


class OpsRevenueSnapshot(Base):
    __tablename__ = "ops_revenue_snapshots"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    mrr_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    arr_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_subscribers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    churned_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_revenue_month_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stripe_balance_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "snapshot_date", name="uq_ops_revenue_snapshot"),
        Index("ix_ops_revenue_snapshots_date", "workspace_id", "snapshot_date"),
    )


class OpsExpenseRecord(Base):
    __tablename__ = "ops_expense_records"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    vendor: Mapped[str] = mapped_column(Text, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="usd")
    description: Mapped[str | None] = mapped_column(Text)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    external_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class OpsDailyReport(Base):
    __tablename__ = "ops_daily_reports"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    morning_plan: Mapped[str | None] = mapped_column(Text)
    evening_summary: Mapped[str | None] = mapped_column(Text)
    tasks_planned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics_snapshot_json: Mapped[str | None] = mapped_column(Text)
    insights_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "report_date", name="uq_ops_daily_report"),
    )


# ---------------------------------------------------------------------------
# Stage S5 — money ledger and live agent status
# ---------------------------------------------------------------------------


class MoneyLedgerEntry(Base):
    """Immutable double-entry posting; amounts are always integer minor units."""

    __tablename__ = "money_ledger_entries"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    debit_account: Mapped[str] = mapped_column(Text, nullable=False)
    credit_account: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str | None] = mapped_column(Text)
    meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source: Mapped[str] = mapped_column(Text, nullable=False, default="internal")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("venture_id", "ref", name="uq_money_ledger_venture_ref"),
        Index("ix_money_ledger_venture_created", "venture_id", "created_at"),
    )


class VentureRevenueSnapshot(Base):
    """Optional reconciled revenue rollup; the ledger remains authoritative."""

    __tablename__ = "venture_revenue_snapshots"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    earned_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_revenue_snapshot_venture", "venture_id", "created_at"),)


class AgentLiveStatus(Base):
    __tablename__ = "agent_live_status"

    agent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agent_instances.id"), primary_key=True
    )
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="idle")
    current_task_desc: Mapped[str | None] = mapped_column(Text)
    last_heartbeat: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_agent_live_venture", "venture_id", "state"),)


class OpsDeployment(Base):
    __tablename__ = "ops_deployments"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    service_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    trigger_source: Mapped[str] = mapped_column(Text, nullable=False, default="api")
    external_id: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    health_check_url: Mapped[str | None] = mapped_column(Text)
    health_check_passed: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()


# ---------------------------------------------------------------------------
# Credits and admin control (doc 99)
# Integer micro-credits only. credit_ledger / admin_audit_log are append-only
# (no updated_at). Distinct from TokenLedger on token_ledger.
# ---------------------------------------------------------------------------


class CreditAccount(Base):
    """One mutable balance row per venture. ``available`` is always derived."""

    __tablename__ = "credit_accounts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="venture")
    venture_id: Mapped[str | None] = mapped_column(Text, unique=True)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    owner_person_id: Mapped[str | None] = mapped_column(Text)
    granted_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    spent_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "workspace_id", "venture_id", name="uq_credit_account_publication_scope"),
        Index("uq_credit_accounts_person", "owner_person_id", unique=True,
              postgresql_where=text("scope = 'person'"), sqlite_where=text("scope = 'person'")),
        CheckConstraint("granted_micro >= 0", name="ck_credit_accounts_granted_micro"),
        CheckConstraint("spent_micro >= 0", name="ck_credit_accounts_spent_micro"),
        CheckConstraint(
            "reserved_micro >= 0", name="ck_credit_accounts_reserved_micro"
        ),
        CheckConstraint(
            "spent_micro + reserved_micro <= granted_micro",
            name="ck_credit_accounts_no_overspend",
        ),
    )


class CreditLedgerEntry(Base):
    """Append-only, hash-chained movement of value. ``seq`` is total order.

    SQLite can only AUTOINCREMENT an INTEGER PRIMARY KEY, so the alembic table
    uses ``seq`` as that key and ``id`` as TEXT UNIQUE. The ORM primary key stays
    ``id`` so ``session.get`` works with ``cle_…`` identifiers.

    ``Identity()`` makes ``create_all`` on Postgres emit ``GENERATED BY DEFAULT
    AS IDENTITY`` so ``pg_get_serial_sequence('credit_ledger', 'seq')`` works;
    SQLite ignores Identity (``supports_identity_columns`` is false) and the
    service still assigns ``max(seq)+1`` under ``BEGIN IMMEDIATE``.
    """

    __tablename__ = "credit_ledger"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(), unique=True, nullable=False
    )
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    amount_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_after_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    reservation_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    entry_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (Index("ix_credit_ledger_account_created", "account_id", "created_at"),)


class CreditReservation(Base):
    """Open claim on future spend. Holds are not ledger movements."""

    __tablename__ = "credit_reservations"
    __fsm__ = "credit_reservation"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(Text)
    is_pool: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    remaining_micro: Mapped[int | None] = mapped_column(BigInteger)
    amount_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    settled_micro: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    venture_id: Mapped[str | None] = mapped_column(Text)
    task_invocation: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    clamped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        CheckConstraint("amount_micro > 0 OR (amount_micro = 0 AND purpose = 'device.work_order' "
                        "AND NOT is_pool AND model IS NULL AND provider IS NULL)",
                        name="ck_credit_reservations_amount"),
        CheckConstraint("(is_pool AND remaining_micro IS NOT NULL AND remaining_micro >= 0 AND remaining_micro <= amount_micro) OR "
                        "(NOT is_pool AND remaining_micro IS NULL)", name="ck_credit_reservations_pool"),
        Index("ix_credit_reservations_status_expires", "status", "expires_at"),
    )


class CreditPolicy(Base):
    """Admin-editable knobs. Seeded by migration b1c2d3e4f5a6."""

    __tablename__ = "credit_policies"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_by: Mapped[str | None] = mapped_column(Text)


class CreditGrantApproval(Base):
    """Dual-control queue for grants above the policy threshold."""

    __tablename__ = "credit_grant_approvals"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    target_email: Mapped[str | None] = mapped_column(Text)
    target_account_id: Mapped[str | None] = mapped_column(Text)
    amount_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(Text)
    applied_ledger_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column()


class AdminUser(Base):
    """Elevated operator. Email is unique and lowercased by the service layer."""

    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enrolled_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text)
    last_login_at: Mapped[datetime | None] = mapped_column()
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column()


class AdminSession(Base):
    """Server-side elevated session. Stores ``token_hash`` only, never the token."""

    __tablename__ = "admin_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    admin_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column()
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)


class AdminAuditLogEntry(Base):
    """Append-only, hash-chained admin action log. No ``updated_at``."""

    __tablename__ = "admin_audit_log"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(), unique=True, nullable=False
    )
    actor_admin_id: Mapped[str | None] = mapped_column(Text)
    actor_email: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    amount_micro: Mapped[int | None] = mapped_column(BigInteger)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    entry_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class CreditOrder(Base):
    """Server-priced credit pack checkout. Credits are granted only on webhook."""

    __tablename__ = "credit_orders"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    person_id: Mapped[str | None] = mapped_column(Text)
    sku: Mapped[str] = mapped_column(Text, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    bonus_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(Text)
    provider_ref: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    ledger_entry_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_order_id",
            name="uq_credit_orders_provider_order",
        ),
        CheckConstraint(
            "credits > 0 AND amount_minor > 0",
            name="ck_credit_orders_positive_amounts",
        ),
        CheckConstraint(
            "status IN ('pending', 'paid', 'failed', 'expired', 'refunded')",
            name="ck_credit_orders_status",
        ),
    )


class PaymentEvent(Base):
    """Provider webhook receipt. ``provider_event_id`` UNIQUE is the replay lock."""

    __tablename__ = "payment_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_event_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(Text)
    amount_cents: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(Text)
    credits_micro: Mapped[int | None] = mapped_column(BigInteger)
    account_id: Mapped[str | None] = mapped_column(Text)
    order_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    signature_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class PersonIdentity(Base):
    """Gateway subject (``usr_…`` or ``founder``) linked to a Person."""

    __tablename__ = "person_identities"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    person_id: Mapped[str] = mapped_column(
        Text, ForeignKey("people.id"), nullable=False
    )
    subject: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class AnalyticsEvent(Base):
    """Append-only product event. Props stay JSON so the schema does not churn."""

    __tablename__ = "analytics_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    person_id: Mapped[str | None] = mapped_column(Text, index=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    venture_id: Mapped[str | None] = mapped_column(Text, index=True)
    event_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    props_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_analytics_events_name_time", "event_name", "occurred_at"),)


class AnalyticsDailyRollup(Base):
    """Pre-aggregated product metrics. Safe to recompute (idempotent per day)."""

    __tablename__ = "analytics_daily_rollup"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    dau: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wau: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mau: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signups: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_ventures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_paid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class AnalyticsFeatureCatalog(Base):
    """Reference copy of ``analytics.feature_catalog`` so report views can list zero-use features."""

    __tablename__ = "analytics_feature_catalog"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    feature_group: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    inventory_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")


class AnalyticsFeatureMap(Base):
    """Control id / chat tool / label alias -> slug, so old rows land in the same buckets as new ones."""

    __tablename__ = "analytics_feature_map"

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False)


class AnalyticsInternalAccount(Base):
    """Team accounts (from env config) excluded from the external half of every report."""

    __tablename__ = "analytics_internal_accounts"

    person_id: Mapped[str] = mapped_column(Text, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class AnalyticsFeatureDaily(Base):
    """Per-day, per-feature usage and error counts. Safe to recompute (idempotent per day)."""

    __tablename__ = "analytics_feature_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    feature: Mapped[str] = mapped_column(Text, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_internal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accounts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accounts_internal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ventures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    screen_views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    control_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chat_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_internal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class AnalyticsErrorDaily(Base):
    """Per-day error counts by feature, source, code and route template."""

    __tablename__ = "analytics_error_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    feature: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    error_code: Mapped[str] = mapped_column(Text, primary_key=True)
    route: Mapped[str] = mapped_column(Text, primary_key=True, default="")
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    count_internal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accounts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class PlanSubscription(Base):
    """Platform subscription, billed per workspace with a seat cap (doc 112)."""

    __tablename__ = "plan_subscriptions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    person_id: Mapped[str | None] = mapped_column(Text, index=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    tier: Mapped[str] = mapped_column(Text, nullable=False, default="free")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    seats: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_period_end: Mapped[datetime | None] = mapped_column()
    trial_ends_at: Mapped[datetime | None] = mapped_column()
    provider: Mapped[str | None] = mapped_column(Text)
    provider_customer_id: Mapped[str | None] = mapped_column(Text)
    provider_subscription_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint("seats >= 1", name="ck_plan_subscriptions_seats"),
        CheckConstraint(
            "tier IN ('free', 'pro', 'team')",
            name="ck_plan_subscriptions_tier",
        ),
        CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'canceled', 'expired')",
            name="ck_plan_subscriptions_status",
        ),
        Index("ix_plan_subscriptions_workspace", "workspace_id"),
    )


class PresenceHeartbeat(Base):
    """Who is currently looking at a workspace. Soft-state; last write wins."""

    __tablename__ = "presence_heartbeats"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    person_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    surface: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "person_id", name="uq_presence_workspace_person"),
        Index("ix_presence_workspace_seen", "workspace_id", "last_seen_at"),
    )


class ConsumerUser(Base):
    """Legacy Cognito account record retained for explicit identity reconciliation."""

    __tablename__ = "consumer_users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    cognito_sub: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    person_id: Mapped[str | None] = mapped_column(Text)
    credit_account_id: Mapped[str | None] = mapped_column(Text)
    plan: Mapped[str] = mapped_column(Text, nullable=False, default="trial")
    plan_started_at: Mapped[datetime] = mapped_column(nullable=False)
    plan_expires_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint("plan IN ('trial', 'paid')", name="ck_consumer_users_plan"),
        CheckConstraint(
            "status IN ('active', 'suspended')", name="ck_consumer_users_status"
        ),
    )


class ConsumerOpenToken(Base):
    """One-time desktop open token. Hash only — the raw token never stays in the DB."""

    __tablename__ = "consumer_open_tokens"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class ConsumerUsageEvent(Base):
    """Per-user hosted usage. Regular users may only read their own rows."""

    __tablename__ = "consumer_usage_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    route: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credits_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class DesktopBugReport(Base):
    """Crash + Settings bug reports. Founder-only list; status is mutable."""

    __tablename__ = "desktop_bug_reports"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new", index=True)
    app_version: Mapped[str] = mapped_column(Text, nullable=False)
    os_name: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    received_at: Mapped[datetime] = mapped_column(nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    stack_text: Mapped[str | None] = mapped_column(Text)
    logs_text: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(Text)
    anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    install_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('crash', 'user')", name="ck_desktop_bug_reports_kind"),
        CheckConstraint(
            "status IN ('new', 'ack', 'closed')", name="ck_desktop_bug_reports_status"
        ),
    )


# ---------------------------------------------------------------------------
# GTM Relationship Engine (docs 115–117) — Loop 1 CRM
# ---------------------------------------------------------------------------


class GtmCompany(Base):
    __tablename__ = "gtm_companies"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(Text)
    yc_batch: Mapped[str | None] = mapped_column(Text)
    accelerator: Mapped[str | None] = mapped_column(Text)
    stage_signal: Mapped[str | None] = mapped_column(Text)
    funding_usd: Mapped[float | None] = mapped_column(Float)
    hq_country: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    source_urls_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    company_type: Mapped[str | None] = mapped_column(Text)
    status_quo_stack_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    wedge_fit: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "domain", name="uq_gtm_company_domain"),
        Index("ix_gtm_companies_ws_name", "workspace_id", "canonical_name"),
    )


class GtmContact(Base):
    __tablename__ = "gtm_contacts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    company_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("gtm_companies.id")
    )
    ops_prospect_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("ops_prospects.id")
    )
    email: Mapped[str | None] = mapped_column(Text)
    email_confidence: Mapped[str] = mapped_column(
        Text, nullable=False, default="none"
    )
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)
    lawful_basis: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    lawful_basis_note: Mapped[str | None] = mapped_column(Text)
    lawful_basis_at: Mapped[datetime | None] = mapped_column()
    relationship_stage: Mapped[str] = mapped_column(
        Text, nullable=False, default="new"
    )
    stage_changed_at: Mapped[datetime] = mapped_column(nullable=False)
    owner_mailbox_id: Mapped[str | None] = mapped_column(Text)
    unsubscribed_at: Mapped[datetime | None] = mapped_column()
    do_not_contact: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str | None] = mapped_column(Text)
    dossier_hook: Mapped[str | None] = mapped_column(Text)
    dossier_json: Mapped[str | None] = mapped_column(Text)
    secondary_email: Mapped[str | None] = mapped_column(Text)
    x_handle: Mapped[str | None] = mapped_column(Text)
    enrichment_json: Mapped[str | None] = mapped_column(Text)
    sequence_state_json: Mapped[str | None] = mapped_column(Text)
    gmail_thread_id: Mapped[str | None] = mapped_column(Text)
    last_emailed_at: Mapped[datetime | None] = mapped_column()
    times_emailed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replied_at: Mapped[datetime | None] = mapped_column()
    preferred_channel: Mapped[str] = mapped_column(Text, nullable=False, default="auto")
    sequence_kind: Mapped[str] = mapped_column(Text, nullable=False, default="cold")
    enrichment_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    linkedin_stage: Mapped[str | None] = mapped_column(Text)
    times_linkedin: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_linkedin_at: Mapped[datetime | None] = mapped_column()
    bounced_at: Mapped[datetime | None] = mapped_column()
    icp_score: Mapped[float | None] = mapped_column(Float)
    icp_tier: Mapped[str | None] = mapped_column(Text)
    outreach_pattern: Mapped[str | None] = mapped_column(Text)
    ask_rung: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    ask_rung_changed_at: Mapped[datetime | None] = mapped_column()
    score_void: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score_sum: Mapped[int | None] = mapped_column(Integer)
    score_band: Mapped[str | None] = mapped_column(Text)
    score_factors_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    score_explain: Mapped[str | None] = mapped_column(Text)
    why_this_week: Mapped[str | None] = mapped_column(Text)
    why_this_week_confirmed_at: Mapped[datetime | None] = mapped_column()
    score_override: Mapped[str | None] = mapped_column(Text)
    score_override_reason: Mapped[str | None] = mapped_column(Text)
    exact_language_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    recipient_geo: Mapped[str | None] = mapped_column(Text)
    contact_provenance: Mapped[str | None] = mapped_column(Text)
    mutual_count: Mapped[int | None] = mapped_column(Integer)
    buyer_tag: Mapped[str | None] = mapped_column(Text)
    recipient_seniority: Mapped[str | None] = mapped_column(Text)
    layer: Mapped[str] = mapped_column(Text, nullable=False, default="B")
    personalize_mode: Mapped[str | None] = mapped_column(Text)
    sku_key: Mapped[str | None] = mapped_column(Text)
    drop_reason: Mapped[str | None] = mapped_column(Text)
    cadence_week: Mapped[int | None] = mapped_column(Integer)
    next_action_at: Mapped[datetime | None] = mapped_column()
    next_action_kind: Mapped[str | None] = mapped_column(Text)
    i_actually_know_them: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "email", name="uq_gtm_contact_email"),
        Index("ix_gtm_contacts_stage", "workspace_id", "relationship_stage"),
        Index("ix_gtm_contacts_linkedin", "workspace_id", "linkedin_url"),
        CheckConstraint(
            "email_confidence IN ('safe', 'risky', 'guess', 'none')",
            name="ck_gtm_contact_email_confidence",
        ),
        CheckConstraint(
            "lawful_basis IN ('legitimate_interest', 'consent', 'none')",
            name="ck_gtm_contact_lawful_basis",
        ),
    )


class GtmSignal(Base):
    __tablename__ = "gtm_signals"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    company_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("gtm_companies.id")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    icp_score: Mapped[float | None] = mapped_column(Float)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    contact_id: Mapped[str | None] = mapped_column(Text, ForeignKey("gtm_contacts.id"))
    source: Mapped[str | None] = mapped_column(Text)
    trigger_code: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    layer: Mapped[str] = mapped_column(Text, nullable=False, default="A")
    extracted_quote: Mapped[str | None] = mapped_column(Text)
    timing_observed_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "dedup_key", name="uq_gtm_signal_dedup"),
        Index("ix_gtm_signals_kind", "workspace_id", "kind", "observed_at"),
    )


class GtmMailbox(Base):
    __tablename__ = "gtm_mailboxes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    sending_domain: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False, default="smtp")
    smtp_host: Mapped[str | None] = mapped_column(Text)
    smtp_port: Mapped[int | None] = mapped_column(Integer)
    imap_host: Mapped[str | None] = mapped_column(Text)
    imap_port: Mapped[int | None] = mapped_column(Integer)
    secret_ref: Mapped[str | None] = mapped_column(Text)
    warmup_started_at: Mapped[datetime | None] = mapped_column()
    warmup_day_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    hard_daily_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    sent_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bounces_7d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    complaints_7d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    health: Mapped[str] = mapped_column(Text, nullable=False, default="warming")
    last_imap_uid: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "address", name="uq_gtm_mailbox_address"),
        CheckConstraint(
            "health IN ('warming', 'healthy', 'paused', 'burnt')",
            name="ck_gtm_mailbox_health",
        ),
        CheckConstraint(
            "provider IN ('gworkspace', 'm365', 'smtp')",
            name="ck_gtm_mailbox_provider",
        ),
    )


class GtmSendingDomain(Base):
    __tablename__ = "gtm_sending_domains"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    spf_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dkim_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dmarc_policy: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column()
    first_send_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "domain", name="uq_gtm_sending_domain"),
    )


class GtmSequence(Base):
    __tablename__ = "gtm_sequences"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    track: Mapped[str] = mapped_column(Text, nullable=False, default="relationship")
    steps_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    armed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    armed_by: Mapped[str | None] = mapped_column(Text)
    armed_at: Mapped[datetime | None] = mapped_column()
    approval_id: Mapped[str | None] = mapped_column(Text)
    hypothesis: Mapped[str | None] = mapped_column(Text)
    metric: Mapped[str | None] = mapped_column(Text)
    threshold: Mapped[float | None] = mapped_column(Float)
    window_days: Mapped[int | None] = mapped_column(Integer)
    product_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class GtmEnrollment(Base):
    __tablename__ = "gtm_enrollments"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    sequence_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_sequences.id"), nullable=False
    )
    step_idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_at: Mapped[datetime | None] = mapped_column()
    mailbox_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("gtm_mailboxes.id")
    )
    thread_root_message_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    cooldown_until: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_enrollments_next", "status", "next_at"),
        CheckConstraint(
            "status IN ('active', 'paused', 'replied', 'converted', "
            "'cooled', 'unsubscribed', 'bounced')",
            name="ck_gtm_enrollment_status",
        ),
    )


class GtmTouch(Base):
    __tablename__ = "gtm_touches"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    venture_id: Mapped[str | None] = mapped_column(Text, index=True)
    provider_thread_id: Mapped[str | None] = mapped_column(Text)
    email_account_id: Mapped[str | None] = mapped_column(Text)
    enrollment_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("gtm_enrollments.id")
    )
    contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    mailbox_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("gtm_mailboxes.id")
    )
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False, default="email")
    message_id: Mapped[str | None] = mapped_column(Text)
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column()
    classification: Mapped[str | None] = mapped_column(Text)
    approval_id: Mapped[str | None] = mapped_column(Text)
    ops_email_log_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("ops_email_logs.id")
    )
    simulated: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ask_rung: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    send_mode: Mapped[str | None] = mapped_column(Text)
    honesty_json: Mapped[str | None] = mapped_column(Text)
    sku_key: Mapped[str | None] = mapped_column(Text)
    copied_at: Mapped[datetime | None] = mapped_column()
    logged_by: Mapped[str | None] = mapped_column(Text)
    numerator_event: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("uq_gtm_touch_venture_reply", "workspace_id", "venture_id", "contact_id", "message_id", unique=True,
              postgresql_where=text("direction = 'in' AND venture_id IS NOT NULL AND message_id IS NOT NULL AND email_account_id IS NULL"),
              sqlite_where=text("direction = 'in' AND venture_id IS NOT NULL AND message_id IS NOT NULL AND email_account_id IS NULL")),
        Index("uq_gtm_touch_mailbox_reply", "workspace_id", "venture_id", "email_account_id", "contact_id", "message_id", unique=True,
              postgresql_where=text("direction = 'in' AND venture_id IS NOT NULL AND message_id IS NOT NULL AND email_account_id IS NOT NULL"),
              sqlite_where=text("direction = 'in' AND venture_id IS NOT NULL AND message_id IS NOT NULL AND email_account_id IS NOT NULL")),
        CheckConstraint("direction IN ('out', 'in')", name="ck_gtm_touch_direction"),
        Index("ix_gtm_touches_contact", "contact_id", "sent_at"),
    )


class GtmEmailAccount(Base):
    """Composio-connected Gmail mailbox used by the AI GTM sequence worker."""

    __tablename__ = "gtm_email_accounts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    app_user_id: Mapped[str] = mapped_column(Text, nullable=False, default="local-user")
    provider: Mapped[str] = mapped_column(Text, nullable=False, default="gmail")
    email: Mapped[str | None] = mapped_column(Text)
    composio_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    connected_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    daily_send_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    sent_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count_date: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("connected_account_id", name="uq_gtm_email_account_ca"),
        Index("ix_gtm_email_accounts_ws", "workspace_id", "status"),
    )


class GtmEmailEvent(Base):
    __tablename__ = "gtm_email_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    contact_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("gtm_contacts.id")
    )
    email_account_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("gtm_email_accounts.id")
    )
    sequence_day: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_email_events_contact", "contact_id", "created_at"),
    )


class GtmEmailSendQueue(Base):
    __tablename__ = "gtm_email_send_queue"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    email_account_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_email_accounts.id"), nullable=False
    )
    sequence_day: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    scheduled_for: Mapped[datetime] = mapped_column(nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_gtm_send_idempotency"),
        UniqueConstraint("contact_id", "sequence_day", name="uq_gtm_send_contact_day"),
        Index("ix_gtm_send_queue_due", "status", "scheduled_for"),
        CheckConstraint(
            "sequence_day IN (1, 2, 4, 7)",
            name="ck_gtm_send_sequence_day",
        ),
    )


class GtmEngineState(Base):
    """Per-workspace Run/Stop and volume ramp for the AI GTM department."""

    __tablename__ = "gtm_engine_state"

    workspace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    discovery_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    send_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    icp_query: Mapped[str] = mapped_column(Text, nullable=False, default="B2B SaaS founders")
    find_batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    ready_email_buffer: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    ready_linkedin_buffer: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    week_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    outreach_week: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    outbound_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="email_department"
    )
    last_discovery_at: Mapped[datetime | None] = mapped_column()
    last_learn_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class GtmResearchJob(Base):
    __tablename__ = "gtm_research_jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="enrich")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    scheduled_for: Mapped[datetime] = mapped_column(nullable=False)
    sequence_day: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_research_due", "status", "scheduled_for"),
    )


class GtmResearchSnapshot(Base):
    __tablename__ = "gtm_research_snapshots"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    sequence_day: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    richness: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_snapshot_contact", "contact_id", "created_at"),
    )


class GtmLinkedInQueue(Base):
    """Friendship-thread steps. Send via Unipile if configured; else human queue."""

    __tablename__ = "gtm_linkedin_queue"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    step: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    scheduled_for: Mapped[datetime] = mapped_column(nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    copied_at: Mapped[datetime | None] = mapped_column()
    sent_at: Mapped[datetime | None] = mapped_column()
    honesty_json: Mapped[str | None] = mapped_column(Text)
    send_mode: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("contact_id", "step", name="uq_gtm_li_contact_step"),
        Index("ix_gtm_li_due", "status", "scheduled_for"),
    )


class GtmLearningEvent(Base):
    __tablename__ = "gtm_learning_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    contact_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class GtmMarketProfile(Base):
    """Versioned description of the seller, product and target market."""

    __tablename__ = "gtm_market_profiles"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    seller_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    product_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    segment_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    constraints_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "version", name="uq_gtm_market_profile_version"
        ),
        Index("ix_gtm_market_profiles_status", "workspace_id", "status"),
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_gtm_market_profile_status",
        ),
    )


class GtmOutreachSettings(Base):
    """Per-workspace First Five Minutes + send rails (OT-11 / OT-26)."""

    __tablename__ = "gtm_outreach_settings"

    workspace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    onboarding_status: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    outbound_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="human_clipboard"
    )
    sender_geo: Mapped[str | None] = mapped_column(Text)
    sender_postal_address: Mapped[str | None] = mapped_column(Text)
    composio_user_id: Mapped[str | None] = mapped_column(Text)
    aup_accepted_at: Mapped[datetime | None] = mapped_column()
    dpa_accepted_at: Mapped[datetime | None] = mapped_column()
    max_connect_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_dm_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    max_email_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    send_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class GtmSuppression(Base):
    __tablename__ = "gtm_suppressions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(Text)
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_suppressions_email", "workspace_id", "email"),
        Index("ix_gtm_suppressions_li", "workspace_id", "linkedin_url"),
        CheckConstraint(
            "source IN ('opt_out', 'unknown_person', 'burned', 'manual')",
            name="ck_gtm_suppression_source",
        ),
    )


class GtmSendPolicy(Base):
    __tablename__ = "gtm_send_policies"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    geo: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    rule_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_send_policies_geo", "workspace_id", "geo", "channel"),
    )


class GtmArtifactSku(Base):
    __tablename__ = "gtm_artifact_skus"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    sku_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    body_md: Mapped[str | None] = mapped_column(Text)
    match_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("workspace_id", "sku_key", name="uq_gtm_sku_key"),
        CheckConstraint(
            "kind IN ('benchmark', 'teardown', 'quote_card', 'calculator')",
            name="ck_gtm_sku_kind",
        ),
        CheckConstraint(
            "status IN ('draft', 'in_review', 'approved', 'retired')",
            name="ck_gtm_sku_status",
        ),
    )


class GtmEvidenceSource(Base):
    """Tenant-asserted Slack/GitHub/Gmail evidence path (OT-39). Never invented graph data."""

    __tablename__ = "gtm_evidence_sources"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="deferred")
    connected_account_id: Mapped[str | None] = mapped_column(Text)
    deferred_at: Mapped[datetime | None] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column()
    connected_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", name="uq_gtm_evidence_provider"),
        CheckConstraint(
            "provider IN ('slack', 'github', 'gmail')",
            name="ck_gtm_evidence_provider",
        ),
        CheckConstraint(
            "status IN ('deferred', 'started', 'connected')",
            name="ck_gtm_evidence_status",
        ),
    )


class GtmNetworkEdge(Base):
    """First-party warm graph, in-tenant only (OT-40)."""

    __tablename__ = "gtm_network_edges"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    from_contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    to_contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_network_edges_ws", "workspace_id", "from_contact_id"),
        CheckConstraint(
            "source IN ('tenant_gmail', 'tenant_slack', 'tenant_github', "
            "'tenant_paste', 'tenant_asserted')",
            name="ck_gtm_network_edge_source",
        ),
    )


class GtmInterview(Base):
    """Meeting notes + Mom-Test flags (OT-42)."""

    __tablename__ = "gtm_interviews"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    company_id: Mapped[str | None] = mapped_column(Text, ForeignKey("gtm_companies.id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="first_meeting")
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    script_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    notes_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    extraction_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    mom_test_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_step: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_interviews_contact", "workspace_id", "contact_id"),
        CheckConstraint(
            "kind IN ('first_meeting', 'followup', 'roundtable', 'async_note')",
            name="ck_gtm_interview_kind",
        ),
    )


class GtmPilot(Base):
    """Narrow paid-conversion tracker (OT-46)."""

    __tablename__ = "gtm_pilots"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    company_id: Mapped[str | None] = mapped_column(Text, ForeignKey("gtm_companies.id"))
    contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    use_case: Mapped[str] = mapped_column(Text, nullable=False)
    team: Mapped[str | None] = mapped_column(Text)
    baseline_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    success_criterion: Mapped[str] = mapped_column(Text, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    started_at: Mapped[datetime | None] = mapped_column()
    ends_at: Mapped[datetime | None] = mapped_column()
    paid_decision_at: Mapped[datetime | None] = mapped_column()
    permission_to_demo_at: Mapped[datetime | None] = mapped_column()
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_pilots_ws", "workspace_id", "status"),
        CheckConstraint(
            "status IN ('proposed', 'running', 'succeeded', 'failed', "
            "'converted', 'dropped')",
            name="ck_gtm_pilot_status",
        ),
    )


class GtmPublicDocument(Base):
    """Layer A public documents only — no Layer B emails (OT-48)."""

    __tablename__ = "gtm_public_documents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    evidence_url: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    body_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_public_docs_kind", "workspace_id", "kind"),
        CheckConstraint(
            "kind IN ('job', 'hn', 'issue', 'other')",
            name="ck_gtm_public_doc_kind",
        ),
    )


class GtmPasteBody(Base):
    """Mutable paste PII (OT-47). Signals are append-only so raw text lives here."""

    __tablename__ = "gtm_paste_bodies"

    signal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    raw_text: Mapped[str | None] = mapped_column(Text)
    raw_text_sha256: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    nulled_at: Mapped[datetime | None] = mapped_column()


class GtmGodPlan(Base):
    """AI God GTM plan: diagnosis, Now/Next/Later, shared artifacts."""

    __tablename__ = "gtm_god_plans"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    venture_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="diagnosing")
    idea: Mapped[str] = mapped_column(Text, nullable=False, default="")
    problem: Mapped[str] = mapped_column(Text, nullable=False, default="")
    brief_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    diagnosis_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    log_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_god_plans_ws", "workspace_id", "status"),
        CheckConstraint(
            "status IN ('diagnosing', 'executing', 'waiting_human', 'paused', 'done')",
            name="ck_gtm_god_plan_status",
        ),
    )


class GtmGodTask(Base):
    """Deterministic work item owned by a specialist agent."""

    __tablename__ = "gtm_god_tasks"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    owner_agent: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    horizon: Mapped[str] = mapped_column(Text, nullable=False, default="now")
    depends_on_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    input_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    human_instruction: Mapped[str | None] = mapped_column(Text)
    evidence_tag: Mapped[str] = mapped_column(Text, nullable=False, default="assumption")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_god_tasks_plan", "plan_id", "status"),
        CheckConstraint(
            "status IN ('queued', 'running', 'blocked_human', 'done', 'skipped', 'failed')",
            name="ck_gtm_god_task_status",
        ),
        CheckConstraint(
            "horizon IN ('now', 'next', 'later')",
            name="ck_gtm_god_task_horizon",
        ),
        CheckConstraint(
            "evidence_tag IN ('assumption', 'validated')",
            name="ck_gtm_god_task_evidence",
        ),
    )


class GtmGodMessage(Base):
    """Inter-agent collaboration bus for a plan."""

    __tablename__ = "gtm_god_messages"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(Text)
    from_agent: Mapped[str] = mapped_column(Text, nullable=False)
    to_agent: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class GtmThesis(Base):
    """Immutable strategy proposal. Approval changes status, never its content."""

    __tablename__ = "gtm_theses"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    profile_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_market_profiles.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    archetype_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    buying_profile_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    strategy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    assumptions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    model: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "profile_id", "version", name="uq_gtm_thesis_profile_version"
        ),
        Index("ix_gtm_theses_status", "workspace_id", "status"),
        CheckConstraint(
            "status IN ('draft', 'approved', 'rejected', 'superseded')",
            name="ck_gtm_thesis_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_gtm_thesis_confidence",
        ),
    )


class GtmCampaignExperiment(Base):
    """A falsifiable experiment compiled from an approved thesis."""

    __tablename__ = "gtm_campaign_experiments"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    thesis_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_theses.id"), nullable=False
    )
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    primary_metric: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    minimum_sample_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    variants_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allocation_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    guardrails_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="planned")
    started_at: Mapped[datetime | None] = mapped_column()
    stopped_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_gtm_experiments_status", "workspace_id", "status"),
        CheckConstraint(
            "status IN ('planned', 'running', 'paused', 'completed', 'cancelled')",
            name="ck_gtm_experiment_status",
        ),
    )


class GtmOutcome(Base):
    """Append-only commercial and trust outcome used by thesis learning."""

    __tablename__ = "gtm_outcomes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    thesis_id: Mapped[str | None] = mapped_column(Text, ForeignKey("gtm_theses.id"))
    experiment_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("gtm_campaign_experiments.id")
    )
    contact_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str | None] = mapped_column(Text)
    variant: Mapped[str | None] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index(
            "ix_gtm_outcomes_experiment",
            "workspace_id",
            "experiment_id",
            "occurred_at",
        ),
    )


class GtmBuyingGroup(Base):
    """Account-level set of stakeholders for one product/use case."""

    __tablename__ = "gtm_buying_groups"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_companies.id"), nullable=False
    )
    product_key: Mapped[str] = mapped_column(Text, nullable=False, default="default")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="forming")
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    engagement_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "company_id",
            "product_key",
            name="uq_gtm_buying_group_product",
        ),
    )


class GtmBuyingGroupMember(Base):
    __tablename__ = "gtm_buying_group_members"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    buying_group_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_buying_groups.id"), nullable=False
    )
    contact_id: Mapped[str] = mapped_column(
        Text, ForeignKey("gtm_contacts.id"), nullable=False
    )
    role_type: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "buying_group_id", "contact_id", name="uq_gtm_buying_group_member"
        ),
    )


class GtmAttributionLink(Base):
    __tablename__ = "gtm_attribution_links"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    contact_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("gtm_contacts.id")
    )
    campaign_id: Mapped[str | None] = mapped_column(Text)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_click_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)


# Tables that must remain append-only (no UPDATE/DELETE API paths).
APPEND_ONLY_TABLES: frozenset[str] = frozenset(
    {
        "event_log",
        "activity_lines",
        "connection_events",
        "boundary_ledger",
        "ops_activity",
        "credit_ledger",
        "admin_audit_log",
        "analytics_events",
        "gtm_touches",
        "gtm_signals",
        "gtm_email_events",
        "gtm_outcomes",
    }
)

# Phase 10 tables (docs 35–47), kept separate for readability.
PHASE10_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "people",
        "workspaces",
        "workspace_members",
        "invites",
        "boundary_ledger",
        "engagements",
        "opportunity_slates",
        "opportunities",
        "site_versions",
        "interest_signals",
        "connections",
        "founder_llm_settings",
        "connection_events",
        "company_maps",
        "workflows",
        "workflow_runs",
        "approvals",
        "api_tokens",
        "idempotency_records",
        "webhook_subscriptions",
        "webhook_deliveries",
        "suspicious_content_events",
    }
)

# Credits and admin control (doc 99).
CREDITS_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "credit_accounts",
        "credit_ledger",
        "credit_reservations",
        "credit_policies",
        "credit_grant_approvals",
        "admin_users",
        "admin_sessions",
        "admin_audit_log",
        "payment_events",
        "credit_orders",
        "person_identities",
        "consumer_users",
        "consumer_open_tokens",
        "consumer_usage_events",
        "desktop_bug_reports",
    }
)

# Desktop monetization / analytics / multi-user (doc 112).
PLAN112_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "analytics_events",
        "analytics_daily_rollup",
        "plan_subscriptions",
        "presence_heartbeats",
    }
)

# Polsia ops supersets (doc 49).
OPS_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "ops_company_config",
        "ops_tasks",
        "ops_agent_runs",
        "ops_activity",
        "ops_memory",
        "ops_social_posts",
        "ops_social_engagements",
        "ops_prospects",
        "ops_email_campaigns",
        "ops_email_logs",
        "ops_ad_campaigns",
        "ops_ad_metrics",
        "ops_competitors",
        "ops_stripe_events",
        "ops_revenue_snapshots",
        "ops_expense_records",
        "ops_daily_reports",
        "ops_deployments",
        "money_ledger_entries",
        "venture_revenue_snapshots",
        "agent_live_status",
    }
)

# ---------------------------------------------------------------------------
# Content engine (doc 119) — voice-consistent, ICP-targeted multi-channel posting
# ---------------------------------------------------------------------------


class ContentAccount(Base):
    """A connected publishing identity. Tokens live in the vault, refs live here."""

    __tablename__ = "content_accounts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_account: Mapped[str] = mapped_column(Text, nullable=False, default="")
    handle: Mapped[str | None] = mapped_column(Text)
    author_urn: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    auth_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    access_secret_ref: Mapped[str | None] = mapped_column(Text)
    refresh_secret_ref: Mapped[str | None] = mapped_column(Text)
    scopes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    extra_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    expires_at: Mapped[datetime | None] = mapped_column()
    daily_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    posts_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    day_key: Mapped[str | None] = mapped_column(Text)
    last_post_at: Mapped[datetime | None] = mapped_column()
    health: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    paused_reason: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "workspace_id", "platform", name="uq_content_account_publication_scope"),
        CheckConstraint("auth_generation >= 1", name="ck_content_account_auth_generation"),
        UniqueConstraint(
            "workspace_id",
            "platform",
            "external_account",
            name="uq_content_account_identity",
        ),
        Index("ix_content_accounts_platform", "workspace_id", "platform", "status"),
        CheckConstraint(
            "status IN ('pending', 'active', 'expired', 'revoked', 'error', 'needs_reconnect')",
            name="ck_content_account_status",
        ),
    )


class ContentOAuthState(Base):
    """Short-lived CSRF + PKCE state for an in-flight authorization."""

    __tablename__ = "content_oauth_states"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    # id stores the SHA-256 digest of the one-time callback nonce.
    actor_subject: Mapped[str | None] = mapped_column(Text)
    auth_method: Mapped[str] = mapped_column(Text, nullable=False, default="native", server_default="native")
    provider_user_id: Mapped[str | None] = mapped_column(Text)
    auth_config_id: Mapped[str | None] = mapped_column(Text)
    connected_account_id: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    gmail_intent: Mapped[str | None] = mapped_column(Text)
    gmail_mailbox_id: Mapped[str | None] = mapped_column(Text)
    # Encrypted with the shared connector vault key; cleared on consume.
    code_verifier: Mapped[str | None] = mapped_column(Text)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    return_to: Mapped[str | None] = mapped_column(Text)
    consumed_at: Mapped[datetime | None] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class ContentBrief(Base):
    """The uploaded JSON: product, ICP, competitors. Versioned, never edited."""

    __tablename__ = "content_briefs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    venture_id: Mapped[str | None] = mapped_column(Text, ForeignKey("ventures.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    source_name: Mapped[str | None] = mapped_column(Text)
    product_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    icp_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    competitors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    voice_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    claims_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    cadence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "venture_id", "version", name="uq_content_brief_version"),
        Index("ix_content_briefs_status", "workspace_id", "venture_id", "status"),
        CheckConstraint(
            "status IN ('active', 'archived')", name="ck_content_brief_status"
        ),
    )


class GtmGodRun(Base):
    """One approved venture-scoped GTM diagnosis, with durable receipts."""
    __tablename__ = "gtm_god_generation_runs"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, ForeignKey("workspaces.id"), nullable=False)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[str] = mapped_column(Text, ForeignKey("gtm_god_plans.id"), nullable=False, unique=True)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False, unique=True)
    idempotency_hash: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)
    ceiling_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", "venture_id", name="uq_gtm_god_generation_scope_identity"),
        UniqueConstraint("workspace_id", "venture_id", "actor_id", "idempotency_hash", name="uq_gtm_god_generation_intent"),
        Index("ix_gtm_god_generation_scope", "workspace_id", "venture_id", "created_at"),
        CheckConstraint("kind IN ('diagnose')", name="ck_gtm_god_generation_kind"),
        CheckConstraint("state IN ('queued', 'running', 'completed', 'needs_input', 'failed', 'cancelled')", name="ck_gtm_god_generation_state"),
        CheckConstraint("ceiling_micro >= 0", name="ck_gtm_god_generation_ceiling"),
    )


class ContentGenerationRun(Base):
    """One company-scoped authoring intent, immutable inputs and private receipts."""
    __tablename__ = "content_generation_runs"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, ForeignKey("workspaces.id"), nullable=False)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    brief_id: Mapped[str] = mapped_column(Text, ForeignKey("content_briefs.id"), nullable=False)
    topic_id: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False, unique=True)
    idempotency_hash: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)
    ceiling_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", "venture_id", name="uq_content_generation_publication_scope"),
        UniqueConstraint("workspace_id", "venture_id", "actor_id", "idempotency_hash", name="uq_content_generation_intent"),
        Index("ix_content_generation_scope", "workspace_id", "venture_id", "created_at"),
        CheckConstraint("kind IN ('topics', 'draft')", name="ck_content_generation_kind"),
        CheckConstraint("state IN ('queued', 'running', 'completed', 'refused', 'needs_input', 'failed', 'cancelled')", name="ck_content_generation_state"),
        CheckConstraint("ceiling_micro >= 0", name="ck_content_generation_ceiling"),
    )


class ContentTopic(Base):
    """One angle the director wants to say, derived from the brief and the ICP."""

    __tablename__ = "content_topics"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    generation_run_id: Mapped[str | None] = mapped_column(Text, ForeignKey("content_generation_runs.id"), index=True)
    brief_id: Mapped[str] = mapped_column(
        Text, ForeignKey("content_briefs.id"), nullable=False
    )
    angle: Mapped[str] = mapped_column(Text, nullable=False)
    hook: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    icp_segment: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(Text, nullable=False, default="value")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_content_topics_status", "workspace_id", "status", "score"),
        CheckConstraint(
            "status IN ('queued', 'drafted', 'published', 'spent', 'dead')",
            name="ck_content_topic_status",
        ),
        CheckConstraint(
            "intent IN ('value', 'promo')", name="ck_content_topic_intent"
        ),
    )


class ContentPost(Base):
    """One platform-specific rendering of a topic, with its full review trail."""

    __tablename__ = "content_posts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    generation_run_id: Mapped[str | None] = mapped_column(Text, ForeignKey("content_generation_runs.id"), index=True)
    topic_id: Mapped[str] = mapped_column(
        Text, ForeignKey("content_topics.id"), nullable=False
    )
    account_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("content_accounts.id")
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False, default="value")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    title: Mapped[str | None] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    simhash: Mapped[str | None] = mapped_column(Text)
    lint_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    critic_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    refusal_reason: Mapped[str | None] = mapped_column(Text)
    scheduled_for: Mapped[datetime | None] = mapped_column()
    published_at: Mapped[datetime | None] = mapped_column()
    external_id: Mapped[str | None] = mapped_column(Text)
    external_url: Mapped[str | None] = mapped_column(Text)
    visual_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    visual_path: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "workspace_id", "generation_run_id", name="uq_content_post_publication_scope"),
        Index("ix_content_posts_state", "workspace_id", "state", "scheduled_for"),
        Index("ix_content_posts_platform", "workspace_id", "platform", "published_at"),
        CheckConstraint(
            "state IN ('draft', 'refused', 'approved', 'scheduled', "
            "'published', 'failed', 'removed')",
            name="ck_content_post_state",
        ),
    )


class ContentPublicationRun(Base):
    """One company-owned immutable external intent; approval/Job stay canonical."""

    __tablename__ = "content_publication_runs"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, ForeignKey("workspaces.id"), nullable=False)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    post_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    review_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_subject: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    credit_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    approval_id: Mapped[str | None] = mapped_column(Text, ForeignKey("approvals.id"), unique=True)
    job_id: Mapped[str | None] = mapped_column(Text, ForeignKey("jobs.id"), unique=True)
    idempotency_hash: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False)
    revision_digest: Mapped[str] = mapped_column(Text, nullable=False)
    target_digest: Mapped[str] = mapped_column(Text, nullable=False)
    account_identity_digest: Mapped[str] = mapped_column(Text, nullable=False)
    price_digest: Mapped[str] = mapped_column(Text, nullable=False)
    ceiling_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        ForeignKeyConstraint(["source_run_id", "workspace_id", "venture_id"], ["content_generation_runs.id", "content_generation_runs.workspace_id", "content_generation_runs.venture_id"], name="fk_publication_generation_scope"),
        ForeignKeyConstraint(["review_run_id", "workspace_id", "venture_id"], ["content_generation_runs.id", "content_generation_runs.workspace_id", "content_generation_runs.venture_id"], name="fk_publication_review_scope"),
        ForeignKeyConstraint(["post_id", "workspace_id", "source_run_id"], ["content_posts.id", "content_posts.workspace_id", "content_posts.generation_run_id"], name="fk_publication_post_scope"),
        ForeignKeyConstraint(["account_id", "workspace_id", "platform"], ["content_accounts.id", "content_accounts.workspace_id", "content_accounts.platform"], name="fk_publication_account_scope"),
        ForeignKeyConstraint(["credit_account_id", "workspace_id", "venture_id"], ["credit_accounts.id", "credit_accounts.workspace_id", "credit_accounts.venture_id"], name="fk_publication_credit_scope"),
        UniqueConstraint("workspace_id", "venture_id", "actor_subject", "idempotency_hash", name="uq_content_publication_intent"),
        Index("ix_content_publication_scope", "workspace_id", "venture_id", "created_at"),
        Index("uq_content_publication_unresolved", "post_id", unique=True,
            postgresql_where=text("state IN ('waiting_review','waiting_approval','approved','queued','running','receipt_saved','blocked','needs_reconciliation')"),
            sqlite_where=text("state IN ('waiting_review','waiting_approval','approved','queued','running','receipt_saved','blocked','needs_reconciliation')")),
        CheckConstraint("state IN ('waiting_review','waiting_approval','approved','queued','running','receipt_saved','completed','rejected','stale','blocked','failed_definitive','needs_reconciliation','cancelled')", name="ck_content_publication_state"),
        CheckConstraint("ceiling_micro >= 0", name="ck_content_publication_ceiling"),
    )


class ContentMetric(Base):
    """A metrics snapshot for a published post. Append-only."""

    __tablename__ = "content_metrics"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    post_id: Mapped[str] = mapped_column(
        Text, ForeignKey("content_posts.id"), nullable=False
    )
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reposts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    captured_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_content_metrics_post", "post_id", "captured_at"),)


class ContentEvent(Base):
    """Append-only stream the founder watches instead of approving each post."""

    __tablename__ = "content_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str | None] = mapped_column(Text)
    post_id: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_content_events_kind", "workspace_id", "kind", "created_at"),
    )


CONTENT_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "content_accounts",
        "content_oauth_states",
        "content_briefs",
        "content_topics",
        "content_posts",
        "content_metrics",
        "content_events",
    }
)


class MarketingPlan(Base):
    """Executable GTM/content strategy produced by the marketing strategist.

    The plan is constraints + cadence + themes — not day-by-day improvisation.
    Jobs compiled from this plan are what the marketing execution tick runs.
    """

    __tablename__ = "marketing_plans"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    brief_id: Mapped[str | None] = mapped_column(Text)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    strategy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    constraints_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source: Mapped[str] = mapped_column(Text, nullable=False, default="strategist")
    starts_at: Mapped[datetime] = mapped_column(nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_marketing_plans_workspace", "workspace_id", "status"),
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'completed', 'archived')",
            name="ck_marketing_plan_status",
        ),
    )


class MarketingAdProposal(Base):
    """A user-reviewable campaign blueprint before any provider mutation."""

    __tablename__ = "marketing_ad_proposals"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    idea: Mapped[str] = mapped_column(Text, nullable=False)
    blueprint_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    approval_id: Mapped[str | None] = mapped_column(Text, unique=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_marketing_ad_proposals_queue", "workspace_id", "status", "created_at"),
        CheckConstraint(
            "platform IN ('google', 'meta')",
            name="ck_marketing_ad_proposals_platform",
        ),
        CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'created', 'blocked', 'rejected')",
            name="ck_marketing_ad_proposals_status",
        ),
    )


class MarketingAdCampaign(Base):
    """Provider-backed campaign created only from an approved proposal."""

    __tablename__ = "marketing_ad_campaigns"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    proposal_id: Mapped[str] = mapped_column(
        Text, ForeignKey("marketing_ad_proposals.id"), nullable=False, unique=True
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PAUSED")
    daily_budget_usd: Mapped[float] = mapped_column(Float, nullable=False)
    provider_response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_marketing_ad_provider_id"),
        Index("ix_marketing_ad_campaigns_workspace", "workspace_id", "created_at"),
        CheckConstraint(
            "platform IN ('google', 'meta')", name="ck_marketing_ad_campaigns_platform"
        ),
        CheckConstraint(
            "status IN ('PAUSED', 'ACTIVE', 'FAILED', 'REMOVED')",
            name="ck_marketing_ad_campaigns_status",
        ),
    )


class MarketingJob(Base):
    """One atomic unit of work compiled from a marketing plan.

    Status ``blocked`` means a human must intervene (CAPTCHA, login expired,
    unexpected UI). Done requires a verification proof — never trust bare ok.
    """

    __tablename__ = "marketing_jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(
        Text, ForeignKey("marketing_plans.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    depends_on_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    scheduled_for: Mapped[datetime | None] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    executor: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    proof_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    block_reason: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_marketing_jobs_due", "workspace_id", "status", "scheduled_for"),
        Index("ix_marketing_jobs_plan", "plan_id", "status"),
        CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'done', 'failed', "
            "'blocked', 'cancelled')",
            name="ck_marketing_job_status",
        ),
        CheckConstraint(
            "kind IN ("
            "'create_content', 'create_visual', 'publish_content', "
            "'find_threads', 'draft_reply', 'publish_reply', "
            "'research_topic', 'check_result', "
            "'generate_image', 'generate_video', 'operate_app', "
            "'operate_browser', 'create_ad', 'enable_ad', 'ingest_asset', "
            "'enqueue_outreach', 'send_email_touch'"
            ")",
            name="ck_marketing_job_kind",
        ),
    )


class MarketingLearningEvent(Base):
    """Append-only marketing outcomes used to nudge the next plan."""

    __tablename__ = "marketing_learning_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class MarketingAsset(Base):
    """Brand kit + generated media. Logos and founder photos are never regenerated."""

    __tablename__ = "marketing_assets"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="upload")
    title: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False, default="image/png")
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    license_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    review_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_marketing_assets_workspace", "workspace_id", "kind", "created_at"),
        CheckConstraint(
            "kind IN ('logo', 'founder', 'product', 'generated_image', "
            "'generated_video', 'stock', 'other')",
            name="ck_marketing_asset_kind",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ck_marketing_asset_review",
        ),
    )


# ---------------------------------------------------------------------------
# Community Department (Loop 2) — docs 123/124
# ---------------------------------------------------------------------------


class CommunityPlan(Base):
    """Thesis + identity + 30d program artifact (community.plan.v1)."""

    __tablename__ = "community_plans"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    venture_id: Mapped[str | None] = mapped_column(Text)
    research_bundle_id: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str] = mapped_column(
        Text, nullable=False, default="community.plan.v1"
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    verdict: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thesis_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thesis_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    objectives_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    identity_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    program_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    work_orders_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_community_plans_workspace", "workspace_id", "status"),
        CheckConstraint(
            "status IN ('draft', 'founder_review', 'approved', 'executing', "
            "'rescore_due', 'archived')",
            name="ck_community_plan_status",
        ),
    )


class CommunityServer(Base):
    """Discord guild linked via Composio DISCORDBOT (never auto-created)."""

    __tablename__ = "community_servers"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    plan_id: Mapped[str | None] = mapped_column(Text)
    guild_id: Mapped[str] = mapped_column(Text, nullable=False)
    guild_name: Mapped[str | None] = mapped_column(Text)
    connected_account_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    provision_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "guild_id", name="uq_community_server_guild"
        ),
        Index("ix_community_servers_workspace", "workspace_id", "status"),
    )


class CommunityPerson(Base):
    """Minimal member row for Loop 2 → GTM handoff (PQCL later)."""

    __tablename__ = "community_people"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    server_id: Mapped[str | None] = mapped_column(Text)
    discord_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text, nullable=False, default="member")
    gtm_contact_id: Mapped[str | None] = mapped_column(Text)
    meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "discord_user_id",
            name="uq_community_person_discord",
        ),
        Index("ix_community_people_stage", "workspace_id", "stage"),
    )


class CommunityLabLog(Base):
    """Optional durable lab log rows (also mirrored to verify JSONL)."""

    __tablename__ = "community_lab_logs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    level: Mapped[str] = mapped_column(Text, nullable=False, default="info")
    action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False)


COMMUNITY_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "community_plans",
        "community_servers",
        "community_people",
        "community_lab_logs",
    }
)


MARKETING_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "marketing_plans",
        "marketing_jobs",
        "marketing_assets",
    }
)


# GTM Relationship Engine (docs 115–117).
GTM_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "gtm_companies",
        "gtm_contacts",
        "gtm_signals",
        "gtm_mailboxes",
        "gtm_sending_domains",
        "gtm_sequences",
        "gtm_enrollments",
        "gtm_touches",
        "gtm_attribution_links",
        "gtm_email_accounts",
        "gtm_email_events",
        "gtm_email_send_queue",
        "gtm_market_profiles",
        "gtm_theses",
        "gtm_campaign_experiments",
        "gtm_outcomes",
        "gtm_buying_groups",
        "gtm_buying_group_members",
    }
)

# Every business table from 04 (warden tables live in a separate DB).
ALL_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "ventures",
        "idea_briefs",
        "charter_slots",
        "clarification_rounds",
        "questions",
        "answers",
        "charters",
        "org_plans",
        "agent_specs",
        "agent_instances",
        "agent_runs",
        "architecture_decision_records",
        "work_orders",
        "gate_results",
        "human_tasks",
        "channels",
        "campaigns",
        "posts",
        "feedback_events",
        "metrics",
        "evolution_cycles",
        "proposals",
        "promotions",
        "archive_entries",
        "world_state_snapshots",
        "simulation_runs",
        "calibration_records",
        "jobs",
        "event_log",
        "token_ledger",
        "onboarding_runs",
        "onboarding_steps",
        "activity_lines",
        "venture_resources",
        "task_fingerprints",
        "planned_tasks",
        "chat_messages",
        "venture_documents",
    }
    | PHASE10_TABLE_NAMES
    | OPS_TABLE_NAMES
    | CREDITS_TABLE_NAMES
    | PLAN112_TABLE_NAMES
    | GTM_TABLE_NAMES
    | CONTENT_TABLE_NAMES
    | MARKETING_TABLE_NAMES
    | COMMUNITY_TABLE_NAMES
)

from app.db.supymem_models import SUPYMEM_TABLE_NAMES  # noqa: E402

ALL_TABLE_NAMES = ALL_TABLE_NAMES | SUPYMEM_TABLE_NAMES

from app.db.import_models import ImportRun, SourceSnapshot  # noqa: E402,F401

ALL_TABLE_NAMES = ALL_TABLE_NAMES | {"import_runs", "source_snapshots"}

from app.db.import_cleanup_models import ImportObjectCleanup  # noqa: E402,F401
ALL_TABLE_NAMES = ALL_TABLE_NAMES | {"import_object_cleanups"}

ALL_TABLE_NAMES = ALL_TABLE_NAMES | {"person_referral_codes", "beta_code_redemptions", "beta_code_allowance_changes", "referral_admissions", "referral_program_state"}

from app.db.task_models import TASK_TABLE_NAMES  # noqa: E402
ALL_TABLE_NAMES = ALL_TABLE_NAMES | TASK_TABLE_NAMES

from app.gtm.shared_models import SharedEmailPattern, SharedAddressValidity, SharedEmailOutcome, PublicResearchCache  # noqa: E402,F401
ALL_TABLE_NAMES = ALL_TABLE_NAMES | {"gtm_shared_email_patterns", "gtm_shared_address_validity", "gtm_shared_email_outcomes", "gtm_public_research_cache"}

from app.lead_library.models import TABLE_NAMES as LEAD_LIBRARY_TABLE_NAMES  # noqa: E402
ALL_TABLE_NAMES = ALL_TABLE_NAMES | LEAD_LIBRARY_TABLE_NAMES

from app.db.inbox_models import InboxBriefingItem, InboxBriefingState  # noqa: F401

APPEND_ONLY_TABLES = APPEND_ONLY_TABLES | frozenset({"task_events"})

from app.config import settings as _settings  # noqa: E402

if _settings.company_os_memory_facts_enabled:
    from app.db.fact_models import VentureFact  # noqa: E402,F401

    ALL_TABLE_NAMES = ALL_TABLE_NAMES | {"venture_facts"}
