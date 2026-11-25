"""Exact failed-GET recovery joins and delayed redirect custody on fixture-owned PG."""
from copy import deepcopy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_browser_qualification import seeded_trial, source_effect
from tests.integration.test_postgres_browser_qualification_context import record_initial_read


def error_case(d, monkeypatch, *, delivered=True):
    from app.build.device_browser_ai import QualificationContext, QualificationErrorLoadProof
    from coding_agents.jev_decision import Observation
    svc, orders, caller, order, grant, args = seeded_trial(d, monkeypatch, task_id="l06_recover_from_error_page")
    record_initial_read(svc, grant)
    rule = next(rule for rule in grant.rules if rule.path.endswith("/reports/q-download"))
    result, request_id = svc.receive_fixture_request(grant.nonce, path=rule.path, query=rule.query, method="GET")
    assert result.status == 503
    if delivered:
        svc.record_response_delivery(grant.nonce, request_id)
    receipt = svc.request_status(d["vid"], caller, grant.trial_id, request_id)
    observation = Observation(revision="synthetic-error-recovery", application="com.google.Chrome",
        location=grant.origin + rule.path, goal=order["manifest"]["instruction"],
        context="503 Service unavailable. Please try again.", controls=[], reviewed_text=[],
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False)
    proof = QualificationErrorLoadProof(kind="qualification_error", qualified=False, session_id="a" * 32,
        page_id="b" * 32, document_id="d" * 32, sequence=1, location=observation.location,
        ready_state="complete", visible_text=True, visibility_scope="bounded_dom_text_geometry",
        observation_digest=observation.digest(), response={"url": observation.location, "status": receipt["status_code"],
            "complete": True, "sequence": 1, "request_id": request_id, "sha256": receipt["response_digest"],
            "size_bytes": receipt["response_size_bytes"], "mime_type": receipt["response_mime_type"]})
    context = QualificationContext(grant_digest=grant.grant_digest)
    return svc, caller, order, grant, args, rule, result, receipt, observation, proof, context


def validate(d, svc, args, context, observation, proof):
    from app.build import device_invocations as calls
    with d["factory"]() as session:
        _, body, account = calls._locked(session, *args)
        return svc.validate_runtime_context(session, account, body, context, observation=observation,
            current_url=observation.location, current_load=proof)


def error_request(order, context, observation, proof):
    from tests.integration.test_postgres_browser_qualification_context import browser_request
    request = browser_request(order, context, observation)
    request.current_load = proof
    notes = json.loads(request.context.notes)
    notes["current"].update({key: getattr(proof, key) for key in (
        "session_id", "page_id", "document_id", "sequence", "location", "observation_digest")})
    notes["current"]["source"].update(facts=[observation.context], fields=[])
    request.context.notes = json.dumps(notes)
    return request


def test_failed_get_has_exact_sent_response_receipt_and_only_recovery_authority(journey_db, monkeypatch):
    from core.canonical import sha256_hex
    d = journey_db
    svc, caller, _, grant, args, rule, result, receipt, observation, proof, context = error_case(d, monkeypatch)
    assert receipt["state"] == "failed_read" and receipt["mutated"] is False
    assert receipt["server_response_sent"] is True and receipt["response_size_bytes"] == len(result.body)
    assert receipt["response_digest"] == sha256_hex(result.body) and receipt["response_mime_type"] == "text/html"
    assert validate(d, svc, args, context, observation, proof) == grant
    assert context._recovery_only is True
    assert rule.rule_id not in context.completed_rules
    assert all(item.method == "GET" for item in grant.rules if item.rule_id in context._eligible_rule_ids)
    view = svc.order_status(d["vid"], caller, grant.trial_id, grant.order_id)
    assert rule.rule_id not in view["completed_rules"] and not view["rules"][rule.rule_id]["protected"]
    assert rule.rule_id not in view["read_prerequisites"]


@pytest.mark.parametrize("change", [
    {"request_id": "foreign_request"}, {"sha256": "a" * 64}, {"size_bytes": 1},
    {"mime_type": "application/json"}, {"mime_type": "text/html; charset=utf-8"},
    {"mime_type": "TEXT/HTML"}, {"status": 404}, {"url": "https://api.supymem.com/foreign"},
    {"complete": False}, {"sequence": 2},
])
def test_error_response_tamper_is_rejected_before_provider_admission(journey_db, monkeypatch, change):
    from app.db.models import VentureDocument
    d = journey_db
    svc, _, order, grant, args, _, _, _, observation, proof, context = error_case(d, monkeypatch)
    forged = deepcopy(proof)
    for key, value in change.items():
        setattr(forged.response, key, value)
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, context, observation, forged)
    with d["factory"]() as session:
        saved = json.loads(session.get(VentureDocument, order["id"]).provenance_json)
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        assert not saved.get("invocations") and not trial["attempts"]


