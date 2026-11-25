"""Print DB-only migration counts and workspace IDs. Never call a provider."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import make_url

from app.db.session import normalize_db_url, resolve_database_url


def migration_report(connection) -> dict:
    workspaces = {row.id: {"workspace_id": row.id, "gmail_connections": 0, "linkedin_connections": 0,
        "queued_sends": 0, "active_sequences": 0} for row in connection.execute(text("SELECT id FROM workspaces"))}
    owners = dict(connection.execute(text("SELECT id, owner_id FROM workspaces")).all())
    pending_sequences = set()
    for row in connection.execute(text("SELECT workspace_id, COUNT(*) AS count FROM gtm_email_accounts "
            "WHERE provider = 'gmail' AND connected_account_id IS NOT NULL AND connected_account_id != '' GROUP BY workspace_id")):
        if row.workspace_id in workspaces:
            workspaces[row.workspace_id]["gmail_connections"] = row.count
    for row in connection.execute(text("SELECT workspace_id, extra_json FROM content_accounts WHERE platform = 'linkedin'")):
        if row.workspace_id in workspaces and json.loads(row.extra_json or "{}").get("connected_account_id"):
            workspaces[row.workspace_id]["linkedin_connections"] += 1
    for row in connection.execute(text("SELECT workspace_id, COUNT(*) AS count FROM gtm_email_send_queue "
            "WHERE status IN ('queued', 'sending') GROUP BY workspace_id")):
        if row.workspace_id in workspaces:
            workspaces[row.workspace_id]["queued_sends"] += row.count
    for row in connection.execute(text("SELECT v.workspace_id, d.provenance_json FROM venture_documents d "
            "JOIN ventures v ON v.id = d.venture_id WHERE d.kind = 'outreach_draft'")):
        try:
            draft = json.loads(row.provenance_json or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(draft, dict) and row.workspace_id in workspaces and draft.get("channel") == "email" and draft.get("state") in {"draft", "delivery_pending", "delivery_unconfirmed"}:
            workspaces[row.workspace_id]["queued_sends"] += 1
            if draft.get("contact_id") and draft.get("day", 1) > 1:
                pending_sequences.add(draft["contact_id"])
    pending_sequences.update(row.contact_id for row in connection.execute(text(
        "SELECT contact_id FROM gtm_email_send_queue WHERE status IN ('queued', 'sending') AND sequence_day > 1")))
    pending_sequences.update(row.contact_id for row in connection.execute(text(
        "SELECT contact_id FROM gtm_enrollments WHERE status = 'active'")))
    for row in connection.execute(text("SELECT id, workspace_id FROM gtm_contacts "
            "WHERE replied_at IS NULL AND bounced_at IS NULL AND do_not_contact = 0 "
            "AND unsubscribed_at IS NULL")):
        if row.workspace_id in workspaces:
            workspaces[row.workspace_id]["active_sequences"] += int(row.id in pending_sequences)
    rows = sorted((row for row in workspaces.values() if any(row[key] for key in
        ("gmail_connections", "linkedin_connections", "queued_sends", "active_sequences"))), key=lambda row: row["workspace_id"])
    return {"founders": len({owners[row["workspace_id"]] for row in rows}), "workspaces": len(rows),
        **{f"{platform}_{kind}": len({owners[row["workspace_id"]] if kind == "founders" else row["workspace_id"]
            for row in rows if row[f"{platform}_connections"]}) for platform in ("gmail", "linkedin") for kind in ("founders", "workspaces")},
        **{key: sum(row[key] for row in rows) for key in ("gmail_connections", "linkedin_connections", "queued_sends", "active_sequences")},
        "workspace_counts": rows}


def main() -> None:
    url = resolve_database_url()
    if not url:
        raise SystemExit("Set COMPANY_OS_DB_URL, DATABASE_URL or DB_URL to a read-only database connection.")
    database_url = make_url(normalize_db_url(url))
    if database_url.get_backend_name() == "sqlite":
        database = database_url.database or ""
        database_url = database_url.set(database=database if database.startswith("file:") else Path(database).resolve().as_uri(),
            query={**database_url.query, "mode": "ro", "uri": "true"})
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("PRAGMA query_only = ON" if engine.dialect.name == "sqlite" else "SET TRANSACTION READ ONLY"))
            print(json.dumps(migration_report(connection), sort_keys=True))
            connection.rollback()
    except SQLAlchemyError:
        raise SystemExit("The read-only migration report could not read the database.") from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
