import asyncio
import json
import logging
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/obs.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    main = load_company_os_app()
    from app import state
    state.reset_db_factory()
    state.db()
    return main


@pytest.mark.hosted_only
def test_content_free_receipt_idempotency_scope_and_metric(core, caplog):
    from app.analytics.usage import capture
    from app.db.models import AnalyticsEvent
    from app.state import db

    with TestClient(core.create_app()) as client:
        created = client.post("/ventures", json={"raw_idea": "Founder observability test"})
        assert created.status_code == 201, created.text
        vid = created.json()["id"]
    body = {"event": "ui.dead_end", "event_id": str(uuid4()), "venture_id": vid,
            "props": {"code": "empty_no_action", "route": "/v/{id}/idea", "feature": "idea"}}
    caplog.set_level(logging.INFO, logger="app.observability")
    first = capture(body, "local")
    assert capture(body, "local") == first
    with db()() as session:
        row = session.get(AnalyticsEvent, first["id"])
        assert json.loads(row.props_json) == body["props"]
        assert row.workspace_id is not None and row.venture_id == vid
        assert row.person_id is None
    metrics = [record for record in caplog.records if hasattr(record, "emf")]
    assert len(metrics) == 1 and metrics[0].emf["dead_end"] == 1
    with pytest.raises(Exception):
        capture({**body, "workspace_id": "foreign"}, "local")


@pytest.mark.hosted_only
def test_idea_transition_completion_is_atomic_and_backtracking_is_not_success(core):
    from app.analytics.observability import stage_step
    from app.core.fsm import advance
    from app.db.models import AnalyticsEvent
    from app.db.supymem_models import IdeaSession
    from app.state import db

    with TestClient(core.create_app()) as client:
        created = client.post("/ventures", json={"raw_idea": "Journey transition observability"})
        assert created.status_code == 201
        vid = created.json()["id"]
    with db()() as session:
        from app.db.models import Venture
        wid = session.get(Venture, vid).workspace_id
        from core.clock import utcnow
        row = IdeaSession(id="ids_obs", workspace_id=wid, venture_id=vid, state="clarifying", case="explore",
                          created_at=utcnow(), updated_at=utcnow())
        session.add(row)
        session.flush()
        advance(session, row, "validating")
        advance(session, row, "clarifying")
        advance(session, row, "validating")
        session.commit()
        events = session.query(AnalyticsEvent).filter(AnalyticsEvent.event_name.like("journey.%")).all()
        pairs = [(event.event_name, json.loads(event.props_json)["step"]) for event in events]
        assert pairs.count(("journey.step_completed", "clarify")) == 1
        assert ("journey.step_completed", "validate") not in pairs
        assert ("journey.step_entered", "validate") in pairs
        stage_step(session, "review", "completed", key="rollback", venture_id="ven_obs")
        session.rollback()
        assert session.query(AnalyticsEvent).filter(AnalyticsEvent.props_json == '{"step": "review"}').count() == 0


@pytest.mark.hosted_only
@pytest.mark.parametrize("reason,expected", [(None, "needs_founder"), ("cu_timeout", "timeout"),
    ("cu_auth_lost", "auth_lost"), ("cu_browser_decision_unavailable", "model_unavailable"),
    ("cu_permission_missing", "blocked"), ("private exception message", "needs_founder")])
def test_computer_outcomes_closed_and_once(core, reason, expected):
    from app.analytics.observability import computer_outcome, stage_computer_outcome
    from app.db.models import AnalyticsEvent
    from app.state import db

    assert computer_outcome(verified=False, reason=reason) == expected
    assert computer_outcome(verified=True, reason=reason) == "ok"
    body = {"execution_id": "dex_obs", "workspace_id": "ws_obs", "venture_id": "ven_obs",
            "native_stop": {"task_verified": False}, "stop_reason_code": reason}
    with db()() as session:
        stage_computer_outcome(session, body)
        stage_computer_outcome(session, body)
        session.commit()
        rows = session.query(AnalyticsEvent).filter_by(event_name="agent.computer_outcome").all()
        assert len(rows) == 1 and json.loads(rows[0].props_json) == {"outcome": expected}


