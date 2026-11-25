"""Fixture literals and submit rule IDs cross the actual actor boundary without widening v1."""
from contextlib import nullcontext
from dataclasses import replace
import json

import pytest

from core.browser_qualification import FixtureField
from coding_agents.jev_decision import Observation
from tests.unit.product.test_browser_qualification_contract import form_grant


@pytest.fixture(autouse=True)
def _company_os():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)


def source(*, preferences=False):
    from app.build import device_browser_ai as ai
    grant = form_grant()
    if preferences:
        fields = (FixtureField("country", "Portugal", "country", "select"),
                  FixtureField("news", "yes", "news", "checkbox"),
                  FixtureField("plan", "pro", "plan-pro", "radio"))
        grant = replace(grant, task_id="select_prefs", rules=(grant.rules[0], replace(grant.rules[1], fields=fields)),
                        allowed_operations=("navigate", "select", "check", "click", "extract"))
    rule = grant.rules[1]
    metadata, controls, values = [], [], []
    for index, field in enumerate(rule.fields):
        operation = "click" if field.kind in {"checkbox", "radio"} else "type"
        value = field.value if field.kind == "select" else field.input_value
        if operation == "type" and value not in values:
            values.append(value)
        control_id = "c" + str(index)
        controls.append({"id": control_id, "label": field.name, "operations": [operation], "visible": True, "enabled": True})
        metadata.append({"id": control_id, "document_path": rule.document_path, "document_id": rule.document_id,
                         "container_id": rule.form_id, "control_id": field.control_id,
                         "tag": "SELECT" if field.kind == "select" else "INPUT",
                         "input_type": "select-one" if field.kind == "select" else field.kind if operation == "click" else field.input_type,
                         "input_value": field.input_value, "kind": field.kind,
                         "select_value": field.value if field.kind in {"select", "checkbox", "radio"} else None})
    controls.append({"id": "c3", "label": "Submit", "operations": ["click"], "visible": True, "enabled": True})
    metadata.append({"id": "c3", "document_path": rule.document_path, "document_id": rule.document_id,
                     "container_id": rule.form_id, "control_id": rule.control_id, "tag": "BUTTON", "input_type": "submit"})
    observation = Observation(revision="ps1_test", application="com.google.Chrome", location=grant.origin + rule.document_path,
        goal="Complete the reviewed fixture form", context="PAGE: Fixture form", controls=controls, reviewed_text=values,
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False)
    qualification = ai.QualificationContext(grant_digest=grant.grant_digest, controls=metadata, completed_rules=[grant.rules[0].rule_id])
    general = {"execution_profile": {"version": "browser_cuai_text_v1"}, "fill_values": {}, "allow_type": False}
    return ai, grant, observation, qualification, general


def reply(action):
    return json.dumps({"actions": [action], "memory": "", "subgoal_done": False})


def candidate(action, *, preferences=False, mutate=None):
    ai, grant, observation, qualification, general = source(preferences=preferences)
    if mutate:
        mutate(qualification)
    return ai._candidate(reply(action), observation, general, False, fixture=grant, qualification=qualification)


@pytest.mark.parametrize("action,index", [
    ({"op": "type", "element_id": "c0", "text": "Atlas"}, 0),
    ({"op": "type", "element_id": "c1", "text": "qa@example.invalid"}, 1),
    ({"op": "type", "element_id": "c2", "text": "ABC123"}, 2),
])
def test_fixture_type_uses_exact_field_literal_without_fill_values(action, index):
    assert candidate(action).value_index == index


def test_fixture_select_and_checks_use_existing_delivery_shapes():
    assert candidate({"op": "type", "element_id": "c0", "text": "Portugal"}, preferences=True).value_index == 0
    for target in ("c1", "c2"):
        result = candidate({"op": "click", "element_id": target}, preferences=True)
        assert result.operation == "click" and result.value_index is None


@pytest.mark.parametrize("action,mutate", [
    ({"op": "type", "element_id": "c0", "text": "qa@example.invalid"}, None),
    ({"op": "type", "element_id": "c0", "text": "Changed"}, None),
    ({"op": "type", "element_id": "c0", "text": "Atlas"}, lambda ctx: setattr(ctx.controls[0], "control_id", "foreign")),
    ({"op": "type", "element_id": "c0", "text": "Atlas"}, lambda ctx: setattr(ctx.controls[0], "input_type", "email")),
    ({"op": "type", "element_id": "c0", "text": "Atlas"}, lambda ctx: setattr(ctx.controls[0], "container_id", "foreign")),
    ({"op": "type", "element_id": "c0", "text": "Atlas"}, lambda ctx: setattr(ctx, "completed_rules", [])),
    ({"op": "click", "element_id": "c0"}, None),
])
def test_wrong_field_value_identity_type_and_prerequisite_fail_closed(action, mutate):
    with pytest.raises(ValueError):
        candidate(action, mutate=mutate)


