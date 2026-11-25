"""HTTP assertion/shape wiring over the separately tested durable order service."""
import json

import pytest
from core.assertion import sign_assertion
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.product.test_device_orders import orders as orders  # noqa: PLC0414

SECRET = "device-route-test-secret-over-thirty-two-characters"


@pytest.fixture
def api(orders, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.api.device_orders_api import router
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture, Workspace
    from app.state import db
    from core.clock import utcnow

    # from-chat persists its review in real venture-owned legacy history.
    # The durable-order fixture's isolated scope mocks do not create these rows.
    owner = ensure_subject_person("founder")
    now = utcnow()
    with db()() as session:
        session.add(Workspace(id="w_one", kind="venture", name="Device review fixture",
                              entry_mode="engagement", owner_id=owner, created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="v_one", workspace_id="w_one", name="Device review fixture",
                            raw_idea="Review an owned page", stage="draft", autonomy_level="L1",
                            created_at=now, updated_at=now))
        session.commit()
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    # Endpoint must require a signed assertion even if the local default is off.
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), orders


def headers(path, method="POST", *, device="dev_one", subject="founder"):
    return {"X-COS-Assertion": sign_assertion(SECRET, sub=subject, email="", role="user", path=path,
                                             method=method, device_id=device)}


def test_signed_device_review_claim_and_cancel_wiring(api):
    client, (_, _, manifest) = api
    base = "/ventures/v_one/device-work-orders"
    result = client.post(base, json=manifest.model_dump(), headers=headers(base))
    assert result.status_code == 200, result.text
    order = result.json()
    assert order["review_path"] == f"/v/v_one/device-work-orders/{order['id']}"
    path = base + "/" + order["id"]
    body = {"review_digest": order["review_digest"]}
    for action in ("approve", "claim"):
        result = client.post(path + "/" + action, json=body, headers=headers(path + "/" + action))
        assert result.status_code == 200, result.text
    assert client.post(path + "/claim", json=body, headers=headers(path + "/claim")).status_code == 409
    assert client.get(path, headers=headers(path, "GET", device="dev_other")).status_code == 404
    assert client.get(path, headers=headers(path, "GET", subject="other")).status_code == 404
    assert client.post(path + "/cancel", json=body, headers=headers(path + "/cancel")).json()["state"] == "cancelled"
    assert client.get(path, headers=headers(path, "GET")).json()["state"] == "cancelled"


@pytest.mark.parametrize("domain", ["Industrial inspection scheduler", "Home gardening journal"])
@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_busy_build_names_your_task_and_its_stop_location(monkeypatch, domain, platform):
    from app.api import device_orders_api
    import sys

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(sys, "platform", platform)
    order = {"id": "dwo_busy", "venture_id": "other_venture", "manifest": {"title": domain}}
    def busy():
        raise device_orders_api.device_orders.ActiveDeviceOrder(order)
    with pytest.raises(device_orders_api.HTTPException) as caught:
        device_orders_api.call(busy)
    detail = caught.value.detail
    assert caught.value.status_code == 409 and detail["code"] == "device_task_has_active_order"
    assert domain in detail["message"]
    assert "Build activity" in detail["message"] and "Stop" in detail["message"] and "Cancel" in detail["message"]
    assert detail["review_path"] == "/v/other_venture/device-work-orders/dwo_busy"
    if platform != "darwin":
        assert "Mac" not in detail["message"]


@pytest.mark.parametrize("domain", ["Industrial inspection scheduler", "Home gardening journal"])
def test_signed_preclaim_failure_transport_keeps_digest_and_failure_code(api, monkeypatch, domain):
    client, (service, caller, manifest) = api
    order = service.prepare("v_one", caller, manifest.model_copy(update={"title": domain}))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr("app.admin.authn.is_local", lambda: False)
    monkeypatch.setattr(service, "fail_before_start", lambda venture, actor, order_id, digest, failure_code: {
        "id": order_id, "venture_id": venture, "state": "failed", "failure_code": failure_code,
        "actor_id": actor.sub, "review_digest": digest})
    path = f"/ventures/v_one/device-work-orders/{order['id']}/fail-before-start"
    body = {"review_digest": order["review_digest"], "failure_code": "coding_checks_unavailable"}
    response = client.post(path, json=body, headers=headers(path))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "failed" and response.json()["failure_code"] == body["failure_code"]
    assert response.json()["review_digest"] == order["review_digest"]
    assert client.post(path, json=body).status_code in {401, 403}
    assert client.post(path, json={**body, "failure_code": "private credentials"}, headers=headers(path)).status_code == 422


def test_heartbeat_requires_signed_device_and_content_free_progress(api):
    client, (service, caller, manifest) = api
    saved = service.prepare("v_one", caller, manifest)
    args = ("v_one", caller, saved["id"], saved["review_digest"])
    service.transition(*args, "approve")
    claimed = service.transition(*args, "claim")
    path = f"/ventures/v_one/device-work-orders/{saved['id']}/heartbeat"
    packet = {"execution_id": claimed["execution_id"], "step": 1, "percent": 10, "last_action": "observing"}
    assert client.post(path, json=packet).status_code == 401
    assert client.post(path, json=packet, headers=headers(path, device="dev_other")).status_code == 404
    assert client.post(path, json={**packet, "text": "private"}, headers=headers(path)).status_code == 422
    result = client.post(path, json=packet, headers=headers(path))
    assert result.status_code == 200 and result.json()["progress"] == {
        "step": 1, "percent": 10, "last_action": "observing"}


