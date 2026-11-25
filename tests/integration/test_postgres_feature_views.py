"""Feature usage and error views, rank with zero-use features, daily rollup and internal split on PostgreSQL."""
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import text

from tests.integration.test_postgres_auth_security import ROOT, migration_head, pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def _seed(d, monkeypatch):
    from app.analytics.rollup import sync_reference_tables
    from app.db.models import AnalyticsEvent
    from core.clock import utcnow

    with d["engine"].begin() as conn:  # the database is shared by the module; start each test clean
        conn.execute(text("DELETE FROM analytics_events"))
        conn.execute(text("DELETE FROM analytics_feature_daily"))
        conn.execute(text("DELETE FROM analytics_error_daily"))
    monkeypatch.setenv("SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS", "usr_team_1")
    sync_reference_tables()
    tag, now = uuid4().hex[:8], utcnow().replace(tzinfo=None)
    ext, team, vid = "usr_ext_" + tag, "usr_team_1", d["vid"]
    n = [0]

    def add(name, props, person=ext, prefix="usage_", venture=vid):
        n[0] += 1
        with d["factory"]() as session:
            session.add(AnalyticsEvent(id=f"{prefix}{tag}{n[0]}", person_id=person, workspace_id=d["wid"],
                venture_id=venture, event_name=name,
                props_json=props if isinstance(props, str) else json.dumps(props), occurred_at=now))
            session.commit()

    add("ui.screen_view", {"feature": "website"})
    add("ui.screen_view", {"feature": "website"}, person=team)
    add("ui.control_activate", {"feature": "website", "control_id": "waitlist_enable_website"})  # old client
    add("ui.control_activate", {"feature": "waitlist", "control_id": "waitlist_enable_website"})  # new client
    add("ui.control_activate", {"feature": "website", "control_id": "website_publish_confirm"}, person=team)
    add("ui.control_exposure", {"feature": "website", "control_id": "website_publish_confirm"})
    add("chat.tool_invoked", {"source": "chat", "tool": "experiment.start", "feature": "idea", "outcome": "returned"},
        prefix="ane_")
    add("feature.used", {"source": "chat", "tool": "experiment.start", "feature": "idea"}, prefix="ane_")
    add("chat.tool_invoked", {"source": "chat", "tool": "idea.confirm", "feature": "chat", "outcome": "raised"},
        prefix="ane_")
    add("feature.used", {"feature": "planning"})  # generic client click
    add("feature.used", {"feature": "voice_chat", "source": "voice"}, prefix="ane_")
    add("ui.screen_view", "not json at all")  # a malformed row never breaks the view
    add("ui.screen_view", {"feature": "totally_new_label"})
    # errors
    add("ui.error", {"feature": "planning", "error_code": "request_error", "error_context": "api",
                     "error_text": "API request_failed (status 404)", "error_operation": "venture_list_read"})
    add("ui.error", {"feature": "idea", "error_code": "job_error", "error_context": "job", "error_text": "Job failure",
                     "job_id": "job_dup"})
    add("ui.error", {"feature": "execution", "error_code": "ui_error", "error_context": "ui", "error_text": "request outcome unknown",
                     "error_source": "tasks.run", "error_intent": "task_run", "error_page": "/v/{id}/tasks",
                     "app_version": "0.1.54", "error_occurred_at": "2026-10-01T12:00:00.000Z"})
    add("ui.error", {"feature": "execution", "error_code": "native_error", "error_context": "native", "error_text": "Mac command_failed",
                     "error_severity": "transient", "error_repeat_count": 4})
    add("job.failed", {"job_id": "job_dup", "kind": "idea.validate", "feature": "idea", "error_code": "handler_failed:KeyError",
                       "attempt": 1, "final": False}, prefix="ane_")
    add("job.failed", {"job_id": "job_dup", "kind": "idea.validate", "feature": "idea", "error_code": "plan_invalid",
                       "attempt": 2, "final": True}, prefix="ane_x")
    add("server.error", {"feature": "website", "route": "/w/{slug}/site", "status": 500, "failure_kind": "http_5xx",
                         "error_class": "none", "request_id": "req_abcdefghjkmn"}, prefix="srv_")
    add("server.error", {"route": "/old", "status": 503, "failure_kind": "unhandled_exception",
                         "error_class": "RuntimeError"}, prefix="srv_", person=None, venture=None)  # pre-attribution row
    add("server.request_error", {"feature": "waitlist", "route": "/ventures/{venture_id}/waitlist", "status": 409,
                                 "error_code": "waitlist_closed"}, prefix="srv_", person=team)
    add("model.output_rejected", {"feature": "research", "rule": "research_output_truncated",
                                  "operation_kind": "research.source_review"}, prefix="ane_")
    add("chat.reply_error", {"source": "chat", "failure_kind": "confirm_failed", "reason": "insufficient_credits",
                             "tool": "site.publish", "feature": "publish", "status": 402}, prefix="ane_")
    add("chat.reply_error", {"source": "chat", "failure_kind": "provider_call"}, prefix="ane_")
    add("build.finished", {"feature": "builds", "success": False, "error_code": "build_timeout", "outcome": "failed"},
        prefix="ane_", person=None)
    add("build.finished", {"feature": "builds", "success": True, "error_code": "none", "outcome": "succeeded"},
        prefix="ane_", person=None)
    return ext, team


