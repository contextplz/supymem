"""Real offline Chrome over catalog HTML; HTTP/Core custody are local doubles, no provider or installed proof."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from coding_agents.jev_decision import Candidate
from coding_agents.owned_browser import OwnedBrowser
from core.browser_qualification_catalog import fixture_contract, fixture_preview, fixture_transition, new_fixture_state
from tests.unit.product.test_browser_qualification_runtime import claimed_gateway, fixture_http_response


@contextmanager
def catalog_browser(tmp_path, task_id, *, read_only=False):
    gateway, original, _, status, receipt, calls = claimed_gateway()
    snapshot = new_fixture_state(task_id, "abcdef1234", "/public/browser-qualification/" + original.nonce)
    contract = fixture_contract(snapshot)
    grant = replace(original, task_id=task_id, catalog_digest=contract["catalog_digest"],
        seed_digest=contract["seed_digest"], rules=contract["rules"], allowed_operations=contract["allowed_operations"])
    if read_only:
        grant = replace(grant, allowed_operations=("navigate", "scroll", "extract", "wait"))
    order = gateway.request("GET", "/order", None)
    order["browser_qualification_grant"] = grant.to_dict()
    order["browser_qualification_trial_binding"] = grant.trial_binding().to_dict()
    order["manifest"]["instruction"] = contract["goal"]
    gateway.browser_qualification_binding = grant.trial_binding()
    gateway.browser_qualification_grant_digest = grant.grant_digest
    status.update({key: getattr(grant, key) for key in ("task_id", "catalog_digest", "seed_digest")})
    status.update(grant_digest=grant.grant_digest, completed_rules=[], rules={rule.rule_id: {
        "request_count": 0, "committed_count": 0, "protected": False} for rule in grant.rules})
    posts = []
    browser = OwnedBrowser(order_id=grant.order_id, goal=contract["goal"], origins=[grant.origin],
        authorize=lambda *_: True,
        admit_effect=lambda effect, digest: gateway.admit_effect(effect["invocation"], effect["observation"], digest),
        cuai_mode=True, general_mode=True, read_only=read_only, profile_dir=tmp_path / "chrome",
        qualification_grant=grant.to_dict(), qualification_current=gateway.qualification_current,
        qualification_gateway=gateway)
    actual_route = browser._route

    def local_route(route):
        def fetched(**kwargs):
            nonlocal snapshot
            assert kwargs["max_redirects"] == kwargs["max_retries"] == 0
            request = route.request
            parsed = urlsplit(request.url)
            if request.method == "GET":
                rule = grant.request_rule(url=request.url, method="GET", completed_rules=status["completed_rules"])
                response = fixture_preview(snapshot, parsed.path.removeprefix(snapshot["base_path"]), parsed.query)
            else:
                body = json.loads(request.post_data_buffer)
                rule = grant.request_rule(url=request.url, method="POST", submitted_fields=list(body.items()),
                    completed_rules=status["completed_rules"])
                transition = fixture_transition(snapshot, rule, body)
                snapshot, response = transition.state, transition.response
                posts.append(body)
                payload = next(value for _, path, value in reversed(calls) if path.endswith("/bind-qualification"))
                assert kwargs["headers"]["X-Supymem-Qualification-Request"] == payload["qualification"]["request_id"]
                receipt.update(payload["qualification"], invocation_id=payload["invocation_id"],
                    effect_digest=payload["effect_digest"], method="POST", path=rule.path, query=rule.query,
                    body_digest=hashlib.sha256(request.post_data_buffer).hexdigest(),
                    response_digest=hashlib.sha256(response.body).hexdigest(), status_code=response.status,
                    response_size_bytes=len(response.body), response_mime_type=response.media_type.split(";", 1)[0].strip().lower())
                status["rules"][rule.rule_id]["committed_count"] += 1
            status["rules"][rule.rule_id]["request_count"] += 1
            status["completed_rules"].append(rule.rule_id)
            return fixture_http_response(gateway, grant, rule, response.body, status=response.status,
                mime_type=response.media_type, headers=response.headers,
                request_id=payload["qualification"]["request_id"] if request.method == "POST" else None)
        actual_route(SimpleNamespace(request=route.request, fetch=fetched, fulfill=route.fulfill, abort=route.abort))

    browser._route = local_route
    try:
        browser.start(contract["start_url"])
        yield SimpleNamespace(browser=browser, grant=grant, status=status, posts=posts, order=order)
    finally:
        browser.close()
    assert browser._page is browser._context is browser._browser is browser._playwright is None


@pytest.mark.parametrize("task_id,offered,withheld", [("counter", "inc", "add"), ("todos", "add", "inc")])
def test_catalog_initial_observation_withholds_other_container_buttons(tmp_path, task_id, offered, withheld):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)
    from app.build.device_browser_ai import QualificationContext
    with catalog_browser(tmp_path, task_id) as fixture:
        observation = fixture.browser.observe()
        context = QualificationContext.model_validate(fixture.browser.qualification_context(observation))
        source_controls = {control.control_id for control in context.controls}
        assert offered in source_controls and withheld not in source_controls
        assert not observation.confirmation_pending and not fixture.posts


def test_five_actual_counter_xhrs_leave_a_valid_final_readback_and_verify_request(tmp_path):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)
    from app.build import device_browser_ai as ai
    with catalog_browser(tmp_path, "counter") as fixture:
        browser, grant = fixture.browser, fixture.grant
        for count in range(1, 6):
            observation = browser.observe()
            context = ai.QualificationContext.model_validate(browser.qualification_context(observation))
            context.completed_rules = list(fixture.status["completed_rules"])
            target = next(control.id for control in context.controls if control.control_id == "inc")
            candidate = Candidate("click", target, observation.digest())
            invocation = f"bai_counter_{count}_a0"
            rule_ids = ai._qualification_click_rules(grant, context, target)
            browser.qualification_proposal(invocation, candidate, rule_ids)
            browser.click(candidate, invocation)
        assert fixture.posts == [{"count": count} for count in range(1, 6)]
        assert browser._page.locator("#c").inner_text() == "5"
        observation = browser.observe()
        context = ai.QualificationContext.model_validate(browser.qualification_context(observation))
        assert observation.controls == context.controls == []
        assert not observation.confirmation_pending and not browser._qualification_pending
        assert all(fixture.status["rules"][rule.rule_id]["committed_count"] == 1
            for rule in grant.rules if rule.method == "POST")
        readback = browser.cuai_readback(observation)
        assert "5" in readback
        profile = ai.orders.BrowserAIProfile.model_validate(
            fixture.order["manifest"]["general_browser_verification"]["execution_profile"])
        request = ai.BrowserAIBody(invocation_id="bai_counter_verify", phase="verify",
            execution_profile_digest=grant.profile_digest, checkpoint_digest="c" * 64,
            observation=observation, current_load={key: browser.last_load_evidence[key]
                for key in ai.LoadProof.model_fields if key in browser.last_load_evidence},
            context={"qualification": context, "notes": readback, "claim": "The count is 5"})
        body = ai.request_body(fixture.order, request, profile, observation)
        assert "The count is 5" in body["input"][1]["content"][0]["text"]


def test_read_only_catalog_grant_offers_only_exact_unconsumed_navigation_links(tmp_path):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)
    from app.build.device_browser_ai import QualificationContext
    with catalog_browser(tmp_path, "paginate", read_only=True) as fixture:
        browser, grant = fixture.browser, fixture.grant
        next_rule = next(rule for rule in grant.rules if rule.query == "page=2")
        exhausted = next(rule for rule in grant.rules if rule.query == "page=3")
        exhausted_url = grant.origin + exhausted.path + "?" + exhausted.query
        unknown_url = grant.origin + next_rule.path + "?page=999"
        fixture.status["rules"][exhausted.rule_id]["request_count"] = exhausted.max_requests
        browser._page.evaluate("""urls => { for (const [label, href] of urls) {
            const a=document.createElement('a'); a.textContent=label; a.href=href; document.body.append(a);
        }}""", [["Exhausted", exhausted_url], ["Unreviewed", unknown_url]])
        observation = browser.observe()
        links = browser.cuai_navigation_targets(observation)
        assert [link["href"] for link in links] == [grant.origin + next_rule.path + "?" + next_rule.query]
        assert [control.label for control in observation.controls] == ["Next"]
        context = QualificationContext.model_validate(browser.qualification_context(observation))
        assert [link.model_dump() for link in context.links] == [{"id": links[0]["id"], "href": links[0]["href"]}]
        assert "click" not in grant.allowed_operations and not fixture.posts
