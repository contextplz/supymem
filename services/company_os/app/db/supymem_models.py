"""Supymem tables (docs/supymem/05 §2). Imported from models so Base.metadata sees them.

Existing ``agent_runs`` (org-agent instances) is unchanged. Runtime runs live on
``agent_runtime_runs`` / ``agent_runtime_steps`` (deviation logged in doc 35/36).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
    false as sa_false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


def _ws() -> Mapped[str]:
    return mapped_column(Text, nullable=False, index=True)


def _ven() -> Mapped[str]:
    return mapped_column(Text, ForeignKey("ventures.id"), nullable=False, index=True)


# --- 2.1 Ideation -----------------------------------------------------------


class LootDropEntry(Base):
    __tablename__ = "loot_drop_entries"
    url: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(1200), nullable=False)
    founded: Mapped[str | None] = mapped_column(String(4))
    ended: Mapped[str | None] = mapped_column(String(4))
    cash_burned: Mapped[str | None] = mapped_column(String(60))
    category: Mapped[str | None] = mapped_column(String(120))
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class IdeaSession(Base):
    __tablename__ = "idea_sessions"
    __fsm__ = "idea_session"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    case: Mapped[str] = mapped_column(Text, nullable=False)  # know|explore
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    question_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    questions_asked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recon_summary: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    current_revision_id: Mapped[str | None] = mapped_column(Text)
    confirmed_revision_id: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirmed_by_person_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_idea_sessions_venture_state", "venture_id", "state"),)


class IdeaRevision(Base):
    __tablename__ = "idea_revisions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, ForeignKey("idea_sessions.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    rev: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    structured: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    diff_from_prev: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "rev", name="uq_idea_revisions_session_rev"),)


class ClarifierTurn(Base):
    __tablename__ = "clarifier_turns"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, ForeignKey("idea_sessions.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    options: Mapped[str | None] = mapped_column(Text)
    chosen: Mapped[str | None] = mapped_column(Text)
    revision_id: Mapped[str | None] = mapped_column(Text)
    llm_usage: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class IdeaClarificationRun(Base):
    """One provider receipt per durable job; output is never conversational memory."""
    __tablename__ = "idea_clarification_runs"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False, unique=True)
    session_id: Mapped[str] = mapped_column(Text, ForeignKey("idea_sessions.id"), nullable=False, index=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    revision_id: Mapped[str] = mapped_column(Text, ForeignKey("idea_revisions.id"), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ValidationRun(Base):
    __tablename__ = "validation_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, ForeignKey("idea_sessions.id"), nullable=False)
    revision_id: Mapped[str] = mapped_column(Text, ForeignKey("idea_revisions.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    method: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    verdict: Mapped[str | None] = mapped_column(Text)
    report: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    founder_decision: Mapped[str | None] = mapped_column(Text)
    founder_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- 2.2 Research -----------------------------------------------------------


class ResearchDossier(Base):
    __tablename__ = "research_dossiers"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    revision_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    budget_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    s3_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class ResearchFinding(Base):
    __tablename__ = "research_findings"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    dossier_id: Mapped[str] = mapped_column(Text, ForeignKey("research_dossiers.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    category: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    license: Mapped[str | None] = mapped_column(Text)
    license_ok: Mapped[bool | None] = mapped_column(Boolean)
    stars: Mapped[int | None] = mapped_column(Integer)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime)
    relevance: Mapped[float | None] = mapped_column(Numeric(4, 3))
    reuse_recommendation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_research_findings_dossier_category", "dossier_id", "category"),)


class ThirdPartyService(Base):
    __tablename__ = "third_party_services"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    dossier_id: Mapped[str] = mapped_column(Text, ForeignKey("research_dossiers.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    pricing_model: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    limits: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    integration_difficulty: Mapped[str] = mapped_column(Text, nullable=False, default="moderate")
    integration_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reliability: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    scaling_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    credentials_required: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    byo_possible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supymem_provided: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sourcing: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(Text)
    monitor: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    limitations: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# --- 2.3 Plan / scope / estimate --------------------------------------------


class ExecutionPlan(Base):
    __tablename__ = "execution_plans"
    __fsm__ = "execution_plan"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    revision_id: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    goal_first_paid_user: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    structured: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    approved_by: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("venture_id", "version", name="uq_execution_plans_ven_version"),)


class PlanMilestone(Base):
    __tablename__ = "plan_milestones"
    __fsm__ = "plan_milestone"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    plan_id: Mapped[str] = mapped_column(Text, ForeignKey("execution_plans.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="in")
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    estimate_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actual_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    depends_on: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    acceptance: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class PlanTask(Base):
    __tablename__ = "plan_tasks"
    __fsm__ = "plan_task"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    milestone_id: Mapped[str] = mapped_column(Text, ForeignKey("plan_milestones.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    agent_role: Mapped[str | None] = mapped_column(Text)
    tool_hints: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    estimate_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actual_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    build_run_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class ScopeDecision(Base):
    __tablename__ = "scope_decisions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    plan_id: Mapped[str] = mapped_column(Text, ForeignKey("execution_plans.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(Text, nullable=False)
    reasons: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    founder_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    alternatives: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    decided_by: Mapped[str] = mapped_column(Text, nullable=False, default="agent")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ResourceEstimate(Base):
    __tablename__ = "resource_estimates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    plan_id: Mapped[str] = mapped_column(Text, ForeignKey("execution_plans.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    breakdown: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    gross_upfront_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_upfront_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    monthly_run_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    confidence: Mapped[str] = mapped_column(Text, nullable=False, default="low")
    method_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class FounderRequest(Base):
    __tablename__ = "founder_requests"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    source: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(Text)
    intent_category: Mapped[str] = mapped_column(Text, nullable=False, default="other")
    classification: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    outcome: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    failure_reason: Mapped[str | None] = mapped_column(Text)
    related_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class ExistingAsset(Base):
    __tablename__ = "existing_assets"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str | None] = mapped_column(Text, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    exists: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    accessible: Mapped[bool | None] = mapped_column(Boolean)
    compatible: Mapped[bool | None] = mapped_column(Boolean)
    usable_tasks: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    usable: Mapped[bool | None] = mapped_column(Boolean)
    credits_saved_estimate: Mapped[int | None] = mapped_column(BigInteger)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="founder")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class FounderAction(Base):
    __tablename__ = "founder_actions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    plan_task_id: Mapped[str | None] = mapped_column(Text)
    scope_decision_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    required_secret_slots: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    blocks_task: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# --- 2.4 Apps / builds / deployments ----------------------------------------


class InfraTier(Base):
    __tablename__ = "infra_tiers"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    cpu: Mapped[float] = mapped_column(Float, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    min_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    db_placement: Mapped[str] = mapped_column(Text, nullable=False, default="shared_cluster")
    monthly_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class FounderApp(Base):
    """Founder-built application. Table name ``apps`` (doc 05)."""

    __tablename__ = "apps"
    __fsm__ = "app"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    build_mode: Mapped[str] = mapped_column(Text, nullable=False, default="supymem")
    deploy_target: Mapped[str] = mapped_column(Text, nullable=False, default="supymem")
    infra_tier_id: Mapped[str | None] = mapped_column(Text, ForeignKey("infra_tiers.id"))
    repo_id: Mapped[str | None] = mapped_column(Text)
    primary_domain_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (UniqueConstraint("id", "workspace_id", "venture_id", name="uq_app_import_scope"),)


class AppRepo(Base):
    __tablename__ = "app_repos"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    remote_url: Mapped[str | None] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(Text, nullable=False, default="main")
    github_installation_id: Mapped[str | None] = mapped_column(Text)
    last_commit_sha: Mapped[str | None] = mapped_column(Text)
    s3_bundle_key: Mapped[str | None] = mapped_column(Text)
    imported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class BuildRun(Base):
    __tablename__ = "build_runs"
    __fsm__ = "build_run"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    plan_task_id: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    agent: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    spec: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sandbox_ref: Mapped[str | None] = mapped_column(Text)
    reservation_id: Mapped[str | None] = mapped_column(Text)
    credits_used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    exit_summary: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    artifact_id: Mapped[str | None] = mapped_column(Text)
    runtime_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    job_id: Mapped[str | None] = mapped_column(Text, ForeignKey("jobs.id"), unique=True)
    request_digest: Mapped[str | None] = mapped_column(Text)
    idempotency_key_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("id", "workspace_id", "venture_id", name="uq_build_run_scope"),
        UniqueConstraint("app_id", "idempotency_key_hash", name="uq_build_run_intent"),
        Index("uq_build_run_open_app", "app_id", unique=True,
              postgresql_where=text("runtime_version = 1 AND closed_at IS NULL"),
              sqlite_where=text("runtime_version = 1 AND closed_at IS NULL")),
    )


class BuildEvent(Base):
    __tablename__ = "build_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    build_run_id: Mapped[str] = mapped_column(Text, ForeignKey("build_runs.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (Index("ix_build_events_run_seq", "build_run_id", "seq"),
                      UniqueConstraint("build_run_id", "seq", name="uq_build_event_sequence"),)


class BuildModelCall(Base):
    """One explicit SDK invocation; identical prompts may be distinct invocations."""
    __tablename__ = "build_model_calls"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    build_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    invocation_key: Mapped[str] = mapped_column(Text, nullable=False)
    input_event_id: Mapped[str | None] = mapped_column(Text)
    pricing_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    consumed_event_seq: Mapped[int | None] = mapped_column(BigInteger)
    consumed_event_id: Mapped[str | None] = mapped_column(Text)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reservation_id: Mapped[str | None] = mapped_column(Text, ForeignKey("credit_reservations.id"))
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    ceiling_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_micro: Mapped[int | None] = mapped_column(BigInteger)
    charged_micro: Mapped[int | None] = mapped_column(BigInteger)
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger)
    response_ref: Mapped[str | None] = mapped_column(Text)
    response_digest: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        ForeignKeyConstraint(["build_run_id", "workspace_id", "venture_id"],
                             ["build_runs.id", "build_runs.workspace_id", "build_runs.venture_id"],
                             name="fk_build_call_scope"),
        UniqueConstraint("build_run_id", "invocation_key", name="uq_build_call_invocation"),
        UniqueConstraint("build_run_id", "sequence", name="uq_build_call_sequence"),
        CheckConstraint("ceiling_micro > 0 AND generation > 0 AND sequence > 0 AND attempt >= 0", name="ck_build_call_bounds"),
        CheckConstraint("actual_micro >= 0 AND charged_micro >= 0 AND charged_micro <= ceiling_micro "
                        "AND prompt_tokens >= 0 AND completion_tokens >= 0", name="ck_build_call_usage"),
    )


class BuildArtifact(Base):
    __tablename__ = "build_artifacts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    build_run_id: Mapped[str] = mapped_column(Text, ForeignKey("build_runs.id"), nullable=False)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    image_uri: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("id", "build_run_id", "app_id", "workspace_id", name="uq_build_artifact_scope"),)


class QaRun(Base):
    __tablename__ = "qa_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    build_run_id: Mapped[str] = mapped_column(Text, ForeignKey("build_runs.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    suites: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    report_s3_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    runtime_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    managed_build_id: Mapped[str | None] = mapped_column(Text)
    generation: Mapped[int | None] = mapped_column(Integer)
    source_artifact_id: Mapped[str | None] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(Text)
    source_manifest_digest: Mapped[str | None] = mapped_column(Text)
    contract_digest: Mapped[str | None] = mapped_column(Text)
    input_digest: Mapped[str | None] = mapped_column(Text)
    report_digest: Mapped[str | None] = mapped_column(Text)
    unsupported: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    receipt_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")

    __table_args__ = (
        ForeignKeyConstraint(["managed_build_id", "workspace_id", "venture_id"],
                             ["build_runs.id", "build_runs.workspace_id", "build_runs.venture_id"], name="fk_qa_managed_build_scope"),
        ForeignKeyConstraint(["source_artifact_id", "build_run_id", "app_id", "workspace_id"],
                             ["build_artifacts.id", "build_artifacts.build_run_id", "build_artifacts.app_id", "build_artifacts.workspace_id"],
                             name="fk_qa_source_scope"),
        Index("uq_qa_managed_input", "build_run_id", "generation", "input_digest", unique=True,
              postgresql_where=text("runtime_version = 1"), sqlite_where=text("runtime_version = 1")),
        CheckConstraint("runtime_version IN (0, 1) AND (runtime_version = 0 OR (passed >= 0 AND failed >= 0 AND unsupported >= 0))", name="ck_qa_runtime_counts"),
        CheckConstraint("runtime_version = 0 OR (managed_build_id IS NOT NULL AND managed_build_id = build_run_id "
                        "AND generation IS NOT NULL AND generation > 0 AND source_artifact_id IS NOT NULL "
                        "AND source_sha256 IS NOT NULL AND length(source_sha256) = 64 "
                        "AND source_manifest_digest IS NOT NULL AND length(source_manifest_digest) = 64 "
                        "AND contract_digest IS NOT NULL AND length(contract_digest) = 64 "
                        "AND input_digest IS NOT NULL AND length(input_digest) = 64 "
                        "AND state IN ('queued','provisioning','running','report_ready','passed','failed','uncertain','cancelled'))",
                        name="ck_qa_managed_receipt"),
    )


class QaFinding(Base):
    __tablename__ = "qa_findings"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    qa_run_id: Mapped[str] = mapped_column(Text, ForeignKey("qa_runs.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    file: Mapped[str | None] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fix_build_run_id: Mapped[str | None] = mapped_column(Text)


class Deployment(Base):
    __tablename__ = "deployments"
    __fsm__ = "deployment"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    build_artifact_id: Mapped[str | None] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    health_url: Mapped[str | None] = mapped_column(Text)
    ecs_service_arn: Mapped[str | None] = mapped_column(Text)
    cf_tenant_id: Mapped[str | None] = mapped_column(Text)
    previous_deployment_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    live_at: Mapped[datetime | None] = mapped_column(DateTime)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("app_id", "version", name="uq_deployments_app_version"),)


class AppRuntimeIntent(Base):
    """Immutable admission and evolving receipts for one founder-app generation."""

    __tablename__ = "app_runtime_intents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    plan_task_id: Mapped[str] = mapped_column(Text, ForeignKey("plan_tasks.id"), nullable=False)
    build_run_id: Mapped[str] = mapped_column(Text, ForeignKey("build_runs.id"), nullable=False)
    source_artifact_id: Mapped[str] = mapped_column(Text, ForeignKey("build_artifacts.id"), nullable=False)
    qa_run_id: Mapped[str] = mapped_column(Text, ForeignKey("qa_runs.id"), nullable=False)
    source_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    source_manifest_digest: Mapped[str] = mapped_column(Text, nullable=False)
    qa_contract_digest: Mapped[str] = mapped_column(Text, nullable=False)
    domain_id: Mapped[str] = mapped_column(Text, ForeignKey("app_domains.id"), nullable=False)
    hostname: Mapped[str] = mapped_column(Text, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    phase: Mapped[str] = mapped_column(Text, nullable=False, default="source_accepted")
    receipts_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    reservation_id: Mapped[str | None] = mapped_column(Text, ForeignKey("credit_reservations.id"))
    approved_by: Mapped[str] = mapped_column(Text, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "app_id", "workspace_id", "venture_id", name="uq_app_runtime_intent_scope"),
        UniqueConstraint("app_id", "generation", name="uq_app_runtime_intent_generation"),
        UniqueConstraint("app_id", "request_digest", name="uq_app_runtime_intent_request"),
        CheckConstraint("generation > 0", name="ck_app_runtime_intent_generation"),
        CheckConstraint("length(source_sha256) = 64 AND length(source_manifest_digest) = 64 "
                        "AND length(qa_contract_digest) = 64 AND length(request_digest) = 64",
                        name="ck_app_runtime_intent_digests"),
        CheckConstraint("state IN ('pending','provisioning','reconciling','staged','review','live','failed','cancelled')",
                        name="ck_app_runtime_intent_state"),
        CheckConstraint("phase IN ('source_accepted','database_secret_created','database_provisioning',"
                        "'auth_provisioned','image_building','image_ready','runtime_migrated','service_staged','preview_live','runtime_qa',"
                        "'publication_review','live')", name="ck_app_runtime_intent_phase"),
        Index("uq_app_runtime_intent_open", "app_id", unique=True,
              postgresql_where=text("state IN ('pending','provisioning','reconciling','staged','review')"),
              sqlite_where=text("state IN ('pending','provisioning','reconciling','staged','review')")),
    )


class AppDomain(Base):
    __tablename__ = "app_domains"
    __fsm__ = "app_domain"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    hostname: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    cf_tenant_id: Mapped[str | None] = mapped_column(Text)
    acm_arn: Mapped[str | None] = mapped_column(Text)
    validation_records: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    active_at: Mapped[datetime | None] = mapped_column(DateTime)


class AppDatabase(Base):
    __tablename__ = "app_databases"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    engine: Mapped[str] = mapped_column(Text, nullable=False, default="postgres")
    placement: Mapped[str] = mapped_column(Text, nullable=False)
    instance_id: Mapped[str | None] = mapped_column(Text)
    db_name: Mapped[str] = mapped_column(Text, nullable=False)
    role_name: Mapped[str] = mapped_column(Text, nullable=False)
    secret_arn: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AppAuthPool(Base):
    __tablename__ = "app_auth_pools"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    provider: Mapped[str] = mapped_column(Text, nullable=False, default="cognito")
    pool_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    domain_prefix: Mapped[str | None] = mapped_column(Text)
    hosted_ui_url: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AppEndUser(Base):
    __tablename__ = "app_end_users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    provider_sub: Mapped[str] = mapped_column(Text, nullable=False)
    email_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)


class AppSecret(Base):
    __tablename__ = "app_secrets"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    secret_arn: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime)


class AppApiKey(Base):
    __tablename__ = "app_api_keys"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    prefix: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    monthly_cap_credits: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class AppUsageRecord(Base):
    __tablename__ = "app_usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    meter: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    credits_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    end_user_hash: Mapped[str | None] = mapped_column(Text)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ledger_entry_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "app_id", "meter", "window_start", "source", name="uq_app_usage_window"
        ),
    )


class AppMetricDaily(Base):
    __tablename__ = "app_metrics_daily"

    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    visits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unique_visitors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signups: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_5xx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    p95_ms: Mapped[int | None] = mapped_column(Integer)
    ai_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revenue_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="INR")


class AppIncident(Base):
    __tablename__ = "app_incidents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    debug_run_id: Mapped[str | None] = mapped_column(Text)


class ScalingEvent(Base):
    __tablename__ = "scaling_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    from_tier: Mapped[str | None] = mapped_column(Text)
    to_tier: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    approved_by: Mapped[str | None] = mapped_column(Text)
    approval_id: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ExternalAwsLink(Base):
    __tablename__ = "external_aws_links"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str | None] = mapped_column(Text)
    role_arn: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(Text, nullable=False, default="us-east-1")
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- 2.5 Sites --------------------------------------------------------------


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    current_version_id: Mapped[str | None] = mapped_column(Text)
    analytics_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SiteVisit(Base):
    __tablename__ = "site_visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[str] = mapped_column(Text, ForeignKey("sites.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    session_hash: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    referrer_host: Mapped[str | None] = mapped_column(Text)
    utm: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    country: Mapped[str | None] = mapped_column(Text)
    device: Mapped[str | None] = mapped_column(Text)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class SiteLead(Base):
    __tablename__ = "site_leads"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    site_id: Mapped[str] = mapped_column(Text, ForeignKey("sites.id"), nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    fields: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(Text)


class EmailEvent(Base):
    __tablename__ = "email_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    to_hash: Mapped[str] = mapped_column(Text, nullable=False)
    template: Mapped[str | None] = mapped_column(Text)
    provider_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# --- 2.6 Agent runtime (new; existing agent_runs untouched) ------------------


class AgentRuntimeRun(Base):
    __tablename__ = "agent_runtime_runs"
    __fsm__ = "agent_run"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    app_id: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    job_id: Mapped[str | None] = mapped_column(Text)
    input: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    credits_used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)


class AgentRuntimeStep(Base):
    __tablename__ = "agent_runtime_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Text, ForeignKey("agent_runtime_runs.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    input_redacted: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output_redacted: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    credits_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class JobAttempt(Base):
    __tablename__ = "job_attempts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


# --- 2.7 Analytics / experiments / security ---------------------------------


class Experiment(Base):
    __tablename__ = "experiments"
    __fsm__ = "experiment"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    venture_id: Mapped[str | None] = mapped_column(Text, index=True)
    app_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    context: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    variables: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state: Mapped[str] = mapped_column(Text, nullable=False, default="proposed")
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    outcome: Mapped[str | None] = mapped_column(Text)
    metric: Mapped[str | None] = mapped_column(Text)
    baseline: Mapped[float | None] = mapped_column(Float)
    result: Mapped[float | None] = mapped_column(Float)
    learnings: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class ExperimentObservation(Base):
    __tablename__ = "experiment_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(Text, ForeignKey("experiments.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class ExperimentAssignment(Base):
    __tablename__ = "experiment_assignments"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    experiment_id: Mapped[str] = mapped_column(Text, ForeignKey("experiments.id"), nullable=False)
    subject_hash: Mapped[str] = mapped_column(Text, nullable=False)
    variant: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SectorTag(Base):
    __tablename__ = "sector_tags"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    sector: Mapped[str] = mapped_column(Text, nullable=False)
    sub_sector: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="agent")


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="info")
    subject: Mapped[str | None] = mapped_column(Text)
    workspace_id: Mapped[str | None] = mapped_column(Text, index=True)
    ip_hash: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class IncidentReport(Base):
    __tablename__ = "incident_reports"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    postmortem_s3_key: Mapped[str | None] = mapped_column(Text)


class DesktopDevice(Base):
    __tablename__ = "desktop_devices"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    person_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False, default="macos")
    app_version: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    refresh_token_hash: Mapped[str | None] = mapped_column(Text)
    previous_refresh_token_hash: Mapped[str | None] = mapped_column(Text)
    refresh_rotated_at: Mapped[datetime | None] = mapped_column(DateTime)
    refresh_token_used: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuthCode(Base):
    __tablename__ = "auth_codes"

    code_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    person_id: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[str | None] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"

    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False, default="delete")


class Notice(Base):
    __tablename__ = "notices"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    person_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_path: Mapped[str | None] = mapped_column(Text)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class GatewayUser(Base):
    __tablename__ = "gateway_users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    session_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class GatewayAuthToken(Base):
    __tablename__ = "gateway_auth_tokens"

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, ForeignKey("gateway_users.id"), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class GatewayIdentity(Base):
    __tablename__ = "gateway_identities"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AppEvent(Base):
    __tablename__ = "app_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    props: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class AppFunnel(Base):
    __tablename__ = "app_funnels"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class AppForecast(Base):
    __tablename__ = "app_forecasts"

    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    resource: Mapped[str] = mapped_column(Text, primary_key=True)
    current: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    projected_30d: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    limit: Mapped[float | None] = mapped_column(Float)
    days_to_limit: Mapped[int | None] = mapped_column(Integer)
    credits_30d: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class LearningProposal(Base):
    __tablename__ = "learning_proposals"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    patch: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state: Mapped[str] = mapped_column(Text, nullable=False, default="proposed")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PatternStat(Base):
    __tablename__ = "pattern_stats"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    prefix: Mapped[str] = mapped_column(Text, nullable=False)
    success_rate: Mapped[float] = mapped_column(Float, nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


SUPYMEM_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "loot_drop_entries",
        "idea_sessions",
        "idea_revisions",
        "clarifier_turns",
        "validation_runs",
        "research_dossiers",
        "research_findings",
        "third_party_services",
        "execution_plans",
        "plan_milestones",
        "plan_tasks",
        "scope_decisions",
        "resource_estimates",
        "founder_requests",
        "existing_assets",
        "founder_actions",
        "infra_tiers",
        "apps",
        "app_repos",
        "build_runs",
        "build_model_calls",
        "build_events",
        "build_artifacts",
        "qa_runs",
        "qa_findings",
        "deployments",
        "app_domains",
        "app_databases",
        "app_auth_pools",
        "app_end_users",
        "app_secrets",
        "app_api_keys",
        "app_usage_records",
        "app_metrics_daily",
        "app_incidents",
        "scaling_events",
        "external_aws_links",
        "sites",
        "site_visits",
        "site_leads",
        "email_events",
        "agent_runtime_runs",
        "agent_runtime_steps",
        "job_attempts",
        "experiments",
        "experiment_observations",
        "experiment_assignments",
        "sector_tags",
        "security_events",
        "incident_reports",
        "desktop_devices",
        "auth_codes",
        "retention_policies",
        "notices",
        "gateway_users",
        "gateway_identities",
        "gateway_auth_tokens",
        "app_events",
        "app_funnels",
        "app_forecasts",
        "learning_proposals",
        "pattern_stats",
    }
)


class VentureWaitlist(Base):
    __tablename__ = "venture_waitlists"
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), primary_key=True)
    workspace_id: Mapped[str] = _ws()
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_version: Mapped[str] = mapped_column(Text, nullable=False)
    enabled_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    probe_hash: Mapped[str | None] = mapped_column(Text)
    probe_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_entry_id: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VentureWaitlistEntry(Base):
    __tablename__ = "venture_waitlist_entries"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("venture_waitlists.venture_id"), nullable=False)
    workspace_id: Mapped[str] = _ws()
    email: Mapped[str] = mapped_column(Text, nullable=False)
    consent_version: Mapped[str] = mapped_column(Text, nullable=False)
    consent_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    props: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("venture_id", "email", name="uq_venture_waitlist_email"),
        Index("ix_venture_waitlist_entries_scope", "workspace_id", "venture_id", "created_at"),
    )


class ChatProposal(Base):
    __tablename__ = "chat_proposals"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    arguments_json: Mapped[str] = mapped_column(Text, nullable=False)
    review_json: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EntryChatProposal(Base):
    """A pre-venture chat route and its one durable confirmation receipt."""

    __tablename__ = "entry_chat_proposals"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    route: Mapped[str] = mapped_column(Text, nullable=False)
    target_venture_id: Mapped[str | None] = mapped_column(Text)
    skipped_steps_json: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    next_step: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("actor_id", "request_id", name="uq_entry_chat_request"),
        Index("ix_entry_chat_actor_created", "actor_id", "created_at"),
    )


SUPYMEM_TABLE_NAMES = SUPYMEM_TABLE_NAMES | {"entry_chat_proposals", "suggestions"}


class CodingSourceReceipt(Base):
    """Immutable transport custody for a reviewed local coding order.

    A stored row is only source custody. It cannot satisfy QA, founder
    acceptance, a plan task, or runtime admission by itself.
    """

    __tablename__ = "coding_source_receipts"

    order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    plan_task_id: Mapped[str] = mapped_column(Text, nullable=False)
    execution_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    receipt_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("state IN ('awaiting_upload','stored','needs_reconciliation')", name="ck_coding_source_state"),
        Index("ix_coding_source_scope", "workspace_id", "venture_id", "app_id", "created_at"),
    )


class CodingSourcePreflight(Base):
    """Immutable, read-only contract inspection; never a runtime QA pass."""

    __tablename__ = "coding_source_preflights"

    order_id: Mapped[str] = mapped_column(Text, ForeignKey("coding_source_receipts.order_id"), primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    app_id: Mapped[str] = mapped_column(Text, ForeignKey("apps.id"), nullable=False)
    source_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    source_manifest_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    report_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('blocked_runtime_contract','requires_runtime_qa')", name="ck_coding_preflight_status"),
        Index("ix_coding_preflight_scope", "workspace_id", "venture_id", "app_id", "created_at"),
    )


class AutopilotSetting(Base):
    """Per-venture God mode (beta) switch and its safety floors."""

    __tablename__ = "autopilot_settings"

    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), primary_key=True)
    god_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    daily_cap_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    allow_send: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    send_max_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allow_mac: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    continue_when_offline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GodModeRequest(Base):
    """A founder's early-access request for God mode. Approval allowlists that venture past the beta gate."""

    __tablename__ = "god_mode_requests"
    __table_args__ = (
        CheckConstraint("status IN ('requested','approved','declined')", name="ck_god_mode_requests_status"),
        # One active (requested or approved) row per person per venture; a declined one may be re-requested.
        Index("uq_god_mode_requests_active", "person_id", "venture_id", unique=True,
              postgresql_where=text("status IN ('requested','approved')"),
              sqlite_where=text("status IN ('requested','approved')")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    person_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    email: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="requested")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(Text)


class Suggestion(Base):
    """A task Supymem offers to take over completely. The founder accepts, saves or dismisses it."""

    __tablename__ = "suggestions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = _ven()
    person_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    why: Mapped[str] = mapped_column(Text, nullable=False, default="")
    plan_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    est_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    action_kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    requires_mac: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # new | accepted | saved | dismissed | executing | done | failed
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_ref: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_suggestions_venture_status", "venture_id", "status"),)


class AutopilotEvent(Base):
    """Autopilot timeline: one row per auto-approved or refused action."""

    __tablename__ = "autopilot_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    venture_id: Mapped[str] = _ven()
    action: Mapped[str] = mapped_column(Text, nullable=False)
    what: Mapped[str] = mapped_column(Text, nullable=False)
    why: Mapped[str] = mapped_column(Text, nullable=False, default="")
    credits_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)  # done | blocked | stopped | failed | info
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_autopilot_events_venture_created", "venture_id", "created_at"),)