def _q(d, sql, **params):
    with d["engine"].connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def test_usage_view_resolves_old_and_new_rows_to_the_same_slug_and_splits_audience(journey_db, monkeypatch):
    d = journey_db
    ext, team = _seed(d, monkeypatch)
    rows = _q(d, "SELECT slug, use_kind, audience, event_name, person_id FROM v_feature_usage_events "
                 "WHERE person_id IN (:e, :t) OR person_id IS NULL", e=ext, t=team)
    by = lambda slug, kind: [r for r in rows if r["slug"] == slug and r["use_kind"] == kind]  # noqa: E731
    assert len(by("waitlist", "use")) == 2  # old coarse label + new fine label land together
    assert len(by("publish", "use")) == 1 and by("publish", "use")[0]["audience"] == "internal"
    assert len(by("publish", "exposure")) == 1
    assert len(by("experiments", "use")) == 1 and len(by("experiments", "chat_dup")) == 1
    assert len(by("idea", "use")) == 1  # idea.confirm: registered tool outside the legacy map, resolved by the catalog
    assert len(by("voice", "use")) == 1  # voice_chat alias
    assert len(by("planning", "click")) == 1
    assert len(by("website", "view")) == 2
    assert {r["audience"] for r in by("website", "view")} == {"external", "internal"}
    assert by("unknown", "view") and by("totally_new_label", "view")  # malformed props and unknown label are kept, not dropped


def test_errors_view_unifies_every_source_and_dedupes_jobs(journey_db, monkeypatch):
    d = journey_db
    _seed(d, monkeypatch)
    rows = _q(d, "SELECT * FROM v_errors_unified")
    by = lambda **kw: [r for r in rows if all(r[k] == v for k, v in kw.items())]  # noqa: E731
    assert by(source="client", error_code="request_error", status=404)[0]["route"] == "venture_list_read"
    seen = by(error_code="ui_error")[0]
    assert (seen["error_text"], seen["error_source"], seen["intent"], seen["page_route"], seen["app_version"]) == (
        "request outcome unknown", "tasks.run", "task_run", "/v/{id}/tasks", "0.1.54")
    transient = by(severity="transient")
    assert len(transient) == 1 and transient[0]["repeat_count"] == 4 and by(source="worker")[0]["severity"] == "error"
    assert seen["client_occurred_at"].year == 2026 and by(source="worker")[0]["error_source"] is None
    jobs = by(job_id="job_dup")
    assert len(jobs) == 1 and jobs[0]["source"] == "worker" and jobs[0]["error_code"] == "plan_invalid"
    assert jobs[0]["job_kind"] == "idea.validate" and jobs[0]["feature"] == "idea"  # client duplicate dropped
    assert by(event_name="server.error", status=500)[0]["feature"] == "website"
    assert by(event_name="server.error", status=503)[0]["feature"] == "unattributed"  # pre-attribution rows still appear
    request_error = by(event_name="server.request_error")[0]
    assert (request_error["feature"], request_error["error_code"], request_error["status"]) == ("waitlist", "waitlist_closed", 409)
    assert request_error["audience"] == "internal"
    assert by(source="model")[0]["error_code"] == "research_output_truncated"
    assert {r["error_code"] for r in by(source="chat")} >= {"confirm_failed", "provider_call"}
    build = by(job_kind="build.run")
    assert len(build) == 1 and build[0]["error_code"] == "build_timeout"  # only the failed build counts


