"""Durable scoped import intent and immutable accepted source; never a fake BuildRun."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.models import Base

STATES = "'awaiting_upload','uploading','queued','acquiring','validating','analyzing','report_complete','partial_report','needs_reconciliation','cancel_requested','cancelled','blocked','failed_definitive','expired'"


class ImportRun(Base):
    __tablename__ = "import_runs"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, ForeignKey("workspaces.id"), nullable=False)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    app_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_subject: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(Text, ForeignKey("founder_requests.id"), nullable=False, unique=True)
    job_id: Mapped[str | None] = mapped_column(Text, unique=True)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_hash: Mapped[str] = mapped_column(Text, nullable=False)
    input_digest: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_source_json: Mapped[str] = mapped_column(Text, nullable=False)
    acquisition_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source_snapshot_id: Mapped[str | None] = mapped_column(Text)
    report_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    report_digest: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquired_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    retained_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", "venture_id", "app_id", name="uq_import_run_scope"),
        UniqueConstraint("workspace_id", "venture_id", "app_id", "actor_subject", "idempotency_hash", name="uq_import_run_intent"),
        ForeignKeyConstraint(["app_id", "workspace_id", "venture_id"], ["apps.id", "apps.workspace_id", "apps.venture_id"], name="fk_import_app_scope"),
        ForeignKeyConstraint(["job_id", "workspace_id", "venture_id"], ["jobs.id", "jobs.workspace_id", "jobs.venture_id"], name="fk_import_job_scope"),
        ForeignKeyConstraint(["source_snapshot_id", "id", "workspace_id", "venture_id", "app_id"], ["source_snapshots.id", "source_snapshots.import_run_id", "source_snapshots.workspace_id", "source_snapshots.venture_id", "source_snapshots.app_id"], name="fk_import_snapshot_scope", use_alter=True),
        CheckConstraint("state IN (" + STATES + ")", name="ck_import_run_state"),
        CheckConstraint("source_kind IN ('github_public','upload_zip')", name="ck_import_run_source"),
        CheckConstraint("state_version >= 0 AND reserved_bytes >= 0 AND acquired_bytes >= 0 AND retained_bytes >= 0", name="ck_import_run_counters"),
        Index("ix_import_run_scope_created", "workspace_id", "venture_id", "app_id", "created_at"),
    )


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    app_id: Mapped[str] = mapped_column(Text, nullable=False)
    import_run_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    normalized_archive_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    storage_receipt_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_provenance_json: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        UniqueConstraint("id", "import_run_id", "workspace_id", "venture_id", "app_id", name="uq_source_snapshot_scope"),
        ForeignKeyConstraint(["import_run_id", "workspace_id", "venture_id", "app_id"], ["import_runs.id", "import_runs.workspace_id", "import_runs.venture_id", "import_runs.app_id"], name="fk_source_snapshot_import_scope"),
        CheckConstraint("size_bytes > 0 AND size_bytes <= 33554432", name="ck_source_snapshot_size"),
    )
