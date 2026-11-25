"""Daily product-metrics rollup. Idempotent so multi-instance AWS is safe."""

from __future__ import annotations

import os
from datetime import date, timedelta, timezone

from sqlalchemy import func, select, text

from app.db.models import (
    AnalyticsDailyRollup, AnalyticsEvent, AnalyticsFeatureCatalog, AnalyticsFeatureMap, AnalyticsInternalAccount,
)
from app.state import db
from core.clock import utcnow


ACTIVE_EVENTS = ("session.active", "chat.message", "venture.created", "idea.confirmed", "plan.approved")


def _first_people(session, *, names, since=None, until=None):
    first = select(AnalyticsEvent.person_id, func.min(AnalyticsEvent.occurred_at).label("first_at")).where(
        AnalyticsEvent.person_id.is_not(None), AnalyticsEvent.event_name.in_(names)
    ).group_by(AnalyticsEvent.person_id).subquery()
    query = select(func.count()).select_from(first)
    if since is not None:
        query = query.where(first.c.first_at >= since)
    if until is not None:
        query = query.where(first.c.first_at < until)
    return int(session.scalar(query) or 0)

def _distinct_people(session, *, since, until, names: tuple[str, ...] | None = None) -> int:
    stmt = select(func.count(func.distinct(AnalyticsEvent.person_id))).where(
        AnalyticsEvent.occurred_at >= since,
        AnalyticsEvent.occurred_at < until,
        AnalyticsEvent.person_id.is_not(None),
    )
    if names:
        stmt = stmt.where(AnalyticsEvent.event_name.in_(names))
    return int(session.scalar(stmt) or 0)


def _count(session, *, since, until, name: str) -> int:
    return int(
        session.scalar(
            select(func.count()).where(
                AnalyticsEvent.event_name == name,
                AnalyticsEvent.occurred_at >= since,
                AnalyticsEvent.occurred_at < until,
            )
        )
        or 0
    )


def _insert(session):
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def _env_list(name: str) -> set[str]:
    return {part.strip() for part in os.getenv(name, "").split(",") if part.strip()}


def sync_reference_tables() -> None:
    """Make the report views' reference tables match code and config. Idempotent.

    The feature catalog and its control/tool/alias map come from
    ``feature_catalog``. Internal accounts come from env, never from code:
    ``SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS`` (ids) and
    ``SUPYMEM_ANALYTICS_INTERNAL_EMAIL_DOMAINS`` (people whose email is on a
    listed domain). Only person ids are stored, never emails.
    """
    from app.analytics.feature_catalog import reference_rows
    from app.db.models import Person, PersonIdentity

    catalog, mapping = reference_rows()
    ids = _env_list("SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS")
    domains = {d.lower().lstrip("@") for d in _env_list("SUPYMEM_ANALYTICS_INTERNAL_EMAIL_DOMAINS")}
    with db()() as session:
        insert = _insert(session)
        by_domain: set[str] = set()
        if domains:
            pairs = (*session.execute(select(Person.id, Person.email)).all(),
                     *session.execute(select(PersonIdentity.person_id, PersonIdentity.email)).all())
            by_domain = {pid for pid, email in pairs if email and email.rsplit("@", 1)[-1].strip().lower() in domains}
        internal = ([{"person_id": i, "reason": "id"} for i in sorted(ids)]
                    + [{"person_id": i, "reason": "domain"} for i in sorted(by_domain - ids)])
        for model, rows, keys in ((AnalyticsFeatureCatalog, catalog, ["slug"]),
                                  (AnalyticsFeatureMap, mapping, ["kind", "key"]),
                                  (AnalyticsInternalAccount, internal, ["person_id"])):
            if rows:
                stmt = insert(model).values(rows)
                session.execute(stmt.on_conflict_do_update(index_elements=keys, set_={
                    c: getattr(stmt.excluded, c) for c in rows[0] if c not in keys}))
        # Drop reference rows the code or config no longer lists.
        session.execute(AnalyticsFeatureCatalog.__table__.delete().where(
            AnalyticsFeatureCatalog.slug.not_in([r["slug"] for r in catalog])))
        session.execute(AnalyticsFeatureMap.__table__.delete().where(
            (AnalyticsFeatureMap.kind + "|" + AnalyticsFeatureMap.key).not_in(
                [f"{r['kind']}|{r['key']}" for r in mapping])))
        session.execute(AnalyticsInternalAccount.__table__.delete().where(
            AnalyticsInternalAccount.person_id.not_in([r["person_id"] for r in internal])))
        session.commit()


