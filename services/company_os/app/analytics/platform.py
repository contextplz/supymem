"""Live founder metrics, aggregated in SQL rather than loading event history."""

from datetime import timedelta

from sqlalchemy import JSON, case, cast, func, or_, select

from app.analytics.feature_catalog import ACTION_SLUG, CONTROL_SLUG, FEATURES, LABEL_ALIAS
from app.analytics.rollup import ACTIVE_EVENTS, _env_list
from app.credits.units import MICRO_PER_CREDIT
from app.db.models import (
    AnalyticsEvent, CreditAccount, CreditLedgerEntry,
    GtmTouch, Person, PersonIdentity, Venture, Workspace,
)
from app.db.supymem_models import VentureWaitlistEntry
from app.state import db
from core.clock import utcnow


def platform_metrics() -> dict:
    now = utcnow().replace(tzinfo=None)
    cutoffs = {"7d": now - timedelta(days=7), "30d": now - timedelta(days=30), "all": None}
    internal = _env_list("SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS")
    domains = {value.lower().lstrip("@") for value in _env_list("SUPYMEM_ANALYTICS_INTERNAL_EMAIL_DOMAINS")}

    def external(owner):
        return or_(owner.is_(None), owner.not_in(internal))

    def totals(session, value, timestamp, *conditions):
        return dict(zip(cutoffs, session.execute(select(*[
            func.coalesce(func.sum(case((timestamp >= cutoff, value), else_=0)), 0)
            if cutoff else func.coalesce(func.sum(value), 0)
            for cutoff in cutoffs.values()
        ]).where(timestamp < now, *conditions)).one()))

    with db()() as session:
        if domains:
            email_domain = func.lower(func.substr(Person.email, func.instr(Person.email, "@") + 1))
            identity_domain = func.lower(func.substr(PersonIdentity.email, func.instr(PersonIdentity.email, "@") + 1))
            if session.get_bind().dialect.name == "postgresql":
                email_domain = func.lower(func.split_part(Person.email, "@", 2))
                identity_domain = func.lower(func.split_part(PersonIdentity.email, "@", 2))
            internal.update(session.scalars(select(Person.id).where(email_domain.in_(domains))))
            internal.update(session.scalars(select(PersonIdentity.person_id).where(identity_domain.in_(domains))))

        people = select(Person.id).where(external(Person.id), Person.created_at < now)
        users_total = session.scalar(select(func.count()).select_from(people.subquery()))
        signups = totals(session, 1, Person.created_at, external(Person.id))
        day = func.date(Person.created_at)
        daily = [{"day": str(date), "users": count} for date, count in session.execute(
            select(day, func.count()).where(external(Person.id), Person.created_at < now)
            .group_by(day).order_by(day)
        )]

        account_owner = func.coalesce(CreditAccount.owner_person_id, Workspace.owner_id)
        accounts = select(CreditAccount.id).outerjoin(Workspace, Workspace.id == CreditAccount.workspace_id).where(
            external(account_owner)
        )
        ledger_scope = CreditLedgerEntry.account_id.in_(accounts)
        consumed = totals(session, -CreditLedgerEntry.amount_micro, CreditLedgerEntry.created_at,
                          ledger_scope, CreditLedgerEntry.kind.in_(("spend", "refund", "release")))
        granted = totals(session, CreditLedgerEntry.amount_micro, CreditLedgerEntry.created_at,
                         ledger_scope, CreditLedgerEntry.kind.in_(("grant", "allowance")))
        held, reserved = session.execute(select(
            func.coalesce(func.sum(CreditAccount.granted_micro - CreditAccount.spent_micro), 0),
            func.coalesce(func.sum(CreditAccount.reserved_micro), 0),
        ).where(CreditAccount.id.in_(accounts))).one()

        props = cast(AnalyticsEvent.props_json, JSON)
        def prop(key):
            if session.get_bind().dialect.name == "sqlite":
                return func.json_extract(case((func.json_valid(AnalyticsEvent.props_json),
                                               AnalyticsEvent.props_json), else_="{}"), f"$.{key}")
            return props[key].as_string()

        slug = func.coalesce(case(CONTROL_SLUG, value=prop("control_id")),
                             case(ACTION_SLUG, value=prop("tool")),
                             case(LABEL_ALIAS, value=prop("feature")), prop("feature"))
        # ponytail: pre-control client clicks omitted to avoid duplicate counts; paired event IDs enable historical inclusion.
        use_event = or_(AnalyticsEvent.event_name.in_(("ui.control_activate", "chat.tool_invoked")),
                        (AnalyticsEvent.event_name == "feature.used")
                        & ~AnalyticsEvent.id.startswith("usage_", autoescape=True)
                        & prop("tool").is_(None))
        uses = select(AnalyticsEvent.person_id, AnalyticsEvent.occurred_at, slug.label("feature")).where(
            AnalyticsEvent.person_id.in_(people), AnalyticsEvent.occurred_at < now,
            use_event,
        ).cte("uses")
        features = {window: [] for window in cutoffs}
        counts = []
        for cutoff in cutoffs.values():
            counts.extend([
                func.count(case((uses.c.occurred_at >= cutoff, 1))) if cutoff else func.count(),
                func.count(func.distinct(case((uses.c.occurred_at >= cutoff, uses.c.person_id))))
                if cutoff else func.count(func.distinct(uses.c.person_id)),
            ])
        for feature, *values in session.execute(select(uses.c.feature, *counts).where(
            uses.c.feature.in_(FEATURES)
        ).group_by(uses.c.feature)):
            for index, window in enumerate(cutoffs):
                count, users = values[index * 2:index * 2 + 2]
                if count:
                    features[window].append({"feature": feature, "title": FEATURES[feature][1], "count": count, "users": users})
        for ranking in features.values():
            ranking.sort(key=lambda row: (-row["count"], row["feature"]))
        active = dict(zip(cutoffs, session.execute(select(*[
            func.count(func.distinct(case((AnalyticsEvent.occurred_at >= cutoff, AnalyticsEvent.person_id))))
            if cutoff else func.count(func.distinct(AnalyticsEvent.person_id))
            for cutoff in cutoffs.values()
        ]).where(AnalyticsEvent.person_id.in_(people), AnalyticsEvent.occurred_at < now,
                 or_(AnalyticsEvent.event_name.in_(ACTIVE_EVENTS), use_event))).one()))

        external_workspaces = select(Workspace.id).where(external(Workspace.owner_id))
        ventures = totals(session, 1, Venture.created_at, Venture.workspace_id.in_(external_workspaces))
        published = totals(session, 1, AnalyticsEvent.occurred_at,
                           AnalyticsEvent.event_name == "site.published",
                           AnalyticsEvent.workspace_id.in_(external_workspaces))
        waitlist = totals(session, 1, VentureWaitlistEntry.created_at,
                          VentureWaitlistEntry.workspace_id.in_(external_workspaces),
                          VentureWaitlistEntry.is_test.is_(False))
        outreach = {
            direction: totals(session, 1, GtmTouch.sent_at,
                              GtmTouch.workspace_id.in_(external_workspaces), GtmTouch.simulated == 0,
                              GtmTouch.direction == direction)
            for direction in ("out", "in")
        }

    return {
        "as_of": now.isoformat() + "Z", "users_total": users_total,
        "active": active, "signups": signups, "signups_daily": daily,
        "credits": {
            "consumed": {key: int(value) / MICRO_PER_CREDIT for key, value in consumed.items()},
            "granted": {key: int(value) / MICRO_PER_CREDIT for key, value in granted.items()},
            "held": int(held) / MICRO_PER_CREDIT, "reserved": int(reserved) / MICRO_PER_CREDIT,
            "available": int(held - reserved) / MICRO_PER_CREDIT,
        },
        "features": features,
        "extras": {"ventures_created": ventures, "landing_pages_published": published,
                   "waitlist_signups": waitlist, "outreach_sent": outreach["out"], "outreach_replies": outreach["in"]},
    }
