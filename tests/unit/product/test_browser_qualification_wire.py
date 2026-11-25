"""Grant-only proposal metadata survives Core's response without widening ordinary CU."""
from dataclasses import replace

import pytest

from desktop_runtime.cu_owned_browser_adapter import validate_profile
from tests.unit.product.test_browser_qualification_reads import observation
from tests.unit.product.test_browser_qualification_runtime import claimed_gateway, fixture_http_response


def proposal_call(operation="click", *, metadata=None, grant_operations=None, owned=True):
    gateway, grant, *_ = claimed_gateway()
    order = gateway.request("GET", "/order", None)
    if grant_operations is not None:
        grant = replace(grant, allowed_operations=grant_operations)
        order["browser_qualification_grant"] = grant.to_dict()
        gateway.browser_qualification_grant_digest = grant.grant_digest
    if not owned:
        order.pop("browser_qualification_grant")
        order.pop("browser_qualification_trial_binding")
        gateway.browser_qualification_binding = gateway.browser_qualification_grant_digest = None
    profile = order["manifest"]["general_browser_verification"]["execution_profile"]
    digest = validate_profile(profile)
    gateway.browser_cuai_profile_digest = digest
    obs = observation(grant.origin + grant.rules[0].document_path)
    proposal = {"action_index": 0, "invocation_id": "bai_one_a0", "candidate": {
        "operation": operation, "target": "c1", "observation_digest": obs.digest()}}
    proposal.update(metadata or {})
    inner = gateway.request

    def request(method, path, body):
        if not path.endswith("/browser-ai-call"):
            return inner(method, path, body)
        return {"authority": "proposal_only", "source_kind": "browser_cuai_text_v1",
            "invocation_id": "bai_one", "phase": "actor", "profile_digest": digest,
            "observation_digest": obs.digest(), "model": profile["roles"]["actor"]["model"],
            "service_tier": "default", "input_tokens": 1, "output_tokens": 1,
            "reply": "{}", "effect_proposals": [proposal]}

    gateway.request = request
    return gateway.browser_ai_call("bai_one", "actor", obs, profile_digest=digest,
        checkpoint_digest="c" * 64, current_load={}, context={})


def test_fixture_submit_rule_ids_survive_the_gateway():
    result = proposal_call(metadata={"qualification_rule_ids": ["submit_form"]})
    assert result["effect_proposals"][0]["qualification_rule_ids"] == ["submit_form"]


@pytest.mark.parametrize("rule_ids", [None, "submit_form", [1], ["submit_form", "submit_form"],
    ["foreign"], ["read_form"], ["submit_form"] * 129])
def test_invalid_or_nonpost_rule_ids_fail_closed(rule_ids):
    with pytest.raises(PermissionError, match="browser_qualification_proposal_invalid"):
        proposal_call(metadata={"qualification_rule_ids": rule_ids})


@pytest.mark.parametrize("operation", ["type", "navigate", "extract"])
def test_submit_rule_ids_cannot_bind_other_operations(operation):
    with pytest.raises(PermissionError, match="browser_qualification_proposal_invalid"):
        proposal_call(operation, metadata={"qualification_rule_ids": ["submit_form"]})


@pytest.mark.parametrize("metadata", [{"qualification_rule_ids": []},
    {"qualification_rule_ids": ["submit_form"]}, {"owned": True}])
def test_ordinary_production_proposals_keep_the_exact_three_key_contract(metadata):
    with pytest.raises(PermissionError, match="browser_cuai_effect_proposal_invalid"):
        proposal_call(metadata=metadata, owned=False)


def test_unknown_fixture_proposal_keys_are_refused():
    with pytest.raises(PermissionError, match="browser_cuai_effect_proposal_invalid"):
        proposal_call(metadata={"owned": True})


@pytest.mark.parametrize("operation,allowed", [("type", ("select",)), ("click", ("check",))])
def test_select_and_check_keep_existing_dispatch_shapes(operation, allowed):
    assert proposal_call(operation, grant_operations=allowed)["effect_proposals"][0]["candidate"]["operation"] == operation


@pytest.mark.parametrize("operation", ["click", "type", "scroll", "navigate", "extract"])
def test_fixture_operations_cannot_escape_the_saved_grant(operation):
    with pytest.raises(PermissionError, match="browser_cuai_effect_proposal_invalid"):
        proposal_call(operation, grant_operations=())


