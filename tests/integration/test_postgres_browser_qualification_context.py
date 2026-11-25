"""Runtime metadata rejoined to sealed Core custody on fixture-created PostgreSQL."""
from dataclasses import replace
import json

import pytest

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_browser_qualification import seeded_trial, source_effect
from tests.integration.test_postgres_browser_qualification_hooks import actor_parent, signed_client


def fixture_context(grant, *, submit=True):
    from app.build.device_browser_ai import QualificationContext
    from coding_agents.jev_decision import Observation
    rule = next(rule for rule in grant.rules if rule.method == "POST")
    metadata, controls, values = [], [], []
    for index, field in enumerate(rule.fields):
        kind = field.kind
        check = kind in {"radio", "checkbox"}
        select = kind == "select"
        metadata.append({"id": f"c{index}", "document_path": rule.document_path, "document_id": rule.document_id,
            "container_id": rule.form_id, "control_id": field.control_id, "tag": "SELECT" if select else "INPUT",
            "input_type": "select-one" if select else kind if check else field.input_type,
            "input_value": field.input_value, "kind": kind, "select_value": field.value if select or check else None})
        controls.append({"id": f"c{index}", "label": field.name, "operations": ["click" if check else "type"],
            "visible": True, "enabled": True})
        if not check:
            values.append(field.value if select else field.input_value)
    if submit:
        identifier = f"c{len(metadata)}"
        metadata.append({"id": identifier, "document_path": rule.document_path, "document_id": rule.document_id,
            "container_id": rule.form_id, "control_id": rule.control_id, "tag": "BUTTON", "input_type": "submit"})
        controls.append({"id": identifier, "label": "Submit", "operations": ["click"], "visible": True, "enabled": True})
    context = QualificationContext(grant_digest=grant.grant_digest, controls=metadata)
    observation = Observation(revision="synthetic-context", application="com.google.Chrome", location=grant.origin + rule.document_path,
        goal="Synthetic fixture", context="Synthetic fixture controls", controls=controls, reviewed_text=list(dict.fromkeys(values)),
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False)
    return context, observation


def record_initial_read(svc, grant, *, delivered=True):
    rule = next(rule for rule in grant.rules if rule.method == "GET")
    _, request_id = svc.receive_fixture_request(grant.nonce, path=rule.path, query=rule.query, method="GET")
    if delivered:
        svc.record_response_delivery(grant.nonce, request_id)
    return request_id


def validate(d, svc, args, context, observation):
    from app.build import device_invocations as calls
    with d["factory"]() as session:
        _, body, account = calls._locked(session, *args)
        return svc.validate_runtime_context(session, account, body, context,
            observation=observation, current_url=observation.location)


def browser_request(order, context, observation):
    from tests.unit.product.test_device_browser_ai import request_for, structured_notes_for
    request = request_for(order)
    request.observation = observation.model_copy(update={"goal": order["manifest"]["instruction"]})
    request.current_load.location = request.observation.location
    request.current_load.response.url = request.observation.location
    request.current_load.observation_digest = request.observation.digest()
    request.context.qualification = context
    notes = structured_notes_for(request)
    notes["schema_version"] = 2
    notes["prior_pages"], notes["input_history"] = [], []
    notes["current"]["source"].update(reads=[], reads_omitted=0)
    request.context.notes = json.dumps(notes)
    return request