def test_literal_kind_operations_and_metadata_cannot_create_input_authority():
    ai, grant, observation, qualification, general = source()
    action = {"op": "type", "element_id": "c0", "text": "Atlas"}
    for changes in ({"allowed_operations": ("click",)}, {"rules": (grant.rules[0],)}):
        with pytest.raises(ValueError):
            ai._candidate(reply(action), observation, general, False, fixture=replace(grant, **changes), qualification=qualification)
    qualification.controls[0].input_value = "Invented"
    with pytest.raises(ValueError, match="type_unreviewed"):
        ai._candidate(reply(action), observation, general, False, fixture=grant, qualification=qualification)
    with pytest.raises(ValueError, match="type_unreviewed"):
        ai._candidate(reply(action), observation, general, False)


def test_fixture_submit_proposes_only_exact_source_bound_post_rules():
    ai, grant, _, qualification, _ = source()
    assert ai._qualification_click_rules(grant, qualification, "c3") == ("submit_form",)
    qualification._eligible_rule_ids = ("read_form",)
    with pytest.raises(ValueError, match="target_invalid"):
        ai._qualification_click_rules(grant, qualification, "c3")
    qualification._eligible_rule_ids = None
    qualification.controls[3].control_id = "foreign-submit"
    with pytest.raises(ValueError, match="target_invalid"):
        ai._qualification_click_rules(grant, qualification, "c3")
    ai, grant, _, qualification, _ = source(preferences=True)
    assert ai._qualification_click_rules(grant, qualification, "c1") == ()


def request_for_source(monkeypatch, *, preferences=False):
    from tests.unit.product.test_device_browser_ai import configure
    ai, grant, observation, qualification, general = source(preferences=preferences)
    profile = configure(monkeypatch)
    general["execution_profile"] = profile.model_dump()
    saved = {"id": grant.order_id, "review_digest": grant.review_digest,
             "browser_qualification_scope": {}, "browser_qualification_grant": grant.to_dict(),
             "manifest": {"instruction": observation.goal, "actions": ["browser.read", "browser.interact"],
                          "general_browser_verification": general}}
    request = ai.BrowserAIBody(invocation_id="actor_one", phase="actor", execution_profile_digest=ai.profile_digest(profile),
        checkpoint_digest="c" * 64, observation=observation, current_load={"qualified": True, "session_id": "a" * 32,
            "page_id": "b" * 32, "document_id": "d" * 32, "sequence": 1, "location": observation.location,
            "observation_digest": observation.digest(), "ready_state": "complete", "visible_text": True,
            "visibility_scope": "bounded_dom_text_geometry", "response": {"url": observation.location, "status": 200, "sequence": 1, "complete": True}},
        context={"qualification": qualification})
    return ai, grant, saved, request, profile


def test_fixture_schema_pairs_each_current_control_with_its_saved_literal(monkeypatch):
    ai, _, saved, request, profile = request_for_source(monkeypatch, preferences=True)
    body = ai.request_body(saved, request, profile, request.observation)
    variants = body["text"]["format"]["schema"]["properties"]["actions"]["items"]["anyOf"]
    fields = [variant for variant in variants if variant["properties"]["op"]["enum"] == ["type"]]
    assert len(fields) == 1
    assert fields[0]["properties"]["element_id"]["enum"] == ["c0"]
    assert fields[0]["properties"]["text"]["enum"] == ["Portugal"]
    for variant in variants:
        if "summary" in variant["properties"]:
            assert "enum" not in variant["properties"]["summary"]
    text = body["input"][1]["content"][0]["text"]
    assert "CONTROLLER-REVIEWED FIXTURE SUBMITS" in text and '"rule_ids": ["submit_form"]' in text
    assert "Select uses op=type" in body["input"][0]["content"][0]["text"]


