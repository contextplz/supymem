"""Host my product: routing, choices, guided plan, Core's own URL check, secrets and recovery."""
import asyncio
import json

import pytest
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_round3_matrix import venture as venture_fixture
from tests.unit.test_autonomous_experiments import confirm_saved_idea

load_company_os_app()


@pytest.fixture
def venture(tmp_path, monkeypatch, request):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    yield from venture_fixture.__wrapped__(tmp_path, monkeypatch, request)


GOOD = (200, "https://shop.vercel.app", "<html><head><title>Volunteer Calendar</title></head><body>hi</body></html>")


def _chat(client, vid, message):
    response = client.post(f"/ventures/{vid}/chat", json={"message": message})
    assert response.status_code == 200, response.text
    return response.json()


def _act(client, vid, action, payload):
    response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": action, "payload": payload})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("text,expected_actions", [
    ("host my product", {"pagebuilder.build", None}), ("Deploy it", {"pagebuilder.build", None}),
    ("put it online please", {"pagebuilder.build", None}), ("make my app live", {"pagebuilder.build", None}),
])
@pytest.mark.parametrize("venture", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"], indirect=True)
def test_hosting_intent_uses_local_publishing_setup_without_queuing_work(venture, text, expected_actions):
    client, vid, _ = venture
    from app.chat.product_build_request import is_product_build_request
    from app.core.local_secrets import update_settings
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        before = set(session.scalars(select(Job.id).where(Job.kind != "idea.page_refresh")))
    assert is_product_build_request(text) is False
    reply = _chat(client, vid, text)
    assert "In Settings > Capabilities, switch on Publish pages." in reply["body_md"]
    assert {action.get("action") for action in reply["actions"]} == expected_actions
    assert any(action.get("href") == "/settings/local" for action in reply["actions"])
    assert not reply.get("next_moves")
    confirm_saved_idea(vid)
    update_settings("capabilities", {"publish_pages": True}, {})
    reply = _chat(client, vid, text)
    assert {action.get("action") for action in reply["actions"]} == expected_actions
    assert "Settings > Keys and connections" in reply["body_md"]
    assert ("export" if "pagebuilder.build" in expected_actions else "Your AI provider") in reply["body_md"]
    with db()() as session:
        assert set(session.scalars(select(Job.id).where(Job.kind != "idea.page_refresh"))) == before


@pytest.mark.parametrize("venture", ["Inspection scheduling for property managers", "Pottery weekends for families"], indirect=True)
def test_local_hosting_uses_your_saved_publishing_account_without_backend_or_coding_orders(venture):
    client, vid, _ = venture
    from app.core.local_secrets import update_settings
    backend_card = _act(client, vid, "host.choose", {"category": "hosting", "provider": "vercel"})
    assert any(item.get("question") is True for item in backend_card["citations"])
    assert {action.get("action") for action in backend_card["actions"]} == {"pagebuilder.build", None}
    assert not backend_card.get("next_moves")
    confirm_saved_idea(vid)
    update_settings("capabilities", {"publish_pages": True}, {})
    backend_card = _act(client, vid, "host.choose", {"category": "hosting", "provider": "vercel"})
    assert "Settings > Keys and connections" in backend_card["body_md"]
    update_settings("publishing", {"provider": "vercel", "project": "your-project"}, {"token": "your-private-publishing-token"})
    plan = _act(client, vid, "host.choose", {"category": "backend", "provider": "supabase"})
    body = plan["body_md"]
    assert "Vercel" in body and "saved settings" in body
    assert {action.get("action") for action in plan["actions"]} == {"pagebuilder.build", "site.publish"}
    assert not any(action.get("kind") == "secret_form" for action in plan["actions"])
    assert "your-private-publishing-token" not in json.dumps(plan)
    assert not plan.get("next_moves")


def test_unknown_or_credential_payloads_are_rejected(venture):
    _, vid, _ = venture
    from app.chat import hosting
    for payload in ({"category": "hosting", "provider": "supabase"}, {"category": "backend", "provider": "vercel"},
                    {"category": "hosting", "provider": "vercel", "api_key": "x"}, {"category": "x", "provider": "y"}):
        assert "Nothing was saved" in hosting.choose(vid, payload)["body_md"]


def test_supabase_keys_are_stored_through_the_slot_and_never_echoed(venture, tmp_path, monkeypatch):
    client, vid, ws = venture
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(tmp_path / "secrets.env"))
    from app.secrets.service import save_secret
    anon = "eyJhbGciOiJIUzI1NiJ9.fakeanonkeyvalue1234567890"
    url = "https://abcdefgh.supabase.co"
    saved = save_secret("supabase.project", {"NEXT_PUBLIC_SUPABASE_URL": url, "NEXT_PUBLIC_SUPABASE_ANON_KEY": anon},
                        workspace_id=ws)
    assert anon not in json.dumps(saved) and saved["status"] == "armed"
    _act(client, vid, "host.choose", {"category": "hosting", "provider": "vercel"})
    plan = _act(client, vid, "host.choose", {"category": "backend", "provider": "supabase"})
    assert "In Settings > Capabilities, switch on Publish pages." in plan["body_md"]
    assert anon not in json.dumps(plan)
    assert not any(a.get("kind") == "secret_form" for a in plan["actions"])
    assert anon not in json.dumps(plan) and url not in json.dumps(plan)