@pytest.mark.parametrize("key,value", [
    ("execution_id", "foreign_execution"), ("trial_id", "foreign_trial"), ("order_id", "foreign_order"),
    ("method", "POST"), ("grant_digest", "a" * 64), ("request_digest", "b" * 64),
    ("rule_id", "foreign_rule"), ("mutated", True), ("response_size_bytes", 1),
    ("response_mime_type", "application/json"),
])
def test_recovery_receipt_must_belong_to_current_saved_execution_and_scope(journey_db, monkeypatch, key, value):
    from app.db.models import VentureDocument
    d = journey_db
    svc, _, _, grant, args, _, _, receipt, observation, proof, context = error_case(d, monkeypatch)
    with d["factory"]() as session:
        row = session.get(VentureDocument, grant.trial_id)
        trial = json.loads(row.provenance_json)
        trial["requests"][receipt["request_id"]][key] = value
        row.provenance_json = json.dumps(trial)
        session.commit()
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, context, observation, proof)


def test_unsent_error_or_error_page_controls_never_authorize_recovery_mutation(journey_db, monkeypatch):
    from coding_agents.jev_decision import Control
    d = journey_db
    svc, _, _, grant, args, rule, _, receipt, observation, proof, context = error_case(d, monkeypatch, delivered=False)
    with pytest.raises(ValueError, match="browser_qualification_request_protected"):
        validate(d, svc, args, context, observation, proof)
    svc.record_response_delivery(grant.nonce, receipt["request_id"])
    control = {"id": "c0", "document_path": rule.document_path, "document_id": rule.document_id,
        "container_id": "report_problem", "control_id": "report_problem_submit", "tag": "BUTTON", "input_type": "submit"}
    forged_context = type(context)(grant_digest=grant.grant_digest, controls=[control])
    forged_observation = observation.model_copy(update={"controls": [Control(id="c0", label="Report problem",
        operations=["click"], visible=True, enabled=True)]})
    proof.observation_digest = forged_observation.digest()
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, forged_context, forged_observation, proof)
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, context, observation.model_copy(update={"reviewed_text": ["unreviewed field"]}), proof)


def test_error_retries_remain_bounded_and_only_successful_read_completes_rule(journey_db, monkeypatch):
    d = journey_db
    svc, caller, _, grant, _, rule, _, _, _, _, _ = error_case(d, monkeypatch)
    for expected_status in (503, 200):
        result, request_id = svc.receive_fixture_request(grant.nonce, path=rule.path, query=rule.query, method="GET")
        assert result.status == expected_status
        svc.record_response_delivery(grant.nonce, request_id)
        receipt = svc.request_status(d["vid"], caller, grant.trial_id, request_id)
        assert receipt["state"] == ("failed_read" if expected_status == 503 else "committed")
        assert receipt["response_mime_type"] == ("text/html" if expected_status == 503 else "text/csv")
        view = svc.order_status(d["vid"], caller, grant.trial_id, grant.order_id)
        assert (rule.rule_id in view["completed_rules"]) is (expected_status == 200)
    for _ in range(3):
        _, request_id = svc.receive_fixture_request(grant.nonce, path=rule.path, query=rule.query, method="GET")
        svc.record_response_delivery(grant.nonce, request_id)
    with pytest.raises(ValueError, match="browser_qualification_request_protected"):
        svc.receive_fixture_request(grant.nonce, path=rule.path, query=rule.query, method="GET")
    assert svc.order_status(d["vid"], caller, grant.trial_id, grant.order_id)["rules"][rule.rule_id]["request_count"] == 6