def test_context_uses_exact_saved_literals_and_server_completion_before_any_provider_admission(journey_db, monkeypatch):
    from app.build import device_browser_ai as ai
    from app.db.models import VentureDocument
    d = journey_db
    svc, orders, caller, order, grant, args = seeded_trial(d, monkeypatch)
    record_initial_read(svc, grant)
    context, observation = fixture_context(grant)
    assert validate(d, svc, args, context, observation) == grant
    assert context.completed_rules == [grant.rules[0].rule_id]

    # One correctly shaped forgery for each trust-boundary field. None gets an
    # invocation reservation or reaches the provider, even through the signed API.
    changes = [{"grant_digest": "a" * 64}, {"completed_rules": [grant.rules[1].rule_id]},
        {"completed_rules": [grant.rules[0].rule_id] * 2}]
    field_changes = [{"document_path": grant.rules[0].path + "/foreign"}, {"document_id": "foreign_document"},
        {"container_id": "foreign_form"}, {"control_id": "foreign_control"}, {"kind": "email"},
        {"input_type": "email"}, {"tag": "TEXTAREA"}, {"input_value": "Caller value"}, {"select_value": "Caller value"}]
    for change in changes:
        with pytest.raises(ValueError, match=svc.INVALID):
            validate(d, svc, args, context.model_copy(update=change, deep=True), observation)
    for change in field_changes:
        forged = context.model_copy(deep=True)
        forged.controls[0] = forged.controls[0].model_copy(update=change)
        with pytest.raises(ValueError, match=svc.INVALID):
            validate(d, svc, args, forged, observation)
    for forged in (None, context.model_copy(update={"controls": context.controls[:-1]}),
                   context.model_copy(update={"controls": [*context.controls, context.controls[0]]})):
        with pytest.raises(ValueError, match=svc.INVALID):
            validate(d, svc, args, forged, observation)
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, context, observation.model_copy(update={"reviewed_text": ["Caller value"]}))

    request = browser_request(order, context.model_copy(update={"grant_digest": "a" * 64}), observation)
    sent = []
    monkeypatch.setattr(ai, "_provider_call", lambda body: sent.append(body))
    client, headers = signed_client(monkeypatch, caller)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-ai-call"
    denied = client.post(path, json=request.model_dump(), headers=headers(path))
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == svc.INVALID
    with d["factory"]() as session:
        saved = json.loads(session.get(VentureDocument, order["id"]).provenance_json)
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        assert not saved.get("invocations") and not trial["attempts"]
    assert sent == []


def test_signed_actor_submit_joins_stub_usage_child_effect_request_and_trial_once(journey_db, monkeypatch):
    from app.build import device_browser_ai as ai, device_effects as effects
    from app.db.models import VentureDocument
    from core.browser_qualification import request_binding_digest
    from tests.unit.product.test_device_browser_ai import response
    d = journey_db
    svc, _, caller, order, grant, args = seeded_trial(d, monkeypatch)
    record_initial_read(svc, grant)
    context, observation = fixture_context(grant)
    request = browser_request(order, context, observation)
    sent = []
    reply = json.dumps({"actions": [{"op": "click", "element_id": context.controls[-1].id}], "memory": "", "subgoal_done": False})
    monkeypatch.setattr(ai, "_provider_call", lambda body: (sent.append(body), response(reply))[1])
    client, headers = signed_client(monkeypatch, caller)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-ai-call"
    result = client.post(path, json=request.model_dump(), headers=headers(path))
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["authority"] == "proposal_only" and value["task_verified"] is False
    rule = next(rule for rule in grant.rules if rule.method == "POST")
    proposal = value["effect_proposals"][0]
    assert proposal["qualification_rule_ids"] == [rule.rule_id] and len(sent) == 1
    child, effect_digest = proposal["invocation_id"], "a" * 64
    assert effects.admit(*args, invocation_id=child, observation_digest=request.observation.digest(), effect_digest=effect_digest)
    assert not effects.admit(*args, invocation_id=child, observation_digest=request.observation.digest(), effect_digest=effect_digest)
    binding = {"grant_digest": grant.grant_digest, "rule_id": rule.rule_id, "request_id": "actor_submit_once",
        "request_digest": request_binding_digest(grant, rule, "actor_submit_once")}
    assert effects.bind_qualification(*args, invocation_id=child, effect_digest=effect_digest, qualification=binding)
    with d["factory"]() as session:
        saved = json.loads(session.get(VentureDocument, order["id"]).provenance_json)
        execution_id = saved["execution_id"]
        assert saved["invocations"][request.invocation_id]["state"] == "completed"
        assert saved["invocations"][child]["proposal"]["qualification_rule_ids"] == [rule.rule_id]
        assert saved["invocations"][child]["no_provider_io"] is True
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        attempt = trial["attempts"][order["id"] + ":" + request.invocation_id]
        assert attempt["state"] == "known" and attempt["actual_micro_usd"] > 0 and len(trial["attempts"]) == 1
    fixture_headers = {"X-Supymem-Qualification-Request": binding["request_id"],
        "X-Supymem-Qualification-Invocation": child, "X-Supymem-Qualification-Effect": effect_digest,
        "X-Supymem-Qualification-Execution": execution_id}
    fields = {field.name: field.value for field in rule.fields}
    submitted = client.post(rule.path, data=fields, headers=fixture_headers, follow_redirects=False)
    assert submitted.status_code == 303
    assert client.post(rule.path, data=fields, headers=fixture_headers, follow_redirects=False).status_code == 409
    effects.receipt(*args, invocation_id=child, effect_digest=effect_digest, outcome="returned")
    with d["factory"]() as session:
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        assert sum(row["mutated"] for row in trial["requests"].values()) == 1
    assert len(sent) == 1


