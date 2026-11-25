"""Regressions for the validated Strix core API findings."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from core.clock import utcnow
from tests.security.test_core_signed_boundary import client as client
from tests.security.test_core_signed_boundary import headers, make_workspace


@pytest.fixture()
def ideas(client):
    from app.db.models import Opportunity, OpportunitySlate
    from app.state import db

    own = make_workspace(client, "usr_alice")
    foreign = make_workspace(client, "usr_bob")
    with db()() as session:
        for label, workspace_id in (("alice", own), ("bob", foreign)):
            session.add(OpportunitySlate(
                id=f"ops_{label}", workspace_id=workspace_id,
                seed_json=json.dumps({"geography": f"Fictional {label} district"}),
                created_at=utcnow(),
            ))
        session.flush()
        for label, workspace_id in (("alice", own), ("bob", foreign)):
            session.add(Opportunity(
                id=f"opp_{label}", slate_id=f"ops_{label}", workspace_id=workspace_id,
                title=f"Fictional {label} inventory tool", one_liner="Track stock",
                first_customer="Independent bookshops", wedge="Inventory exports",
                why_now="New export format", kill_criteria="No interested shops",
                shape_tags_json='["fictional_inventory"]', created_at=utcnow(),
            ))
        session.commit()
    return own, foreign


@pytest.mark.parametrize("signed", [True, False])
@pytest.mark.parametrize("body", [
    {"reject_reasons": [{"opportunity_id": "opp_bob"}]},
    {"reject_reasons": [{"opportunity_id": "opp_alice"}, {"opportunity_id": "opp_bob"}]},
    {"previous_slate_id": "ops_bob"},
])
def test_reroll_http_rejects_foreign_body_selectors(client, ideas, monkeypatch, signed, body):
    from app.db.models import Opportunity, OpportunitySlate, WorkspaceMember
    from app.idealab import slate
    from app.state import db

    own, foreign = ideas
    reroll = AsyncMock(return_value=None)
    monkeypatch.setattr(slate, "reroll", reroll)
    with db()() as session:
        session.add(WorkspaceMember(id="wsm_alice_bob", workspace_id=foreign,
            person_id="usr_alice", role="owner", can_approve=1, created_at=utcnow()))
        session.commit()
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1" if signed else "0")
    path = f"/workspaces/{own}/idealab/reroll"
    auth = headers(path, "POST") if signed else {"X-Subject": "usr_alice"}
    response = client.post(path, json=body, headers=auth)
    assert response.status_code == 404, response.text
    reroll.assert_not_called()
    with db()() as session:
        assert len(session.scalars(select(OpportunitySlate)).all()) == 2
        for label in ("alice", "bob"):
            row = session.get(Opportunity, f"opp_{label}")
            assert row.status == "proposed" and row.reject_reason is None


@pytest.mark.parametrize("previous, rejections, code", [
    ("ops_bob", [], "slate_not_found"),
    ("ops_missing", [], "slate_not_found"),
    ("ops_alice", [{"opportunity_id": "opp_bob"}], "opportunity_not_found"),
    ("ops_alice", [{"opportunity_id": "opp_alice"}, {"opportunity_id": "opp_bob"}], "opportunity_not_found"),
    ("ops_alice", [{"opportunity_id": "opp_alice"}, {"opportunity_id": "opp_missing"}], "opportunity_not_found"),
    ("ops_alice", [{"opportunity_id": "opp_alice"}, {"opportunity_id": "opp_bob", "note": 7}], "invalid_rejection"),
    ("ops_alice", [{"opportunity_id": "opp_alice"}, {}], "invalid_rejection"),
])
def test_reroll_service_validates_entire_batch_before_use(ideas, monkeypatch, previous, rejections, code):
    from app.db.models import Opportunity
    from app.idealab import slate
    from app.state import db

    generate = AsyncMock(return_value=None)
    monkeypatch.setattr(slate, "create_slate", generate)
    with pytest.raises(slate.IdeaLabError) as exc:
        asyncio.run(slate.reroll(workspace_id=ideas[0], previous_slate_id=previous,
            reject_reasons=rejections, actor_id="usr_alice"))
    assert exc.value.code == code
    generate.assert_not_called()
    with db()() as session:
        assert all(session.get(Opportunity, f"opp_{label}").status == "proposed"
            for label in ("alice", "bob"))


@pytest.mark.parametrize("body", [
    {"reject_reasons": [{"opportunity_id": "opp_alice"}, {"note": "Missing selector"}]},
    {"reject_reasons": [{"opportunity_id": "opp_alice"}, {"opportunity_id": "opp_alice", "note": 7}]},
    {"reject_reasons": ["not an entry"]},
])
def test_reroll_malformed_batch_is_422_without_writes(client, ideas, body):
    from app.db.models import Opportunity
    from app.state import db

    path = f"/workspaces/{ideas[0]}/idealab/reroll"
    response = client.post(path, json=body, headers=headers(path, "POST"))
    assert response.status_code == 422, response.text
    with db()() as session:
        assert session.get(Opportunity, "opp_alice").status == "proposed"


@pytest.mark.parametrize("previous", [None, "ops_alice"])
def test_own_reroll_preserves_seed_and_rejects_owned_opportunity(client, ideas, monkeypatch, previous):
    from app.idealab import slate

    result = slate.get_slate("ops_alice")
    generate = AsyncMock(return_value=result)
    monkeypatch.setattr(slate, "create_slate", generate)
    path = f"/workspaces/{ideas[0]}/idealab/reroll"
    response = client.post(path, headers=headers(path, "POST"), json={
        "previous_slate_id": previous,
        "reject_reasons": [{"opportunity_id": "opp_alice", "reason_code": "too_hard"}],
    })
    assert response.status_code == 201, response.text
    assert generate.call_args.kwargs["seed_input"] == result.seed
    assert generate.call_args.kwargs["round"] == 2
    assert slate.get_opportunity("opp_alice").status == "rejected"


@pytest.mark.parametrize("action", ["save", "reject"])
def test_opportunity_mutations_require_workspace_before_writing(ideas, action):
    from app.idealab import slate

    own, _foreign = ideas
    mutation = slate.save_opportunity if action == "save" else slate.reject_opportunity
    kwargs = {"reason_code": "too_hard"} if action == "reject" else {}
    with pytest.raises(slate.IdeaLabError) as exc:
        mutation("opp_bob", workspace_id=own, **kwargs)
    assert exc.value.code == "opportunity_not_found"
    assert slate.get_opportunity("opp_bob").status == "proposed"


def test_viewer_cannot_reroll(client, ideas):
    from app.db.models import WorkspaceMember
    from app.idealab import slate
    from app.state import db

    own, _foreign = ideas
    with db()() as session:
        row = session.scalar(select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == own, WorkspaceMember.person_id == "usr_alice"))
        row.role = "viewer"
        session.commit()
    path = f"/workspaces/{own}/idealab/reroll"
    response = client.post(path, headers=headers(path, "POST"), json={
        "reject_reasons": [{"opportunity_id": "opp_alice"}],
    })
    assert response.status_code == 403, response.text
    assert slate.get_opportunity("opp_alice").status == "proposed"


@pytest.mark.parametrize("scoped", [True, False])
def test_existing_bearer_obeys_block_and_unblock_on_reads_writes_and_listing(client, scoped):
    from app.db.models import ActivityLine, Approval
    from app.state import db
    from app.surfaces.auth import resolve_principal
    from app.surfaces.tokens import mint_token
    from app.workspaces.block import set_blocked

    own = make_workspace(client, "usr_alice")
    other = make_workspace(client, "usr_alice")
    foreign = make_workspace(client, "usr_bob")
    token = mint_token(person_id="usr_alice", label="fictional-rest-client",
        scopes=["read", "work:approve"], workspace_id=own if scoped else None)
    auth = {"Authorization": f"Bearer {token.token}"}
    principal = resolve_principal(auth["Authorization"])
    with db()() as session:
        session.add(ActivityLine(workspace_id=own, venture_id="ven_fictional", phase="test",
            status="done", text="Fictional stock report", kind="test", ts=utcnow()))
        session.add(Approval(id="apr_stock", workspace_id=own, kind="credit_reserve_large",
            subject_ref="fictional_stock", summary="Approve stock check", requested_by="usr_bob",
            created_at=utcnow(), expires_at=utcnow() + timedelta(days=1)))
        session.commit()
    activity = f"/api/v1/workspaces/{own}/activity"
    assert client.get(activity, headers=auth).status_code == 200
    assert client.get(f"/api/v1/workspaces/{foreign}/activity", headers=auth).status_code == 403
    assert principal.may("approve", workspace_id=own).allowed
    set_blocked(own, True, actor="adm_fictional")
    verdict = principal.may("read", workspace_id=own)
    assert not verdict.allowed and verdict.limited_by == "workspace"
    assert client.get(activity, headers=auth).status_code == 403
    decide = "/api/v1/approvals/apr_stock/decide"
    assert client.post(decide, json={"decision": "approved"}, headers=auth).status_code == 403
    items = client.get("/api/v1/workspaces", headers=auth).json()["items"]
    assert own not in {item["id"] for item in items}
    assert (other in {item["id"] for item in items}) is not scoped
    with db()() as session:
        assert session.get(Approval, "apr_stock").status == "pending"
    set_blocked(own, False, actor="adm_fictional")
    assert client.get(activity, headers=auth).status_code == 200
    assert own in {item["id"] for item in client.get("/api/v1/workspaces", headers=auth).json()["items"]}
    assert client.post(decide, json={"decision": "approved"}, headers=auth).status_code == 200


@pytest.mark.parametrize("environment, signed, allowed", [
    ("local", True, False), (" StAgInG ", False, False),
    ("production", False, False), ("preview", False, False),
    ("local", False, True),
])
@pytest.mark.parametrize("tier", ["founder", "pro", "team"])
def test_missing_provider_paid_activation_is_local_only(client, monkeypatch, environment, signed, allowed, tier):
    from app.billing.entitlements import activate_paid, current_plan
    from app.payments.errors import ProviderUnavailable

    own = make_workspace(client, "usr_alice")
    monkeypatch.setenv("PAYMENTS_ACCEPTING", "1")
    before = current_plan(workspace_id=own, person_id="usr_alice")
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1" if signed else "0")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    path = "/billing/checkout"
    auth = headers(path, "POST") if signed else {"X-Subject": "usr_alice"}
    response = client.post(path, json={"workspace_id": own, "tier": tier}, headers=auth)
    assert response.status_code == (200 if allowed else 503), response.text
    if allowed:
        assert response.json()["provider"] == "stub"
        assert current_plan(workspace_id=own, person_id="usr_alice").tier == ("pro" if tier == "founder" else tier)
    else:
        assert response.json()["detail"]["error"]["code"] == "provider_unavailable"
        assert current_plan(workspace_id=own, person_id="usr_alice") == before
        with pytest.raises(ProviderUnavailable):
            activate_paid(workspace_id=own, person_id="usr_alice", tier=tier,
                provider="stub", provider_subscription_id="stub_fictional")
        assert current_plan(workspace_id=own, person_id="usr_alice") == before


@pytest.mark.parametrize("provider", ["stripe", "razorpay"])
def test_hosted_live_checkout_does_not_activate_until_payment(client, monkeypatch, provider):
    from app.billing.entitlements import current_plan
    from app.payments.base import CheckoutHandle

    own = make_workspace(client, "usr_alice")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PAYMENTS_ACCEPTING", "1")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    if provider == "stripe":
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fictional")
        adapter = "app.payments.stripe.StripeProvider"
    else:
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fictional")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fictional_secret")
        adapter = "app.payments.razorpay.RazorpayProvider"
    handle = CheckoutHandle(provider_order_id="checkout_fictional",
        checkout_url="https://example.test/fictional-checkout", expires_at=None)
    monkeypatch.setattr(f"{adapter}.create_subscription_checkout", lambda self, **kwargs: handle)
    before = current_plan(workspace_id=own, person_id="usr_alice")
    path = "/billing/checkout"
    response = client.post(path, json={"workspace_id": own, "tier": "team"}, headers=headers(path, "POST"))
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == provider
    assert current_plan(workspace_id=own, person_id="usr_alice") == before