def test_conditional_cancel_signed_wire_rejects_late_claim_and_keeps_ordinary_stop(api):
    from tests.unit.product.test_product_build_orders import _order_account_state
    client, (svc, _, manifest) = api
    base = "/ventures/v_one/device-work-orders"
    order = client.post(base, json=manifest.model_dump(), headers=headers(base)).json()
    path = base + "/" + order["id"]
    body = {"review_digest": order["review_digest"], "unstarted_only": True}
    before = _order_account_state(svc, order["id"])
    for action in ("approve", "claim", "acknowledge"):
        result = client.post(path + "/" + action, json=body, headers=headers(path + "/" + action))
        assert result.status_code == 409 and result.json()["detail"]["code"] == "device_order_transition_invalid"
        assert _order_account_state(svc, order["id"]) == before
    # The same review digest can survive another page approving and starting it.
    for action in ("approve", "claim"):
        result = client.post(path + "/" + action, json={"review_digest": order["review_digest"]},
                             headers=headers(path + "/" + action))
        assert result.status_code == 200, result.text
    before = _order_account_state(svc, order["id"])
    result = client.post(path + "/cancel", json=body, headers=headers(path + "/cancel"))
    assert result.status_code == 409 and result.json()["detail"]["code"] == "device_order_transition_invalid"
    assert _order_account_state(svc, order["id"]) == before
    result = client.post(path + "/cancel", json={"review_digest": order["review_digest"]}, headers=headers(path + "/cancel"))
    assert result.status_code == 200 and result.json()["state"] == "cancelled"


def test_conditional_cancel_signed_wire_allows_only_same_device_pending_review(api):
    from tests.unit.product.test_product_build_orders import _order_account_state
    client, (svc, _, manifest) = api
    base = "/ventures/v_one/device-work-orders"
    order = client.post(base, json=manifest.model_dump(), headers=headers(base)).json()
    path = base + "/" + order["id"] + "/cancel"
    body = {"review_digest": order["review_digest"], "unstarted_only": True}
    before = _order_account_state(svc, order["id"])
    result = client.post(path, json=body, headers=headers(path, device="dev_reconnected"))
    assert result.status_code == 409 and result.json()["detail"]["code"] == "device_order_transition_invalid"
    assert _order_account_state(svc, order["id"]) == before
    result = client.post(path, json=body, headers=headers(path))
    assert result.status_code == 200 and result.json()["state"] == "cancelled"
    assert not result.json().get("claimed_at") and not result.json().get("execution_id")


def test_conditional_cancel_flag_is_exact_true_and_validation_never_mutates(api):
    from tests.unit.product.test_product_build_orders import _order_account_state
    client, (svc, _, manifest) = api
    base = "/ventures/v_one/device-work-orders"
    order = client.post(base, json=manifest.model_dump(), headers=headers(base)).json()
    path = base + "/" + order["id"] + "/cancel"
    before = _order_account_state(svc, order["id"])
    for value in (False, 0, 1, "true", None):
        result = client.post(path, json={"review_digest": order["review_digest"], "unstarted_only": value}, headers=headers(path))
        assert result.status_code == 422
        assert _order_account_state(svc, order["id"]) == before


def test_emergency_stop_wiring_is_signed_digestless_and_idempotent(api):
    client, (_, _, manifest) = api
    base = "/ventures/v_one/device-work-orders"
    order = client.post(base, json=manifest.model_dump(), headers=headers(base)).json()
    path = base + "/" + order["id"]
    for action in ("approve", "claim"):
        client.post(path + "/" + action, json={"review_digest": order["review_digest"]}, headers=headers(path + "/" + action))
    stop = path + "/emergency-stop"
    assert client.post(stop, json={"source": "menu"}).status_code == 401
    first = client.post(stop, json={"source": "menu"}, headers=headers(stop))
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "cancelled" and first.json()["stopped_by_user"]["source"] == "menu"
    second = client.post(stop, json={}, headers=headers(stop))
    assert second.json()["stopped_by_user"] == first.json()["stopped_by_user"]
    assert client.post(stop, json={"source": "x", "extra": 1}, headers=headers(stop)).status_code == 422


def test_unsigned_spoofed_or_browser_identity_cannot_prepare_device_work(api):
    client, (_, _, manifest) = api
    path = "/ventures/v_one/device-work-orders"
    assert client.post(path, json=manifest.model_dump(), headers={"X-Subject": "founder", "X-Device-Id": "dev_one"}).status_code == 401
    signed = headers(path, device=None)
    signed["X-Device-Id"] = "dev_one"
    assert client.post(path, json=manifest.model_dump(), headers=signed).status_code == 403


def test_no_authority_fields_or_replayed_assertions_are_accepted(api):
    client, (_, _, manifest) = api
    path = "/ventures/v_one/device-work-orders"
    assert client.post(path, json={**manifest.model_dump(), "actor_id": "other"}, headers=headers(path)).status_code == 422
    signed = headers(path)
    assert client.post(path, json=manifest.model_dump(), headers=signed).status_code == 200
    assert client.post(path, json=manifest.model_dump(), headers=signed).status_code == 401


def test_internal_errors_do_not_disclose_private_messages(api, monkeypatch):
    client, (service, _, manifest) = api
    def reject(*args):
        raise ValueError("private customer text")
    monkeypatch.setattr(service, "prepare", reject)
    path = "/ventures/v_one/device-work-orders"
    result = client.post(path, json=manifest.model_dump(), headers=headers(path))
    assert result.status_code == 409
    assert result.json() == {"detail": {"code": "device_order_invalid"}}


