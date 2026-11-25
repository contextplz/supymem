"""Usage replay and concurrent daily summaries on migrated PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def test_usage_replay_and_parallel_rollup(journey_db):
    from app.analytics.rollup import feature_views, rollup_today, workflow_outcomes
    from app.analytics.usage import capture
    d = journey_db
    event = {"event": "ui.screen_view", "event_id": str(uuid4()), "venture_id": d["vid"],
             "context_subject": d["actor"], "props": {"feature": "planning", "platform": "macos", "session_id": str(uuid4())}}
    with ThreadPoolExecutor(max_workers=5) as pool:
        receipts = list(pool.map(lambda _: capture(event, d["actor"]), range(5)))
    assert len({receipt["id"] for receipt in receipts}) == 1
    views = feature_views()
    assert views == [{"feature": "planning", "views": 1, "people": 1}]
    with ThreadPoolExecutor(max_workers=5) as pool:
        days = list(pool.map(lambda _: rollup_today().day, range(5)))
    assert len(set(days)) == 1
    outcomes = workflow_outcomes()
    assert any(row["workflow"] == "plan.compile" for row in outcomes)


def test_public_venture_events_never_establish_payment_and_rollup_is_concurrent(journey_db):
    from app.db.supymem_models import AppEvent, FounderApp
    from app.product_analytics.service import (
        overview,
        retention,
        revenue,
        rollup_app_day,
    )
    from core.clock import utcnow
    d = journey_db
    app_id = 'app_' + uuid4().hex
    with d['factory']() as session:
        session.add(FounderApp(id=app_id, workspace_id=d['wid'], venture_id=d['vid'],
            slug=app_id, name='Analytics test', kind='web', state='draft',
            created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        for name, props in [('page_view', '{}'), ('signup', '{}'), ('payment', '{"amount":"invalid"}')]:
            session.add(AppEvent(app_id=app_id, workspace_id=d['wid'], ts=utcnow(), name=name, props=props))
        session.commit()
    with ThreadPoolExecutor(max_workers=5) as pool:
        rows = list(pool.map(lambda _: rollup_app_day(app_id), range(5)))
    assert all(row['visits'] == 1 and row['reported_payment_events'] == 1 and row['revenue_minor'] is None for row in rows)
    report = overview(app_id)
    assert report['status'] == 'unverified' and report['today']['revenue_minor'] is None
    assert revenue(app_id)['amount_minor'] is None
    assert retention(app_id)['weekly_active'] is None


def test_venture_activity_snapshot_is_scoped_and_view_is_not_action(journey_db):
    from datetime import timedelta

    from app.analytics.snapshot import snapshot
    from app.authz.workspace import ensure_subject_person
    from app.db.models import AnalyticsEvent, Charter, ChatMessage, Job, Venture
    from app.db.supymem_models import FounderRequest
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.ids import new_id
    d = journey_db
    stranger = "usr_pg_other_" + uuid4().hex[:10]
    ensure_subject_person(stranger)
    other_workspace = create_workspace(kind="company", name="Other analytics tenant", owner_id=stranger)
    other_venture = new_id("ven")
    active_charter = new_id("chr")
    other_charter = new_id("chr")
    now = utcnow()
    with d["factory"]() as session:
        session.add(Venture(id=other_venture, workspace_id=other_workspace.id, name="Other product",
                            raw_idea="Other test product", stage="draft", autonomy_level="L1",
                            active_charter_id=other_charter,
                            created_at=now, updated_at=now))
        session.flush()
        session.add(Charter(id=active_charter, venture_id=d["vid"], version="analytics-active",
                            locked=1, content_json='{"build_archetype":"integration_bot"}',
                            checksum="analytics", slots_snapshot_json="{}", dissent_json="{}", created_at=now))
        session.add(Charter(id=new_id("chr"), venture_id=d["vid"], version="analytics-newer",
                            locked=0, content_json='{"build_archetype":"marketplace"}',
                            checksum="analytics", slots_snapshot_json="{}", dissent_json="{}",
                            created_at=now + timedelta(seconds=1)))
        session.add(Charter(id=other_charter, venture_id=other_venture,
                            version="analytics-other", locked=1,
                            content_json='{"build_archetype":"marketplace"}', checksum="analytics",
                            slots_snapshot_json="{}", dissent_json="{}", created_at=now))
        ours = session.get(Venture, d["vid"])
        assert ours is not None
        ours.active_charter_id = active_charter
        session.add(AnalyticsEvent(id=new_id("ane"), person_id=d["actor"], workspace_id=d["wid"],
                                   venture_id=d["vid"], event_name="venture.created", props_json="{}", occurred_at=now))
        session.add(AnalyticsEvent(id=new_id("ane"), person_id=stranger, workspace_id=other_workspace.id,
                                   venture_id=other_venture, event_name="venture.created", props_json="{}", occurred_at=now))
        session.add(AnalyticsEvent(id="usage_" + uuid4().hex, person_id=d["actor"], workspace_id=d["wid"],
                                   venture_id=d["vid"], event_name="ui.screen_view",
                                   props_json='{"feature":"analytics","platform":"macos","session_id":"one"}', occurred_at=now))
        runtime_session = str(uuid4())
        for seconds in (60, 0):
            session.add(AnalyticsEvent(id="usage_" + uuid4().hex, person_id=d["actor"],
                                       workspace_id=None, venture_id=None,
                                       event_name="desktop.process_heartbeat",
                                       props_json='{"app_version":"0.1.48","feature":"settings","platform":"macos","session_id":"' + runtime_session + '"}',
                                       occurred_at=now - timedelta(seconds=seconds)))
        session.add(Job(id=new_id("job"), kind="gtm.god.run", venture_id=d["vid"], workspace_id=d["wid"],
                        payload_json='{"_actor_subject":"' + d["actor"] + '"}', status="leased", priority=0,
                        attempts=1, max_attempts=3, timeout_s=900, started_at=now, created_at=now))
        for vid, wid, category in ((d["vid"], d["wid"], "planning"),
                                   (other_venture, other_workspace.id, "import")):
            session.add(FounderRequest(id=new_id("frq"), workspace_id=wid, venture_id=vid,
                source="chat", text="private founder request", intent="test",
                intent_category=category, classification="unknown", outcome="done", created_at=now))
            session.add(ChatMessage(id=new_id("chm"), venture_id=vid, workspace_id=wid,
                role="user", kind="text", body_md="private chat message", created_at=now))
        session.commit()
    ai = snapshot(venture_id=d["vid"])
    other = snapshot(venture_id=other_venture)
    assert ai["venture_id"] == d["vid"] and ai["activity"]["dau"] == 1
    assert ai["features"] and any(row["feature"] == "analytics" and row["successes"] == 0 for row in ai["features"])
    assert ai["agents"]["in_progress_by_type"]["gtm_diagnosis"] >= 1
    assert ai["agents"]["attributed_starting_people"] >= 1
    assert ai["attention"]["process_uptime_seconds"] is None
    assert ai["founder_requests"]["saved_by_category"] == {"planning": 1}
    assert ai["chat"]["saved_messages_by_role"] == {"user": 1}
    assert other["founder_requests"]["saved_by_category"] == {"import": 1}
    assert "private founder request" not in str(ai)
    assert "private chat message" not in str(ai)
    assert all(row["venture_id"] == d["vid"] for row in ai["venture_ranking"])
    assert other["activity"]["dau"] == 1
    operator = snapshot()
    assert operator["attention"]["process_uptime_seconds"] == 60
    portfolio = operator["venture_portfolio"]
    assert portfolio is not None
    assert portfolio["by_build_archetype"]["integration_bot"] >= 1
    assert portfolio["by_build_archetype"]["marketplace"] >= 1
    assert sum(portfolio["by_build_archetype"].values()) == portfolio["nonpurged_total"]
    assert ai["venture_portfolio"] is None
    # Scoped reads are an internal operator query only. Founders have no route
    # to this platform dataset; tenant product analytics remain separate.
