"""Inbox briefing sidecars (mutable, tenant-scoped). Kept under app.db so they register once with Base."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class InboxBriefingState(Base):
    __tablename__ = "inbox_briefing_state"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    founder_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    mailbox_id: Mapped[str] = mapped_column(Text, nullable=False)
    consent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checkpoint: Mapped[datetime | None] = mapped_column(DateTime)
    data_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    __table_args__ = (UniqueConstraint("founder_id", "mailbox_id", name="uq_inbox_state_owner_mailbox"),)


class InboxBriefingItem(Base):
    __tablename__ = "inbox_briefing_items"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    founder_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    mailbox_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sender_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sender_email: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    urgent_kind: Mapped[str] = mapped_column(Text, nullable=False, default="")
    briefed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pinged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("founder_id", "mailbox_id", "message_id", name="uq_inbox_message"),
        Index("ix_inbox_items_retention", "received_at"))