@pytest.fixture
def local_build_api(monkeypatch):
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    load_company_os_app()
    from app.api import device_orders_api

    return device_orders_api


@pytest.mark.parametrize("domain", ["Warehouse stock reconciliation", "Family walking journal"])
@pytest.mark.parametrize("code, next_step", [
    ("device_coding_request_contains_secret", "Settings > Keys and connections"),
    ("device_coding_request_too_long", "In your venture chat, shorten your request to the code changes you want made, then send it again."),
    ("device_coding_plan_invalid", "failed plan check"),
    ("device_coding_loop_scope_invalid", "failed scope check"),
    ("device_coding_task_invalid", "failed task check"),
    ("device_coding_focus_too_long", "Plan"),
])
def test_local_build_errors_keep_code_and_exact_next_step(local_build_api, caplog, domain, code, next_step):
    device_orders_api = local_build_api

    def reject(instruction):
        assert domain in instruction
        raise ValueError(code)

    with pytest.raises(device_orders_api.HTTPException) as caught:
        device_orders_api.call(reject, "Build " + domain)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == code
    assert next_step in caught.value.detail["message"]
    if code == "device_coding_focus_too_long":
        assert "12000" in caught.value.detail["message"]
    if "invalid" in code:
        assert "Open Builds in your venture" in caught.value.detail["message"]
    assert any(getattr(record, "error_code", None) == code for record in caplog.records)
    assert domain not in caplog.text


@pytest.mark.parametrize("domain", ["Warehouse stock reconciliation", "Family walking journal"])
def test_local_unknown_build_error_is_actionable_without_logging_private_text(local_build_api, caplog, domain):
    device_orders_api = local_build_api

    def reject():
        raise ValueError(domain + " private credentials")

    with pytest.raises(device_orders_api.HTTPException) as caught:
        device_orders_api.call(reject)
    assert caught.value.detail["code"] == "device_order_invalid"
    assert "Report a problem" in caught.value.detail["message"]
    assert "device_order_invalid" in caught.value.detail["message"]
    assert domain not in json.dumps(caught.value.detail) + caplog.text
    direct = device_orders_api._conflict_detail(domain + " private credentials")
    assert direct["code"] == "device_order_invalid"
    assert domain not in json.dumps(direct)