def test_rank_view_lists_every_catalog_feature_with_zero_use_last(journey_db, monkeypatch):
    from app.analytics.feature_catalog import FEATURES

    d = journey_db
    _seed(d, monkeypatch)
    rows = _q(d, "SELECT * FROM v_feature_rank_7d ORDER BY rank, slug")
    in_catalog = {r["slug"] for r in rows if r["in_catalog"]}
    assert in_catalog == set(FEATURES)
    zero = [r for r in rows if r["in_catalog"] and r["events"] == 0 and r["errors"] == 0]
    assert zero, "an unused feature must still be listed"
    ranks = [r["rank"] for r in rows]
    assert ranks == sorted(ranks)
    assert max(r["rank"] for r in rows if r["events"] > 0) < min(r["rank"] for r in zero)
    website = next(r for r in rows if r["slug"] == "website")
    assert website["views"] >= 2 and website["accounts_external"] >= 1 and website["errors"] >= 1
    drift = next(r for r in rows if r["slug"] == "totally_new_label")
    assert drift["in_catalog"] is False  # label drift is visible, not silent
    waitlist = next(r for r in rows if r["slug"] == "waitlist")
    assert waitlist["uses"] == 2 and waitlist["errors"] >= 1
    assert next(r for r in rows if r["slug"] == "experiments")["inventory_ids"].startswith("F")


def test_daily_rollup_fills_feature_and_error_tables_idempotently(journey_db, monkeypatch):
    from app.analytics.rollup import rollup_today
    from core.clock import utcnow

    d = journey_db
    _seed(d, monkeypatch)
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert len({r.day for r in pool.map(lambda _: rollup_today(), range(5))}) == 1
    today = utcnow().date()
    features = {r["slug"]: r for r in _q(d, "SELECT * FROM analytics_feature_daily WHERE day = :day", day=today)}
    assert features["waitlist"]["control_uses"] == 2 and features["waitlist"]["errors"] == 1
    assert features["publish"]["events_internal"] >= 1 and features["publish"]["feature"] == "website"
    assert features["experiments"]["chat_uses"] == 1 and features["website"]["screen_views"] >= 2
    errors = _q(d, "SELECT * FROM analytics_error_daily WHERE day = :day", day=today)
    assert {(r["source"], r["error_code"]) for r in errors} >= {("worker", "plan_invalid"), ("model", "research_output_truncated"),
                                                                ("server", "waitlist_closed")}
    assert next(r for r in errors if r["error_code"] == "waitlist_closed")["count_internal"] == 1
    total = _q(d, "SELECT count(*) AS n FROM analytics_feature_daily")[0]["n"]
    rollup_today()
    assert _q(d, "SELECT count(*) AS n FROM analytics_feature_daily")[0]["n"] == total  # no duplicates on rerun


def test_internal_accounts_come_from_config_not_code(journey_db, monkeypatch):
    from app.analytics.rollup import sync_reference_tables

    d = journey_db
    monkeypatch.setenv("SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS", "usr_a,usr_b")
    monkeypatch.setenv("SUPYMEM_ANALYTICS_INTERNAL_EMAIL_DOMAINS", "")
    sync_reference_tables()
    assert {r["person_id"] for r in _q(d, "SELECT person_id FROM analytics_internal_accounts")} == {"usr_a", "usr_b"}
    monkeypatch.delenv("SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS")
    sync_reference_tables()
    assert _q(d, "SELECT count(*) AS n FROM analytics_internal_accounts")[0]["n"] == 0


