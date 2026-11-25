"""Per-feature usage and unified error views, daily rollup tables, internal/external split.

Additive only: new tables, one index and PostgreSQL views. No analytics row is
rewritten; the views map historical rows to catalog slugs through
``analytics_feature_map`` (control id / chat tool / label alias).
"""

import sqlalchemy as sa
from alembic import op

revision = "s025analyticsfeat"
down_revision = "s024entrychat"
branch_labels = None
depends_on = None

_JSON = """CROSS JOIN LATERAL (SELECT CASE WHEN e.props_json LIKE '{%' THEN e.props_json::jsonb
                                  ELSE '{}'::jsonb END AS p) j"""

FEATURE_USAGE_VIEW = f"""
CREATE OR REPLACE VIEW v_feature_usage_events AS
SELECT e.id AS event_id, e.occurred_at, e.occurred_at::date AS day, e.person_id, e.workspace_id, e.venture_id,
       e.event_name, j.p->>'feature' AS feature_raw, j.p->>'control_id' AS control_id, j.p->>'tool' AS tool,
       j.p->>'source' AS source, j.p->>'outcome' AS outcome,
       COALESCE(c.slug, t.slug, a.slug, j.p->>'feature', 'unknown') AS slug,
       CASE e.event_name
            WHEN 'ui.screen_view' THEN 'view' WHEN 'ui.navigation' THEN 'view'
            WHEN 'ui.control_activate' THEN 'use' WHEN 'ui.control_exposure' THEN 'exposure'
            WHEN 'chat.tool_invoked' THEN 'use'
            ELSE CASE WHEN j.p->>'tool' IS NOT NULL THEN 'chat_dup'
                      WHEN e.id LIKE 'usage_%' THEN 'click' ELSE 'use' END END AS use_kind,
       CASE WHEN ia.person_id IS NULL THEN 'external' ELSE 'internal' END AS audience
FROM analytics_events e
{_JSON}
LEFT JOIN analytics_feature_map c ON c.kind = 'control' AND c.key = j.p->>'control_id'
LEFT JOIN analytics_feature_map t ON t.kind = 'tool' AND t.key = j.p->>'tool'
LEFT JOIN analytics_feature_map a ON a.kind = 'label' AND a.key = j.p->>'feature'
LEFT JOIN analytics_internal_accounts ia ON ia.person_id = e.person_id
WHERE e.event_name IN ('ui.screen_view', 'ui.navigation', 'ui.control_activate', 'ui.control_exposure',
                       'chat.tool_invoked', 'feature.used')
"""