@pytest.mark.parametrize("domain", ["Warehouse stock reconciliation", "Family walking journal"])
def test_local_invalid_plan_receipt_names_the_failed_plan_without_echoing_input(local_build_api, monkeypatch, caplog, domain):
    import asyncio
    from starlette.requests import Request

    device_orders_api = local_build_api
    monkeypatch.setattr(device_orders_api, "verify_caller", lambda *_args, **_kwargs: None)

    async def receive():
        return {"type": "http.request", "body": json.dumps({"private": domain}).encode(), "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
    with pytest.raises(device_orders_api.HTTPException) as caught:
        asyncio.run(device_orders_api._product_receipt(request, "venture", "order",
            device_orders_api.device_orders.ProductPlanReceipt, lambda *_args: pytest.fail("Invalid plan cannot be saved"), 32768))
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "device_coding_plan_invalid"
    assert "failed plan check" in caught.value.detail["message"]
    assert domain not in json.dumps(caught.value.detail) + caplog.text
    assert any(getattr(record, "error_code", None) == "device_coding_plan_invalid" for record in caplog.records)


def test_missing_reviewed_email_returns_actionable_code(api, monkeypatch):
    client, (service, _, _) = api

    def missing_email(*_args):
        raise ValueError("device_chat_email_required")

    monkeypatch.setattr(service, "prepare_from_chat", missing_email)
    path = "/ventures/v_one/device-work-orders/from-chat"
    result = client.post(path, json={"instruction": "Go to resend.com and sign up using my work email"},
                         headers=headers(path))
    assert result.status_code == 409
    assert result.json() == {"detail": {"code": "device_chat_email_required"}}


def test_public_site_review_requires_signed_device_and_returns_saved_review_path(api, monkeypatch):
    client, (service, _, manifest) = api
    path = "/ventures/v_one/plan-tasks/task_public/public-site-review"
    expected = {"id": "dwo_public", "venture_id": "v_one", "manifest": manifest.model_dump()}
    def prepare_review(venture_id, caller, task_id):
        if not caller.device_id:
            raise PermissionError("device required")
        return expected
    monkeypatch.setattr(service, "prepare_public_site_review", prepare_review)
    assert client.post(path).status_code == 401
    assert client.post(path, headers=headers(path, device=None)).status_code == 403
    result = client.post(path, headers=headers(path))
    assert result.status_code == 200, result.text
    assert result.json()["review_path"] == "/v/v_one/device-work-orders/dwo_public"


def test_public_site_review_conflicts_keep_actionable_code(api, monkeypatch):
    client, (service, _, _) = api
    path = "/ventures/v_one/plan-tasks/task_public/public-site-review"
    def reject(*args):
        raise ValueError("device_public_review_deployment_ambiguous")
    monkeypatch.setattr(service, "prepare_public_site_review", reject)
    result = client.post(path, headers=headers(path))
    assert result.status_code == 409
    assert result.json() == {"detail": {"code": "device_public_review_deployment_ambiguous"}}


def test_chat_browser_request_uses_server_derived_review(api, monkeypatch):
    client, (service, _, manifest) = api
    path = "/ventures/v_one/device-work-orders/from-chat"
    expected = {"id": "dwo_chat", "venture_id": "v_one", "workspace_id": "w_one",
                "manifest": manifest.model_dump()}
    seen = []
    def prepare_chat(venture_id, caller, instruction, target_application=None,
                     target_pid=None, target_process_start=None, target_window=None,
                     target_window_title=None):
        seen.append((venture_id, caller.device_id, instruction))
        assert (target_application, target_pid, target_process_start,
                target_window, target_window_title) == (None, None, None, None, None)
        return expected
    monkeypatch.setattr(service, "prepare_from_chat", prepare_chat)
    result = client.post(path, json={"instruction": "Verify the live site and waitlist"}, headers=headers(path))
    assert result.status_code == 200, result.text
    assert result.json()["review_path"] == "/v/v_one/device-work-orders/dwo_chat"
    assert seen == [("v_one", "dev_one", "Verify the live site and waitlist")]
    # A retry returns the same order. The founder request, review card and
    # operator telemetry must each be durable exactly once.
    retry = client.post(path, json={"instruction": "Verify the live site and waitlist"}, headers=headers(path))
    assert retry.status_code == 200
    from app.chat.store import list_messages
    from app.db.models import AnalyticsEvent
    from app.state import db
    from sqlalchemy import select
    messages = list_messages("v_one")
    assert [(row["role"], row["kind"]) for row in messages] == [
        ("user", "text"), ("assistant", "confirmation")]
    assert messages[1]["actions"][0]["href"] == "/v/v_one/device-work-orders/dwo_chat"
    with db()() as session:
        events = session.scalars(select(AnalyticsEvent).where(
            AnalyticsEvent.venture_id == "v_one",
            AnalyticsEvent.event_name.in_(["request.received", "request.outcome", "feature.used"]))).all()
    assert sorted(row.event_name for row in events) == ["feature.used", "request.outcome", "request.received"]
    assert all("Verify the live site" not in row.props_json for row in events)
    received = next(row for row in events if row.event_name == "request.received")
    assert json.loads(received.props_json)["order_id"] == "dwo_chat"
    feature_use = next(row for row in events if row.event_name == "feature.used")
    assert json.loads(feature_use.props_json) == {"source": "chat", "feature": "computer_use",
                                                   "invocation": "mac_review_prepared", "order_id": "dwo_chat"}
    assert client.post(path, json={"instruction": "x", "origins": ["https://evil.test"]},
                       headers=headers(path)).status_code == 422


def test_chat_mac_task_rejects_pasted_secret_before_preparation(api, monkeypatch):
    client, (service, _, _) = api
    path = "/ventures/v_one/device-work-orders/from-chat"
    monkeypatch.setattr(service, "prepare_from_chat", lambda *_args: (_ for _ in ()).throw(
        AssertionError("secret must not reach order preparation")))
    response = client.post(path, json={"instruction": "Use API_" + "KEY=" + "example-example-example"},
                           headers=headers(path))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "device_chat_secret_use_form"


def test_chat_browser_request_keeps_supported_conflict_code(api, monkeypatch):
    client, (service, _, _) = api
    path = "/ventures/v_one/device-work-orders/from-chat"
    monkeypatch.setattr(service, "prepare_from_chat",
                        lambda *args: (_ for _ in ()).throw(ValueError("device_chat_intent_unsupported")))
    result = client.post(path, json={"instruction": "Delete everything"}, headers=headers(path))
    assert result.status_code == 409
    body = result.json()
    assert body["detail"]["code"] == "device_chat_intent_unsupported"
    assert "Supported tasks" in body["detail"]["message"]
    assert "Open this venture's Builds or Settings in Supymem" in body["detail"]["supported"]


@pytest.mark.parametrize("target", [{}, {"target_application": "com.google.Chrome", "target_pid": 77,
                                      "target_process_start": "123", "target_window": 10},
                                     {"reviewed_surfaces": [{"application_bundle": "com.google.Chrome",
                                      "process_id": 77, "process_start_identity": "123", "window_id": 10, "window_title": "Owned"}]}])
def test_readonly_browser_chat_refuses_before_scope_card_quote_or_credit_effect(api, monkeypatch, target):
    client, (service, _, _) = api
    from app.chat.store import list_messages
    from app.db.models import AnalyticsEvent, CreditAccount, VentureDocument
    from app.state import db
    from sqlalchemy import select

    def proof():
        with db()() as session:
            return (
                [(r.id, r.provenance_json) for r in session.scalars(select(VentureDocument)).all()],
                [(r.id, r.spent_micro, r.reserved_micro) for r in session.scalars(select(CreditAccount)).all()],
                [r.id for r in session.scalars(select(AnalyticsEvent)).all()],
                list_messages("v_one"),
            )

    before = proof()
    def unexpected(*_args, **_kwargs):
        raise AssertionError("a refused read-only request must not resolve scope or prepare any effect")
    monkeypatch.setattr(service, "_scope", unexpected)
    monkeypatch.setattr(service, "prepare", unexpected)
    monkeypatch.setattr(service, "prepare_multisurface_from_chat", unexpected)
    path = "/ventures/v_one/device-work-orders/from-chat"
    result = client.post(path, json={"instruction": (
        "Use my Mac for a read-only background browser task. Allowed sites are https://example.test "
        "and https://second.test. Compare the pages. Do not type, submit, download or change anything."
    ), **target}, headers=headers(path))
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "device_read_only_browser_task_unavailable"
    assert "no-change constraint" in result.json()["detail"]["message"]
    assert "did not prepare or start a new task or reserve credits" in result.json()["detail"]["message"]
    assert "review_path" not in result.json()
    assert proof() == before


def test_saved_review_receipt_preserves_truthful_prepared_card(api, monkeypatch):
    client, (service, caller, manifest) = api
    from app.chat import claims
    from app.chat.store import list_messages

    saved = service.prepare("v_one", caller, manifest)
    monkeypatch.setattr(service, "prepare_from_chat", lambda *_args: saved)
    path = "/ventures/v_one/device-work-orders/from-chat"
    result = client.post(path, json={"instruction": "Verify the live site and waitlist"}, headers=headers(path))
    assert result.status_code == 200, result.text
    assert service.retrieve("v_one", caller, saved["id"])["state"] == "pending"
    card = list_messages("v_one")[-1]
    assert card["kind"] == "confirmation" and claims.claims_action(card["body_md"])
    assert "I prepared a Mac task for review:" in card["body_md"]
    assert card["actions"][0]["href"] == result.json()["review_path"]


@pytest.mark.parametrize("code,expected", [
    ("device_personal_build_unavailable", "Confirm the current personal project idea"),
    ("device_personal_build_app_ambiguous", "Open Builds to choose"),
])
def test_personal_build_blocker_is_actionable(api, monkeypatch, code, expected):
    client, (service, _, _) = api
    path = "/ventures/v_one/device-work-orders/from-chat"
    monkeypatch.setattr(service, "prepare_from_chat",
                        lambda *_args: (_ for _ in ()).throw(ValueError(code)))
    result = client.post(path, json={"instruction": "Use Codex to build my personal project"},
                         headers=headers(path))
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == code
    assert expected in result.json()["detail"]["message"]


def test_fixed_terminal_capability_does_not_require_a_model_provider(api, monkeypatch):
    client, (service, caller, manifest) = api
    terminal = service.Manifest.model_validate({**manifest.model_dump(),
        "applications": ["com.companyos.app"], "origins": [], "actions": ["terminal.execute"],
        "terminal_verification": {"operation": "git_status_v1", "workspace_slug": "fixture-one"},
    })
    order = service.prepare("v_one", caller, terminal)
    path = f"/ventures/v_one/device-work-orders/{order['id']}/capabilities"
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.delenv("JEV_DEVICE_DECISIONS_ENABLED", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    result = client.get(path, headers=headers(path, "GET"))
    assert result.status_code == 200
    assert result.json() == {"available": True}


def test_signed_decision_endpoint_meters_once_and_never_returns_execution_authority(api, monkeypatch):
    client, (service, caller, manifest) = api
    from app.build import device_jev
    monkeypatch.setenv("JEV_DEVICE_DECISIONS_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    order = service.prepare("v_one", caller, manifest.model_copy(update={"credit_ceiling_micro": 8000}))
    for action in ("approve", "claim"):
        service.transition("v_one", caller, order["id"], order["review_digest"], action)
    path = f"/ventures/v_one/device-work-orders/{order['id']}/browser-decision"
    body = {"invocation_id": "decision_one", "observation": {"revision": "one", "application": "com.apple.Safari",
        "location": "https://example.test", "goal": "Client goal is not authority", "context": "Expected page loaded",
        "controls": [], "permitted": True, "confirmation_pending": False, "prior_effect_uncertain": False}}
    sent = []
    def provider(request):
        sent.append(request)
        return {"model": "jev-1.13.0", "usage": {"input_tokens": 1000, "output_tokens": 40},
            "answers": {"operation": {"type": "choice", "choice": "done",
                "probabilities": {"done": 1.0, "wait": 0.0, "blocked": 0.0}}}}
    monkeypatch.setattr(device_jev, "_provider_call", provider)
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers=headers(path, device=None)).status_code == 403
    assert client.post(path, json=body, headers=headers(path, device="dev_foreign")).status_code == 404
    for field, value in [("actual_micro", 0), ("quote_micro", 0), ("provider_url", "https://foreign.test"),
                         ("model", "different"), ("device_id", "dev_one")]:
        assert client.post(path, json={**body, field: value}, headers=headers(path)).status_code == 422
    assert sent == []
    result = client.post(path, json=body, headers=headers(path))
    assert result.status_code == 200, result.text
    assert result.json()["authority"] == "proposal_only"
    assert result.json()["candidate"]["operation"] == "done"
    assert result.json()["candidate"]["requires_outcome_verification"] is True
    assert sent[0]["state"]["goal"] == manifest.instruction
    assert client.post(path, json=body, headers=headers(path)).status_code == 409
    assert len(sent) == 1
    saved = service.retrieve("v_one", caller, order["id"])
    assert saved["state"] == "claimed"
    assert saved["invocations"]["decision_one"]["usage"]["input_tokens"] == 1000
    monkeypatch.delenv("JEV_DEVICE_DECISIONS_ENABLED")
    assert client.post(path, json={**body, "invocation_id": "second"}, headers=headers(path)).status_code == 503
    assert len(sent) == 1


def test_desktop_vision_route_requires_signed_bound_device(api, monkeypatch):
    import base64
    import json
    from io import BytesIO

    from PIL import Image
    from app.build import device_vision

    client, (service, caller, manifest) = api
    with service.db()() as session:
        from app.db.models import CreditAccount
        account = session.get(CreditAccount, "cra_device_test")
        account.granted_micro = 1_000_000
        session.commit()
    desktop = service.Manifest.model_validate({**manifest.model_dump(exclude_none=True),
        "applications": ["com.apple.Calculator"], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "credit_ceiling_micro": 500_000,
        "desktop_verification": {"operation": "desktop_goal_v1",
            "application_bundle": "com.apple.Calculator", "process_id": 123,
            "process_start_identity": "123456", "window_id": 10,
            "window_title": "Calculator", "max_steps": 4,
            "reviewed_text": [], "visual_review": True}})
    order = service.prepare("v_one", caller, desktop)
    for action in ("approve", "claim"):
        service.transition("v_one", caller, order["id"], order["review_digest"], action)
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "synthetic-test-key")
    calls = []
    def provider(body):
        calls.append(body)
        return {"model": device_vision.MODEL, "usage": {"input_tokens": 100, "output_tokens": 40},
            "output": [{"content": [{"type": "output_text", "text": json.dumps({
                    "verdict": "blocked", "visible_facts": [], "next_step": "",
                    "click_control_id": "none"})}]}]}
    monkeypatch.setattr(device_vision, "_provider_call", provider)
    image = Image.new("RGB", (64, 64), "white")
    buffer = BytesIO()
    image.save(buffer, "PNG")
    path = f"/ventures/v_one/device-work-orders/{order['id']}/desktop-vision"
    body = {"invocation_id": "vision_route_one", "snapshot_id": "ps1_abc",
            "screenshot_png_base64": base64.b64encode(buffer.getvalue()).decode()}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers=headers(path, device=None)).status_code == 403
    assert client.post(path, json=body, headers=headers(path, device="dev_foreign")).status_code == 404
    assert calls == []
    result = client.post(path, json=body, headers=headers(path))
    assert result.status_code == 200, result.text
    assert result.json()["authority"] == "observation_only"
    assert client.post(path, json=body, headers=headers(path)).status_code == 409
    assert len(calls) == 1


