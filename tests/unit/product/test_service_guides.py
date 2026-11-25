"""Product setup guidance is concrete, scoped and honest about unverified services."""
import asyncio
import json

import pytest
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_round3_matrix import venture as venture_fixture
from tests.unit.test_autonomous_experiments import confirm_saved_idea

load_company_os_app()
venture = venture_fixture


def test_free_backend_question_has_exact_guide_without_queuing_work(venture):
    client, vid, _ = venture
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        before = set(session.scalars(select(Job.id).where(Job.kind != "idea.page_refresh")))
    response = client.post(f"/ventures/{vid}/chat", json={"message": "How do I set up a free backend database?"})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert "Supabase" in reply["body_md"] and "Neon" in reply["body_md"]
    assert any(item.get("question") is True for item in reply["citations"])
    assert {action["action"] for action in reply["actions"]} == {"service.select"}
    assert not reply.get("next_moves")
    confirm_saved_idea(vid)
    response = client.post(f"/ventures/{vid}/chat", json={"message": "How do I set up a free backend database?"})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert any(action.get("action") == "service.select" for action in reply["actions"])
    with db()() as session:
        assert set(session.scalars(select(Job.id).where(Job.kind != "idea.page_refresh"))) == before


def test_service_choice_survives_and_enters_reviewed_coding_guidance(venture):
    client, vid, _ = venture
    from app.chat.service_guides import coding_guidance
    from app.db.models import VentureDocument
    from app.state import db
    assert coding_guidance(vid) == ""
    response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "service.select", "payload": {
        "category": "backend", "provider": "supabase"}})
    assert response.status_code == 200, response.text
    body = response.json()["body_md"]
    assert "Open Connect" in body and "Settings > API Keys" in body
    assert "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY" in body
    with db()() as session:
        saved = session.get(VentureDocument, f"service-plan:{vid}")
        assert json.loads(saved.body_md) == {"backend": "supabase"}
        assert json.loads(saved.provenance_json)["verified"] is False
    instructions = coding_guidance(vid)
    assert "Supabase" in instructions and "DEPLOY.md" in instructions
    assert "row-level security" in instructions and "Do not sign up or deploy" in instructions
    assert len(instructions) <= 1800


def test_reported_product_is_separate_from_landing_and_never_verified(venture):
    client, vid, _ = venture
    from app.chat.service_guides import record_product, status
    from app.ventures.state_brief import get_venture_state_brief, render_brief_for_llm
    before = get_venture_state_brief(vid, force=True)["website"]
    record_product(vid, {"url": "https://product.example.com"})
    reply = status(vid)
    assert "unverified" in reply["body_md"] and "product.example.com" in reply["body_md"]
    brief = get_venture_state_brief(vid, force=True)
    assert brief["website"] == before
    assert brief["product_services"]["verified"] is False
    assert brief["product_services"]["product_url"] == "https://product.example.com"
    assert "app, database and sign-in unverified" in render_brief_for_llm(brief)
    response = client.post(f"/ventures/{vid}/chat", json={"message": "Show my hosted product"})
    assert response.status_code == 200
    assert "product.example.com" in response.json()["body_md"]


@pytest.mark.parametrize("url", ["https://user:password@example.com", "http://example.com",
    "https://localhost/x", "https://127.0.0.1/x", "https://app.local", "https://example.com?token=abc",
    "https://example.com/#secret", "https://example.com/sb_secret_fakecredential123456", "javascript:alert(1)"])
def test_secret_or_private_product_urls_are_not_saved(venture, url):
    _, vid, _ = venture
    from app.chat.service_guides import record_product
    from app.db.models import VentureDocument
    from app.state import db
    assert "Nothing was saved" in record_product(vid, {"url": url})["body_md"]
    with db()() as session:
        assert session.get(VentureDocument, f"hosted-product:{vid}") is None


@pytest.mark.parametrize("payload", [{"provider": [], "category": "backend"},
    {"provider": "supabase", "category": "hosting"},
    {"provider": "supabase", "category": "backend", "api_key": "value"},
    {"provider": "unknown", "category": "backend"}])
def test_malformed_or_credential_service_payload_is_rejected(venture, payload):
    _, vid, _ = venture
    from app.chat.service_guides import coding_guidance, select_service
    assert "did not save" in select_service(vid, payload)["body_md"]
    assert coding_guidance(vid) == ""


def test_guidance_states_vendor_limits_and_concrete_clicks(venture):
    _, vid, _ = venture
    from app.chat.service_guides import guide
    assert "Add new project" in guide(vid, "netlify")["body_md"]
    assert "personal, non-commercial" in guide(vid, "vercel")["body_md"]
    render = guide(vid, "render")["body_md"]
    assert "15 idle minutes" in render and "30 days" in render and "0.0.0.0" in render


@pytest.mark.parametrize("credential", ["sb_secret_fakecredential123456", "sb_publishable_fakecredential123456",
    "postgresql://demo:fakepassword123@database.example.com/app"])
@pytest.mark.parametrize("venture", ["Stock forecasting for retailers", "Recipe clubs for home cooks"], indirect=True)
def test_service_credentials_are_withheld_before_chat_persistence(venture, credential):
    _, vid, _ = venture
    from app.chat.router import handle_message
    from app.chat.store import list_messages
    reply = asyncio.run(handle_message(vid, credential))
    assert reply["kind"] == "needs_you"
    from app.core.local_mode import is_local
    if is_local():
        assert "Settings > Keys and connections" in reply["body_md"] and "Import .env" in reply["body_md"]
        assert "retry your request without the credential" in reply["body_md"]
        assert any(action.get("label") == "Open Keys and connections" and action.get("href") == "/settings/local"
                   for action in reply["actions"])
    else:
        assert "supported secure credential field on Connections" in reply["body_md"]
        assert "no credential form was opened from chat" in reply["body_md"]
        assert any(action.get("label") == "Open Connections" and "connections" in action.get("href", "")
                   for action in reply["actions"])
    assert all(credential not in str(message) for message in list_messages(vid, limit=30))


@pytest.mark.parametrize("text", ["Is my app deployed on Vercel?", "Build my app with Supabase",
                                    "Research Supabase alternatives", "Do not set up Supabase", "What is my deployment status?",
                                    "Show my Supabase connection status"])
def test_setup_guide_does_not_swallow_build_status_research_or_negation(text):
    from app.chat.service_guides import matches
    assert not matches(text)


def test_static_guidance_keeps_backend_as_future_work_without_source_scaffolding(venture):
    from app.chat.service_guides import coding_guidance, select_service
    _, vid, _ = venture
    select_service(vid, {'category': 'backend', 'provider': 'supabase'})
    select_service(vid, {'category': 'hosting', 'provider': 'vercel'})
    text = coding_guidance(vid, artifact_profile='static_landing_v1')
    assert 'Supabase' in text and 'Vercel' in text and 'DEPLOY.md only' in text
    assert 'future product work only' in text
    assert 'Do not add .env files, database migrations, backend services, authentication' in text
    assert 'non-commercial' in text and len(text) <= 1800
    assert 'Provide reviewed database migrations' not in text
    generic = coding_guidance(vid)
    assert 'Provide reviewed database migrations' in generic and '.env.example' in generic
