"""Stripe cash reads stay private, read-only, and separate from experiment attribution."""
import asyncio
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest
from starlette.requests import Request

from tests.unit.test_composio_toolkit_catalog import DOMAINS, scopes


@pytest.fixture(autouse=True)
def revenue_mode():
    return os.environ.get("SUPYMEM_MODE", "")


@pytest.fixture(params=DOMAINS)
def revenue_case(revenue_mode, scopes, monkeypatch, request):
    from app.core.local_mode import is_local
    from app.connections import revenue
    from app.content import composio_social
    from app.content.oauth import callbacks
    from app.credits import metered, policy
    from app.gtm.supymem import composio_gmail
    from app.tenancy import bind

    if revenue_mode == "local":
        monkeypatch.setenv("SUPYMEM_MODE", "local")
    if is_local():
        from app.core.local_secrets import update_settings
        from app.core.providers_local import save_settings
        update_settings("composio", {"auth_configs": {"stripe": "ac_stripe_test"}}, {"api_key": "test-only"})
        update_settings("capabilities", {"connected_apps": True}, {})
        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
            models={"chat": "local-test-model"})

    scope = next(scope for scope in scopes if scope["domain"] == request.param)
    now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    state = {"flag": True, "calls": [], "meters": [], "remote": {}, "failure": None, "active": True}
    _set_tracking(state, True)
    charge_sync = metered.charge_sync
    monkeypatch.setattr(policy, "get_policy", lambda _key, default=None: {"experiments_autopilot_v1": state["flag"]})
    monkeypatch.setattr(revenue, "utcnow", lambda: now)
    monkeypatch.setenv("COMPOSIO_STRIPE_AUTH_CONFIG_ID", "ac_stripe_test")
    monkeypatch.setenv("COMPOSIO_STRIPE_TOOLKIT_VERSION", "20261026_00")
    monkeypatch.setenv("GMAIL_TOOLKIT_VERSION", "20260101_00")

    @contextmanager
    def charge(**kwargs):
        state["meters"].append(kwargs)
        with charge_sync(**kwargs) as meter:
            yield meter

    monkeypatch.setattr(metered, "charge_sync", charge)

    def payment(identifier, days, customer, amount, currency="usd", **extra):
        return {"id": identifier, "created": int((now - timedelta(days=days)).timestamp()),
                "customer": customer, "amount": amount, "amount_captured": amount if extra.get("captured", True) else 0,
                "amount_refunded": 0, "currency": currency,
                "paid": True, "captured": True, "status": "succeeded", "livemode": True, "disputed": False,
                "billing_details": {"email": "private@example.test"},
                "payment_method_details": {"card": {"last4": "1111"}}, **extra}

    def subscription(identifier, days, amount, currency="usd", interval="month", **extra):
        return {"id": identifier, "created": int((now - timedelta(days=days)).timestamp()),
                "livemode": True, "status": "active", "customer": "private_customer",
                "items": {"has_more": False, "data": [{"quantity": 1, "price": {"currency": currency,
                    "unit_amount": amount, "recurring": {"interval": interval, "interval_count": 1}}}]}, **extra}

    state["charges"] = [payment("ch_existing", 2, "customer_old", 5000),
        payment("ch_new", 1, "customer_new", 3000, amount_refunded=500),
        payment("ch_earlier", 20, "customer_earlier", 4000, "eur"),
        payment("ch_repeat", 4, "customer_earlier", 1000, "eur"),
        payment("ch_boundary", 7, "customer_boundary", 100, "jpy"),
        payment("ch_failed", 1, "customer_failed", 2000, paid=False, status="failed"),
        payment("ch_refund", 1, "customer_refund", 1000, amount_refunded=1000),
        payment("ch_dispute", 1, "customer_dispute", 500, disputed=True),
        payment("ch_authorization", 1, "customer_authorized", 10000, captured=False),
        payment("ch_test", 1, "customer_test", 999, livemode=False)]
    state["history"] = {"customer_old": [payment("ch_old_failed", 40, "customer_old", 10, paid=False, status="failed"),
        payment("ch_old_paid", 45, "customer_old", 1000, amount_refunded=1000)]}
    state["subscriptions"] = [subscription("sub_recent", 1, 4000),
        subscription("sub_earlier", 20, 12000, "eur", "year"),
        subscription("sub_trial", 1, 999, status="trialing"), subscription("sub_test", 1, 999, livemode=False)]

    def social_request(method, path, **kwargs):
        state["calls"].append((method, path, kwargs))
        if path == "/connected_accounts/link":
            assert method == "POST"
            body = kwargs["json_body"]
            identifier = f"ca_stripe_{len(state['remote'])}"
            state["remote"][identifier] = {"id": identifier, "user_id": body["user_id"], "status": "ACTIVE",
                "toolkit": {"slug": "stripe"}, "auth_config": {"id": "ac_stripe_test"}}
            return {"connected_account_id": identifier, "redirect_url": "https://connect.composio.dev/test"}
        identifier = path.removeprefix("/connected_accounts/")
        assert identifier in state["remote"]
        if method == "DELETE":
            del state["remote"][identifier]
            return {}
        assert method == "GET"
        return {**state["remote"][identifier], "status": "ACTIVE" if state["active"] else "EXPIRED"}

    def gmail_request(method, path, **kwargs):
        state["calls"].append((method, path, kwargs))
        if method == "GET":
            slug = path.removeprefix("/tools/")
            assert slug in revenue.READ_TOOLS
            return {"slug": slug if state["failure"] != "slug" else "STRIPE_CREATE_CHARGE"}
        slug = path.removeprefix("/tools/execute/")
        assert method == "POST" and slug in revenue.READ_TOOLS
        body = kwargs["json"]
        assert body["user_id"] == scope["owner"]
        assert body["connected_account_id"] in state["remote"]
        assert body["version"] == "20261026_00" and "dangerously_skip_version_check" not in body
        arguments = body["arguments"]
        assert arguments["limit"] == 100
        if state["failure"] == "provider":
            raise RuntimeError("private error with private@example.test and card 1111")
        if state["failure"] == "empty_page":
            return {"successful": True, "data": {"data": [], "has_more": True}}
        if state["failure"] == "malformed":
            return {"successful": True, "data": {"data": "private@example.test"}}
        if state["failure"] == "uncertain":
            return {"data": {"data": [], "has_more": False}}
        rows = state["subscriptions"] if slug == revenue.READ_TOOLS[1] else (
            state["history"].get(arguments["customer"], []) if "customer" in arguments else state["charges"])
        cursor = arguments.get("starting_after")
        offset = next(index + 1 for index, row in enumerate(rows) if row["id"] == cursor) if cursor else 0
        page = rows[offset:offset + 2]
        return {"successful": True, "data": {"data": page, "has_more": offset + 2 < len(rows)}}

    monkeypatch.setattr(composio_social, "_request", social_request)
    monkeypatch.setattr(composio_gmail, "_request", gmail_request)

    def connect():
        with bind(workspace_id=scope["workspace"], venture_id=scope["venture"], actor=scope["owner"]):
            started = callbacks.start_managed(workspace_id=scope["workspace"], platform="stripe")
            response = callbacks.callback_response("stripe", Request({"type": "http", "query_string":
                urlencode({"supymem_state": started["state"]}).encode()}))
            if is_local():
                assert response.status_code == 200 and b"oauth=connected" in response.body
            else:
                assert response.status_code == 303 and "oauth=connected" in response.headers["location"]
        state["calls"].clear()
        state["meters"].clear()

    return scope, state, now, connect