def test_effect_routes_are_disabled_until_enabled_and_consume_saved_proposal_once(api, monkeypatch):
    from app.build import device_effects, device_invocations
    from coding_agents.jev_decision import Candidate
    client, (service, caller, manifest) = api
    order = service.prepare("v_one", caller, manifest.model_copy(update={"actions": ["browser.read", "browser.interact"]}))
    args = ("v_one", caller, order["id"])
    for action in ("approve", "claim"):
        service.transition(*args, order["review_digest"], action)
    device_invocations.admit(*args, invocation_id="once", request_digest="a" * 64, quote_micro=100)
    device_invocations.record_result(*args, invocation_id="once", actual_micro=42)
    device_effects.save_proposal(*args, invocation_id="once", candidate=Candidate("click", "c0", "b" * 64))
    path = f"/ventures/v_one/device-work-orders/{order['id']}/effects/admit"
    authority = path.replace("/effects/admit", "/authority")
    body = {"invocation_id": "once", "effect_digest": "c" * 64, "observation_digest": "b" * 64}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers=headers(path)).status_code == 503
    assert client.get(authority, headers=headers(authority, "GET")).status_code == 503
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    assert client.get(authority, headers=headers(authority, "GET")).json() == {"current": True}
    assert client.post(path, json=body, headers=headers(path)).json() == {"admitted_now": True}
    assert client.post(path, json=body, headers=headers(path)).json() == {"admitted_now": False}
    monkeypatch.delenv("DEVICE_BROWSER_EXECUTION_ENABLED")
    receipt = path.replace("/admit", "/receipt")
    payload = {"invocation_id": "once", "effect_digest": "c" * 64, "outcome": "uncertain"}
    assert client.post(receipt, json=payload, headers=headers(receipt)).json() == {"recorded": True}
    with pytest.raises(ValueError, match="reconciliation"):
        device_invocations.admit(*args, invocation_id="next", request_digest="d" * 64, quote_micro=100)
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    service.transition(*args, order["review_digest"], "cancel")
    assert client.get(authority, headers=headers(authority, "GET")).status_code == 409


