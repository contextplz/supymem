"""Core-only capability and source receipt boundaries; providers and fixture HTTP stay offline."""
from dataclasses import replace
import hashlib
import json

import pytest

from coding_agents.cu_ai_loop import parse_action
from coding_agents.jev_decision import Observation
from coding_agents.owned_browser import cuai_document_target
from tests.unit.product.test_browser_qualification_contract import form_grant
from tests.unit.product.test_browser_qualification_runtime import claimed_gateway, fixture_http_response


@pytest.fixture(autouse=True)
def _company_os():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)


def capability_source(monkeypatch, *, operations=None, tabs=1, recovery=False):
    from app.build import device_browser_ai as ai
    from tests.unit.product.test_device_browser_ai import configure
    original = form_grant()
    first = original.rules[0]
    second = replace(first, rule_id="read_second", path=first.path + "-second", document_path=first.path + "-second",
                     document_id="document-second")
    operations = operations or ("navigate", "extract", "scroll", "wait", "click", "hover", "new_tab", "switch_tab")
    grant = replace(original, rules=(first, second), max_tabs=2 if "new_tab" in operations else 1, allowed_operations=operations)
    controls = [] if recovery else [{"id": "c0", "label": "Support", "operations": ["click"], "visible": True, "enabled": True}]
    observation = Observation(revision="ps1_capabilities", application="com.google.Chrome", location=grant.origin + first.path,
        goal="Read the exact reviewed fixture pages", context="PAGE: fixture", controls=controls, reviewed_text=[], permitted=True,
        confirmation_pending=False, prior_effect_uncertain=False)
    context = ai.QualificationContext(grant_digest=grant.grant_digest, controls=[] if recovery else [{"id": "c0",
        "document_path": first.document_path, "document_id": first.document_id, "container_id": "", "control_id": "support", "tag": "DIV"}],
        tabs=[{"id": "t" + ("a" if index == 0 else "b") * 32,
               "url": grant.origin + (first.path if index == 0 else second.path)} for index in range(tabs)], completed_rules=[first.rule_id])
    context._eligible_rule_ids = (first.rule_id, second.rule_id)
    context._recovery_only = recovery
    profile = configure(monkeypatch)
    general = {"execution_profile": profile.model_dump(), "allow_type": False, "fill_values": {}}
    saved = {"id": grant.order_id, "review_digest": grant.review_digest, "browser_qualification_scope": {},
             "browser_qualification_grant": grant.to_dict(), "manifest": {"instruction": observation.goal,
             "actions": ["browser.read", "browser.interact"], "general_browser_verification": general}}
    load = {"qualified": not recovery, "session_id": "s" * 32, "page_id": "a" * 32, "document_id": "d" * 32,
        "sequence": 1, "location": observation.location, "observation_digest": observation.digest(), "ready_state": "complete",
        "visible_text": True, "visibility_scope": "bounded_dom_text_geometry", "response": {
            "url": observation.location, "status": 503 if recovery else 200, "sequence": 1, "complete": True}}
    load["session_id"] = "c" * 32
    if recovery:
        load["kind"] = "qualification_error"
        load["response"].update(request_id="bqr_error", sha256=hashlib.sha256(b"Retry later").hexdigest(),
                                size_bytes=11, mime_type="text/html")
    request = ai.BrowserAIBody(invocation_id="bai_actor", phase="actor", execution_profile_digest=ai.profile_digest(profile),
        checkpoint_digest="c" * 64, observation=observation, current_load=load, context={"qualification": context})
    return ai, grant, saved, request, profile


def action_reply(action):
    return json.dumps({"actions": [action], "memory": "", "subgoal_done": False})


def propose(source, action, **kwargs):
    ai, grant, saved, request, _ = source
    return ai._candidate(action_reply(action), request.observation, saved["manifest"]["general_browser_verification"], False,
        fixture=grant, qualification=request.context.qualification, current_load=request.current_load.model_dump(), **kwargs)


def offered(body):
    variants = body["text"]["format"]["schema"]["properties"]["actions"]["items"]["anyOf"]
    return {op for variant in variants for op in variant["properties"]["op"]["enum"]}


def test_grant_capabilities_reach_schema_and_exact_direct_new_tab_without_dom_link(monkeypatch):
    source = capability_source(monkeypatch)
    ai, grant, saved, request, profile = source
    body = ai.request_body(saved, request, profile, request.observation)
    assert {"hover", "new_tab"} <= offered(body)
    assert "switch_tab" not in offered(body)  # Only the current owned tab exists.
    action = {"op": "new_tab", "text": grant.origin + grant.rules[1].path}
    candidate = propose(source, action)
    assert candidate.target == cuai_document_target(request.current_load.model_dump(), parse_action(action))
    assert candidate.operation == "new_tab" and candidate.value_index is None
    assert "CONTROLLER-REVIEWED DOCUMENT URLS" in body["input"][1]["content"][0]["text"]