def _set_tracking(state, value):
    from app.core.local_mode import is_local

    state["flag"] = value
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"run_for_me_experiments": value}, {})


def test_stripe_totals_are_read_only_net_live_cash_and_first_time_customers(revenue_case):
    from app.connections import revenue

    scope, state, _now, connect = revenue_case
    connect()
    result = revenue.snapshot(scope["venture"])
    assert result["strength"] == "cash" and result["scope"] == "connected_account"
    assert result["windows"]["30"] == {
        "charges": {"count": 5, "amounts": {"usd": 7500, "eur": 5000, "jpy": 100}},
        "new_paying_customers": {"count": 3, "amounts": {"usd": 2500, "eur": 5000, "jpy": 100}},
        "subscriptions": {"count": 2, "amounts": {"usd/month/1": 4000, "eur/year/1": 12000}}}
    assert result["windows"]["7"] == {
        "charges": {"count": 4, "amounts": {"usd": 7500, "eur": 1000, "jpy": 100}},
        "new_paying_customers": {"count": 2, "amounts": {"usd": 2500, "jpy": 100}},
        "subscriptions": {"count": 1, "amounts": {"usd/month/1": 4000}}}
    assert len(state["meters"]) == len(state["calls"])
    assert all(meter["provider"] == "composio" and meter["purpose"].startswith("experiments.revenue")
               or meter["purpose"] == "connections.status" for meter in state["meters"])
    assert all(method == "GET" or path in {f"/tools/execute/{slug}" for slug in revenue.READ_TOOLS}
               for method, path, _kwargs in state["calls"])
    serialized = json.dumps(result) + " ".join(revenue.summary_lines(result))
    assert all(secret not in serialized for secret in ("private@example.test", "1111", "customer_", "ch_", "sub_"))
    assert "JPY 100" in serialized and "USD 75.00" in serialized and "not proof" in serialized
    assert "Subscription amounts are not collected revenue" in serialized


