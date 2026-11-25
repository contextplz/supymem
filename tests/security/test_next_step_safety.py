"""Private next-step API, immutable receipts and no execution authorization."""
from __future__ import annotations

import json
from threading import BoundedSemaphore

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.gtm.test_journey import journey_db as _journey_db

base_journey_db = _journey_db


@pytest.fixture
def journey_db(base_journey_db, monkeypatch):
    from app.ventures import next_step_engine as engine
    monkeypatch.setattr(engine, "_SLOTS", BoundedSemaphore(0))
    return base_journey_db


def test_next_step_api_auth_scope_schema_and_draft_outcomes(journey_db, monkeypatch):
    from app.api.next_step_api import router
    from app.ventures import next_step_engine as engine
    data = journey_db
    monkeypatch.setattr(engine, "_choose", lambda *args: None)
    application = FastAPI()
    application.include_router(router)
    client = TestClient(application)
    path = f"/ventures/{data['vid']}/next-step"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Subject": "usr_other"}).status_code in {403, 404}
    headers = {"X-Subject": data["actor"]}
    response = client.get(path, headers=headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    decision = response.json()
    assert decision["venture_id"] == data["vid"] and len(decision["alternatives"]) <= 2
    receipt = {"decision_id": decision["decision_id"], "action_id": decision["hero"]["action_id"], "outcome": "skip"}
    assert client.post(path + "/outcome", headers=headers, json={**receipt, "execute": True}).status_code == 422
    assert client.post(path + "/outcome", headers=headers, json={**receipt, "action_id": "collect_first_payment"}).status_code == 422
    assert client.post(path + "/outcome", headers=headers, json=receipt).status_code == 200
    assert client.post(path + "/outcome", headers=headers, json=receipt).status_code == 200
    assert client.post(path + "/outcome", headers=headers, json={**receipt, "outcome": "done"}).status_code == 409


def test_next_step_viewer_cannot_change_outcomes(journey_db, monkeypatch):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import WorkspaceMember
    from app.ventures import next_step_engine as engine
    from app.ventures.next_step_models import OutcomeBody
    from fastapi import HTTPException
    from core.clock import utcnow
    data = journey_db
    actor = "usr_next_step_viewer"
    ensure_subject_person(actor)
    with data["factory"]() as session:
        session.add(WorkspaceMember(id="wsm_next_viewer", workspace_id=data["wid"], person_id=actor, role="viewer", can_approve=0, created_at=utcnow()))
        session.commit()
    monkeypatch.setattr(engine, "_choose", lambda *args: None)
    decision = engine.get_next_step(data["vid"], actor)
    with pytest.raises(HTTPException) as denied:
        engine.post_outcome(data["vid"], actor, OutcomeBody(decision_id=decision.decision_id, action_id=decision.hero.action_id, outcome="skip"))
    assert denied.value.status_code == 403


def test_next_step_revenue_requires_existing_verified_receipt(journey_db):
    from app.db.models import MoneyLedgerEntry, VentureRevenueSnapshot
    from app.ventures.next_step_snapshot import venture_snapshot
    from core.clock import utcnow
    data = journey_db
    with data["factory"]() as session:
        session.add(MoneyLedgerEntry(id="unverified_money", venture_id=data["vid"], debit_account="cash:stripe", credit_account="revenue",
            amount_minor=5000, currency="USD", source="stripe", meta_json="{}", created_at=utcnow()))
        session.add(VentureRevenueSnapshot(id="unverified_rollup", venture_id=data["vid"], earned_minor=5000, currency="USD", mode="live", created_at=utcnow()))
        session.commit()
        snapshot = venture_snapshot(session, data["vid"])
    assert snapshot.paid_customers == 0
    assert "unverified_money" not in snapshot.evidence and "unverified_rollup" not in snapshot.evidence


def test_next_step_metadata_is_content_free_and_documents_are_private(journey_db, monkeypatch):
    from app.db.models import VentureDocument
    from app.ventures import next_step_engine as engine
    from app.ventures.startup_review import PRIVATE_KINDS
    data = journey_db
    monkeypatch.setattr(engine, "_choose", lambda *args: None)
    decision = engine.get_next_step(data["vid"], data["actor"])
    with data["factory"]() as session:
        stored = session.get(VentureDocument, decision.decision_id)
        metadata = json.loads(stored.provenance_json)
        assert stored.kind in PRIVATE_KINDS
        assert set(metadata) == {"snapshot_hash", "eligible", "chosen_id", "catalogue_version", "prompt_version", "model", "latency_ms"}
        assert "internal_next_step_outcome" in PRIVATE_KINDS