def test_native_terminal_run_receipt_and_gateway_reuse_closed_outcome():
    from desktop_runtime.cu_reliability import outcome_code
    from desktop_runtime.device_gateway import WorkOrderGateway
    from desktop_runtime.device_worker import DeviceWorker

    receipts, calls = [], []
    gateway = WorkOrderGateway(order_id="dwo_obs", device_id="dev_obs", request=lambda *args: calls.append(args),
                               account_id="usr_obs", venture_id="ven_obs")
    gateway.claimed, gateway.execution_id = True, "dex_obs"
    worker = DeviceWorker(gateway, receipts.append)
    worker.progress("stopped", outcome="step_limit_reached", evidence={"outcome": "step_limit_reached"})
    assert receipts[-1]["outcome_code"] == "timeout"
    gateway.stop_receipt("a" * 64, True)
    assert calls[-1][2]["outcome_code"] == "timeout"
    assert outcome_code("verified", verified=False) == "needs_founder"
    assert outcome_code({"private": "content"}, failure=["private"]) == "needs_founder"


def test_emf_is_root_json_and_has_only_closed_dimensions(caplog):
    from core.error_event import observability_metric
    from core.logging import JsonFormatter

    caplog.set_level(logging.INFO, logger="app.observability")
    observability_metric("http_5xx", "idea", service="gateway")
    record = next(record for record in caplog.records if hasattr(record, "emf"))
    payload = json.loads(JsonFormatter().format(record))
    assert payload["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["feature", "service"]]
    assert payload["http_5xx"] == 1 and payload["feature"] == "idea"
    assert set(payload) == {"_aws", "feature", "service", "http_5xx"}


def test_request_trace_reaches_core_enqueue_worker_context_and_echo(core, caplog, monkeypatch):
    from app.core.jobs import JobQueue, JobSpec, enqueue, lease
    from app.db.models import Job
    from app.state import db
    from core.logging import JsonFormatter
    from core.request_id import RequestIdMiddleware, current_request_id, request_context, resolve_request_id

    rid, sent, job_ids = "req_abc012345678", [], []
    job_kind = "analytics.ingest_batch"
    async def backend(scope, receive, send):
        assert scope["state"]["request_id"] == rid == current_request_id()
        with db()() as session:
            job_ids.append(enqueue(session, job_kind, {"_trace_id": "forged"}))
            session.commit()
        await send({"type": "http.response.start", "status": 200, "headers": []})
    async def send(message):
        sent.append(message)
    caplog.set_level(logging.INFO, logger="app.request")
    scope = {"type": "http", "headers": [(b"x-request-id", rid.encode())]}
    asyncio.run(RequestIdMiddleware(RequestIdMiddleware(backend))(scope, None, send))
    assert sent[0]["headers"] == [(b"x-request-id", rid.encode())]
    with db()() as session:
        payload = json.loads(session.get(Job, job_ids[0]).payload_json)
    assert payload["_trace_id"] == rid
    from app.worker import main as worker

    children = []
    async def dispatch(kind, payload):
        assert current_request_id() == rid
        with db()() as session:
            children.append(enqueue(session, job_kind, {}))
            session.commit()
    monkeypatch.setattr(worker, "dispatch", dispatch)
    caplog.set_level(logging.INFO, logger="app.worker")
    with db()() as session:
        job = lease(session, "obs-worker")[0]
        session.commit()
    running = {(job.id, job.attempts)}
    asyncio.run(worker._run_one(db(), job, running))
    assert not running
    with db()() as session:
        assert session.get(Job, job.id).status == "succeeded"
        assert json.loads(session.get(Job, children[0]).payload_json)["_trace_id"] == rid
    worker_logs = [json.loads(JsonFormatter().format(record)) for record in caplog.records
                   if record.getMessage() in {"worker_job_started", "worker_job_finished"}]
    assert len(worker_logs) == 2 and all(record["trace_id"] == rid for record in worker_logs)
    with request_context(payload["_trace_id"]):
        assert current_request_id() == rid
        record = logging.LogRecord("app.worker", logging.INFO, "", 0, "worker_job_started", (), None)
        assert json.loads(JsonFormatter().format(record))["trace_id"] == rid
        queue = JobQueue(session_factory=db())
        queued = queue.enqueue(JobSpec(job_kind, {}))
        with queue._factory() as session:
            assert json.loads(session.get(Job, queued).payload_json)["_trace_id"] == rid
    assert current_request_id() is None
    with request_context("private-person@example.com"):
        assert current_request_id() is None
    assert resolve_request_id("attacker-request", generated_only=True) != "attacker-request"
    assert resolve_request_id("req_custom_id_01") == "req_custom_id_01"


def test_http_5xx_captures_emit_feature_metrics_without_request_content(core, caplog):
    from types import SimpleNamespace
    from app.diagnostics.server_errors import ServerErrorCaptureMiddleware
    from core.error_event import ErrorEventMiddleware

    caplog.set_level(logging.INFO, logger="app.observability")
    async def backend(scope, receive, send):
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b"private response"})
    async def send(message):
        pass
    scope = {"type": "http", "method": "GET", "path": "/api/ventures/ven_private/idea",
             "route": SimpleNamespace(path="/ventures/{venture_id}/idea"),
             "path_params": {"path": "ventures/ven_private/idea"}, "state": {}}
    asyncio.run(ErrorEventMiddleware(ServerErrorCaptureMiddleware(backend), component="gateway")(scope, None, send))
    metrics = [record.emf for record in caplog.records if hasattr(record, "emf")]
    assert {(metric["feature"], metric["service"], metric["http_5xx"]) for metric in metrics} == {
        ("idea", "core", 1), ("idea", "gateway", 1)}
    assert "private" not in json.dumps(metrics)


