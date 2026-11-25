"""Migrated Core admits real one-use zero-provider typed browser proposals."""

import json
from dataclasses import replace
import pytest
from fastapi import HTTPException
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_device_orders import prepare
from tests.unit.product.test_device_browser_navigation import request_value


@pytest.fixture
def claimed(journey_db, monkeypatch):
    from app.build import device_orders as orders, device_jev

    d = journey_db
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.setattr(
        device_jev,
        "_provider_call",
        lambda *_: pytest.fail("no provider call is permitted"),
    )
    original = orders.prepare

    def typed(vid, caller, manifest, **kwargs):
        manifest.applications = ["com.google.Chrome"]
        manifest.browser_verification = orders.BrowserVerification(
            expected_locations=["https://example.test/", "https://example.test/next"]
        )
        return original(vid, caller, manifest, **kwargs)

    with monkeypatch.context() as local:
        local.setattr(orders, "prepare", typed)
        service, caller, order = prepare(
            d, actions=["browser.read", "browser.interact"]
        )
    args = (d["vid"], caller, order["id"])
    service.transition(*args, order["review_digest"], "approve")
    service.transition(*args, order["review_digest"], "claim")
    return d, service, caller, order, args


def test_real_controller_proposal_effect_and_zero_usage_without_provider(
    claimed, monkeypatch
):
    from app.build import (
        device_browser_navigation as nav,
        device_effects,
        device_invocations as calls,
    )
    from app.api import device_orders_api as api

    d, service, caller, order, args = claimed
    monkeypatch.setattr(api, "verify_caller", lambda *_args, **_kwargs: caller)
    request = nav.NavigationBody.model_validate(request_value())
    reply = api.browser_navigation_proposal(d["vid"], order["id"], request, None)
    assert (
        reply["source_kind"] == nav.SOURCE_KIND and reply["candidate"]["target"] == "c1"
    )
    saved = service.retrieve(*args)
    invocation = saved["invocations"][request.invocation_id]
    assert (
        invocation["state"] == "completed"
        and invocation["actual_micro"] == invocation["quote_micro"] == 0
    )
    assert (
        invocation["provider_actual_micro"] == 0
        and invocation["no_provider_io"] is True
    )
    assert "usage" not in invocation and "model" not in invocation
    assert (
        device_effects.admit(
            *args,
            invocation_id=request.invocation_id,
            observation_digest=request.observation.digest(),
            effect_digest="e" * 64,
        )
        is True
    )
    assert (
        device_effects.admit(
            *args,
            invocation_id=request.invocation_id,
            observation_digest=request.observation.digest(),
            effect_digest="e" * 64,
        )
        is False
    )
    with pytest.raises(ValueError, match="already_attempted"):
        nav.propose(*args, request=request)
    # A distinct invocation cannot reissue the same document/progress.
    altered = request.model_copy(update={"invocation_id": "nav_two"})
    with pytest.raises(ValueError, match="reconciliation"):
        nav.propose(*args, request=altered)
    device_effects.receipt(
        *args,
        invocation_id=request.invocation_id,
        effect_digest="e" * 64,
        outcome="returned",
    )
    with pytest.raises(ValueError, match="typed_navigation_unavailable"):
        nav.propose(*args, request=altered)
    with pytest.raises(ValueError, match="proposal_unavailable"):
        device_effects.admit(
            *args,
            invocation_id="fabricated",
            observation_digest=request.observation.digest(),
            effect_digest="f" * 64,
        )
    caps = api.execution_capabilities(d["vid"], order["id"], None)
    assert caps["typed_navigation_proposal_v1"] is True
    assert len(service.retrieve(*args)["invocations"]) == 1
    calls.check_current(*args)