def test_stripe_requires_enabled_tracking_founder_identity_and_active_receipt(revenue_case):
    from app.chat import connect as chat_connect, experiments
    from app.connections import revenue, toolkits
    from app.content.oauth import attempts, callbacks
    from app.db.models import ContentOAuthState
    from app.core.local_mode import is_local
    from app.state import db

    scope, state, _now, connect = revenue_case
    assert revenue.snapshot(scope["venture"]) == {"status": "not_connected", "action": revenue.CONNECT_ACTION}
    _set_tracking(state, False)
    assert chat_connect.wants_connect("connect stripe") and chat_connect.service_from("connect stripe") == "stripe"
    assert revenue.snapshot(scope["venture"]) is None and state["calls"] == []
    connect()
    if is_local():
        receipt = toolkits.accounts(scope["workspace"], "stripe")[0]
        assert receipt["provider_user_id"] == scope["owner"]
        with db()() as session:
            session.get(ContentOAuthState, receipt["id"]).provider_user_id = f"content:{scope['workspace']}"
            session.commit()
    assert toolkits.accounts(scope["workspace"], "stripe")[0]["provider_user_id"] == f"content:{scope['workspace']}"
    _set_tracking(state, True)
    assert chat_connect.wants_connect("connect stripe") and chat_connect.service_from("connect stripe") == "stripe"
    assert revenue.snapshot(scope["venture"])["status"] == "not_connected" and state["calls"] == []
    reply = asyncio.run(experiments.review_reply(scope["venture"]))
    assert sum(action.get("action") == "connections.connect" and action.get("label") == "Connect Stripe"
               for action in reply["actions"]) == 1
    action = next(action for action in reply["actions"] if action.get("action") == "connections.connect"
                  and action.get("label") == "Connect Stripe")
    assert action["label"] == "Connect Stripe" and action["payload"]["query"] == "reconnect stripe"
    from app.tenancy import bind
    with bind(workspace_id=scope["workspace"], venture_id=scope["venture"], actor=scope["owner"]):
        started = chat_connect.handle(scope["venture"], action["payload"]["query"])
    assert "already connected" not in started["body_md"]
    assert started["watch"] == "stripe" and revenue.founder_connection(scope["workspace"]) is None
    assert any(method == "POST" and path == "/connected_accounts/link" for method, path, _kwargs in state["calls"])
    connect()
    assert chat_connect._is_connected(scope["workspace"], "stripe")
    state["calls"].clear()
    receipt = next(row for row in toolkits.accounts(scope["workspace"], "stripe") if row["provider_user_id"] == scope["owner"])
    assert receipt["actor_subject"] == scope["owner"] and receipt["provider_user_id"] == scope["owner"]
    with db()() as session:
        row = session.get(ContentOAuthState, receipt["id"])
        row.actor_subject = "person_other"
        session.commit()
    assert revenue.snapshot(scope["venture"])["status"] == "not_connected" and state["calls"] == []
    with db()() as session:
        row = session.get(ContentOAuthState, receipt["id"])
        row.actor_subject = scope["owner"]
        session.commit()
    state["active"] = False
    assert revenue.snapshot(scope["venture"])["status"] == "not_connected"
    with pytest.raises(attempts.OAuthError):
        callbacks._verified_connection(receipt)
    assert all(method == "GET" for method, _path, _kwargs in state["calls"])
    state["active"] = True
    _set_tracking(state, False)
    calls = len(state["calls"])
    assert revenue.snapshot(scope["venture"]) is None and len(state["calls"]) == calls
    assert chat_connect._is_connected(scope["workspace"], "stripe")
    assert all(method == "GET" and path.startswith("/connected_accounts/") for method, path, _kwargs in state["calls"][calls:])
    _set_tracking(state, True)
    with db()() as session:
        toolkits.revoke_account(session, scope["workspace"], receipt["id"], expected_connection_id=receipt["connected_account_id"])
    state["calls"].clear()
    assert revenue.snapshot(scope["venture"])["status"] == "not_connected" and state["calls"] == []
    assert attempts.successful_receipts(scope["workspace"], platform="stripe")


