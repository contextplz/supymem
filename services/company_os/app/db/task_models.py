"""Founder task contracts and durable checkpoints on the existing jobs queue."""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, ForeignKey("workspaces.id"), nullable=False)
    venture_id: Mapped[str | None] = mapped_column(Text, ForeignKey("ventures.id"))
    owner_person_id: Mapped[str] = mapped_column(Text, ForeignKey("people.id"), nullable=False)
    create_key: Mapped[str | None] = mapped_column(Text, unique=True)
    create_digest: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="one_time")
    trigger_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    done_test_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    scope_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    budget_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    tolerance: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="submitted")
    blocker_json: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deadline_is_estimate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    __table_args__ = (
        CheckConstraint("kind in ('one_time','scheduled','event')", name="ck_tasks_kind"),
        CheckConstraint("tolerance >= 0 and tolerance <= 1", name="ck_tasks_tolerance"),
        CheckConstraint("state in ('submitted','working','input_required','auth_required','approval_required','paused','completed','failed','canceled','rejected')", name="ck_tasks_state"),
        Index("ix_tasks_due", "state", "next_fire_at"),
        Index("ix_tasks_scope", "workspace_id", "venture_id", "updated_at"),
        Index("ix_tasks_owner_schedule", "workspace_id", "venture_id", "owner_person_id", "kind"),
    )


class TaskRun(Base):
    __tablename__ = "task_runs"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, ForeignKey("tasks.id"), nullable=False)
    run_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="submitted")
    wallet_account_id: Mapped[str | None] = mapped_column(Text)
    cost_micro: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_cost_micro: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    control_job_id: Mapped[str | None] = mapped_column(Text, ForeignKey("jobs.id", ondelete="SET NULL"))
    evaluation_json: Mapped[str | None] = mapped_column(Text)
    allowance_json: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("task_id", "run_key", name="uq_task_run_fire"),)


class TaskStep(Base):
    __tablename__ = "task_steps"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, ForeignKey("task_runs.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    skill: Mapped[str] = mapped_column(Text, nullable=False)
    inputs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    done_test_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state: Mapped[str] = mapped_column(Text, nullable=False, default="submitted")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    strategy_id: Mapped[str] = mapped_column(Text, nullable=False, default="default")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    cost_micro: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    job_id: Mapped[str | None] = mapped_column(Text, ForeignKey("jobs.id", ondelete="SET NULL"), unique=True)
    runs_on: Mapped[str] = mapped_column(Text, nullable=False, default="server")
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_task_step_position"),
        CheckConstraint("runs_on in ('server','device','either')", name="ck_task_step_location"),
    )


class TaskEvent(Base):
    __tablename__ = "task_events"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, ForeignKey("tasks.id"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(Text, ForeignKey("task_runs.id"))
    step_id: Mapped[str | None] = mapped_column(Text, ForeignKey("task_steps.id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    __table_args__ = (Index("ix_task_events_timeline", "task_id", "created_at", "id"),)


class MaintenanceSchedule(Base):
    __tablename__ = "maintenance_schedules"
    tick: Mapped[str] = mapped_column(Text, primary_key=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    next_fire_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    __table_args__ = (Index("ix_maintenance_due", "next_fire_at"), CheckConstraint("interval_seconds > 0", name="ck_maintenance_interval"))


TASK_TABLE_NAMES = frozenset({"tasks", "task_runs", "task_steps", "task_events", "maintenance_schedules"})
