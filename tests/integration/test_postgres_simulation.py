"""Migrated, isolated PostgreSQL: concurrent clicks create one paid intent."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.unit.simulation.test_rehearse import install_transport, lease, seed, start


def setup(d, monkeypatch):
    monkeypatch.setenv("SUPYMEM_SIMULATION_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", "offline-provider-key")
    return seed(d)


def test_concurrent_explicit_clicks_share_current_frozen_input(journey_db, monkeypatch):
    from app.core.jobs import complete_job
    from app.db.models import Job
    from app.simulation import runtime, service
    d = setup(journey_db, monkeypatch)
    q = service.quote(d["wid"], d["vid"], d["actor"], d["refs"])
    barrier = Barrier(4)
    def click(index):
        barrier.wait(timeout=10)
        return service.start(d["wid"], d["vid"], d["actor"], d["refs"], epoch=q["epoch"],
            quote_digest=q["quote_digest"], confirmed=True, request_id=f"click-{index}")["job_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(click, range(4)))
    assert len(set(ids)) == 1
    with d["factory"]() as session:
        rows = list(session.scalars(select(Job).where(Job.kind == service.KIND, Job.venture_id == d["vid"])))
        assert len(rows) == 1 and rows[0].max_attempts == 1 and rows[0].timeout_s == 150
    calls = install_transport(monkeypatch)
    payload = lease(d, ids[0])
    runtime.run(payload)
    assert len(calls) == 4
    with d["factory"]() as session:
        complete_job(session, ids[0], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        session.commit()


def test_signed_route_and_four_genuine_settled_operations(journey_db, monkeypatch):
    from app.api.simulation_api import router
    from app.db.models import CreditReservation
    from app.simulation import runtime
    from core.assertion import sign_assertion
    d = setup(journey_db, monkeypatch)
    secret = "isolated-pg-rehearse-signed-caller-32bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    root = f"/api/v1/ventures/{d['vid']}/simulation/rehearse"
    def post(path, body):
        return client.post(path + "?workspace_id=" + d["wid"], json=body,
            headers={"X-COS-Assertion": sign_assertion(secret, sub=d["actor"], email="", role="user", path=path, method="POST")})
    q = post(root + "/quote", {"drafts": d["refs"]})
    assert q.status_code == 200
    q = q.json()
    queued = post(root, {"drafts": d["refs"], "epoch": q["epoch"], "quote_digest": q["quote_digest"], "confirmed": True, "request_id": "signed-click"})
    assert queued.status_code == 202
    jid = queued.json()["job_id"]
    calls = install_transport(monkeypatch)
    result = runtime.run(lease(d, jid))
    assert len(calls) == 4 and result["badge"] == "Simulated · AI personas"
    with d["factory"]() as session:
        holds = list(session.scalars(select(CreditReservation).where(CreditReservation.purpose == "simulation.rehearse", CreditReservation.account_id == d["account"])))
        assert len(holds) == 4 and all(h.status == "settled" and h.request_id.startswith(jid + ":") for h in holds)


@pytest.mark.parametrize("failure", ["usage", "timeout"])
def test_actual_unknown_usage_or_outcome_preserves_hold_no_repeat(journey_db, monkeypatch, failure):
    from app.core.jobs import FatalError
    from app.db.models import CreditAccount, CreditReservation
    from app.simulation import runtime
    d = setup(journey_db, monkeypatch)
    queued, _ = start(d)
    calls = install_transport(monkeypatch, fail=failure)
    payload = lease(d, queued["job_id"])
    for _ in range(2):
        with pytest.raises(FatalError, match="outcome_uncertain"):
            runtime.run(payload)
    assert start(d, "second-click")[0]["job_id"] == queued["job_id"] and len(calls) == 1
    with d["factory"]() as session:
        hold = session.scalar(select(CreditReservation).where(CreditReservation.account_id == d["account"]))
        assert hold.status == "reconciling" and session.get(CreditAccount, d["account"]).reserved_micro == hold.amount_micro


def test_registered_app_signed_start_dispatch_and_durable_saved_read(journey_db, monkeypatch):
    """Real router registry, HTTP scope, catalogue and worker; only transport is fake."""
    from app.core.jobs import lease as claim
    from app.db.models import CreditAccount, CreditReservation, Job
    from app.simulation import service
    from app.tenancy import current_or_none
    from app.worker import catalog, handlers
    from app.worker.main import _run_one
    from core.assertion import sign_assertion
    from llm_gateway import sole

    d = setup(journey_db, monkeypatch)
    secret = "registered-pg-rehearse-signed-caller-32bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "registered-pg-vault-fixture-secret-32bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "offline-ops@example.test")
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_ROLE", "api")
    from app.main import create_app
    monkeypatch.delenv("WORKER_KINDS", raising=False)
    catalog.load_catalogue.cache_clear()
    handlers.install_default_handlers()
    assert service.KIND in catalog.stage2_kinds() and service.KIND in catalog.enabled_kinds()
    assert service.KIND in handlers.HANDLERS
    app = create_app()
    client = TestClient(app)  # No lifespan/bootstrap, scheduler or external service starts.
    root = f"/api/v1/ventures/{d['vid']}/simulation/rehearse"

    def request(method, path, body=None):
        return client.request(method, path + "?workspace_id=" + d["wid"], json=body,
            headers={"X-COS-Assertion": sign_assertion(secret, sub=d["actor"], email="", role="user", path=path, method=method)})

    q = request("POST", root + "/quote", {"drafts": d["refs"]})
    assert q.status_code == 200, q.text
    reviewed = q.json()
    started = request("POST", root, {"drafts": d["refs"], "epoch": reviewed["epoch"],
        "quote_digest": reviewed["quote_digest"], "confirmed": True, "request_id": "registered-click"})
    assert started.status_code == 202, started.text
    jid = started.json()["job_id"]
    calls = install_transport(monkeypatch)
    transport = sole.transmit

    async def scoped_transport(**kwargs):
        scope = current_or_none()
        assert scope and (scope.workspace_id, scope.venture_id, scope.actor) == (d["wid"], d["vid"], d["actor"])
        return await transport(**kwargs)

    monkeypatch.setattr(sole, "transmit", scoped_transport)
    with d["factory"]() as session:
        row = session.get(Job, jid)
        assert json.loads(row.payload_json)["_actor_subject"] == d["actor"]
        claimed = claim(session, "registered-reader-worker", kinds=[service.KIND], n=1)
        session.commit()
    assert len(claimed) == 1 and claimed[0].id == jid
    running = {(jid, claimed[0].attempts)}
    asyncio.run(_run_one(d["factory"], claimed[0], running))
    assert not running and len(calls) == 4
    saved = request("GET", root + "/" + jid)
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result["state"] == "completed" and result["result"]["badge"] == "Simulated · AI personas"
    assert result["actor_id"] == d["actor"] and result["input_digest"] == reviewed["input_digest"]
    with d["factory"]() as session:
        row = session.get(Job, jid)
        assert row.status == "succeeded" and row.attempts == 1, (row.status, row.last_error)
        holds = list(session.scalars(select(CreditReservation).where(CreditReservation.account_id == d["account"], CreditReservation.purpose == service.KIND)))
        assert len(holds) == 4 and all(h.status == "settled" for h in holds)
        assert result["credits_micro"] == sum(h.settled_micro for h in holds) > 0
        assert session.get(CreditAccount, d["account"]).reserved_micro == 0