def test_context_cannot_supply_prerequisite_proof_or_continue_after_unknown_response(journey_db, monkeypatch):
    d = journey_db
    svc, _, _, _, grant, args = seeded_trial(d, monkeypatch)
    context, observation = fixture_context(grant)
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, context, observation)
    context.completed_rules = [grant.rules[0].rule_id]
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, context, observation)
    context.completed_rules = []
    request_id = record_initial_read(svc, grant, delivered=False)
    with pytest.raises(ValueError, match="browser_qualification_request_protected"):
        validate(d, svc, args, context, observation)
    svc.record_response_delivery(grant.nonce, request_id)
    assert validate(d, svc, args, context, observation) == grant


def test_current_trial_grant_device_flag_and_budget_are_rechecked_from_database(journey_db, monkeypatch):
    from app.db.models import VentureDocument
    from app.db.supymem_models import DesktopDevice
    d = journey_db
    svc, orders, caller, _, grant, args = seeded_trial(d, monkeypatch)
    record_initial_read(svc, grant)
    context, observation = fixture_context(grant)
    with d["factory"]() as session:
        original = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
    changes = [("state", "closed", svc.UNAVAILABLE), ("expires_at", 1, svc.INVALID),
        ("budget_breached", True, svc.UNAVAILABLE)]
    for key, value, code in changes:
        with d["factory"]() as session:
            session.get(VentureDocument, grant.trial_id).provenance_json = json.dumps(original | {key: value})
            session.commit()
        with pytest.raises(ValueError, match=code):
            validate(d, svc, args, context, observation)
    forged = json.loads(json.dumps(original))
    forged["grants"][grant.order_id]["actor_id"] = "usr_foreign"
    with d["factory"]() as session:
        session.get(VentureDocument, grant.trial_id).provenance_json = json.dumps(forged)
        session.commit()
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, context, observation)
    with d["factory"]() as session:
        session.get(VentureDocument, grant.trial_id).provenance_json = json.dumps(original)
        session.get(DesktopDevice, caller.device_id).revoked_at = orders.utcnow()
        session.commit()
    with pytest.raises(ValueError, match=svc.UNAVAILABLE):
        validate(d, svc, args, context, observation)
    with d["factory"]() as session:
        session.get(DesktopDevice, caller.device_id).revoked_at = None
        session.commit()
    monkeypatch.delenv(svc.ENABLED_ENV)
    with pytest.raises(ValueError, match=svc.UNAVAILABLE):
        validate(d, svc, args, context, observation)


def test_context_without_fixture_grant_does_not_create_authority(journey_db, monkeypatch):
    from app.build import device_invocations as calls
    d = journey_db
    svc, orders, _, _, grant, args = seeded_trial(d, monkeypatch)
    context, observation = fixture_context(grant)
    with d["factory"]() as session:
        _, body, account = calls._locked(session, *args)
        del body["browser_qualification_scope"], body["browser_qualification_grant"]
        assert svc.validate_runtime_context(session, account, body, None, observation=observation,
            current_url=observation.location) is None
        with pytest.raises(ValueError, match=svc.INVALID):
            svc.validate_runtime_context(session, account, body, context, observation=observation, current_url=observation.location)
        binding = grant.trial_binding()
        body["browser_qualification_scope"] = svc.scope_policy(binding)
        body["browser_qualification_trial_binding"] = binding.to_dict()
        body["review_digest"] = orders._digest(body)
        with pytest.raises(ValueError, match=svc.INVALID):
            svc.validate_runtime_context(session, account, body, context, observation=observation, current_url=observation.location)


