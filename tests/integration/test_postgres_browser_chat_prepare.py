"""Qualified pending reviews through actual signed chat/voice; never launch or approve."""
import asyncio
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.unit.product.test_device_browser_ai import configure

GOAL = "Open https://example.test/ and inspect the main headline in the browser."


@pytest.fixture()
def prepared_context(journey_db, monkeypatch):
    from app.admin.authn import CallerContext
    from app.build import device_browser_ai as ai
    from app.chat.conversations import create
    from app.db.supymem_models import DesktopDevice
    from app.tenancy import bind
    from core.clock import utcnow
    d = journey_db
    profile = configure(monkeypatch)
    monkeypatch.setenv("SUPYMEM_CHAT_BROWSER_PREP_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_CHAT_BROWSER_PREP_PROFILE_DIGEST", ai.profile_digest(profile))
    monkeypatch.setenv("SUPYMEM_VOICE_ENABLED", "1")
    monkeypatch.delenv("DEVICE_DESKTOP_EXECUTION_ENABLED", raising=False)
    monkeypatch.setattr(ai, "_provider_call", lambda *_: pytest.fail("Preparation may not call a provider"))
    async def no_router(*_):
        pytest.fail("Explicit browser preparation may not dispatch a routing model")
    monkeypatch.setattr("app.chat.llm_router.route", no_router)
    monkeypatch.setattr("app.chat.link_context._safe_host", lambda _: True)
    with d["factory"]() as session:
        session.add(DesktopDevice(id="dev_" + d["vid"], person_id=d["actor"], name="Originating test Mac", app_version="0.1.84", last_seen_at=utcnow()))
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        conversation = create(d["vid"], idempotency_key="qualified-browser")
    caller = CallerContext(d["actor"], "", "user", "/test", "POST", 9999999999, "fixture", device_id="dev_" + d["vid"])
    d.update(profile=profile, caller=caller, conversation=conversation["id"])
    return d


def run_tool(d, caller=None, goal=GOAL, conversation=None):
    from app.chat.browser_prepare import bind_caller
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind
    from app.tools import ToolContext, execute_tool
    ctx = ToolContext(venture_id=d["vid"], workspace_id=d["wid"])
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), bind_conversation(d["vid"], conversation or d["conversation"]), bind_caller(caller or d["caller"]):
        result = asyncio.run(execute_tool("prepare_browser_review", {"goal": goal}, ctx))
    return result, ctx.actions


def test_actual_pending_order_and_same_conversation_replay_without_native_scope_or_spend(prepared_context):
    from app.db.models import CreditReservation, VentureDocument
    from app.build.device_orders import KIND
    d = prepared_context
    result, actions = run_tool(d)
    assert not result.startswith("error:"), result
    assert "read and interact" in result and "not a no-mutation" in result
    assert len(actions) == 1
    assert run_tool(d)[1] == actions
    with d["factory"]() as session:
        rows = list(session.scalars(select(VentureDocument).where(VentureDocument.venture_id == d["vid"], VentureDocument.kind == KIND)))
        assert len(rows) == 1
        value = json.loads(rows[0].provenance_json)
        assert value["state"] == "pending" and value["device_id"] == d["caller"].device_id
        assert value["browser_chat_scope"]["conversation_id"] == d["conversation"]
        assert value["manifest"]["applications"] == ["com.google.Chrome"]
        assert value["manifest"]["actions"] == ["browser.read", "browser.interact"]
        assert not value["manifest"].get("coding_verification") and not value["manifest"].get("desktop_verification")
        assert not session.scalar(select(CreditReservation.id).where(CreditReservation.workspace_id == d["wid"]))


