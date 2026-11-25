"""Source-bound venture context; replaced statements remain in history."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class VentureFact(Base):
    __tablename__ = "venture_facts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, ForeignKey("workspaces.id"), nullable=False)
    venture_id: Mapped[str] = mapped_column(Text, ForeignKey("ventures.id"), nullable=False)
    person_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_digest: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column()
    recorded_at: Mapped[datetime] = mapped_column(nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column()
    forgotten_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("workspace_id", "venture_id", "person_id", "dedupe_key", name="uq_venture_fact_source"),
        Index("uq_venture_fact_current", "workspace_id", "venture_id", "person_id", "subject", "predicate",
              unique=True, postgresql_where=text("valid_to IS NULL AND forgotten_at IS NULL"),
              sqlite_where=text("valid_to IS NULL AND forgotten_at IS NULL")),
        Index("ix_venture_fact_history", "workspace_id", "venture_id", "person_id", "subject", "predicate", "valid_from"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_venture_fact_confidence"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_venture_fact_validity"),
        CheckConstraint("superseded_at IS NULL OR superseded_at >= recorded_at", name="ck_venture_fact_recording"),
        CheckConstraint("(valid_to IS NULL) = (superseded_at IS NULL)", name="ck_venture_fact_retirement"),
    )
