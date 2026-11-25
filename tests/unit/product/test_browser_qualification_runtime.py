"""Controller metadata fences only; no services, model calls or installed proof."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agents.owned_browser import OwnedBrowser
from desktop_runtime.cu_owned_browser_adapter import validate_profile
from desktop_runtime.device_gateway import WorkOrderGateway
from tests.unit.product.test_browser_qualification_contract import form_grant


def fixture_order():
    profile = json.loads((Path(__file__).parents[3] / "config/browser_cuai_profile.json").read_text())
    grant = replace(form_grant(), profile_digest=validate_profile(profile))
    order = {"id": grant.order_id, "actor_id": grant.actor_id, "device_id": grant.device_id,
        "venture_id": grant.venture_id, "workspace_id": grant.workspace_id,
        "review_digest": grant.review_digest, "state": "approved",
        "browser_qualification_grant": grant.to_dict(),
        "browser_qualification_trial_binding": grant.trial_binding().to_dict(),
        "manifest": {"general_browser_verification": {"execution_profile": profile}}}
    return grant, order


@pytest.mark.parametrize("marker", [None, False, "true", 1])
def test_old_or_nonliteral_capability_refuses_before_claim(marker):
    grant, order = fixture_order()
    calls = []
    def request(method, path, body):
        calls.append((method, path))
        if path.endswith("/capabilities"):
            return {"available": True, "browser_qualification_v1": marker}
        assert method == "GET"
        return order
    gateway = WorkOrderGateway(venture_id=grant.venture_id, order_id=grant.order_id,
        device_id=grant.device_id, account_id=grant.actor_id, request=request)
    with pytest.raises(PermissionError, match="qualification_capability_unavailable"):
        gateway.claim()
    assert not gateway.claim_attempted and all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("field", ["actor_id", "device_id", "workspace_id", "review_digest"])
def test_controller_grant_cannot_borrow_changed_current_identity(field):
    grant, order = fixture_order()
    order[field] = "foreign" if not field.endswith("digest") else "f" * 64
    gateway = WorkOrderGateway(venture_id=grant.venture_id, order_id=grant.order_id,
        device_id=grant.device_id, account_id=grant.actor_id, request=lambda *_: order)
    with pytest.raises(PermissionError):
        gateway.read()


def test_terminal_expired_receipt_remains_readable_but_running_authority_expires():
    grant, order = fixture_order()
    grant = replace(grant, expires_at=1)
    order.update(state="stopped", browser_qualification_grant=grant.to_dict(),
        browser_qualification_trial_binding=grant.trial_binding().to_dict())
    gateway = WorkOrderGateway(venture_id=grant.venture_id, order_id=grant.order_id,
        device_id=grant.device_id, account_id=grant.actor_id, request=lambda *_: order)
    assert gateway.read()["state"] == "stopped"
    order["state"] = "claimed"
    with pytest.raises(PermissionError, match="qualification_binding_changed"):
        gateway.read()


def test_parsed_grant_or_origin_never_supply_current_authority(tmp_path):
    grant, _ = fixture_order()
    browser = OwnedBrowser(order_id=grant.order_id, goal="Owned fixture", origins=[grant.origin],
        authorize=lambda *_: True, admit_effect=lambda *_: pytest.fail("No effect may be admitted"),
        cuai_mode=True, general_mode=True, profile_dir=tmp_path,
        qualification_grant=grant.to_dict(), qualification_current=lambda: True)
    with pytest.raises(PermissionError, match="qualification_authority_required"):
        browser._check()


@pytest.mark.parametrize("method,suffix,allowed", [("GET", "", True), ("GET", "/unreviewed", False),
    ("POST", "", False)])
def test_fixture_network_uses_exact_document_rule_and_post_is_closed(tmp_path, method, suffix, allowed):
    gateway, grant, *_ = claimed_gateway()
    browser = OwnedBrowser(order_id=grant.order_id, goal="Owned fixture", origins=[grant.origin],
        authorize=lambda *_: True, admit_effect=lambda *_: pytest.fail("No mutation permission"),
        cuai_mode=True, general_mode=True, profile_dir=tmp_path,
        qualification_grant=grant.to_dict(), qualification_current=lambda: grant)
    class Page:
        url = "about:blank"
    page = Page()
    page.main_frame = SimpleNamespace(page=page)
    browser._page = page
    browser._qualification_owned_pages.add(page)
    actions = []
    def fetched(**kwargs):
        assert kwargs["max_redirects"] == kwargs["max_retries"] == 0
        return fixture_http_response(gateway, grant, grant.rules[0], b"Fixture")
    route = SimpleNamespace(request=SimpleNamespace(method=method, resource_type="document",
        url=grant.origin + grant.rules[0].path + suffix, frame=page.main_frame),
        fetch=fetched, fulfill=lambda **_: actions.append("allowed"), abort=lambda *_: actions.append("blocked"))
    browser._load_pages[page] = {"request": route.request, "sequence": 1, "qualification_response": None}
    browser._route(route)
    assert actions == ["allowed" if allowed else "blocked"]
    source = browser._load_pages[page]["qualification_response"]
    if allowed:
        assert source["url"] == route.request.url and source["sequence"] == 1
        assert source["sha256"] == hashlib.sha256(b"Fixture").hexdigest()
    else:
        assert source is None


def claimed_gateway():
    from core.browser_qualification import request_binding_digest
    grant, order = fixture_order()
    order.update(state="claimed", execution_id="dex_synthetic")
    status = {key: getattr(grant, key) for key in (
        "actor_id", "device_id", "workspace_id", "venture_id", "trial_id", "order_id", "catalog_digest",
        "task_id", "seed_digest", "review_digest", "profile_digest", "expires_at")}
    status.update(grant_digest=grant.grant_digest, completed_rules=[grant.rules[0].rule_id],
        rules={rule.rule_id: {"request_count": 1 if rule.method == "GET" else 0,
            "committed_count": 0, "protected": False} for rule in grant.rules})
    rule = grant.rules[1]
    qualification = {"grant_digest": grant.grant_digest, "rule_id": rule.rule_id, "request_id": "bqr_synthetic"}
    qualification["request_digest"] = request_binding_digest(grant, rule, qualification["request_id"])
    receipt = {**qualification, "trial_id": grant.trial_id, "order_id": grant.order_id,
        "execution_id": "dex_synthetic", "invocation_id": "bai_synthetic_a0", "effect_digest": "e" * 64,
        "workspace_id": grant.workspace_id, "venture_id": grant.venture_id, "actor_id": grant.actor_id,
        "device_id": grant.device_id, "method": rule.method, "path": rule.path, "query": rule.query,
        "body_digest": "b" * 64, "response_digest": "c" * 64, "state": "committed", "mutated": True,
        "status_code": 200, "response_size_bytes": 0, "response_mime_type": "text/html", "server_response_sent": True}
    calls, read_receipts = [], {}
    def request(method, path, body):
        calls.append((method, path, body))
        if path.endswith("/authority"):
            return {"current": True}
        if "/browser-qualification/" in path:
            return read_receipts.get(path.rsplit("/", 1)[-1], receipt) if "/requests/" in path else status
        if path.endswith("/bind-qualification"):
            return {"bound_now": True}
        if path.endswith("/effects/admit"):
            return {"admitted_now": True}
        return order
    gateway = WorkOrderGateway(venture_id=grant.venture_id, order_id=grant.order_id,
        device_id=grant.device_id, account_id=grant.actor_id, request=request)
    gateway.claimed, gateway.execution_id = True, "dex_synthetic"
    gateway.review_digest = grant.review_digest
    gateway.browser_qualification_binding = grant.trial_binding()
    gateway.browser_qualification_grant_digest = grant.grant_digest
    gateway._fixture_read_receipts = read_receipts
    return gateway, grant, qualification, status, receipt, calls


def fixture_http_response(gateway, grant, rule, body, *, status=200, mime_type="text/html", headers=(), request_id=None):
    """A local source server reply with exact separate Core GET custody; no network fallback."""
    from core.browser_qualification import request_binding_digest
    request_id = request_id or "bqr_fixture_" + str(len(gateway._fixture_read_receipts) + 1)
    response_headers = {key.lower(): value for key, value in dict(headers).items()}
    response_headers.update({"content-type": mime_type, "content-length": str(len(body)),
                             "x-supymem-qualification-request": request_id})
    if rule.method == "GET":
        gateway._fixture_read_receipts[request_id] = {"grant_digest": grant.grant_digest,
            "trial_id": grant.trial_id, "request_id": request_id, "rule_id": rule.rule_id,
            "request_digest": request_binding_digest(grant, rule, request_id), "order_id": grant.order_id,
            "execution_id": gateway.execution_id, "workspace_id": grant.workspace_id,
            "venture_id": grant.venture_id, "actor_id": grant.actor_id, "device_id": grant.device_id,
            "method": "GET", "path": rule.path, "query": rule.query, "status_code": status,
            "response_digest": hashlib.sha256(body).hexdigest(), "response_size_bytes": len(body),
            "response_mime_type": mime_type.split(";", 1)[0].strip().lower(), "mutated": False,
            "server_response_sent": True, "state": "failed_read" if status >= 400 else "committed"}
    return SimpleNamespace(status=status, headers=response_headers, body=lambda: body, dispose=lambda: None)


def test_actual_gateway_joins_saved_status_and_one_effect_request_before_dispatch():
    gateway, grant, qualification, status, receipt, calls = claimed_gateway()
    assert gateway.qualification_order_status() == status
    assert gateway.admit_effect("bai_synthetic_a0", "o" * 64, "e" * 64) is True
    assert gateway.bind_qualification_effect(qualification, invocation_id="bai_synthetic_a0", effect_digest="e" * 64) is True
    assert gateway.qualification_receipt(qualification, invocation_id="bai_synthetic_a0", effect_digest="e" * 64) == receipt
    payload = next(body for _, path, body in calls if path.endswith("/bind-qualification"))
    assert payload == {"invocation_id": "bai_synthetic_a0", "effect_digest": "e" * 64, "qualification": qualification}
    assert all(path.startswith(f"/api/ventures/{grant.venture_id}/") for _, path, _ in calls)


@pytest.mark.parametrize("field,value", [("grant_digest", "f" * 64), ("actor_id", "usr_foreign"),
    ("device_id", "dev_foreign"), ("order_id", "dwo_foreign"), ("expires_at", True),
    ("completed_rules", ["foreign"]), ("completed_rules", ["read_form", "read_form"])])
def test_signed_order_status_cannot_supply_foreign_or_untyped_completion(field, value):
    gateway, _, _, status, _, _ = claimed_gateway()
    status[field] = value
    with pytest.raises(PermissionError, match="status_unconfirmed"):
        gateway.qualification_order_status()


@pytest.mark.parametrize("field,value", [("invocation_id", "paid_parent"), ("effect_digest", "f" * 64),
    ("request_digest", "f" * 64), ("device_id", "dev_other"), ("server_response_sent", 0), ("mutated", 1),
    ("status_code", True), ("response_digest", None), ("method", "GET"),
    ("response_size_bytes", True), ("response_size_bytes", -1), ("response_size_bytes", 65537),
    ("response_size_bytes", None), ("response_mime_type", None), ("response_mime_type", "text/html; charset=utf-8")])
def test_request_readback_requires_all_exact_custody_and_known_delivery(field, value):
    gateway, _, qualification, _, receipt, _ = claimed_gateway()
    receipt[field] = value
    with pytest.raises(PermissionError, match="receipt_unconfirmed"):
        gateway.qualification_receipt(qualification, invocation_id="bai_synthetic_a0", effect_digest="e" * 64)


def test_pending_and_unknown_readback_remain_explicit_without_known_delivery():
    gateway, _, qualification, _, receipt, _ = claimed_gateway()
    for state in ("pending", "unknown"):
        receipt.update(state=state, status_code=None, response_digest=None, response_size_bytes=None,
                       response_mime_type=None, server_response_sent=False, mutated=False)
        assert gateway.qualification_receipt(qualification, invocation_id="bai_synthetic_a0", effect_digest="e" * 64)["state"] == state


def test_known_unsent_response_is_pollable_but_never_claims_delivery():
    gateway, _, qualification, _, receipt, _ = claimed_gateway()
    receipt["server_response_sent"] = False
    pending = gateway.qualification_receipt(qualification, invocation_id="bai_synthetic_a0", effect_digest="e" * 64)
    assert pending["state"] == "committed" and pending["server_response_sent"] is False
    # The runtime's source join still requires an actual sent=True before clearing the effect.
    assert pending["request_digest"] == qualification["request_digest"]


@pytest.mark.parametrize("body", [b"project=Atlas&email=qa%40example.invalid&code=OTHER",
    b"project=Atlas&email=qa%40example.invalid&code=ABC123&code=ABC123"])
def test_post_raw_body_mismatch_or_duplicate_never_binds_or_dispatches(tmp_path, body):
    gateway, grant, _, _, _, calls = claimed_gateway()
    browser = OwnedBrowser(order_id=grant.order_id, goal="Owned fixture", origins=[grant.origin],
        authorize=lambda *_: True, admit_effect=lambda *_: True, cuai_mode=True, general_mode=True,
        profile_dir=tmp_path, qualification_grant=grant.to_dict(), qualification_current=lambda: grant,
        qualification_gateway=gateway)
    browser._page = SimpleNamespace(url=grant.origin + grant.rules[0].path)
    browser._input_binding = lambda *_: ("frozen",)
    browser._qualification_pending = {"handle": object(), "binding": ("frozen",), "locked": False,
        "rule_ids": (grant.rules[1].rule_id,), "invocation": "bai_synthetic_a0", "effect_digest": "e" * 64}
    request = SimpleNamespace(method="POST", url=grant.origin + grant.rules[1].path, resource_type="xhr",
        frame=SimpleNamespace(page=browser._page), is_navigation_request=lambda: False, post_data_buffer=body,
        headers={"content-type": "application/x-www-form-urlencoded"})
    actions = []
    browser._route(SimpleNamespace(request=request, fetch=lambda **_: pytest.fail("Invalid raw body was dispatched"),
        abort=lambda *_: actions.append("blocked")))
    assert actions == ["blocked"] and browser._uncertain is True
    assert not any(path.endswith("/bind-qualification") for _, path, _ in calls)


@pytest.mark.parametrize("reply", [False, "lost_ack"])
def test_unknown_or_false_bind_is_frozen_and_cannot_repeat_http(tmp_path, reply):
    gateway, grant, _, _, _, _ = claimed_gateway()
    attempts = []
    def bind(*args, **kwargs):
        attempts.append((args, kwargs))
        if reply == "lost_ack":
            raise TimeoutError("owned persistence acknowledgement lost")
        return False
    gateway.bind_qualification_effect = bind
    browser = OwnedBrowser(order_id=grant.order_id, goal="Owned fixture", origins=[grant.origin],
        authorize=lambda *_: True, admit_effect=lambda *_: True, cuai_mode=True, general_mode=True,
        profile_dir=tmp_path, qualification_grant=grant.to_dict(), qualification_current=lambda: grant,
        qualification_gateway=gateway)
    browser._page = SimpleNamespace(url=grant.origin + grant.rules[0].path)
    browser._input_binding = lambda *_: ("frozen",)
    browser._qualification_pending = {"handle": object(), "binding": ("frozen",), "locked": False,
        "rule_ids": (grant.rules[1].rule_id,), "invocation": "bai_synthetic_a0", "effect_digest": "e" * 64}
    request = SimpleNamespace(method="POST", url=grant.origin + grant.rules[1].path, resource_type="xhr",
        frame=SimpleNamespace(page=browser._page), is_navigation_request=lambda: False, post_data_buffer=b"project=Atlas&email=qa%40example.invalid&code=ABC123",
        headers={"content-type": "application/x-www-form-urlencoded"})
    actions = []
    route = SimpleNamespace(request=request, fetch=lambda **_: pytest.fail("Unknown bind was dispatched"), abort=lambda *_: actions.append("blocked"))
    browser._route(route)
    browser._route(route)
    assert len(attempts) == 1 and actions == ["blocked", "blocked"]
    assert browser._qualification_pending["locked"] is True and browser._uncertain is True


def test_actual_dom_synthetic_form_dispatch_is_sealed_once_and_hash_joined(tmp_path, monkeypatch):
    import hashlib
    from urllib.parse import parse_qsl
    from coding_agents.jev_decision import Candidate
    gateway, grant, _, status, receipt, calls = claimed_gateway()
    html = (f'<html><body id="document-form"><h1>Project form</h1>'
        f'<form id="form-project" method="post" action="{grant.rules[1].path}">'
        '<label>Project<input id="project" name="project"></label>'
        '<label>Email<input id="email" name="email"></label>'
        '<label>Code<input id="code" name="code"></label>'
        '<button id="form-submit">Submit</button></form></body></html>').encode()
    result = b'<html><body id="document-form"><h1>Project stored</h1></body></html>'
    http_calls = []
    browser = OwnedBrowser(order_id=grant.order_id, goal="Enter the reviewed synthetic project",
        origins=[grant.origin], authorize=lambda *_: True,
        admit_effect=lambda effect, effect_digest: gateway.admit_effect(effect["invocation"], effect["observation"], effect_digest),
        cuai_mode=True, general_mode=True, profile_dir=tmp_path / "chrome",
        qualification_grant=grant.to_dict(), qualification_current=gateway.qualification_current,
        qualification_gateway=gateway)
    # Every outgoing request is fulfilled locally. No DNS/provider/production call.
    actual_route = browser._route
    def fixture_route(route):
        def fetched(**kwargs):
            assert kwargs["max_redirects"] == kwargs["max_retries"] == 0
            http_calls.append((route.request.method, route.request.url))
            if route.request.method == "GET":
                rule = grant.request_rule(url=route.request.url, method="GET", completed_rules=status["completed_rules"])
                return fixture_http_response(gateway, grant, rule, html)
            else:
                headers = kwargs["headers"]
                payload = next(body for _, path, body in reversed(calls) if path.endswith("/bind-qualification"))
                assert headers["X-Supymem-Qualification-Invocation"] == "bai_form_a0"
                assert headers["X-Supymem-Qualification-Execution"] == "dex_synthetic"
                assert headers["X-Supymem-Qualification-Request"] == payload["qualification"]["request_id"]
                assert dict(parse_qsl(route.request.post_data_buffer.decode())) == {field.name: field.value for field in grant.rules[1].fields}
                receipt.update(payload["qualification"], invocation_id=payload["invocation_id"], effect_digest=payload["effect_digest"],
                    body_digest=hashlib.sha256(route.request.post_data_buffer).hexdigest(), response_digest=hashlib.sha256(result).hexdigest(),
                    response_size_bytes=len(result), response_mime_type="text/html")
                status["completed_rules"].append(grant.rules[1].rule_id)
                status["rules"][grant.rules[1].rule_id].update(request_count=1, committed_count=1)
                return fixture_http_response(gateway, grant, grant.rules[1], result,
                    request_id=payload["qualification"]["request_id"])
        actual_route(SimpleNamespace(request=route.request, fetch=fetched, fulfill=route.fulfill, abort=route.abort))
    monkeypatch.setattr(browser, "_route", fixture_route)
    try:
        browser.start(grant.origin + grant.rules[0].path)
        for field in grant.rules[1].fields:
            observation = browser.observe()
            target = next(identity for identity, info in browser._control_meta.items() if info["source_control_id"] == field.control_id)
            assert "type" in next(control for control in observation.controls if control.id == target).operations
            browser.type_text(Candidate("type", target, observation.digest(), value_index=observation.reviewed_text.index(field.input_value)),
                "bai_fill_a0", field.input_value)
        for field in grant.rules[1].fields:
            assert browser._page.locator("#" + field.control_id).input_value() == field.input_value
        assert browser._page.locator("#form-project").evaluate("el => el.checkValidity()") is True
        observation = browser.observe()
        assert not observation.confirmation_pending
        target = next(identity for identity, info in browser._control_meta.items() if info["source_control_id"] == "form-submit")
        assert browser._page.locator("#form-submit").evaluate("el => el.type") == "submit"
        candidate = Candidate("click", target, observation.digest())
        browser.qualification_proposal("bai_form_a0", candidate, [grant.rules[1].rule_id])
        try:
            browser.click(candidate, "bai_form_a0")
        except Exception as error:
            pending = browser._qualification_pending or {}
            pytest.fail(f"owned fixture dispatch failed: {type(error).__name__}; "
                f"http_count={len(http_calls)}; locked={pending.get('locked')}; request={pending.get('request') is not None}; "
                f"response={pending.get('response_digest') is not None}")
        assert browser._page.locator("h1").inner_text() == "Project stored"
        assert browser.last_qualification_response["mutated"] is True
        assert browser.last_qualification_response["response_digest"] == hashlib.sha256(result).hexdigest()
        assert sum(method == "POST" for method, _ in http_calls) == 1
        assert sum(path.endswith("/bind-qualification") for _, path, _ in calls) == 1
    finally:
        browser.close()


def test_exact_synthetic_field_cannot_become_password_or_duplicate_source_control(tmp_path):
    gateway, grant, _, status, _, _ = claimed_gateway()
    browser = OwnedBrowser(order_id=grant.order_id, goal="Exact code only", origins=[grant.origin],
        authorize=lambda *_: True, admit_effect=lambda *_: True, cuai_mode=True, general_mode=True,
        profile_dir=tmp_path, qualification_grant=grant.to_dict(), qualification_current=lambda: grant,
        qualification_gateway=gateway)
    info = {"source_control_id": "code", "source_document_path": grant.rules[0].path,
        "source_document_id": "document-form", "source_container_id": "form-project", "source_unique": True,
        "source_frame_id": "", "source_input_type": "text", "tag": "INPUT", "name": "code", "input_type": "text",
        "autocomplete": "", "label": "Code", "terms_checkbox": False}
    assert browser._qualification_field(info, status, "type").input_value == "ABC123"
    for change in ({"source_unique": False}, {"source_input_type": "password"}, {"name": "password"},
        {"autocomplete": "one-time-code"}, {"source_control_id": "other"}, {"source_document_id": "foreign"}):
        assert browser._qualification_field(info | change, status, "type") is None