@pytest.mark.parametrize("failure", ["off", "pin", "no_device", "foreign_device", "revoked", "old_runner", "readonly", "coding", "native", "credentials", "url", "archived", "device_owner"])
def test_gate_or_scope_refusal_never_prepares(prepared_context, monkeypatch, failure):
    from app.db.models import VentureDocument
    from app.db.supymem_models import DesktopDevice
    from app.build.device_orders import KIND
    from core.clock import utcnow
    d, caller, goal = prepared_context, prepared_context["caller"], GOAL
    if failure == "off":
        monkeypatch.setenv("SUPYMEM_CHAT_BROWSER_PREP_ENABLED", "0")
    if failure == "pin":
        monkeypatch.setenv("SUPYMEM_CHAT_BROWSER_PREP_PROFILE_DIGEST", "0" * 64)
    if failure == "no_device":
        caller = replace(caller, device_id=None)
    if failure == "foreign_device":
        caller = replace(caller, device_id="dev_unknown")
    if failure in {"revoked", "old_runner"}:
        with d["factory"]() as session:
            device = session.get(DesktopDevice, caller.device_id)
            if failure == "revoked":
                device.revoked_at = utcnow()
            else:
                device.app_version = "0.1.83"
            session.commit()
    if failure == "archived":
        from app.db.models import Venture
        with d["factory"]() as session:
            session.get(Venture, d["vid"]).archived_at = utcnow()
            session.commit()
    if failure == "device_owner":
        from app.authz.workspace import ensure_subject_person
        other = ensure_subject_person("usr_browser_foreign")
        with d["factory"]() as session:
            session.get(DesktopDevice, caller.device_id).person_id = other
            session.commit()
    if failure == "readonly":
        goal += " Read only; no changes."
    if failure == "coding":
        goal = "Use Codex to build the website at https://example.test/"
    if failure == "native":
        goal = "Use my Mac to open TextEdit and read https://example.test/"
    if failure == "credentials":
        goal = "Open https://user:password@example.test/ and inspect it."
    if failure == "url":
        goal = "Open http://127.0.0.1/ and inspect it."
    result, actions = run_tool(d, caller, goal)
    assert result.startswith("error:") and actions == [], result
    with d["factory"]() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == KIND, VentureDocument.venture_id == d["vid"]))


def signed_client(d, monkeypatch):
    from core.assertion import sign_assertion
    secret = "qualified-browser-signed-test-more-than-thirty-two"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "qualified-browser-vault-fixture-more-than-32bytes")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "offline-ops@example.test")
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
    from app.main import create_app
    client = TestClient(create_app())
    def post(path, body, *, device=None, actor=None):
        return client.post(path, json=body, headers={"X-COS-Assertion": sign_assertion(secret,
            sub=actor or d["actor"], email="", role="user", path=path, method="POST", device_id=device or d["caller"].device_id)})
    return post


def test_signed_chat_uses_actual_originating_caller_and_saves_review_in_private_conversation(prepared_context, monkeypatch):
    from app.chat.store import list_messages
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind
    d = prepared_context
    post = signed_client(d, monkeypatch)
    path = f"/ventures/{d['vid']}/chat"
    result = post(path, {"message": GOAL, "conversation_id": d["conversation"], "idempotency_key": "browser-chat-one"})
    assert result.status_code == 200, result.text
    assert result.json()["actions"][0]["href"].startswith(f"/v/{d['vid']}/device-work-orders/")
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), bind_conversation(d["vid"], d["conversation"]):
        rows = list_messages(d["vid"])
        assert any(r.get("actions") for r in rows)


def test_signed_voice_rejects_unsupported_prepare_and_generic_confirmation_without_writes(prepared_context, monkeypatch):
    from app.db.models import AnalyticsEvent
    from core.clock import utcnow
    d = prepared_context
    post = signed_client(d, monkeypatch)
    eid = "ane_" + d["vid"]
    with d["factory"]() as session:
        session.add(AnalyticsEvent(id=eid, event_name="voice.session_started", person_id=d["actor"], workspace_id=d["wid"],
            venture_id=d["vid"], occurred_at=utcnow(), props_json="{}"))
        session.commit()
    body = {"session_event_id": eid, "turn_id": "turn_browser", "call_id": "call_browser", "arguments": json.dumps({"goal": GOAL}), "conversation_id": d["conversation"]}
    path = f"/ventures/{d['vid']}/voice/tools/prepare_browser_review"
    result = post(path, body)
    assert result.status_code == 403, result.text
    assert result.json()["detail"]["error"]["code"] == "voice_cannot_confirm"
    assert post(path, body).json() == result.json()
    generic = f"/ventures/{d['vid']}/device-work-orders/from-chat"
    rejected = post(generic, {"instruction": GOAL, "source": "voice"})
    assert rejected.status_code == 403
    from app.db.models import VentureDocument, CreditReservation
    from app.build.device_orders import KIND
    with d["factory"]() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == KIND, VentureDocument.venture_id == d["vid"]))
        assert not session.scalar(select(CreditReservation.id).where(CreditReservation.workspace_id == d["wid"]))