_FEATURE_DAILY = """
WITH u AS (
    SELECT slug, count(*) AS events, count(*) FILTER (WHERE audience = 'internal') AS events_internal,
           count(DISTINCT person_id) AS accounts,
           count(DISTINCT person_id) FILTER (WHERE audience = 'internal') AS accounts_internal,
           count(DISTINCT venture_id) AS ventures,
           count(*) FILTER (WHERE event_name = 'ui.screen_view') AS screen_views,
           count(*) FILTER (WHERE event_name = 'ui.control_activate') AS control_uses,
           count(*) FILTER (WHERE event_name = 'chat.tool_invoked') AS chat_uses
    FROM v_feature_usage_events WHERE day = :day AND use_kind IN ('view', 'use', 'click') GROUP BY slug
), er AS (
    SELECT feature AS slug, count(*) AS errors, count(*) FILTER (WHERE audience = 'internal') AS errors_internal
    FROM v_errors_unified WHERE day = :day AND severity <> 'transient' GROUP BY feature
)
INSERT INTO analytics_feature_daily (day, feature, slug, events, events_internal, accounts, accounts_internal,
    ventures, screen_views, control_uses, chat_uses, errors, errors_internal, updated_at)
SELECT :day, COALESCE(c.feature_group, s.slug), s.slug, COALESCE(u.events, 0), COALESCE(u.events_internal, 0),
       COALESCE(u.accounts, 0), COALESCE(u.accounts_internal, 0), COALESCE(u.ventures, 0),
       COALESCE(u.screen_views, 0), COALESCE(u.control_uses, 0), COALESCE(u.chat_uses, 0),
       COALESCE(er.errors, 0), COALESCE(er.errors_internal, 0), :now
FROM (SELECT slug FROM u UNION SELECT slug FROM er) s
LEFT JOIN u ON u.slug = s.slug LEFT JOIN er ON er.slug = s.slug
LEFT JOIN analytics_feature_catalog c ON c.slug = s.slug
ORDER BY s.slug
ON CONFLICT (day, feature, slug) DO UPDATE SET events = EXCLUDED.events, events_internal = EXCLUDED.events_internal,
    accounts = EXCLUDED.accounts, accounts_internal = EXCLUDED.accounts_internal, ventures = EXCLUDED.ventures,
    screen_views = EXCLUDED.screen_views, control_uses = EXCLUDED.control_uses, chat_uses = EXCLUDED.chat_uses,
    errors = EXCLUDED.errors, errors_internal = EXCLUDED.errors_internal, updated_at = EXCLUDED.updated_at
"""
_ERROR_DAILY = """
INSERT INTO analytics_error_daily (day, feature, source, error_code, route, count, count_internal, accounts, updated_at)
SELECT :day, feature, source, error_code, COALESCE(route, ''), count(*),
       count(*) FILTER (WHERE audience = 'internal'), count(DISTINCT person_id), :now
FROM v_errors_unified WHERE day = :day AND severity <> 'transient' GROUP BY feature, source, error_code, COALESCE(route, '')
ORDER BY feature, source, error_code, COALESCE(route, '')
ON CONFLICT (day, feature, source, error_code, route) DO UPDATE SET count = EXCLUDED.count,
    count_internal = EXCLUDED.count_internal, accounts = EXCLUDED.accounts, updated_at = EXCLUDED.updated_at
"""


def rollup_features(day: date) -> None:
    """Refresh one day of per-feature usage and error counts from the report views (PostgreSQL only)."""
    with db()() as session:
        if session.get_bind().dialect.name != "postgresql":
            return  # the views are PostgreSQL-only; SQLite dev databases skip the feature rollup
        params = {"day": day, "now": utcnow().replace(tzinfo=None)}
        session.execute(text(_FEATURE_DAILY), params)
        session.execute(text(_ERROR_DAILY), params)
        session.commit()


