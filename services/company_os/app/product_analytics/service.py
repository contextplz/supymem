"""Founder product analytics ingest + dashboards (docs/supymem/16)."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.apps.keys import lookup_bearer
from app.core.local_mode import is_local
try:
    from app.core.local_capabilities import enabled, blocked_message
except ImportError:
    def enabled(name: str) -> bool:
        return name == "page_measurement"

    def blocked_message(name: str) -> str:
        return "Turn on Page measurement in Settings > Features, then record your page counts in chat."
from app.db.supymem_models import (
    AppEvent,
    AppFunnel,
    AppIncident,
    AppMetricDaily,
    AppUsageRecord,
    FounderApp,
)
from app.desktop_notices import enqueue as notice
from app.secrets.detect import looks_like_secret
from app.state import db
from core.clock import utcnow

_EVENT = re.compile(r"^[a-z_]{1,40}$")
_DEFAULT_FUNNEL = ["landing_view", "cta_click", "signup", "first_action", "payment"]
_LOCAL_EVENTS = {"landing_view", "page_view", "cta_click", "signup"}
_LOCAL_PROPS = {"path", "referrer_host", "utm_source", "utm_medium", "utm_campaign", "utm_content",
    "variant", "experiment_id", "price_point", "ref"}


def ingest_public(body: dict[str, Any]) -> bool:
    """Return True if stored. Unknown key → False (caller returns 204)."""
    if is_local() and not enabled("page_measurement"):
        return False
    key = str(body.get("key") or "")
    if is_local() and not key.startswith("pk_app_"):
        return False
    found = lookup_bearer(key)
    if found is None:
        return False
    if not str(found.get("prefix") or "").startswith("pk_app_"):
        return False
    event = str(body.get("event") or body.get("name") or "")
    if event in {"survey_response", "request_submission"}:
        return False
    if not _EVENT.match(event):
        return False
    props = body.get("props") if isinstance(body.get("props"), dict) else {}
    if len(json.dumps(props)) > 4096 or len(props) > 20:
        return False  # Oversize values remain oversize if only keys are truncated.
    if is_local():
        if event not in _LOCAL_EVENTS or any(field in props for field in ("email", "password", "token", "api_key")):
            return False
        props = {field: value for field, value in props.items() if field in _LOCAL_PROPS}
        if any(not isinstance(value, str) or len(value) > 200 or "@" in value or looks_like_secret(value)
                or any(ord(character) < 32 for character in value) for value in props.values()):
            return False
    factory = db()
    now = utcnow()
    with factory() as session:
        row = AppEvent(
            app_id=found["app_id"],
            workspace_id=found["workspace_id"],
            ts=now,
            name=event,
            props=json.dumps(props, default=str),
        )
        session.add(row)
        session.commit()
    if not is_local():
        _maybe_alert(found, event)
    return True


def record_counts(venture_id: str, payload: dict) -> dict:
    from app.db.models import Venture
    from app.db.session import serialize_writes
    from app.db.supymem_models import Experiment
    from app.product_analytics.target_app import select_venture_app
    from app.tenancy import current_or_none

    if not is_local():
        raise ValueError("Open your local app, then record your page counts in chat.")
    if not enabled("page_measurement"):
        raise ValueError(blocked_message("page_measurement"))
    if not isinstance(payload, dict) or set(payload) - {"experiment_id", "path", "variant", "channel", "start_date", "end_date", "views", "clicks", "signups"}:
        raise ValueError("Record views, clicks and signups with a page path and start_date/end_date in chat, then try again.")
    if not all(field in payload for field in ("views", "clicks", "signups")):
        raise ValueError("Record all three counts, views, clicks and signups, in chat so missing numbers are not treated as zero.")
    counts = {field: payload[field] for field in ("views", "clicks", "signups")}
    if any(type(value) is not int or not 0 <= value <= 1_000_000_000 for value in counts.values()):
        raise ValueError("Record views, clicks and signups as non-negative whole numbers in chat, then try again.")
    try:
        start, end = (date.fromisoformat(payload[field]) for field in ("start_date", "end_date"))
        if start > end:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("Record a past or current date range as YYYY-MM-DD in chat, then try again.") from None
    dimensions = {field: payload.get(field, "") for field in ("experiment_id", "path", "variant", "channel")}
    dimensions["path"] = dimensions["path"] or "/"
    if (any(not isinstance(value, str) or len(value) > 200 or "@" in value or "\\" in value or looks_like_secret(value)
            or any(ord(character) < 32 for character in value) for value in dimensions.values())
            or not dimensions["path"].startswith("/") or any(character in dimensions["path"] for character in "?#")
            or dimensions["path"].startswith("//")):
        raise ValueError("Record a page path without a query string or personal information in chat, then try again.")
    with db()() as session:
        serialize_writes(session, key="page-measurement:" + venture_id)
        venture = session.get(Venture, venture_id)
        scope = current_or_none()
        if (not venture or venture.archived_at or venture.stage in {"purged", "archived", "paused"}
                or scope and (scope.workspace_id not in {"none", venture.workspace_id}
                    or scope.venture_id and scope.venture_id != venture_id)):
            raise ValueError("Open your active venture, then record its page counts in chat.")
        app = select_venture_app(session, venture_id, venture.workspace_id)
        if app is None:
            raise ValueError("Build your venture's page on the Site screen, then record its counts in chat.")
        spec = {}
        allowed_dates = None
        if dimensions["experiment_id"]:
            from app.experiments.page_tests import measurement_publication, observation_window, uses_publication_window

            experiment = session.get(Experiment, dimensions["experiment_id"])
            context = json.loads(experiment.context or "{}") if experiment else {}
            spec = context.get("spec", {})
            if (not experiment or (experiment.venture_id, experiment.workspace_id, spec.get("app_id")) != (venture_id, venture.workspace_id, app.id)
                    or spec.get("scan") not in {"page_test", "channel_test"}):
                raise ValueError("Choose a page or channel test in your venture, then record its counts in chat.")
            choices = {item["key"] for item in spec.get("variants", [])}
            if spec.get("channel_kind") == "referral":
                choices = set(json.loads(experiment.context).get("cohort", {}).values())
            selected = dimensions["channel"] if spec.get("scan") == "channel_test" else dimensions["variant"]
            if not choices and spec.get("channel_kind") == "referral":
                raise ValueError("Collect a consented signup on Site > Waitlist, then prepare your referral test in chat before recording its counts.")
            if selected not in choices:
                raise ValueError("Choose one of your test's variants or referral links, then record its counts in chat.")
            publication = measurement_publication(session, spec) if uses_publication_window(spec) else None
            earliest, latest = (instant.date() for instant in observation_window(spec, context, publication=publication))
            if not earliest <= start <= end <= latest:
                raise ValueError(f"In chat, record counts from {earliest} through {latest} for this test. Use only totals from within those dates.")
            allowed_dates = (earliest.isoformat(), latest.isoformat())
        elif end > utcnow().date():
            raise ValueError("Record a past or current date range as YYYY-MM-DD in chat, then try again.")
        existing = None
        # shortcut: scan your venture's JSON counts, add indexed dimensions when manual history makes readouts slow.
        for row in session.scalars(select(AppEvent).where(AppEvent.app_id == app.id,
                AppEvent.workspace_id == venture.workspace_id, AppEvent.name == "manual_counts")):
            props = json.loads(row.props)
            if allowed_dates and not _within_count_range(props, allowed_dates):
                continue
            same_range = props["start_date"] == start.isoformat() and props["end_date"] == end.isoformat()
            overlapping = props["start_date"] <= end.isoformat() and props["end_date"] >= start.isoformat()
            if (spec.get("page_kind") == "pricing" and overlapping and not same_range
                    and props.get("experiment_id") == dimensions["experiment_id"]
                    and props.get("path", "").rstrip("/") == dimensions["path"].rstrip("/")):
                raise ValueError("Use the same date range for all prices on your page in chat, or record a non-overlapping range, so shared views are not counted twice.")
            if any(props.get(field, "") != value for field, value in dimensions.items()):
                continue
            if same_range:
                existing = row
            elif overlapping:
                raise ValueError("Record a non-overlapping date range, or reuse the exact saved range to correct your counts in chat.")
        row = existing or AppEvent(app_id=app.id, workspace_id=venture.workspace_id, name="manual_counts", ts=utcnow())
        row.props = json.dumps({**dimensions, **counts, "start_date": start.isoformat(), "end_date": end.isoformat()})
        session.add(row)
        session.commit()
    if dimensions["experiment_id"]:
        from app.experiments import channel_tests, page_tests

        try:
            readout = channel_tests.readout if spec.get("scan") == "channel_test" else page_tests.readout
            result = readout(venture_id, {"experiment_id": dimensions["experiment_id"]})
        except Exception:
            return {"kind": "text", "body_md": "Your page counts are saved on your laptop, but your test result could not be refreshed. Open Experiments, choose your test and refresh your results.",
                "actions": [], "citations": [], "counts": counts}
        return {**result, "body_md": "Your page counts are saved on your laptop. Your manual totals replace events for the same page and date range. " + result["body_md"], "counts": counts}
    next_step = "Choose your page or channel test in chat, then use Record your page counts on its readout to attribute these numbers to that test."
    return {"kind": "text", "body_md": "Your page counts are saved on your laptop. Your manual totals replace events for the same page and date range. " + next_step,
        "actions": [], "citations": [], "counts": counts}


def _within_count_range(props, allowed_dates):
    return allowed_dates[0] <= props.get("start_date", "") <= props.get("end_date", "") <= allowed_dates[1]


def measurement_counts(spec: dict, context: dict, events: list, entries: list, *, window=None) -> tuple[dict, list[str], int]:
    from urllib.parse import urlsplit
    from core.clock import as_utc
    from app.experiments.page_tests import observation_window

    channel_test = spec.get("scan") == "channel_test"
    referral = spec.get("channel_kind") == "referral"
    pricing = not channel_test and spec.get("page_kind") == "pricing"
    keys = set(context.get("cohort", {}).values()) if referral else {item["key"] for item in spec["variants"]}
    totals = {key: {"views": 0, "clicks": 0, "signups": 0} for key in keys}
    sources = set()
    manual = []
    shared_page_views = {}
    total_views = 0
    allowed_dates = None
    for event in events:
        props = json.loads(event.props or "{}")
        if event.name == "manual_counts" and props.get("experiment_id") == spec["id"]:
            if allowed_dates is None:
                allowed_dates = tuple(instant.date().isoformat() for instant in (window or observation_window(spec, context)))
            if not _within_count_range(props, allowed_dates):
                continue
            key = props.get("channel" if channel_test else "variant")
            if key in totals:
                manual.append(props)
                for field in totals[key]:
                    if not pricing or field != "views":
                        totals[key][field] += props[field]
                sources.add("manual_counts")
                if pricing:
                    page_range = (props["path"].rstrip("/"), props["start_date"], props["end_date"])
                    shared_page_views[page_range] = max(shared_page_views.get(page_range, 0), props["views"])
                else:
                    total_views += props["views"]
    shared_views = sum(shared_page_views.values())
    total_views += shared_views
    if pricing:
        for counts in totals.values():
            counts["views"] += shared_views

    def attribution(props):
        if channel_test:
            if referral:
                return props.get("referred_by") or props.get("ref")
            if props.get("utm_campaign") != spec["id"]:
                return None
            return next((item["key"] for item in spec["variants"] if item["key"] == props.get("utm_source")
                and item["medium"] == props.get("utm_medium")), None)
        if props.get("experiment_id") != spec["id"]:
            return None
        return props.get("price_point") or props.get("variant")

    def covered(props, timestamp, key, shared_view=False):
        day = as_utc(timestamp).date().isoformat()
        return any((shared_view or item.get("channel" if channel_test else "variant") == key)
            and item["path"].rstrip("/") == props.get("path", item["path"]).rstrip("/")
            and item["start_date"] <= day <= item["end_date"] for item in manual)

    consented = {key: 0 for key in keys}
    tracked_page_views = Counter()
    for entry in entries:
        props = json.loads(entry.props or "{}")
        key = attribution(props)
        if key in totals and entry.consent_text.strip() and not covered(props, entry.created_at, key):
            consented[key] += 1
    for event in events:
        if event.name not in _LOCAL_EVENTS or event.name == "signup":
            continue
        props = json.loads(event.props or "{}")
        if spec.get("page_kind") == "pricing" and event.name == "cta_click" and props.get("price_point") not in totals:
            continue
        if str(props.get("path", "")).startswith("/public/waitlists/"):
            continue
        key = attribution(props)
        event_keys = [key] if key in totals else []
        if not channel_test and spec.get("page_kind") == "pricing" and event.name in {"landing_view", "page_view"} and props.get("experiment_id") == spec["id"] and key not in totals:
            event_keys = list(keys)
        if channel_test and not referral and props.get("path", "").rstrip("/") != urlsplit(spec["site_url"]).path.rstrip("/"):
            continue
        if pricing and shared_page_views and event_keys and event.name in {"landing_view", "page_view"}:
            day = as_utc(event.ts).date().isoformat()
            for page_range in shared_page_views:
                path, start, end = page_range
                if path == props.get("path", "/").rstrip("/") and start <= day <= end:
                    tracked_page_views[page_range] += 1
        counted_view = False
        for key in event_keys:
            if covered(props, event.ts, key, shared_view=pricing and event.name in {"landing_view", "page_view"}):
                continue
            field = "views" if event.name in {"landing_view", "page_view"} else "clicks" if event.name == "cta_click" else "signups"
            totals[key][field] += 1
            sources.add("local_events")
            if field == "views" and not counted_view:
                total_views += 1
                counted_view = True
    for key, count in consented.items():
        if count:
            totals[key]["signups"] += count
            sources.add("consented_waitlist")
    if pricing:
        incomplete_ranges = {page_range for page_range, views in shared_page_views.items()
            if tracked_page_views[page_range] < max(1, views)}
        for key, counts in totals.items():
            reported = {(item["path"].rstrip("/"), item["start_date"], item["end_date"]) for item in manual if item.get("variant") == key}
            counts["clicks_complete"] = incomplete_ranges <= reported
    return totals, sorted(sources), total_views


def measurement_source_line(sources: list[str]) -> str:
    labels = {"local_events": "your local page events", "manual_counts": "your manual counts", "consented_waitlist": "your consented waitlist"}
    return " Counts use " + ", ".join(labels[source] for source in sources) + "." if sources else " No page counts are recorded yet."


def _maybe_alert(found: dict[str, Any], event: str) -> None:
    if event != "signup":
        return
    factory = db()
    with factory() as session:
        prior = session.scalar(
            select(func.count()).where(
                AppEvent.app_id == found["app_id"],
                AppEvent.name == event,
            )
        )
    if int(prior or 0) == 1:
        title = "First browser-reported signup"
        notice(
            title=title,
            body=f"{title} on app {found['app_id'][-8:]}",
            venture_id=found.get("venture_id"),
            kind="analytics",
        )


def rollup_app_day(app_id: str, day: date | None = None) -> dict[str, Any]:
    target = day or utcnow().date()
    start = utcnow().replace(tzinfo=None).replace(year=target.year, month=target.month, day=target.day, hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    factory = db()
    with factory() as session:
        app = session.get(FounderApp, app_id)
        if app is None:
            raise KeyError("app")
        visits = int(
            session.scalar(
                select(func.count()).where(
                    AppEvent.app_id == app_id,
                    AppEvent.ts >= start,
                    AppEvent.ts < end,
                    AppEvent.name.in_(("page_view", "landing_view")),
                )
            )
            or 0
        )
        signups = int(
            session.scalar(
                select(func.count()).where(
                    AppEvent.app_id == app_id,
                    AppEvent.ts >= start,
                    AppEvent.ts < end,
                    AppEvent.name == "signup",
                )
            )
            or 0
        )
        # Public browser events are observations, never financial evidence.
        pay_count = int(session.scalar(select(func.count()).where(
            AppEvent.app_id == app_id, AppEvent.ts >= start, AppEvent.ts < end,
            AppEvent.name == "payment",
        )) or 0)
        errors = int(
            session.scalar(
                select(func.count()).where(
                    AppEvent.app_id == app_id,
                    AppEvent.ts >= start,
                    AppEvent.ts < end,
                    AppEvent.name == "error",
                )
            )
            or 0
        )
        ai_calls = int(
            session.scalar(
                select(func.count()).where(
                    AppUsageRecord.app_id == app_id,
                    AppUsageRecord.window_start >= start,
                    AppUsageRecord.window_start < end,
                )
            )
            or 0
        )
        # Legacy integer columns cannot express unavailable. Never expose these
        # zeros as measured unique people or verified revenue in API responses.
        values = dict(app_id=app_id, day=target, visits=visits, signups=signups,
                      unique_visitors=0, active_users=0, requests=visits + signups + pay_count,
                      errors_5xx=errors, ai_calls=ai_calls, revenue_minor=0)
        if session.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        statement = insert(AppMetricDaily).values(**values)
        session.execute(statement.on_conflict_do_update(index_elements=["app_id", "day"],
            set_={key: value for key, value in values.items() if key not in {"app_id", "day"}}))
        session.commit()
        return {
            "app_id": app_id,
            "day": target.isoformat(),
            "visits": visits,
            "signups": signups,
            "reported_payment_events": pay_count,
            "payments": None,
            "revenue_minor": None,
            "measurement_basis": "unverified_browser_events",
            "errors": errors,
            "ai_calls": ai_calls,
        }


def rollup_all_apps() -> int:
    factory = db()
    with factory() as session:
        ids = list(session.scalars(select(FounderApp.id).where(FounderApp.deleted_at.is_(None))))
    for app_id in ids:
        rollup_app_day(app_id)
    return len(ids)


def overview(app_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        rows = list(
            session.scalars(
                select(AppMetricDaily)
                .where(AppMetricDaily.app_id == app_id)
                .order_by(AppMetricDaily.day.desc())
                .limit(14)
            )
        )
        first_signup = session.scalar(
            select(AppEvent.ts)
            .where(AppEvent.app_id == app_id, AppEvent.name == "signup")
            .order_by(AppEvent.ts.asc())
            .limit(1)
        )
    # Read-only: count today's events live instead of writing a rollup on every page view.
    today_date = utcnow().date()
    start = datetime.combine(today_date, datetime.min.time())
    with factory() as session:
        def _count(*names: str) -> int:
            return int(session.scalar(select(func.count()).where(
                AppEvent.app_id == app_id, AppEvent.ts >= start,
                AppEvent.ts < start + timedelta(days=1), AppEvent.name.in_(names))) or 0)
        live_visits, live_signups = _count("page_view", "landing_view"), _count("signup")
    return {
        "goal": "First paid user",
        "status": "unverified",
        "achieved_on": None,
        "measurement_basis": "unverified_browser_events",
        "payment_verification": "provider_receipts_not_integrated",
        "unique_visitors": None,
        "active_users": None,
        "first_signup": first_signup.isoformat() if first_signup else None,
        "today": {
            "visits": live_visits,
            "signups": live_signups,
            "revenue_minor": None,
        },
        "days": [
            {
                "day": r.day.isoformat(),
                "visits": live_visits if r.day == today_date else r.visits,
                "signups": live_signups if r.day == today_date else r.signups,
                "revenue_minor": None,
                "errors_5xx": None,
                "reported_error_events": r.errors_5xx,
                "ai_calls": r.ai_calls,
            }
            for r in rows
        ],
    }


def funnel(app_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        configured = session.scalar(select(AppFunnel).where(AppFunnel.app_id == app_id))
        steps = json.loads(configured.steps) if configured else list(_DEFAULT_FUNNEL)
        counts: dict[str, int] = {}
        for step in steps:
            counts[step] = int(
                session.scalar(
                    select(func.count()).where(AppEvent.app_id == app_id, AppEvent.name == step)
                )
                or 0
            )
    return {"steps": steps, "counts": counts, "measurement_basis": "independent_browser_event_counts_not_ordered_people"}


def events(app_id: str, *, limit: int = 50) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        rows = list(
            session.scalars(
                select(AppEvent)
                .where(AppEvent.app_id == app_id)
                .order_by(AppEvent.ts.desc())
                .limit(limit)
            )
        )
        top = Counter(r.name for r in rows).most_common(12)
    return {
        "items": [
            {"ts": r.ts.isoformat(), "name": r.name, "props": json.loads(r.props or "{}")}
            for r in rows
        ],
        "top": [{"name": n, "count": c} for n, c in top],
    }


def retention(app_id: str) -> dict[str, Any]:
    return {"weekly_active": None, "weeks": None, "status": "unavailable",
            "reason": "Distinct-person activity and cohort instrumentation are not established."}


def revenue(app_id: str) -> dict[str, Any]:
    # Never infer money, currency or first customer from a publicly writable event.
    return {"count": None, "amount_minor": None, "by_day": None, "currency": None,
            "status": "unavailable", "reason": "Verified payment provider receipts are not integrated."}


def health(app_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        open_n = int(
            session.scalar(
                select(func.count()).where(
                    AppIncident.app_id == app_id,
                    AppIncident.state.in_(("open", "diagnosed")),
                )
            )
            or 0
        )
        latest = session.scalars(
            select(AppMetricDaily)
            .where(AppMetricDaily.app_id == app_id)
            .order_by(AppMetricDaily.day.desc())
            .limit(1)
        ).first()
    return {
        "open_incidents": open_n,
        "errors_5xx": None,
        "reported_error_events": latest.errors_5xx if latest else 0,
        "measurement_basis": "unverified_browser_events",
        "p95_ms": latest.p95_ms if latest else None,
        "adapter": "local",
    }


def ai_usage(app_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        rows = list(session.scalars(select(AppUsageRecord).where(AppUsageRecord.app_id == app_id)))
    credits = sum(int(r.credits_micro) for r in rows)
    return {
        "calls": len(rows),
        "credits_micro": credits,
        "meters": sorted({r.meter for r in rows}),
    }


def traffic_sources(app_id: str, days: int = 30) -> list[dict[str, Any]]:
    """Landing views grouped by where they came from: the UTM source (outreach links carry one), else the referrer host."""
    from datetime import timedelta

    with db()() as session:
        rows = session.scalars(select(AppEvent).where(AppEvent.app_id == app_id, AppEvent.name == "landing_view",
                                                      AppEvent.ts >= utcnow() - timedelta(days=days))).all()
    out: dict[tuple[str, str], int] = {}
    for r in rows:
        try:
            p = json.loads(r.props or "{}")
        except ValueError:
            continue
        key = (str(p.get("utm_source") or p.get("referrer_host") or "direct"), str(p.get("utm_campaign") or ""))
        out[key] = out.get(key, 0) + 1
    return [{"source": s, "campaign": c, "views": n} for (s, c), n in sorted(out.items(), key=lambda kv: -kv[1])]