@pytest.mark.parametrize(
    "retirement",
    [
        "foreign_device",
        "foreign_actor",
        "cancelled",
        "expired",
        "paused_account",
        "task_changed",
        "uncertain_provider",
        "unknown_effect",
        "wrong_digest",
        "review_changed",
        "foreign_venture",
        "reservation_changed",
        "over_budget",
    ],
)
def test_actual_locked_admission_refuses_retirement_or_unknown_work(
    claimed, retirement
):
    from app.build import (
        device_browser_navigation as nav,
        device_invocations as calls,
        device_effects,
    )
    from app.db.models import VentureDocument, CreditAccount
    from app.db.supymem_models import PlanTask
    from datetime import timedelta

    d, service, caller, order, args = claimed
    body = nav.NavigationBody.model_validate(request_value())
    if retirement == "foreign_device":
        args = (d["vid"], replace(caller, device_id="dev_foreign"), order["id"])
    elif retirement == "foreign_actor":
        args = (d["vid"], replace(caller, sub="usr_foreign"), order["id"])
    elif retirement == "foreign_venture":
        args = ("ven_foreign", caller, order["id"])
    elif retirement == "cancelled":
        service.transition(*args, order["review_digest"], "cancel")
    elif retirement == "uncertain_provider":
        calls.admit(
            *args, invocation_id="unknown", request_digest="a" * 64, quote_micro=10
        )
        calls.record_result(*args, invocation_id="unknown", actual_micro=None)
    elif retirement == "unknown_effect":
        nav.propose(*args, request=body)
        device_effects.admit(
            *args,
            invocation_id=body.invocation_id,
            observation_digest=body.observation.digest(),
            effect_digest="e" * 64,
        )
        device_effects.receipt(
            *args,
            invocation_id=body.invocation_id,
            effect_digest="e" * 64,
            outcome="uncertain",
        )
        body = body.model_copy(update={"invocation_id": "different"})
    else:
        with d["factory"]() as session:
            row = session.get(VentureDocument, order["id"])
            saved = json.loads(row.provenance_json)
            if retirement == "expired":
                saved["claimed_at"] = (
                    service.utcnow() - timedelta(seconds=9999)
                ).isoformat()
            elif retirement == "paused_account":
                account = (
                    session.query(CreditAccount)
                    .filter_by(venture_id=d["vid"], scope="venture")
                    .one()
                )
                account.status = "paused"
            elif retirement == "task_changed":
                session.get(
                    PlanTask, order["manifest"]["task_id"]
                ).title = "Changed task"
            elif retirement == "review_changed":
                saved["review_digest"] = "0" * 64
            elif retirement == "wrong_digest":
                saved["manifest"]["instruction"] = "Changed review text"
            elif retirement == "reservation_changed":
                from app.db.models import CreditReservation

                session.get(
                    CreditReservation, saved["reservation_id"]
                ).amount_micro += 1
            elif retirement == "over_budget":
                saved["invocations"] = {
                    "prior": {
                        "id": "prior",
                        "state": "completed",
                        "quote_micro": 1001,
                        "actual_micro": 1001,
                    }
                }
            row.provenance_json = json.dumps(saved)
            session.commit()
    with pytest.raises((ValueError, KeyError, PermissionError, HTTPException)):
        nav.propose(*args, request=body)


@pytest.mark.parametrize("marked", [False, True])
def test_actual_signed_http_navigation_requires_current_actor_device_and_flag(
    claimed, monkeypatch, marked
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api.device_orders_api import router

    d, service, caller, order, args = claimed
    secret = "typed-navigation-controller-test-secret-more-than-32-bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-navigation-proposal"
    value = request_value()
    if marked:
        value["control_snapshots"][0]["href"] += "?supymem_check=1"

    def post(actor=d["actor"], device="dev_one"):
        assertion = sign_assertion(
            secret,
            sub=actor,
            email="",
            role="user",
            path=path,
            method="POST",
            device_id=device,
        )
        return client.post(path, json=value, headers={"X-COS-Assertion": assertion})

    assert client.post(path, json=value).status_code == 401
    assert post(actor="usr_foreign").status_code == 404
    assert post(device="dev_foreign").status_code == 404
    assert post(device=None).status_code == 403
    monkeypatch.delenv("DEVICE_BROWSER_EXECUTION_ENABLED")
    assert post().status_code == 503
    assert not service.retrieve(*args).get("invocations")
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    response = post()
    assert response.status_code == 200, response.text
    assert response.json()["source_kind"] == "typed_reviewed_url_v1"
    assert post().status_code == 409
    assert len(service.retrieve(*args)["invocations"]) == 1