def _fake(monkeypatch, result):
    from app.chat import hosting
    def fake(url):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(hosting, "_get", fake)


def test_verified_hosted_product_is_recorded_shown_and_moves_next_step_on(venture, monkeypatch):
    client, vid, _ = venture
    from app.chat.recovery import external_site
    from app.chat.service_guides import snapshot
    from app.state import db
    from app.ventures.next_step import _facts
    from app.ventures.state_brief import get_venture_state_brief, render_brief_for_llm
    _act(client, vid, "host.choose", {"category": "hosting", "provider": "vercel"})
    _fake(monkeypatch, GOOD)
    reply = _chat(client, vid, "verify my product https://shop.vercel.app")
    assert "HTTP 200" in reply["body_md"] and "Volunteer Calendar" in reply["body_md"]
    with db()() as session:
        saved = snapshot(session, vid)
    assert saved["verified"] and saved["product_url"] == GOOD[1] and saved["verified_at"]
    site = external_site(vid)
    assert site["verified"] is True and "verified by Supymem" in site["note"]
    dash = client.get(f"/ventures/{vid}/dashboard")
    if dash.status_code == 200:
        assert (dash.json().get("tiles", {}).get("external_site") or {}).get("verified") is True
    # a verified hosted product replaces the page build/publish steps, so the journey moves on to go-to-market
    assert _facts(vid)["external_site"] is True
    brief = get_venture_state_brief(vid, force=True)
    assert "verified by Supymem" in render_brief_for_llm(brief)
    assert "verified" in _chat(client, vid, "Show my hosted product")["body_md"]


@pytest.mark.parametrize("result,reason", [
    ((404, "https://x.vercel.app", "<title>404: NOT_FOUND</title>"), "HTTP 404"),
    ((200, "https://x.vercel.app", "<title>My app</title> DEPLOYMENT_NOT_FOUND"), "error or login"),
    ((200, "https://x.vercel.app", "<title>Log in to Vercel</title>"), "error or login"),
    ((200, "https://x.vercel.app", "<html>no title</html>"), "no title"),
    (ValueError("not_public_https"), "could not open"),
])
def test_failed_check_is_founder_reported_with_do_it_another_way(venture, monkeypatch, result, reason):
    client, vid, _ = venture
    from app.chat.service_guides import snapshot
    from app.state import db
    _fake(monkeypatch, result)
    reply = _chat(client, vid, "verify my product https://x.vercel.app")
    assert reason in reply["body_md"] and "not verified" in reply["body_md"]
    assert any(a.get("action") == "recovery.alternative" and a["payload"]["step"] == "host.deploy" for a in reply["actions"])
    with db()() as session:
        saved = snapshot(session, vid)
    assert saved["verified"] is False and saved["state"] == "founder_reported"


def test_private_urls_never_fetched(venture, monkeypatch):
    _, vid, _ = venture
    from app.chat import hosting
    _fake(monkeypatch, AssertionError("must not fetch"))
    assert "Nothing was saved" in hosting.verify(vid, "https://localhost/x")["body_md"]
    from core.research_web.guard import check_url
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))])
    assert check_url("https://localhost")


def test_diy_recovery_guide_and_submit_verify_the_pasted_address(venture, monkeypatch):
    client, vid, _ = venture
    from app.chat import recovery
    guide = asyncio.run(recovery.alternative(vid, {"step": "host.deploy"}))
    assert "vercel.com/new" in guide["body_md"] and "Deploy" in guide["body_md"]
    _fake(monkeypatch, GOOD)
    done = recovery.submit(vid, {"step": "host.deploy", "url": "https://shop.vercel.app"})
    assert "verified" in done["body_md"] or "I checked it myself" in done["body_md"]
    missing = recovery.submit(vid, {"step": "host.deploy", "text": "done"})
    assert "web address" in missing["body_md"]


def test_failure_message_for_host_failed_gets_recovery_button():
    from app.chat import recovery
    assert recovery.KIND_STEP["host_failed"] == "host.deploy"
    assert recovery.STEPS["host.deploy"]["feature"] == "hosting"
    assert any(a["action"] == recovery.ALTERNATIVE for a in recovery.decorate([], "host_failed", None))


def test_events_and_actions_are_registered_in_feature_catalog():
    from app.analytics.feature_catalog import ACTION_SLUG, FEATURES
    assert "hosting" in FEATURES
    assert {ACTION_SLUG[a] for a in ("host.choose", "host.plan", "host.verify")} == {"hosting"}


def test_mac_task_finished_after_coding_build_offers_hosting(venture):
    _, vid, _ = venture
    from app.chat.mac_stop import post_mac_finished
    from app.chat.store import list_messages
    assert post_mac_finished(vid, "ord_1", "Build approved task with Claude")
    last = list_messages(vid, limit=5)[-1]
    assert any(a.get("action") == "host.plan" for a in last["actions"])
