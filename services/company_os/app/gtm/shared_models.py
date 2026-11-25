"""Only company patterns, opaque outcome keys, and public provider results are shared."""
from datetime import datetime

from sqlalchemy import CheckConstraint, Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class SharedEmailPattern(Base):
    __tablename__ = "gtm_shared_email_patterns"
    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    pattern: Mapped[str] = mapped_column(Text, primary_key=True)
    confidence: Mapped[int] = mapped_column(Integer, default=80)
    published_pattern: Mapped[bool] = mapped_column(default=False)
    workspace_count: Mapped[int] = mapped_column(Integer, default=0)
    sends: Mapped[float] = mapped_column(Float, default=0)
    deliveries: Mapped[float] = mapped_column(Float, default=0)
    replies: Mapped[float] = mapped_column(Float, default=0)
    hard_bounces: Mapped[float] = mapped_column(Float, default=0)
    retired_at: Mapped[datetime | None] = mapped_column()
    last_seen: Mapped[datetime] = mapped_column()


class SharedAddressValidity(Base):
    __tablename__ = "gtm_shared_address_validity"
    __table_args__ = (CheckConstraint("status IN ('unknown','deliverable','retired')", name="ck_gtm_shared_address_status"),)
    address_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, default="unknown")
    workspace_count: Mapped[int] = mapped_column(Integer, default=0)
    delivery_workspaces: Mapped[int] = mapped_column(Integer, default=0)
    reply_workspaces: Mapped[int] = mapped_column(Integer, default=0)
    hard_bounce_workspaces: Mapped[int] = mapped_column(Integer, default=0)
    deliveries: Mapped[float] = mapped_column(Float, default=0)
    replies: Mapped[float] = mapped_column(Float, default=0)
    hard_bounces: Mapped[float] = mapped_column(Float, default=0)
    suppressed: Mapped[bool] = mapped_column(default=False)
    retired_at: Mapped[datetime | None] = mapped_column()
    last_seen: Mapped[datetime] = mapped_column()


class SharedEmailOutcome(Base):
    __tablename__ = "gtm_shared_email_outcomes"
    event_key: Mapped[str] = mapped_column(Text, primary_key=True)
    address_hash: Mapped[str] = mapped_column(Text, index=True)
    workspace_hash: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(Text, index=True)
    pattern: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(index=True)


class PublicResearchCache(Base):
    __tablename__ = "gtm_public_research_cache"
    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    response_json: Mapped[str] = mapped_column(Text)
    saved_at: Mapped[datetime] = mapped_column()