def test_consumed_rule_cannot_reoffer_literals_or_submit_and_is_withheld_from_actor(journey_db, monkeypatch):
    from app.build import device_effects as effects
    d = journey_db
    svc, _, _, _, grant, args = seeded_trial(d, monkeypatch)
    record_initial_read(svc, grant)
    context, observation = fixture_context(grant)
    headers = source_effect(d, grant, args)
    rule = next(rule for rule in grant.rules if rule.method == "POST")
    response, request_id = svc.receive_fixture_request(grant.nonce, path=rule.path, query=rule.query, method="POST",
        submitted_fields=tuple((field.name, field.value) for field in rule.fields), body_kind=rule.body_kind,
        request_id=headers["X-Supymem-Qualification-Request"], invocation_id=headers["X-Supymem-Qualification-Invocation"],
        effect_digest=headers["X-Supymem-Qualification-Effect"], execution_id=headers["X-Supymem-Qualification-Execution"])
    assert response.status == 303
    svc.record_response_delivery(grant.nonce, request_id)
    effects.receipt(*args, invocation_id=headers["X-Supymem-Qualification-Invocation"],
        effect_digest=headers["X-Supymem-Qualification-Effect"], outcome="returned")
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, context, observation)
    passive = context.model_copy(update={"controls": []})
    passive_observation = observation.model_copy(update={"controls": [], "reviewed_text": []})
    assert validate(d, svc, args, passive, passive_observation) == grant
    assert rule.rule_id in passive.completed_rules and rule.rule_id not in passive._eligible_rule_ids


def test_counter_current_controls_and_exhausted_passive_verify_keep_exact_custody(journey_db, monkeypatch):
    from app.build import device_browser_ai as ai, device_effects as effects, device_invocations as calls
    from app.db.models import VentureDocument
    from coding_agents.jev_decision import Observation
    from core.browser_qualification import request_binding_digest
    from tests.unit.product.test_device_browser_ai import response
    d = journey_db
    svc, _, _, order, grant, args = seeded_trial(d, monkeypatch, task_id="counter")
    record_initial_read(svc, grant)
    first = next(rule for rule in grant.rules if rule.method == "POST")
    context = ai.QualificationContext(grant_digest=grant.grant_digest, controls=[{
        "id": "c0", "document_path": first.document_path, "document_id": first.document_id,
        "container_id": first.form_id, "control_id": first.control_id, "tag": "BUTTON", "input_type": "button"}])
    observation = Observation(revision="synthetic-counter", application="com.google.Chrome", location=grant.origin + first.document_path,
        goal=order["manifest"]["instruction"], context="Count: 0", controls=[{"id": "c0", "label": "Increment",
            "operations": ["click"], "visible": True, "enabled": True}], permitted=True,
        confirmation_pending=False, prior_effect_uncertain=False)
    assert validate(d, svc, args, context, observation) == grant
    for index, rule in enumerate(rule for rule in grant.rules if rule.method == "POST"):
        invocation_id, effect_digest = f"counter_effect_{index}", str(index + 1) * 64
        headers = source_effect(d, grant, args, request_id=f"counter_request_{index}",
            invocation_id=invocation_id, effect_digest=effect_digest)
        # Trusted journal fixture only: select this exact seeded count rule.
        # The real handler, prerequisites and delivery receipts remain active.
        with d["factory"]() as session:
            row, body, _ = calls._locked(session, *args)
            body["invocations"][invocation_id]["effect"]["qualification"].update(rule_id=rule.rule_id,
                request_digest=request_binding_digest(grant, rule, headers["X-Supymem-Qualification-Request"]))
            row.provenance_json = json.dumps(body)
            session.commit()
        result, request_id = svc.receive_fixture_request(grant.nonce, path=rule.path, query=rule.query, method="POST",
            submitted_fields=tuple((field.name, field.value) for field in rule.fields), body_kind=rule.body_kind,
            request_id=headers["X-Supymem-Qualification-Request"], invocation_id=invocation_id,
            effect_digest=effect_digest, execution_id=headers["X-Supymem-Qualification-Execution"])
        assert result.status == 204
        svc.record_response_delivery(grant.nonce, request_id)
        effects.receipt(*args, invocation_id=invocation_id, effect_digest=effect_digest, outcome="returned")
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, context, observation)
    final_context = context.model_copy(update={"controls": []})
    final_observation = observation.model_copy(update={"controls": [], "context": "Count: 5"})
    assert validate(d, svc, args, final_context, final_observation) == grant
    assert not set(final_context._eligible_rule_ids) & {rule.rule_id for rule in grant.rules if rule.method == "POST"}
    request = browser_request(order, final_context, final_observation)
    request.phase, request.invocation_id = "verify", "counter_final_verify"
    notes = json.loads(request.context.notes)
    notes["current"]["source"].update(facts=["Count: 5"], fields=[])
    request.context.notes = json.dumps(notes)
    sent = []
    monkeypatch.setattr(ai, "_provider_call", lambda body: (sent.append(body), response('{"passed":true,"reason":"Count is five"}'))[1])
    result = ai.run(*args, request=request)
    assert result["effect_proposals"] == [] and result["task_verified"] is False and len(sent) == 1
    with d["factory"]() as session:
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        assert trial["task_states"]["counter"]["current"]["state"]["count"] == 5
        assert sum(receipt["mutated"] for receipt in trial["requests"].values()) == 5
        assert len(trial["attempts"]) == 1  # Only the final local provider stub was metered.


