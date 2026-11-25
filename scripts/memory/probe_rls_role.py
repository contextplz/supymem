"""Read-only role/policy metadata. Never boot the app or print its database URL.

Root runs this after reviewing the selected image and secret-reference custody.
A direct connection proves credential role flags, not a live app Session hook.
"""
from __future__ import annotations

import json
import os

import psycopg
from psycopg.rows import dict_row


def probe(database_url: str) -> dict:
    # Direct libpq URL; ORM driver suffixes are not part of psycopg's URI.
    database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10,
                         options="-c default_transaction_read_only=on -c statement_timeout=5000") as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user, session_user, current_setting('row_security') AS row_security, "
                           "r.rolsuper, r.rolbypassrls FROM pg_roles r WHERE r.rolname=current_user")
            role = cursor.fetchone()
            # SET ROLE can inherit broader privileges even when this login's
            # own flags are safe. Only metadata, never password fields.
            cursor.execute("WITH RECURSIVE memberships(roleid) AS ("
                           "SELECT oid FROM pg_roles WHERE rolname=current_user UNION "
                           "SELECT m.roleid FROM pg_auth_members m JOIN memberships x ON m.member=x.roleid) "
                           "SELECT r.rolname,r.rolsuper,r.rolbypassrls FROM pg_roles r "
                           "JOIN memberships x ON x.roleid=r.oid ORDER BY r.rolname")
            memberships = cursor.fetchall()
            cursor.execute("SELECT c.relname, pg_get_userbyid(c.relowner) AS owner, c.relrowsecurity, "
                           "c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                           "WHERE n.nspname='public' AND c.relname = ANY(%s) ORDER BY c.relname",
                           (["institutional_memories", "memory_working_entries", "chat_conversations", "chat_messages",
                             "chat_context_checkpoints", "venture_facts", "venture_documents"],))
            tables = cursor.fetchall()
    return {"role": role, "memberships": memberships, "tables": tables,
            "direct_credential_role_only": True, "actual_runtime_session_verified": False,
            "limits": "Does not inspect SET ROLE, application scope binding, pool reuse or active API/worker sessions."}


if __name__ == "__main__":
    url = os.environ.get("S05_ROLE_PROBE_DATABASE_URL")
    if not url:
        raise SystemExit("S05_ROLE_PROBE_DATABASE_URL must be provided in the process environment")
    try:
        print(json.dumps(probe(url), sort_keys=True))
    except Exception as exc:
        # Driver error strings may contain endpoint/user values; class only.
        raise SystemExit("Role metadata probe failed: " + type(exc).__name__) from None