def test_outer_navigation_offers_match_validated_qualification_projection(monkeypatch):
    from app.build import device_jev
    ai, _, saved, request, _ = request_for_source(monkeypatch)
    href = request.observation.location
    request.context.links = [ai.NavigationTarget(id="c3", href=href, tag="A", form=False)]
    request.context.qualification.links = [ai.QualificationLink(id="c3", href=href)]
    monkeypatch.setattr(device_jev, "_scoped_observation", lambda saved, observation: observation)
    monkeypatch.setattr(ai, "source_notes_card_surfaces", lambda *args: [])
    ai.scoped(saved, request)
    request.context.links[0].href += "?forged=1"
    with pytest.raises(ValueError, match="device_browser_ai_request_invalid"):
        ai.scoped(saved, request)


def test_fixture_navigation_uses_durable_completed_prerequisites():
    ai, grant, observation, qualification, general = source()
    next_rule = replace(grant.rules[0], rule_id="read_next", path=grant.rules[0].path + "-next",
                        document_path=grant.rules[0].path + "-next", prerequisites=("submit_form",))
    grant = replace(grant, rules=grant.rules + (next_rule,))
    qualification.grant_digest = grant.grant_digest
    href = grant.origin + next_rule.path
    links = [ai.NavigationTarget(id="c3", href=href, tag="A", form=False)]
    args = (reply({"op": "navigate", "text": href}), observation, general, False)
    with pytest.raises(ValueError, match="device_browser_ai_target_invalid"):
        ai._candidate(*args, links=links, fixture=grant, qualification=qualification)
    qualification.completed_rules.append("submit_form")
    assert ai._candidate(*args, links=links, fixture=grant, qualification=qualification).target == "c3"


@pytest.mark.parametrize("action,expected_rules", [
    ({"op": "click", "element_id": "c3"}, ["submit_form"]),
    ({"op": "type", "element_id": "c0", "text": "Atlas"}, None),
])
def test_run_validates_authority_then_persists_and_returns_submit_rules(monkeypatch, action, expected_rules):
    from app.build import browser_qualification
    from tests.unit.product.test_device_browser_ai import response
    ai, grant, saved, request, profile = request_for_source(monkeypatch)
    events, stored = [], []
    monkeypatch.setattr(ai.orders, "retrieve", lambda *args: saved)
    monkeypatch.setattr(ai.orders, "db", lambda: lambda: nullcontext(object()))
    monkeypatch.setattr(ai.calls, "_locked", lambda *args: (None, saved, None))
    def validate(*args, **kwargs):
        events.append("validated")
        assert kwargs["observation"] == request.observation and kwargs["current_url"] == request.current_load.location
        return grant
    monkeypatch.setattr(browser_qualification, "validate_runtime_context", validate)
    monkeypatch.setattr(ai, "scoped", lambda *args: (profile, request.observation))
    monkeypatch.setattr(ai.calls, "admit", lambda *args, **kwargs: {"admitted_now": True})
    monkeypatch.setattr(ai.calls, "record_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai.calls, "check_current", lambda *args: None)
    monkeypatch.setattr(ai, "_provider_call", lambda body: (events.append("provider"), response(reply(action)))[1])
    monkeypatch.setattr(ai.calls, "save_browser_actor_proposal", lambda *args, **kwargs: (stored.append(kwargs), "actor_one_a0")[1])
    result = ai.run("ven_one", object(), grant.order_id, request=request)
    assert events == ["validated", "provider"]
    assert result["effect_proposals"][0].get("qualification_rule_ids") == expected_rules
    assert stored[0].get("qualification_rule_ids") == (tuple(expected_rules) if expected_rules else None)


def test_context_rejection_stops_run_before_provider_or_spend(monkeypatch):
    from app.build import browser_qualification
    ai, grant, saved, request, _ = request_for_source(monkeypatch)
    monkeypatch.setattr(ai.orders, "retrieve", lambda *args: saved)
    monkeypatch.setattr(ai.orders, "db", lambda: lambda: nullcontext(object()))
    monkeypatch.setattr(ai.calls, "_locked", lambda *args: (None, saved, None))
    def reject(*args, **kwargs):
        raise ValueError("browser_qualification_binding_invalid")
    monkeypatch.setattr(browser_qualification, "validate_runtime_context", reject)
    monkeypatch.setattr(ai.calls, "admit", lambda *args, **kwargs: pytest.fail("rejected context reserved spend"))
    monkeypatch.setattr(ai, "_provider_call", lambda body: pytest.fail("rejected context reached provider"))
    with pytest.raises(ValueError, match="browser_qualification_binding_invalid"):
        ai.run("ven_one", object(), grant.order_id, request=request)