def test_real_303_passive_get_before_post_delivery_keeps_actor_and_effect_protected(journey_db, monkeypatch):
    from app.api.browser_qualification_api import router
    from app.build.device_browser_ai import QualificationContext
    from coding_agents.jev_decision import Observation
    d = journey_db
    svc, _, caller, order, grant, args = seeded_trial(d, monkeypatch)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get(grant.rules[0].path).status_code == 200
    headers = source_effect(d, grant, args)
    post_rule = next(rule for rule in grant.rules if rule.method == "POST")
    original = svc.record_response_delivery
    delayed = []

    def hold_post_delivery(nonce, request_id):
        if request_id == headers["X-Supymem-Qualification-Request"]:
            delayed.append((nonce, request_id))
        else:
            original(nonce, request_id)

    monkeypatch.setattr(svc, "record_response_delivery", hold_post_delivery)
    result = client.post(post_rule.path, data={field.name: field.value for field in post_rule.fields},
        headers=headers, follow_redirects=False)
    assert result.status_code == 303 and delayed
    followed = client.get(result.headers["Location"])
    assert followed.status_code == 200 and followed.headers["X-Supymem-Qualification-Request"]
    receipt = svc.request_status(d["vid"], caller, grant.trial_id, headers["X-Supymem-Qualification-Request"])
    assert receipt["state"] == "committed" and receipt["mutated"] is True and not receipt["server_response_sent"]
    view = svc.order_status(d["vid"], caller, grant.trial_id, grant.order_id)
    assert post_rule.rule_id not in view["completed_rules"] and view["rules"][post_rule.rule_id]["protected"]
    assert post_rule.rule_id in view["read_prerequisites"]
    context = QualificationContext(grant_digest=grant.grant_digest)
    observation = Observation(revision="synthetic-delayed-303", application="com.google.Chrome",
        location=grant.origin + result.headers["Location"], goal=order["manifest"]["instruction"],
        context="Recorded", controls=[], reviewed_text=[], permitted=True,
        confirmation_pending=False, prior_effect_uncertain=False)
    with pytest.raises(ValueError, match="browser_qualification_request_protected"):
        validate(d, svc, args, context, observation, None)
    assert client.post(post_rule.path, data={field.name: field.value for field in post_rule.fields},
        headers=headers, follow_redirects=False).status_code == 409
    original(*delayed[0])
    assert post_rule.rule_id in svc.order_status(d["vid"], caller, grant.trial_id, grant.order_id)["completed_rules"]


def rejected_post_case(d, monkeypatch):
    from app.build import device_browser_ai as ai, device_effects as effects
    from coding_agents.jev_decision import Observation
    svc, _, caller, order, grant, args = seeded_trial(d, monkeypatch, task_id="form_recover")
    record_initial_read(svc, grant)
    rule = next(rule for rule in grant.rules if rule.method == "POST")
    headers = source_effect(d, grant, args)
    result, request_id = svc.receive_fixture_request(grant.nonce, path=rule.path, query=rule.query, method="POST",
        submitted_fields=tuple((field.name, field.value) for field in rule.fields), body_kind=rule.body_kind,
        request_id=headers["X-Supymem-Qualification-Request"], invocation_id=headers["X-Supymem-Qualification-Invocation"],
        effect_digest=headers["X-Supymem-Qualification-Effect"], execution_id=headers["X-Supymem-Qualification-Execution"])
    assert result.status == 400
    svc.record_response_delivery(grant.nonce, request_id)
    effects.receipt(*args, invocation_id=headers["X-Supymem-Qualification-Invocation"],
        effect_digest=headers["X-Supymem-Qualification-Effect"], outcome="returned")
    receipt = svc.request_status(d["vid"], caller, grant.trial_id, request_id)
    observation = Observation(revision="synthetic-rejected-form", application="com.google.Chrome",
        location=grant.origin + rule.path, goal=order["manifest"]["instruction"],
        context="Error: code must be exactly six uppercase letters or digits", controls=[], reviewed_text=[],
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False)
    proof = ai.QualificationErrorLoadProof(kind="qualification_error", qualified=False, session_id="a" * 32,
        page_id="b" * 32, document_id="d" * 32, sequence=1, location=observation.location,
        ready_state="complete", visible_text=True, visibility_scope="bounded_dom_text_geometry",
        observation_digest=observation.digest(), response={"url": observation.location, "status": 400,
            "complete": True, "sequence": 1, "request_id": request_id, "sha256": receipt["response_digest"],
            "size_bytes": receipt["response_size_bytes"], "mime_type": receipt["response_mime_type"]})
    return svc, caller, grant, args, headers, receipt, observation, proof, ai.QualificationContext(grant_digest=grant.grant_digest)


def test_known_rejected_post_recovery_preserves_historical_source_lineage_without_new_effect_authority(journey_db, monkeypatch):
    from app.build import device_invocations as calls
    d = journey_db
    svc, _, grant, args, _, receipt, observation, proof, context = rejected_post_case(d, monkeypatch)
    assert receipt["method"] == "POST" and receipt["state"] == "rejected_before_mutation" and receipt["mutated"] is False
    with d["factory"]() as session:
        row, body, _ = calls._locked(session, *args)
        body["browser_ai_latest_invocation_id"] = "recovery_plan"
        body["invocations"]["recovery_plan"] = {"state": "completed", "phase": "plan",
            "source_kind": "browser_cuai_text_v1", "profile_digest": grant.profile_digest, "quote_micro": 0}
        row.provenance_json = json.dumps(body)
        session.commit()
    assert validate(d, svc, args, context, observation, proof) == grant and context._recovery_only
    assert all(rule.method == "GET" for rule in grant.rules if rule.rule_id in context._eligible_rule_ids)
    # Historical recovery custody never turns the old child into a fresh POST admission.
    with pytest.raises(ValueError, match="browser_qualification_request_protected"):
        rule = next(rule for rule in grant.rules if rule.method == "POST")
        svc.receive_fixture_request(grant.nonce, path=rule.path, query=rule.query, method="POST",
            submitted_fields=tuple((field.name, field.value) for field in rule.fields), body_kind=rule.body_kind,
            request_id=receipt["request_id"])