def test_concurrent_prepares_share_one_review_and_another_conversation_cannot_retarget_it(prepared_context):
    from concurrent.futures import ThreadPoolExecutor
    from app.chat.conversations import create
    from app.db.models import VentureDocument
    from app.tenancy import bind
    from app.build.device_orders import KIND
    d = prepared_context
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_tool(d), range(2)))
    assert all(not result.startswith("error:") for result, _ in results), results
    assert results[0][1] == results[1][1]
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        other = create(d["vid"], idempotency_key="another-browser-conversation")
    rejected, actions = run_tool(d, conversation=other["id"])
    assert rejected == "error: browser_review_exists_in_another_scope" and not actions
    with d["factory"]() as session:
        rows = list(session.scalars(select(VentureDocument).where(VentureDocument.kind == KIND, VentureDocument.venture_id == d["vid"])))
        assert len(rows) == 1
        assert json.loads(rows[0].provenance_json)["browser_chat_scope"]["conversation_id"] == d["conversation"]


def test_lost_save_ack_preserves_atomic_conversation_custody_and_recovers_same_review(prepared_context, monkeypatch):
    from app.build import device_orders as orders
    from app.chat.conversations import create
    from app.db.models import VentureDocument
    from app.tenancy import bind
    d = prepared_context
    original = orders.prepare_general_browser_from_chat
    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("fixture lost acknowledgment after durable save")
    monkeypatch.setattr(orders, "prepare_general_browser_from_chat", lost_ack)
    result, actions = run_tool(d)
    assert result == "error: browser_preparation_unconfirmed" and not actions
    with d["factory"]() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.kind == orders.KIND, VentureDocument.venture_id == d["vid"]))
        value = json.loads(row.provenance_json)
        assert value["browser_chat_scope"]["conversation_id"] == d["conversation"]
        assert value["review_digest"] == orders._digest(value)
        original_id = row.id
    monkeypatch.setattr(orders, "prepare_general_browser_from_chat", original)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        other = create(d["vid"], idempotency_key="lost-ack-other-conversation")
    assert run_tool(d, conversation=other["id"])[1] == []
    recovered, actions = run_tool(d)
    assert not recovered.startswith("error:") and actions[0]["href"].endswith("/" + original_id)
    with d["factory"]() as session:
        assert len(list(session.scalars(select(VentureDocument).where(VentureDocument.kind == orders.KIND, VentureDocument.venture_id == d["vid"])))) == 1


def test_server_only_conversation_argument_must_match_current_private_actor_context(prepared_context):
    from app.build.device_orders import prepare_general_browser_from_chat, KIND
    from app.chat.conversations import create, bind_conversation
    from app.db.models import VentureDocument
    from app.tenancy import bind
    d = prepared_context
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        other = create(d["vid"], idempotency_key="wrong-current-conversation")
        with bind_conversation(d["vid"], d["conversation"]), pytest.raises(ValueError, match="device_browser_conversation_scope_invalid"):
            prepare_general_browser_from_chat(d["vid"], d["caller"], GOAL, d["profile"], conversation_id=other["id"])
    with d["factory"]() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == KIND, VentureDocument.venture_id == d["vid"]))


def test_clicked_approval_keeps_real_hold_and_repeat_preparation_does_not_claim_or_misreport_pending(prepared_context):
    from app.build import device_orders as orders
    from app.db.models import CreditAccount, CreditReservation, VentureDocument
    from core.clock import utcnow
    d = prepared_context
    result, actions = run_tool(d)
    assert not result.startswith("error:")
    with d["factory"]() as session:
        value = json.loads(session.scalar(select(VentureDocument).where(VentureDocument.kind == orders.KIND, VentureDocument.venture_id == d["vid"])).provenance_json)
        session.add(CreditAccount(id="cra_" + d["vid"], workspace_id=d["wid"], venture_id=d["vid"], scope="venture",
            granted_micro=1_000_000, spent_micro=0, reserved_micro=0, status="active", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    approved = orders.transition(d["vid"], d["caller"], value["id"], value["review_digest"], "approve")
    assert approved["state"] == "approved" and approved["review_digest"] == orders._digest(approved)
    repeated, current_actions = run_tool(d)
    assert "already has a saved review" in repeated and "pending" not in repeated
    assert current_actions == actions
    with d["factory"]() as session:
        saved = json.loads(session.get(VentureDocument, value["id"]).provenance_json)
        account = session.get(CreditAccount, "cra_" + d["vid"])
        assert saved["state"] == "approved" and not saved.get("claimed_at")
        assert account.spent_micro == 0 and account.reserved_micro == 500_000
        assert len(list(session.scalars(select(CreditReservation).where(CreditReservation.workspace_id == d["wid"])))) == 1