# One row per error. The latest job.failed per job_id wins, and a client job_error
# is dropped when the worker already recorded that job (the raw client row stays).
ERRORS_VIEW = f"""
CREATE OR REPLACE VIEW v_errors_unified AS
WITH ev AS (
    SELECT e.id, e.occurred_at, e.person_id, e.workspace_id, e.venture_id, e.event_name, j.p
    FROM analytics_events e
    {_JSON}
    WHERE e.event_name IN ('ui.error', 'server.error', 'server.request_error', 'job.failed',
                           'model.output_rejected', 'chat.reply_error', 'build.finished')
), worker_jobs AS (
    SELECT DISTINCT ON (p->>'job_id') * FROM ev
    WHERE event_name = 'job.failed' AND p->>'job_id' IS NOT NULL
    ORDER BY p->>'job_id', occurred_at DESC, id DESC
), merged AS (
    SELECT id, occurred_at, person_id, workspace_id, venture_id, event_name, 'client' AS source,
           p->>'feature' AS feature_raw, p->>'error_code' AS error_code,
           NULLIF(substring(p->>'error_text' from 'status ([0-9]+)'), '')::int AS status,
           p->>'error_operation' AS route, NULL::text AS job_kind, p->>'job_id' AS job_id,
           p->>'error_ref' AS request_id
    FROM ev WHERE event_name = 'ui.error'
      AND NOT (p->>'error_code' = 'job_error' AND p->>'job_id' IN (SELECT p->>'job_id' FROM worker_jobs))
    UNION ALL
    SELECT id, occurred_at, person_id, workspace_id, venture_id, event_name, 'server',
           p->>'feature', COALESCE(NULLIF(p->>'error_class', 'none'), p->>'failure_kind'),
           CASE WHEN (p->>'status') ~ '^[0-9]{{1,3}}$' THEN (p->>'status')::int END,
           p->>'route', NULL, NULL, p->>'request_id'
    FROM ev WHERE event_name = 'server.error'
    UNION ALL
    SELECT id, occurred_at, person_id, workspace_id, venture_id, event_name, 'server',
           p->>'feature', COALESCE(p->>'error_code', 'none'),
           CASE WHEN (p->>'status') ~ '^[0-9]{{1,3}}$' THEN (p->>'status')::int END,
           p->>'route', NULL, NULL, p->>'request_id'
    FROM ev WHERE event_name = 'server.request_error'
    UNION ALL
    SELECT id, occurred_at, person_id, workspace_id, venture_id, event_name, 'worker',
           p->>'feature', p->>'error_code', NULL, NULL, p->>'kind', p->>'job_id', NULL
    FROM worker_jobs
    UNION ALL
    SELECT id, occurred_at, person_id, workspace_id, venture_id, event_name, 'model',
           p->>'feature', p->>'rule', NULL, NULL, p->>'operation_kind', p->>'job_id', NULL
    FROM ev WHERE event_name = 'model.output_rejected'
    UNION ALL
    SELECT id, occurred_at, person_id, workspace_id, venture_id, event_name, 'chat',
           p->>'feature', COALESCE(p->>'failure_kind', 'unknown'),
           CASE WHEN (p->>'status') ~ '^[0-9]{{1,3}}$' THEN (p->>'status')::int END,
           p->>'tool', NULL, NULL, NULL
    FROM ev WHERE event_name = 'chat.reply_error'
    UNION ALL
    SELECT id, occurred_at, person_id, workspace_id, venture_id, event_name, 'worker',
           p->>'feature', COALESCE(p->>'error_code', 'none'), NULL, NULL, 'build.run', NULL, NULL
    FROM ev WHERE event_name = 'build.finished' AND p->>'success' = 'false'
)
SELECT m.id AS event_id, m.occurred_at, m.occurred_at::date AS day, m.source, m.event_name, m.person_id,
       m.workspace_id, m.venture_id, COALESCE(a.slug, m.feature_raw, 'unattributed') AS feature,
       COALESCE(m.error_code, 'none') AS error_code, m.status, m.route, m.job_kind, m.job_id, m.request_id,
       CASE WHEN ia.person_id IS NULL THEN 'external' ELSE 'internal' END AS audience
FROM merged m
LEFT JOIN analytics_feature_map a ON a.kind = 'label' AND a.key = m.feature_raw
LEFT JOIN analytics_internal_accounts ia ON ia.person_id = m.person_id
"""

# Every catalog slug appears, zero-use ones last. Slugs seen in events but absent
# from the catalog also appear (in_catalog = false) so label drift is visible.
RANK_VIEW = """
CREATE OR REPLACE VIEW v_feature_rank_7d AS
WITH since AS (SELECT (now() AT TIME ZONE 'utc') - interval '7 days' AS at),
u AS (
    SELECT slug,
           count(*) FILTER (WHERE use_kind = 'view') AS views,
           count(*) FILTER (WHERE use_kind = 'use') AS uses,
           count(*) FILTER (WHERE use_kind = 'click') AS clicks,
           count(*) FILTER (WHERE audience = 'external') AS events_external,
           count(DISTINCT person_id) AS accounts,
           count(DISTINCT person_id) FILTER (WHERE audience = 'external') AS accounts_external,
           count(DISTINCT venture_id) AS ventures, max(occurred_at) AS last_seen
    FROM v_feature_usage_events, since
    WHERE occurred_at >= since.at AND use_kind IN ('view', 'use', 'click') GROUP BY slug
), er AS (
    SELECT feature AS slug, count(*) AS errors, count(*) FILTER (WHERE audience = 'external') AS errors_external
    FROM v_errors_unified, since WHERE occurred_at >= since.at GROUP BY feature
), seen AS (SELECT slug FROM u UNION SELECT slug FROM er),
joined AS (
    SELECT COALESCE(c.slug, s.slug) AS slug, c.slug IS NOT NULL AS in_catalog,
           COALESCE(c.feature_group, s.slug) AS feature_group, c.title, COALESCE(c.inventory_ids, '') AS inventory_ids,
           COALESCE(u.views, 0) AS views, COALESCE(u.uses, 0) AS uses, COALESCE(u.clicks, 0) AS clicks,
           COALESCE(u.events_external, 0) AS events_external, COALESCE(u.accounts, 0) AS accounts,
           COALESCE(u.accounts_external, 0) AS accounts_external, COALESCE(u.ventures, 0) AS ventures,
           COALESCE(er.errors, 0) AS errors, COALESCE(er.errors_external, 0) AS errors_external, u.last_seen
    FROM analytics_feature_catalog c
    FULL JOIN seen s ON s.slug = c.slug
    LEFT JOIN u ON u.slug = COALESCE(c.slug, s.slug)
    LEFT JOIN er ON er.slug = COALESCE(c.slug, s.slug)
)
SELECT RANK() OVER (ORDER BY views + uses + clicks DESC) AS rank, joined.*,
       views + uses + clicks AS events
FROM joined
"""