def test_native_beta_gate_exposes_truthful_unavailable_metadata_and_refuses_old_cards(api, monkeypatch):
    from tests.unit.product.test_native_desktop_admission import desktop_manifest
    from app.build import device_effects, device_invocations
    from coding_agents.jev_decision import Candidate
    client, (service, caller, _) = api
    order = service.prepare("v_one", caller, desktop_manifest(service))
    args = ("v_one", caller, order["id"])
    for action in ("approve", "claim"):
        service.transition(*args, order["review_digest"], action)
    device_invocations.admit(*args, invocation_id="native", request_digest="a" * 64, quote_micro=100)
    device_invocations.record_result(*args, invocation_id="native", actual_micro=42)
    device_effects.save_proposal(*args, invocation_id="native", candidate=Candidate("key", "c0", "b" * 64))
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.delenv(service.DESKTOP_EXECUTION_ENV)
    base = "/ventures/v_one/device-work-orders"
    path = base + "/" + order["id"]
    capability = client.get(path + "/capabilities", headers=headers(path + "/capabilities", "GET"))
    assert capability.json()["available"] is False
    assert capability.json()["code"] == "device_desktop_execution_unavailable"
    metadata = "/ventures/v_one/device-desktop-pause"
    assert client.get(metadata).status_code == 401
    result = client.get(metadata, headers=headers(metadata, "GET")).json()
    assert result["paused"] is False and result["execution_available"] is False
    assert result["unavailability_code"] == "device_desktop_execution_unavailable"
    assert "not available in this beta" in result["execution_message"]
    for action in ("approve", "claim"):
        response = client.post(path + "/" + action, json={"review_digest": order["review_digest"]},
                               headers=headers(path + "/" + action))
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "device_desktop_execution_unavailable"
    authority = client.get(path + "/authority", headers=headers(path + "/authority", "GET"))
    assert authority.status_code == 409 and authority.json()["detail"]["code"] == "device_desktop_execution_unavailable"
    effect = client.post(path + "/effects/admit", json={"invocation_id": "native",
        "effect_digest": "c" * 64, "observation_digest": "b" * 64}, headers=headers(path + "/effects/admit"))
    assert effect.status_code == 409 and effect.json()["detail"]["code"] == "device_desktop_execution_unavailable"
    assert "effect" not in service.retrieve(*args)["invocations"]["native"]
    create = client.post(base, json=desktop_manifest(service).model_dump(), headers=headers(base))
    assert create.status_code == 409 and create.json()["detail"]["code"] == "device_desktop_execution_unavailable"
    assert client.post(path + "/cancel", json={"review_digest": order["review_digest"]},
                       headers=headers(path + "/cancel")).json()["state"] == "cancelled"


