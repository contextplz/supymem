"""Idempotent columns/tables for the Email-Supymem port + GTM department.

Local Company OS DBs are often created with ``create_all`` rather than Alembic,
so new columns must be added in-place.
"""

from __future__ import annotations

import json

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

_CONTACT_COLUMNS = {
    "secondary_email": "TEXT",
    "x_handle": "TEXT",
    "enrichment_json": "TEXT",
    "sequence_state_json": "TEXT",
    "gmail_thread_id": "TEXT",
    "last_emailed_at": "TIMESTAMP",
    "times_emailed": "INTEGER DEFAULT 0",
    "replied_at": "TIMESTAMP",
    "preferred_channel": "TEXT DEFAULT 'auto'",
    "sequence_kind": "TEXT DEFAULT 'cold'",
    "enrichment_level": "INTEGER DEFAULT 0",
    "linkedin_stage": "TEXT",
    "times_linkedin": "INTEGER DEFAULT 0",
    "last_linkedin_at": "TIMESTAMP",
    "bounced_at": "TIMESTAMP",
    "icp_score": "FLOAT",
    "icp_tier": "TEXT",
    "outreach_pattern": "TEXT",
    "ask_rung": "TEXT DEFAULT 'none'",
    "ask_rung_changed_at": "TIMESTAMP",
    "score_void": "INTEGER DEFAULT 0",
    "score_sum": "INTEGER",
    "score_band": "TEXT",
    "score_factors_json": "TEXT DEFAULT '{}'",
    "score_explain": "TEXT",
    "why_this_week": "TEXT",
    "why_this_week_confirmed_at": "TIMESTAMP",
    "score_override": "TEXT",
    "score_override_reason": "TEXT",
    "exact_language_json": "TEXT DEFAULT '[]'",
    "recipient_geo": "TEXT",
    "contact_provenance": "TEXT",
    "mutual_count": "INTEGER",
    "buyer_tag": "TEXT",
    "recipient_seniority": "TEXT",
    "layer": "TEXT DEFAULT 'B'",
    "personalize_mode": "TEXT",
    "sku_key": "TEXT",
    "drop_reason": "TEXT",
    "cadence_week": "INTEGER",
    "next_action_at": "TIMESTAMP",
    "next_action_kind": "TEXT",
    "i_actually_know_them": "INTEGER DEFAULT 0",
}

_COMPANY_COLUMNS = {
    "company_type": "TEXT",
    "status_quo_stack_json": "TEXT DEFAULT '[]'",
    "wedge_fit": "TEXT",
    "notes": "TEXT",
}

_SIGNAL_COLUMNS = {
    "contact_id": "TEXT",
    "source": "TEXT",
    "trigger_code": "TEXT",
    "raw_text": "TEXT",
    "confidence": "TEXT",
    "layer": "TEXT DEFAULT 'A'",
    "extracted_quote": "TEXT",
    "timing_observed_at": "TIMESTAMP",
}

_TOUCH_COLUMNS = {
    "email_account_id": "TEXT",
    "workspace_id": "TEXT",
    "ask_rung": "TEXT",
    "outcome": "TEXT",
    "send_mode": "TEXT",
    "honesty_json": "TEXT",
    "sku_key": "TEXT",
    "copied_at": "TIMESTAMP",
    "logged_by": "TEXT",
    "numerator_event": "TEXT",
}

_LI_QUEUE_COLUMNS = {
    "copied_at": "TIMESTAMP",
    "sent_at": "TIMESTAMP",
    "honesty_json": "TEXT",
    "send_mode": "TEXT",
}

_ENGINE_COLUMNS = {
    "outreach_week": "INTEGER DEFAULT 1",
    "outbound_mode": "TEXT DEFAULT 'email_department'",
}

_CHILD_WORKSPACE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("gtm_enrollments", "workspace_id"),
    ("gtm_email_events", "workspace_id"),
    ("gtm_email_send_queue", "workspace_id"),
    ("gtm_research_snapshots", "workspace_id"),
)

