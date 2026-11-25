"""Exact import object retirement, fenced erasure and quota release receipt."""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, Integer, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.db.models import Base


class ImportObjectCleanup(Base):
    __tablename__ = "import_object_cleanups"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    venture_id: Mapped[str] = mapped_column(Text, nullable=False)
    app_id: Mapped[str] = mapped_column(Text, nullable=False)
    import_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    object_kind: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str | None] = mapped_column(Text, unique=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    initiated_by: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="scheduled")
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    due_at: Mapped[datetime] = mapped_column(nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column()
    operation_owner: Mapped[str | None] = mapped_column(Text)
    target_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    fence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    plan_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    receipt_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    released_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        UniqueConstraint("import_run_id", "object_kind", name="uq_import_cleanup_object"),
        ForeignKeyConstraint(["import_run_id", "workspace_id", "venture_id", "app_id"],
            ["import_runs.id", "import_runs.workspace_id", "import_runs.venture_id", "import_runs.app_id"], name="fk_import_cleanup_scope"),
        ForeignKeyConstraint(["job_id", "workspace_id", "venture_id"], ["jobs.id", "jobs.workspace_id", "jobs.venture_id"], name="fk_import_cleanup_job_scope"),
        CheckConstraint("object_kind IN ('raw','source')", name="ck_import_cleanup_kind"),
        CheckConstraint("state IN ('scheduled','fencing','deleting_versions','verifying','complete','needs_attention')", name="ck_import_cleanup_state"),
        CheckConstraint("state_version >= 0 AND recovery_count BETWEEN 0 AND 1 AND released_bytes >= 0", name="ck_import_cleanup_counters"),
        Index("ix_import_cleanup_due", "state", "due_at"),
    )