@pytest.mark.parametrize("operation", ["type", "click"])
def test_select_and_check_only_grants_keep_source_proposal_and_effect_custody(journey_db, monkeypatch, operation):
    from app.build import device_effects as effects, device_invocations as calls
    from app.db.models import VentureDocument
    from coding_agents.jev_decision import Candidate
    d = journey_db
    svc, orders, _, order, grant, args = seeded_trial(d, monkeypatch, task_id="select_prefs", claim=False)
    # Trusted test producer: preserve the complete review/trial seals while
    # proving the operation mapping without a generic type/click allowance.
    allowed = tuple(value for value in grant.allowed_operations if value not in {"type", "click"})
    draft = replace(grant, allowed_operations=allowed)
    with d["factory"]() as session:
        row = session.get(VentureDocument, order["id"])
        body = json.loads(row.provenance_json)
        body["browser_qualification_scope"] = svc.scope_policy(draft)
        body["review_digest"] = orders._digest(body)
        grant = replace(draft, review_digest=body["review_digest"])
        body["browser_qualification_grant"] = grant.to_dict()
        row.provenance_json = json.dumps(body)
        trial_row = session.get(VentureDocument, grant.trial_id)
        trial = json.loads(trial_row.provenance_json)
        trial["grants"][grant.order_id] = grant.to_dict()
        trial_row.provenance_json = json.dumps(trial)
        session.commit()
    for action in ("approve", "claim"):
        orders.transition(*args, grant.review_digest, action)
    record_initial_read(svc, grant)
    context, observation = fixture_context(grant, submit=False)
    assert validate(d, svc, args, context, observation) == grant
    target = next(control for control in observation.controls if operation in control.operations)
    parent_id = "bai_" + "1" * 32
    actor_parent(d, grant, args, parent_id)
    candidate = Candidate(operation, target.id, observation.digest(), value_index=0 if operation == "type" else None)
    child = calls.save_browser_actor_proposal(*args, invocation_id=parent_id, candidate=candidate, qualification_rule_ids=[])
    assert effects.admit(*args, invocation_id=child, observation_digest=observation.digest(), effect_digest="a" * 64)
    assert not effects.admit(*args, invocation_id=child, observation_digest=observation.digest(), effect_digest="a" * 64)


def test_links_frames_tabs_and_null_literal_read_controls_stay_in_exact_scope(journey_db, monkeypatch):
    from app.build.device_browser_ai import QualificationContext
    from coding_agents.jev_decision import Observation
    d = journey_db
    svc, _, _, _, grant, args = seeded_trial(d, monkeypatch, task_id="multitab")
    record_initial_read(svc, grant)
    current, destination = grant.rules[:2]
    observation = Observation(revision="synthetic-link", application="com.google.Chrome", location=grant.origin + current.path,
        goal="Synthetic compare", context="Compare source pages", controls=[{"id": "c0", "label": "Source B",
            "operations": ["click"], "visible": True, "enabled": True}],
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False)
    context = QualificationContext(grant_digest=grant.grant_digest, controls=[{"id": "c0", "document_path": current.document_path,
        "document_id": current.document_id, "container_id": "", "control_id": "", "tag": "A"}],
        links=[{"id": "c0", "href": grant.origin + destination.path}], tabs=[{"id": "t" + "a" * 32, "url": observation.location}])
    assert validate(d, svc, args, context, observation) == grant
    foreign = context.model_copy(deep=True)
    foreign.links[0].href += "?unreviewed=true"
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, foreign, observation)
    foreign = context.model_copy(deep=True)
    foreign.tabs[0].url = grant.origin + current.path.replace(grant.nonce, "a" * len(grant.nonce))
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, foreign, observation)
    duplicated = context.model_copy(update={"tabs": context.tabs * 3}, deep=True)
    with pytest.raises(ValueError, match=svc.INVALID):
        validate(d, svc, args, duplicated, observation)