_EXTRA_TABLES = (
    "gtm_email_accounts",
    "gtm_email_events",
    "gtm_email_send_queue",
    "gtm_engine_state",
    "gtm_research_jobs",
    "gtm_research_snapshots",
    "gtm_linkedin_queue",
    "gtm_learning_events",
    "gtm_market_profiles",
    "gtm_theses",
    "gtm_campaign_experiments",
    "gtm_outcomes",
    "gtm_buying_groups",
    "gtm_buying_group_members",
    "gtm_outreach_settings",
    "gtm_suppressions",
    "gtm_send_policies",
    "gtm_artifact_skus",
    "gtm_evidence_sources",
    "gtm_network_edges",
    "gtm_interviews",
    "gtm_pilots",
    "gtm_public_documents",
    "gtm_paste_bodies",
    "gtm_god_plans",
    "gtm_god_tasks",
    "gtm_god_messages",
)


def _alter_columns(engine: Engine, table: str, wanted: dict[str, str], tables: set[str]) -> None:
    if table not in tables:
        return
    insp = inspect(engine)
    existing = {c["name"] for c in insp.get_columns(table)}
    missing = [(name, ddl) for name, ddl in wanted.items() if name not in existing]
    if not missing:
        return
    with engine.begin() as conn:
        for name, ddl in missing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def persist_default_send_policies(session: Session, *, workspace_id: str | None = None) -> int:
    """Write ``seed_default_send_policies`` list-sink rows into ``gtm_send_policies``."""

    from app.db.models import GtmSendPolicy
    from app.gtm.relationships.geo_policy import seed_default_send_policies

    sink: list = []
    seed_default_send_policies(sink, workspace_id=workspace_id)
    q = select(GtmSendPolicy)
    if workspace_id is not None:
        q = q.where(GtmSendPolicy.workspace_id == workspace_id)
    existing = {(row.geo, row.channel, row.workspace_id) for row in session.scalars(q)}
    added = 0
    now = utcnow()
    for row in sink:
        key = (row.geo, row.channel, row.workspace_id)
        if key in existing:
            continue
        session.add(
            GtmSendPolicy(
                id=new_id("gtmpol"),
                workspace_id=row.workspace_id,
                geo=row.geo,
                channel=row.channel,
                rule_json=json.dumps(dict(row.rule)),
                created_at=now,
            )
        )
        existing.add(key)
        added += 1
    return added


def ensure_supymem_schema(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    _alter_columns(engine, "workspaces", {"default_gmail_mailbox_id": "TEXT"}, tables)
    _alter_columns(engine, "ventures", {"default_gmail_mailbox_id": "TEXT"}, tables)
    _alter_columns(engine, "content_oauth_states", {"gmail_intent": "TEXT", "gmail_mailbox_id": "TEXT"}, tables)
    _alter_columns(engine, "gtm_contacts", _CONTACT_COLUMNS, tables)
    _alter_columns(engine, "gtm_companies", _COMPANY_COLUMNS, tables)
    _alter_columns(engine, "gtm_signals", _SIGNAL_COLUMNS, tables)
    _alter_columns(engine, "gtm_touches", _TOUCH_COLUMNS, tables)
    _alter_columns(engine, "gtm_engine_state", _ENGINE_COLUMNS, tables)
    _alter_columns(engine, "gtm_linkedin_queue", _LI_QUEUE_COLUMNS, tables)
    _alter_columns(
        engine,
        "gtm_outreach_settings",
        {"dpa_accepted_at": "TIMESTAMP"},
        tables,
    )
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table, col in _CHILD_WORKSPACE_COLUMNS:
        _alter_columns(engine, table, {col: "TEXT"}, tables)

    from app.db.models import Base

    if "gtm_touches" in tables:
        indexes = {index["name"] for index in inspect(engine).get_indexes("gtm_touches")}
        if "uq_gtm_touch_mailbox_reply" not in indexes:
            with engine.begin() as connection:
                if "uq_gtm_touch_venture_reply" in indexes:
                    connection.execute(text("DROP INDEX uq_gtm_touch_venture_reply"))
                for index in Base.metadata.tables["gtm_touches"].indexes:
                    if index.name in {"uq_gtm_touch_venture_reply", "uq_gtm_touch_mailbox_reply"}:
                        index.create(connection, checkfirst=True)

    wanted = [Base.metadata.tables[name] for name in _EXTRA_TABLES if name in Base.metadata.tables]
    if wanted:
        Base.metadata.create_all(engine, tables=wanted)

    insp = inspect(engine)
    if "gtm_send_policies" in insp.get_table_names():
        with Session(engine) as session:
            persist_default_send_policies(session, workspace_id=None)
            session.commit()