def test_runtime_links_require_exact_routes_durable_prerequisites_and_remaining_requests(tmp_path):
    from coding_agents.owned_browser import OwnedBrowser
    gateway, grant, _, status, *_ = claimed_gateway()
    browser = OwnedBrowser(order_id=grant.order_id, goal="Read fixture routes", origins=[grant.origin],
        authorize=lambda *_: True, admit_effect=lambda *_: True, cuai_mode=True, general_mode=True,
        profile_dir=tmp_path, qualification_grant=grant.to_dict(),
        qualification_current=gateway.qualification_current, qualification_gateway=gateway)
    href = grant.origin + grant.rules[0].path
    try:
        assert browser._qualification_link_allowed(href, status)
        assert not browser._qualification_link_allowed(href + "?foreign=1", status)
        assert not browser._qualification_link_allowed("https://foreign.invalid" + grant.rules[0].path, status)
        status["rules"][grant.rules[0].rule_id]["request_count"] = grant.rules[0].max_requests
        assert not browser._qualification_link_allowed(href, status)
        status["rules"][grant.rules[0].rule_id].update(request_count=0, protected=True)
        assert not browser._qualification_link_allowed(href, status)
    finally:
        browser.close()


@pytest.mark.parametrize("failure", [None, "synthetic network failure"])
def test_complete_204_headers_record_the_empty_hash_but_known_failure_stays_uncertain(failure):
    import hashlib
    from types import SimpleNamespace
    from coding_agents.owned_browser import OwnedBrowser
    request = SimpleNamespace(failure=failure)
    browser = SimpleNamespace(_qualification_pending={"request": request}, _uncertain=False)
    response = SimpleNamespace(request=request, status=204,
        finished=lambda: pytest.fail("Unconsumed 204 fetch need not emit requestfinished"),
        body=lambda: pytest.fail("Chrome has no 204 body to retrieve"))
    OwnedBrowser._qualification_response(browser, response)
    if failure is None:
        assert browser._qualification_pending["response_digest"] == hashlib.sha256(b"").hexdigest()
        assert browser._qualification_pending["status_code"] == 204 and not browser._uncertain
    else:
        assert browser._uncertain and "response_digest" not in browser._qualification_pending