@pytest.mark.parametrize("failure", ["provider", "empty_page", "malformed", "uncertain", "slug", "currency", "history", "subscription", "missing_live", "duplicate", "limit", "refund", "fraction"])
def test_stripe_incomplete_or_untrusted_reads_never_become_zero_or_cash(revenue_case, failure, caplog):
    from app.connections import revenue

    scope, state, _now, connect = revenue_case
    connect()
    state["failure"] = failure
    if failure == "currency":
        state["charges"][0]["currency"] = "USD\nprivate@example.test"
    elif failure == "history":
        state["history"]["customer_old"][0]["customer"] = "foreign_customer"
    elif failure == "subscription":
        state["subscriptions"][0]["items"]["has_more"] = True
    elif failure == "missing_live":
        del state["charges"][0]["livemode"]
    elif failure == "duplicate":
        state["charges"][1]["id"] = state["charges"][0]["id"]
    elif failure == "limit":
        state["charges"] = [{**state["charges"][0], "id": f"ch_page_{index}"} for index in range(21)]
    elif failure == "refund":
        state["charges"][0]["amount_refunded"] = 5001
    elif failure == "fraction":
        state["charges"][0]["amount"] = 1.5
    assert revenue.snapshot(scope["venture"]) == {"status": "unavailable"}
    assert "private@example.test" not in caplog.text and "1111" not in caplog.text


def test_cash_precedes_failed_tests_without_claiming_they_worked(revenue_case):
    from app.chat import experiments
    from app.connections import revenue
    from app.experiments import ledger

    scope, state, now, connect = revenue_case
    connect()
    identifiers = ledger.create_from_tests(scope["venture"], "founder", [
        {"hypothesis": f"People want {scope['domain']} through invitation {number}"} for number in (1, 2)])
    for identifier in identifiers:
        ledger.record_result(identifier, scope["venture"], True, "I sent 20 invitations. Nobody replied", "refuted",
            "Try another invitation", sample_size=20, provenance={"source": "Your invitation log",
                "method": "Counted replies to your sent invitations", "observed_at": now.isoformat()})
    result = ledger.loop_state(scope["venture"])
    assert result["failed"] and not result["supported"] and not result["ready"]
    chain = experiments._evidence_chain(scope["venture"], result)
    assert chain[0]["strength"] == "cash" and chain[0]["scope"] == "connected_account"
    assert all(item["outcome"] == "refuted" for item in chain[1:])
    reply = asyncio.run(experiments.review_reply(scope["venture"], result))
    assert all(line in reply["body_md"] for line in revenue.summary_lines(result["revenue"]))
    assert "before you change it" in reply["body_md"]
    assert not any(action["action"] in {"experiment.pivot", "experiment.kill"} for action in reply["actions"])
    assert "—" not in reply["body_md"] and "strength" not in reply["body_md"]
    _set_tracking(state, False)
    legacy = ledger.loop_state(scope["venture"])
    assert legacy["ready"] and "revenue" not in legacy
    assert all("strength" not in item for item in experiments._evidence_chain(scope["venture"], result))


def test_variable_subscription_amounts_do_not_discard_collected_cash(revenue_case):
    from app.connections import revenue

    scope, state, _now, connect = revenue_case
    connect()
    price = state["subscriptions"][0]["items"]["data"][0]["price"]
    price["unit_amount"] = None
    price["recurring"]["usage_type"] = "metered"
    result = revenue.snapshot(scope["venture"])
    assert result["strength"] == "cash" and result["windows"]["7"]["charges"]["amounts"]["usd"] == 7500
    assert result["windows"]["7"]["subscriptions"] == {"count": 1, "amounts": {}, "unpriced_count": 1}
    assert "amounts unavailable for 1 subscription" in " ".join(revenue.summary_lines(result))