@pytest.mark.hosted_only
def test_gtm_search_and_manual_outcome_reuse_durable_receipts(core, monkeypatch):
    from app.db.models import AnalyticsEvent, Venture, VentureDocument
    from app.gtm import journey
    from app.state import db

    with TestClient(core.create_app()) as client:
        created = client.post("/ventures", json={"raw_idea": "Founder journey receipt test"})
        assert created.status_code == 201
        vid = created.json()["id"]
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
        from core.clock import utcnow
        row = VentureDocument(id="doc_obs", venture_id=vid, kind="gtm_journey", title="Journey", path="",
                              body_md="", provenance_json="{}", created_at=utcnow(), updated_at=utcnow())
        session.add(row)
        journey._store(session, row, {"state": "queued", "job_id": "job_obs"})
        journey._store(session, row, {"state": "ready", "job_id": "job_obs"})
        journey._store(session, row, {"state": "ready", "job_id": "job_obs"})
        session.commit()
    monkeypatch.setattr(journey, "_processing_block", lambda *args: False)
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: {"reachable_leads": [{"id": "contact_obs"}],
        "industry_experts": [], "prospects": [], "channels": [], "need_signals": [], "manual_outreach": []})
    journey._manual_outcome(wid, vid, "contact_obs", booked=True)
    journey._manual_outcome(wid, vid, "contact_obs", booked=True)
    with db()() as session:
        events = session.query(AnalyticsEvent).filter(AnalyticsEvent.event_name.like("journey.%")).all()
        assert {(event.event_name, json.loads(event.props_json)["step"]) for event in events} == {
            ("journey.step_entered", "find_customers"), ("journey.step_completed", "find_customers"),
            ("journey.step_completed", "sent"), ("journey.step_completed", "call_booked"),
            ("journey.step_entered", "idea"), ("journey.step_completed", "idea"), ("journey.step_entered", "clarify")}
        assert len(events) == 7 and all(event.workspace_id == wid and event.venture_id == vid for event in events)
        notes = session.query(VentureDocument).filter_by(venture_id=vid, kind=journey.contact_work.NOTE).all()
        assert sum(json.loads(note.provenance_json).get("type") == "manual_outreach" for note in notes) == 1
