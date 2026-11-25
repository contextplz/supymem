"""Owned-fixture extract/scroll/navigate: grant-scoped on the unchanged v1 production profile."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from coding_agents.cu_ai_loop import parse_action
from coding_agents.jev_decision import Control, Observation
from coding_agents.owned_browser import OwnedBrowser, cuai_document_target
from core.browser_qualification import FIXTURE_ORIGIN
from tests.unit.product.test_browser_qualification_contract import form_grant
from tests.unit.product.test_browser_qualification_runtime import claimed_gateway

V1 = {"execution_profile": {"version": "browser_cuai_text_v1"}}


@pytest.fixture(autouse=True)
def _company_os():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)


def observation(location):
    return Observation(revision="ps1_test", application="com.google.Chrome", location=location, goal="Read the form",
        context="PAGE: Fixture", controls=[{"id": "c1", "label": "Form", "operations": ["click"], "visible": True, "enabled": True}],
        reviewed_text=[], permitted=True, confirmation_pending=False, prior_effect_uncertain=False)


def candidate(action, *, fixture, location=None, links=()):
    from app.build import device_browser_ai as ai
    grant = form_grant()
    obs = observation(location or FIXTURE_ORIGIN + grant.rules[0].document_path)
    load = {"session_id": "s", "page_id": "p", "document_id": "d", "sequence": 1,
            "location": obs.location, "observation_digest": obs.digest()}
    reply = json.dumps({"actions": [action], "memory": "", "subgoal_done": False})
    return ai._candidate(reply, obs, V1, False, current_load=load, links=list(links), fixture=fixture), load


def link(href):
    from app.build import device_browser_ai as ai
    return ai.NavigationTarget(id="c1", href=href, tag="A", form=False)


def test_grant_named_reads_and_reviewed_navigation_are_proposed_on_v1():
    grant = form_grant()
    action = {"op": "extract", "text": "Project"}
    result, load = candidate(action, fixture=grant)
    assert (result.operation, result.target) == ("extract", cuai_document_target(load, parse_action(action)))
    href = FIXTURE_ORIGIN + grant.rules[0].path
    result, _ = candidate({"op": "navigate", "text": href}, fixture=grant, links=[link(href)])
    assert (result.operation, result.target) == ("navigate", "c1")


@pytest.mark.parametrize("action,links,code", [
    ({"op": "scroll", "direction": "down", "amount": 2}, (), "reply_invalid"),  # not in this grant's operations
    ({"op": "navigate", "text": FIXTURE_ORIGIN + form_grant().rules[0].path + "?x=1"},
     [FIXTURE_ORIGIN + form_grant().rules[0].path + "?x=1"], "target_invalid"),  # offered but not a reviewed GET rule
    ({"op": "navigate", "text": FIXTURE_ORIGIN + "/public/browser-qualification/" + "b" * 32 + "/form"},
     [FIXTURE_ORIGIN + "/public/browser-qualification/" + "b" * 32 + "/form"], "target_invalid"),  # foreign nonce
    ({"op": "extract", "text": "sk-" + "a" * 32}, (), "reply_invalid"),
])
def test_unreviewed_fixture_reads_and_routes_are_refused(action, links, code):
    with pytest.raises(ValueError, match="device_browser_ai_" + code):
        candidate(action, fixture=form_grant(), links=[link(href) for href in links])


def test_without_grant_v1_production_actor_still_refuses_reads():
    for action in ({"op": "extract", "text": "Project"}, {"op": "scroll", "direction": "down", "amount": 1}):
        with pytest.raises(ValueError, match="device_browser_ai_reply_invalid"):
            candidate(action, fixture=None)


def test_reads_follow_grant_not_profile_version():
    from app.build import device_browser_ai as ai
    v2 = {"execution_profile": {"version": "browser_cuai_text_v2"}}
    assert ai._reads(V1, None) == frozenset()
    assert ai._reads(v2, None) == ai.READS
    assert ai._reads(V1, form_grant()) == {"navigate", "extract"}
    assert ai._reads(v2, replace(form_grant(), allowed_operations=("click",))) == frozenset()


def test_owned_browser_read_dispatch_requires_grant_operation():
    allowed = OwnedBrowser._read_op_allowed
    assert allowed(SimpleNamespace(qualification_grant=form_grant(), cuai_version="browser_cuai_text_v1"), "extract")
    assert not allowed(SimpleNamespace(qualification_grant=form_grant(), cuai_version="browser_cuai_text_v2"), "scroll")
    assert not allowed(SimpleNamespace(qualification_grant=None, cuai_version="browser_cuai_text_v1"), "extract")
    assert allowed(SimpleNamespace(qualification_grant=None, cuai_version="browser_cuai_text_v2"), "scroll")


@pytest.mark.parametrize("variant,accepted", [
    ("current", True), ("foreign", False), ("query", False),
    ("reviewed_query", False), ("suffix", False), ("label", False), ("secret", False),
])
def test_navigation_masks_only_exact_current_grant_nonce_card_digits(variant, accepted):
    original = form_grant()
    # The actual CI nonce has a 13-digit substring that is not card content.
    nonce = "c10e99f57d45c106b22fab4575236503064ccad1b5787507"
    rules = tuple(replace(rule, path=rule.path.replace(original.nonce, nonce),
        document_path=rule.document_path.replace(original.nonce, nonce)) for rule in original.rules)
    if variant == "reviewed_query":
        rules = (replace(rules[0], query="reference=4111111111111111"), rules[1])
    if variant == "suffix":
        rules = (replace(rules[0], path=rules[0].path + "/4111111111111111"), rules[1])
    grant = replace(original, nonce=nonce, rules=rules)
    href = grant.origin + rules[0].path + ("?" + rules[0].query if rules[0].query else "")
    if variant == "foreign":
        href = href.replace(nonce, "b" * 48)
    if variant == "query":
        href += "?reference=4111111111111111"
    if variant == "secret":
        href += "?reference=sk-" + "a" * 32
    control = Control(id="c1", label="4111111111111111" if variant == "label" else "Reviewed source",
        operations=["click"], visible=True, enabled=True)
    obs = observation(grant.origin + rules[0].document_path)
    obs.controls = [control]
    browser = object.__new__(OwnedBrowser)
    browser._check = lambda: None
    browser._allow_loopback = False
    browser.qualification_grant = grant
    browser._observation = obs
    browser.verified_load = lambda _: True
    browser._control_meta = {"c1": {"href": href, "tag": "A", "form": False}}
    browser._qualification_status = lambda: {"grant_digest": grant.grant_digest, "completed_rules": [],
        "rules": {rule.rule_id: {"protected": False, "request_count": 0} for rule in rules}}
    assert browser.cuai_navigation_targets(obs) == ([{"id": "c1", "href": href, "tag": "A", "form": False}]
        if accepted else [])


@pytest.mark.parametrize("operation,accepted", [("extract", True), ("navigate", True), ("scroll", False)])
def test_gateway_accepts_only_grant_read_proposals_on_v1(operation, accepted):
    from desktop_runtime.cu_owned_browser_adapter import validate_profile
    gateway, grant, *_ = claimed_gateway()
    order = gateway.request("GET", "/order", None)
    profile = order["manifest"]["general_browser_verification"]["execution_profile"]
    digest = validate_profile(profile)
    gateway.browser_cuai_profile_digest = digest
    obs = observation(FIXTURE_ORIGIN + grant.rules[0].document_path)
    inner = gateway.request

    def request(method, path, body):
        if not path.endswith("/browser-ai-call"):
            return inner(method, path, body)
        return {"authority": "proposal_only", "source_kind": "browser_cuai_text_v1", "invocation_id": "bai_one",
            "phase": "actor", "profile_digest": digest, "observation_digest": obs.digest(),
            "model": profile["roles"]["actor"]["model"], "service_tier": "default", "input_tokens": 1, "output_tokens": 1,
            "reply": "{}", "effect_proposals": [{"action_index": 0, "invocation_id": "bai_one_a0",
                "candidate": {"operation": operation, "target": "c1", "observation_digest": obs.digest()}}]}
    gateway.request = request
    load = {"qualified": True, "session_id": "s", "page_id": "p", "sequence": 1, "location": obs.location,
            "document_id": "d", "ready_state": "complete", "visible_text": "", "visibility_scope": "main",
            "observation_digest": obs.digest(), "response": {}}
    call = lambda: gateway.browser_ai_call("bai_one", "actor", obs, profile_digest=digest,  # noqa: E731
        checkpoint_digest="c" * 64, current_load=load, context={})
    if accepted:
        assert call()["effect_proposals"][0]["candidate"]["operation"] == operation
    else:
        with pytest.raises(PermissionError, match="browser_cuai_effect_proposal_invalid"):
            call()