def test_authorizations_and_disputes_are_not_cash(revenue_case):
    from app.connections import revenue

    scope, state, _now, connect = revenue_case
    connect()
    state["charges"] = [row for row in state["charges"] if row["id"] in {"ch_authorization", "ch_dispute", "ch_refund", "ch_test"}]
    result = revenue.snapshot(scope["venture"])
    assert result["status"] == "connected" and result["strength"] == "none"
    assert result["windows"]["30"]["charges"] == {"count": 0, "amounts": {}}
    assert result["windows"]["7"]["new_paying_customers"] == {"count": 0, "amounts": {}}


def test_partial_captures_use_collected_amount_not_authorized_amount(revenue_case):
    from app.connections import revenue

    scope, state, _now, connect = revenue_case
    connect()
    state["charges"] = [{**state["charges"][0], "id": "ch_partial", "customer": "customer_partial",
                          "amount": 10000, "amount_captured": 2000, "amount_refunded": 500}]
    result = revenue.snapshot(scope["venture"])
    assert result["strength"] == "cash"
    assert result["windows"]["30"]["charges"] == {"count": 1, "amounts": {"usd": 1500}}
    assert result["windows"]["7"]["new_paying_customers"] == {"count": 1, "amounts": {"usd": 1500}}


def test_weekly_digest_includes_owned_cash_even_without_completed_tests(revenue_case, monkeypatch):
    from app.connections import revenue
    from app.db.models import Venture
    from app.email import triggers
    from app.state import db

    scope, state, now, connect = revenue_case
    connect()
    monkeypatch.setattr("app.chat.next_moves.experiment_learning", lambda _venture: None)
    with db()() as session:
        session.add(Venture(id="same_account_venture", workspace_id=scope["workspace"], name="Another direction",
            raw_idea="Another direction", stage="idea", created_at=now - timedelta(days=1), updated_at=now))
        session.commit()
        digest = triggers._weekly_learning(session, scope["owner"], now.replace(tzinfo=None) - timedelta(days=7), now.replace(tzinfo=None))
    assert digest and "paying customers" in digest["next_step"]
    assert digest["path"] == "/connections" and "_venture_id" not in digest
    assert sum("last 30 days" in line for line in digest["changes"]) == 1
    assert sum("last 7 days" in line for line in digest["changes"]) == 1
    assert "not proof" in " ".join(digest["changes"])
    assert scope["domain"] not in " ".join(digest["changes"])
    assert "Another direction" not in " ".join(digest["changes"])
    assert all(other not in json.dumps(digest) for other in DOMAINS if other != scope["domain"])
    assert "private@example.test" not in json.dumps(digest)
    _set_tracking(state, False)
    calls = len(state["calls"])
    with db()() as session:
        assert triggers._weekly_learning(session, scope["owner"], now - timedelta(days=7), now) is None
    assert len(state["calls"]) == calls and revenue.snapshot(scope["venture"]) is None


def test_weekly_digest_counts_a_shared_owned_connection_once(revenue_case, monkeypatch):
    from app.connections import revenue
    from app.db.models import Venture, Workspace
    from app.email import triggers
    from app.state import db

    scope, _state, now, connect = revenue_case
    connect()
    snapshot = revenue.snapshot(scope["venture"], until=now)
    account = revenue.founder_connection(scope["workspace"])
    calls = []
    monkeypatch.setattr(revenue, "founder_connection", lambda workspace_id: account)
    monkeypatch.setattr(revenue, "snapshot", lambda identifier, **kwargs: calls.append(identifier) or snapshot)
    monkeypatch.setattr("app.chat.next_moves.experiment_learning", lambda identifier: None)
    with db()() as session:
        session.add(Workspace(id="shared-revenue-workspace", owner_id=scope["owner"], name=scope["domain"],
            kind="company", entry_mode="idea", created_at=now, updated_at=now))
        session.add(Venture(id="shared-revenue-venture", workspace_id="shared-revenue-workspace", name=scope["domain"],
            raw_idea=scope["domain"], stage="idea", created_at=now, updated_at=now))
        session.commit()
        digest = triggers._weekly_learning(session, scope["owner"], now - timedelta(days=7), now)
    assert len(calls) == 1
    assert sum("last 30 days" in line for line in digest["changes"]) == 1
    assert digest["path"] == "/connections" and "_venture_id" not in digest