@pytest.mark.parametrize("change", ["child_state", "parent_state", "parent_source", "profile", "digest", "binding", "uncertain"])
def test_rejected_post_historical_child_or_parent_tamper_cannot_recover(journey_db, monkeypatch, change):
    from app.build import device_invocations as calls
    d = journey_db
    svc, _, _, args, headers, _, observation, proof, context = rejected_post_case(d, monkeypatch)
    with d["factory"]() as session:
        row, body, _ = calls._locked(session, *args)
        child = body["invocations"][headers["X-Supymem-Qualification-Invocation"]]
        parent = body["invocations"][child["parent_invocation_id"]]
        if change == "child_state":
            child["state"] = "calling"
        elif change == "parent_state":
            parent["state"] = "calling"
        elif change == "parent_source":
            parent["source_kind"] = "foreign_source"
        elif change == "profile":
            parent["profile_digest"] = "a" * 64
        elif change == "digest":
            child["effect"]["digest"] = "b" * 64
        elif change == "binding":
            child["effect"]["qualification"]["request_id"] = "foreign_request"
        else:
            child["effect"]["state"] = "uncertain"
        row.provenance_json = json.dumps(body)
        session.commit()
    with pytest.raises(ValueError, match="browser_qualification_effect_unavailable"):
        validate(d, svc, args, context, observation, proof)


@pytest.mark.parametrize("field,value", [("size_bytes", 1), ("mime_type", "application/json")])
def test_signed_recovery_api_rejoins_source_before_any_provider_reservation(journey_db, monkeypatch, field, value):
    from app.build import device_browser_ai as ai
    from app.db.models import VentureDocument
    from tests.integration.test_postgres_browser_qualification_hooks import signed_client
    d = journey_db
    svc, caller, order, grant, _, _, _, _, observation, proof, context = error_case(d, monkeypatch)
    request = error_request(order, context, observation, proof.model_copy(deep=True))
    setattr(request.current_load.response, field, value)
    monkeypatch.setattr(ai, "_provider_call", lambda _: pytest.fail("forged source reached provider"))
    client, headers = signed_client(monkeypatch, caller)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-ai-call"
    result = client.post(path, json=request.model_dump(), headers=headers(path))
    assert result.status_code == 409 and result.json()["detail"]["code"] == svc.INVALID
    with d["factory"]() as session:
        saved = json.loads(session.get(VentureDocument, order["id"]).provenance_json)
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        assert not saved.get("invocations") and not trial["attempts"]


@pytest.mark.parametrize("action", [{"op": "done", "summary": "Success"},
    {"op": "click", "element_id": "c0"}, {"op": "type", "element_id": "c0", "text": "unreviewed"}])
def test_error_actor_cannot_complete_or_mutate_and_honestly_retains_stub_usage(journey_db, monkeypatch, action):
    from app.build import device_browser_ai as ai
    from app.db.models import VentureDocument
    from tests.unit.product.test_device_browser_ai import response
    d = journey_db
    _, _, order, grant, args, _, _, _, observation, proof, context = error_case(d, monkeypatch)
    request = error_request(order, context, observation, proof)
    sent = []
    reply = json.dumps({"actions": [action], "memory": "", "subgoal_done": False})
    monkeypatch.setattr(ai, "_provider_call", lambda body: (sent.append(body), response(reply))[1])
    with pytest.raises(ValueError, match="device_browser_ai_reply_invalid"):
        ai.run(*args, request=request)
    with d["factory"]() as session:
        saved = json.loads(session.get(VentureDocument, order["id"]).provenance_json)
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        assert len(saved["invocations"]) == 1 and saved["invocations"][request.invocation_id]["state"] == "completed"
        assert saved["invocations"][request.invocation_id]["actual_micro"] > 0
        assert not any(receipt["mutated"] for receipt in trial["requests"].values())
        assert trial["attempts"][order["id"] + ":" + request.invocation_id]["state"] == "known"
    assert len(sent) == 1