def test_hover_and_switch_bind_exact_observed_control_and_owned_tab(monkeypatch):
    source = capability_source(monkeypatch, tabs=2)
    ai, _, saved, request, profile = source
    assert propose(source, {"op": "hover", "element_id": "c0"}).target == "c0"
    assert propose(source, {"op": "switch_tab", "element_id": "t" + "b" * 32}).target == "t" + "b" * 32
    body = ai.request_body(saved, request, profile, request.observation)
    variants = body["text"]["format"]["schema"]["properties"]["actions"]["items"]["anyOf"]
    switch = next(variant for variant in variants if variant["properties"]["op"]["enum"] == ["switch_tab"])
    assert switch["properties"]["element_id"]["enum"] == ["t" + "b" * 32]


def test_hover_only_control_reaches_schema_without_any_click_authority(monkeypatch):
    source = capability_source(monkeypatch, operations=("navigate", "hover", "wait"))
    ai, _, saved, request, profile = source
    request.context.qualification.controls[0].hover_only = True
    body = ai.request_body(saved, request, profile, request.observation)
    assert "hover" in offered(body) and "click" not in offered(body)
    assert propose(source, {"op": "hover", "element_id": "c0"}).target == "c0"
    with pytest.raises(ValueError):
        propose(source, {"op": "click", "element_id": "c0"})


def test_hover_only_source_is_excluded_from_click_schema_even_when_grant_allows_click(monkeypatch):
    source = capability_source(monkeypatch)
    ai, _, saved, request, profile = source
    request.context.qualification.controls[0].hover_only = True
    body = ai.request_body(saved, request, profile, request.observation)
    assert "hover" in offered(body) and "click" not in offered(body)
    with pytest.raises(ValueError, match="target_invalid"):
        propose(source, {"op": "click", "element_id": "c0"})


@pytest.mark.parametrize("action", [
    {"op": "hover", "element_id": "c9"},
    {"op": "switch_tab", "element_id": "t" + "a" * 32},
    {"op": "switch_tab", "element_id": "t" + "f" * 32},
    {"op": "new_tab", "text": "https://foreign.invalid/public/browser-qualification/form"},
    {"op": "new_tab", "text": form_grant().origin + form_grant().rules[0].path + "?unreviewed=1"},
])
def test_foreign_stale_and_current_targets_cannot_create_capability_effects(monkeypatch, action):
    with pytest.raises(ValueError):
        propose(capability_source(monkeypatch), action)


def test_full_tab_limit_and_consumed_read_rule_refuse_direct_new_tab(monkeypatch):
    source = capability_source(monkeypatch, tabs=2)
    _, grant, _, request, _ = source
    action = {"op": "new_tab", "text": grant.origin + grant.rules[1].path}
    with pytest.raises(ValueError, match="target_invalid"):
        propose(source, action)
    request.context.qualification.tabs.pop()
    request.context.qualification._eligible_rule_ids = (grant.rules[0].rule_id,)
    with pytest.raises(ValueError, match="target_invalid"):
        propose(source, action)


def test_direct_new_tab_never_treats_reviewed_download_as_document(monkeypatch):
    from core.browser_qualification import FixtureDownload
    source = capability_source(monkeypatch)
    ai, grant, saved, request, profile = source
    contract = FixtureDownload(grant.rules[1].rule_id, "fixture.csv", "text/csv", 1, hashlib.sha256(b"x").hexdigest())
    grant = replace(grant, downloads=(contract,), allowed_operations=grant.allowed_operations + ("download",))
    request.context.qualification.grant_digest = grant.grant_digest
    saved["browser_qualification_grant"] = grant.to_dict()
    updated = (ai, grant, saved, request, profile)
    url = grant.origin + grant.rules[1].path
    with pytest.raises(ValueError, match="target_invalid"):
        propose(updated, {"op": "new_tab", "text": url})
    body = ai.request_body(saved, request, profile, request.observation)
    document_urls = body["input"][1]["content"][0]["text"].split("CONTROLLER-REVIEWED DOCUMENT URLS", 1)[1]
    assert url not in document_urls


@pytest.mark.parametrize("action", [{"op": "hover", "element_id": "c0"},
    {"op": "new_tab", "text": form_grant().origin + form_grant().rules[0].path},
    {"op": "switch_tab", "element_id": "t" + "b" * 32}])
def test_no_grant_v1_keeps_capability_operations_unavailable(monkeypatch, action):
    ai, _, saved, request, _ = capability_source(monkeypatch)
    with pytest.raises(ValueError, match="reply_invalid"):
        ai._candidate(action_reply(action), request.observation, saved["manifest"]["general_browser_verification"], False)


def test_known_error_load_admits_only_recovery_schema_and_exact_get(monkeypatch):
    source = capability_source(monkeypatch, recovery=True)
    ai, grant, saved, request, profile = source
    assert isinstance(request.current_load, ai.QualificationErrorLoadProof)
    body = ai.request_body(saved, request, profile, request.observation)
    assert offered(body) == {"navigate", "wait", "fail", "needs_you"}
    action = {"op": "navigate", "text": grant.origin + grant.rules[1].path}
    candidate = propose(source, action)
    assert candidate.target == cuai_document_target(request.current_load.model_dump(), parse_action(action))
    assert "KNOWN FAILED FIXTURE DOCUMENT" in body["input"][0]["content"][0]["text"]