def test_real_chrome_actor_gateway_adapter_fill_select_check_and_single_submit(tmp_path, monkeypatch):
    """Offline production-component round trip; model/HTTP answers are local test doubles."""
    from dataclasses import asdict
    import hashlib
    import json
    from threading import Event
    import time
    from types import SimpleNamespace
    from urllib.parse import parse_qsl

    from coding_agents.owned_browser import OwnedBrowser
    from core.browser_qualification import FixtureField
    from desktop_runtime.cu_owned_browser_adapter import CuOwnedBrowserAdapter
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)
    from app.build import device_browser_ai as ai

    gateway, grant, _, status, receipt, calls = claimed_gateway()
    order = gateway.request("GET", "/order", None)
    fields = grant.rules[1].fields + (FixtureField("plan", "pro", "plan", "select"),
        FixtureField("consent", "yes", "consent", "checkbox"))
    grant = replace(grant, rules=(grant.rules[0], replace(grant.rules[1], fields=fields)),
        allowed_operations=grant.allowed_operations + ("select", "check"))
    order["browser_qualification_grant"] = grant.to_dict()
    gateway.browser_qualification_grant_digest = grant.grant_digest
    status["grant_digest"] = grant.grant_digest
    profile = order["manifest"]["general_browser_verification"]["execution_profile"]
    gateway.browser_cuai_profile_digest = validate_profile(profile)
    html = (f'<body id="document-form"><h1>Project form</h1><a href="{grant.rules[0].path}">Read form</a>'
        f'<form id="form-project" method="post" action="{grant.rules[1].path}">'
        '<label>Project<input id="project" name="project"></label>'
        '<label>Email<input id="email" name="email"></label>'
        '<label>Code<input id="code" name="code"></label>'
        '<label>Plan<select id="plan" name="plan"><option value="basic">Basic</option>'
        '<option value="pro">Pro</option></select></label>'
        '<label>Consent<input id="consent" name="consent" type="checkbox" value="yes"></label>'
        '<button id="form-submit">Submit</button></form></body>').encode()
    result = b'<body id="document-form"><h1>Project stored</h1></body>'
    posts, submitted_proposals = [], []
    browser = OwnedBrowser(order_id=grant.order_id, goal="Enter the reviewed synthetic project",
        origins=[grant.origin], authorize=lambda *_: True,
        admit_effect=lambda effect, digest: gateway.admit_effect(effect["invocation"], effect["observation"], digest),
        cuai_mode=True, general_mode=True, profile_dir=tmp_path / "chrome",
        qualification_grant=grant.to_dict(), qualification_current=gateway.qualification_current,
        qualification_gateway=gateway)
    inner_request = gateway.request

    def request(method, path, body):
        if not path.endswith("/browser-ai-call"):
            return inner_request(method, path, body)
        obs = ai.Observation.model_validate(body["observation"])
        q = ai.QualificationContext.model_validate(body["context"]["qualification"])
        # Durable context normalization itself is covered on real PG separately.
        q.completed_rules = list(status["completed_rules"])
        proposals = []
        if body["phase"] == "plan":
            reply = json.dumps({"subgoals": ["Fill the reviewed form and submit once"]})
        else:
            target = next((control for control in obs.controls if "type" in control.operations), None)
            if target is not None:
                action = {"op": "type", "element_id": target.id, "text": browser.resolve_fill(target.id)}
            else:
                target = next(control for control in q.controls if control.control_id in {"consent", "form-submit"})
                action = {"op": "click", "element_id": target.id}
            reply = json.dumps({"actions": [action], "memory": "", "subgoal_done": False})
            candidate = ai._candidate(reply, obs, {"execution_profile": profile}, False,
                fixture=grant, qualification=q)
            proposal = {"action_index": 0, "invocation_id": body["invocation_id"] + "_a0", "candidate": asdict(candidate)}
            ids = ai._qualification_click_rules(grant, q, candidate.target) if candidate.operation == "click" else ()
            if ids:
                proposal["qualification_rule_ids"] = list(ids)
                submitted_proposals.append(proposal)
            proposals.append(proposal)
        return {"authority": "proposal_only", "source_kind": "browser_cuai_text_v1",
            "invocation_id": body["invocation_id"], "phase": body["phase"],
            "profile_digest": gateway.browser_cuai_profile_digest, "observation_digest": obs.digest(),
            "model": profile["roles"][body["phase"]]["model"], "service_tier": "default",
            "input_tokens": 1, "output_tokens": 1, "reply": reply, "effect_proposals": proposals}

    gateway.request = request
    gateway.cancellable_request = lambda method, path, body, **_: request(method, path, body)
    actual_route = browser._route

    def fixture_route(route):
        def fetched(**kwargs):
            assert kwargs["max_redirects"] == kwargs["max_retries"] == 0
            if route.request.method == "GET":
                rule = grant.request_rule(url=route.request.url, method="GET", completed_rules=status["completed_rules"])
                return fixture_http_response(gateway, grant, rule, html)
            posts.append(route.request.post_data_buffer)
            assert dict(parse_qsl(posts[-1].decode())) == {field.name: field.value for field in fields}
            payload = next(value for _, path, value in reversed(calls) if path.endswith("/bind-qualification"))
            assert kwargs["headers"]["X-Supymem-Qualification-Request"] == payload["qualification"]["request_id"]
            receipt.update(payload["qualification"], invocation_id=payload["invocation_id"],
                effect_digest=payload["effect_digest"], body_digest=hashlib.sha256(posts[-1]).hexdigest(),
                response_digest=hashlib.sha256(result).hexdigest(), response_size_bytes=len(result), response_mime_type="text/html")
            status["completed_rules"].append("submit_form")
            status["rules"]["submit_form"].update(request_count=1, committed_count=1)
            return fixture_http_response(gateway, grant, grant.rules[1], result,
                request_id=payload["qualification"]["request_id"])
        actual_route(SimpleNamespace(request=route.request, fetch=fetched, fulfill=route.fulfill, abort=route.abort))

    monkeypatch.setattr(browser, "_route", fixture_route)
    try:
        browser.start(grant.origin + grant.rules[0].path)
        adapter = CuOwnedBrowserAdapter(browser=browser, gateway=gateway, profile=profile, goal=browser.goal,
            stop=Event(), deadline=time.monotonic() + 60)
        for _ in range(6):
            obs = browser.observe()
            proposal = adapter.decide(obs)
            adapter.before_dispatch(proposal, obs)
            if proposal.action.op == "type":
                browser.type_text(proposal.candidate, proposal.invocation_id, proposal.action.text)
            else:
                browser.click(proposal.candidate, proposal.invocation_id)
            adapter.delivered(proposal)
        assert browser._page.locator("h1").inner_text() == "Project stored"
        assert len(posts) == len(submitted_proposals) == 1
        assert submitted_proposals[0]["qualification_rule_ids"] == ["submit_form"]
        assert browser.last_qualification_response["response_digest"] == hashlib.sha256(result).hexdigest()
        assert sum(path.endswith("/bind-qualification") for _, path, _ in calls) == 1
    finally:
        browser.close()
    assert browser._page is browser._context is browser._browser is browser._playwright is None
