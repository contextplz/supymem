"""v_errors_unified exposes what the user saw: message, source, intent, page, app version, client time.

Additive: the same view with extra trailing columns (CREATE OR REPLACE keeps v_feature_rank_7d valid). Only
client ``ui.error`` rows carry them; other sources get NULL.
"""

import importlib.util
from pathlib import Path

from alembic import op

revision = "s027usererrors"
down_revision = "s026autopilot"
branch_labels = None
depends_on = None


def _s025():
    spec = importlib.util.spec_from_file_location("_s025_views", Path(__file__).with_name("s025_analytics_feature_views.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _view() -> str:
    module = _s025()
    base, json_join = module.ERRORS_VIEW.split("AS\n", 1)[1], module._JSON
    return f"""
CREATE OR REPLACE VIEW v_errors_unified AS
SELECT b.*, x.p->>'error_context' AS error_context, x.p->>'error_text' AS error_text,
       x.p->>'error_source' AS error_source, x.p->>'error_intent' AS intent, x.p->>'error_page' AS page_route,
       x.p->>'app_version' AS app_version,
       CASE WHEN (x.p->>'error_occurred_at') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T' THEN (x.p->>'error_occurred_at')::timestamptz END
           AS client_occurred_at,
       x.p->>'error_request_id' AS error_request_id,
       COALESCE(x.p->>'error_severity', 'error') AS severity,
       CASE WHEN (x.p->>'error_repeat_count') ~ '^[0-9]{{1,6}}$' THEN (x.p->>'error_repeat_count')::int END AS repeat_count
FROM ({base}) b
LEFT JOIN (SELECT e.id, j.p FROM analytics_events e {json_join} WHERE e.event_name = 'ui.error') x
       ON b.source = 'client' AND x.id = b.event_id
"""


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_view())
        # Per-feature error counts are user-facing errors: transient (retried / expected) rows are listed, not counted.
        op.execute(_s025().RANK_VIEW.replace("WHERE occurred_at >= since.at GROUP BY feature",
                                             "WHERE occurred_at >= since.at AND severity <> 'transient' GROUP BY feature"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Restore the s025 definition (the extra columns cannot be dropped by REPLACE).
        op.execute("DROP VIEW IF EXISTS v_feature_rank_7d")
        op.execute("DROP VIEW IF EXISTS v_errors_unified")
        module = _s025()
        op.execute(module.ERRORS_VIEW)
        op.execute(module.RANK_VIEW)