@pytest.mark.parametrize("action", [{"op": "done", "summary": "Success"}, {"op": "click", "element_id": "c0"},
    {"op": "type", "element_id": "c0", "text": "Atlas"}, {"op": "extract", "text": "Retry"},
    {"op": "navigate", "text": form_grant().origin + form_grant().rules[0].path + "?unreviewed=1"}])
def test_known_error_is_never_completion_or_mutation_authority(monkeypatch, action):
    with pytest.raises(ValueError):
        propose(capability_source(monkeypatch, recovery=True), action)


@pytest.mark.parametrize("path,value", [("qualified", True), ("qualified", 0), ("visible_text", 1),
    ("kind", "ordinary"), ("response.complete", 1), ("response.status", 200),
    ("response.sequence", 2), ("response.url", "https://foreign.invalid/"),
    ("response.size_bytes", True), ("response.size_bytes", 65537), ("response.mime_type", "text/html; charset=utf-8"),
    ("response.request_id", ""), ("response.sha256", "unknown")])
def test_known_error_dto_requires_exact_typed_source_proof(monkeypatch, path, value):
    ai, _, _, request, _ = capability_source(monkeypatch, recovery=True)
    wire = request.model_dump()
    target = wire["current_load"]
    field = path
    if "." in path:
        parent, field = path.split(".")
        target = target[parent]
    target[field] = value
    with pytest.raises(ValueError):
        ai.BrowserAIBody.model_validate(wire)


def exact_read_source(*, status_code=200, body=b"Synthetic source response"):
    gateway, grant, _, status, _, _ = claimed_gateway()
    rule = grant.rules[0]
    response = fixture_http_response(gateway, grant, rule, body, status=status_code)
    request_id = response.headers["x-supymem-qualification-request"]
    proof = {"complete": True, "url": grant.origin + rule.path, "request_id": request_id, "status": status_code,
             "sha256": hashlib.sha256(body).hexdigest(), "size_bytes": len(body), "mime_type": "text/html"}
    return gateway, grant, status, proof, gateway._fixture_read_receipts[request_id]


@pytest.mark.parametrize("status_code,state", [(200, "committed"), (503, "failed_read")])
def test_gateway_read_receipt_joins_exact_source_size_mime_and_known_error(status_code, state):
    gateway, _, _, proof, receipt = exact_read_source(status_code=status_code)
    assert gateway.qualification_read_receipt(proof) == receipt
    assert receipt["state"] == state and receipt["mutated"] is False


@pytest.mark.parametrize("field,value", [("response_size_bytes", 999), ("response_size_bytes", True),
    ("response_mime_type", "text/plain"), ("response_digest", "f" * 64), ("server_response_sent", False),
    ("mutated", 1), ("device_id", "foreign"), ("execution_id", "foreign"), ("path", "/unreviewed"),
    ("state", "pending")])
def test_gateway_read_receipt_rejects_changed_source_or_custody(field, value):
    gateway, _, _, proof, receipt = exact_read_source()
    receipt[field] = value
    with pytest.raises(PermissionError, match="receipt_unconfirmed"):
        gateway.qualification_read_receipt(proof)


def test_gateway_receipt_size_boolean_never_aliases_one_byte_source():
    gateway, _, _, proof, receipt = exact_read_source(body=b"x")
    receipt["response_size_bytes"] = True
    with pytest.raises(PermissionError, match="receipt_unconfirmed"):
        gateway.qualification_read_receipt(proof)


@pytest.mark.parametrize("field,value", [("size_bytes", True), ("size_bytes", 1048577), ("mime_type", "text/html; charset=utf-8"),
    ("complete", 1), ("url", "https://foreign.invalid/"), ("request_id", "foreign"), ("status", 303)])
def test_gateway_read_source_proof_itself_must_be_closed_and_typed(field, value):
    gateway, _, _, proof, _ = exact_read_source()
    proof[field] = value
    with pytest.raises(PermissionError, match="receipt_unconfirmed"):
        gateway.qualification_read_receipt(proof)


@pytest.mark.parametrize("value", [["foreign"], ["read_form", "read_form"], [], [True], "read_form"])
def test_gateway_status_read_prerequisites_cannot_borrow_foreign_or_missing_completion(value):
    gateway, _, _, status, _, _ = claimed_gateway()
    status["read_prerequisites"] = value
    with pytest.raises(PermissionError, match="status_unconfirmed"):
        gateway.qualification_order_status()


def test_gateway_status_allows_server_safe_read_prerequisites_without_asserting_completed_effect():
    gateway, _, _, status, _, _ = claimed_gateway()
    status["read_prerequisites"] = ["read_form", "submit_form"]
    assert gateway.qualification_order_status()["completed_rules"] == ["read_form"]
    assert gateway.qualification_order_status()["read_prerequisites"] == ["read_form", "submit_form"]
