"""Unit tests for Community Department thesis gates + scoring."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)

from app.community.identity import generate_identity, objective_mix
from app.community.plan import research_from_payload
from app.community.program import generate_program_30d
from app.community.thesis import (
    evaluate_thesis,
    sample_build_research,
    sample_dont_build_research,
)
from app.marketing.router import route


def test_build_owned_fixture_passes_gates_and_scores_high():
    thesis = evaluate_thesis(sample_build_research())
    assert all(g.passed for g in thesis.gates), [g for g in thesis.gates if not g.passed]
    assert thesis.thesis_score >= 70
    assert thesis.verdict == "build_owned"
    assert thesis.motion == "invite_only_founding_50"
    assert "Build because" in thesis.because


def test_dont_build_fixture_fails_closed():
    thesis = evaluate_thesis(sample_dont_build_research())
    assert thesis.verdict != "build_owned"
    assert any(not g.passed for g in thesis.gates)
    assert thesis.motion in {
        "founder_led_content",
        "customer_slack_cab",
        "founder_office_hours",
        "watering_hole_residency",
    }
    assert "Don't build" in thesis.because or "Don't build yet" in thesis.because


def test_identity_and_program_only_meaningful_on_build():
    research = sample_build_research()
    thesis = evaluate_thesis(research)
    objs = objective_mix(thesis, research)
    identity = generate_identity(thesis, research, objs)
    program = generate_program_30d(thesis, research, identity, objs)
    assert "ForecastForge Community" != identity.name
    assert len(identity.rules) == 5
    assert program.public_launch is False
    assert program.staff_post_share_min >= 0.7
    assert program.invites_target >= 25
    assert (
        objs.education
        + objs.networking
        + objs.product_adoption
        + objs.acquisition
        == 100
    )


def test_research_from_brief_shape():
    bundle = research_from_payload(
        {
            "brief": {
                "product_name": "X",
                "icp": {
                    "segments": {
                        "ops": {
                            "role": "RevOps lead",
                            "pains": ["weekly forecast"],
                        }
                    }
                },
            },
            "paying_customers": 40,
        }
    )
    assert bundle["product_name"] == "X"
    assert bundle["icp_segments"][0]["role"] == "RevOps lead"
    assert bundle["paying_customers"] == 40


def test_discord_fail_closed_without_key(monkeypatch):
    from app.community import composio_discord as discord
    from app.core.local_mode import is_local

    if is_local():
        monkeypatch.setenv("COMPOSIO_API_KEY", "ignored-in-local-mode")
    else:
        monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.setattr(discord, "_request", lambda *args, **kwargs: pytest.fail("Discord must not send without the owner's key"))
    st = discord.needs_you_status("ws_test")
    assert st["composio_configured"] is False
    assert st["can_create_guild"] is False
    code = "discord_settings_required" if is_local() else "composio_api_key_missing"
    blocker = next(blocker for blocker in st["blockers"] if blocker["code"] == code)
    if is_local():
        assert "Settings > Keys and connections > Your connected apps" in blocker["message"]
    out = discord.run_lab_action(
        "ws_test",
        "send_message",
        {"channel_id": "1", "content": "x", "confirm": True},
    )
    assert out["ok"] is False
    assert out["status"] == "blocked"


@pytest.fixture
def configured_discord(monkeypatch):
    from app.community import composio_discord as discord
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("composio", {"auth_configs": {"discordbot": "ac_discord_fixture"}}, {"api_key": "test-key-not-real"})
        update_settings("capabilities", {"connected_apps": True}, {})
    else:
        monkeypatch.setenv("COMPOSIO_API_KEY", "test-key-not-real")
    return discord


def test_active_connection_ignores_initializing(monkeypatch, configured_discord):
    discord = configured_discord

    def _fake_list(_workspace_id=None):
        return [{"id": "ca_pending", "status": "INITIALIZING"}]

    monkeypatch.setattr(discord, "list_connections", _fake_list)
    assert discord.active_connection("ws") is None
    st = discord.connection_status("ws")
    assert st["connected"] is False
    assert st["needs_you"] is True
    assert "INITIALIZING" in (st.get("needs_you_reason") or "")


def test_active_connection_accepts_active(monkeypatch, configured_discord):
    discord = configured_discord

    def _fake_list(_workspace_id=None):
        return [
            {"id": "ca_pending", "status": "INITIALIZING"},
            {"id": "ca_ok", "status": "ACTIVE"},
        ]

    monkeypatch.setattr(discord, "list_connections", _fake_list)
    active = discord.active_connection("ws")
    assert active and active["id"] == "ca_ok"
    st = discord.connection_status("ws")
    assert st["connected"] is True
    assert st["needs_you"] is False



def test_router_routes_community_department_kinds():
    for kind in (
        "community.evaluate_thesis",
        "community.generate_identity",
        "community.provision_discord",
    ):
        decision = route({"kind": kind, "title": kind, "payload": {}})
        assert decision["adapter"] == "community_department"
        assert decision["lane"] == "code"


def test_reddit_community_adapter_still_routed():
    decision = route(
        {
            "kind": "draft_reply",
            "title": "draft",
            "payload": {},
            "tools_allowed": [],
        }
    )
    assert decision["adapter"] == "community"