_TABLES = ("analytics_feature_catalog", "analytics_feature_map", "analytics_internal_accounts",
           "analytics_feature_daily", "analytics_error_daily")


def upgrade() -> None:
    bind = op.get_bind()
    have = set(sa.inspect(bind).get_table_names())
    if "analytics_feature_catalog" not in have:
        op.create_table("analytics_feature_catalog",
            sa.Column("slug", sa.Text(), primary_key=True), sa.Column("feature_group", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False), sa.Column("inventory_ids", sa.Text(), nullable=False))
    if "analytics_feature_map" not in have:
        op.create_table("analytics_feature_map",
            sa.Column("kind", sa.Text(), primary_key=True), sa.Column("key", sa.Text(), primary_key=True),
            sa.Column("slug", sa.Text(), nullable=False))
    if "analytics_internal_accounts" not in have:
        op.create_table("analytics_internal_accounts",
            sa.Column("person_id", sa.Text(), primary_key=True), sa.Column("reason", sa.Text(), nullable=False))
    if "analytics_feature_daily" not in have:
        op.create_table("analytics_feature_daily",
            sa.Column("day", sa.Date(), primary_key=True), sa.Column("feature", sa.Text(), primary_key=True),
            sa.Column("slug", sa.Text(), primary_key=True),
            *[sa.Column(name, sa.Integer(), nullable=False, server_default="0") for name in (
                "events", "events_internal", "accounts", "accounts_internal", "ventures", "screen_views",
                "control_uses", "chat_uses", "errors", "errors_internal")],
            sa.Column("updated_at", sa.DateTime(), nullable=False))
    if "analytics_error_daily" not in have:
        op.create_table("analytics_error_daily",
            sa.Column("day", sa.Date(), primary_key=True), sa.Column("feature", sa.Text(), primary_key=True),
            sa.Column("source", sa.Text(), primary_key=True), sa.Column("error_code", sa.Text(), primary_key=True),
            sa.Column("route", sa.Text(), primary_key=True, server_default=""),
            *[sa.Column(name, sa.Integer(), nullable=False, server_default="0") for name in (
                "count", "count_internal", "accounts")],
            sa.Column("updated_at", sa.DateTime(), nullable=False))
    if "ix_analytics_events_name_time" not in {i["name"] for i in sa.inspect(bind).get_indexes("analytics_events")}:
        op.create_index("ix_analytics_events_name_time", "analytics_events", ["event_name", "occurred_at"])
    # Seed the reference rows from the catalog as of this release; the analytics
    # rollup keeps them in step with later catalog edits (idempotent upserts).
    from app.analytics.feature_catalog import reference_rows

    catalog, mapping = reference_rows()
    for table, rows in (("analytics_feature_catalog", catalog), ("analytics_feature_map", mapping)):
        if not bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            op.bulk_insert(sa.table(table, *[sa.column(c) for c in rows[0]]), rows)
    if bind.dialect.name == "postgresql":
        for view in (FEATURE_USAGE_VIEW, ERRORS_VIEW, RANK_VIEW):
            op.execute(view)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in ("v_feature_rank_7d", "v_errors_unified", "v_feature_usage_events"):
            op.execute(f"DROP VIEW IF EXISTS {name}")
    have = set(sa.inspect(bind).get_table_names())
    for table in reversed(_TABLES):
        if table in have:
            op.drop_table(table)
    if "ix_analytics_events_name_time" in {i["name"] for i in sa.inspect(bind).get_indexes("analytics_events")}:
        op.drop_index("ix_analytics_events_name_time", table_name="analytics_events")
