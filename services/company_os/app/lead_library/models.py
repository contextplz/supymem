"""Shared Lead Library: licensed public business contacts, separate from each workspace's own contacts."""
from datetime import datetime

from sqlalchemy import CheckConstraint, Float, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base

TABLE_NAMES = frozenset({"lead_library_entries", "lead_library_tags", "lead_library_usage"})


class LeadLibraryEntry(Base):
    __tablename__ = "lead_library_entries"
    __table_args__ = (
        CheckConstraint("status IN ('active','suppressed','retired')", name="ck_lead_library_entry_status"),
        Index("ix_lead_library_entries_type_country", "lead_type", "country_code"),
        Index("ix_lead_library_entries_function_seniority", "function", "seniority"),
        Index("ix_lead_library_entries_status_rank", "status", "fit_score"),
        Index("ix_lead_library_entries_email_hash", "email_hash"),
        Index("ix_lead_library_entries_company_domain", "company_domain"),
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    external_lead_id: Mapped[str] = mapped_column(Text, unique=True)
    person_name: Mapped[str] = mapped_column(Text)
    role_title: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    company_domain: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    x_url: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    email_hash: Mapped[str | None] = mapped_column(Text)
    email_status: Mapped[str] = mapped_column(Text, default="none")
    phone: Mapped[str | None] = mapped_column(Text)
    phone_status: Mapped[str] = mapped_column(Text, default="none")
    country_code: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    geography_raw: Mapped[str | None] = mapped_column(Text)
    lead_type: Mapped[str] = mapped_column(Text)
    function: Mapped[str] = mapped_column(Text)
    seniority: Mapped[str] = mapped_column(Text)
    company_stage: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    fit_score: Mapped[int | None] = mapped_column(Integer)
    quality_score: Mapped[float | None] = mapped_column(Float)
    usable_channels: Mapped[str] = mapped_column(Text, default="[]")
    quality_flags: Mapped[str] = mapped_column(Text, default="[]")
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    source_url: Mapped[str | None] = mapped_column(Text)
    source_host: Mapped[str | None] = mapped_column(Text)
    discovery_method: Mapped[str | None] = mapped_column(Text)
    license_source: Mapped[str] = mapped_column(Text)
    batch_id: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column()
    content_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    status_reason: Mapped[str | None] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text, default="")


class LeadLibraryTag(Base):
    __tablename__ = "lead_library_tags"
    __table_args__ = (Index("ix_lead_library_tags_lookup", "tag_type", "tag", "entry_id"),)
    entry_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tag_type: Mapped[str] = mapped_column(Text, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)


class LeadLibraryUsage(Base):
    __tablename__ = "lead_library_usage"
    __table_args__ = (Index("ix_lead_library_usage_entry_time", "entry_id", "contacted_at"),
        Index("ix_lead_library_usage_workspace", "workspace_id", "venture_id"))
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    entry_id: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[str] = mapped_column(Text)
    venture_id: Mapped[str | None] = mapped_column(Text)
    contacted_at: Mapped[datetime] = mapped_column()
    outcome: Mapped[str] = mapped_column(Text)