class BetaCode(Base):
    __tablename__ = "beta_codes"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    owner_person_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer)


class ReferralProgramState(Base):
    __tablename__ = "referral_program_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    grandfather_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReferralAdmission(Base):
    __tablename__ = "referral_admissions"
    person_id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    legacy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PersonReferralCode(Base):
    __tablename__ = "person_referral_codes"
    person_id: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[str] = mapped_column(String(8), ForeignKey("beta_codes.code"), nullable=False, unique=True)
    last_nudged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BetaCodeRedemption(Base):
    __tablename__ = "beta_code_redemptions"
    person_id: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[str] = mapped_column(String(8), ForeignKey("beta_codes.code"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reward_ledger_id: Mapped[str | None] = mapped_column(Text, ForeignKey("credit_ledger.id"), unique=True)


class BetaCodeAllowanceChange(Base):
    __tablename__ = "beta_code_allowance_changes"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[str] = mapped_column(String(8), ForeignKey("beta_codes.code"), nullable=False, index=True)
    actor_admin_id: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    before_limit: Mapped[int | None] = mapped_column(Integer)
    after_limit: Mapped[int | None] = mapped_column(Integer)
    before_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    after_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BetaCodeDownload(Base):
    __tablename__ = "beta_code_downloads"
    __table_args__ = (Index("ix_beta_code_downloads_code", "code", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(8), ForeignKey("beta_codes.code"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip_hash: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    platform: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    version: Mapped[str] = mapped_column(String(40), nullable=False, default="")


class EmailMessage(Base):
    """Outbox row for every email Supymem sends or deliberately does not send."""
    __tablename__ = "email_messages"
    __table_args__ = (Index("ix_email_messages_person", "person_id", "created_at"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    person_id: Mapped[str] = mapped_column(Text, nullable=False)
    to_email: Mapped[str] = mapped_column(Text, nullable=False, default="")
    template: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(Text, nullable=False)  # queued|sent|failed|suppressed
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    context_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    provider_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class EmailPreference(Base):
    """Per-person opt-outs. Transactional mail has no switch."""
    __tablename__ = "email_preferences"
    person_id: Mapped[str] = mapped_column(Text, primary_key=True)
    product_updates: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    engagement_reminders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    digests: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    suggestions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class EmailSuppression(Base):
    """Addresses that bounced hard or complained; nothing is sent to them again."""
    __tablename__ = "email_suppressions"
    email: Mapped[str] = mapped_column(Text, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NeedsYouRequest(Base):
    """Something only the founder can do; one open row per (venture, blocking_ref)."""

    __tablename__ = "needs_you_requests"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = _ws()
    venture_id: Mapped[str] = _ven()
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    what_we_need: Mapped[str] = mapped_column(Text, nullable=False, default="")
    how_to_resume: Mapped[str] = mapped_column(Text, nullable=False, default="")
    blocking_ref: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="normal")  # blocking | normal
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")  # open | resolved | expired
    open_key: Mapped[str | None] = mapped_column(Text, unique=True)  # venture|ref while open, else NULL (dedupe)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminders_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ErrorReport(Base):
    """One row per error fingerprint (code + route/kind + top frame). Closed labels and ids only."""

    __tablename__ = "error_reports"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    feature: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    place: Mapped[str] = mapped_column(Text, nullable=False, default="")  # route template, job kind or client intent
    frame: Mapped[str] = mapped_column(Text, nullable=False, default="")  # top app frame "module:function", no line numbers
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="error")  # error | warning
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open", index=True)  # open | resolved
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    release: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sample_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # last person/workspace/venture/status
    affected_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # distinct person ids, capped
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)


class ProblemReport(Base):
    """A founder-written "Report a problem" with the error fingerprints seen around it."""

    __tablename__ = "problem_reports"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    person_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    venture_id: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    page: Mapped[str] = mapped_column(Text, nullable=False, default="")
    app_version: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    fingerprints_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    screenshot: Mapped[bytes | None] = mapped_column(LargeBinary)  # only when the founder attached one and consented
    screenshot_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