def rollup_day(day: date) -> AnalyticsDailyRollup:
    start = utcnow().replace(tzinfo=None).replace(
        year=day.year, month=day.month, day=day.day, hour=0, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(days=1)
    factory = db()
    with factory() as session:
        dau = _distinct_people(session, since=start, until=end, names=ACTIVE_EVENTS)
        wau = _distinct_people(session, since=start - timedelta(days=6), until=end, names=ACTIVE_EVENTS)
        mau = _distinct_people(session, since=start - timedelta(days=29), until=end, names=ACTIVE_EVENTS)
        signups = _first_people(session, names=("signup",), since=start, until=end)
        first_ventures = _first_people(session, names=("venture.created",), since=start, until=end)
        # Subscription-start events may be stubs, not verified payments. This
        # legacy field now explicitly represents first recorded credit top-ups.
        first_paid = _first_people(session, names=("credits.topup",), since=start, until=end)
        values = dict(day=day, dau=dau, wau=wau, mau=mau, signups=signups,
                      first_ventures=first_ventures, first_paid=first_paid, updated_at=utcnow())
        if session.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        statement = insert(AnalyticsDailyRollup).values(**values)
        session.execute(statement.on_conflict_do_update(index_elements=["day"],
            set_={key: value for key, value in values.items() if key != "day"}))
        session.commit()
        row = session.get(AnalyticsDailyRollup, day)
        session.refresh(row)
        session.expunge(row)
        return row


def rollup_today() -> AnalyticsDailyRollup:
    today = utcnow().date()
    try:
        sync_reference_tables()
        rollup_features(today - timedelta(days=1))  # late rows and the day boundary
        rollup_features(today)
    except Exception:  # noqa: BLE001 - feature reports never block the product metrics
        import logging

        logging.getLogger("app.analytics").exception("feature rollup failed")
    return rollup_day(today)


def latest_product_metrics() -> dict[str, object]:
    """Refresh today's observed metrics; never silently reuse an old day."""
    row = rollup_today()
    return {
        "day": row.day.isoformat(),
        "updated_at": row.updated_at.replace(tzinfo=timezone.utc).isoformat(),
        "coverage": "partial_instrumentation",
        "paid_basis": "first_recorded_credit_topup_not_revenue",
        "dau": row.dau,
        "wau": row.wau,
        "mau": row.mau,
        "signups": row.signups,
        "first_ventures": row.first_ventures,
        "first_paid": row.first_paid,
    }


def activation_funnel() -> dict[str, int]:
    """Ordered observed cohorts; missing instrumentation is not inferred."""
    def first(name, alias):
        return select(AnalyticsEvent.person_id.label("person"), func.min(AnalyticsEvent.occurred_at).label("at")).where(
            AnalyticsEvent.event_name == name, AnalyticsEvent.person_id.is_not(None)
        ).group_by(AnalyticsEvent.person_id).subquery(alias)
    signup, venture, paid = first("signup", "signed_up"), first("venture.created", "created"), first("credits.topup", "paid")
    joined = signup.join(venture, (signup.c.person == venture.c.person) & (venture.c.at >= signup.c.at))
    paid_joined = joined.join(paid, (paid.c.person == signup.c.person) & (paid.c.at >= venture.c.at))
    with db()() as session:
        return {"signup": int(session.scalar(select(func.count()).select_from(signup)) or 0),
                "first_venture": int(session.scalar(select(func.count()).select_from(joined)) or 0),
                "first_paid": int(session.scalar(select(func.count()).select_from(paid_joined)) or 0)}


def venture_activity(limit: int = 10) -> list[dict[str, object]]:
    factory = db()
    since = (utcnow() - timedelta(days=30)).replace(tzinfo=None)
    with factory() as session:
        rows = session.execute(
            select(
                AnalyticsEvent.venture_id,
                func.count().label("events"),
            )
            .where(
                AnalyticsEvent.venture_id.is_not(None),
                AnalyticsEvent.occurred_at >= since,
            )
            .group_by(AnalyticsEvent.venture_id)
            .order_by(func.count().desc())
            .limit(limit)
        ).all()
    return [{"venture_id": vid, "events_30d": int(n)} for vid, n in rows if vid]


def feature_views() -> list[dict]:
    """Last30days of schema-validated client screen views, not task success."""
    from sqlalchemy import cast
    since = (utcnow() - timedelta(days=30)).replace(tzinfo=None)
    with db()() as session:
        if session.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import JSONB
            feature = cast(AnalyticsEvent.props_json, JSONB)["feature"].astext
        else:
            feature = func.json_extract(AnalyticsEvent.props_json, "$.feature")
        rows = session.execute(select(feature.label("feature"), func.count().label("views"),
            func.count(func.distinct(AnalyticsEvent.person_id)).label("people")).where(
                AnalyticsEvent.id.startswith("usage_", autoescape=True),
                AnalyticsEvent.event_name == "ui.screen_view", AnalyticsEvent.occurred_at >= since
            ).group_by(feature).order_by(func.count().desc()).limit(30)).all()
    return [{"feature": row.feature, "views": int(row.views), "people": int(row.people)} for row in rows if row.feature]


def workflow_outcomes() -> list[dict]:
    """Canonical persisted job outcomes, not browser claims of completion."""
    from app.db.models import Job
    kinds = ("idea.recon", "idea.clarify_turn", "idea.generate_candidates", "idea.validate",
             "research.deep", "plan.compile", "content.topics", "content.draft", "build.run",
             "build.watch", "qa.run", "deploy.run", "app.debug", "app.iterate", "import.analyze", "import.cleanup")
    since = (utcnow() - timedelta(days=30)).replace(tzinfo=None)
    with db()() as session:
        duration = (func.extract("epoch", Job.finished_at - Job.started_at)
            if session.get_bind().dialect.name == "postgresql"
            else (func.julianday(Job.finished_at) - func.julianday(Job.started_at)) * 86400)
        rows = session.execute(select(Job.kind, Job.status, func.count().label("runs"),
            func.avg(duration).label("mean_seconds")).where(Job.kind.in_(kinds), Job.created_at >= since)
            .group_by(Job.kind, Job.status).order_by(Job.kind, Job.status)).all()
    return [{"workflow": row.kind, "state": row.status, "runs": int(row.runs),
             "mean_seconds": round(float(row.mean_seconds), 1) if row.mean_seconds is not None else None} for row in rows]
