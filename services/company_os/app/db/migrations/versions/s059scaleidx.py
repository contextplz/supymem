"""Provider token fences, bounded class claims and latest mailbox events.

The memory lane supplies s058memsearch, whose parent is s057inboxbrief.
"""

from contextlib import nullcontext
import re

from alembic import op
import sqlalchemy as sa

revision = "s059scaleidx"
down_revision = "s058memsearch"
branch_labels = None
depends_on = None

_SCOPE = "coalesce(workspace_id, venture_id, id)"
_PRIORITY_KINDS = ("chat.recovery", "chat.respond", "estimate.compute", "idea.clarify_turn",
                   "idea.generate_candidates", "idea.recon", "idea.validate", "scope.classify")
_CLASS = "(CASE WHEN kind IN (" + ", ".join(f"'{kind}'" for kind in _PRIORITY_KINDS) + ") THEN 0 ELSE 1 END)"
INDEXES = (
    ("ix_jobs_workspace_claim", "jobs", f"{_SCOPE}, {_CLASS}, priority ASC, created_at ASC, id ASC",
     "status IN ('pending', 'retry_wait', 'scheduled') AND cancel_requested IS false"),
    ("ix_jobs_workspace_inflight", "jobs", f"{_SCOPE}, {_CLASS}, status", None),
    ("ix_jobs_workspace_rotation", "jobs", f"{_SCOPE}, {_CLASS}, started_at DESC", "started_at IS NOT NULL"),
    ("ix_gtm_email_events_mailbox_type_latest", "gtm_email_events",
     "email_account_id, event_type, created_at DESC", None),
)
_POSTGRES_CLASS = "( CASE WHEN (kind = ANY (ARRAY[" + ", ".join(
    f"'{kind}'::text" for kind in _PRIORITY_KINDS) + "])) THEN 0 ELSE 1 END)"
_POSTGRES_SCOPE = "COALESCE(workspace_id, venture_id, id)"
_POSTGRES_DEFINITIONS = (
    f"btree ({_POSTGRES_SCOPE}, {_POSTGRES_CLASS}, priority, created_at, id) WHERE "
    "((status = ANY (ARRAY['pending'::text, 'retry_wait'::text, 'scheduled'::text])) AND (cancel_requested IS false))",
    f"btree ({_POSTGRES_SCOPE}, {_POSTGRES_CLASS}, status)",
    f"btree ({_POSTGRES_SCOPE}, {_POSTGRES_CLASS}, started_at DESC) WHERE (started_at IS NOT NULL)",
    "btree (email_account_id, event_type, created_at DESC)",
)


def _definition_tokens(definition):
    return [token if token.startswith(("'", '"')) else token.lower()
            for token in re.findall(r"""'(?:''|[^'])*'|"(?:""|[^"])*"|\w+|[^\s]""", definition)]


def _index_valid(name, table, expected):
    connection = op.get_bind()
    expected = _definition_tokens(expected)
    if connection.dialect.name == "postgresql":
        row = connection.execute(sa.text("SELECT indisvalid, indrelid = to_regclass(:table), "
            "pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": name, "table": table}).first()
        if row and row[0] and (not row[1] or not row[2].startswith("CREATE INDEX ")
                              or _definition_tokens(row[2].partition(" USING ")[2]) != expected):
            raise RuntimeError(f"scale_index_definition_mismatch:{name}")
        return row[0] if row else None
    definition = connection.scalar(sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
                                   {"name": name})
    if definition and _definition_tokens(definition) != expected:
        raise RuntimeError(f"scale_index_definition_mismatch:{name}")
    return True if definition else None


def upgrade():
    postgres = op.get_bind().dialect.name == "postgresql"
    op.execute(sa.text("CREATE TABLE IF NOT EXISTS provider_admission_fences (scope TEXT PRIMARY KEY, "
        "token TEXT NOT NULL, acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"))
    with op.get_context().autocommit_block() if postgres else nullcontext():
        for (name, table, columns, predicate), definition in zip(INDEXES, _POSTGRES_DEFINITIONS):
            if predicate and not postgres:
                predicate = predicate.replace("IS false", "IS 0")
            expected = definition if postgres else (f"CREATE INDEX {name} ON {table} ({columns})"
                + (f" WHERE {predicate}" if predicate else ""))
            if _index_valid(name, table, expected) is False:
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
            op.execute(sa.text(f"CREATE INDEX {'CONCURRENTLY ' if postgres else ''}IF NOT EXISTS {name} "
                f"ON {table} ({columns})" + (f" WHERE {predicate}" if predicate else "")))
            if _index_valid(name, table, expected) is not True:
                raise RuntimeError(f"scale_index_invalid:{name}")


def downgrade():
    if op.get_context().dialect.name == "postgresql":
        op.get_bind().execute(sa.text("LOCK TABLE provider_admission_fences IN ACCESS EXCLUSIVE MODE"))
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM provider_admission_fences)")):
        raise RuntimeError("provider_admission_active_prevents_downgrade")
    # A later history guard must be able to roll back the entire downgrade chain.
    for name, *_rest in reversed(INDEXES):
        op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    op.execute(sa.text("DROP TABLE provider_admission_fences"))