def test_native_flag_off_does_not_change_isolated_browser_capability_or_first_party_navigation(api, monkeypatch):
    client, (service, caller, manifest) = api
    monkeypatch.delenv(service.DESKTOP_EXECUTION_ENV)
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("JEV_DEVICE_DECISIONS_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "owned-synthetic-provider")
    order = service.prepare("v_one", caller, manifest)
    path = f"/ventures/v_one/device-work-orders/{order['id']}/capabilities"
    assert client.get(path, headers=headers(path, "GET")).json() == {"available": True}
    assert service.classify_device_chat_intent("Open Builds in Supymem") != "desktop"


def test_stop_transport_requires_signed_execution_binding_and_is_idempotent(api):
    client, (service, caller, manifest) = api
    order = service.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"], order["review_digest"])
    service.transition(*args, "approve")
    claimed = service.transition(*args, "claim")
    path = f"/ventures/v_one/device-work-orders/{order['id']}/stop-receipt"
    body = {"execution_id": claimed["execution_id"], "evidence_digest": "d" * 64, "processes_stopped": True}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json={**body, "execution_id": "dex_wrong"}, headers=headers(path)).status_code == 409
    first = client.post(path, json=body, headers=headers(path))
    assert first.status_code == 200 and first.json() == {"state": "stopped", "settled_micro": 0}
    assert client.post(path, json=body, headers=headers(path)).json() == first.json()


def test_signed_stop_cannot_verify_an_order_without_execution_evidence(api):
    client, (service, caller, manifest) = api
    reviewed = service.Manifest.model_validate({**manifest.model_dump(exclude_none=True),
        "browser_verification": {"expected_locations": ["https://example.test"]}})
    order = service.prepare("v_one", caller, reviewed)
    args = ("v_one", caller, order["id"], order["review_digest"])
    service.transition(*args, "approve")
    claimed = service.transition(*args, "claim")
    path = f"/ventures/v_one/device-work-orders/{order['id']}/stop-receipt"
    body = {"execution_id": claimed["execution_id"], "evidence_digest": "d" * 64,
        "processes_stopped": True, "task_verified": True}
    assert client.post(path, json=body, headers=headers(path)).status_code == 409
    assert service.retrieve("v_one", caller, order["id"]) == claimed
    body["task_verified"] = False
    assert client.post(path, json=body, headers=headers(path)).json()["state"] == "stopped"


def test_stop_receipt_stores_reason_code_outside_receipt(api):
    client, (service, caller, manifest) = api
    order = service.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"], order["review_digest"])
    service.transition(*args, "approve")
    claimed = service.transition(*args, "claim")
    path = f"/ventures/v_one/device-work-orders/{order['id']}/stop-receipt"
    body = {"execution_id": claimed["execution_id"], "evidence_digest": "d" * 64, "processes_stopped": True}
    assert client.post(path, json={**body, "reason_code": "Bad Code"}, headers=headers(path)).status_code == 422
    ok = client.post(path, json={**body, "reason_code": "cu_repeated_action"}, headers=headers(path))
    assert ok.status_code == 200 and ok.json()["state"] == "stopped"
    assert client.post(path, json=body, headers=headers(path)).status_code == 200  # retry without code stays idempotent


def test_startup_reconciliation_transport_requires_cancelled_zero_call_claim(api):
    from app.build import device_stop
    client, (service, caller, manifest) = api
    order = service.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"])
    service.transition(*args, order["review_digest"], "approve")
    claimed = service.transition(*args, order["review_digest"], "claim")
    body = {"execution_id": claimed["execution_id"], "evidence_digest": "d" * 64}
    device_stop.record(*args, **body, processes_stopped=False)
    path = f"/ventures/v_one/device-work-orders/{order['id']}/startup-reconciliation"
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers=headers(path)).status_code == 409
    service.transition(*args, order["review_digest"], "cancel")
    result = client.post(path, json=body, headers=headers(path))
    assert result.status_code == 200, result.text
    assert result.json() == {"state": "stopped", "settled_micro": 0,
                             "reconciliation": "pre_inference_startup_failure"}


