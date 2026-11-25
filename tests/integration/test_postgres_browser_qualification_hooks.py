"""Existing review/effect API wiring on migrated PG, without provider or native I/O."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_browser_qualification import seeded_trial, source_effect


def signed_client(monkeypatch, caller):
    from app.api.device_orders_api import router
    from app.api.browser_qualification_api import router as fixture_router
    from core.assertion import sign_assertion
    secret = "synthetic-qualification-hook-signing-key-more-than-32bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    app = FastAPI()
    app.include_router(router)
    app.include_router(fixture_router)

    def headers(path):
        return {"X-COS-Assertion": sign_assertion(secret, sub=caller.sub, email="", role="user",
            path=path, method="POST", device_id=caller.device_id)}

    return TestClient(app), headers


def test_qualification_approval_rejects_old_client_before_hold_and_preserves_displayed_hash(journey_db, monkeypatch):
    from app.db.models import CreditAccount, CreditReservation
    d = journey_db
    _, orders, caller, order, grant, _ = seeded_trial(d, monkeypatch, claim=False)
    client, headers = signed_client(monkeypatch, caller)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/approve"
    body = {"review_digest": grant.review_digest}
    denied = client.post(path, json=body, headers=headers(path))
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "browser_qualification_parser_unavailable"
    for value in (False, 1, "true"):
        assert client.post(path, json=body | {"browser_qualification_parser_supported": value},
            headers=headers(path)).status_code == 422
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.reserved_micro == account.spent_micro == 0
        assert session.scalar(select(CreditReservation).where(CreditReservation.request_id == order["id"])) is None
    approved = client.post(path, json=body | {"browser_qualification_parser_supported": True}, headers=headers(path))
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"
    assert approved.json()["expires_at"] == order["expires_at"]
    assert approved.json()["review_digest"] == orders._digest(approved.json()) == grant.review_digest


def proposal_without_effect(d, grant, args):
    from app.build import device_invocations as calls
    source_effect(d, grant, args)
    with d["factory"]() as session:
        row, body, _ = calls._locked(session, *args)
        child = body["invocations"]["source_effect"]
        del child["effect"]
        child["proposal"] = {"operation": "click", "target": "submit-control", "observation_digest": "b" * 64,
            "qualification_rule_ids": [rule.rule_id for rule in grant.rules if rule.method == "POST"]}
        row.provenance_json = json.dumps(body)
        session.commit()


def test_paused_request_binds_once_and_requires_server_send_receipt(journey_db, monkeypatch):
    from app.build import device_effects as effects
    from core.browser_qualification import request_binding_digest
    d = journey_db
    svc, _, caller, _, grant, args = seeded_trial(d, monkeypatch)
    proposal_without_effect(d, grant, args)
    assert effects.admit(*args, invocation_id="source_effect", observation_digest="b" * 64, effect_digest="a" * 64)
    rule = next(rule for rule in grant.rules if rule.method == "POST")
    binding = {"grant_digest": grant.grant_digest, "rule_id": rule.rule_id, "request_id": "paused_http_once",
        "request_digest": request_binding_digest(grant, rule, "paused_http_once")}
    client, headers = signed_client(monkeypatch, caller)
    assert client.get(grant.rules[0].path).status_code == 200
    path = f"/ventures/{d['vid']}/device-work-orders/{args[2]}/effects/bind-qualification"
    body = {"invocation_id": "source_effect", "effect_digest": "a" * 64, "qualification": binding}
    assert client.post(path, json=body, headers=headers(path)).json() == {"bound_now": True}
    assert client.post(path, json=body, headers=headers(path)).json() == {"bound_now": False}
    changed = binding | {"request_id": "another_http", "request_digest": request_binding_digest(grant, rule, "another_http")}
    denied = client.post(path, json=body | {"qualification": changed}, headers=headers(path))
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "browser_qualification_effect_binding_changed"
    result, request_id = svc.receive_fixture_request(grant.nonce, path=rule.path, query="", method="POST",
        submitted_fields=tuple((field.name, field.value) for field in rule.fields), body_kind=rule.body_kind,
        request_id=binding["request_id"], invocation_id="source_effect", effect_digest="a" * 64,
        execution_id=_execution(d, args))
    assert result.status == 303
    with pytest.raises(ValueError, match="browser_qualification_request_protected"):
        effects.receipt(*args, invocation_id="source_effect", effect_digest="a" * 64, outcome="returned")
    svc.record_response_delivery(grant.nonce, request_id)
    effects.receipt(*args, invocation_id="source_effect", effect_digest="a" * 64, outcome="returned")
    with pytest.raises(ValueError, match="browser_qualification_request_already_attempted|browser_qualification_request_protected"):
        svc.receive_fixture_request(grant.nonce, path=rule.path, query="", method="POST",
            submitted_fields=tuple((field.name, field.value) for field in rule.fields), body_kind=rule.body_kind,
            request_id=binding["request_id"], invocation_id="source_effect", effect_digest="a" * 64,
            execution_id=_execution(d, args))


def _execution(d, args):
    from app.build import device_invocations as calls
    with d["factory"]() as session:
        _, body, _ = calls._locked(session, *args)
        return body["execution_id"]


def test_retired_paid_parent_refuses_new_request_binding(journey_db, monkeypatch):
    from app.build import device_effects as effects, device_invocations as calls
    from core.browser_qualification import request_binding_digest
    d = journey_db
    _, _, _, _, grant, args = seeded_trial(d, monkeypatch)
    proposal_without_effect(d, grant, args)
    assert effects.admit(*args, invocation_id="source_effect", observation_digest="b" * 64, effect_digest="a" * 64)
    with d["factory"]() as session:
        row, body, _ = calls._locked(session, *args)
        body["invocations"]["source_effect_parent"]["state"] = "uncertain"
        row.provenance_json = json.dumps(body)
        session.commit()
    rule = next(rule for rule in grant.rules if rule.method == "POST")
    binding = {"grant_digest": grant.grant_digest, "rule_id": rule.rule_id, "request_id": "retired_http",
        "request_digest": request_binding_digest(grant, rule, "retired_http")}
    with pytest.raises(ValueError, match="browser_qualification_effect_unavailable"):
        effects.bind_qualification(*args, invocation_id="source_effect", effect_digest="a" * 64, qualification=binding)


def actor_parent(d, grant, args, parent_id):
    """Trusted journal fixture for one completed paid actor step; no provider call."""
    from app.build import device_invocations as calls
    with d["factory"]() as session:
        row, body, _ = calls._locked(session, *args)
        body["browser_ai_latest_invocation_id"] = parent_id
        body.setdefault("invocations", {})[parent_id] = {"id": parent_id, "state": "completed", "phase": "actor",
            "source_kind": "browser_cuai_text_v1", "profile_digest": grant.profile_digest, "request_digest": "r" * 64,
            "quote_micro": 0}
        row.provenance_json = json.dumps(body)
        session.commit()


@pytest.mark.parametrize("operation,target", [("extract", "document_" + "d" * 64),
    ("scroll", "document_" + "e" * 64), ("navigate", "c1")])
def test_grant_reads_join_proposal_and_one_use_effect_on_v1_profile(journey_db, monkeypatch, operation, target):
    from app.build import device_effects as effects, device_invocations as calls
    from coding_agents.jev_decision import Candidate
    d = journey_db
    _, _, _, order, grant, args = seeded_trial(d, monkeypatch)
    assert order["manifest"]["general_browser_verification"]["execution_profile"]["version"] == "browser_cuai_text_v1"
    assert {"navigate", "scroll", "extract"} <= set(grant.allowed_operations)
    assert "hover" not in grant.allowed_operations
    actor_parent(d, grant, args, "bai_" + "1" * 32)
    child = calls.save_browser_actor_proposal(*args, invocation_id="bai_" + "1" * 32,
        candidate=Candidate(operation, target, "b" * 64))
    assert effects.admit(*args, invocation_id=child, observation_digest="b" * 64, effect_digest="a" * 64) is True
    assert effects.admit(*args, invocation_id=child, observation_digest="b" * 64, effect_digest="a" * 64) is False
    actor_parent(d, grant, args, "bai_" + "2" * 32)
    with pytest.raises(ValueError, match="device_effect_proposal_unavailable"):
        calls.save_browser_actor_proposal(*args, invocation_id="bai_" + "2" * 32,
            candidate=Candidate("hover", target, "b" * 64))
