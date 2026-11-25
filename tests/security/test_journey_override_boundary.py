"""Founder override cards traverse the assembled signed chat and idea APIs."""
from __future__ import annotations

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_core_signed_boundary import headers
from tests.security.test_durable_journey_boundary import request, venture


def test_chat_skip_applies_without_a_card_and_rejects_another_founder(client):
    vid, _, sid = venture(client)
    path = f"/ventures/{vid}/chat"
    message = {"message": "skip to find customers because I already know the market"}
    refused = request(client, "POST", path, "usr_bob", json=message)
    assert refused.status_code == 404, refused.text
    assert request(client, "GET", f"/ventures/{vid}/journey").json()["skipped"] == {}
    reply = request(client, "POST", path, json=message)
    assert reply.status_code == 200, reply.text
    assert reply.json()["kind"] != "confirmation"  # founder decision 2026-11-01: what they asked for just happens
    after = request(client, "GET", f"/ventures/{vid}/journey").json()
    assert after["target"] == "find_customers"
    assert after["skipped"]["research"]["who"] == "usr_alice"
    assert "already know" in after["skipped"]["research"]["why"]
    idea = request(client, "GET", f"/ventures/{vid}/idea/sessions/{sid}")
    assert idea.json()["state"] == "confirmed"
    dashboard = request(client, "GET", f"/ventures/{vid}/dashboard")
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["next_step"]["key"] == "gtm.outreach"
    assert "Lower confidence" in dashboard.json()["next_step"]["detail"]


def test_confirmed_override_rejects_mismatched_workspace_header(client):
    vid, _, _ = venture(client)
    other, other_workspace, _ = venture(client)
    assert vid != other
    path = f"/ventures/{vid}/chat/confirm"
    reply = request(client, "POST", path, json={"action": "journey.skip_step", "payload": {"step": "research", "why": "No research."}})
    assert reply.status_code == 200, reply.text
    card = reply.json()["actions"][0]["confirmation"]
    forged_headers = {**headers(path, "POST", "usr_alice"), "X-Workspace-ID": other_workspace}
    refused = client.post(path, headers=forged_headers, json={"action": "journey.skip_step", "payload": card["resulting_state"]})
    assert refused.status_code in {403, 404}, refused.text
    assert request(client, "GET", f"/ventures/{vid}/journey").json()["skipped"] == {}


def test_idea_confirmation_never_accepts_another_ventures_session(client):
    vid, _, _ = venture(client)
    other, _, other_sid = venture(client)
    assert vid != other
    result = request(client, "POST", f"/ventures/{vid}/idea/sessions/{other_sid}/confirm", json={})
    assert result.status_code == 404, result.text