def test_migration_roundtrip_drops_and_restores_views_and_keeps_events(journey_db, monkeypatch):
    from alembic import command
    from alembic.config import Config

    d = journey_db
    _seed(d, monkeypatch)
    before = _q(d, "SELECT count(*) AS n FROM analytics_events")[0]["n"]
    cfg = Config(str(ROOT / "services/company_os/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "services/company_os/app/db/migrations"))
    names = "('v_feature_usage_events','v_errors_unified','v_feature_rank_7d')"
    tables = ("analytics_feature_catalog", "analytics_feature_map", "analytics_internal_accounts",
              "analytics_feature_daily", "analytics_error_daily")
    for _ in range(2):
        command.downgrade(cfg, "s024entrychat")
        assert _q(d, f"SELECT viewname FROM pg_views WHERE viewname IN {names}") == []
        assert not {t for t in tables if _q(d, "SELECT to_regclass(:t) AS r", t=t)[0]["r"]}
        assert _q(d, "SELECT count(*) AS n FROM analytics_events")[0]["n"] == before
        command.upgrade(cfg, "head")
        assert len(_q(d, f"SELECT viewname FROM pg_views WHERE viewname IN {names}")) == 3
        assert _q(d, "SELECT count(*) AS n FROM analytics_feature_catalog")[0]["n"] > 40
    assert _q(d, "SELECT version_num FROM alembic_version")[0]["version_num"] == migration_head()
    assert _q(d, "SELECT indexname FROM pg_indexes WHERE indexname = 'ix_analytics_events_name_time'")


def _verify_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("verify_feature_usage", ROOT / "scripts/analytics/verify_feature_usage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verify_script_payload_is_read_only_and_lists_zero_use_features(journey_db, monkeypatch):
    import pytest
    from sqlalchemy.exc import DBAPIError

    from app.analytics.feature_catalog import FEATURES, reference_rows

    d = journey_db
    _seed(d, monkeypatch)
    verify = _verify_module()
    catalog = {r["slug"]: r["inventory_ids"] for r in reference_rows()[0]}
    for baseline in (False, True):
        with d["engine"].connect() as conn:
            rows = list(verify.collect(conn, 24, catalog=catalog, force_baseline=baseline))
            with pytest.raises(DBAPIError):  # the session is read-only even if the payload tried to write
                conn.execute(text("DELETE FROM analytics_events"))
        features = {r["slug"]: r for r in rows if r["t"] == "feature"}
        assert set(FEATURES) <= set(features) and rows[0]["views"] is (not baseline) and rows[-1] == {"t": "done"}
        assert features["experiments"]["inventory"].startswith("F")
        seen = [r for r in rows if r["t"] == "user_error"]
        assert [(r["error_source"], r["intent"], r["n"]) for r in seen] == [("tasks.run", "task_run", 1)]
        assert [(r["context"], r["n"], r["repeats"]) for r in rows if r["t"] == "transient_error"] == [("native", 1, 4)]
        assert not [r for r in rows if r["t"] == "error" and r["error_code"] == "native_error"]  # transients are not counted
        if baseline:
            assert features["website"]["views"] >= 2  # coarse labels from raw events
            continue
        assert features["waitlist"]["uses"] == 2 and features["publish"]["uses"] == 1
        assert features["publish"]["ext_accounts"] == 0 and features["simulate"]["views"] == 0
        assert any(r["t"] == "error" and r["source"] == "worker" and r["error_code"] == "plan_invalid" for r in rows)
        assert {r["source"]: r["n"] for r in rows if r["t"] == "dbcount"} == {"client": 4, "server": 3, "worker": 3, "model": 1, "chat": 1}
        health = next(r for r in rows if r["t"] == "health")
        assert health["catalog_rows"] == len(FEATURES) and health["internal_accounts"] == 1
        text_report = verify.render(rows, None, 24)
        assert "ZERO USE" in text_report and "plan_invalid" in text_report
        assert not any("person" in json.dumps(r, default=str) or "usr_" in json.dumps(r, default=str) for r in rows)


def test_error_digest_groups_by_component_source_intent_code_and_splits_audience(journey_db, monkeypatch):
    from app.db.models import AnalyticsEvent
    from core.clock import utcnow

    d = journey_db
    ext, team = _seed(d, monkeypatch)
    now = utcnow().replace(tzinfo=None)
    native = {"feature": "shell", "error_code": "native_error", "error_context": "native", "error_text": "Mac process_crashed",
              "error_source": "desktop_shell", "error_intent": "previous_session"}
    with d["factory"]() as session:
        for i, (person, version) in enumerate(((ext, "0.1.50"), (ext, "0.1.51"), (team, "0.1.51"))):
            session.add(AnalyticsEvent(id=f"usage_dig{i}", person_id=person, workspace_id=d["wid"], venture_id=None,
                event_name="ui.error", props_json=json.dumps({**native, "app_version": version}), occurred_at=now))
        session.commit()
    verify = _verify_module()
    with d["engine"].connect() as conn:
        rows = [r for r in verify.collect(conn, 24) if r["t"] == "digest"]
    crash = next(r for r in rows if r["component"] == "desktop" and r["source"] == "desktop_shell")
    assert (crash["intent"], crash["code"], crash["n"]) == ("previous_session", "native_error", 3)
    assert (crash["internal_accounts"], crash["external_accounts"]) == (1, 1) and crash["versions"] == "0.1.50,0.1.51"
    assert crash["first_seen"] and crash["last_seen"]
    server = next(r for r in rows if r["component"] == "core_api" and r["source"] == "/w/{slug}/site")
    assert (server["intent"], server["code"]) == ("website", "http_5xx")
    assert any(r["component"] == "worker" and r["source"] == "idea.validate" and r["code"] == "plan_invalid" for r in rows)
    assert any(r["component"] == "worker" and r["source"] == "build" and r["code"] == "build_timeout" for r in rows)
    assert not any("usr_" in json.dumps(r, default=str) for r in rows)
    assert "Error digest" in verify.render(list(rows), None, 24) and "previous_session" in verify.render(list(rows), None, 24)


def test_worker_failure_writes_job_failed_row_with_venture_and_account(journey_db, monkeypatch):
    import asyncio
    import logging

    from app.core.jobs import lease
    from app.worker import main as worker

    d = journey_db

    async def boom(kind, payload):
        raise worker.FatalError("plan_invalid")

    monkeypatch.setattr(worker, "dispatch", boom)
    lines = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logger = logging.getLogger("app.error_event")  # a handler on the logger itself; no caplog/propagation needed
    handler, old = Collect(level=logging.DEBUG), (logger.level, logger.disabled)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    with d["factory"]() as session:
        jobs = [j for j in lease(session, "w-feature-views", kinds=["plan.compile"], n=50) if j.venture_id == d["vid"]]
        session.commit()
    assert jobs
    try:
        asyncio.run(worker._run_one(d["factory"], jobs[0], {jobs[0].id}))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old[0])
        logger.disabled = old[1]
    rows = _q(d, "SELECT person_id, venture_id, workspace_id, props_json FROM analytics_events "
                 "WHERE event_name = 'job.failed' AND venture_id = :v", v=d["vid"])
    assert len(rows) == 1
    props = json.loads(rows[0]["props_json"])
    assert props["kind"] == "plan.compile" and props["feature"] == "planning" and props["error_code"] == "plan_invalid"
    assert props["job_id"] == jobs[0].id and props["final"] is True and props["terminal_status"] == "failed_dead"
    assert (rows[0]["venture_id"], rows[0]["workspace_id"]) == (d["vid"], d["wid"])
    assert rows[0]["person_id"] == d["actor"]
    line = next(line for line in lines if line.startswith("error_event "))
    assert '"feature": "planning"' in line and '"code": "plan_invalid"' in line and d["actor"] not in line
    unified = _q(d, "SELECT source, feature, error_code, job_kind FROM v_errors_unified WHERE venture_id = :v", v=d["vid"])
    assert unified == [{"source": "worker", "feature": "planning", "error_code": "plan_invalid", "job_kind": "plan.compile"}]