def test_stopped_order_can_be_marked_reviewed_and_clears_needs_you(api):
    import pytest

    from app.db.models import VentureDocument
    from app.state import db
    from app.ventures.run_status import _project_recent_mac_task

    client, (service, caller, manifest) = api
    order = service.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"], order["review_digest"])
    service.transition(*args, "approve")
    claimed = service.transition(*args, "claim")
    stop = f"/ventures/v_one/device-work-orders/{order['id']}/stop-receipt"
    body = {"execution_id": claimed["execution_id"], "evidence_digest": "d" * 64, "processes_stopped": True}
    assert client.post(stop, json=body, headers=headers(stop)).status_code == 200

    def status():
        result = {"state": "idle", "stage": "", "activity": "", "changed_at": None, "job_id": None,
                  "continues_when_closed": None, "progress": None, "action": None}
        with db()() as session:
            _project_recent_mac_task(result, "v_one", session.get(VentureDocument, order["id"]))
        return result["state"]

    assert status() == "needs_you"
    path = f"/ventures/v_one/device-work-orders/{order['id']}/acknowledge"
    ok = client.post(path, json={"review_digest": order["review_digest"]}, headers=headers(path))
    assert ok.status_code == 200 and ok.json()["state"] == "stopped" and ok.json()["reviewed_at"]
    assert status() == "completed"
    other = service.prepare("v_one", caller, manifest)
    with pytest.raises(ValueError):
        service.transition("v_one", caller, other["id"], other["review_digest"], "acknowledge")


def test_claimed_or_cancelled_run_with_open_reconciliation_can_be_attested_reviewed(api):
    import pytest

    from app.ventures.device_attention import pending_mac_reconciliation

    client, (service, caller, manifest) = api
    order = service.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"], order["review_digest"])
    with pytest.raises(ValueError):  # not yet claimed
        service.transition(*args, "acknowledge")
    service.transition(*args, "approve")
    service.transition(*args, "claim")
    done = service.transition(*args, "acknowledge")
    assert done["state"] == "claimed" and done["reviewed_at"] and done["reviewed_open_reconciliation"] is True
    cancelled = {"state": "cancelled", "stop_reconciliation_required": True}
    assert pending_mac_reconciliation(cancelled)
    assert pending_mac_reconciliation({**cancelled, "reviewed_at": "2026-09-30T00:00:00"}) is None


def test_static_source_qa_requires_signed_device_and_exact_budget_body(api, monkeypatch):
    client, _ = api
    from app.build import subscription_static_admission as service
    quote_path = '/ventures/v_one/device-work-orders/dwo_static/static-qa-quote'
    admit_path = '/ventures/v_one/device-work-orders/dwo_static/static-qa'
    calls = []
    def estimate(vid, oid, caller):
        calls.append((vid, oid, caller.device_id))
        return {'quote_id': 'ssq_' + 'a' * 48, 'quote_digest': 'b' * 64, 'qa_ceiling_micro': 1020,
                'model_ceiling_micro': 0, 'builder_compute_ceiling_micro': 0}
    monkeypatch.setattr(service, 'estimate', estimate)
    assert client.post(quote_path).status_code == 401
    assert calls == []
    quote = client.post(quote_path, headers=headers(quote_path))
    assert quote.status_code == 200 and calls == [('v_one', 'dwo_static', 'dev_one')]
    entered = []
    monkeypatch.setattr(service, 'admit', lambda vid, oid, caller, **body: entered.append(body) or {'id': 'bsi_saved'})
    approved = {**{k: quote.json()[k] for k in ('quote_id', 'quote_digest')}, 'approve_qa_ceiling_micro': 1020}
    for changes in ({'approve_qa_ceiling_micro': True}, {'approve_qa_ceiling_micro': 0}, {'builder_budget': 1}):
        assert client.post(admit_path, json={**approved, **changes}, headers=headers(admit_path)).status_code == 422
    assert entered == []
    result = client.post(admit_path, json=approved, headers=headers(admit_path))
    assert result.status_code == 200 and entered == [approved]


def test_multisurface_from_chat_signed_transport_is_staged_and_shape_closed(api, monkeypatch):
    client, (svc, _, _) = api
    from tests.unit.product.test_device_orders import _multi_surface_manifest
    path = '/ventures/v_one/device-work-orders/from-chat'
    surfaces = _multi_surface_manifest(svc)['desktop_verification']['reviewed_surfaces']
    body = {'instruction': 'Copy only the selected existing windows', 'reviewed_surfaces': surfaces}
    monkeypatch.delenv('SUPYMEM_REVIEWED_DESKTOP_V2', raising=False)
    response = client.post(path, json=body, headers=headers(path))
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'device_desktop_multisurface_unavailable'
    assert 'No task was started' in response.json()['detail']['message']
    assert client.post(path, json=body, headers={'X-Subject':'founder'}).status_code == 401
    ambiguous = {**body, 'target_application':'com.apple.TextEdit'}
    assert client.post(path, json=ambiguous, headers=headers(path)).status_code == 422
    authority = {**body, 'reviewed_surfaces':[{**surfaces[0], 'allow_launch':True}]}
    assert client.post(path, json=authority, headers=headers(path)).status_code == 422
    generic = _multi_surface_manifest(svc)
    path = '/ventures/v_one/device-work-orders'
    response = client.post(path, json=generic, headers=headers(path))
    assert response.status_code == 409 and response.json()['detail']['code'] == 'device_desktop_multisurface_unavailable'
